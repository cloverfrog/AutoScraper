# 生产追踪契约测试：覆盖事件顺序、凭据替换、大小限制、统计和错误隔离。
# 也验证并发上下文、框架日志、CLI 结果及 Agent 异常收尾，避免追踪改变业务行为。
"""Tracing contracts, failure isolation and privacy without paid LLM calls."""

import asyncio
import json
import logging
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from autoscraper.agent.explorer import AgentSettings
from autoscraper.agent.clarification import ClarificationRequest, ClarificationSession
from autoscraper.scraping.executor import ScrapeResult, ScraperExecutor
from autoscraper.scraping.spec import ScraperSpec
from autoscraper.observability.tracing import FileSink, Trace, current_trace, emit, labels, operation
from autoscraper.workflow.outcome import TaskOutcome
from autoscraper.workflow.runner import run_task


# 将事件、History 和汇总收集到内存，便于断言输出结构与关联关系。
class MemorySink:
    def __init__(self):
        self.events = []
        self.histories = []
        self.summary_data = None

    def event(self, event):
        self.events.append(event)

    def history(self, attempt, entry):
        self.histories.append((attempt, entry))

    def summary(self, data):
        self.summary_data = data


# 为追踪测试提供固定模型配置和可识别的测试密钥。
@pytest.fixture
def settings():
    return AgentSettings(
        api_key="PRIVATE_API_KEY", model="fixture", timeout_seconds=30, base_url="https://llm.test"
    )


# 提供一份最小合法规则，用于工作流和 CLI 的模拟结果。
@pytest.fixture
def spec():
    return ScraperSpec.model_validate(
        {
            "start_url": "https://fixture.test/private?token=HIDDEN_QUERY",
            "item_selector": "article",
            "fields": {"title": {"selector": "h2"}},
            "max_items": 1,
        }
    )


# 检查流式事件、明确凭据替换、最终指标和业务汇总。
def test_streaming_privacy_and_summary(tmp_path):
    sink = FileSink(tmp_path)
    trace = Trace(sink, secrets=("PRIVATE_API_KEY",))
    with trace.run(
        url="https://user:pw@fixture.test/private?token=HIDDEN_QUERY", task="PRIVATE_TASK"
    ):
        with labels(attempt=2, step=3), operation("tool", tool="inspect_page"):
            emit(
                "tool.diagnostics",
                inputs={
                    "selector": "a[href='PRIVATE_PAGE']",
                    "authorization": "Bearer PRIVATE_API_KEY",
                },
                message="PRIVATE_ERROR",
                dom="PRIVATE_DOM",
                output="PRIVATE_PAGE",
                url="https://fixture.test/PRIVATE_PATH?token=HIDDEN_QUERY",
            )
            trace.history(
                {
                    "url": "https://fixture.test/?token=HIDDEN_QUERY",
                    "message": "PRIVATE_HISTORY",
                    "step_number": 3,
                }
            )
            logging.getLogger("browser_use.fixture").warning("PRIVATE_LOG %s", "PRIVATE_API_KEY")
            # Events and history are visible before normal completion.
            assert (sink.directory / "events.jsonl").read_text()
            assert (sink.directory / "history/attempt-2.jsonl").read_text()
        trace.outcome = TaskOutcome(status="validated").summary()
    contents = "\n".join(path.read_text() for path in sink.directory.rglob("*") if path.is_file())
    assert "PRIVATE_API_KEY" not in contents
    for value in (
        "PRIVATE_TASK",
        "PRIVATE_ERROR",
        "PRIVATE_HISTORY",
        "PRIVATE_LOG",
        "PRIVATE_PATH",
        "PRIVATE_PAGE",
    ):
        assert value in contents
    assert not (sink.directory / "console.log").exists()
    assert "HIDDEN_QUERY" not in contents
    assert "user:pw" not in contents
    events = [
        json.loads(line) for line in (sink.directory / "events.jsonl").read_text().splitlines()
    ]
    assert [event["seq"] for event in events] == list(range(1, len(events) + 1))
    diagnostic = next(event for event in events if event["event_type"] == "tool.diagnostics")
    assert diagnostic["attempt"] == 2 and diagnostic["step"] == 3
    assert diagnostic["call_id"]
    summary = json.loads((sink.directory / "summary.json").read_text())
    assert summary["outcome"]["status"] == "validated"
    assert summary["metrics"]["tokens"] is None
    assert summary["metrics"]["tools"] == 1
    assert current_trace() is None


# 一个消费者写入失败不能中断任务或其他消费者。
@pytest.mark.parametrize("failure", ["start", "event", "history", "summary"])
def test_consumer_failure_does_not_change_task_or_other_consumers(failure, capsys):
    class Broken:
        def __getattr__(self, name):
            def write(*args):
                if name == failure:
                    raise OSError("PRIVATE_DISK_ERROR")

            return write

    memory = MemorySink()
    trace = Trace(Broken(), memory)
    with trace.run():
        emit("test.event")
        trace.history({"step_number": 1})
        emit("test.another")
        trace.outcome = TaskOutcome(status="success").summary()
    assert memory.summary_data["outcome"]["status"] == "success"
    assert len(trace.errors) == 1
    stderr = capsys.readouterr().err
    assert stderr.count("trace consumer failed") == 1
    assert "PRIVATE_DISK_ERROR" not in stderr


# 事件达到空间上限时仍保留汇总空间，并限制单条事件大小。
def test_size_limit_reserves_summary_and_bounds_events(tmp_path, capsys):
    sink = FileSink(tmp_path, max_bytes=70000)
    trace = Trace(sink)
    with trace.run():
        for _ in range(100):
            emit("large", data=[{"status": "x" * 10000}] * 50)
    assert sum(p.stat().st_size for p in sink.directory.rglob("*") if p.is_file()) <= 70000
    assert json.loads((sink.directory / "summary.json").read_text())["trace_errors"]
    assert capsys.readouterr().err.count("trace consumer failed") == 1


# 异常和中断应留下已发生事件，同时保留原始异常语义。
@pytest.mark.parametrize(
    "error", [RuntimeError("PRIVATE_ERROR"), KeyboardInterrupt(), asyncio.CancelledError()]
)
def test_exception_and_interruption_preserve_events_and_original_error(error, tmp_path):
    sink = FileSink(tmp_path)
    trace = Trace(sink)
    with pytest.raises(type(error)) as raised:
        with trace.run():
            with operation("attempt"):
                emit("checkpoint")
                raise error
    assert raised.value is error
    summary = json.loads((sink.directory / "summary.json").read_text())
    assert summary["termination"] in {"exception", "interrupted"}
    assert summary["outcome"] is None
    events = (sink.directory / "events.jsonl").read_text()
    assert "checkpoint" in events and "attempt.finished" in events
    if isinstance(error, RuntimeError):
        finished = next(
            json.loads(line)
            for line in events.splitlines()
            if json.loads(line)["event_type"] == "attempt.finished"
        )
        assert finished["payload"]["message"] == "PRIVATE_ERROR"
        assert "test_exception_and_interruption" in finished["payload"]["traceback"]


# 并发任务的标签、事件和框架日志不得互相混入。
def test_concurrent_runs_and_logging_are_isolated():
    memories = [MemorySink(), MemorySink()]

    async def worker(index):
        with Trace(memories[index]).run():
            with labels(attempt=index + 1):
                await asyncio.sleep(0)
                emit("worker", count=index)
                logging.getLogger("browser_use.fixture").warning("PRIVATE_LOG")

    async def scenario():
        await asyncio.gather(worker(0), worker(1))

    asyncio.run(scenario())
    for index, memory in enumerate(memories):
        event = next(e for e in memory.events if e["event_type"] == "worker")
        assert event["attempt"] == index + 1
        assert event["payload"]["count"] == index
        assert len({e["run_id"] for e in memory.events}) == 1
        logs = [e for e in memory.events if e["event_type"] == "framework.log"]
        assert len(logs) == 1 and logs[0]["payload"]["message"] == "PRIVATE_LOG"
    assert memories[0].events[0]["run_id"] != memories[1].events[0]["run_id"]


# 检查模型来源与 token 统计，即使解析输出失败也保留已发生用量。
@pytest.mark.parametrize("source", ["requirements", "main", "wait"])
@pytest.mark.parametrize("invalid", [False, True])
def test_llm_usage_sources_and_parse_failure(monkeypatch, source, invalid):
    pytest.importorskip("browser_use")
    from pydantic import BaseModel
    from autoscraper.agent.responses import ChatOpenAIResponses

    class Output(BaseModel):
        ready: bool

    response = SimpleNamespace(
        status="completed",
        output_text="INVALID_PRIVATE_RESPONSE" if invalid else '{"ready":true}',
        usage=SimpleNamespace(
            input_tokens=11,
            output_tokens=3,
            total_tokens=14,
            input_tokens_details=SimpleNamespace(cached_tokens=2),
        ),
    )
    monkeypatch.setattr(
        ChatOpenAIResponses,
        "get_client",
        lambda self: SimpleNamespace(
            responses=SimpleNamespace(create=AsyncMock(return_value=response))
        ),
    )
    model = ChatOpenAIResponses(model="fixture")
    if source == "wait":
        model.trace_source = "wait"
    memory = MemorySink()
    with Trace(memory).run(), labels(source=source):
        if invalid:
            with pytest.raises(Exception):
                asyncio.run(model.ainvoke([], output_format=Output))
        else:
            assert asyncio.run(model.ainvoke([], output_format=Output)).completion.ready
    metrics = memory.summary_data["metrics"]
    assert metrics["llm_calls_by_source"] == {source: 1}
    assert metrics["tokens"]["total_tokens"] == 14
    assert metrics["usage_missing_calls"] == 0
    finished = next(e for e in memory.events if e["event_type"] == "llm.finished")
    if invalid:
        assert "INVALID_PRIVATE_RESPONSE" in finished["payload"]["message"]
    assert memory.events[-2]["event_type"] == "llm.finished"


# 多轮修复、候选和反馈必须关联同一个追踪运行。
def test_workflow_repair_and_feedback_share_run(monkeypatch, settings, spec):
    pytest.importorskip("browser_use")
    import autoscraper.workflow.runner as workflow

    attempts = []

    async def generate(*args, **kwargs):
        attempts.append(kwargs)
        return spec if len(attempts) == 2 else spec.model_copy(update={"fields": {}})

    monkeypatch.setattr(workflow, "generate_spec", generate)
    monkeypatch.setattr(
        ScraperExecutor,
        "run",
        lambda *args: ScrapeResult(
            records=[{"title": "PRIVATE_PAGE"}], pages_visited=1, stop_reason="max_items"
        ),
    )
    memory = MemorySink()
    outcome = run_task(
        str(spec.start_url), "前1本，字段名为title", settings, execute=True, trace=Trace(memory)
    )
    assert outcome.status == "success" and outcome.attempts == 2
    assert len(attempts) == 2 and attempts[1]["feedback"]
    assert memory.summary_data["outcome"]["status"] == outcome.summary()["status"]
    metrics = memory.summary_data["metrics"]
    assert metrics["attempts"] == 2 and metrics["repairs"] == 1
    candidate_events = [e for e in memory.events if e["event_type"] == "spec.candidate"]
    assert [e["attempt"] for e in candidate_events] == [1, 2]
    assert len({e["run_id"] for e in memory.events}) == 1
    failed = next(e for e in memory.events if e["event_type"] == "validation.feedback")
    assert failed["attempt"] == 1
    assert failed["payload"]["feedback"][0]["fields"] == ["title"]
    assert (
        failed["payload"]["feedback"][0]["message"]
        == "Include every requested field and mark it required"
    )
    assert memory.summary_data["outcome"]["requirements"]["fields"] == ["title"]


# 追踪澄清时保留当前尝试轮次和可读用户回答。
def test_clarification_keeps_attempt_and_readable_answer():
    memory = MemorySink()
    session = ClarificationSession(handler=lambda request: "PRIVATE_ANSWER")
    session.pending = ClarificationRequest(question="PRIVATE_QUESTION", explanation="reason")
    with Trace(memory).run(), labels(attempt=2):
        session.answer_pending()
    assert session.history[0]["answer"] == "PRIVATE_ANSWER"
    event = next(e for e in memory.events if e["event_type"] == "clarification.answer")
    assert event["attempt"] == 2
    assert event["payload"]["answer"] == "PRIVATE_ANSWER"


# CLI JSON、退出码和追踪汇总应描述相同的业务结果。
@pytest.mark.parametrize(
    "status,code",
    [
        ("success", 0),
        ("validated", 0),
        ("unsupported", 3),
        ("needs_clarification", 4),
        ("unfulfilled", 1),
    ],
)
def test_cli_json_exit_codes_and_trace_summary(monkeypatch, tmp_path, capsys, spec, status, code):
    import autoscraper.cli.agent as cli

    monkeypatch.setenv("AUTOSCRAPER_API_KEY", "PRIVATE_API_KEY")
    outcome = TaskOutcome(status=status, spec=spec if code == 0 else None)
    monkeypatch.setattr(cli, "run_task", lambda *args, **kwargs: outcome)
    monkeypatch.setattr(
        "sys.argv",
        [
            "autoscraper-agent",
            "https://fixture.test/",
            "PRIVATE_TASK",
            "--spec-output",
            str(tmp_path / "rule.json"),
            "--runs-dir",
            str(tmp_path / "runs"),
        ],
    )
    assert cli.main() == code
    summary = json.loads(capsys.readouterr().out)
    assert summary["status"] == status
    persisted = json.loads(next((tmp_path / "runs").glob("*/summary.json")).read_text())
    assert persisted["outcome"]["status"] == status
    assert persisted["metadata"]["task"] == "PRIVATE_TASK"
    assert "PRIVATE_API_KEY" not in json.dumps(persisted)


# 保存产物失败时，追踪仍应保留此前完成的工作流结果。
def test_cli_artifact_error_preserves_workflow_result(monkeypatch, tmp_path, spec, capsys):
    import autoscraper.cli.agent as cli

    monkeypatch.setenv("AUTOSCRAPER_API_KEY", "PRIVATE_API_KEY")
    monkeypatch.setattr(
        cli, "run_task", lambda *args, **kwargs: TaskOutcome(status="validated", spec=spec)
    )
    monkeypatch.setattr(cli, "save_spec", lambda *args: (_ for _ in ()).throw(OSError("disk full")))
    monkeypatch.setattr(
        "sys.argv",
        [
            "autoscraper-agent",
            "https://fixture.test/",
            "task",
            "--spec-output",
            str(tmp_path / "rule.json"),
            "--runs-dir",
            str(tmp_path / "runs"),
        ],
    )
    assert cli.main() == 1
    assert json.loads(capsys.readouterr().err)["status"] == "unfulfilled"
    summary = json.loads(next((tmp_path / "runs").glob("*/summary.json")).read_text())
    assert summary["outcome"]["status"] == "validated"
    events = next((tmp_path / "runs").glob("*/events.jsonl")).read_text()
    assert "artifacts.finished" in events and "cli.failed" in events


# 日志写入失败不应把成功任务改成失败。
def test_cli_logging_failure_does_not_change_success(monkeypatch, tmp_path, spec, capsys):
    import autoscraper.cli.agent as cli

    blocked = tmp_path / "not-a-directory"
    blocked.write_text("block")
    monkeypatch.setenv("AUTOSCRAPER_API_KEY", "PRIVATE_API_KEY")
    monkeypatch.setattr(
        cli, "run_task", lambda *args, **kwargs: TaskOutcome(status="validated", spec=spec)
    )
    monkeypatch.setattr(
        "sys.argv",
        [
            "autoscraper-agent",
            "https://fixture.test/",
            "task",
            "--spec-output",
            str(tmp_path / "rule.json"),
            "--runs-dir",
            str(blocked),
        ],
    )
    assert cli.main() == 0
    captured = capsys.readouterr()
    assert json.loads(captured.out)["status"] == "validated"
    assert captured.err.count("trace consumer failed") == 1


# 通过真实工具分发验证返回值、工具错误和取消事件。
@pytest.mark.parametrize("mode", ["return_error", "raise", "timeout", "cancel"])
def test_actual_traced_tools_record_result_errors_and_cancellation(mode):
    pytest.importorskip("browser_use")
    from browser_use import ActionResult
    from autoscraper.agent.tools import TracedTools

    tools = TracedTools()

    @tools.action("Fixture")
    async def fixture(mode: str):
        if mode == "return_error":
            return ActionResult(error="PRIVATE_ERROR")
        if mode == "raise":
            raise ValueError("PRIVATE_ERROR")
        if mode == "timeout":
            await asyncio.Event().wait()
        raise asyncio.CancelledError()

    action = tools.registry.create_action_model()(fixture={"mode": mode})
    memory = MemorySink()

    async def scenario():
        with Trace(memory).run(), labels(attempt=1, step=2):
            if mode == "cancel":
                with pytest.raises(asyncio.CancelledError):
                    await tools.act(action, browser_session=None)
            else:
                result = await tools.act(action, browser_session=None, action_timeout=0.01)
                assert result.error

    asyncio.run(scenario())
    start = next(e for e in memory.events if e["event_type"] == "tool.started")
    end = next(e for e in memory.events if e["event_type"] == "tool.finished")
    assert start["call_id"] == end["call_id"]
    assert end["step"] == 2 and end["attempt"] == 1
    assert end["payload"]["duration_seconds"] >= 0
    assert end["payload"]["outcome"] == ("raised" if mode == "cancel" else "error")
    if mode in {"return_error", "raise"}:
        assert "PRIVATE_ERROR" in end["payload"]["error"]


# 模型未报告 token 用量时应明确为空，不能误记为零。
@pytest.mark.parametrize("usage", [None, "request_failure"])
def test_missing_usage_is_explicit(monkeypatch, usage):
    pytest.importorskip("browser_use")
    from autoscraper.agent.responses import ChatOpenAIResponses

    create = AsyncMock(
        return_value=SimpleNamespace(status="completed", output_text="ok", usage=None)
    )
    if usage == "request_failure":
        create.side_effect = RuntimeError("PRIVATE_PROVIDER_ERROR")
    monkeypatch.setattr(
        ChatOpenAIResponses,
        "get_client",
        lambda self: SimpleNamespace(responses=SimpleNamespace(create=create)),
    )
    memory = MemorySink()
    with Trace(memory).run():
        if usage is None:
            asyncio.run(ChatOpenAIResponses(model="fixture").ainvoke([]))
        else:
            with pytest.raises(Exception):
                asyncio.run(ChatOpenAIResponses(model="fixture").ainvoke([]))
    metrics = memory.summary_data["metrics"]
    assert metrics["tokens"] is None
    assert metrics["usage_missing_calls"] == 1
    assert metrics["llm_calls_by_source"] == {"main": 1}
    if usage == "request_failure":
        end = next(e for e in memory.events if e["event_type"] == "llm.finished")
        assert "PRIVATE_PROVIDER_ERROR" in end["payload"]["message"]


# 框架未触发步骤结束回调时，外层清理仍需关闭追踪跨度。
def test_step_without_end_callback_is_closed_by_outer_cleanup():
    pytest.importorskip("browser_use")
    from autoscraper.observability.tracing import StepObserver

    agent = SimpleNamespace(
        state=SimpleNamespace(n_steps=7, last_result=None), history=SimpleNamespace(history=[])
    )
    observer = StepObserver()
    memory = MemorySink()

    async def scenario():
        with Trace(memory).run(), labels(attempt=2):
            await observer.start(agent)
            observer.planned(
                None,
                SimpleNamespace(
                    action=[
                        SimpleNamespace(
                            model_dump=lambda **kwargs: {
                                "navigate": {"url": "https://fixture.test/"}
                            },
                        )
                    ]
                ),
                7,
            )
            emit("checkpoint")
            observer.finish(agent, interrupted=True)
            observer.finish(agent, interrupted=True)  # Cleanup must not duplicate closure.
            emit("after.cleanup")

    asyncio.run(scenario())
    ends = [event for event in memory.events if event["event_type"] == "step.finished"]
    assert len(ends) == 1
    assert ends[0]["step"] == 7 and ends[0]["payload"]["incomplete"]
    planned = next(e for e in memory.events if e["event_type"] == "step.planned")
    assert planned["payload"]["actions"] == [{"tool": "navigate"}]
    assert "step" not in next(e for e in memory.events if e["event_type"] == "after.cleanup")


# 异常 Unicode 和递归结构不能破坏日志序列化或拖垮记录器。
def test_unusual_unicode_and_cyclic_payload_do_not_break_recording(tmp_path):
    sink = FileSink(tmp_path)
    circular = {"selector": "div\ud800", "count": float("nan")}
    circular["cycle"] = circular
    with Trace(sink).run():
        emit("tool.diagnostics", inputs=circular)
    events = [
        json.loads(line) for line in (sink.directory / "events.jsonl").read_text().splitlines()
    ]
    assert any(e["event_type"] == "tool.diagnostics" for e in events)
    assert not json.loads((sink.directory / "summary.json").read_text())["trace_errors"]


# 开启追踪后，成功、失败、预算和不支持等终态保持原有业务语义。
@pytest.mark.parametrize(
    "kind,status,code",
    [
        ("unsupported", "unsupported", "unsupported_capability"),
        ("agent_failure", "unfulfilled", "agent_failure"),
        ("needs_clarification", "needs_clarification", None),
        ("validation_timeout", "unfulfilled", "content_timeout"),
    ],
)
def test_workflow_terminal_paths_preserve_original_semantics(
    monkeypatch,
    settings,
    spec,
    kind,
    status,
    code,
):
    pytest.importorskip("browser_use")
    import autoscraper.workflow.runner as workflow
    from autoscraper.agent.explorer import AgentError, UnsupportedTaskError
    from autoscraper.agent.clarification import ClarificationRequired
    from autoscraper.scraping.executor import ContentTimeout

    async def generate(*args, **kwargs):
        if kind == "unsupported":
            raise UnsupportedTaskError("PRIVATE_REASON", ["title"])
        if kind == "agent_failure":
            raise AgentError("PRIVATE_ERROR")
        if kind == "needs_clarification":
            raise ClarificationRequired(
                ClarificationRequest(question="PRIVATE_QUESTION", explanation="PRIVATE_REASON")
            )
        return spec

    def execute(*args):
        raise ContentTimeout(str(spec.start_url), "PRIVATE_ERROR")

    monkeypatch.setattr(workflow, "generate_spec", generate)
    monkeypatch.setattr(ScraperExecutor, "run", execute)
    memory = MemorySink()
    outcome = run_task(str(spec.start_url), "前1本，字段名为title", settings, trace=Trace(memory))
    assert outcome.status == status
    if code:
        assert outcome.feedback[0].code == code
    assert memory.summary_data["outcome"]["status"] == status
    assert memory.summary_data["metrics"]["attempts"] == 1
    assert memory.summary_data["outcome"]["requirements"]["fields"] == ["title"]
    if code:
        assert "PRIVATE_" in memory.summary_data["outcome"]["feedback"][0]["message"]
    assert any(event["event_type"] == "attempt.finished" for event in memory.events)


# 在全新进程中检查日志初始化，避免追踪抢先阻断框架终端日志。
@pytest.mark.parametrize("preconfigured", [False, True])
def test_fresh_process_keeps_terminal_logging_and_captures_framework_logs(tmp_path, preconfigured):
    """pytest's root handlers otherwise hide browser-use's first-import guard."""
    pytest.importorskip("browser_use")
    import os
    import subprocess
    import sys

    script = """
import json
import logging
import sys
from pathlib import Path
from autoscraper.observability.tracing import FileSink, Trace

root = logging.getLogger()
assert not root.handlers
if sys.argv[2] == "True":
    root.addHandler(logging.StreamHandler(sys.stderr))
    root.setLevel(logging.INFO)
original_handlers = list(root.handlers)
sink = FileSink(Path(sys.argv[1]))
with Trace(sink).run():
    assert root.handlers == original_handlers
    import browser_use
    logging.getLogger("browser_use.agent").info("TERMINAL_MARKER")
    logging.getLogger("browser_use.agent").warning("FRAMEWORK_WARNING")
assert root.handlers
assert all(handler.__class__.__name__ != "_LogHandler"
           for name in ("browser_use", "bubus", "autoscraper")
           for handler in logging.getLogger(name).handlers)
print(json.dumps({"status": "validated"}))
"""
    env = {
        **os.environ,
        "BROWSER_USE_SETUP_LOGGING": "true",
        "BROWSER_USE_LOGGING_LEVEL": "info",
        "ANONYMIZED_TELEMETRY": "false",
        "BROWSER_USE_CLOUD_SYNC": "false",
    }
    env.pop("BROWSER_USE_DEBUG_LOG_FILE", None)
    env.pop("BROWSER_USE_INFO_LOG_FILE", None)
    result = subprocess.run(
        [sys.executable, "-c", script, str(tmp_path), str(preconfigured)],
        capture_output=True,
        text=True,
        env=env,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr
    assert result.stderr.count("TERMINAL_MARKER") == 1
    assert json.loads(result.stdout) == {"status": "validated"}
    assert not list(tmp_path.glob("*/console.log"))
    events = [
        json.loads(line) for line in next(tmp_path.glob("*/events.jsonl")).read_text().splitlines()
    ]
    logs = [e for e in events if e["event_type"] == "framework.log"]
    assert len(logs) == 1
    assert logs[0]["payload"]["message"] == "FRAMEWORK_WARNING"
    assert result.stderr.count("FRAMEWORK_WARNING") == 1


# 默认追踪保留有用诊断，只替换明确凭据，不泛化删除普通文本。
def test_default_policy_preserves_url_selectors_text_and_noncredential_headers():
    from autoscraper.observability.tracing import Sanitizer

    sanitizer = Sanitizer(secrets=("KNOWN_KEY",))
    original = "https://user:pw@example.test:8443/cars?page=2&sort=year&token=abc#details"
    result = sanitizer.clean(
        {
            "url": original,
            "selector": "a[aria-label='Next']",
            "task": "抓取前25条，保留原始字段",
            "reason": "Basic inspection found a Load More button without href",
            "headers": {"Content-Type": "application/json", "Authorization": "Bearer OTHER_KEY"},
            "cookie_count": 3,
            "authorization_status": "missing",
            "api_key": "KNOWN_KEY",
            "message": "Request to /cars?page=2 failed; Authorization: Bearer OTHER_KEY\nCookie: session=abc",
        }
    )
    assert (
        result["url"] == "https://example.test:8443/cars?page=2&sort=year&token=[redacted]#details"
    )
    assert result["selector"] == "a[aria-label='Next']"
    assert result["task"] == "抓取前25条，保留原始字段"
    assert result["reason"] == "Basic inspection found a Load More button without href"
    assert result["headers"] == {"Content-Type": "application/json", "Authorization": "[redacted]"}
    assert result["cookie_count"] == 3 and result["authorization_status"] == "missing"
    assert "OTHER_KEY" not in result["message"] and "session=abc" not in result["message"]
    assert "Request to /cars?page=2 failed" in result["message"]
    assert result["api_key"] == "[redacted]"


# 工具反馈应保留可读证据，同时不持久化完整 DOM。
def test_tool_feedback_keeps_evidence_without_persisting_entire_dom():
    from autoscraper.observability.tracing import summarize_tool_output

    nodes = [{"tag": "div", "attrs": {"class": f"card-{i}"}, "text": f"Car {i}"} for i in range(30)]
    nodes.append({"tag": "button", "attrs": {"id": "load-more"}, "text": "Load More"})
    feedback = summarize_tool_output(
        json.dumps(
            {
                "url": "https://example.test/load-more?page=1",
                "title": "Cars",
                "total_nodes": 100,
                "nodes": nodes,
            }
        )
    )
    assert "nodes" not in feedback
    assert len(feedback["nodes_sample"]) == 8
    assert feedback["controls_sample"][0]["text"] == "Load More"
    assert feedback["observed_nodes"] == 31 and feedback["total_nodes"] == 100
    assert feedback["url"].endswith("/load-more?page=1")
    assert "absence" in feedback["scope"]
    selector = {
        "status": "sample_failed",
        "count": 6,
        "samples": [
            {"status": "no_match", "matches": 0, "values": []},
        ],
    }
    assert summarize_tool_output(json.dumps(selector)) == selector


# 模型自报评价在存在时保留，缺失时不编造内容。
def test_model_assessments_remain_readable_and_optional():
    from autoscraper.observability.tracing import StepObserver

    memory = MemorySink()
    with Trace(memory).run():
        StepObserver().planned(
            None,
            SimpleNamespace(
                action=[],
                memory="6 records, need 25",
                next_goal="Inspect pagination",
                evaluation_previous_goal="Name selector passed",
            ),
            1,
        )
        StepObserver().planned(None, SimpleNamespace(action=[]), 2)
    planned = [e["payload"] for e in memory.events if e["event_type"] == "step.planned"]
    assert planned[0]["assessment"] == {
        "memory": "6 records, need 25",
        "next_goal": "Inspect pagination",
        "evaluation_previous_goal": "Name selector passed",
    }
    assert planned[1]["assessment"] == {}


# 框架异常保留诊断和堆栈，同时替换已知密钥。
def test_framework_exception_keeps_message_and_stack_without_known_key():
    memory = MemorySink()
    with Trace(memory, secrets=("KNOWN_KEY",)).run():
        try:
            raise ValueError("Navigation /cars?page=2 failed using KNOWN_KEY")
        except ValueError:
            logging.getLogger("browser_use.fixture").exception("Cannot open /cars?page=2")
    event = next(e for e in memory.events if e["event_type"] == "framework.log")
    assert event["payload"]["message"] == "Cannot open /cars?page=2"
    assert "ValueError" in event["payload"]["traceback"]
    assert "/cars?page=2" in event["payload"]["traceback"]
    assert "KNOWN_KEY" not in json.dumps(event)


# 运行目录按开始时间可排序，同时用 run_id 区分同时启动的任务。
def test_run_directories_sort_by_time_and_still_distinguish_simultaneous_runs(tmp_path, monkeypatch):
    from datetime import datetime, timedelta, timezone
    import autoscraper.observability.tracing as tracing

    earlier = datetime(2026, 10, 27, 8, 0, tzinfo=timezone.utc)
    moments = iter([earlier, earlier + timedelta(seconds=2), earlier + timedelta(seconds=2)])
    monkeypatch.setattr(tracing, "datetime", SimpleNamespace(now=lambda: next(moments)))
    ids = ["f" * 32, "0" * 32, "1" * 32]
    sinks = [FileSink(tmp_path) for _ in ids]
    for sink, run_id in zip(sinks, ids):
        sink.start(run_id)
    names = sorted(path.name for path in tmp_path.iterdir())
    assert [name.rsplit("_", 1)[1] for name in names] == ids
    assert len(set(names)) == 3
