import csv
import json
import os
from pathlib import Path

import pytest
from playwright.sync_api import sync_playwright
from pydantic import ValidationError

from autoscraper.executor import ScrapeError, ScraperExecutor
from autoscraper.export import export_records
from autoscraper.spec import ScraperSpec


def make_spec(**updates):
    data = dict(
        start_url="https://fixture.test/1",
        item_selector="article",
        fields={
            "title": {"selector": "h3"},
            "url": {
                "selector": "a",
                "extract": "attribute",
                "attribute": "href",
                "transform": "absolute_url",
            },
            "optional": {"selector": ".missing", "required": False},
        },
        pagination={"next_selector": ".next"},
        dedupe_by=["url"],
        timeout_ms=500,
    )
    data.update(updates)
    return ScraperSpec.model_validate(data)


@pytest.fixture
def page():
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page()
        yield page
        browser.close()


def item(name, href):
    return f'<article><h3>{name}</h3><a href="{href}">detail</a></article>'


def route_pages(page, pages):
    page.route(
        "**/*",
        lambda route: route.fulfill(
            status=200, content_type="text/html; charset=utf-8", body=pages[route.request.url]
        ),
    )


def test_pagination_dedupe_and_normalization(page):
    route_pages(
        page,
        {
            "https://fixture.test/1": item(" A  Book ", "/a")
            + '<a class="next" href="/2">Next</a>',
            "https://fixture.test/2": item("duplicate", "/a") + item("中文", "/b"),
        },
    )
    result = ScraperExecutor().run_page(make_spec(), page)
    assert result.pages_visited == 2
    assert result.duplicates == 1
    assert result.stop_reason == "no_next"
    assert result.records == [
        {"title": "A Book", "url": "https://fixture.test/a", "optional": None},
        {"title": "中文", "url": "https://fixture.test/b", "optional": None},
    ]


@pytest.mark.parametrize(
    "limits,reason,count",
    [
        ({"max_items": 1}, "max_items", 1),
        ({"max_pages": 1}, "max_pages", 2),
        ({}, "pagination_cycle", 2),
    ],
)
def test_limits_and_cycle(page, limits, reason, count):
    route_pages(
        page,
        {
            "https://fixture.test/1": item("A", "/a")
            + item("B", "/b")
            + '<a class="next" href="/1">Next</a>'
        },
    )
    result = ScraperExecutor().run_page(make_spec(**limits), page)
    assert len(result.records) == count
    assert result.stop_reason == reason


def test_item_errors(page):
    route_pages(page, {"https://fixture.test/1": "<article><h3>missing URL</h3></article>"})
    result = ScraperExecutor().run_page(make_spec(), page)
    assert result.records == []
    assert len(result.errors) == 1
    with pytest.raises(ScrapeError, match="Required field"):
        ScraperExecutor().run_page(make_spec(on_item_error="fail"), page)


@pytest.mark.parametrize(
    "body,match",
    [
        ("<p>no list</p>", "Browser failure"),
        (item("A", "/a") + '<a class="next">Next</a>', "requires a Next link"),
        (item("A", "javascript:alert(1)"), "Unsupported URL"),
    ],
)
def test_fatal_errors(page, body, match):
    route_pages(page, {"https://fixture.test/1": body})
    with pytest.raises(ScrapeError, match=match):
        ScraperExecutor().run_page(make_spec(on_item_error="fail"), page)


def test_http_error(page):
    page.route("**/*", lambda route: route.fulfill(status=503, body="Unavailable"))
    with pytest.raises(ScrapeError, match="HTTP 503"):
        ScraperExecutor().run_page(make_spec(), page)


@pytest.mark.parametrize("selector", ["[invalid", "article >> text=Book"])
def test_invalid_selector(page, selector):
    route_pages(page, {"https://fixture.test/1": item("A", "/a")})
    with pytest.raises(ScrapeError):
        ScraperExecutor().run_page(make_spec(item_selector=selector), page)


def test_no_pagination_and_disabled_next(page):
    route_pages(
        page,
        {
            "https://fixture.test/1": item("A", "/a")
            + '<a class="next" aria-disabled="true" href="/2">Next</a>'
        },
    )
    assert ScraperExecutor().run_page(make_spec(), page).stop_reason == "no_next"
    assert (
        ScraperExecutor().run_page(make_spec(pagination=None), page).stop_reason == "no_pagination"
    )


@pytest.mark.parametrize(
    "updates",
    [
        {"max_items": 0},
        {"dedupe_by": ["unknown"]},
        {"version": "2"},
        {"python": "print(1)"},
        {"start_url": "file:///tmp/data"},
        {"fields": {"x": {"selector": "a", "extract": "attribute"}}},
        {"fields": {"x": {"selector": "a", "attribute": "href"}}},
    ],
)
def test_invalid_specs(updates):
    with pytest.raises(ValidationError):
        make_spec(**updates)


def test_exports(tmp_path):
    records = [{"title": '中文, "Book"', "url": None}]
    for suffix in ("json", "csv"):
        path = tmp_path / f"books.{suffix}"
        export_records(records, path, ["title", "url"])
        if suffix == "json":
            assert json.loads(path.read_text(encoding="utf-8")) == records
        else:
            with path.open(encoding="utf-8", newline="") as stream:
                assert list(csv.DictReader(stream)) == [{"title": records[0]["title"], "url": ""}]
    empty = tmp_path / "empty.csv"
    export_records([], empty, ["title"])
    assert empty.read_text().strip() == "title"


@pytest.mark.live
@pytest.mark.skipif(os.getenv("RUN_LIVE") != "1", reason="Set RUN_LIVE=1 to access website")
def test_books_first_100():
    spec = ScraperSpec.model_validate_json(Path("examples/books.json").read_text())
    result = ScraperExecutor().run(spec)
    assert len(result.records) == len({row["url"] for row in result.records}) == 100
    assert result.pages_visited == 5
    assert result.stop_reason == "max_items"
    assert result.errors == []
    assert result.records[0]["title"] == "A Light in the Attic"
    assert all(row["price"].startswith("£") for row in result.records)
