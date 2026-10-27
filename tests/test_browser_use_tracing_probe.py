"""Offline component probes of browser-use 0.13.11 tracing boundaries.

Real framework dispatch, step, finalization and continuation methods are exercised;
browser infrastructure and model decisions are replaced. No browser or API calls.
The recorder below is a probe, not a production tracing or sanitization interface.
"""

import asyncio
import logging
import time
from importlib.metadata import version
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

pytest.importorskip("browser_use")

from browser_use import ActionResult, Agent, Tools
from browser_use.agent.views import AgentHistoryList, AgentOutput, AgentState, AgentStepInfo

from autoscraper.agent_tools import build_tools
from autoscraper.clarification import ClarificationSession


class ProbeTools(Tools):
    def __init__(self):
        self.clarification = ClarificationSession()
        self.__dict__.update(build_tools(clarification=self.clarification).__dict__)
        self.events = []

        @self.action("Probe exception and cancellation propagation")
        async def probe_failure(mode: str):
            if mode == "raise":
                raise ValueError("probe failure")
            if mode == "hang":
                await asyncio.Event().wait()
            raise asyncio.CancelledError()

    async def act(self, action, **kwargs):
        call_id = len(self.events)
        self.events.append(("start", call_id, action.model_dump(exclude_unset=True)))
        started = time.monotonic()
        try:
            result = await super().act(action, **kwargs)
        except BaseException as exc:
            self.events.append(("raised", call_id, type(exc).__name__))
            raise
        else:
            self.events.append(("result", call_id, result))
            return result
        finally:
            self.events.append(("duration", call_id, time.monotonic() - started))


class NavigationEvent:
    def __await__(self):
        return asyncio.sleep(0).__await__()

    async def event_result(self, **kwargs):
        return None


def fake_browser():
    return SimpleNamespace(
        event_bus=SimpleNamespace(dispatch=Mock(return_value=NavigationEvent())),
        get_current_page_url=AsyncMock(return_value="https://fixture.test/"),
        get_browser_state_summary=AsyncMock(return_value=SimpleNamespace(
            url="https://fixture.test/",
            dom_state=SimpleNamespace(_root=object(), llm_representation=lambda: "content"),
        )),
        cdp_client=object(),
        logger=logging.getLogger("probe.browser"),
    )


def test_pinned_version_and_builtin_and_project_tool_dispatch():
    assert version("browser-use") == "0.13.11"
    tools = ProbeTools()
    action_model = tools.registry.create_action_model()
    browser = fake_browser()

    async def scenario():
        result = await tools.act(
            action_model(navigate={"url": "https://fixture.test/"}), browser_session=browser
        )
        assert not result.error
        assert "fixture.test" in result.extracted_content
        result = await tools.act(action_model(ask_user={
            "question": "Which source?", "explanation": "Probe", "options": ["A", "B"],
        }), browser_session=browser)
        assert result.is_done and result.success is False and not result.error
        assert tools.clarification.pending.question == "Which source?"

    asyncio.run(scenario())
    assert [event[0] for event in tools.events] == ["start", "result", "duration"] * 2
    assert list(tools.events[0][2]) == ["navigate"]
    assert list(tools.events[3][2]) == ["ask_user"]
    assert all(event[2] >= 0 for event in tools.events if event[0] == "duration")
    browser.event_bus.dispatch.assert_called_once()


@pytest.mark.parametrize("mode", ["raise", "hang", "cancel"])
def test_tool_errors_timeout_and_cancellation(mode):
    tools = ProbeTools()
    action = tools.registry.create_action_model()(probe_failure={"mode": mode})

    async def scenario():
        if mode == "cancel":
            with pytest.raises(asyncio.CancelledError):
                await tools.act(action, browser_session=fake_browser())
            assert tools.events[1][0:3] == ("raised", 0, "CancelledError")
        else:
            result = await tools.act(
                action, browser_session=fake_browser(), action_timeout=0.01
            )
            assert result.error
            assert ("probe failure" if mode == "raise" else "timed out") in result.error
            assert tools.events[1][0] == "result"
        assert tools.events[-1][0] == "duration"

    asyncio.run(scenario())


def bare_agent():
    # Avoid Agent construction's filesystem, cloud and model setup. Methods under
    # investigation below remain real; only external dependencies are supplied.
    agent = Agent.__new__(Agent)
    agent.id = "probe-agent"
    agent.task_id = "probe-task"
    agent.state = AgentState()
    agent.history = AgentHistoryList(history=[])
    agent.settings = SimpleNamespace(step_timeout=1, save_conversation_path=None)
    agent._demo_mode_log = AsyncMock()
    agent._message_manager = SimpleNamespace(add_new_task=Mock(), last_state_message_text=None)
    agent.save_file_system_state = Mock()
    agent._log_step_completion_summary = Mock(return_value=None)
    agent.eventbus = SimpleNamespace(dispatch=Mock())
    return agent


def test_real_history_contains_metadata_and_raw_content():
    agent = bare_agent()
    agent.browser_session = None
    agent.step_start_time = time.time()
    agent.state.last_result = [ActionResult(extracted_content="RAW_PAGE_TEXT")]
    agent._message_manager.last_state_message_text = "RAW_STATE_MESSAGE"
    summary = SimpleNamespace(
        url="https://fixture.test/?token=RAW_SECRET", title="Page", tabs=[], screenshot=None,
        dom_state=SimpleNamespace(selector_map={}),
    )
    asyncio.run(agent._finalize(summary))
    assert agent.state.n_steps == 2
    item = agent.history.history[0]
    assert item.metadata.step_number == 1
    assert item.metadata.duration_seconds >= 0
    assert item.result[0].extracted_content == "RAW_PAGE_TEXT"
    assert item.state_message == "RAW_STATE_MESSAGE"
    assert "RAW_SECRET" in item.state.url


def test_page_change_guard_only_traces_executed_actions():
    tools = ProbeTools()
    action_model = tools.registry.create_action_model()
    agent = bare_agent()
    agent.browser_session = fake_browser()
    agent.browser_session.id = "probe-browser"
    agent.browser_session.agent_focus_target_id = "probe-target"
    agent.browser_session._cached_browser_state_summary = None
    agent.browser_session.browser_profile = SimpleNamespace(wait_between_actions=0)
    agent.tools = tools
    agent.file_system = None
    agent.settings.page_extraction_llm = None
    agent.sensitive_data = None
    agent.available_file_paths = []
    agent.extraction_schema = None
    agent._check_stop_or_pause = AsyncMock()
    agent._log_action = AsyncMock()
    results = asyncio.run(agent.multi_act([
        action_model(navigate={"url": "https://fixture.test/"}),
        action_model(ask_user={"question": "Skipped?", "explanation": "Probe"}),
    ]))
    assert len(results) == 1 and not results[0].error
    assert [event[0] for event in tools.events] == ["start", "result", "duration"]
    assert tools.clarification.pending is None


def test_step_callback_order_finalization_and_followup():
    agent = bare_agent()
    events = []
    agent.browser_session = None
    agent._prepare_context = AsyncMock(return_value=None)
    agent._post_process = AsyncMock()
    agent._handle_step_error = AsyncMock()

    def planned(state, output, step):
        events.append(("plan", step))

    agent.register_new_step_callback = planned

    async def model_decision(state):
        agent.state.last_model_output = AgentOutput(action=[])
        await agent._handle_post_llm_processing(state, [])

    async def execute():
        events.append(("tool", agent.state.n_steps))
        agent.state.last_result = [ActionResult(extracted_content="probe")]

    agent._get_next_action = model_decision
    agent._execute_actions = execute

    async def start(current):
        events.append(("start", current.state.n_steps))

    async def end(current):
        events.append(("end", current.state.n_steps))

    async def scenario():
        assert not await agent._execute_step(0, 4, AgentStepInfo(0, 4), start, end)
        history = agent.history
        agent.state.stopped = True
        agent.add_new_task("Continue after clarification")
        assert agent.task_id == "probe-task"
        assert agent.state.follow_up_task and not agent.state.stopped
        assert agent.history is history
        assert not await agent._execute_step(1, 4, AgentStepInfo(1, 4), start, end)
        await agent.eventbus.stop(clear=True, timeout=1)

    asyncio.run(scenario())
    assert events == [
        ("start", 1), ("plan", 1), ("tool", 1), ("end", 2),
        ("start", 2), ("plan", 2), ("tool", 2), ("end", 3),
    ]
    agent._handle_step_error.assert_not_called()


@pytest.mark.parametrize("mode", ["timeout", "cancel", "raise"])
def test_step_end_is_not_a_finally_hook(mode):
    agent = bare_agent()
    agent.settings.step_timeout = 0.01
    events = []

    async def interrupted_step(info):
        if mode == "timeout":
            await asyncio.Event().wait()
        if mode == "cancel":
            raise asyncio.CancelledError()
        raise RuntimeError("escaped step failure")

    agent.step = interrupted_step

    async def start(current):
        events.append("start")

    async def end(current):
        events.append("end")

    async def scenario():
        call = agent._execute_step(0, 4, AgentStepInfo(0, 4), start, end)
        if mode == "timeout":
            assert not await call
            assert agent.state.n_steps == 2
            assert "timed out" in agent.state.last_result[0].error
        else:
            with pytest.raises(asyncio.CancelledError if mode == "cancel" else RuntimeError):
                await call

    asyncio.run(scenario())
    assert events == (["start", "end"] if mode == "timeout" else ["start"])
