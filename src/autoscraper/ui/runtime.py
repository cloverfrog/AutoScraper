# Web 任务适配：同步抓取在独立线程运行，通过条件变量接收网页上的人工决定。
# 单实例只允许一个活动任务；认证和 Trace 留在私有目录，只有产物目录可下载。
"""Threaded local UI jobs, independent of Gradio and browser-use imports."""

from collections import deque
from dataclasses import dataclass, field
import logging
import os
from pathlib import Path
import re
import threading
import time
import uuid

from autoscraper.browser.access import BrowserAccess
from autoscraper.exceptions import HandoffRequired
from autoscraper.observability.tracing import FileSink, Sanitizer, Trace
from autoscraper.scraping.executor import ScraperExecutor
from autoscraper.scraping.export import export_records
from autoscraper.scraping.spec import ScraperSpec


# UI 参数只保存非敏感配置；密钥沿用 CLI 环境变量，不经过网页。
@dataclass
class RunConfig:
    mode: str = "agent"
    url: str = ""
    task: str = ""
    spec_json: str = ""
    execute: bool = False
    headed: bool = False
    login: bool = False
    auth_profile: str = ""
    browser_path: str = ""
    fields: str = ""
    count: int | None = None
    agent_options: dict = field(default_factory=dict)
    validation_options: dict = field(default_factory=dict)
    content_timeout_ms: int | None = None
    navigation_timeout_ms: int | None = None


# 条件变量保护请求和回答；每个请求带独立 ID，避免重复提交回答后续问题。
class Job:
    def __init__(self, config: RunConfig):
        self.id = uuid.uuid4().hex
        self.config = config
        self.condition = threading.Condition(threading.RLock())
        self.status = "running"
        self.pending = None
        self.answer = None
        self.logs = deque(maxlen=1500)
        self.summary = {}
        self.preview = []
        self.files = []
        self.done = False
        self.sanitizer = Sanitizer((
            os.getenv("AUTOSCRAPER_API_KEY"), os.getenv("DEEPSEEK_API_KEY"),
        ))

    # 在回调中等待用户输入；超时释放任务，断开网页不会无限占用浏览器。
    def ask(self, kind, request, timeout=900):
        with self.condition:
            self.pending = {"id": uuid.uuid4().hex, "kind": kind, **request.model_dump()}
            self.status = "needs_clarification" if kind == "clarification" else "needs_user"
            self.answer = None
            self.condition.notify_all()
            deadline = time.monotonic() + timeout
            while self.answer is None:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                self.condition.wait(remaining)
            answer = self.answer
            self.pending = None
            self.status = "running"
            return answer

    # 只接受当前请求的决定；取消澄清返回空字符串，让既有模块生成待澄清终态。
    def respond(self, request_id, answer, *, cancel=False):
        with self.condition:
            if self.done or not self.pending or self.pending["id"] != request_id:
                raise ValueError("问题已结束或已更新，请等待页面刷新。")
            if self.answer is not None:
                raise ValueError("回答已提交，请等待任务继续。")
            if not cancel and (not answer or not answer.strip()):
                raise ValueError("请先选择选项或填写回答。")
            self.answer = ("cancel" if self.pending["kind"] == "handoff" else "") if cancel else answer
            self.condition.notify_all()

    # 对日志、状态和结果进行一致快照；预览限量，完整数据通过文件下载。
    def snapshot(self):
        with self.condition:
            return {
                "status": self.status, "pending": dict(self.pending) if self.pending else None,
                "summary": self.sanitizer.clean(self.summary), "preview": self.preview[:100],
                "files": list(self.files), "logs": "\n".join(self.logs), "done": self.done,
            }


# 直接订阅框架命名空间，兼容 browser-use 禁止日志向 root 传播的配置。
# 不替换 stdout/stderr，不影响已有终端输出；有界缓存复用 Trace 的凭据清理器。
class JobLogHandler(logging.Handler):
    def __init__(self, job):
        super().__init__(logging.INFO)
        self.job = job
        self.setFormatter(logging.Formatter("%(levelname)s [%(name)s] %(message)s"))

    def emit(self, record):
        if getattr(record, "_autoscraper_ui_seen", False):
            return
        record._autoscraper_ui_seen = True
        with self.job.condition:
            self.job.logs.append(self.job.sanitizer.text(self.format(record)))


# 任务管理器由所有标签页和会话共享；浏览器只携带随机任务 ID，不能传入本地路径。
class JobManager:
    def __init__(self, root: Path):
        self.root = root.resolve()
        self.lock = threading.Lock()
        self.active = None
        self.jobs = {}
        for name in ("artifacts", "auth", "traces"):
            (self.root / name).mkdir(parents=True, exist_ok=True, mode=0o700)

    def start(self, config):
        if config.mode not in {"agent", "spec"}:
            raise ValueError("未知运行模式")
        if config.auth_profile and not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", config.auth_profile):
            raise ValueError("认证名称仅允许 1–64 位英文字母、数字、下划线或连字符。")
        if config.login and not config.headed:
            raise ValueError("人工登录需要启用可见浏览器。")
        with self.lock:
            if self.active is not None:
                raise ValueError("已有任务正在运行或等待人工操作，请先完成该任务。")
            # 只保留最近 20 个任务的内存快照；文件仍留在本机供用户自行清理。
            while len(self.jobs) >= 20:
                self.jobs.pop(next(iter(self.jobs)))
            job = Job(config)
            self.jobs[job.id] = job
            self.active = job.id
            thread = threading.Thread(target=self._work, args=(job,), daemon=True)
            try:
                thread.start()
            except Exception:
                self.active = None
                del self.jobs[job.id]
                raise
            return job.id

    def get(self, job_id):
        with self.lock:
            return self.jobs.get(job_id)

    # 文件名由服务端生成，认证名称永远不解释为用户提供的文件路径。
    def _work(self, job):
        config = job.config
        output = self.root / "artifacts" / job.id
        handler = JobLogHandler(job)
        loggers = [logging.getLogger(name) for name in ("autoscraper", "browser_use", "bubus")]
        trace = Trace(FileSink(self.root / "traces"), secrets=job.sanitizer.secrets)
        access = BrowserAccess(
            handler=(lambda request: job.ask("handoff", request)) if config.headed else None
        )
        auth_path = self.root / "auth" / f"{config.auth_profile}.json"
        try:
            output.mkdir(mode=0o700)
            if config.auth_profile and auth_path.exists():
                access.load(auth_path)
            # Agent 懒加载可能配置框架日志；先初始化，再安装网页日志处理器。
            if config.mode == "agent":
                try:
                    import browser_use  # noqa: F401
                except ImportError:
                    raise ValueError('Agent 模式需要安装：uv sync --extra ui --extra agent') from None
            for logger in loggers:
                logger.addHandler(handler)
            with trace.run(url=config.url, task=config.task):
                self._execute(job, access, output, trace)
                trace.outcome = job.summary
        except HandoffRequired as exc:
            job.summary = {
                "status": "cancelled" if exc.cancelled else "needs_user",
                "pending_handoff": exc.request.model_dump(),
            }
        except Exception as exc:
            job.summary = {"status": "unfulfilled", "error": job.sanitizer.text(str(exc))}
        finally:
            try:
                if config.auth_profile:
                    access.save(auth_path)
            except OSError as exc:
                job.summary["auth_save_error"] = job.sanitizer.text(str(exc))
            for logger in loggers:
                logger.removeHandler(handler)
            with job.condition:
                job.status = job.summary.get("status", "unfulfilled")
                job.done = True
            with self.lock:
                self.active = None

    # 两类模式调用现有 Python 接口；只有成功规则才作为已验证规则发布。
    def _execute(self, job, access, output, trace):
        config = job.config
        if config.mode == "agent":
            from autoscraper.cli.agent import settings_from_env
            from autoscraper.workflow.runner import run_task
            from autoscraper.workflow.validation import ValidationSettings

            settings = settings_from_env(
                **config.agent_options, headless=not config.headed,
                executable_path=config.browser_path or None,
                content_timeout_ms=config.content_timeout_ms,
            )
            outcome = run_task(
                config.url, config.task, settings,
                validation=ValidationSettings(**config.validation_options),
                fields=config.fields.split() or None, count=config.count,
                execute=config.execute, trace=trace, access=access,
                ask_user=lambda request: job.ask("clarification", request),
            )
            job.summary = outcome.summary()
            spec, data = outcome.spec, outcome.data
            if spec is not None:
                name = "rule.json" if outcome.status in {"success", "validated"} else "rule.unvalidated.json"
                path = output / name
                path.write_text(spec.model_dump_json(indent=2) + "\n", encoding="utf-8")
                job.files.append(str(path))
            if outcome.status not in {"success", "validated"}:
                return
        else:
            spec = ScraperSpec.model_validate_json(config.spec_json)
            overrides = {}
            if config.content_timeout_ms is not None:
                overrides["content_timeout_ms"] = config.content_timeout_ms
            if config.navigation_timeout_ms is not None:
                overrides["timeout_ms"] = config.navigation_timeout_ms
            spec = ScraperSpec.model_validate({**spec.model_dump(), **overrides})
            data = ScraperExecutor(
                headless=not config.headed,
                executable_path=config.browser_path or os.getenv("AUTOSCRAPER_BROWSER_PATH"),
                access=access, login=config.login,
            ).run(spec)
            job.summary = {
                "status": "partial" if data.errors or data.stop_reason == "detail_limit" else "success",
                "items": len(data.records), "pages": data.pages_visited,
                "detail_pages": data.detail_pages_visited, "duplicates": data.duplicates,
                "errors": data.errors, "stop_reason": data.stop_reason,
                "error_details": [vars(error) for error in data.error_details],
            }
            path = output / "rule.json"
            path.write_text(spec.model_dump_json(indent=2) + "\n", encoding="utf-8")
            job.files.append(str(path))
        if data is not None:
            job.preview = data.records[:100]
            for suffix in ("json", "csv"):
                path = output / f"data.{suffix}"
                export_records(data.records, path, list(spec.all_fields))
                job.files.append(str(path))
