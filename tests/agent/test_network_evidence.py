# 网络验证边界回归：使用真实工具注册和受控捕获记录，不调用付费模型。
import asyncio
import json
from types import SimpleNamespace

import pytest

from autoscraper.agent.network import network_feedback, validate_network, rule_key
from autoscraper.agent.submission import page_key
from autoscraper.agent.network_actions import begin_action
from autoscraper.agent.tools import build_tools
from autoscraper.browser.network import AgentResponseCapture, CapturedResponse, ResponseBuffer
from autoscraper.scraping.spec import NetworkSource, ScraperSpec


URL = "https://fixture.test/"
SOURCE = dict(url_pattern=URL + "api", records_path=["hits"], fields={"id": {"path": ["objectID"]}})


# 每个用例独立维护请求序号，和真实捕获器使用相同的窗口边界。
def setup(source=None):
    capture = AgentResponseCapture()
    tools = build_tools(network_capture=capture, check_submission=True, listing_url=URL,
                        content_timeout_ms=30)
    source = NetworkSource.model_validate(source or SOURCE)
    return capture, tools, source


def response(capture, *, page=URL, status=200, error=None, body=None, complete=True):
    capture.buffer.sequence += 1
    number = capture.buffer.sequence
    entry = CapturedResponse(number, URL + "api", "GET", page_url=page, status=status,
        error=error, complete=complete, body={"hits": [{"objectID": number}]} if body is None else body)
    capture.buffer.entries[number] = entry
    return entry


def call(tools, name="test_network", **kwargs):
    if name == "test_network" and "start_url" in kwargs:
        return asyncio.run(validate_network(tools, tools.network_capture, 30, **kwargs))
    return json.loads(asyncio.run(tools.registry.registry.actions[name].function(**kwargs)).extracted_content)


def test_search_page_requires_final_start_url_and_keeps_query_private():
    capture, tools, source = setup()
    search = URL + "?q=private-search&token=secret"
    response(capture, page=search)
    wrong = call(tools, source=source)
    assert wrong["errors"] == ["initial_page_mismatch"]
    assert not wrong["initial_page_verified"]
    assert "private-search" not in json.dumps(wrong) and "secret" not in json.dumps(wrong)
    correct = call(tools, source=source, start_url=search)
    assert correct["status"] == "passed"
    # 换成同端点的另一个搜索条件，不得借用此前的成功。
    changed = call(tools, source=source, start_url=URL + "?q=other")
    assert changed["status"] == "failed" and not changed["initial_page_verified"]
    assert rule_key(source, [], URL) not in capture.evidence


def test_observed_redirect_alias_works_without_accepting_unrelated_pages():
    capture, tools, source = setup()
    redirected = URL + "search?q=python"
    tools.submission.aliases[page_key(URL)] = redirected
    response(capture, page=redirected)
    assert call(tools, source=source)["status"] == "passed"
    assert call(tools, source=source, start_url=URL + "unrelated")["status"] == "failed"


def test_http_history_requires_fresh_initial_evidence_and_new_failure_revokes_success():
    capture, tools, source = setup()
    response(capture, status=400, error="http_status")
    failed = call(tools, source=source)
    assert failed["diagnostics"][0]["status"] == 400
    response(capture)
    assert "http_status" in call(tools, source=source)["errors"]
    # 明确选定新尝试的请求边界，而不是任意忽略失败；新窗口必须独立通过。
    # 普通重新导航自动开启新验证窗口，无新增模型工具参数。
    navigation = begin_action(capture, "navigate", URL, 1)
    navigation["success"] = True
    passed = call(tools, source=source)
    assert passed["status"] == "passed"
    assert call(tools, source=source)["status"] == "passed"
    response(capture, status=400, error="http_status")
    current = call(tools, source=source)
    assert current["status"] == "failed" and "http_status" in current["errors"]
    assert rule_key(source, [], URL) not in capture.evidence
    navigation = begin_action(capture, "navigate", URL, 3)
    navigation["success"] = True
    empty = call(tools, source=source)
    assert "no_matching_responses" in empty["errors"] and not empty["initial_page_verified"]


def test_zero_matches_and_top_level_json_have_explicit_capability_feedback():
    capture, tools, source = setup()
    empty = call(tools, source=source)
    assert "no_matching_responses" in empty["errors"]
    request = SimpleNamespace(resource_type="document", frame=SimpleNamespace(parent_frame=None),
                              url=URL + "api?secret=private", method="GET")
    assert ResponseBuffer().begin(request) is None
    capture.responded(SimpleNamespace(request=request, status=200,
                                     headers={"content-type": "application/json"}))
    result = call(tools, source=source)
    assert "top_level_json_not_captured" in result["errors"]
    assert result["responses"] == result["extracted"] == 0
    assert "private" not in json.dumps(result)
    assert all(item.code == "response_failure" for item in network_feedback(result))
    assert not capture.buffer.entries


@pytest.mark.parametrize("body,error,code", [
    ({"wrong": []}, "records_path_failure", "json_path_failure"),
    ({"hits": [{"objectID": {"nested": 1}}]}, "json_path_or_field_failure", "json_path_failure"),
    ({"hits": [{"wrong": 1}]}, "field_never_resolved", "json_path_failure"),
    ({"hits": []}, "empty_extraction", "empty_extraction"),
])
def test_failure_categories_are_actionable(body, error, code):
    capture, tools, source = setup()
    response(capture, body=body)
    result = call(tools, source=source)
    assert result["status"] == "failed" and error in result["errors"]
    assert code in {item.code for item in network_feedback(result)}
    again = call(tools, source=source)
    assert again["unchanged_evidence"] and again["errors"] == result["errors"]


def test_raw_dedupe_key_explains_declared_field_constraint():
    capture, tools, source = setup()
    response(capture)
    result = call(tools, source=source, dedupe_by=["objectID"])
    assert result["errors"] == ["invalid_dedupe_by"]
    assert "declared output fields" in result["message"]
    assert result["declared_fields"] == ["id"]
    assert call(tools, source=source, dedupe_by=["id"])["status"] == "passed"



@pytest.mark.parametrize("kind", ["click", "link", "scroll"])
def test_pagination_requires_exact_control_page_and_action_window(kind):
    pagination = dict(type=kind, **({"next_selector": "#next"} if kind != "scroll" else {"container_selector": "#list"}))
    capture, tools, source = setup({**SOURCE, "pagination": pagination})
    response(capture)
    response(capture)
    # 任意点击、导航或滚动，即使出现新数据也不能验证最终规则。
    capture.actions = [(kind, 1)]
    assert "pagination_evidence_missing" in call(tools, source=source)["errors"]
    capture.actions = []
    exact = begin_action(capture, "navigate" if kind == "link" else kind, URL, 1)
    exact.update(end=2, success=True, selectors=["#next" if kind != "scroll" else "#list"])
    # 本例 link 仍在同一页面取数，不应被当成重新导航初始页的窗口。
    if kind == "link":
        exact["destination"] = URL + "?page=2"
        capture.buffer.entries[2].page_url = exact["destination"]
    assert call(tools, source=source)["status"] == "passed"
    exact["page"] = URL + "other"
    assert call(tools, source=source)["status"] == "failed"
    exact["page"] = URL
    exact["end"] = 1
    assert call(tools, source=source)["status"] == "failed"
    exact["end"] = 2
    exact["selectors"] = ["#other"]
    assert call(tools, source=source)["status"] == "failed"


def test_done_reports_http_error_and_uses_candidate_search_page():
    capture, tools, source = setup()
    tools.use_structured_output_action(ScraperSpec)
    actions = tools.registry.create_action_model()
    search = URL + "?q=python"
    response(capture, page=search, status=400, error="http_status")
    candidate = dict(version="3", start_url=search, network=source.model_dump())
    result = asyncio.run(tools.act(actions.model_validate({"done": {"data": candidate}})))
    rejected = json.loads(result.extracted_content)
    assert rejected["status"] == "candidate_rejected"
    assert "http_status" in rejected["network_test"]["errors"]
    assert all(item["code"] != "json_path_failure" for item in rejected["feedback"])
    assert "q" in rejected["network_test"]["expected_page"]["query_keys"]


def test_evicted_window_requires_recapture_and_cannot_restore_old_success():
    capture, tools, source = setup()
    response(capture)
    assert call(tools, source=source)["status"] == "passed"
    capture.buffer.entries.clear()
    capture.buffer.dropped = 1
    response(capture)
    assert "response_limit" in call(tools, source=source)["errors"]
    navigation = begin_action(capture, "navigate", URL, 1)
    navigation["success"] = True
    assert call(tools, source=source)["status"] == "passed"


# 首轮模型输入契约：不新增工具、参数或深层探查入口。
def test_original_network_tool_contract_is_preserved():
    _, tools, _ = setup()
    actions = tools.registry.registry.actions
    assert "advance_network" not in actions
    assert set(actions["inspect_network_response"].param_model.model_fields) == {"response_id"}
    assert set(actions["test_network"].param_model.model_fields) == {"source", "dedupe_by"}
