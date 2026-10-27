# 任务执行追踪：记录阶段、模型、工具、步骤事件及最终汇总，帮助定位失败原因。
# ContextVar 传递当前任务和调用标签；写入前替换明确凭据并限制事件和文件大小。
# 文件输出、框架日志桥接和 Agent 回调都在本模块实现，追踪失败不阻断抓取任务。
"""Task-local, bounded observability. No browser-use or CLI dependency."""

import functools
import json
import logging
import math
import re
import sys
import threading
import time
import traceback
import uuid
from collections import Counter
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import datetime, timezone
from pathlib import Path
from itertools import islice


# 追踪器、关联标签和模型用量槽分别按执行上下文隔离，避免并发任务串日志。
_current = ContextVar("autoscraper_trace", default=None)
_labels = ContextVar("autoscraper_trace_labels", default={})
_usage = ContextVar("autoscraper_trace_usage", default=None)


# 取得当前上下文中的追踪器；未启用追踪时返回 None。
def current_trace():
    return _current.get()


# 临时叠加轮次、步骤和调用关联标签，离开作用域后恢复外层上下文。
@contextmanager
def labels(**values):
    token = _labels.set({**_labels.get(), **values})
    try:
        yield
    finally:
        _labels.reset(token)


# 向当前任务发出事件；无追踪器时直接忽略，业务代码无需反复判断。
def emit(event_type, **payload):
    trace = current_trace()
    if trace is not None:
        trace.emit(event_type, payload)


# 生成统一的 UTC ISO 时间戳，用于事件和汇总中的绝对时间。
def utc_now():
    return datetime.now(timezone.utc).isoformat()


# 摘取模型自报的记忆、下一目标和上轮评价；这些文字不代表独立验证事实。
def agent_assessment(output):
    """Reported model summaries, not independently verified observations."""
    return {
        key: value
        for key in ("memory", "next_goal", "evaluation_previous_goal")
        if (value := getattr(output, key, None)) is not None
    }


# 保留有界的工具诊断和 DOM 样本，递归处理等待工具嵌套的 DOM 证据。
# 样本缺少某节点不能证明整页不存在该节点，摘要会保留这一范围限制。
def summarize_tool_output(content):
    """Keep structured feedback and bounded DOM evidence, not the entire DOM."""
    if not content:
        return None
    if len(content) > 100000:
        return {"excerpt": content[:4096], "truncated": True, "chars": len(content)}
    try:
        data = json.loads(content)
    except (ValueError, TypeError, RecursionError):
        return {"excerpt": content[:4096], "truncated": len(content) > 4096}
    if not isinstance(data, dict):
        return data
    data = dict(data)
    if isinstance(data.get("nodes"), list):
        nodes = [node for node in data["nodes"] if isinstance(node, dict)]
        controls = [
            node
            for node in nodes
            if str(node.get("tag", "")).lower() in {"a", "button", "input", "select"}
        ]
        data = {key: data[key] for key in ("url", "title", "error", "total_nodes") if key in data}
        data.update(
            observed_nodes=len(nodes),
            observed_controls=len(controls),
            nodes_sample=nodes[:8],
            controls_sample=controls[:12],
            scope="sampled inspection root; absence in samples does not prove page-wide absence",
        )
    if isinstance(data.get("dom"), str):
        data["dom_evidence"] = summarize_tool_output(data.pop("dom"))
    return data


# 逐级缩小长文本和列表，使单个事件尽量落在字节上限内并保留诊断键。
def bounded_payload(data):
    """Retain diagnostic keys when limiting a large event instead of discarding it."""

    # 递归截断字符串及列表，保留字典结构和被裁剪的数量提示。
    def shrink(value, text_limit, list_limit):
        if isinstance(value, str) and len(value) > text_limit:
            return value[:text_limit] + "…[truncated]"
        if isinstance(value, dict):
            return {k: shrink(v, text_limit, list_limit) for k, v in value.items()}
        if isinstance(value, list):
            result = [shrink(v, text_limit, list_limit) for v in value[:list_limit]]
            if len(value) > list_limit:
                result.append({"truncated_items": len(value) - list_limit})
            return result
        return value

    for text_limit, list_limit in ((4096, 50), (1024, 10), (256, 3)):
        if len(json.dumps(data, ensure_ascii=False).encode()) <= 12000:
            return data
        data = {**shrink(data, text_limit, list_limit), "truncated": True}
    encoded = json.dumps(data, ensure_ascii=False)
    return (
        data if len(encoded.encode()) <= 12000 else {"truncated": True, "excerpt": encoded[:2000]}
    )


# 追踪数据清理器：替换已注册密钥和明确凭据，并限制结构深度、节点数和文本长度。
# 普通 URL 参数、路径和页面文本仍保留，不提供通用敏感内容识别。
class Sanitizer:
    """Preserve diagnostics; replace only known keys and explicit credentials.

    This is deliberately not a general sensitive-content detector. Paths, query
    parameters, CSS literals and ordinary text remain useful diagnostic records.
    """

    CREDENTIAL_KEYS = {
        "apikey",
        "authorization",
        "proxyauthorization",
        "cookie",
        "setcookie",
        "password",
        "passwd",
        "secret",
        "clientsecret",
        "accesstoken",
        "refreshtoken",
        "idtoken",
        "token",
    }
    NUMERIC = {"prompt_tokens", "completion_tokens", "total_tokens", "prompt_cached_tokens"}

    # 保存调用方明确注册的非空凭据，后续即使凭据嵌在其他文本里也会被替换。
    def __init__(self, secrets=()):
        self.secrets = tuple(value for value in secrets if value)

    # 清理文本中的显式密钥、URL 凭据、认证头和控制字符，再限制长度。
    def text(self, value):
        for secret in self.secrets:
            value = value.replace(secret, "[redacted]")
        value = re.sub(r"(?i)(https?://)[^/\s?#]*@", r"\1", value)
        value = re.sub(
            r"(?i)([?&](?:api[_-]?key|access_token|refresh_token|id_token|token|password|client_secret)=)[^&#\s\"'<>]*",
            r"\1[redacted]",
            value,
        )
        value = re.sub(
            r"(?i)(\b(?:Proxy-)?Authorization\s*[:=]\s*(?:Bearer|Basic)\s+)[^\s,'\";}]+",
            r"\1[redacted]",
            value,
        )
        value = re.sub(r"(?im)(\b(?:set-cookie|cookie)\s*:\s*)[^\r\n]+", r"\1[redacted]", value)
        value = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f\ud800-\udfff]", " ", value)
        return value if len(value) <= 4096 else value[:4096] + "…[truncated]"

    # 递归生成可 JSON 序列化的有界副本；凭据键替换、非有限浮点数转为 None。
    # budget 用单元素列表在所有递归分支共享，防止宽结构绕过总节点上限。
    def clean(self, value, key="", depth=0, budget=None):
        budget = [500] if budget is None else budget
        budget[0] -= 1
        if depth > 8 or budget[0] < 0:
            return "[depth limit]"
        if key.lower().replace("_", "").replace("-", "") in self.CREDENTIAL_KEYS and isinstance(
            value, str
        ):
            return "[redacted]"
        if isinstance(value, float) and not math.isfinite(value):
            return None
        if value is None or isinstance(value, (bool, int, float)):
            return value
        if isinstance(value, str):
            return self.text(value)
        if isinstance(value, dict):
            result = {
                self.text(str(k)): self.clean(v, str(k), depth + 1, budget)
                for k, v in islice(value.items(), 50)
                if budget[0] > 0
            }
            if len(result) < len(value):
                result["_trace_truncated_items"] = len(value) - len(result)
            return result
        if isinstance(value, (list, tuple)):
            result = [self.clean(v, key, depth + 1, budget) for v in value[:50] if budget[0] > 0]
            if len(result) < len(value):
                result.append({"_trace_truncated_items": len(value) - len(result)})
            return result
        return {"type": type(value).__name__}


# 每次运行一个目录的文件消费者，分别写事件、各轮 History 和最终汇总。
class FileSink:
    """One run directory; flushed appends, finite total byte budget, no buffers."""

    # 配置根目录与总字节预算；实际运行目录在 start 时创建。
    def __init__(self, root=Path(".agent-runtime/runs"), *, max_bytes=20_000_000):
        self.root = Path(root)
        self.max_bytes = max_bytes
        self.written = 0
        self.directory = None

    # 按本地开始时间和 run_id 创建独立目录及 History 子目录。
    def start(self, run_id):
        stamp = datetime.now().astimezone().strftime("%Y%m%d-%H%M%S-%f%z")
        self.directory = self.root / f"{stamp}_{run_id}"
        self.directory.mkdir(parents=True, mode=0o700)
        (self.directory / "history").mkdir(mode=0o700)

    # 以 UTF-8 写入并及时 flush，按实际字节累计预算；预留空间保证还能尝试写汇总。
    def _write(self, path, data, *, append=False, reserve=False):
        raw = data.encode("utf-8")
        # 为最终汇总预留空间，避免事件或 History 用尽全部文件预算。
        limit = self.max_bytes if reserve else max(0, self.max_bytes - 65536)
        if self.written + len(raw) > limit:
            raise OSError("Trace size limit reached")
        mode = "a" if append else "w"
        import os

        flags = os.O_WRONLY | os.O_CREAT | (os.O_APPEND if append else os.O_TRUNC)
        fd = os.open(path, flags, 0o600)
        with os.fdopen(fd, mode, encoding="utf-8") as stream:
            stream.write(data)
            stream.flush()
        self.written += len(raw)

    # 逐行追加结构化事件，不在内存中积累整段执行历史。
    def event(self, event):
        line = json.dumps(event, ensure_ascii=False, allow_nan=False) + "\n"
        self._write(self.directory / "events.jsonl", line, append=True)

    # 将框架历史快照追加到所属尝试轮次的 JSONL 文件。
    def history(self, attempt, entry):
        # 执行中直接追加 JSONL，避免在内存里累积越来越大的 History 数组。
        filename = f"attempt-{attempt}.jsonl"
        self._write(
            self.directory / "history" / filename,
            json.dumps(entry, ensure_ascii=False) + "\n",
            append=True,
        )

    # 先写临时文件，再替换 summary.json，避免留下只写了一半的最终汇总。
    def summary(self, summary):
        path = self.directory / "summary.json"
        temporary = path.with_suffix(".tmp")
        self._write(
            temporary, json.dumps(summary, ensure_ascii=False, indent=2) + "\n", reserve=True
        )
        temporary.replace(path)


# 把当前任务相关的框架 WARNING 及以上日志转为普通追踪事件。
class _LogHandler(logging.Handler):
    # 绑定目标追踪器，并设置日志级别以避免采集大量无关调试文本。
    def __init__(self, trace):
        super().__init__(level=logging.WARNING)
        self.trace = trace

    # 只转发上下文属于本追踪器的日志，附上已有异常堆栈。
    def emit(self, record):
        if current_trace() is self.trace:
            self.trace.emit(
                "framework.log",
                {
                    "level": record.levelname,
                    "logger": record.name,
                    "message": record.getMessage(),
                    "traceback": "".join(traceback.format_exception(*record.exc_info))
                    if record.exc_info and record.exc_info[0]
                    else None,
                },
            )


# 聚合事件、指标和多个消费者；清理后的字典才会发送给消费者。
# 单个消费者失败只会被禁用并警告，不应改变业务结果；共享写入通过锁串行化。
class Trace:
    """Reusable recorder. Consumers receive sanitized dictionaries only.

    Consumer errors disable that consumer and warn once; counters remain available.
    ContextVars isolate concurrent tasks; locking serializes writers sharing a run.
    """

    # 初始化一次运行的标识、计数器、文件消费者和可重入锁；此时不写文件。
    def __init__(self, *sinks, secrets=()):
        self.run_id = uuid.uuid4().hex
        self.sanitizer = Sanitizer(secrets)
        self.sinks = list(sinks)
        self.failed = set()
        self.errors = []
        self.seq = 0
        self.counts = Counter()
        self.llm_sources = Counter()
        self.tokens = Counter()
        self.usage_calls = 0
        self.durations = Counter()
        self.outcome = None
        self.exit_code = None
        self.metadata = {}
        self.started = None
        self.active = False
        self.lock = threading.RLock()

    # 向支持指定方法的消费者分发；失败后隔离该消费者，结束时仍尝试发送汇总。
    def _send(self, method, *args):
        for index, sink in enumerate(self.sinks):
            if index in self.failed and method != "summary":
                continue
            try:
                callback = getattr(sink, method, None)
                if callback is not None:
                    callback(*args)
            except Exception:
                if index not in self.failed:
                    self.failed.add(index)
                    self.errors.append({"consumer": index, "code": "trace_write_failure"})
                    # 警告不附带文件系统或消费者异常原文，避免绕过凭据清理流程。
                    try:
                        print(
                            "AutoScraper: trace consumer failed or reached size limit; "
                            "task continues, trace is incomplete.",
                            file=sys.stderr,
                        )
                    except Exception:
                        pass

    # 在锁内分配事件序号、累计计数，并清理和裁剪负载后分发。
    def emit(self, event_type, payload=None):
        with self.lock:
            event_type = self.sanitizer.text(event_type)
            self.seq += 1
            self.counts[event_type] += 1
            data = self.sanitizer.clean(payload or {})
            data = bounded_payload(data)
            event = {
                "schema_version": 1,
                "run_id": self.run_id,
                "seq": self.seq,
                "timestamp": utc_now(),
                "event_type": event_type,
                **_labels.get(),
                "payload": data,
            }
            self._send("event", event)

    # 将 History 片段清理后关联到当前轮次/步骤，再发给消费者。
    def history(self, entry):
        with self.lock:
            entry = self.sanitizer.clean(entry)
            entry = bounded_payload(entry)
            self._send(
                "history",
                _labels.get().get("attempt", 1),
                {"run_id": self.run_id, **_labels.get(), **entry},
            )

    # 管理追踪生命周期：设置上下文、挂接框架日志、记录终态和汇总后恢复环境。
    # 同一追踪器被嵌套使用时复用当前运行，避免 CLI 与工作流各生成一份重复日志。
    @contextmanager
    def run(self, *, url=None, task=None, model=None):
        if self.active:
            # CLI 外层已开启运行时，工作流装饰器复用它，最终汇总由最外层负责写出。
            yield self
            return
        self.active = True
        self.started = time.monotonic()
        started_at = utc_now()
        token = _current.set(self)
        context_token = _labels.set({})
        handler = _LogHandler(self)
        self._send("start", self.run_id)
        # 不挂到 root：browser-use 懒初始化检查 hasHandlers()，提前挂接会阻止终端处理器安装。
        # 框架日志也可能停止向上传播，所以直接订阅相关命名空间。
        loggers = [logging.getLogger(name) for name in ("autoscraper", "browser_use", "bubus")]
        for logger in loggers:
            logger.addHandler(handler)
        termination = "returned"
        try:
            self.metadata = self.sanitizer.clean(
                {"original_url": url, "task": task, "model": model}
            )
            self.emit("run.started", self.metadata)
            yield self
        except BaseException as exc:
            termination = (
                "interrupted"
                if isinstance(exc, (KeyboardInterrupt, SystemExit))
                or type(exc).__name__ == "CancelledError"
                else "exception"
            )
            self.emit(
                "run.failed",
                {
                    "exception": type(exc).__name__,
                    "message": str(exc),
                    "traceback": "".join(traceback.format_exception(exc)),
                },
            )
            raise
        finally:
            try:
                elapsed = time.monotonic() - self.started
                self.emit("run.finished", {"termination": termination, "duration_seconds": elapsed})
                summary = {
                    "schema_version": 1,
                    "run_id": self.run_id,
                    "started_at": started_at,
                    "ended_at": utc_now(),
                    "termination": termination,
                    "exit_code": self.exit_code,
                    "metadata": self.metadata,
                    "outcome": self.sanitizer.clean(self.outcome),
                    "metrics": {
                        "duration_seconds": elapsed,
                        "events": dict(self.counts),
                        "phase_seconds": dict(self.durations),
                        "llm_calls_by_source": dict(self.llm_sources),
                        "tokens": dict(self.tokens) if self.usage_calls else None,
                        "usage_calls": self.usage_calls,
                        "usage_missing_calls": max(
                            0, self.counts["llm.started"] - self.usage_calls
                        ),
                        "steps": self.counts["step.started"],
                        "tools": self.counts["tool.started"],
                        "attempts": self.counts["attempt.started"],
                        "repairs": self.counts["repair.started"],
                        "validations": self.counts["validation.started"],
                    },
                    "trace_errors": self.errors,
                }
                self._send("summary", summary)
            finally:
                for logger in loggers:
                    logger.removeHandler(handler)
                _labels.reset(context_token)
                _current.reset(token)
                self.active = False


# 记录一个可嵌套阶段的开始、耗时和结束；异常或取消时也保留诊断并继续抛出。
@contextmanager
def operation(name, **payload):
    """A phase or call span; always closes on errors/cancellation."""
    trace = current_trace()
    details = {"outcome": "returned"}
    if trace is None:
        yield details
        return
    call_id = uuid.uuid4().hex
    started = time.monotonic()
    with labels(parent_call_id=_labels.get().get("call_id"), call_id=call_id):
        emit(name + ".started", **payload)
        try:
            yield details
        except BaseException as exc:
            details.update(
                exception=type(exc).__name__,
                message=str(exc),
                outcome="raised",
                traceback="".join(traceback.format_exception(exc)),
            )
            if getattr(exc, "code", None) is not None:
                details["code"] = exc.code
            if getattr(exc, "status_code", None) is not None:
                details["status_code"] = exc.status_code
            raise
        finally:
            duration = time.monotonic() - started
            trace.durations[name] += duration
            emit(name + ".finished", **payload, **details, duration_seconds=duration)


# 把底层 API 返回的 token 用量填入当前模型调用的追踪槽位。
def capture_usage(usage):
    slot = _usage.get()
    if slot is not None and usage is not None:
        slot["usage"] = {key: getattr(usage, key, None) for key in Sanitizer.NUMERIC}


# 异步模型调用装饰器：统计主模型/需求解析/等待模型的调用量和 token。
def traced_llm(function):
    # 为每次请求建立独立用量槽位，结束时恢复上下文并合并本次统计。
    @functools.wraps(function)
    async def wrapped(self, messages, *args, **kwargs):
        trace = current_trace()
        if trace is None:
            return await function(self, messages, *args, **kwargs)
        source = getattr(self, "trace_source", None) or _labels.get().get("source", "main")
        if source not in {"main", "requirements", "wait"}:
            source = "other"
        with (
            labels(source=source),
            operation("llm", model=self.model, message_count=len(messages)) as details,
        ):
            trace.llm_sources[source] += 1
            token = _usage.set(details)
            try:
                return await function(self, messages, *args, **kwargs)
            finally:
                _usage.reset(token)
                usage = details.get("usage")
                if usage is not None:
                    trace.usage_calls += 1
                    trace.tokens.update({k: v for k, v in usage.items() if v is not None})

    return wrapped


# 同步任务装饰器：注册当前密钥并管理工作流追踪，不要求业务入口处理文件消费者。
def traced_task(function):
    # 选择显式或上下文追踪器，运行任务后将相同业务摘要写入最终追踪。
    @functools.wraps(function)
    def wrapped(url, task, settings, *args, trace=None, **kwargs):
        recorder = trace or current_trace()
        if recorder is None:
            return function(url, task, settings, *args, **kwargs)
        # 注册密钥字面值，确保它嵌在 URL、选择器或元数据中时也能被替换。
        key = settings.api_key.get_secret_value()
        if key and key not in recorder.sanitizer.secrets:
            recorder.sanitizer.secrets = (*recorder.sanitizer.secrets, key)
        with recorder.run(url=url, task=task, model=settings.model):
            recorder.metadata["model"] = recorder.sanitizer.clean(settings.model, "model")
            with operation("workflow"):
                outcome = function(url, task, settings, *args, **kwargs)
                recorder.outcome = outcome.summary()
                emit("workflow.result", **recorder.outcome)
                return outcome

    return wrapped


# browser-use 的步骤回调适配器；增量摘取 History，关联规划、执行和结束事件。
class StepObserver:
    """Per-Agent callbacks; snapshots use History metadata without page text."""

    # 保存尚未关闭的步骤上下文及已采集 History 的位置，避免重复记录。
    def __init__(self):
        self.scope = None
        self.span = None
        self.details = None
        self.history_index = 0

    # 步骤开始时进入带 step 标签的追踪范围，等待 end 或异常清理关闭。
    async def start(self, agent):
        self.scope = labels(step=agent.state.n_steps)
        self.scope.__enter__()
        self.span = operation("step")
        self.details = self.span.__enter__()

    # 记录模型规划出的动作名称与自报评价，不采集完整模型消息。
    def planned(self, state, output, step):
        emit(
            "step.planned",
            assessment=agent_assessment(output),
            actions=[
                {"tool": next(iter(action.model_dump(exclude_unset=True)), "unknown")}
                for action in output.action
            ],
        )

    # 从上次游标之后提取新增 History，记录状态、动作和有界工具反馈。
    def snapshot(self, agent):
        trace = current_trace()
        if trace is None:
            return
        history = agent.history.history
        for item in history[self.history_index :]:
            entry = {
                "metadata": item.metadata.model_dump() if item.metadata else None,
                "url": item.state.url,
                "assessment": agent_assessment(item.model_output),
                "actions": [
                    action.model_dump(exclude_unset=True, mode="json")
                    for action in item.model_output.action[:10]
                ]
                if item.model_output
                else [],
                "results": [
                    {
                        "error": result.error,
                        "success": result.success,
                        "is_done": result.is_done,
                        "output_chars": len(result.extracted_content or ""),
                        "feedback": summarize_tool_output(result.extracted_content),
                    }
                    for result in item.result
                ],
            }
            trace.history(entry)
        self.history_index = len(history)

    # 正常步骤结束时采集历史并关闭本步骤跨度。
    async def end(self, agent):
        self.finish(agent)

    # 统一处理正常结束和中断；即使采集失败也释放手工进入的上下文。
    def finish(self, agent, *, interrupted=False):
        try:
            self.snapshot(agent)
            if self.details is not None:
                self.details["incomplete"] = interrupted
                self.details["errors"] = [
                    result.error for result in (agent.state.last_result or []) if result.error
                ]
        finally:
            if self.span is not None:
                self.span.__exit__(None, None, None)
                self.span = None
                self.scope.__exit__(None, None, None)
                self.scope = None
                self.details = None
