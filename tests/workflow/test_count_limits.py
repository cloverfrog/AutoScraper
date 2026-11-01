# 数量是可复用上限：同一份规则每日可取不同数量，只有超过上限才截断。
import pytest
from playwright.sync_api import sync_playwright

import test_workflow
from autoscraper.agent.explorer import save_spec
from autoscraper.scraping.executor import ScrapeResult, ScraperExecutor
from autoscraper.scraping.export import export_records
from autoscraper.scraping.spec import ScraperSpec
from autoscraper.workflow.requirements import TaskRequirements
from autoscraper.workflow.runner import run_task
from autoscraper.workflow.validation import (
    ValidationSettings, build_preview_spec, validate_spec, validate_execution_data,
    validate_preview_data,
)

settings = test_workflow.settings
workflow = test_workflow.workflow
URL = "https://fixture.test/daily"


# 保存后的协议再次用于不同日期，验证实际 Executor、完整工作流与落盘规则都保持 100。
def test_same_saved_spec_across_days(monkeypatch, tmp_path, workflow, settings):
    module, model = workflow
    spec = ScraperSpec(start_url=URL, item_selector="article", max_items=100,
        fields={"id": {"selector": "b"}, "title": {"selector": "h2"}}, dedupe_by=["id"])
    async def generate(*args, **kwargs):
        model.calls += 1
        assert kwargs["requirements"].count == 100
        assert kwargs["initial_spec"].max_items == 100
        return spec
    monkeypatch.setattr(module, "generate_spec", generate)
    def no_question(request):
        pytest.fail("Natural exhaustion below the cap must not ask to lower it")
    daily_count = 20
    path = tmp_path / "daily-rule.json"
    def scrape(self, selected):
        # 与生产执行器一样在执行阶段创建浏览器，不把 Playwright 的事件循环带入需求解析。
        with sync_playwright() as p:
            browser = p.chromium.launch()
            try:
                page = browser.new_page()
                page.route("**/*", lambda route: route.fulfill(body="".join(
                    f"<article><b>{i}</b><h2>Record {i}</h2></article>" for i in range(daily_count))))
                return self.run_page(selected, page)
            finally:
                browser.close()
    monkeypatch.setattr(ScraperExecutor, "run", scrape)
    for daily_count, expected in [(20, 20), (50, 50), (102, 100)]:
        outcome = run_task(URL, "抓取前100条，字段名为id、title", settings,
            initial_spec=spec, execute=True, ask_user=no_question)
        assert outcome.status == "success" and outcome.summary()["task_fulfilled"]
        assert len(outcome.data.records) == expected
        assert [row["id"] for row in outcome.data.records] == [str(i) for i in range(expected)]
        assert outcome.spec.max_items == outcome.requirements.count == 100
        assert outcome.original_requirements.count == 100 and not outcome.clarifications
        assert outcome.attempts == 1 and not outcome.feedback
        assert outcome.data.stop_reason == ("max_items" if daily_count > 100 else "no_pagination")
        save_spec(outcome.spec, path)
        export_records(outcome.data.records, tmp_path / "daily-data.json", list(spec.fields))
        spec = ScraperSpec.model_validate_json(path.read_text())
        assert spec.max_items == 100  # 次日复用保存后的原上限，不能变成当天的 20/50。



# 所有自然结束方式均可少于上限成功，目标 100 不要求凑足 100。
@pytest.mark.parametrize("reason", ["no_next", "no_pagination", "end_marker", "no_more_content"])
def test_natural_end_below_limit_is_complete(reason):
    result = ScrapeResult(records=[{"name": str(i)} for i in range(20)], stop_reason=reason)
    requirements = TaskRequirements(fields=["name"], count=100)
    issues, complete = validate_execution_data(result, requirements)
    assert issues == [] and complete and requirements.count == 100


# 预算耗尽仍是未完成，不能因为允许少取记录就把预算截断当作自然结束。
@pytest.mark.parametrize("reason", ["max_pages", "detail_limit", "scroll_limit"])
def test_budget_exhaustion_below_limit_is_incomplete(reason):
    result = ScrapeResult(records=[{"name": "A"}], stop_reason=reason)
    issues, complete = validate_execution_data(result, TaskRequirements(fields=["name"], count=100))
    assert not complete and any(issue.code == "validation_limit" for issue in issues)


# Agent 不能根据当日只有 20 条就把用户的 100 条上限改小。
def test_candidate_must_preserve_reusable_limit():
    spec = ScraperSpec(start_url=URL, item_selector="article", fields={"name": {"selector": "b"}}, max_items=20)
    issues = validate_spec(spec, TaskRequirements(fields=["name"], count=100))
    assert issues[0].code == "count_mismatch" and "upper limit 100" in issues[0].message


# 返回超过上限的数据仍不能通过验证。
def test_result_cannot_exceed_limit():
    result = ScrapeResult(records=[{"name": str(i)} for i in range(101)], stop_reason="max_items")
    issues, _ = validate_execution_data(result, TaskRequirements(fields=["name"], count=100))
    assert issues[0].code == "count_mismatch" and not issues[0].recoverable


# 试抓取上限只影响预览，样本通过后仍需完整执行，不能降低原规则上限。
def test_preview_limit_remains_temporary():
    spec = ScraperSpec(start_url=URL, item_selector="article", fields={"name": {"selector": "b"}}, max_items=100)
    preview = build_preview_spec(spec, ValidationSettings(max_items=10))
    result = ScrapeResult(records=[{"name": str(i)} for i in range(10)], stop_reason="max_items")
    issues, complete = validate_preview_data(result, TaskRequirements(fields=["name"], count=100),
        spec, preview, execute=True)
    assert not issues and not complete and preview.max_items == 10 and spec.max_items == 100


# 原始列表成功提取后全部被筛除可以正常返回空集合，原上限不需要取消。
def test_filtered_empty_result_preserves_limit():
    result = ScrapeResult(items_extracted=40, filtered_out=40, stop_reason="no_next")
    requirements = TaskRequirements(fields=["name"], count=100)
    issues, complete = validate_execution_data(result, requirements)
    assert issues == [] and complete and requirements.count == 100


# 对应实际日志：查询只返回 21 条时，23 是上限，直接成功并保留 23 供下次复用。
def test_native_query_below_cap_preserves_protocol(monkeypatch, workflow, settings):
    module, model = workflow
    requirements = TaskRequirements(fields=["name", "year"], count=23,
        filters=[{"field": "team", "operator": "eq", "value": "Boston Bruins"}])
    async def parse(*args):
        return requirements
    monkeypatch.setattr(module, "extract_requirements", parse)
    spec = ScraperSpec(start_url="https://fixture.test/?q=Boston+Bruins", item_selector="tr.team",
        fields={"name": {"selector": "td.name"}, "year": {"selector": "td.year"}}, max_items=23)
    test_workflow.install_candidates(monkeypatch, workflow, [spec])
    monkeypatch.setattr(ScraperExecutor, "run", lambda *args: ScrapeResult(
        records=[{"name": "Boston Bruins", "year": str(1990 + i)} for i in range(21)],
        items_extracted=21, pages_visited=1, stop_reason="no_pagination"))
    def no_question(request):
        pytest.fail("21 matching records naturally satisfy an upper limit of 23")
    outcome = run_task(str(spec.start_url), "最多23条Boston Bruins记录", settings,
        execute=True, ask_user=no_question)
    assert outcome.status == "success" and outcome.attempts == model.calls == 1
    assert len(outcome.data.records) == 21 and outcome.spec.max_items == outcome.requirements.count == 23
    assert not outcome.feedback and not outcome.clarifications
    assert outcome.summary()["task_fulfilled"] and not outcome.spec.filters
