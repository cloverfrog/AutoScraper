"""Offline v2 listing/detail integration tests using real Chromium and fixed routes."""

import csv
import json
import os

import pytest
from playwright.sync_api import sync_playwright
from pydantic import ValidationError

from autoscraper.executor import ScrapeError, ScraperExecutor
from autoscraper.spec import ScraperSpec
from autoscraper.validation import TaskRequirements, validate_data, validate_spec


BASE = "https://fixture.test/catalogue/"


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
        "pagination": {"next_selector": ".next"},
        "max_items": 25,
        "max_pages": 2,
        "timeout_ms": 200,
        "dedupe_by": ["url"],
        "on_item_error": "fail",
    }
    data.update(updates)
    return ScraperSpec.model_validate(data)


def card(number, href=None):
    href = href if href is not None else f"book-{number}/index.html"
    return (
        f'<article><h3><a title="Full book {number}" href="{href}">Short</a></h3>'
        f'<p class="price">£{number}.00</p></article>'
    )


def description(number):
    return f'<div id="product_description"></div><p> Description   {number} </p>'


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
    if mode == "fail":
        with pytest.raises(ScrapeError, match="item 1 detail="):
            ScraperExecutor().run_page(spec, page)
    else:
        result = ScraperExecutor().run_page(spec, page)
        assert result.records == []
        assert len(result.errors) == 1
        assert "item 1 detail=" in result.errors[0]
    assert page.context.pages == [page]


@pytest.mark.parametrize("selector", ["[invalid", "p >> text=x"])
def test_invalid_detail_css_fails_even_with_skip(site, selector):
    page, routes, _ = site
    routes[BASE + "book-1/index.html"] = description(1)
    spec = detail_spec(on_item_error="skip")
    spec.detail.fields["description"].selector = selector
    with pytest.raises(ScrapeError):
        ScraperExecutor().run_page(spec, page)
    assert page.context.pages == [page]


def test_redirect_base_and_optional_field(site):
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    from threading import Thread
    from autoscraper.spec import FieldSpec

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


@pytest.mark.parametrize("keys,expected_visits", [(["url"], 1), (["description"], 2), ([], 2)])
def test_dedupe_before_or_after_detail(site, keys, expected_visits):
    page, routes, _ = site
    routes[BASE + "page-1.html"] = card(1) + card(1)
    routes[BASE + "book-1/index.html"] = description(1)
    result = ScraperExecutor().run_page(detail_spec(dedupe_by=keys), page)
    assert len(result.records) == 1
    assert result.duplicates == 1
    assert result.detail_pages_visited == expected_visits


@pytest.mark.parametrize(
    "change",
    [
        {"version": "1"},
        {"detail": {"url_selector": "a", "fields": {}}},
        {"detail": {"url_selector": "a", "fields": {"title": {"selector": "h1"}}}},
        {"detail": {"url_selector": "a", "fields": {" ": {"selector": "p"}}}},
        {"detail": {"url_selector": "a", "fields": {"d": {"selector": "p"}}, "max_pages": 0}},
        {"detail": {"url_selector": "a", "fields": {"d": {"selector": "p"}}, "script": "x"}},
        {
            "detail": {"url_selector": "a", "fields": {"d": {"selector": "p", "required": False}}},
            "dedupe_by": ["d"],
        },
    ],
)
def test_protocol_constraints(change):
    with pytest.raises(ValidationError):
        detail_spec(**change)


def test_both_cli_exports_include_detail_fields(monkeypatch, tmp_path, capsys):
    from autoscraper import cli, agent_cli
    from autoscraper.executor import ScrapeResult
    from autoscraper.workflow import TaskOutcome

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


def test_executor_run_creates_shared_context(monkeypatch):
    from playwright.sync_api import Browser, BrowserType

    launch = BrowserType.launch
    new_context = Browser.new_context

    def headless_launch(self, **kwargs):
        return launch(self, **{**kwargs, "headless": True})

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

    monkeypatch.setattr(BrowserType, "launch", headless_launch)
    monkeypatch.setattr(Browser, "new_context", routed_context)
    result = ScraperExecutor().run(detail_spec(max_items=1))
    assert result.records[0]["description"] == "Description 1"
    assert result.detail_pages_visited == 1


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
    from autoscraper.executor import ItemError, http_url

    with pytest.raises(ItemError):
        http_url(BASE, value)


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
    from autoscraper.executor import http_url

    assert http_url(BASE + "page.html", value) == expected


def test_detail_limit_cli_reports_partial_result(monkeypatch, tmp_path, capsys):
    from autoscraper import cli
    from autoscraper.executor import ScrapeResult
    from autoscraper.workflow import TaskOutcome

    spec = detail_spec()
    result = ScrapeResult(stop_reason="detail_limit", detail_pages_visited=25)
    assert not TaskOutcome(status="unfulfilled", data=result).summary()["execution_success"]
    path = tmp_path / "rule.json"
    path.write_text(spec.model_dump_json())
    monkeypatch.setattr(ScraperExecutor, "run", lambda *args: result)
    monkeypatch.setattr("sys.argv", ["autoscraper", str(path), "--output", str(tmp_path / "x.csv")])
    assert cli.main() == 2
    assert json.loads(capsys.readouterr().out)["stop_reason"] == "detail_limit"
