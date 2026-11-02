# 访问前置条件回归：四种组合、无交互终态及访问失败均走统一执行入口。
from contextlib import nullcontext
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from autoscraper.browser.access import BrowserAccess, HandoffRequired
from autoscraper.scraping.errors import NavigationError
from autoscraper.scraping.executor import ScraperExecutor, ScrapeResult
from autoscraper.scraping.spec import ScraperSpec


@pytest.fixture
def spec():
    return ScraperSpec(start_url="https://fixture.test/", item_selector="article",
                       fields={"title": {"selector": "h2"}})


# 旧协议保留默认值，未知访问要求必须被拒绝，防止静默跳过前置操作。
def test_access_schema_defaults_and_validation(spec):
    assert spec.access == "none"
    assert ScraperSpec.model_validate_json(spec.model_dump_json()).access == "none"
    with pytest.raises(ValidationError):
        ScraperSpec.model_validate({**spec.model_dump(), "access": "auto"})


@pytest.fixture
def browser(monkeypatch):
    import autoscraper.scraping.executor as module

    events = []
    state = {"cookies": [], "origins": []}
    page = SimpleNamespace(goto=lambda *args, **kwargs: events.append("entry"))
    context = SimpleNamespace(new_page=lambda: page, storage_state=lambda: state)

    def new_context(**kwargs):
        events.append(("context", kwargs))
        return context

    def launch(**kwargs):
        events.append("launch")
        return SimpleNamespace(new_context=new_context, close=lambda: events.append("close"))

    monkeypatch.setattr(module, "sync_playwright", lambda: nullcontext(
        SimpleNamespace(chromium=SimpleNamespace(launch=launch))))
    return events, state


# 同一执行器运行两次，覆盖试抓取完成后复用快照、不重复接管的行为。
@pytest.mark.parametrize("requirement", ["none", "manual"])
@pytest.mark.parametrize("loaded", [False, True])
def test_access_matrix_and_state_reuse(monkeypatch, spec, browser, requirement, loaded):
    events, state = browser

    def handoff(request):
        events.append("handoff")
        return "continue"

    access = BrowserAccess(handler=handoff, state=state if loaded else None)
    rule = spec.model_copy(update={"access": requirement})

    def extract(self, rule, page):
        events.append("extract")
        return ScrapeResult()

    monkeypatch.setattr(ScraperExecutor, "run_page", extract)
    executor = ScraperExecutor(headless=False, access=access)
    executor.run(rule)
    executor.run(rule)
    assert events.count("extract") == 2
    expected_manual = requirement == "manual" and not loaded
    assert events.count("handoff") == int(expected_manual)
    assert events.count("entry") == int(expected_manual)
    contexts = [item[1] for item in events if isinstance(item, tuple)]
    assert contexts[0] == ({"storage_state": state} if loaded else {})
    assert contexts[1] == ({"storage_state": state} if loaded or expected_manual else {})
    if expected_manual:
        assert events.index("entry") < events.index("handoff") < events.index("extract")


# 无头模式即使误传处理器也不能挂起；没有处理器或 BrowserAccess 同样明确返回需求。
@pytest.mark.parametrize("headless,access", [
    (True, BrowserAccess(handler=lambda request: pytest.fail("不可调用处理器"))),
    (False, BrowserAccess()), (False, None),
])
def test_manual_without_interaction_does_not_launch(spec, browser, headless, access):
    with pytest.raises(HandoffRequired) as error:
        ScraperExecutor(headless=headless, access=access).run(
            spec.model_copy(update={"access": "manual"}))
    assert not error.value.cancelled
    assert browser[0] == []


# 状态可能失效，但访问错误不能成为再次接管或自动恢复的依据。
@pytest.mark.parametrize("loaded", [False, True])
def test_access_failure_never_retries_handoff(monkeypatch, spec, browser, loaded):
    calls = []
    access = BrowserAccess(handler=lambda request: calls.append(request) or "continue",
                           state=browser[1] if loaded else None)
    failure = NavigationError(str(spec.start_url), "HTTP 403")

    def extract(*args):
        raise failure

    monkeypatch.setattr(ScraperExecutor, "run_page", extract)
    with pytest.raises(NavigationError) as error:
        ScraperExecutor(headless=False, access=access).run(
            spec.model_copy(update={"access": "manual"}))
    assert error.value is failure
    assert len(calls) == (0 if loaded else 1)
    assert browser[0][-1] == "close"


# 首次指定认证保存路径但文件尚不存在时，CLI 仍应返回接管需求并保留原输出。
@pytest.mark.parametrize("flags", [[], ["--headed"]])
def test_cli_new_auth_file_requires_manual(monkeypatch, tmp_path, capsys, spec, flags):
    from autoscraper.cli.scraper import main

    rule = tmp_path / "rule.json"
    rule.write_text(spec.model_copy(update={"access": "manual"}).model_dump_json())
    output = tmp_path / "data.json"
    output.write_text("preserved")
    state = tmp_path / "new-state.json"
    monkeypatch.setattr("sys.argv", ["autoscraper", str(rule), "--output", str(output),
                                     "--auth-state", str(state), *flags])
    assert main() == 5
    assert '"needs_user"' in capsys.readouterr().out
    assert output.read_text() == "preserved"
    assert not state.exists()


# 展示人工操作入口时的网络错误仍是导航错误，不能被工作流当作选择器错误修复。
@pytest.mark.parametrize("timeout", [False, True])
def test_manual_entry_navigation_failure_is_classified(monkeypatch, spec, browser, timeout):
    from playwright.sync_api import Error, TimeoutError
    from autoscraper.scraping.errors import NavigationTimeout
    import autoscraper.scraping.executor as module

    events, state = browser
    failure = TimeoutError("navigation timed out") if timeout else Error("connection refused")

    def goto(*args, **kwargs):
        raise failure

    page = SimpleNamespace(goto=goto)
    context = SimpleNamespace(new_page=lambda: page)
    fake_browser = SimpleNamespace(new_context=lambda: context,
                                   close=lambda: events.append("close"))
    monkeypatch.setattr(module, "sync_playwright", lambda: nullcontext(
        SimpleNamespace(chromium=SimpleNamespace(launch=lambda **kwargs: fake_browser))))
    access = BrowserAccess(handler=lambda request: pytest.fail("导航失败不能触发接管"))
    with pytest.raises(NavigationTimeout if timeout else NavigationError) as error:
        ScraperExecutor(headless=False, access=access).run(
            spec.model_copy(update={"access": "manual"}))
    assert error.value.__cause__ is failure
    assert events == ["close"]
