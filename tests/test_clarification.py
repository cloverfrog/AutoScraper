"""Clarification pauses execution, preserves answers, and never assumes a default."""

import asyncio
import io
import json
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from autoscraper.agent import AgentSettings, generate_spec
from autoscraper.agent_cli import ask_user
from autoscraper.clarification import (
    ClarificationRequest,
    ClarificationRequired,
    ClarificationSession,
)
from autoscraper.validation import TaskRequirements


URL = "https://fixture.test/scroll"
REQUEST = ClarificationRequest(
    question="必须从原入口抓取，还是允许传统分页入口？",
    explanation="原页面依赖无限滚动，当前协议不支持；另一入口的数据和顺序尚未证明等价。",
    options=["必须使用原入口", "允许传统分页入口"],
)
RULE = {
    "start_url": "https://fixture.test/",
    "item_selector": "article",
    "fields": {"quote": {"selector": "p"}},
    "max_items": 1,
    "on_item_error": "fail",
}


class Terminal(io.StringIO):
    def isatty(self):
        return True


@pytest.mark.parametrize(
    "text, expected",
    [
        ("2\n", REQUEST.options[1]),
        ("保留原顺序\n", "保留原顺序"),
        ("\n", None),
        ("", None),
        ("9\n", None),
    ],
)
def test_cli_answers(monkeypatch, capsys, text, expected):
    monkeypatch.setattr("sys.stdin", Terminal(text))
    assert ask_user(REQUEST) == expected
    captured = capsys.readouterr()
    assert captured.out == ""
    assert REQUEST.explanation in captured.err


def test_non_terminal_does_not_consume_stdin(monkeypatch):
    stream = io.StringIO("yes\n")
    monkeypatch.setattr("sys.stdin", stream)
    assert ask_user(REQUEST) is None
    assert stream.tell() == 0


@pytest.mark.parametrize("answer", [None, "", "  "])
def test_no_answer_remains_pending(answer):
    session = ClarificationSession(handler=lambda request: answer, pending=REQUEST)
    with pytest.raises(ClarificationRequired):
        session.answer_pending()
    assert session.pending == REQUEST
    assert session.history == []


def test_interrupt_remains_pending():
    def interrupted(request):
        raise KeyboardInterrupt

    session = ClarificationSession(handler=interrupted, pending=REQUEST)
    with pytest.raises(ClarificationRequired):
        session.answer_pending()
    assert session.pending == REQUEST


def test_real_browser_use_action_batch_stops_at_question():
    bu = pytest.importorskip("browser_use")
    from autoscraper.agent_tools import build_tools

    session = ClarificationSession()
    tools = build_tools(clarification=session)
    action_model = tools.registry.create_action_model()
    agent = object.__new__(bu.Agent)
    browser = SimpleNamespace(
        _cached_browser_state_summary=None,
        id="test-browser",
        cdp_client=None,
        agent_focus_target_id="tab",
        get_current_page_url=AsyncMock(return_value=URL),
    )
    agent.browser_session = browser
    agent.tools = tools
    agent.file_system = None
    agent.settings = SimpleNamespace(page_extraction_llm=None)
    agent.sensitive_data = None
    agent.available_file_paths = None
    agent.extraction_schema = None
    agent._check_stop_or_pause = AsyncMock()
    agent._log_action = AsyncMock()
    agent._demo_mode_log = AsyncMock()
    agent.state = SimpleNamespace(n_steps=1)
    actions = [
        action_model(ask_user=REQUEST.model_dump()),
        action_model(navigate={"url": RULE["start_url"]}),
    ]
    results = asyncio.run(agent.multi_act(actions))
    assert len(results) == 1
    assert results[0].is_done and results[0].success is False
    assert session.pending == REQUEST
    assert json.loads(results[0].extracted_content)["status"] == "needs_clarification"
    assert agent._log_action.await_count == 1  # No queued navigate after the question.


@pytest.mark.parametrize("answer", ["允许传统分页入口", "必须使用原入口", None])
def test_generation_resumes_same_agent_or_returns_pending(monkeypatch, answer):
    bu = pytest.importorskip("browser_use")
    captured = {"runs": 0, "instances": 0, "followups": []}
    session = ClarificationSession(handler=lambda request: answer)

    class Browser:
        def __init__(self, **kwargs):
            pass

        async def kill(self):
            captured["killed"] = True

    class Agent:
        def __init__(self, **kwargs):
            captured["instances"] += 1
            captured["tools"] = kwargs["tools"]

        def add_new_task(self, text):
            captured["followups"].append(text)

        async def run(self, max_steps):
            assert max_steps == 4  # Resume never replenishes the step budget.
            captured["runs"] += 1
            if captured["runs"] == 1:
                action = captured["tools"].registry.registry.actions["ask_user"].function
                result = await action(**REQUEST.model_dump())
                return SimpleNamespace(final_result=lambda: result.extracted_content)
            assert answer in captured["followups"][-1]
            rejected = answer == REQUEST.options[0]
            return SimpleNamespace(
                is_successful=lambda: not rejected,
                final_result=lambda: json.dumps(
                    {"status": "unsupported", "reason": "原入口依赖无限滚动"} if rejected else RULE
                ),
            )

    monkeypatch.setattr(bu, "Agent", Agent)
    monkeypatch.setattr(bu, "BrowserSession", Browser)
    settings = AgentSettings(api_key="fake", model="test", base_url="https://llm.test", max_steps=4)
    from autoscraper.agent import UnsupportedTaskError

    async def run():
        return await generate_spec(
            URL,
            "前1条语录，字段名为quote",
            settings,
            requirements=TaskRequirements(fields=["quote"], count=1),
            llm=object(),
            wait_llm=SimpleNamespace(max_calls=0, calls=0),
            clarification=session,
        )

    if answer is None:
        with pytest.raises(ClarificationRequired):
            asyncio.run(run())
        assert captured["runs"] == 1
    elif answer == REQUEST.options[0]:
        with pytest.raises(UnsupportedTaskError):
            asyncio.run(run())
        assert captured["runs"] == 2
    else:
        assert str(asyncio.run(run()).start_url) == RULE["start_url"]
        assert captured["runs"] == 2
    assert captured["instances"] == 1
    assert captured["killed"]


def test_human_wait_excluded_from_timeout(monkeypatch):
    bu = pytest.importorskip("browser_use")
    captured = {}

    def answer(request):
        time.sleep(1.1)  # Longer than the entire active exploration budget.
        return "允许传统分页入口"

    session = ClarificationSession(handler=answer)

    class Browser:
        def __init__(self, **kwargs):
            pass

        async def kill(self):
            pass

    class Agent:
        runs = 0

        def __init__(self, **kwargs):
            captured.update(kwargs)

        def add_new_task(self, text):
            pass

        async def run(self, max_steps):
            self.runs += 1
            await asyncio.sleep(0.01)
            if self.runs == 1:
                action = captured["tools"].registry.registry.actions["ask_user"].function
                await action(**REQUEST.model_dump())
            return SimpleNamespace(
                is_successful=lambda: True, final_result=lambda: json.dumps(RULE)
            )

    monkeypatch.setattr(bu, "Agent", Agent)
    monkeypatch.setattr(bu, "BrowserSession", Browser)
    settings = AgentSettings(
        api_key="fake",
        model="test",
        base_url="https://llm.test",
        timeout_seconds=1,
    )
    result = asyncio.run(
        generate_spec(
            URL,
            "quotes",
            settings,
            llm=object(),
            wait_llm=SimpleNamespace(max_calls=0, calls=0),
            clarification=session,
        )
    )
    assert str(result.start_url) == RULE["start_url"]
    assert session.waiting_seconds >= 1.1


@pytest.mark.parametrize("mode", ["answer", "pending", "budget"])
def test_workflow_shares_answers_budget_and_waiting_time(monkeypatch, mode):
    pytest.importorskip("browser_use")
    import autoscraper.agent_tools as helpers
    import autoscraper.workflow as workflow
    from autoscraper.executor import ScrapeResult
    from autoscraper.spec import ScraperSpec

    model = SimpleNamespace(calls=0)
    wait_model = SimpleNamespace(calls=0)
    monkeypatch.setattr(helpers, "make_llm", lambda settings: model)
    monkeypatch.setattr(helpers, "make_wait_llm", lambda settings: wait_model)
    received = []
    executions = []

    async def generate(url, task, settings, **kwargs):
        received.append(kwargs)
        model.calls += 1
        session = kwargs["clarification"]
        if len(received) == 1:
            session.pending = REQUEST
            session.answer_pending()
            session.waiting_seconds += 1000  # Must not expire the shared workflow deadline.
            if mode == "budget":
                model.calls = 4
            broken = ScraperSpec.model_validate(RULE)
            broken.fields["quote"].required = False
            return broken
        assert model.calls == 2
        assert session is received[0]["clarification"]
        assert "必须使用原入口" in session.prompt()
        assert "Questions/explanation/options are Agent proposals" in session.prompt()
        assert settings.max_steps == 3
        # A rejected alternative is not used in the subsequent candidate.
        return ScraperSpec.model_validate({**RULE, "start_url": URL})

    def execute(self, spec):
        executions.append(spec)
        return ScrapeResult(records=[{"quote": "A"}], stop_reason="max_items")

    monkeypatch.setattr(workflow, "generate_spec", generate)
    monkeypatch.setattr(workflow.ScraperExecutor, "run", execute)
    settings = AgentSettings(
        api_key="fake",
        model="test",
        base_url="https://llm.test",
        max_steps=4,
    )
    outcome = workflow.run_task(
        URL,
        "前1条，字段名为quote",
        settings,
        execute=True,
        ask_user=(lambda request: "必须使用原入口") if mode != "pending" else None,
    )
    summary = outcome.summary()
    assert summary["original_url"] == URL
    if mode == "pending":
        assert outcome.status == "needs_clarification"
        assert summary["pending_clarification"] == REQUEST.model_dump()
        assert summary["agent_status"] == "not_completed"
        assert not summary["execution_success"] and not summary["task_fulfilled"]
        assert not executions and len(received) == 1
    elif mode == "budget":
        assert outcome.status == "unfulfilled"
        assert outcome.feedback[0].code == "budget_exhausted"
        assert not executions and len(received) == 1
        assert summary["llm_calls"] == 4
    else:
        assert outcome.status == "success"
        assert summary["actual_start_url"] == URL
        assert summary["clarifications"][0]["answer"] == "必须使用原入口"
        assert summary["llm_calls"] == 2
        assert len(executions) == 1 and len(received) == 2


@pytest.mark.parametrize("non_interactive", [False, True])
def test_cli_pending_preserves_existing_outputs(monkeypatch, capsys, tmp_path, non_interactive):
    import autoscraper.agent_cli as cli
    from autoscraper.workflow import TaskOutcome

    rule = tmp_path / "rule.json"
    data = tmp_path / "data.json"
    rule.write_text("original rule")
    data.write_text("original data")
    monkeypatch.setenv("AUTOSCRAPER_API_KEY", "fake")

    def run(*args, **kwargs):
        assert (kwargs["ask_user"] is None) == non_interactive
        return TaskOutcome(
            status="needs_clarification",
            original_url=URL,
            pending_clarification=REQUEST.model_dump(),
        )

    monkeypatch.setattr(cli, "run_task", run)
    monkeypatch.setattr(
        "sys.argv",
        [
            "autoscraper-agent",
            URL,
            "quotes",
            "--spec-output",
            str(rule),
            "--output",
            str(data),
            *(["--non-interactive"] if non_interactive else []),
        ],
    )
    assert cli.main() == 4
    summary = json.loads(capsys.readouterr().out)
    assert summary["status"] == "needs_clarification"
    assert summary["pending_clarification"]["question"] == REQUEST.question
    assert rule.read_text() == "original rule"
    assert data.read_text() == "original data"
    assert sorted(p.name for p in tmp_path.iterdir()) == ["data.json", "rule.json"]


def test_real_agent_continues_with_browser_and_history(monkeypatch):
    """Exercise pinned browser-use run/add_new_task with real local Chromium, no paid LLM."""
    pytest.importorskip("browser_use")
    import threading
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    from browser_use.llm.views import ChatInvokeCompletion
    from playwright.sync_api import sync_playwright

    from autoscraper.agent_tools import make_llm
    from autoscraper.openai_responses import ChatOpenAIResponses

    visits = []

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            visits.append(self.path)
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write(
                b"<html><title>Local fixture</title><article><p>A</p></article></html>"
            )

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    url = f"http://127.0.0.1:{server.server_port}/scroll"
    rule = {**RULE, "start_url": url.removesuffix("scroll")}
    calls = []

    async def scripted(self, messages, output_format=None, **kwargs):
        calls.append(messages)
        if len(calls) == 1:
            actions = [{"navigate": {"url": url}}]
        elif len(calls) == 2:
            actions = [{"inspect_page": {}}]
        elif len(calls) == 3:
            actions = [{"ask_user": REQUEST.model_dump()}, {"navigate": {"url": rule["start_url"]}}]
        else:
            assert len(calls) == 4
            # The follow-up request contains the user's response and initial task.
            text = "\n".join(str(message.content) for message in messages)
            assert "允许传统分页入口" in text
            assert url in text
            assert "retain-observation" in text
            actions = [{"done": {"data": rule}}]
        return ChatInvokeCompletion(
            completion=output_format.model_validate(
                {"memory": "retain-observation", "action": actions}
            ),
            usage=None,
        )

    monkeypatch.setattr(ChatOpenAIResponses, "ainvoke", scripted)
    with sync_playwright() as playwright:
        executable_path = playwright.chromium.executable_path
    settings = AgentSettings(
        api_key="fake",
        model="test",
        base_url="https://llm.test",
        max_steps=5,
        executable_path=executable_path,
        timeout_seconds=60,
    )
    model = make_llm(settings)

    def answer(request):
        # The queued alternative navigation must not have executed before the answer.
        assert "/scroll" in visits
        assert "/" not in visits
        return "允许传统分页入口"

    session = ClarificationSession(handler=answer)
    try:
        spec = asyncio.run(
            generate_spec(
                url,
                "前1条，字段名为quote",
                settings,
                llm=model,
                wait_llm=SimpleNamespace(max_calls=0, calls=0),
                clarification=session,
            )
        )
        assert str(spec.start_url) == rule["start_url"]
        assert model.calls == 4
        assert session.history[0]["answer"] == "允许传统分页入口"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
