"""Manual handoff control, authenticated workflow, and private state persistence."""

import asyncio
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import io
import json
import os
import time
from threading import Thread
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from playwright.async_api import async_playwright
from playwright.sync_api import sync_playwright

from autoscraper.browser.access import BrowserAccess, HandoffRequest, HandoffRequired
from autoscraper.cli.interaction import wait_for_user
from autoscraper.agent.explorer import AgentError, AgentSettings, generate_spec
from autoscraper.scraping.executor import ScraperExecutor
from autoscraper.scraping.spec import ScraperSpec


REQUEST = HandoffRequest(reason="请在浏览器中完成登录")
RULE = {
    "start_url": "https://fixture.test/",
    "item_selector": "article",
    "fields": {"title": {"selector": "h2"}},
    "max_items": 2,
}


class Terminal(io.StringIO):
    def isatty(self):
        return True


@pytest.mark.parametrize(
    "text,expected",
    [("\n", "continue"), ("q\n", "cancel"), ("password-do-not-store\n", "cancel"), ("", None)],
)
def test_terminal_control_never_returns_credentials(monkeypatch, capsys, text, expected):
    monkeypatch.setattr("sys.stdin", Terminal(text))
    assert wait_for_user(REQUEST) == expected
    output = capsys.readouterr()
    assert output.out == ""
    assert "password-do-not-store" not in output.err


def test_noninteractive_does_not_consume_input(monkeypatch):
    stream = io.StringIO("\n")
    monkeypatch.setattr("sys.stdin", stream)
    assert wait_for_user(REQUEST) is None
    assert stream.tell() == 0


@pytest.mark.parametrize("answer,cancelled", [(None, False), ("cancel", True)])
def test_pending_and_cancelled_are_distinct(answer, cancelled):
    access = BrowserAccess(handler=lambda request: answer, pending=REQUEST)
    with pytest.raises(HandoffRequired) as error:
        access.resume()
    assert error.value.cancelled is cancelled
    assert access.pending == REQUEST
    assert not access.handed_off


def test_state_file_is_private_atomic_and_absent_from_repr(tmp_path):
    path = tmp_path / "site-account" / "state.json"
    access = BrowserAccess(
        state={
            "cookies": [],
            "origins": [
                {
                    "origin": "https://fixture.test",
                    "localStorage": [{"name": "auth", "value": "private-sentinel"}],
                }
            ],
        }
    )
    assert "private-sentinel" not in repr(access)
    access.save(path)
    loaded = BrowserAccess()
    loaded.load(path)
    assert loaded.state == access.state
    if os.name == "posix":
        assert path.stat().st_mode & 0o777 == 0o600
    assert list(path.parent.iterdir()) == [path]
    path.write_text('{"private-sentinel": ', encoding="utf-8")
    with pytest.raises(ValueError, match="^Invalid authentication state file$"):
        loaded.load(path)


def test_wait_ends_real_action_batch():
    bu = pytest.importorskip("browser_use")
    from autoscraper.agent.tools import build_tools

    access = BrowserAccess()
    tools = build_tools(access=access)
    action_model = tools.registry.create_action_model()
    agent = object.__new__(bu.Agent)
    agent.browser_session = SimpleNamespace(
        _cached_browser_state_summary=None,
        id="probe",
        cdp_client=None,
        agent_focus_target_id="tab",
        get_current_page_url=AsyncMock(return_value=RULE["start_url"]),
    )
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
    result = asyncio.run(
        agent.multi_act(
            [
                action_model(wait_for_user=REQUEST.model_dump()),
                action_model(navigate={"url": RULE["start_url"]}),
            ]
        )
    )
    assert len(result) == 1 and result[0].is_done
    assert access.pending == REQUEST
    assert agent._log_action.await_count == 1


@pytest.mark.parametrize(
    "answer,final_success",
    [("continue", True), ("continue", False), ("cancel", False), (None, False)],
)
def test_same_agent_resumes_and_clears_old_selector_evidence(monkeypatch, answer, final_success):
    bu = pytest.importorskip("browser_use")
    observed = {"runs": 0, "instances": 0}

    def handler(request):
        if final_success:
            time.sleep(1.05)  # Longer than exploration timeout; excluded from its budget.
        return answer

    access = BrowserAccess(handler=handler)

    def save_state(cdp_url):
        access.state = {"cookies": [], "origins": []}

    capture = AsyncMock(side_effect=save_state)
    access.capture = capture

    class Browser:
        cdp_url = "http://localhost:1234"

        def __init__(self, **kwargs):
            pass

        async def kill(self):
            observed["killed"] = True

    class Agent:
        def __init__(self, **kwargs):
            observed["instances"] += 1
            self.tools = kwargs["tools"]

        def add_new_task(self, text):
            assert "Observe the current page again" in text

        async def run(self, max_steps):
            observed["runs"] += 1
            if observed["runs"] == 1:
                self.tools.submission.tests["old"] = "old page evidence"
                await self.tools.registry.registry.actions["wait_for_user"].function(
                    **REQUEST.model_dump()
                )
                return SimpleNamespace(final_result=lambda: None)
            assert not self.tools.submission.tests
            return SimpleNamespace(
                is_successful=lambda: final_success, final_result=lambda: json.dumps(RULE)
            )

    monkeypatch.setattr(bu, "BrowserSession", Browser)
    monkeypatch.setattr(bu, "Agent", Agent)
    settings = AgentSettings(
        api_key="fake", model="test", base_url="https://llm.test", timeout_seconds=1
    )
    call = generate_spec(RULE["start_url"], "two titles", settings, llm=object(), access=access)
    if answer == "continue":
        if final_success:
            assert asyncio.run(call).max_items == 2
            assert capture.await_count == 2
        else:
            with pytest.raises(AgentError):
                asyncio.run(call)
            capture.assert_awaited_once()
        assert access.state is not None
        assert observed["runs"] == 2
    else:
        with pytest.raises(HandoffRequired) as error:
            asyncio.run(call)
        assert error.value.cancelled == (answer == "cancel")
        assert observed["runs"] == 1
        capture.assert_not_awaited()
    assert observed["killed"] and observed["instances"] == 1


@contextmanager
def protected_site(unauthenticated_status=200):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_GET(self):
            authenticated = "auth=cookie-private-sentinel" in self.headers.get("Cookie", "")
            self.send_response(200 if authenticated else unauthenticated_status)
            self.send_header("Content-Type", "text/html")
            self.end_headers()
            if authenticated:
                body = """<main id="content"></main><script>
                    if(localStorage.getItem('auth') === 'storage-private-sentinel')
                    document.querySelector('main').innerHTML =
                    '<article><h2>A</h2></article><article><h2>B</h2></article>';
                    </script>"""
            else:
                body = "<h1>Login required</h1>"
            self.wfile.write(body.encode())

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_authenticated_workflow_transfers_and_refreshes_cookie_and_local_storage(
    monkeypatch, tmp_path
):
    bu = pytest.importorskip("browser_use")
    import autoscraper.agent.llm as models
    from autoscraper.workflow.runner import run_task
    from autoscraper.workflow.validation import ValidationSettings
    from autoscraper.observability.tracing import FileSink, Trace

    holder = {}
    model = SimpleNamespace(calls=0)
    monkeypatch.setattr(models, "make_llm", lambda settings: model)
    monkeypatch.setattr(
        models, "make_wait_llm", lambda settings: SimpleNamespace(calls=0, max_calls=0)
    )

    class Agent:
        def __init__(self, **kwargs):
            self.browser = kwargs["browser_session"]
            self.history = SimpleNamespace(history=[])
            self.tools = kwargs["tools"]
            self.runs = 0
            holder["browser"] = self.browser

        def add_new_task(self, text):
            pass

        async def run(self, max_steps, **callbacks):
            model.calls += 1
            self.runs += 1
            if self.runs == 1:
                await self.browser.start()
                async with async_playwright() as playwright:
                    attached = await playwright.chromium.connect_over_cdp(self.browser.cdp_url)
                    page = attached.contexts[0].pages[0]
                    await page.goto(holder["base"])
                    if await page.locator("article").count() == 2:
                        return SimpleNamespace(
                            is_successful=lambda: True,
                            final_result=lambda: json.dumps(holder["rule"]),
                        )
                await self.tools.registry.registry.actions["wait_for_user"].function(
                    **REQUEST.model_dump()
                )
                return SimpleNamespace(final_result=lambda: None)
            return SimpleNamespace(
                is_successful=lambda: True, final_result=lambda: json.dumps(holder["rule"])
            )

    def operate(request):
        def login():
            with sync_playwright() as playwright:
                attached = playwright.chromium.connect_over_cdp(holder["browser"].cdp_url)
                context = attached.contexts[0]
                context.add_cookies(
                    [{"name": "auth", "value": "cookie-private-sentinel", "url": holder["base"]}]
                )
                context.pages[0].evaluate(
                    "localStorage.setItem('auth', 'storage-private-sentinel')"
                )
                assert context.pages[0].url.startswith(holder["base"])
                snapshot = context.storage_state()
                assert any(
                    cookie["value"] == "cookie-private-sentinel" for cookie in snapshot["cookies"]
                )
                assert context.pages[0].evaluate(
                    "localStorage.getItem('auth') === 'storage-private-sentinel'"
                )

        with ThreadPoolExecutor(max_workers=1) as pool:
            pool.submit(login).result(timeout=30)
        return "continue"

    original = ScraperExecutor.run_page
    runs = []

    def extract(self, spec, page):
        assert any(
            cookie["value"] == "cookie-private-sentinel" for cookie in self.access.state["cookies"]
        )
        assert any(
            item["value"] == "storage-private-sentinel"
            for origin in self.access.state["origins"]
            for item in origin["localStorage"]
        )
        result = original(self, spec, page)
        runs.append(len(result.records))
        if len(runs) == 2:
            assert page.evaluate("localStorage.getItem('phase')") == "preview"
        page.evaluate("localStorage.setItem('phase', 'preview')")
        return result

    monkeypatch.setattr(bu, "Agent", Agent)
    monkeypatch.setattr(ScraperExecutor, "run_page", extract)
    access = BrowserAccess(handler=operate)
    with protected_site() as base:
        holder.update(base=base, rule={**RULE, "start_url": base})
        with sync_playwright() as playwright:
            executable_path = playwright.chromium.executable_path
        settings = AgentSettings(
            api_key="fake",
            model="test",
            base_url="https://llm.test",
            executable_path=executable_path,
            content_timeout_ms=2000,
        )
        trace_dir = tmp_path / "traces"
        result = run_task(
            base,
            "前2条，字段名为title",
            settings,
            execute=True,
            access=access,
            validation=ValidationSettings(max_items=1),
            trace=Trace(FileSink(trace_dir)),
        )
        assert result.status == "success", result.summary()
        assert result.clarifications == []
        assert runs == [1, 2]
        assert result.data.records == [{"title": "A"}, {"title": "B"}]
        state = tmp_path / "private.json"
        access.save(state)
        restored = BrowserAccess()
        restored.load(state)
        reused = run_task(
            base,
            "前2条，字段名为title",
            settings,
            execute=True,
            access=restored,
            trace=Trace(FileSink(trace_dir)),
        )
        assert reused.status == "success", reused.summary()
        assert not restored.handed_off
        for path in trace_dir.rglob("*.json*"):
            text = path.read_text(encoding="utf-8")
            assert "cookie-private-sentinel" not in text
            assert "storage-private-sentinel" not in text
        assert (
            ScraperExecutor(access=restored, executable_path=executable_path)
            .run(ScraperSpec.model_validate(holder["rule"]))
            .records
            == result.data.records
        )


@pytest.mark.parametrize("status,code", [("needs_user", 5), ("cancelled", 130)])
def test_cli_handoff_preserves_outputs(monkeypatch, tmp_path, capsys, status, code):
    from autoscraper.cli import agent as cli
    from autoscraper.workflow.outcome import TaskOutcome

    monkeypatch.setenv("AUTOSCRAPER_API_KEY", "fake")
    monkeypatch.setattr(
        cli,
        "run_task",
        lambda *args, **kwargs: TaskOutcome(status=status, pending_handoff=REQUEST.model_dump()),
    )
    spec = tmp_path / "rule.json"
    data = tmp_path / "data.json"
    for path in (spec, data):
        path.write_text("keep", encoding="utf-8")
    monkeypatch.setattr(
        "sys.argv",
        [
            "agent",
            RULE["start_url"],
            "two titles",
            "--spec-output",
            str(spec),
            "--output",
            str(data),
        ],
    )
    assert cli.main() == code
    assert spec.read_text() == data.read_text() == "keep"
    report = json.loads(capsys.readouterr().out)
    assert report["status"] == status
    assert report["pending_handoff"] == REQUEST.model_dump()
    if status == "needs_user":
        assert "--headed" in report["resume_hint"]


def test_executor_explicit_login_accepts_401_and_rechecks_target(monkeypatch):
    import autoscraper.scraping.executor as executor

    holder = {}

    @contextmanager
    def captured_playwright():
        with sync_playwright() as playwright:

            def launch(**kwargs):
                holder["browser"] = playwright.chromium.launch(**kwargs)
                return holder["browser"]

            yield SimpleNamespace(chromium=SimpleNamespace(launch=launch))

    def login(request):
        context = holder["browser"].contexts[0]
        context.add_cookies([{"name": "auth", "value": "cookie-private-sentinel", "url": base}])
        context.pages[0].evaluate("localStorage.setItem('auth', 'storage-private-sentinel')")
        return "continue"

    monkeypatch.setattr(executor, "sync_playwright", captured_playwright)
    with protected_site(401) as base:
        access = BrowserAccess(handler=login)
        result = ScraperExecutor(access=access, login=True).run(
            ScraperSpec.model_validate({**RULE, "start_url": base})
        )
        assert result.records == [{"title": "A"}, {"title": "B"}]
        assert access.state is not None
