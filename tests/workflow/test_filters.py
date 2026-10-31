# 筛选需求与工作流回归：独立保留条件，允许 URL 覆盖，并区分无匹配与提取失败。
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


# 条件遗漏、修改或额外缩小范围都被拒绝，转换参数不参与与用户条件的直接比较。
@pytest.mark.parametrize("filters,valid", [
    ([], False), ([PRICE], True), ([{**PRICE, "transforms": TRANSFORMS}], True),
    ([{**PRICE, "value": 3}], False), ([{**PRICE, "operator": "lte"}], False),
    ([{**PRICE, "field": "title"}], False),
    ([PRICE, {"field": "title", "operator": "eq", "value": "A"}], False),
])
def test_authoritative_filter_validation(candidate, filters, valid):
    spec = ScraperSpec.model_validate({**candidate.model_dump(), "filters": filters})
    requirements = TaskRequirements(fields=["title", "price"], count=2, filters=[PRICE])
    assert (not validate_spec(spec, requirements)) is valid


# URL 条件只声明覆盖即可沿用原始字段提取；不强制再添加同一份本地筛选。
def test_url_filter_coverage(candidate):
    spec = ScraperSpec.model_validate({**candidate.model_dump(),
        "start_url": "https://fixture.test/?max_price=2", "url_filters": [PRICE]})
    requirements = TaskRequirements(fields=["title", "price"], count=2, filters=[PRICE])
    assert validate_spec(spec, requirements) == [] and not spec.filters
    assert validate_spec(spec, requirements.model_copy(update={"filters": []}))[0].code == "filter_mismatch"


# 全部筛除自然结束是正常空结果；有目标数量时只报告不可修复的数量不足。
@pytest.mark.parametrize("count", [None, 2])
def test_empty_filtered_result_is_not_selector_failure(count):
    result = ScrapeResult(items_extracted=3, filtered_out=2, filter_unknown=1,
                          filter_unknown_reasons={"price: not numeric": 1}, stop_reason="no_next")
    issues = validate_data(result, TaskRequirements(fields=["title"], count=count))
    assert not any(issue.code == "empty_extraction" for issue in issues)
    assert [issue.code for issue in issues] == (["insufficient_items"] if count else [])
    assert all(not issue.recoverable for issue in issues)


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


# 真实离线提取与有限修复：第一轮遗漏条件，第二轮保留临时转换且只保存匹配记录。
def test_workflow_repairs_omitted_filter(candidate, settings, workflow, monkeypatch, executor_page):
    module, _ = workflow
    requirements = TaskRequirements(fields=["title", "price"], count=1, filters=[PRICE])
    async def parse(*args):
        return requirements
    monkeypatch.setattr(module, "extract_requirements", parse)
    plain = candidate.model_copy(update={"max_items": 1})
    filtered = ScraperSpec.model_validate({**plain.model_dump(), "filters": [{**PRICE, "transforms": TRANSFORMS}]})
    received = test_workflow.install_candidates(monkeypatch, workflow, [plain, filtered])
    result = run_task(test_workflow.URL, "价格低于2元，前1条", settings, execute=True)
    assert result.status == "success" and result.attempts == 2
    assert received[1]["feedback"][0].code == "filter_mismatch"
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


# URL 与本地可以各覆盖一部分条件；不要求 URL 覆盖的字段出现在输出中。
def test_mixed_url_and_local_filter_coverage(candidate):
    city = {"field": "city", "operator": "eq", "value": "北京"}
    spec = ScraperSpec.model_validate({**candidate.model_dump(),
        "start_url": "https://fixture.test/?city=beijing", "url_filters": [city],
        "filters": [{**PRICE, "transforms": TRANSFORMS}]})
    assert validate_spec(spec, TaskRequirements(fields=["title", "price"], count=2, filters=[city, PRICE])) == []
    assert "city" not in spec.fields


# 没有匹配记录时不启动选择器修复；未知原因仍通过摘要返回给调用方。
def test_workflow_empty_filtered_result_does_not_repair(candidate, settings, workflow, monkeypatch):
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
    assert result.status == "unfulfilled" and result.attempts == 1
    assert [issue.code for issue in result.feedback] == ["insufficient_items"]
    assert not result.feedback[0].recoverable
    assert result.summary()["filter_unknown_reasons"] == {"price: missing mapping": 1}
