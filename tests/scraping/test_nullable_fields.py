# 空值回归：字段声明、实际定位与值是否为空分开；不掩盖类型/路径错误。
import asyncio
import json

import pytest

import test_scraper
from autoscraper.agent.dom import SELECTOR_SCRIPT
from autoscraper.agent.submission import SubmissionChecks
from autoscraper.browser.network import AgentResponseCapture, CapturedResponse
from autoscraper.scraping.executor import ScrapeResult, ScraperExecutor
from autoscraper.scraping.export import export_records
from autoscraper.scraping.network import JsonFieldError, JsonPathError, extract_json_record
from autoscraper.scraping.spec import ScraperSpec
from autoscraper.workflow.requirements import TaskRequirements
from autoscraper.workflow.validation import validate_data, validate_spec

page = test_scraper.page


def network_spec(fields, **updates):
    data = dict(version="3", start_url="https://fixture.test/", network=dict(
        url_pattern="https://fixture.test/api", records_path=[], fields=fields),
        dedupe_by=["id"] if "id" in fields else [], max_items=20, timeout_ms=300,
        content_timeout_ms=300, on_item_error="fail")
    data.update(updates)
    return ScraperSpec.model_validate(data)


def route_json(page, rows):
    page.route("**/*", lambda route: route.fulfill(
        content_type="application/json" if route.request.url.endswith("/api") else "text/html",
        body=json.dumps(rows) if route.request.url.endswith("/api") else
        '<script>fetch("/api")</script>'))


@pytest.mark.parametrize("required", [True, False])
def test_network_preserves_empty_and_individually_missing_fields(page, tmp_path, required):
    rows = [dict(id=1, title="", tags=[], author=None),
            dict(id=2, title=None, tags=["", "tag"], author=dict(link="")),
            dict(id=3, title="Third", tags=None), dict(id=4)]
    route_json(page, rows)
    spec = network_spec({name: dict(path=path, required=required) for name, path in dict(
        id=["id"], title=["title"], tags=["tags"], link=["author", "link"]).items()})
    expected = [dict(id=1, title="", tags=[], link=None),
                dict(id=2, title=None, tags=["", "tag"], link=""),
                dict(id=3, title="Third", tags=None, link=None),
                dict(id=4, title=None, tags=None, link=None)]
    result = ScraperExecutor().run_page(spec, page)
    assert result.records == expected and not result.errors
    assert result.fields_found == dict(id=4, title=3, tags=3, link=2)
    requirements = TaskRequirements(fields=list(spec.all_fields))
    assert not validate_spec(spec, requirements) and not validate_data(result, requirements)
    export_records(result.records, tmp_path / "data.json", list(spec.all_fields))
    export_records(result.records, tmp_path / "data.csv", list(spec.all_fields))
    assert json.loads((tmp_path / "data.json").read_text()) == expected
    assert "[]" in (tmp_path / "data.csv").read_text()


@pytest.mark.parametrize("value", [None, "", [], [""], 0, False])
def test_explicit_empty_column_is_not_a_missing_path(page, value):
    route_json(page, [dict(id=1, value=value), dict(id=2, value=value)])
    spec = network_spec(dict(id=dict(path=["id"]), value=dict(path=["value"])))
    result = ScraperExecutor().run_page(spec, page)
    assert [row["value"] for row in result.records] == [value, value]
    assert not validate_data(result, TaskRequirements(fields=["value"]))


@pytest.mark.parametrize("required", [True, False])
def test_never_resolved_column_cannot_hide_behind_optional_flag(page, required):
    route_json(page, [dict(id=1), dict(id=2)])
    spec = network_spec(dict(id=dict(path=["id"]), value=dict(path=["typo"], required=required)))
    if required:
        with pytest.raises(JsonPathError, match="value.*did not resolve"):
            ScraperExecutor().run_page(spec, page)
    else:
        result = ScraperExecutor().run_page(spec, page)
        assert [row["value"] for row in result.records] == [None, None]
        issues = validate_data(result, TaskRequirements(fields=["value"]))
        assert issues[0].code == "missing_data" and issues[0].fields == ["value"]


@pytest.mark.parametrize("row,reason", [(dict(author="not an object"), "incompatible_path_type"),
                                        (dict(author=dict(link={})), "unsupported_value_type")])
def test_incompatible_json_types_remain_precise_errors(row, reason):
    source = network_spec(dict(link=dict(path=["author", "link"], required=False))).network
    with pytest.raises(JsonFieldError) as caught:
        extract_json_record(row, source)
    assert caught.value.field == "link" and caught.value.reason == reason
    assert caught.value.path == ["author", "link"]


def test_nullable_identifier_does_not_merge_distinct_records(page):
    route_json(page, [dict(id=None, value="A"), dict(id=None, value="B"), dict(id=None, value="A")])
    spec = network_spec(dict(id=dict(path=["id"], required=False), value=dict(path=["value"])))
    result = ScraperExecutor().run_page(spec, page)
    assert [row["value"] for row in result.records] == ["A", "B"] and result.duplicates == 1


def test_dom_empty_and_sparse_values_keep_records_and_evidence(page):
    test_scraper.route_pages(page, {"https://fixture.test/":
        '<article><b>1</b></article><article><b>2</b><span></span></article>'
        '<article><b>3</b><span>A</span><a href=""></a><i></i></article>'})
    spec = ScraperSpec.model_validate(dict(start_url="https://fixture.test/", item_selector="article",
        fields=dict(id=dict(selector="b"), title=dict(selector="span"),
                    tags=dict(selector="i", extract="texts"),
                    url=dict(selector="a", extract="attribute", attribute="href", transform="absolute_url")),
        dedupe_by=["id"], on_item_error="fail"))
    result = ScraperExecutor().run_page(spec, page)
    assert [row["title"] for row in result.records] == [None, "", "A"]
    assert [row["tags"] for row in result.records] == [[], [], [""]]
    assert [row["url"] for row in result.records] == [None, None, ""]
    assert not validate_data(result, TaskRequirements(fields=list(spec.fields)))
    # 前五条都没有字段时，第六条的真实定位仍可成为有界值采样之外的证据。
    page.set_content('<article></article>'*5 + '<article><span></span></article>')
    sampled = page.evaluate("(args) => ("+SELECTOR_SCRIPT+")(...args)", ["article", "span", ""])
    assert sampled["resolved_count"] == 1 and sampled["tested_count"] == 5
    assert SubmissionChecks.passed(sampled)


def test_validation_accepts_real_empty_values_but_not_absent_column():
    result = ScrapeResult(records=[dict(value=None), dict(value=[]), dict(value="")])
    assert not validate_data(result, TaskRequirements(fields=["value"]))
    assert validate_data(result, TaskRequirements(fields=["other"]))[0].fields == ["other"]


# 精确诊断继续检查后续记录，不把第一条坏数据之后的成功记录漏计；不返回原始数据。
def test_network_tool_reports_field_and_record_without_raw_values():
    from autoscraper.agent.tools import build_tools
    capture = AgentResponseCapture()
    capture.buffer.entries[7] = CapturedResponse(7, "https://fixture.test/api", "GET", status=200,
        page_url="https://fixture.test/", complete=True,
        body=[dict(id=1, tags={"secret": "private-sentinel"}), dict(id=2, tags=[])])
    spec = network_spec(dict(id=dict(path=["id"]), tags=dict(path=["tags"])))
    tools = build_tools(network_capture=capture, check_submission=True,
                        listing_url="https://fixture.test/", requirements=TaskRequirements(fields=["id", "tags"]))
    action = tools.registry.registry.actions["test_network"].function
    data = json.loads(asyncio.run(action(source=spec.network, dedupe_by=["id"])).extracted_content)
    assert data["status"] == "failed" and data["extracted"] == 1
    assert data["diagnostics"] == [dict(response_id=7, record_index=1, field="tags", path=["tags"],
                                        reason="unsupported_value_type")]
    assert "private-sentinel" not in json.dumps(data)
    capture.buffer.entries[7].body[0]["tags"] = []
    fixed = json.loads(asyncio.run(action(source=spec.network, dedupe_by=["id"])).extracted_content)
    assert fixed["status"] == "passed" and fixed["extracted"] == 2
    tools.use_structured_output_action(ScraperSpec)
    actions = tools.registry.create_action_model()
    # 改错路径后自动提交复核会把具体原因带进下一轮修复摘要。
    broken = spec.model_copy(deep=True)
    broken.network.fields["tags"].path = ["missing"]
    rejected = asyncio.run(tools.act(actions.model_validate({"done": {"data": broken.model_dump(mode="json")}})))
    feedback = json.loads(rejected.extracted_content)
    assert not rejected.is_done and feedback["network_test"]["diagnostics"][0]["field"] == "tags"
    assert feedback["evidence_summary"]["network_test"]["diagnostics"][0]["reason"] == "path_missing_in_all_records"


# 筛选排空结果不能掩盖已经执行过、但整列从未定位到的错误路径。
def test_filters_cannot_hide_never_resolved_requested_field(page):
    route_json(page, [dict(id=1), dict(id=2)])
    spec = network_spec(dict(id=dict(path=["id"]), tags=dict(path=["wrong"], required=False)),
        filters=[dict(field="tags", operator="contains", value="tag")])
    result = ScraperExecutor().run_page(spec, page)
    assert not result.records and result.filter_unknown == 2
    feedback = validate_data(result, TaskRequirements(fields=["id", "tags"]))
    assert feedback[0].code == "missing_data" and feedback[0].fields == ["tags"]
