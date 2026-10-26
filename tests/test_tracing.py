"""Tracing contracts, failure isolation and privacy without paid LLM calls."""

import asyncio
import json
import logging
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from autoscraper.agent import AgentSettings
from autoscraper.clarification import ClarificationRequest, ClarificationSession
from autoscraper.executor import ScrapeResult, ScraperExecutor
from autoscraper.spec import ScraperSpec
from autoscraper.tracing import FileSink, Trace, current_trace, emit, labels, operation
from autoscraper.workflow import TaskOutcome, run_task


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


@pytest.fixture
def settings():
    return AgentSettings(
        api_key="PRIVATE_API_KEY", model="fixture", timeout_seconds=30, base_url="https://llm.test"
    )


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
            logging.getLogger("fixture").warning("PRIVATE_LOG %s", "PRIVATE_API_KEY")
            # Events and history are visible before normal completion.
            assert (sink.directory / "events.jsonl").read_text()
            assert (sink.directory / "history/attempt-2.jsonl").read_text()
        trace.outcome = TaskOutcome(status="validated").summary()
    contents = "\n".join(path.read_text() for path in sink.directory.rglob("*") if path.is_file())
    assert "PRIVATE_" not in contents
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


@pytest.mark.parametrize("failure", ["start", "event", "console", "history", "summary"])
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
        trace.console({"message": "text"})
        trace.history({"step_number": 1})
        emit("test.another")
        trace.outcome = TaskOutcome(status="success").summary()
    assert memory.summary_data["outcome"]["status"] == "success"
    assert len(trace.errors) == 1
    stderr = capsys.readouterr().err
    assert stderr.count("trace consumer failed") == 1
    assert "PRIVATE_DISK_ERROR" not in stderr


def test_size_limit_reserves_summary_and_bounds_events(tmp_path, capsys):
    sink = FileSink(tmp_path, max_bytes=70000)
    trace = Trace(sink)
    with trace.run():
        for _ in range(100):
            emit("large", data=[{"status": "x" * 10000}] * 50)
    assert sum(p.stat().st_size for p in sink.directory.rglob("*") if p.is_file()) <= 70000
    assert json.loads((sink.directory / "summary.json").read_text())["trace_errors"]
    assert capsys.readouterr().err.count("trace consumer failed") == 1


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
    assert "PRIVATE_ERROR" not in events


def test_concurrent_runs_and_logging_are_isolated():
    memories = [MemorySink(), MemorySink()]

    async def worker(index):
        with Trace(memories[index]).run():
            with labels(attempt=index + 1):
                await asyncio.sleep(0)
                emit("worker", count=index)
                logging.getLogger("fixture").warning("PRIVATE_LOG")

    async def scenario():
        await asyncio.gather(worker(0), worker(1))

    asyncio.run(scenario())
    for index, memory in enumerate(memories):
        event = next(e for e in memory.events if e["event_type"] == "worker")
        assert event["attempt"] == index + 1
        assert event["payload"]["count"] == index
        assert len({e["run_id"] for e in memory.events}) == 1
    assert memories[0].events[0]["run_id"] != memories[1].events[0]["run_id"]


@pytest.mark.parametrize("source", ["requirements", "main", "wait"])
@pytest.mark.parametrize("invalid", [False, True])
def test_llm_usage_sources_and_parse_failure(monkeypatch, source, invalid):
    pytest.importorskip("browser_use")
    from pydantic import BaseModel
    from autoscraper.openai_responses import ChatOpenAIResponses

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
    assert "INVALID_PRIVATE_RESPONSE" not in json.dumps(memory.events)
    assert memory.events[-2]["event_type"] == "llm.finished"


def test_workflow_repair_and_feedback_share_run(monkeypatch, settings, spec):
    pytest.importorskip("browser_use")
    import autoscraper.workflow as workflow

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


def test_clarification_keeps_attempt_and_omits_answer():
    memory = MemorySink()
    session = ClarificationSession(handler=lambda request: "PRIVATE_ANSWER")
    session.pending = ClarificationRequest(question="PRIVATE_QUESTION", explanation="reason")
    with Trace(memory).run(), labels(attempt=2):
        session.answer_pending()
    assert session.history[0]["answer"] == "PRIVATE_ANSWER"
    event = next(e for e in memory.events if e["event_type"] == "clarification.answer")
    assert event["attempt"] == 2
    assert "PRIVATE_" not in json.dumps(memory.events)


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
    import autoscraper.agent_cli as cli

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
    assert "PRIVATE_" not in json.dumps(persisted)


def test_cli_artifact_error_preserves_workflow_result(monkeypatch, tmp_path, spec, capsys):
    import autoscraper.agent_cli as cli

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


def test_cli_logging_failure_does_not_change_success(monkeypatch, tmp_path, spec, capsys):
    import autoscraper.agent_cli as cli

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


@pytest.mark.parametrize("mode", ["return_error", "raise", "timeout", "cancel"])
def test_actual_traced_tools_record_result_errors_and_cancellation(mode):
    pytest.importorskip("browser_use")
    from browser_use import ActionResult
    from autoscraper.agent_tools import TracedTools

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
    assert "PRIVATE_ERROR" not in json.dumps(memory.events)


@pytest.mark.parametrize("usage", [None, "request_failure"])
def test_missing_usage_is_explicit(monkeypatch, usage):
    pytest.importorskip("browser_use")
    from autoscraper.openai_responses import ChatOpenAIResponses

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
    assert "PRIVATE_PROVIDER_ERROR" not in json.dumps(memory.events)


def test_step_without_end_callback_is_closed_by_outer_cleanup():
    pytest.importorskip("browser_use")
    from autoscraper.tracing import StepObserver

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
    import autoscraper.workflow as workflow
    from autoscraper.agent import AgentError, UnsupportedTaskError
    from autoscraper.clarification import ClarificationRequired
    from autoscraper.executor import ContentTimeout

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
    assert "PRIVATE_" not in json.dumps(memory.summary_data)
    assert any(event["event_type"] == "attempt.finished" for event in memory.events)
