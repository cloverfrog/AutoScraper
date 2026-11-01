# 自然结束后的数量决策独立于选择器修复：接受现有结果可直接完成，不必再浏览网站。
import json
from types import SimpleNamespace

import pytest

import test_workflow
from autoscraper.scraping.executor import ScrapeResult, ScraperExecutor
from autoscraper.scraping.spec import ScraperSpec
from autoscraper.workflow.requirements import TaskRequirements
from autoscraper.workflow.runner import run_task
from autoscraper.workflow.validation import Feedback, ValidationSettings, needs_count_clarification

settings = test_workflow.settings
workflow = test_workflow.workflow
FIELDS = {"name": {"selector": "td.name"}, "year": {"selector": "td.year"},
          "win_rate": {"selector": "td.pct"}}
CONDITION = {"field": "team", "operator": "eq", "value": "Boston Bruins"}
ROWS = [{"name": "Boston Bruins", "year": str(1990 + i), "win_rate": "0.55"} for i in range(23)]


# 同时覆盖 URL 原生查询和已扫完全部页的本地筛选；前者不需要重复本地筛选。
def scenario(monkeypatch, workflow, settings, *, mode="local", response="accept", target=23,
             available=21, change_scope=False, update_failure=False, stopped="no_next", regenerate=False):
    module, model = workflow
    baseline = TaskRequirements(fields=list(FIELDS), count=target, filters=[CONDITION])
    async def parse(*args):
        return baseline.model_copy(deep=True)
    monkeypatch.setattr(module, "extract_requirements", parse)
    questions, updates, runs = [], [], []
    async def invoke(messages, output_format):
        model.calls += 1
        payload = json.loads(messages[-1].content)
        updates.append(payload)
        assert output_format is TaskRequirements
        assert payload["current_requirements"]["count"] == target
        if update_failure:
            return SimpleNamespace(completion={"fields": [], "count": available})
        count = (available or None) if response == "accept" else (20 if response == "twenty" else target)
        data = {**baseline.model_dump(), "count": count}
        if change_scope:
            data["filters"] = []
        return SimpleNamespace(completion=TaskRequirements.model_validate(data))
    model.ainvoke = invoke
    spec = ScraperSpec(start_url="https://fixture.test/?q=Boston+Bruins" if mode == "url"
        else "https://fixture.test/", item_selector="tr.team", fields=FIELDS, max_items=target,
        filters=[] if mode == "url" else [{**CONDITION, "field": "name"}])
    candidates = [spec]
    if regenerate:
        candidates.append(spec.model_copy(update={"max_items": 20}))
    generated = test_workflow.install_candidates(monkeypatch, workflow, candidates)
    def scrape(self, selected):
        runs.append(selected)
        if regenerate and len(generated) == 2:
            return ScrapeResult(records=ROWS[:20], pages_visited=1, stop_reason="max_items")
        if mode == "local" and len(runs) == 1:
            return ScrapeResult(records=ROWS[:min(5, available)], items_extracted=125,
                filtered_out=125 - min(5, available),
                pages_visited=1, stop_reason="max_pages")
        return ScrapeResult(records=ROWS[:available], pages_visited=25 if mode == "local" else 1,
            items_extracted=available + (584 if mode == "local" else 0),
            filtered_out=584 if mode == "local" else 0, stop_reason=stopped)
    monkeypatch.setattr(ScraperExecutor, "run", scrape)
    def answer(question):
        questions.append(question)
        assert str(available) in question.question and str(target) in question.question
        if response == "pending":
            return None
        if response == "twenty":
            return "只要前20条即可"
        if response == "new_scope":
            return "移除球队条件，保留23条要求"
        return question.options[0 if response == "accept" else 1]
    outcome = run_task(str(spec.start_url), "按球队筛选", settings, execute=True, ask_user=answer,
        validation=ValidationSettings(max_pages=1, max_repairs=1 if regenerate else 0))
    return outcome, questions, updates, runs, generated, model


# 复现 23 条请求仅有 21 条：URL 和本地路径都提问，用户同意后复用结果，不重启 Agent。
@pytest.mark.parametrize("mode", ["local", "url"])
def test_accept_existing_records_completes_without_regeneration(monkeypatch, workflow, settings, mode):
    outcome, questions, updates, runs, generated, model = scenario(monkeypatch, workflow, settings, mode=mode)
    assert outcome.status == "success" and outcome.requirements.count == outcome.spec.max_items == 21
    assert outcome.original_requirements.count == 23 and outcome.attempts == 1
    assert len(questions) == len(generated) == 1 and not updates
    assert len(runs) == (2 if mode == "local" else 1)
    assert len(outcome.data.records) == 21 and outcome.data.records == ROWS[:21]
    assert model.calls == 1 and outcome.summary()["fulfillment_basis"] == "clarified_task"


# 用户保留原数量要求时明确未完成，不反复提问或触发选择器修复。
def test_keep_requested_count_stops_after_one_question(monkeypatch, workflow, settings):
    outcome, questions, updates, _, generated, _ = scenario(monkeypatch, workflow, settings, response="keep")
    assert outcome.status == "unfulfilled" and outcome.requirements.count == 23
    assert len(questions) == len(generated) == 1 and not updates
    assert outcome.feedback[0].code == "insufficient_items" and not outcome.feedback[0].recoverable


# 非交互或未回答进入待澄清，不再直接宣告任务未完成；已取得的数据仍可诊断。
def test_unanswered_shortfall_remains_pending(monkeypatch, workflow, settings):
    outcome, questions, updates, _, _, _ = scenario(monkeypatch, workflow, settings, response="pending")
    assert outcome.status == "needs_clarification" and outcome.pending_clarification
    assert len(questions) == 1 and not updates
    assert len(outcome.data.records) == 21 and outcome.requirements.count == 23


# 自由回答交回 Agent，避免把新来源/范围要求误当成接受现有数据。
def test_free_text_instructions_require_regeneration(monkeypatch, workflow, settings):
    outcome, _, _, _, _, _ = scenario(monkeypatch, workflow, settings, response="twenty")
    assert outcome.status == "unfulfilled" and outcome.requirements.count == 20
    assert outcome.feedback[0].code == "invalid_spec"
    assert outcome.original_requirements.count == 23


# 数量已经满足时不打扰用户，也不额外调用需求模型。
def test_matching_target_does_not_ask(monkeypatch, workflow, settings):
    outcome, questions, updates, _, _, model = scenario(monkeypatch, workflow, settings,
        target=20, available=20, stopped="max_items")
    assert outcome.status == "success" and not questions and not updates and model.calls == 1


# 数据范围改变时不能把旧结果当作新任务的成功输出。
def test_changed_scope_does_not_reuse_old_result_as_success(monkeypatch, workflow, settings):
    outcome, _, _, _, _, _ = scenario(monkeypatch, workflow, settings, response="new_scope", change_scope=True)
    assert outcome.status == "unfulfilled" and outcome.feedback[0].code == "invalid_spec"
    assert outcome.requirements.filters == [] and outcome.original_requirements.filters


# 修改解析失败不得接受现有结果；原始数量和已取得数据仍保留。
def test_invalid_answer_update_cannot_report_success(monkeypatch, workflow, settings):
    outcome, questions, _, _, _, _ = scenario(monkeypatch, workflow, settings, response="twenty", update_failure=True)
    assert outcome.status == "unfulfilled" and len(questions) == 1
    assert outcome.requirements.count == 23 and outcome.feedback[0].code == "agent_failure"


# 预算截断及真实提取错误不是自然可用数量，不能据此建议用户降低目标。
@pytest.mark.parametrize("reason", ["max_pages", "scroll_limit", "detail_limit"])
def test_budget_cutoff_does_not_offer_partial_total(reason):
    result = ScrapeResult(stop_reason=reason)
    assert not needs_count_clarification(result, [Feedback(code="insufficient_items", message="short")])


def test_extraction_error_does_not_offer_partial_total():
    result = ScrapeResult(stop_reason="no_next", errors=["Required field missing"])
    assert not needs_count_clarification(result, [Feedback(code="insufficient_items", message="short")])


# 明确接受现有结果不需要额外模型额度，避免任务完成后仍因无法解析简单选项而失败。
def test_explicit_count_choice_needs_no_extra_model_budget(monkeypatch, workflow, settings):
    settings.max_llm_calls = 1
    settings.repair_reserve_calls = 0
    outcome, questions, updates, _, _, model = scenario(monkeypatch, workflow, settings)
    assert outcome.status == "success" and model.calls == 1
    assert len(questions) == 1 and not updates and outcome.requirements.count == 21


# 当前无匹配时可以明确接受空结果，数量要求取消而不是写入非法的零目标。
def test_accept_empty_result_clears_count_requirement(monkeypatch, workflow, settings):
    outcome, questions, updates, _, _, _ = scenario(monkeypatch, workflow, settings, available=0)
    assert outcome.status == "success" and outcome.requirements.count is None
    assert outcome.data.records == [] and len(questions) == 1 and not updates


# 自由回答仍通过现有 Agent 路径重新生成规则，后续不会沿用旧的 23 条要求。
def test_free_count_answer_regenerates_with_updated_requirements(monkeypatch, workflow, settings):
    outcome, questions, updates, _, generated, _ = scenario(monkeypatch, workflow, settings,
        response="twenty", regenerate=True)
    assert outcome.status == "success" and outcome.attempts == 2
    assert len(questions) == len(updates) == 1 and len(generated) == 2
    assert generated[1]["requirements"].count == 20 and outcome.spec.max_items == 20
    assert outcome.data.records == ROWS[:20] and outcome.original_requirements.count == 23
