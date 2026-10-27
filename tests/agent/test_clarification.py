# 需求澄清测试：覆盖终端回答、无回答暂停、动作批次停止和原 Agent 继续执行。
# 同时验证问答历史与预算共享、人工等待不占探索超时，以及待澄清时不覆盖产物。
"""Clarification pauses execution, preserves answers, and never assumes a default."""

import asyncio
import io
import json
import time
from importlib import import_module
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from autoscraper.agent.explorer import AgentSettings, generate_spec
from autoscraper.cli.agent import ask_user
from autoscraper.agent.clarification import (
    ClarificationRequest,
    ClarificationRequired,
    ClarificationSession,
)
from autoscraper.workflow.validation import TaskRequirements


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


# 可控制 isatty 的输入流替身，用于区分交互与非交互终端。
class Terminal(io.StringIO):
    def isatty(self):
        return True


# 终端处理器应正确解释选项编号和自由回答，不默认选择无效输入。
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


# 非交互输入应返回待澄清，不消费其他管道内容。
def test_non_terminal_does_not_consume_stdin(monkeypatch):
    stream = io.StringIO("yes\n")
    monkeypatch.setattr("sys.stdin", stream)
    assert ask_user(REQUEST) is None
    assert stream.tell() == 0


# 空回答保留原问题，不能伪造已完成澄清。
@pytest.mark.parametrize("answer", [None, "", "  "])
def test_no_answer_remains_pending(answer):
    session = ClarificationSession(handler=lambda request: answer, pending=REQUEST)
    with pytest.raises(ClarificationRequired):
        session.answer_pending()
    assert session.pending == REQUEST
    assert session.history == []


# EOF 或用户中断回答时仍返回待澄清状态。
def test_interrupt_remains_pending():
    def interrupted(request):
        raise KeyboardInterrupt

    session = ClarificationSession(handler=interrupted, pending=REQUEST)
    with pytest.raises(ClarificationRequired):
        session.answer_pending()
    assert session.pending == REQUEST


# 调用 ask_user 后必须停止同批后续动作，避免回答前执行替代方案。
def test_real_browser_use_action_batch_stops_at_question():
    bu = pytest.importorskip("browser_use")
    from autoscraper.agent.tools import build_tools

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


# 有答案时复用原 Agent 继续，无答案时保留请求并清理浏览器。
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
    from autoscraper.agent.explorer import UnsupportedTaskError

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


# 人工回答耗时即使超过探索预算，也不应消耗有效探索时间。
def test_human_wait_excluded_from_timeout(monkeypatch):
    bu = pytest.importorskip("browser_use")
    # 在替换 BrowserSession 前加载工具，避免单独运行时缓存替身作为参数注解。
    import_module("autoscraper.agent.tools")
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
    # 验证人工等待超过任务预算；不要求 Windows 计时结果精确达到 sleep 的参数。
    assert session.waiting_seconds > settings.timeout_seconds


# 多轮修复应共享问答、调用预算和人工等待累计值。
@pytest.mark.parametrize("mode", ["answer", "pending", "budget"])
def test_workflow_shares_answers_budget_and_waiting_time(monkeypatch, mode):
    pytest.importorskip("browser_use")
    import autoscraper.agent.tools as helpers
    import autoscraper.workflow.runner as workflow
    from autoscraper.scraping.executor import ScrapeResult
    from autoscraper.scraping.spec import ScraperSpec

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


# 待澄清退出时返回问题和退出码 4，并保留已有规则与数据。
@pytest.mark.parametrize("non_interactive", [False, True])
def test_cli_pending_preserves_existing_outputs(monkeypatch, capsys, tmp_path, non_interactive):
    import autoscraper.cli.agent as cli
    from autoscraper.workflow.runner import TaskOutcome

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


# 用真实本地 Chromium 与脚本模型验证框架的暂停/继续和不支持终态。
@pytest.mark.parametrize("mode", ["clarification", "unsupported"])
def test_real_agent_continues_with_browser_and_history(monkeypatch, tmp_path, mode):
    """Exercise pinned browser-use run/add_new_task with real local Chromium, no paid LLM."""
    pytest.importorskip("browser_use")
    import threading
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    from browser_use import Agent
    from browser_use.llm.views import ChatInvokeCompletion
    from playwright.sync_api import sync_playwright

    from autoscraper.agent.tools import make_llm
    from autoscraper.agent.responses import ChatOpenAIResponses
    from autoscraper.observability.tracing import FileSink, Trace, labels

    visits = []

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            visits.append(self.path)
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write(
                b"<html><title>Local fixture</title><article><p>A</p></article>"
                b"<button id='load-more'>Load More</button></html>"
            )

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    url = f"http://127.0.0.1:{server.server_port}/scroll"
    rule = {**RULE, "start_url": url.removesuffix("scroll")}
    calls = []
    trace = []
    runs = []
    original_run = Agent.run

    async def traced_run(agent, *args, **kwargs):
        # Exercise the public callbacks across both real run() invocations.
        runs.append((agent.task_id, agent.state.n_steps, len(agent.history.history)))
        original_start = kwargs.pop("on_step_start", None)
        original_end = kwargs.pop("on_step_end", None)
        original_plan = agent.register_new_step_callback

        async def start(current):
            trace.append(("start", current.state.n_steps))
            if original_start:
                await original_start(current)

        def plan(state, output, step):
            trace.append(("plan", step))
            if original_plan:
                original_plan(state, output, step)

        async def end(current):
            item = current.history.history[-1]
            trace.append(("end", item.metadata.step_number))
            assert item.metadata.duration_seconds >= 0
            assert item.result
            if original_end:
                await original_end(current)

        agent.register_new_step_callback = plan
        try:
            return await original_run(agent, *args, on_step_start=start, on_step_end=end, **kwargs)
        finally:
            agent.register_new_step_callback = original_plan

    monkeypatch.setattr(Agent, "run", traced_run)

    async def scripted(self, messages, output_format=None, **kwargs):
        calls.append(messages)
        memory = "retain-observation"
        if len(calls) == 1:
            actions = [{"navigate": {"url": url}}]
        elif len(calls) == 2:
            actions = [{"inspect_page": {}}]
        elif len(calls) == 3:
            if mode == "unsupported":
                memory = "Inspection found Load More button without href; test the visible record count."
                actions = [{"test_selector": {"item_selector": "article", "field_selector": "p"}}]
            else:
                actions = [{"ask_user": REQUEST.model_dump()}, {"navigate": {"url": rule["start_url"]}}]
        else:
            assert len(calls) == 4
            if mode == "unsupported":
                memory = "Verified 1 record, need 2; Load More has no href. Protocol requires an anchor."
                actions = [{"report_unsupported": {"reason": memory, "fields": ["quote"]}}]
            else:
                # The follow-up request contains the user's response and initial task.
                text = "\n".join(str(message.content) for message in messages)
                assert "允许传统分页入口" in text
                assert url in text
                assert "retain-observation" in text
                actions = [{"done": {"data": rule}}]
        return ChatInvokeCompletion(
            completion=output_format.model_validate(
                {"memory": memory, "action": actions}
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
    sink = FileSink(tmp_path / "runs")
    try:
        if mode == "unsupported":
            from autoscraper.workflow.runner import run_task
            monkeypatch.setattr("autoscraper.agent.tools.make_llm", lambda settings: model)
            outcome = run_task(url, "前2条，字段名为quote", settings, trace=Trace(sink))
            assert outcome.status == "unsupported"
            summary = json.loads((sink.directory / "summary.json").read_text())
            assert "Verified 1 record, need 2" in summary["outcome"]["feedback"][0]["message"]
            assert len(runs) == 1
        else:
            with Trace(sink).run(), labels(attempt=1):
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
            assert session.history[0]["answer"] == "允许传统分页入口"
            assert len(runs) == 2
            assert runs[0][0] == runs[1][0]
            assert runs[0][1:] == (1, 0)
            assert runs[1][1:] == (4, 3)
        assert model.calls == 4
        assert trace == [
            (kind, step) for step in range(1, 5) for kind in ("start", "plan", "end")
        ]
        events = [json.loads(line) for line in
                  (sink.directory / "events.jsonl").read_text().splitlines()]
        steps = [event for event in events if event["event_type"] == "step.started"]
        assert [event["step"] for event in steps] == [1, 2, 3, 4]
        tools = [event for event in events if event["event_type"] == "tool.started"]
        assert [event["payload"]["tool"] for event in tools] == (
            ["navigate", "inspect_page", "test_selector", "report_unsupported"]
            if mode == "unsupported" else ["navigate", "inspect_page", "ask_user", "done"]
        )
        assert all(event["attempt"] == 1 for event in steps + tools)
        histories = (sink.directory / "history/attempt-1.jsonl").read_text().splitlines()
        assert len(histories) == 4
        assert "retain-observation" in "\n".join(histories)
        if mode == "unsupported":
            finished = [event for event in events if event["event_type"] == "tool.finished"]
            inspection = finished[1]["payload"]["feedback"]
            button = next(node for node in inspection["controls_sample"] if node["tag"] == "button")
            assert button["text"] == "Load More" and "href" not in button["attrs"]
            assert finished[2]["payload"]["feedback"]["count"] == 1
            assessment = next(event for event in events if event["event_type"] == "step.planned" and event["step"] == 4)
            assert "Verified 1 record, need 2" in assessment["payload"]["assessment"]["memory"]
        else:
            assert "允许传统分页入口" in (sink.directory / "events.jsonl").read_text()
        assert not (sink.directory / "console.log").exists()
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
