# 基础执行器与导出测试：覆盖字段提取、分页循环、记录上限、错误策略和协议解析。
# 主要通过 Playwright 本地路由构造页面，无需外网；真实书站抓取为可选测试。
import csv
import json
import os
from pathlib import Path

import pytest
from playwright.sync_api import sync_playwright
from pydantic import ValidationError

from autoscraper.scraping.executor import ScrapeError, ScraperExecutor
from autoscraper.scraping.export import export_records
from autoscraper.scraping.spec import ScraperSpec


# 构造通用列表抓取规则，按用例覆盖分页、去重、字段和执行上限。
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
        pagination={"type": "link", "next_selector": ".next"},
        dedupe_by=["url"],
        timeout_ms=500,
    )
    data.update(updates)
    return ScraperSpec.model_validate(data)


# 为用例创建和关闭真实本地 Chromium 页面。
@pytest.fixture
def page():
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page()
        yield page
        browser.close()


# 生成列表记录 HTML，便于拼接分页与重复数据场景。
def item(name, href):
    return f'<article><h3>{name}</h3><a href="{href}">detail</a></article>'


# 将测试 URL 映射到固定 HTML，让导航走浏览器但不访问真实网站。
def route_pages(page, pages):
    page.route(
        "**/*",
        lambda route: route.fulfill(
            status=200, content_type="text/html; charset=utf-8", body=pages[route.request.url]
        ),
    )


# 验证跨页记录去重、文本空白归一化和相对链接补全。
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


# 检查记录数、页数上限与分页循环的停止原因。
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


# 检查字段提取失败时跳过单条或立即终止的行为。
def test_item_errors(page):
    route_pages(page, {"https://fixture.test/1": "<article><h3>missing URL</h3></article>"})
    result = ScraperExecutor().run_page(make_spec(), page)
    assert result.records == []
    assert len(result.errors) == 1
    with pytest.raises(ScrapeError, match="Required field"):
        ScraperExecutor().run_page(make_spec(on_item_error="fail"), page)


# 无法继续抓取的分页或提取错误应明确终止执行。
@pytest.mark.parametrize(
    "body,match",
    [
        ("<p>no list</p>", "Content did not appear"),
        (item("A", "/a") + '<a class="next">Next</a>', "requires a Next link"),
        (item("A", "javascript:alert(1)"), "Unsupported URL"),
    ],
)
def test_fatal_errors(page, body, match):
    route_pages(page, {"https://fixture.test/1": body})
    with pytest.raises(ScrapeError, match=match):
        ScraperExecutor().run_page(make_spec(on_item_error="fail"), page)


# HTTP 失败响应应转换为导航错误。
def test_http_error(page):
    page.route("**/*", lambda route: route.fulfill(status=503, body="Unavailable"))
    with pytest.raises(ScrapeError, match="HTTP 503"):
        ScraperExecutor().run_page(make_spec(), page)


# 非法 CSS 必须报告规则错误，而不是当作没有匹配元素。
@pytest.mark.parametrize("selector", ["[invalid", "article >> text=Book"])
def test_invalid_selector(page, selector):
    route_pages(page, {"https://fixture.test/1": item("A", "/a")})
    with pytest.raises(ScrapeError):
        ScraperExecutor().run_page(make_spec(item_selector=selector), page)


# 无分页配置、下一页缺失或禁用时应自然结束。
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


# 拒绝字段、转换和去重约束等不合法的输入规则。
@pytest.mark.parametrize(
    "updates",
    [
        {"max_items": 0},
        {"dedupe_by": ["unknown"]},
        {"version": "3"},
        {"python": "print(1)"},
        {"start_url": "file:///tmp/data"},
        {"fields": {"x": {"selector": "a", "extract": "attribute"}}},
        {"fields": {"x": {"selector": "a", "attribute": "href"}}},
    ],
)
def test_invalid_specs(updates):
    with pytest.raises(ValidationError):
        make_spec(**updates)


# 检查 JSON/CSV 内容、中文保存以及协议字段顺序。
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


# 可选联网测试：执行真实书站规则并检查前 100 条记录。
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


# 多元素按 DOM 顺序保留重复值和中文，去重同时支持默认全字段和显式列表键。
@pytest.mark.parametrize("dedupe_by", [[], ["tags"]])
def test_multi_element_extraction_and_dedupe(page, dedupe_by):
    body = '<article><b> 中文  标签 </b><b>two</b><b>two</b></article>'
    route_pages(page, {"https://fixture.test/1": body * 2 + '<article><b>other</b></article>'})
    spec = make_spec(fields={
        "tags": {"selector": "b", "extract": "texts"},
        "count": {"selector": "b", "extract": "count"},
        "zero": {"selector": ".missing", "extract": "count"},
        "empty": {"selector": ".missing", "extract": "texts", "required": False},
    }, dedupe_by=dedupe_by, pagination=None, max_items=2)
    result = ScraperExecutor().run_page(spec, page)
    assert result.errors == []
    assert result.duplicates == 1
    assert result.stop_reason == "max_items"
    assert result.records == [
        {"tags": ["中文 标签", "two", "two"], "count": 3, "zero": 0, "empty": []},
        {"tags": ["other"], "count": 1, "zero": 0, "empty": []},
    ]


# 必填列表缺失或含空文本沿用单条错误策略，不将可选空集合变为 None。
@pytest.mark.parametrize("body", ["<article></article>", "<article><b> </b></article>"])
def test_required_texts_errors(page, body):
    route_pages(page, {"https://fixture.test/1": body})
    spec = make_spec(fields={"tags": {"selector": "b", "extract": "texts"}},
                     dedupe_by=[], pagination=None)
    result = ScraperExecutor().run_page(spec, page)
    assert not result.records and len(result.errors) == 1
    with pytest.raises(ScrapeError, match="Required field 'tags'"):
        ScraperExecutor().run_page(spec.model_copy(update={"on_item_error": "fail"}), page)


# 多元素扩展不放宽属性约束，也不允许 absolute_url 或任意转换作用于列表/计数。
@pytest.mark.parametrize("extract", ["texts", "count"])
@pytest.mark.parametrize("extra", [{"attribute": "href"}, {"transform": "absolute_url"},
                                   {"transforms": []}])
def test_invalid_multi_element_specs(extract, extra):
    with pytest.raises(ValidationError):
        make_spec(fields={"x": {"selector": "b", "extract": extract, **extra}}, dedupe_by=[])


# 多元素的非法选择器仍报告规则错误，不能伪装为空集合或零。
@pytest.mark.parametrize("extract", ["texts", "count"])
def test_multi_element_invalid_css(page, extract):
    route_pages(page, {"https://fixture.test/1": "<article></article>"})
    with pytest.raises(ScrapeError):
        ScraperExecutor().run_page(make_spec(
            fields={"x": {"selector": "[", "extract": extract}}, dedupe_by=[]), page)


# JSON 保留真实数组和整数；CSV 数组以 JSON 单元格编码，保留空数组、引号和中文。
def test_multi_element_exports(tmp_path):
    records = [{"tags": ['中文, "标签"', "two"], "zero": 0}, {"tags": [], "zero": 0}]
    json_path = tmp_path / "multi.json"
    export_records(records, json_path, ["tags", "zero"])
    assert json.loads(json_path.read_text()) == records
    csv_path = tmp_path / "multi.csv"
    export_records(records, csv_path, ["tags", "zero"])
    with csv_path.open(newline="") as stream:
        rows = list(csv.DictReader(stream))
    assert [json.loads(row["tags"]) for row in rows] == [record["tags"] for record in records]
    assert [row["zero"] for row in rows] == ["0", "0"]
