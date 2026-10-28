# 内容等待与错误分类测试：覆盖 DOM 变化检测、语义等待预算、CSS 等待和超时取消。
# 同时检查执行器的列表/分页/详情等待时限，以及超时不会被误当成可修复选择器错误。
"""Bounded waits, independent semantic budget, and timeout classification."""

import asyncio
import os
from types import SimpleNamespace

import pytest
from playwright.sync_api import Error, TimeoutError as PlaywrightTimeout

from autoscraper.scraping.executor import (
    ContentTimeout,
    NavigationError,
    NavigationTimeout,
    ScrapeError,
    ScrapeFailure,
    ScrapeResult,
    ScraperExecutor,
    navigate,
)
from autoscraper.scraping.spec import ScraperSpec
from autoscraper.workflow.validation import validate_data
from autoscraper.workflow.requirements import TaskRequirements


# 可脚本化的语义模型替身，记录调用次数并模拟任务级调用上限。
class Model:
    def __init__(self, answers, max_calls=8):
        self.answers = iter(answers)
        self.calls = 0
        self.max_calls = max_calls

    async def ainvoke(self, messages, output_format=None):
        self.calls += 1
        return SimpleNamespace(completion={"ready": next(self.answers)})


# 按顺序提供 DOM 快照的浏览器会话替身。
class Session:
    def __init__(self, snapshots):
        self.snapshots = iter(snapshots)
        self.current = "{}"

    async def must_get_current_page(self):
        return self

    async def evaluate(self, *args):
        self.current = next(self.snapshots, self.current)
        return self.current


# 根据给定模型和时限构造等待器，供无浏览器的语义等待用例复用。
def waiter(model, timeout=1000):
    pytest.importorskip("browser_use")
    from autoscraper.agent.waiting import ContentWaiter

    return ContentWaiter(timeout, model)


# 单次等待工具调用内应轮询变化后的 DOM，并在满足条件时返回。
def test_semantic_wait_checks_changed_dom_in_one_call():
    model = Model([False, True])
    session = Session(['{"text":"loading"}', '{"text":"loading"}', '{"text":"quote"}'])
    result = asyncio.run(waiter(model).content(session, "A quote with author", 0.1, 3))
    assert result["status"] == "ready"
    assert result["checks"] == 3
    assert result["llm_calls"] == model.calls == 2
    assert "quote" in result["dom"]


# DOM 未变不重复调用模型，但仍消耗检查次数并最终结束等待。
def test_semantic_wait_unchanged_dom_still_exhausts_checks():
    model = Model([False])
    result = asyncio.run(waiter(model).content(Session(["{}"]), "A quote", 0.1, 3))
    assert result["status"] == "timeout"
    assert result["checks"] == 3
    assert model.calls == 1


# 多次语义等待必须共享同一模型调用预算。
def test_semantic_budget_is_shared_across_tool_calls():
    model = Model([False], max_calls=1)
    service = waiter(model)

    async def run():
        first = await service.content(Session(["{}"]), "Quote", 0.1, 1)
        second = await service.content(Session(['{"text":"new"}']), "Quote", 0.1, 1)
        return first, second

    first, second = asyncio.run(run())
    assert first["status"] == "timeout"
    assert second["status"] == "budget_exhausted"
    assert model.calls == 1


# 模型错误应单独反馈，不能伪装成内容未就绪。
def test_semantic_llm_error_is_not_content_timeout():
    class Broken(Model):
        async def ainvoke(self, *args, **kwargs):
            raise RuntimeError("provider unavailable")

    result = asyncio.run(waiter(Broken([])).content(Session(["{}"]), "Quote"))
    assert result["status"] == "llm_error"


# 达到等待截止时间时应取消仍未完成的模型请求。
def test_semantic_deadline_cancels_model_request():
    class Slow(Model):
        async def ainvoke(self, *args, **kwargs):
            await asyncio.sleep(5)

    result = asyncio.run(waiter(Slow([]), timeout=20).content(Session(["{}"]), "Quote"))
    assert result["status"] == "timeout"
    assert result["elapsed_ms"] < 1000


# 拒绝空条件或超范围的间隔、检查次数等等待参数。
@pytest.mark.parametrize(
    "params",
    [
        dict(max_checks=0),
        dict(max_checks=31),
        dict(interval_seconds=0),
        dict(success_criteria=""),
        dict(success_criteria="x" * 2001),
    ],
)
def test_semantic_parameters_bounded(params):
    arguments = {"success_criteria": "Quote", **params}
    result = asyncio.run(waiter(Model([])).content(Session([]), **arguments))
    assert result["status"] == "invalid_parameters"


# 已知 CSS 等待应遵守时限，并返回 DOM 或明确错误状态。
@pytest.mark.parametrize("outcome", ["ready", "timeout"])
def test_selector_wait_is_bounded_and_returns_dom(monkeypatch, outcome):
    from autoscraper.agent import waiting as agent_waiting

    seen = []

    async def wait(session, selector, timeout_ms):
        seen.append((selector, timeout_ms))
        if outcome == "timeout":
            raise PlaywrightTimeout("not attached")

    monkeypatch.setattr(agent_waiting, "selector_wait", wait)
    result = asyncio.run(
        waiter(None, timeout=50).selector(Session(['{"text":"quote"}']), ".quote", 30000)
    )
    assert seen == [(".quote", 50)]
    assert result["status"] == outcome
    if outcome == "ready":
        assert "quote" in result["dom"]


# 按操作异常类型区分导航超时与其他导航失败。
@pytest.mark.parametrize(
    "error,expected",
    [
        (PlaywrightTimeout("goto timed out"), NavigationTimeout),
        (Error("proxy unavailable"), NavigationError),
    ],
)
def test_navigation_error_types(error, expected):
    def fail(*args, **kwargs):
        raise error

    with pytest.raises(expected):
        navigate(SimpleNamespace(goto=fail), "https://fixture.test", 50)


# 等待和导航失败不应引导无效规则修复或重复报数量不足。
@pytest.mark.parametrize("code", ["content_timeout", "navigation_timeout"])
def test_timeout_feedback_does_not_suggest_repair_or_missing_rows(code):
    feedback = validate_data(
        ScrapeResult(
            errors=["timed out"],
            error_details=[ScrapeFailure(code, "timed out", "https://fixture.test")],
        ),
        TaskRequirements(fields=["quote"], count=25),
    )
    assert len(feedback) == 1
    assert feedback[0].code == code
    assert not feedback[0].recoverable


# 为内容加载测试创建并回收真实本地 Chromium 页面。
@pytest.fixture
def page():
    from playwright.sync_api import sync_playwright

    with sync_playwright() as p:
        browser = p.chromium.launch()
        yield browser.new_context().new_page()
        browser.close()


# 构造带独立内容等待上限的抓取规则。
def rule(**updates):
    return ScraperSpec.model_validate(
        {
            "start_url": "https://fixture.test/",
            "item_selector": "article",
            "fields": {"quote": {"selector": "p"}},
            "max_items": 1,
            "timeout_ms": 500,
            "content_timeout_ms": 2000,
            **updates,
        }
    )


# 页面延迟渲染应使用内容等待上限，而非已完成的导航超时。
def test_delayed_render_uses_content_timeout_independent_of_navigation(page):
    page.route(
        "**/*",
        lambda route: route.fulfill(
            content_type="text/html",
            body="""
        <body><script>setTimeout(() => {
            document.body.innerHTML = '<article><p>Delayed quote</p></article>';
        }, 700)</script></body>""",
        ),
    )
    result = ScraperExecutor().run_page(rule(), page)
    assert result.records == [{"quote": "Delayed quote"}]


# 内容未出现与 CSS 本身非法必须产生不同错误。
def test_content_timeout_and_invalid_css_are_distinct(page):
    page.route("**/*", lambda route: route.fulfill(body="<body>Loading</body>"))
    with pytest.raises(ContentTimeout):
        ScraperExecutor().run_page(rule(content_timeout_ms=50), page)
    with pytest.raises(ScrapeError) as caught:
        ScraperExecutor().run_page(rule(item_selector="[invalid"), page)
    assert not isinstance(caught.value, ContentTimeout)


# 可选联网测试：抓取真实延迟加载的 Quotes 页面。
@pytest.mark.live
@pytest.mark.skipif(os.getenv("RUN_LIVE") != "1", reason="Set RUN_LIVE=1 for real delayed page")
def test_live_delayed_quotes(page):
    result = ScraperExecutor().run_page(
        rule(
            start_url="https://quotes.toscrape.com/js-delayed/",
            item_selector="div.quote",
            fields={"quote": {"selector": ".text"}},
            timeout_ms=30000,
            content_timeout_ms=30000,
        ),
        page,
    )
    assert len(result.records) == 1
    assert result.records[0]["quote"]


# 已知 CSS 等待必须附着到聚焦 CDP 目标，并保留调用方浏览器。
def test_selector_wait_attaches_focused_cdp_target_and_preserves_browser():
    import socket
    from playwright.async_api import async_playwright
    from autoscraper.agent.waiting import selector_wait

    async def run():
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            port = sock.getsockname()[1]
        async with async_playwright() as p:
            browser = await p.chromium.launch(args=[f"--remote-debugging-port={port}"])
            try:
                context = await browser.new_context()
                await context.new_page()  # A different tab must not be selected by URL.
                page = await context.new_page()
                session = await context.new_cdp_session(page)
                info = await session.send("Target.getTargetInfo")
                await session.detach()
                await page.set_content("""<body><script>setTimeout(() => {
                    document.body.innerHTML = '<p class="quote">Ready</p>';
                }, 500)</script></body>""")
                focus = SimpleNamespace(
                    cdp_url=f"http://127.0.0.1:{port}",
                    agent_focus_target_id=info["targetInfo"]["targetId"],
                )
                await selector_wait(focus, ".quote", 3000)
                assert await page.locator(".quote").inner_text() == "Ready"
                assert browser.is_connected()
                with pytest.raises(ValueError, match="Invalid CSS"):
                    await selector_wait(focus, "[", 100)
                with pytest.raises(PlaywrightTimeout):
                    await selector_wait(focus, ".not-present", 50)
            finally:
                await browser.close()

    asyncio.run(run())


# 详情提取的内容超时在跳过或终止路径中都保留错误分类。
@pytest.mark.parametrize("mode", ["fail", "skip"])
def test_detail_content_timeout_preserves_type(page, mode):
    spec = rule(
        version="2",
        on_item_error=mode,
        content_timeout_ms=50,
        detail={"url_selector": "a", "fields": {"author": {"selector": ".author"}}},
    )
    page.context.route(
        "**/*",
        lambda route: route.fulfill(
            content_type="text/html",
            body=(
                '<article><p>Quote</p><a href="/detail">Detail</a></article>'
                if route.request.url == "https://fixture.test/"
                else "<body>Loading author</body>"
            ),
        ),
    )
    if mode == "fail":
        with pytest.raises(ContentTimeout):
            ScraperExecutor().run_page(spec, page)
    else:
        result = ScraperExecutor().run_page(spec, page)
        assert result.error_details[0].code == "content_timeout"
        feedback = validate_data(result, TaskRequirements(fields=["quote", "author"]))
        assert len(feedback) == 1
        assert not feedback[0].recoverable


# 每次进入下一列表页后都应重新等待列表内容。
def test_content_wait_applies_after_every_pagination_navigation(page):
    def serve(route):
        first = route.request.url == "https://fixture.test/"
        html = "<article><p>" + ("A" if first else "B") + "</p></article>"
        if first:
            html += '<a class="next" href="/2">Next</a>'
        route.fulfill(
            content_type="text/html",
            body="<body><script>setTimeout(() => {"
            + f"document.body.innerHTML = `{html}`;"
            + "}, 100)</script></body>",
        )

    page.route("**/*", serve)
    result = ScraperExecutor().run_page(
        rule(max_items=2, pagination={"next_selector": ".next"}), page
    )
    assert result.records == [{"quote": "A"}, {"quote": "B"}]
    assert result.pages_visited == 2


# 先解析并规范化 DOM JSON，再裁剪返回文本，避免先截断导致解析失败。
def test_large_dom_is_truncated_after_normalizing_json():
    model = Model([True])
    result = asyncio.run(
        waiter(model).content(Session(['{"text":"' + "x" * 40000 + '"}']), "Quote")
    )
    assert result["status"] == "ready"
    assert len(result["dom"]) == 30000


# 执行器 CLI 超时应给出正确错误码，并保留已有输出文件。
@pytest.mark.parametrize(
    "error_type,code",
    [
        (ContentTimeout, "content_timeout"),
        (NavigationTimeout, "navigation_timeout"),
        (ScrapeError, "selector_failure"),
    ],
)
def test_executor_cli_reports_error_code_and_preserves_output(
    monkeypatch, tmp_path, capsys, error_type, code
):
    import json
    import sys
    from autoscraper.cli.scraper import main

    spec_path = tmp_path / "spec.json"
    spec_path.write_text(rule().model_dump_json())
    output = tmp_path / "data.json"
    output.write_text("previous data")
    error = (
        error_type("bad CSS")
        if error_type is ScrapeError
        else error_type("https://fixture.test/", "timed out")
    )

    def execute(self, spec):
        assert spec.content_timeout_ms == 45000
        assert spec.timeout_ms == 70000
        raise error

    monkeypatch.setattr(ScraperExecutor, "run", execute)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "autoscraper",
            str(spec_path),
            "--output",
            str(output),
            "--content-timeout-ms",
            "45000",
            "--navigation-timeout-ms",
            "70000",
        ],
    )
    assert main() == 1
    assert json.loads(capsys.readouterr().err)["code"] == code
    assert output.read_text() == "previous data"
