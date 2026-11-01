# 筛选需求与工作流回归：独立保留条件，允许 URL 直接筛选，并区分无匹配与提取失败。
import asyncio
from types import SimpleNamespace

import pytest

import test_workflow
from autoscraper.scraping.executor import ScrapeResult, ScraperExecutor
from autoscraper.scraping.spec import ScraperSpec
from autoscraper.workflow.requirements import TaskRequirements, extract_requirements
from autoscraper.workflow.validation import (
    ValidationSettings, build_preview_spec, validate_data, validate_spec, validate_preview_data,
)
from autoscraper.workflow.runner import run_task

settings = test_workflow.settings
workflow = test_workflow.workflow
candidate = test_workflow.candidate
executor_page = test_workflow.executor_page
PRICE = {"field": "price", "operator": "lt", "value": 2}
TRANSFORMS = [{"op": "regex_extract", "pattern": r"\d+"}, {"op": "to_number"}]


# 即使字段和数量已明确，仍独立解析筛选，不向模型提供候选规则，也不允许改写显式值。
def test_explicit_fields_count_do_not_bypass_filter_extraction():
    seen = []
    class LLM:
        async def ainvoke(self, messages, output_format):
            seen.append(messages[-1].content)
            assert output_format is TaskRequirements
            return SimpleNamespace(completion=TaskRequirements(
                fields=["other"], count=999, filters=[PRICE]))
    result = asyncio.run(extract_requirements(
        "价格低于2元，抓取前1条，字段名为title、price", LLM()))
    assert len(seen) == 1 and "selector" not in seen[0]
    assert result.fields == ["title", "price"] and result.count == 1
    assert result.filters[0].model_dump() == PRICE


# URL 可直接实现筛选，需求中的逻辑字段名无需对应 URL 参数名或输出字段。
def test_url_filter_without_coverage_declaration(candidate):
    spec = ScraperSpec.model_validate({**candidate.model_dump(),
        "start_url": "https://fixture.test/?q=Boston+Bruins"})
    requirements = TaskRequirements(fields=["title", "price"], count=2,
        filters=[{"field": "team", "operator": "eq", "value": "Boston Bruins"}])
    assert validate_spec(spec, requirements) == [] and not spec.filters


# 本地筛选可以使用真实提取字段名，不因需求解析选用了不同命名而被误拒绝。
def test_local_filter_uses_listing_field_names(candidate):
    spec = ScraperSpec.model_validate({**candidate.model_dump(), "filters": [
        {"field": "title", "operator": "eq", "value": "A"}]})
    requirements = TaskRequirements(fields=["title", "price"], count=2,
        filters=[{"field": "name", "operator": "eq", "value": "A"}])
    assert validate_spec(spec, requirements) == []


# 全部筛除自然结束是正常空结果，数量上限不会变成最少记录要求。
@pytest.mark.parametrize("count", [None, 2])
def test_empty_filtered_result_is_not_selector_failure(count):
    result = ScrapeResult(items_extracted=3, filtered_out=2, filter_unknown=1,
                          filter_unknown_reasons={"price: not numeric": 1}, stop_reason="no_next")
    issues = validate_data(result, TaskRequirements(fields=["title"], count=count))
    assert not any(issue.code == "empty_extraction" for issue in issues)
    assert issues == []


# 真正的提取错误不能因为同批存在筛除记录而被忽略。
@pytest.mark.parametrize("count", [None, 2])
def test_extraction_errors_remain_recoverable(count):
    result = ScrapeResult(items_extracted=1, filtered_out=1, errors=["Required title is empty"])
    issues = validate_data(result, TaskRequirements(fields=["title"], count=count))
    assert issues[0].code == "selector_failure" and all(issue.recoverable for issue in issues)
    assert validate_data(ScrapeResult(), TaskRequirements(fields=["title"]))[0].code == "empty_extraction"


# 验证页数截断且样本全被筛除时，允许后续完整执行，而非选择器修复。
def test_empty_preview_continues_full_execution(candidate):
    spec = ScraperSpec.model_validate({**candidate.model_dump(), "filters": [PRICE], "max_pages": 10})
    preview = build_preview_spec(spec, ValidationSettings(max_pages=1))
    result = ScrapeResult(items_extracted=10, filtered_out=10, pages_visited=1, stop_reason="max_pages")
    issues, complete = validate_preview_data(result,
        TaskRequirements(fields=["title", "price"], count=2, filters=[PRICE]), spec, preview, execute=True)
    assert issues == [] and not complete
    assert preview.filters == spec.filters


# URL 筛选候选无需冗余声明即可通过完整工作流，独立需求仍传给 Agent。
def test_workflow_accepts_url_filter_without_declaration(
    candidate, settings, workflow, monkeypatch, executor_page,
):
    module, _ = workflow
    requirement = {"field": "name", "operator": "eq", "value": "A"}
    requirements = TaskRequirements(fields=["title", "price"], count=1, filters=[requirement])
    async def parse(*args):
        return requirements
    monkeypatch.setattr(module, "extract_requirements", parse)
    spec = ScraperSpec.model_validate({**candidate.model_dump(), "max_items": 1,
        "start_url": "https://fixture.test/?q=A"})
    received = test_workflow.install_candidates(monkeypatch, workflow, [spec])
    result = run_task(test_workflow.URL, "名称为A的前1条", settings, execute=True)
    assert result.status == "success" and result.attempts == 1
    assert received[0]["requirements"].filters[0].model_dump() == requirement
    assert result.data.records == [{"title": "A", "price": "£1"}]


# 样本全被筛除后工作流确实发起完整执行；条件和原始值始终保留。
def test_workflow_executes_after_filtered_preview(candidate, settings, workflow, monkeypatch):
    module, _ = workflow
    async def parse(*args):
        return TaskRequirements(fields=["title", "price"], count=2, filters=[PRICE])
    monkeypatch.setattr(module, "extract_requirements", parse)
    filtered = ScraperSpec.model_validate({**candidate.model_dump(), "filters": [{**PRICE, "transforms": TRANSFORMS}]})
    test_workflow.install_candidates(monkeypatch, workflow, [filtered])
    runs = []
    def execute(self, spec):
        runs.append(spec)
        if len(runs) == 1:
            return ScrapeResult(items_extracted=5, filtered_out=5, stop_reason="max_pages", pages_visited=1)
        return ScrapeResult(records=[{"title": "A", "price": "£1"}, {"title": "B", "price": "£1"}], stop_reason="max_items")
    monkeypatch.setattr(ScraperExecutor, "run", execute)
    result = run_task(test_workflow.URL, "价格低于2元，前2条", settings, execute=True,
                      validation=ValidationSettings(max_pages=1))
    assert result.status == "success" and result.attempts == 1 and len(runs) == 2
    assert runs[0].filters == runs[1].filters


# URL 与本地可以各实现一部分筛选，URL 筛选不要求额外提取字段。
def test_mixed_url_and_local_filtering(candidate):
    city = {"field": "city", "operator": "eq", "value": "北京"}
    spec = ScraperSpec.model_validate({**candidate.model_dump(),
        "start_url": "https://fixture.test/?city=beijing",
        "filters": [{**PRICE, "transforms": TRANSFORMS}]})
    assert validate_spec(spec, TaskRequirements(fields=["title", "price"], count=2, filters=[city, PRICE])) == []
    assert "city" not in spec.fields


# 无匹配但列表自然结束时正常完成，保留原数量上限及未知原因。
def test_workflow_empty_filtered_result_keeps_cap(candidate, settings, workflow, monkeypatch):
    module, _ = workflow
    async def parse(*args):
        return TaskRequirements(fields=["title", "price"], count=2, filters=[PRICE])
    monkeypatch.setattr(module, "extract_requirements", parse)
    filtered = ScraperSpec.model_validate({**candidate.model_dump(), "filters": [{**PRICE, "transforms": TRANSFORMS}]})
    test_workflow.install_candidates(monkeypatch, workflow, [filtered])
    monkeypatch.setattr(ScraperExecutor, "run", lambda *args: ScrapeResult(
        items_extracted=2, filtered_out=1, filter_unknown=1, filter_unknown_reasons={"price: missing mapping": 1},
        stop_reason="no_next"))
    result = run_task(test_workflow.URL, "价格低于2元，前2条", settings, execute=True)
    assert result.status == "success" and result.attempts == 1
    assert not result.pending_clarification and result.requirements.count == result.spec.max_items == 2
    assert result.summary()["filter_unknown_reasons"] == {"price: missing mapping": 1}
