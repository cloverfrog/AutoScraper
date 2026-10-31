# 筛选的协议、临时转换与真实离线导航回归：不访问外网或调用模型。
import pytest
from playwright.sync_api import sync_playwright
from pydantic import ValidationError

from autoscraper.scraping.filtering import evaluate_filters
from autoscraper.scraping.spec import FilterSpec, ScraperSpec
from autoscraper.scraping.executor import ScrapeResult, ScraperExecutor


# 构造单字段条件，转换与比较值完全来自声明式数据。
def condition(operator="lt", value=30, transforms=None, field="price"):
    return FilterSpec(field=field, operator=operator, value=value, transforms=transforms or [])


NUMBER = [{"op": "regex_extract", "pattern": r"\d+(?:\.\d+)?"}, {"op": "to_number"}]


# 比较操作覆盖数值边界、字符串精确比较以及列表成员；大小比较不隐式转换文本。
@pytest.mark.parametrize("source,operator,target,expected", [
    (3, "eq", 3, True), (3, "ne", 4, True), (3, "lt", 3, False),
    (3, "lte", 3, True), (3, "gt", 3, False), (3, "gte", 3, True),
    ("北京", "eq", "北京", True), ("北京", "ne", "北京", False),
    ("北京市", "contains", "北京", True), (["tag", "other"], "contains", "tag", True),
    (["tagged"], "contains", "tag", False), ([], "contains", "tag", False),
    (None, "eq", "", None), ("3", "lt", 4, None), (3, "eq", "3", None),
])
def test_filter_comparisons(source, operator, target, expected):
    assert evaluate_filters({"price": source}, [condition(operator, target)]).matched is expected


# 转换按顺序仅操作临时值，映射支持数字零，替换支持 Python 捕获组引用。
@pytest.mark.parametrize("source,transforms,target", [
    ("£25.80", NUMBER, 25.8),
    ("star-rating Four", [{"op": "regex_extract", "pattern": r"\b(One|Four)\b", "group": 1},
                          {"op": "map", "values": {"One": 1, "Four": 4}}], 4),
    ("£1,234.50", [{"op": "regex_replace", "pattern": "[£,]", "replacement": ""},
                   {"op": "to_number"}], 1234.5),
    ("none", [{"op": "map", "values": {"none": 0}}], 0),
    ("name=北京", [{"op": "regex_extract", "pattern": "name=(?P<city>.+)", "group": "city"}], "北京"),
    ("ab", [{"op": "regex_replace", "pattern": "(a)(b)", "replacement": r"\2\1"}], "ba"),
    ("text", [{"op": "regex_replace", "pattern": "unknown"}], "text"),
])
def test_temporary_transforms_preserve_original(source, transforms, target):
    record = {"price": source}
    assert evaluate_filters(record, [condition("eq", target, transforms)]).matched is True
    assert record == {"price": source}


# 合法转换无法处理特定值时返回未知及原因，而非抛字段错误。
@pytest.mark.parametrize("source,transforms", [
    ("面议", NUMBER), ("NaN", [{"op": "to_number"}]), ("1e9999", [{"op": "to_number"}]),
    ("Five", [{"op": "map", "values": {"Four": 4}}]),
    (["1", "2"], [{"op": "to_number"}]),
    ("b", [{"op": "regex_extract", "pattern": "(a)?b", "group": 1}]),
])
def test_unknown_filter_values(source, transforms):
    result = evaluate_filters({"price": source}, [condition(transforms=transforms)])
    assert result.matched is None and result.reason.startswith("price:")


# AND 中确定不匹配优先于未知；条件顺序不改变记录是否能被确定筛除。
@pytest.mark.parametrize("reverse", [True, False])
def test_and_false_overrides_unknown(reverse):
    filters = [condition(transforms=NUMBER), condition("eq", "北京", field="city")]
    assert evaluate_filters({"price": "面议", "city": "上海"}, filters[::-1] if reverse else filters).matched is False


# 错误正则、组引用、未知操作及非法数值目标在执行前就被拒绝。
@pytest.mark.parametrize("update", [
    {"transforms": [{"op": "regex_extract", "pattern": "["}]},
    {"transforms": [{"op": "regex_extract", "pattern": "a", "group": 1}]},
    {"transforms": [{"op": "regex_extract", "pattern": "a", "group": "missing"}]},
    {"transforms": [{"op": "regex_replace", "pattern": "a", "replacement": r"\2"}]},
    {"transforms": [{"op": "python", "code": "pass"}]},
    {"operator": "lt", "value": "30"}, {"value": float("inf")}, {"value": True},
    {"field": " "}, {"operator": "contains", "value": 3},
])
def test_invalid_filters(update):
    with pytest.raises(ValidationError):
        FilterSpec.model_validate({"field": "price", "operator": "lt", "value": 30, **update})


# 本地筛选只能引用列表字段；URL 覆盖声明不要求额外提取字段，但必须带查询参数。
def test_filter_protocol_boundaries():
    data = dict(start_url="https://fixture.test/", item_selector="article", fields={"title": {"selector": "h2"}})
    with pytest.raises(ValidationError, match="listing fields"):
        ScraperSpec(**data, filters=[condition()])
    with pytest.raises(ValidationError, match="listing fields"):
        ScraperSpec(**data, version="2", detail={"url_selector": "a", "fields": {"price": {"selector": "p"}}}, filters=[condition()])
    with pytest.raises(ValidationError, match="query parameters"):
        ScraperSpec(**data, url_filters=[condition()])
    spec = ScraperSpec(**{**data, "start_url": "https://fixture.test/?price=30"}, url_filters=[condition().model_dump(exclude={"transforms"})])
    assert not spec.filters


# 离线浏览器使用显式 Context，以允许列表与独立详情页共用本地路由。
@pytest.fixture
def page():
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_context().new_page()
        yield page
        browser.close()


# 首批全部筛除后仍推进；未知和无效详情链接不消耗预算，去重后才统计 max_items。
def test_filter_before_detail_and_pagination(page):
    pages = {
        "https://fixture.test/1": '<article><h2>drop</h2><b>£40</b></article>'
                                   '<article><h2>unknown</h2><b>面议</b></article>'
                                   '<a class="next" href="/2">Next</a>',
        "https://fixture.test/2": '<article><h2>A</h2><b>£25.80</b><a href="/a">Detail</a></article>' * 2
                                  + '<article><h2>B</h2><b>£0</b><a href="/b">Detail</a></article>',
        "https://fixture.test/a": '<p>Details A</p>', "https://fixture.test/b": '<p>Details B</p>',
    }
    visited = []
    def serve(route):
        visited.append(route.request.url)
        route.fulfill(body=pages[route.request.url], content_type="text/html; charset=utf-8")
    page.context.route("**/*", serve)
    spec = ScraperSpec(start_url="https://fixture.test/1", item_selector="article", version="2",
        fields={"title": {"selector": "h2"}, "price": {"selector": "b"}},
        filters=[condition(transforms=NUMBER)], pagination={"type": "link", "next_selector": ".next"},
        detail={"url_selector": "a", "fields": {"desc": {"selector": "p"}}, "max_pages": 2},
        max_items=2, dedupe_by=["title"], on_item_error="fail")
    result = ScraperExecutor().run_page(spec, page)
    assert result.stop_reason == "max_items" and result.pages_visited == 2
    assert result.detail_pages_visited == 2 and result.duplicates == 1
    assert result.items_extracted == 5 and result.filtered_out == result.filter_unknown == 1
    assert sum(result.filter_unknown_reasons.values()) == 1 and not result.errors
    assert [r["price"] for r in result.records] == ["£25.80", "£0"]
    assert visited == ["https://fixture.test/1", "https://fixture.test/2", "https://fixture.test/a", "https://fixture.test/b"]


# 可选筛选字段缺失记为未知；AND 组合只保留满足全部条件的记录。
def test_multiple_conditions_and_missing_optional_field(page):
    page.context.route("**/*", lambda route: route.fulfill(content_type="text/html; charset=utf-8", body='''
        <article><h2>A</h2><b>北京</b><i>tag</i><i>other</i></article>
        <article><h2>B</h2><b>上海</b><i>tag</i></article>
        <article><h2>C</h2><i>tag</i></article>
        <article><h2>D</h2><b>北京</b><i>tagged</i></article>'''))
    spec = ScraperSpec(start_url="https://fixture.test/", item_selector="article", fields={
        "title": {"selector": "h2"}, "city": {"selector": "b", "required": False},
        "tags": {"selector": "i", "extract": "texts"}}, filters=[
        {"field": "city", "operator": "eq", "value": "北京"},
        {"field": "tags", "operator": "contains", "value": "tag"}], on_item_error="fail")
    result = ScraperExecutor().run_page(spec, page)
    assert [r["title"] for r in result.records] == ["A"]
    assert result.filtered_out == 2 and result.filter_unknown == 1 and not result.errors


# 现有规则 CLI 暴露未知原因与筛除计数，仍按原始字段值导出，不将未知当成执行错误。
def test_filter_diagnostics_in_cli_and_summary(tmp_path, monkeypatch, capsys):
    import json
    from autoscraper.cli import scraper
    from autoscraper.workflow.outcome import TaskOutcome

    spec = ScraperSpec(start_url="https://fixture.test/", item_selector="article", fields={"price": {"selector": "b"}},
                       filters=[condition(transforms=NUMBER)])
    result = ScrapeResult(
        records=[{"price": "£25.80"}], items_extracted=3, filtered_out=1, filter_unknown=1,
        filter_unknown_reasons={"price: numeric value unavailable": 1}, stop_reason="no_pagination")
    monkeypatch.setattr(ScraperExecutor, "run", lambda *args: result)
    input_path = tmp_path / "spec.json"
    input_path.write_text(spec.model_dump_json())
    output = tmp_path / "output.json"
    monkeypatch.setattr("sys.argv", ["autoscraper", str(input_path), "--output", str(output)])
    assert scraper.main() == 0
    report = json.loads(capsys.readouterr().out)
    summary = TaskOutcome(status="success", spec=spec, data=result).summary()
    for name in ("items_extracted", "filtered_out", "filter_unknown", "filter_unknown_reasons"):
        assert report[name] == summary[name] == getattr(result, name)
    assert json.loads(output.read_text()) == [{"price": "£25.80"}]
