# 单层详情抓取测试：用本地页面覆盖列表与详情合并、失败、去重、跳转和独立访问预算。
# 同时检查 v1/v2 协议约束和两个 CLI 的详情字段导出；真实网站测试需显式开启。
"""Offline v2 listing/detail integration tests using real Chromium and fixed routes."""

import csv
import json
import os

import pytest
from playwright.sync_api import sync_playwright
from pydantic import ValidationError

from autoscraper.scraping.executor import ScrapeError, ScraperExecutor
from autoscraper.scraping.spec import ScraperSpec
from autoscraper.workflow.validation import validate_data, validate_spec
from autoscraper.workflow.requirements import TaskRequirements


BASE = "https://fixture.test/catalogue/"


# 构造带列表字段和详情描述字段的 v2 规则，允许各用例覆盖参数。
def detail_spec(**updates):
    data = {
        "version": "2",
        "start_url": BASE + "page-1.html",
        "item_selector": "article",
        "fields": {
            "title": {"selector": "h3 a", "extract": "attribute", "attribute": "title"},
            "price": {"selector": ".price"},
            "url": {
                "selector": "h3 a",
                "extract": "attribute",
                "attribute": "href",
                "transform": "absolute_url",
            },
        },
        "detail": {
            "url_selector": "h3 a",
            "fields": {
                "description": {"selector": "#product_description + p"},
            },
            "max_pages": 25,
        },
        "pagination": {"type": "link", "next_selector": ".next"},
        "max_items": 25,
        "max_pages": 2,
        # Leave room for Chromium scheduling; deliberate short waits have dedicated tests.
        "timeout_ms": 1000,
        "dedupe_by": ["url"],
        "on_item_error": "fail",
    }
    data.update(updates)
    return ScraperSpec.model_validate(data)


# 生成一条带详情链接的列表 HTML，用作本地网页夹具。
def card(number, href=None):
    href = href if href is not None else f"book-{number}/index.html"
    return (
        f'<article><h3><a title="Full book {number}" href="{href}">Short</a></h3>'
        f'<p class="price">£{number}.00</p></article>'
    )


# 生成详情文档的字段内容，供重定向、缺失和重复场景复用。
def description(number):
    return f'<div id="product_description"></div><p> Description   {number} </p>'


# 提供本地浏览器与页面路由，模拟多页列表和详情响应。
@pytest.fixture
def site():
    with sync_playwright() as p:
        browser = p.chromium.launch()
        context = browser.new_context()
        page = context.new_page()
        routes = {BASE + "page-1.html": card(1)}
        requests = []

        def serve(route):
            url = route.request.url
            requests.append(url)
            value = routes.get(url)
            if value is None:
                route.abort()
            elif isinstance(value, dict):
                route.fulfill(**value)
            else:
                route.fulfill(content_type="text/html; charset=utf-8", body=value)

        context.route("**/*", serve)
        yield page, routes, requests
        browser.close()


# 验证跨列表页抓取并合并 25 条完整详情记录。
def test_25_books_across_listing_pages(site):
    page, routes, requests = site
    routes[BASE + "page-1.html"] = "".join(card(i) for i in range(1, 21)) + (
        '<a class="next" href="page-2.html">Next</a>'
    )
    routes[BASE + "page-2.html"] = "".join(card(i) for i in range(21, 31))
    routes.update({BASE + f"book-{i}/index.html": description(i) for i in range(1, 31)})
    result = ScraperExecutor().run_page(detail_spec(), page)
    assert result.pages_visited == 2
    assert result.detail_pages_visited == 25
    assert result.stop_reason == "max_items"
    assert result.errors == []
    assert result.records == [
        {
            "title": f"Full book {i}",
            "price": f"£{i}.00",
            "url": BASE + f"book-{i}/index.html",
            "description": f"Description {i}",
        }
        for i in range(1, 26)
    ]
    assert len(requests) == 27
    assert page.url == BASE + "page-2.html"
    assert page.context.pages == [page]  # Executor closes its own detail Page.
    requirements = TaskRequirements(fields=["title", "price", "description"], count=25)
    assert validate_spec(detail_spec(), requirements) == []
    assert validate_data(result, requirements) == []


# 检查单条详情失败在 skip 和 fail 策略下的不同处理。
@pytest.mark.parametrize("mode", ["fail", "skip"])
@pytest.mark.parametrize(
    "failure",
    [
        "link",
        "href",
        "ambiguous_link",
        "unsafe",
        "field",
        "empty",
        "multiple",
        "http",
        "abort",
        "timeout",
    ],
)
def test_detail_item_errors(site, mode, failure):
    page, routes, requests = site
    routes[BASE + "book-1/index.html"] = description(1)
    spec = detail_spec(on_item_error=mode)
    if failure == "link":
        spec.detail.url_selector = ".missing"
    elif failure == "href":
        routes[BASE + "page-1.html"] = card(1).replace('href="book-1/index.html"', "")
    elif failure == "ambiguous_link":
        spec.detail.url_selector = "a"
        routes[BASE + "page-1.html"] = card(1).replace("</article>", '<a href="/x">x</a></article>')
    elif failure == "unsafe":
        routes[BASE + "page-1.html"] = card(1, "javascript:alert(1)")
        # Avoid the listing URL field rejecting it before the detail link is checked.
        spec.fields.pop("url")
        spec.dedupe_by = []
    elif failure == "field":
        routes[BASE + "book-1/index.html"] = "<p>Missing description</p>"
    elif failure == "empty":
        routes[BASE + "book-1/index.html"] = description(1).replace("Description   1", " ")
    elif failure == "multiple":
        spec.detail.fields["description"].selector = "p"
        routes[BASE + "book-1/index.html"] = "<p>A</p><p>B</p>"
    elif failure == "http":
        routes[BASE + "book-1/index.html"] = {"status": 503, "body": "Unavailable"}
    elif failure == "abort":
        routes.pop(BASE + "book-1/index.html")
    elif failure == "timeout":
        routes[BASE + "book-1/index.html"] = '<script src="/never.js"></script>' + description(1)
        page.context.route("**/never.js", lambda route: None)
    if failure == "empty":
        result = ScraperExecutor().run_page(spec, page)
        assert result.records[0]["description"] == "" and not result.errors
    elif failure == "field":
        if mode == "fail":
            with pytest.raises(ScrapeError, match="Required field 'description'"):
                ScraperExecutor().run_page(spec, page)
        else:
            result = ScraperExecutor().run_page(spec, page)
            assert result.records[0]["description"] is None and len(result.errors) == 1
    elif mode == "fail":
        with pytest.raises(ScrapeError, match="item 1 detail="):
            ScraperExecutor().run_page(spec, page)
    else:
        result = ScraperExecutor().run_page(spec, page)
        assert result.records == []
        assert len(result.errors) == 1
        assert "item 1 detail=" in result.errors[0]
    assert page.context.pages == [page]


# 非法 CSS 属于规则错误，不能被单条跳过策略掩盖。
@pytest.mark.parametrize("selector", ["[invalid", "p >> text=x"])
def test_invalid_detail_css_fails_even_with_skip(site, selector):
    page, routes, _ = site
    routes[BASE + "book-1/index.html"] = description(1)
    spec = detail_spec(on_item_error="skip")
    spec.detail.fields["description"].selector = selector
    with pytest.raises(ScrapeError):
        ScraperExecutor().run_page(spec, page)
    assert page.context.pages == [page]


# 详情相对链接应使用重定向后地址，可选字段缺失允许为空。
def test_redirect_base_and_optional_field(site):
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    from threading import Thread
    from autoscraper.scraping.spec import FieldSpec

    page, _, _ = site
    page.context.unroute("**/*")

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            if self.path == "/catalogue/book-1/index.html":
                self.send_response(302)
                self.send_header("Location", "/final/book.html")
                self.end_headers()
                return
            body = (
                card(1)
                if self.path == "/catalogue/page-1.html"
                else description(1) + '<a href="cover.jpg">cover</a>'
            ).encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    origin = f"http://127.0.0.1:{server.server_port}"
    try:
        spec = detail_spec(
            start_url=origin + "/catalogue/page-1.html", max_items=1, timeout_ms=2000
        )
        spec.detail.fields["cover"] = FieldSpec(
            selector="a", extract="attribute", attribute="href", transform="absolute_url"
        )
        spec.detail.fields["optional"] = FieldSpec(selector=".missing", required=False)
        result = ScraperExecutor().run_page(spec, page)
        assert result.records[0]["cover"] == origin + "/final/cover.jpg"
        assert result.records[0]["optional"] is None
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


# 详情导航失败也必须消耗详情访问预算。
@pytest.mark.parametrize("failed_first", [False, True])
def test_attempt_budget_includes_failed_navigation(site, failed_first):
    page, routes, requests = site
    routes[BASE + "page-1.html"] = card(1) + card(2)
    if not failed_first:
        routes[BASE + "book-1/index.html"] = description(1)
    routes[BASE + "book-2/index.html"] = description(2)
    spec = detail_spec(on_item_error="skip")
    spec.detail.max_pages = 1
    result = ScraperExecutor().run_page(spec, page)
    assert result.detail_pages_visited == 1
    assert result.stop_reason == "detail_limit"
    assert BASE + "book-2/index.html" not in requests
    assert len(result.records) == (0 if failed_first else 1)
    issues = validate_data(result, TaskRequirements(fields=["description"]))
    assert issues[0].code == "validation_limit"
    assert issues[0].recoverable is False


# 根据去重键是否含详情字段，验证提前去重与合并后去重。
@pytest.mark.parametrize("keys,expected_visits", [(["url"], 1), (["description"], 2), ([], 2)])
def test_dedupe_before_or_after_detail(site, keys, expected_visits):
    page, routes, _ = site
    routes[BASE + "page-1.html"] = card(1) + card(1)
    routes[BASE + "book-1/index.html"] = description(1)
    result = ScraperExecutor().run_page(detail_spec(dedupe_by=keys), page)
    assert len(result.records) == 1
    assert result.duplicates == 1
    assert result.detail_pages_visited == expected_visits


# 检查详情版本、字段重名和去重字段等跨字段协议约束。
@pytest.mark.parametrize(
    "change",
    [
        {"version": "1"},
        {"detail": {"url_selector": "a", "fields": {}}},
        {"detail": {"url_selector": "a", "fields": {"title": {"selector": "h1"}}}},
        {"detail": {"url_selector": "a", "fields": {" ": {"selector": "p"}}}},
        {"detail": {"url_selector": "a", "fields": {"d": {"selector": "p"}}, "max_pages": 0}},
        {"detail": {"url_selector": "a", "fields": {"d": {"selector": "p"}}, "script": "x"}},
    ],
)
def test_protocol_constraints(change):
    with pytest.raises(ValidationError):
        detail_spec(**change)


# 独立执行器和 Agent CLI 导出都应包含合并后的详情字段。
def test_both_cli_exports_include_detail_fields(monkeypatch, tmp_path, capsys):
    from autoscraper.cli import scraper as cli, agent as agent_cli
    from autoscraper.scraping.executor import ScrapeResult
    from autoscraper.workflow.outcome import TaskOutcome

    spec = detail_spec(max_items=1)
    record = {"title": "A", "price": "£1", "url": "https://fixture.test/a", "description": "正文"}
    result = ScrapeResult(records=[record], detail_pages_visited=1, stop_reason="max_items")
    rule_path = tmp_path / "rule.json"
    rule_path.write_text(spec.model_dump_json())
    output = tmp_path / "data.csv"
    monkeypatch.setattr(ScraperExecutor, "run", lambda *args: result)
    monkeypatch.setattr("sys.argv", ["autoscraper", str(rule_path), "--output", str(output)])
    assert cli.main() == 0
    assert json.loads(capsys.readouterr().out)["detail_pages"] == 1
    with output.open() as f:
        assert list(csv.DictReader(f)) == [record]
    monkeypatch.setenv("AUTOSCRAPER_API_KEY", "fake")
    monkeypatch.setattr(
        agent_cli,
        "run_task",
        lambda *args, **kwargs: TaskOutcome(status="success", spec=spec, data=result),
    )
    monkeypatch.setattr(
        "sys.argv",
        [
            "autoscraper-agent",
            BASE,
            "Get descriptions",
            "--spec-output",
            str(rule_path),
            "--output",
            str(output),
        ],
    )
    assert agent_cli.main() == 0
    with output.open() as f:
        assert list(csv.DictReader(f)) == [record]


# 执行器创建的列表页与详情页应共享同一浏览器上下文。
def test_executor_run_creates_shared_context(monkeypatch):
    from playwright.sync_api import Browser, BrowserType

    launch = BrowserType.launch
    new_context = Browser.new_context

    with sync_playwright() as playwright:
        executable_path = playwright.chromium.executable_path
    launches = []

    def configured_launch(self, **kwargs):
        launches.append(kwargs)
        return launch(self, **kwargs)

    def routed_context(self, **kwargs):
        context = new_context(self, **kwargs)
        context.route(
            "**/*",
            lambda route: route.fulfill(
                content_type="text/html; charset=utf-8",
                body=card(1) if route.request.url.endswith("page-1.html") else description(1),
            ),
        )
        return context

    monkeypatch.setattr(BrowserType, "launch", configured_launch)
    monkeypatch.setattr(Browser, "new_context", routed_context)
    result = ScraperExecutor(executable_path=executable_path).run(detail_spec(max_items=1))
    assert launches == [{"headless": True, "executable_path": executable_path}]
    assert result.records[0]["description"] == "Description 1"
    assert result.detail_pages_visited == 1


# 可选联网测试：在真实 Books to Scrape 上抓取 25 条详情记录。
@pytest.mark.live
@pytest.mark.skipif(os.getenv("RUN_LIVE") != "1", reason="Set RUN_LIVE=1 to access Books to Scrape")
def test_live_books_25_details():
    from pathlib import Path

    spec = ScraperSpec.model_validate_json(Path("examples/books.detail.json").read_text())
    with sync_playwright() as p:
        browser = p.chromium.launch()
        try:
            result = ScraperExecutor().run_page(spec, browser.new_context().new_page())
        finally:
            browser.close()
    assert len(result.records) == len({row["url"] for row in result.records}) == 25
    assert result.pages_visited == 2
    assert result.detail_pages_visited == 25
    assert result.stop_reason == "max_items"
    assert result.errors == []
    assert result.records[0]["title"] == "A Light in the Attic"
    assert all(
        row["description"].strip() and row["price"].startswith("£") for row in result.records
    )


# 拒绝非 HTTP(S) 或格式损坏的详情链接。
@pytest.mark.parametrize(
    "value",
    [
        "javascript:alert(1)",
        "data:text/html,hi",
        "file:///tmp/x",
        "mailto:a@example.com",
        "http://[bad",
        "https://",
    ],
)
def test_detail_url_rejects_non_http_and_malformed_values(value):
    from autoscraper.scraping.executor import ItemError, http_url

    with pytest.raises(ItemError):
        http_url(BASE, value)


# 检查相对链接、绝对链接等详情 URL 的补全规则。
@pytest.mark.parametrize(
    "value,expected",
    [
        ("../book/index.html", "https://fixture.test/book/index.html"),
        ("/book", "https://fixture.test/book"),
        ("//other.test/book", "https://other.test/book"),
        ("https://other.test/book", "https://other.test/book"),
    ],
)
def test_detail_url_resolution(value, expected):
    from autoscraper.scraping.executor import http_url

    assert http_url(BASE + "page.html", value) == expected


# 独立执行器达到详情上限时仍可导出部分数据，并以退出码 2 标记。
def test_detail_limit_cli_reports_partial_result(monkeypatch, tmp_path, capsys):
    from autoscraper.cli import scraper as cli
    from autoscraper.scraping.executor import ScrapeResult
    from autoscraper.workflow.outcome import TaskOutcome

    spec = detail_spec()
    result = ScrapeResult(stop_reason="detail_limit", detail_pages_visited=25)
    assert not TaskOutcome(status="unfulfilled", data=result).summary()["execution_success"]
    path = tmp_path / "rule.json"
    path.write_text(spec.model_dump_json())
    monkeypatch.setattr(ScraperExecutor, "run", lambda *args: result)
    monkeypatch.setattr("sys.argv", ["autoscraper", str(path), "--output", str(tmp_path / "x.csv")])
    assert cli.main() == 2
    assert json.loads(capsys.readouterr().out)["stop_reason"] == "detail_limit"


# 详情页空计数无需等待不存在的元素；合并后的列表字段仍能用于去重。
def test_detail_multi_element_fields_and_zero_count(site):
    page, routes, requests = site
    routes[BASE + "page-1.html"] = card(1) + card(2)
    for number in (1, 2):
        routes[BASE + f"book-{number}/index.html"] = '<b> A </b><b>B</b>'
    spec = detail_spec(detail={"url_selector": "h3 a", "fields": {
        "tags": {"selector": "b", "extract": "texts"},
        "count": {"selector": ".missing", "extract": "count"},
    }}, dedupe_by=["tags"])
    result = ScraperExecutor().run_page(spec, page)
    assert len(result.records) == 1 and result.duplicates == 1
    assert result.detail_pages_visited == 2 and result.errors == []
    assert result.records[0]["tags"] == ["A", "B"]
    assert result.records[0]["count"] == 0
    assert validate_data(result, TaskRequirements(fields=["tags", "count"])) == []
