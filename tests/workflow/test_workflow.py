# 任务编排回归测试：覆盖独立需求、有限修复、试抓取与完整执行、失败终态和产物保护。
# 模型以脚本候选代替；提取测试使用本地 Chromium/路由，验证真实选择器行为而不付费。
import asyncio
import json
from pathlib import Path
from unittest.mock import AsyncMock
from types import SimpleNamespace

import pytest

from autoscraper.agent.explorer import AgentSettings, UnsupportedTaskError
from autoscraper.scraping.executor import ScrapeResult, ScraperExecutor
from autoscraper.scraping.spec import ScraperSpec
from autoscraper.workflow.validation import (
    UnsupportedResult,
    ValidationSettings,
    validate_data,
    validate_spec,
)
from autoscraper.workflow.requirements import (
    TaskRequirements,
    explicit_requirements,
    extract_requirements,
)
from autoscraper.workflow.runner import run_task


URL = "https://fixture.test/"
TASK = "抓取前2本书，字段名为title、price，分别为书名、价格"


# 提供满足两个字段、两条记录需求的最小候选规则。
@pytest.fixture
def candidate():
    return ScraperSpec.model_validate(
        {
            "start_url": URL,
            "item_selector": "article",
            "max_items": 2,
            "timeout_ms": 500,
            "content_timeout_ms": 500,
            "fields": {"title": {"selector": "h2"}, "price": {"selector": ".price"}},
            "on_item_error": "fail",
        }
    )


# 提供较小主调用预算和超时，用于稳定验证预算边界。
@pytest.fixture
def settings():
    return AgentSettings(
        api_key="fake",
        model="test",
        base_url="https://llm.test",
        max_steps=6,
        timeout_seconds=30,
        content_timeout_ms=500,
    )


# 替换主模型工厂，记录整次工作流共享的模型调用数。
@pytest.fixture
def workflow(monkeypatch, explicit_unfiltered_requirements):
    pytest.importorskip("browser_use")
    import autoscraper.workflow.runner as module
    import autoscraper.agent.llm as models

    model = SimpleNamespace(calls=0)
    monkeypatch.setattr(models, "make_llm", lambda settings: model)
    return module, model


# 保留真实提取逻辑，只替换浏览器入口并将页面路由到固定 HTML。
@pytest.fixture
def executor_page(monkeypatch):
    from playwright.sync_api import sync_playwright

    original = ScraperExecutor.run_page

    def run(self, spec):
        with sync_playwright() as p:
            browser = p.chromium.launch()
            try:
                page = browser.new_page()
                page.route(
                    "**/*",
                    lambda route: route.fulfill(
                        content_type="text/html; charset=utf-8",
                        body='<article><h2>A</h2><b class="price">£1</b></article>'
                        '<article><h2>B</h2><b class="price">£2</b></article>',
                    ),
                )
                return original(self, spec, page)
            finally:
                browser.close()

    monkeypatch.setattr(ScraperExecutor, "run", run)


# 按轮次返回指定候选或异常，并记录传给探索器的反馈和历史规则。
def install_candidates(monkeypatch, workflow, values):
    module, model = workflow
    received = []

    async def generate(url, task, settings, **kwargs):
        received.append(kwargs)
        model.calls += 1
        assert len(received) <= len(values), kwargs["feedback"]
        value = values[len(received) - 1]
        if isinstance(value, Exception):
            raise value
        return value

    monkeypatch.setattr(module, "generate_spec", generate)
    return received


# 资源规则可以进入 Agent 工作流，但试抓取、独立验证、修复和完整执行始终不下载。
@pytest.mark.parametrize('repair', [False, True])
def test_agent_workflow_resource_rules_remain_url_only(monkeypatch, workflow, candidate, settings, executor_page, repair):
    from autoscraper.scraping.resources import ResourceSaver
    candidate = ScraperSpec.model_validate({**candidate.model_dump(),
        'resources': [dict(field='title', mode='request')]})

    def forbidden(*args, **kwargs):
        raise AssertionError('Agent workflow must never initialize resource saving')

    monkeypatch.setattr(ResourceSaver, '__init__', forbidden)
    candidates = [candidate]
    if repair:
        bad = candidate.model_dump()
        bad['fields']['price']['selector'] = '.missing-price'
        candidates.insert(0, ScraperSpec.model_validate(bad))
    received = install_candidates(monkeypatch, workflow, candidates)
    outcome = run_task(URL, TASK, settings, fields=['title', 'price'], count=2, execute=True)
    assert outcome.status == 'success'
    assert len(received) == len(candidates)
    assert outcome.data.records and outcome.data.resources == []


def test_file_saving_requirement_cannot_be_replaced_with_url_only(candidate):
    requirements = TaskRequirements(fields=['title', 'price'], resource_fields=['title'])
    assert any(issue.code == 'invalid_spec' for issue in validate_spec(candidate, requirements))
    candidate = ScraperSpec.model_validate({**candidate.model_dump(),
        'resources': [dict(field='title', mode='response')]})
    assert validate_spec(candidate, requirements) == []


# 明确字段名与数量必须完整保留，显式参数优先于需求文本。
def test_explicit_fields_and_count_cannot_be_dropped():
    fields, count = explicit_requirements(
        "抓取前25本书，字段名为title、price、description，分别为完整书名、价格、详情描述",
        None,
        None,
    )
    assert fields == ["title", "price", "description"]
    assert count == 25
    assert explicit_requirements(TASK, ["override"], 3) == (["override"], 3)


# 中英文并列连接词不能导致最后一个字段漏解析。
@pytest.mark.parametrize(
    "task",
    [
        "first 25 books, fields named title, price and description",
        "first 25 books, fields named title, price, and description",
        "前25本书，字段名为title、price和description",
    ],
)
def test_explicit_conjunctions_do_not_drop_last_field(task):
    fields, count = explicit_requirements(task, None, None)
    assert fields == ["title", "price", "description"]
    assert count == 25


# 模型补充缺失需求时，不能覆盖已明确的记录数量。
def test_bounded_natural_language_requirements():
    pytest.importorskip("browser_use")

    class LLM:
        calls = 0

        async def ainvoke(self, messages, output_format):
            self.calls += 1
            assert "selector" not in messages[-1].content
            assert output_format is TaskRequirements
            return SimpleNamespace(
                completion=TaskRequirements(fields=["title", "price"], count=999)
            )

    model = LLM()
    result = asyncio.run(extract_requirements("前25本书的名称和价格", model))
    assert result.fields == ["title", "price"]
    assert result.count == 25
    assert model.calls == 1


# 推断数量的模型输出不能改动用户明确命名的字段。
def test_inferred_count_cannot_change_explicit_fields():
    pytest.importorskip("browser_use")

    class LLM:
        async def ainvoke(self, *args, **kwargs):
            return SimpleNamespace(completion=TaskRequirements(fields=["title"], count=25))

    result = asyncio.run(
        extract_requirements("抓取二十五本书，字段名为title、price、description", LLM())
    )
    assert result.fields == ["title", "price", "description"]
    assert result.count == 25


# 候选规则必须和独立需求比较，不能自己定义成功标准。
def test_spec_validation_is_independent(candidate):
    requirements = TaskRequirements(fields=["title", "price", "description"], count=25)
    issues = validate_spec(candidate, requirements)
    assert {item.code for item in issues} == {"missing_required_field", "count_mismatch"}
    assert issues[0].fields == ["description"]
    candidate.fields["price"].required = False
    assert validate_spec(candidate, requirements)[0].fields == ["description"]


# 检查空结果、必需字段值和超过数量上限等确定性数据反馈。
@pytest.mark.parametrize(
    "rows,code",
    [
        ([], "empty_extraction"),
        ([{"title": "A"}], "missing_data"),

        ([{"title": "A", "price": "£1"}] * 3, "count_mismatch"),
    ],
)
def test_data_checks(rows, code):
    issues = validate_data(
        ScrapeResult(records=rows), TaskRequirements(fields=["title", "price"], count=2)
    )
    assert code in [item.code for item in issues]


# 支持的任务应通过真实提取校验并返回一致的完成状态。
def test_supported_task(candidate, settings, workflow, monkeypatch, executor_page):
    received = install_candidates(monkeypatch, workflow, [candidate])
    result = run_task(URL, TASK, settings, execute=True)
    assert result.status == "success"
    assert result.summary()["task_fulfilled"]
    assert result.attempts == result.llm_calls == 1
    assert len(result.data.records) == 2
    assert received[0]["requirements"].fields == ["title", "price"]


# 将真实 CSS 提取失败反馈给下一轮，修复后再验证成功。
@pytest.mark.parametrize("mode", ["fail", "skip"])
def test_repair_real_selector_failure(
    candidate, settings, workflow, monkeypatch, executor_page, mode
):
    broken = candidate.model_copy(deep=True)
    broken.on_item_error = mode
    broken.fields["price"].selector = ".not-there"
    received = install_candidates(monkeypatch, workflow, [broken, candidate])
    result = run_task(URL, TASK, settings, execute=True)
    assert result.status == "success"
    assert result.attempts == 2
    assert received[1]["feedback"][0].code == "selector_failure"
    assert received[1]["previous_spec"] == broken
    assert len(result.data.records) == 2


# 必需字段缺失应触发有限修复，不能先执行不完整规则。
def test_missing_fields_exhaust_repair_without_execution(
    candidate, settings, workflow, monkeypatch
):
    received = install_candidates(monkeypatch, workflow, [candidate] * 3)
    monkeypatch.setattr(
        ScraperExecutor, "run", lambda *args: pytest.fail("Do not execute incomplete spec")
    )
    result = run_task(URL, "抓取前2本书，字段名为title、price、description", settings)
    assert result.status == "unfulfilled"
    assert result.attempts == 3
    assert not result.summary()["task_fulfilled"]
    assert result.feedback[0].fields == ["description"]
    assert all(
        call["requirements"].fields == ["title", "price", "description"] for call in received
    )


# 协议不支持的任务应立即结束，不浪费修复预算。
def test_known_unsupported_stops_without_repairs(settings, workflow, monkeypatch):
    received = install_candidates(
        monkeypatch,
        workflow,
        [UnsupportedTaskError("description requires login", ["description"])],
    )
    result = run_task(URL, "抓取前25本书，字段名为title、price、description", settings)
    assert result.status == "unsupported"
    assert len(received) == 1
    assert result.feedback[0].code == "unsupported_capability"
    assert not result.feedback[0].recoverable


# 探索器调用 report_unsupported 后不能按成功候选继续处理。
def test_explorer_unsupported_tool_is_a_failure_terminal(settings, monkeypatch):
    bu = pytest.importorskip("browser_use")
    from autoscraper.agent.explorer import generate_spec

    class Browser:
        killed = False

        def __init__(self, **kwargs):
            pass

        async def kill(self):
            Browser.killed = True

    class Agent:
        def __init__(self, **kwargs):
            assert kwargs["output_model_schema"] is ScraperSpec
            assert "report_unsupported" in kwargs["tools"].registry.registry.actions
            assert "description" in kwargs["task"]

        async def run(self, **kwargs):
            return SimpleNamespace(
                is_successful=lambda: False,
                final_result=lambda: json.dumps(
                    {
                        "status": "unsupported",
                        "reason": "description requires login",
                        "unsupported_fields": ["description"],
                    }
                ),
            )

    monkeypatch.setattr(bu, "BrowserSession", Browser)
    monkeypatch.setattr("autoscraper.agent.explorer.AgentResponseCapture.start", AsyncMock())
    monkeypatch.setattr(bu, "Agent", Agent)
    with pytest.raises(UnsupportedTaskError, match="login"):
        asyncio.run(
            generate_spec(
                URL,
                TASK,
                settings,
                llm=object(),
                requirements=TaskRequirements(fields=["description"]),
            )
        )
    assert Browser.killed


# 独立需求已明确要求不支持能力时，无需启动浏览器探索。
def test_explicit_unsupported_requirements_skip_browser(settings, workflow, monkeypatch):
    module, model = workflow

    async def requirements(*args):
        return TaskRequirements(fields=["description"], unsupported_capabilities=["captcha_bypass"])

    monkeypatch.setattr(module, "extract_requirements", requirements)
    monkeypatch.setattr(
        module, "generate_spec", lambda *args, **kwargs: pytest.fail("No browser needed")
    )
    result = run_task(URL, "Go to detail pages", settings)
    assert result.status == "unsupported"
    assert result.attempts == 0


# max_repairs=0 仍允许初次生成，但失败后不再修复。
def test_zero_repair_limit(candidate, settings, workflow, monkeypatch):
    install_candidates(monkeypatch, workflow, [candidate])
    result = run_task(
        URL,
        "前2本，字段名为title、price、description",
        settings,
        validation=ValidationSettings(max_repairs=0),
    )
    assert result.attempts == 1
    assert result.status == "unfulfilled"


# 需求解析和多轮探索必须共享主模型调用预算。
def test_shared_call_budget(candidate, settings, workflow, monkeypatch):
    settings.max_llm_calls = 1
    settings.repair_reserve_calls = 0
    received = install_candidates(monkeypatch, workflow, [candidate])
    result = run_task(URL, "前2本，字段名为title、price、description", settings)
    assert result.status == "unfulfilled"
    assert len(received) == result.llm_calls == 1
    assert result.feedback[0].code == "budget_exhausted"


# 只验证部分样本时可返回 validated，但不能声称已完成全部抓取。
def test_rule_only_sampling_does_not_claim_full_task(candidate, settings, workflow, monkeypatch):
    larger = candidate.model_copy(update={"max_items": 200})
    install_candidates(monkeypatch, workflow, [larger])
    monkeypatch.setattr(
        ScraperExecutor,
        "run",
        lambda self, spec: ScrapeResult(
            records=[{"title": "A", "price": "£1"}] * 100, stop_reason="max_items"
        ),
    )
    result = run_task(URL, "字段名为title、price，前200本", settings)
    assert result.status == "validated"
    assert result.sample_only
    assert not result.summary()["task_fulfilled"]


# 需要导出数据且试抓取未完成时，必须继续执行完整规则。
def test_output_requires_full_execution_after_sample(candidate, settings, workflow, monkeypatch):
    larger = candidate.model_copy(update={"max_items": 200})
    install_candidates(monkeypatch, workflow, [larger])
    seen = []

    def execute(self, spec):
        seen.append(spec.max_items)
        return ScrapeResult(
            records=[{"title": "A", "price": "£1"}] * spec.max_items, stop_reason="max_items"
        )

    monkeypatch.setattr(ScraperExecutor, "run", execute)
    result = run_task(URL, "字段名为title、price，前200本", settings, execute=True)
    assert seen == [100, 200]
    assert result.summary()["task_fulfilled"]


# 探索、试抓取和完整执行应收到同样的显示模式与路径，且不会因进入新阶段回退到默认值。
@pytest.mark.parametrize("headless", [True, False])
@pytest.mark.parametrize("path", [None, "C:/Custom Browser/chrome.exe"])
def test_browser_settings_shared_across_all_stages(
    candidate, settings, workflow, monkeypatch, headless, path
):
    module, _ = workflow
    settings.headless = headless
    settings.executable_path = path
    exploration = []
    execution = []

    async def generate(url, task, run_settings, **kwargs):
        exploration.append((run_settings.headless, run_settings.executable_path))
        return candidate

    def run(self, spec):
        execution.append((spec.max_items, self.headless, self.executable_path))
        return ScrapeResult(
            records=[{"title": "A", "price": "1"}] * spec.max_items, stop_reason="max_items"
        )

    monkeypatch.setattr(module, "generate_spec", generate)
    monkeypatch.setattr(ScraperExecutor, "run", run)
    outcome = run_task(
        URL, TASK, settings, execute=True, validation=ValidationSettings(max_items=1)
    )
    assert outcome.status == "success"
    assert exploration == [(headless, path)]
    assert execution == [(1, headless, path), (2, headless, path)]


# 验证页数预算不足应明确报告，而不是要求 Agent 重写正确规则。
def test_validation_budget_exhaustion_is_not_repaired(candidate, settings, workflow, monkeypatch):
    install_candidates(monkeypatch, workflow, [candidate])
    monkeypatch.setattr(
        ScraperExecutor,
        "run",
        lambda self, spec: ScrapeResult(
            records=[{"title": "A", "price": "£1"}], stop_reason="max_pages"
        ),
    )
    result = run_task(URL, TASK, settings, validation=ValidationSettings(max_pages=1))
    assert result.status == "unfulfilled"
    assert result.attempts == 1
    assert result.feedback[0].code == "validation_limit"


# 区分验证页数截断、完整成功及完整执行失败，保护既有编排决策。
@pytest.mark.parametrize(
    "execute, final_stop, status, code, sample_only",
    [
        (False, None, "unfulfilled", "validation_limit", True),
        (True, "max_items", "success", None, False),
        (True, "no_next", "success", None, False),
        (True, "end_marker", "success", None, False),
        (True, "no_more_content", "success", None, False),
        (True, "scroll_limit", "unfulfilled", "validation_limit", True),
        (True, "max_pages", "unfulfilled", "validation_limit", True),
        (True, "detail_limit", "unfulfilled", "validation_limit", True),
        (True, "pagination_cycle", "unfulfilled", "selector_failure", False),
    ],
)
def test_preview_page_limit_preserves_execution_decisions(
    candidate, settings, workflow, monkeypatch, execute, final_stop, status, code, sample_only
):
    """区分验证预算截断与完整执行失败，防止重构改变导出或修复决策。"""
    received = install_candidates(monkeypatch, workflow, [candidate])
    seen = []
    rows = [{"title": "A", "price": "1"}, {"title": "B", "price": "2"}]

    def run(self, spec):
        seen.append(spec.max_pages)
        if len(seen) == 1:
            return ScrapeResult(records=rows[:1], stop_reason="max_pages")
        return ScrapeResult(records=rows, stop_reason=final_stop)

    monkeypatch.setattr(ScraperExecutor, "run", run)
    outcome = run_task(
        URL, TASK, settings, execute=execute,
        validation=ValidationSettings(max_pages=1, max_repairs=0),
    )
    assert seen == ([1, candidate.max_pages] if execute else [1])
    assert len(received) == outcome.attempts == 1
    assert outcome.status == status
    assert outcome.sample_only is sample_only
    assert [item.code for item in outcome.feedback] == ([code] if code else [])
    assert outcome.summary()["task_fulfilled"] is (status == "success")


# 验证预算截断不能掩盖真实字段缺失。
def test_preview_page_limit_does_not_hide_missing_data(
    candidate, settings, workflow, monkeypatch
):
    install_candidates(monkeypatch, workflow, [candidate])
    seen = []

    def run(self, spec):
        seen.append(spec.max_pages)
        return ScrapeResult(records=[{"title": "A", "price": None}],
                            fields_found={"title": 1, "price": 0}, stop_reason="max_pages")

    monkeypatch.setattr(ScraperExecutor, "run", run)
    outcome = run_task(
        URL, TASK, settings, execute=True,
        validation=ValidationSettings(max_pages=1, max_repairs=0),
    )
    assert seen == [1]
    assert outcome.status == "unfulfilled"
    assert [item.code for item in outcome.feedback] == ["missing_data"]


# 需求本身不合法时直接结束，不开始浏览器探索。
def test_bad_requirements_stop_without_exploration(settings, workflow, monkeypatch):
    result = run_task(URL, TASK, settings, count=0)
    assert result.status == "unfulfilled"
    assert result.feedback[0].code == "invalid_requirements"
    assert result.attempts == result.llm_calls == 0


# 不支持结果必须提供明确原因，且状态值不能冒充其他结果。
def test_unsupported_envelope_requires_reason():
    with pytest.raises(ValueError):
        UnsupportedResult()
    with pytest.raises(ValueError):
        UnsupportedResult(status="candidate", reason="unsupported")


# 回归保护：CLI 不能把缺少必需字段的候选当作正式规则保存。
def test_issue_regression_cli_does_not_save_partial_spec(
    candidate, workflow, monkeypatch, tmp_path, capsys
):
    import autoscraper.cli.agent as cli

    install_candidates(monkeypatch, workflow, [candidate])
    monkeypatch.setenv("AUTOSCRAPER_API_KEY", "fake")
    spec_path, data_path = tmp_path / "spec.json", tmp_path / "data.json"
    spec_path.write_text("existing rule")
    data_path.write_text("existing data")
    monkeypatch.setattr(
        "sys.argv",
        [
            "autoscraper-agent",
            URL,
            "抓取前25本书，字段名为title、price、description，分别为完整书名、价格、详情描述",
            "--spec-output",
            str(spec_path),
            "--output",
            str(data_path),
            "--max-repairs",
            "0",
        ],
    )
    assert cli.main() == 1
    report = json.loads(capsys.readouterr().out)
    assert not report["task_fulfilled"]
    assert report["feedback"][0]["fields"] == ["description"]
    assert spec_path.read_text() == "existing rule"
    assert data_path.read_text() == "existing data"


# 各种失败终态都应保留原有已验证规则和数据。
@pytest.mark.parametrize("status,exit_code", [("unsupported", 3), ("unfulfilled", 1)])
def test_failed_cli_preserves_existing_files(monkeypatch, tmp_path, status, exit_code):
    import autoscraper.cli.agent as cli
    from autoscraper.workflow.validation import Feedback
    from autoscraper.workflow.outcome import TaskOutcome

    paths = [tmp_path / "rule.json", tmp_path / "data.json", tmp_path / "data.csv"]
    for path in paths:
        path.write_text("previous successful output", encoding="utf-8")
    monkeypatch.setenv("AUTOSCRAPER_API_KEY", "fake")
    monkeypatch.setattr(
        cli,
        "run_task",
        lambda *args, **kwargs: TaskOutcome(
            status=status,
            feedback=[
                Feedback(
                    code="missing_required_field",
                    fields=["description"],
                    message="missing description",
                )
            ],
        ),
    )
    monkeypatch.setattr(
        "sys.argv",
        [
            "autoscraper-agent",
            URL,
            TASK,
            "--spec-output",
            str(paths[0]),
            "--output",
            str(paths[1]),
            "--output",
            str(paths[2]),
        ],
    )
    assert cli.main() == exit_code
    assert all(path.read_text(encoding="utf-8") == "previous successful output" for path in paths)


# 一层详情能力应被需求解析和探索提示词识别为受支持能力。
def test_detail_requirements_and_agent_prompt():
    from autoscraper.agent.explorer import build_task

    assert "detail_pages" not in str(TaskRequirements.model_json_schema())
    prompt = build_task(URL, "Get descriptions from detail pages")
    assert 'version="2"' in prompt
    assert "observed detail href" in prompt
    assert 'item_selector="html"' in prompt


# 在列表候选上增加详情描述字段和独立详情页预算。
def detail_candidate(candidate):
    data = candidate.model_dump()
    data.update(
        version="2",
        detail={
            "url_selector": "a",
            "max_pages": 2,
            "fields": {"description": {"selector": ".description"}},
        },
    )
    return ScraperSpec.model_validate(data)


# 必需详情字段应纳入与列表字段相同的独立校验。
def test_detail_field_validation(candidate):
    spec = detail_candidate(candidate)
    requirements = TaskRequirements(fields=["title", "description"], count=2)
    assert validate_spec(spec, requirements) == []
    spec.detail.fields["description"].required = False
    assert validate_spec(spec, requirements) == []


# 真实详情选择器失败应可反馈、修复，并完成合并字段抓取。
@pytest.mark.parametrize("mode", ["fail", "skip"])
def test_detail_selector_repair_and_full_workflow(candidate, settings, workflow, monkeypatch, mode):
    from playwright.sync_api import sync_playwright

    spec = detail_candidate(candidate)
    broken = spec.model_copy(deep=True)
    broken.on_item_error = mode
    broken.detail.fields["description"].selector = "p, body"
    received = install_candidates(monkeypatch, workflow, [broken, spec])

    def execute(self, rule):
        with sync_playwright() as p:
            browser = p.chromium.launch()
            try:
                page = browser.new_context().new_page()
                page.context.route(
                    "**/*",
                    lambda route: route.fulfill(
                        content_type="text/html",
                        body=(
                            '<article><h2>A</h2><b class="price">1</b><a href="/a">a</a></article>'
                            '<article><h2>B</h2><b class="price">2</b><a href="/b">b</a></article>'
                            if route.request.url == URL
                            else '<p class="description">Full text</p>'
                        ),
                    ),
                )
                return self.run_page(rule, page)
            finally:
                browser.close()

    monkeypatch.setattr(ScraperExecutor, "run", execute)
    outcome = run_task(URL, "前2本，字段名为title、price、description", settings, execute=True)
    assert outcome.status == "success"
    assert outcome.attempts == 2
    assert outcome.summary()["detail_pages"] == 2
    assert received[1]["previous_spec"] == broken
    assert "/a" in received[1]["feedback"][0].message
    assert "description" in received[1]["feedback"][0].message
    assert all(row["description"] == "Full text" for row in outcome.data.records)


# 详情试抓取预算耗尽是范围限制，不是可恢复的选择器问题。
def test_detail_preview_budget_is_nonrecoverable(candidate, settings, workflow, monkeypatch):
    spec = detail_candidate(candidate)
    install_candidates(monkeypatch, workflow, [spec])
    seen = []

    def execute(self, rule):
        seen.append(rule.detail.max_pages)
        return ScrapeResult(
            records=[{"title": "A", "price": "1", "description": "D"}],
            detail_pages_visited=1,
            stop_reason="detail_limit",
        )

    monkeypatch.setattr(ScraperExecutor, "run", execute)
    outcome = run_task(
        URL,
        "前2本，字段名为title、price、description",
        settings,
        validation=ValidationSettings(max_detail_pages=1),
    )
    assert seen == [1]
    assert spec.detail.max_pages == 2
    assert outcome.status == "unfulfilled"
    assert not outcome.summary()["task_fulfilled"]
    assert outcome.attempts == 1
    assert outcome.feedback[0].code == "validation_limit"
    assert not outcome.feedback[0].recoverable


# 详情样本通过后，需要数据输出时继续完整详情抓取。
def test_detail_full_execution_after_sample(candidate, settings, workflow, monkeypatch):
    spec = detail_candidate(candidate)
    install_candidates(monkeypatch, workflow, [spec])
    seen = []

    def execute(self, rule):
        seen.append(rule.max_items)
        return ScrapeResult(
            records=[{"title": "A", "price": "1", "description": "D"}] * rule.max_items,
            stop_reason="max_items",
        )

    monkeypatch.setattr(ScraperExecutor, "run", execute)
    outcome = run_task(
        URL,
        "前2本，字段名为title、price、description",
        settings,
        execute=True,
        validation=ValidationSettings(max_items=1),
    )
    assert seen == [1, 2]
    assert outcome.summary()["task_fulfilled"]


# 列表、分页或详情导航失败都不能误触发 CSS 规则修复。
@pytest.mark.parametrize("stage", ["listing", "pagination", "detail"])
@pytest.mark.parametrize("mode", ["fail", "skip"])
@pytest.mark.parametrize("failure", ["timeout", "proxy", "http"])
def test_navigation_failure_does_not_repair(
    candidate, settings, workflow, monkeypatch, stage, mode, failure
):
    from playwright.sync_api import Error, Page, TimeoutError, sync_playwright

    spec = detail_candidate(candidate) if stage == "detail" else candidate.model_copy(deep=True)
    spec.on_item_error = mode
    if stage == "pagination":
        spec = ScraperSpec.model_validate(
            {**spec.model_dump(), "max_pages": 2, "pagination": {"type": "link", "next_selector": ".next"}}
        )
    target = URL + {"listing": "", "pagination": "page-2", "detail": "detail"}[stage]
    original = Page.goto
    message = {
        "timeout": "Page.goto: Timeout 500ms exceeded.",
        "proxy": "Page.goto: net::ERR_PROXY_CONNECTION_FAILED",
        "http": "HTTP 503",
    }[failure]
    error_type = TimeoutError if failure == "timeout" else Error

    def goto(self, url, **kwargs):
        if url == target and failure != "http":
            raise error_type(message)
        return original(self, url, **kwargs)

    monkeypatch.setattr(Page, "goto", goto)

    def execute(self, rule):
        with sync_playwright() as p:
            browser = p.chromium.launch()
            try:
                page = browser.new_context().new_page()
                page.context.route(
                    "**/*",
                    lambda route: route.fulfill(
                        status=503 if failure == "http" and route.request.url == target else 200,
                        content_type="text/html",
                        body='<article><h2>A</h2><b class="price">1</b>'
                        '<a href="/detail">Detail</a></article>'
                        '<a class="next" href="/page-2">Next</a>',
                    ),
                )
                return self.run_page(rule, page)
            finally:
                browser.close()

    monkeypatch.setattr(ScraperExecutor, "run", execute)
    received = install_candidates(monkeypatch, workflow, [spec])
    outcome = run_task(URL, TASK, settings, execute=True)
    assert outcome.status == "unfulfilled"
    assert outcome.attempts == outcome.llm_calls == len(received) == 1
    assert outcome.spec == spec
    assert not outcome.summary()["execution_success"]
    assert len(outcome.feedback) == 1
    feedback = outcome.feedback[0]
    assert feedback.code == ("navigation_timeout" if failure == "timeout" else "navigation_failure")
    assert not feedback.recoverable
    assert message in feedback.message
    assert target in feedback.message
    assert outcome.history[0]["feedback"][0] == feedback.model_dump()
    if stage == "detail" and mode == "skip":
        assert outcome.data.error_details[0].url == target
        assert outcome.data.errors[0] == feedback.message


# 完整执行阶段导航失败应保留候选并准确标记失败阶段。
def test_navigation_failure_in_full_execution(candidate, settings, workflow, monkeypatch):
    from autoscraper.scraping.executor import NavigationError

    received = install_candidates(monkeypatch, workflow, [candidate])
    seen = []

    def execute(self, spec):
        seen.append(spec.max_items)
        if len(seen) == 1:
            return ScrapeResult(records=[{"title": "A", "price": "1"}], stop_reason="max_items")
        raise NavigationError(URL + "page-2", "net::ERR_CONNECTION_RESET")

    monkeypatch.setattr(ScraperExecutor, "run", execute)
    outcome = run_task(
        URL, TASK, settings, execute=True, validation=ValidationSettings(max_items=1)
    )
    assert seen == [1, 2]
    assert outcome.status == "unfulfilled"
    assert len(received) == outcome.attempts == 1
    assert outcome.feedback[0].code == "navigation_failure"
    assert not outcome.feedback[0].recoverable


# 导航反馈应保留有助定位原因的完整诊断。
def test_navigation_feedback_preserves_full_diagnostic():
    from autoscraper.scraping.executor import ScrapeFailure

    message = "Navigation failed: " + "x" * 2100 + " net::ERR_NAME_NOT_RESOLVED at " + URL
    result = ScrapeResult(
        errors=[message], error_details=[ScrapeFailure("navigation_failure", message, URL)]
    )
    issues = validate_data(result, TaskRequirements(fields=["title"], count=2))
    assert len(issues) == 1
    assert issues[0].code == "navigation_failure"
    assert issues[0].message == message
    assert not issues[0].recoverable


# 试抓取或完整执行内容超时时停止修复，并保留可诊断状态。
@pytest.mark.parametrize("stage", ["preview", "full"])
@pytest.mark.parametrize("error_name", ["ContentTimeout", "NavigationTimeout", "PaginationActionError"])
def test_wait_timeouts_stop_without_repair(
    candidate, settings, workflow, monkeypatch, stage, error_name
):
    import autoscraper.scraping.executor as executor

    received = install_candidates(monkeypatch, workflow, [candidate])
    calls = []

    def run(self, spec):
        calls.append(spec)
        if stage == "preview" or len(calls) == 2:
            if error_name == "PaginationActionError":
                raise executor.PaginationActionError("action failed")
            raise getattr(executor, error_name)(URL, "deadline reached")
        return ScrapeResult(records=[{"title": "A", "price": "1"}], stop_reason="max_items")

    monkeypatch.setattr(ScraperExecutor, "run", run)
    outcome = run_task(
        URL, TASK, settings, execute=True, validation=ValidationSettings(max_items=1)
    )
    assert len(received) == outcome.attempts == 1
    assert outcome.feedback[0].code == getattr(executor, error_name).code
    assert not outcome.feedback[0].recoverable
    assert outcome.failure_stage == ("validation" if stage == "preview" else "execution")
    assert outcome.summary()["agent_status"] == "completed"


# CLI 超时时另存未验证候选，避免覆盖正式规则与数据。
@pytest.mark.parametrize("stage", ["validation", "execution"])
def test_timeout_cli_keeps_candidate_without_overwriting_outputs(
    candidate, monkeypatch, tmp_path, capsys, stage
):
    import autoscraper.cli.agent as cli
    from autoscraper.workflow.validation import Feedback
    from autoscraper.workflow.outcome import TaskOutcome

    spec_path = tmp_path / "rule.json"
    data_path = tmp_path / "data.json"
    spec_path.write_text("previous verified rule")
    data_path.write_text("previous data")
    monkeypatch.setenv("AUTOSCRAPER_API_KEY", "fake")
    monkeypatch.setattr(
        cli,
        "run_task",
        lambda *args, **kwargs: TaskOutcome(
            status="unfulfilled",
            spec=candidate,
            failure_stage=stage,
            feedback=[
                Feedback(
                    code="navigation_timeout", message="Page.goto timed out", recoverable=False
                )
            ],
        ),
    )
    monkeypatch.setattr(
        "sys.argv",
        [
            "autoscraper-agent",
            URL,
            TASK,
            "--spec-output",
            str(spec_path),
            "--output",
            str(data_path),
        ],
    )
    for index in (1, 2):
        assert cli.main() == 1
        report = json.loads(capsys.readouterr().out)
        expected = tmp_path / ("rule.unvalidated.json" if index == 1 else "rule.unvalidated-2.json")
        assert report["agent_status"] == "completed"
        assert report["failure_stage"] == stage
        assert report["unvalidated_spec"] == str(expected)
        assert ScraperSpec.model_validate_json(expected.read_text()) == candidate
    assert spec_path.read_text() == "previous verified rule"
    assert data_path.read_text() == "previous data"


# 缩短试抓取导航时限时，仍保留用户设置的内容等待上限。
def test_validation_keeps_user_content_timeout(candidate, settings, workflow, monkeypatch):
    settings.content_timeout_ms = 30000
    install_candidates(monkeypatch, workflow, [candidate])
    seen = []

    def run(self, spec):
        seen.append(spec)
        return ScrapeResult(
            records=[{"title": "A", "price": "1"}, {"title": "B", "price": "2"}],
            stop_reason="max_items",
        )

    monkeypatch.setattr(ScraperExecutor, "run", run)
    outcome = run_task(URL, TASK, settings, validation=ValidationSettings(timeout_ms=50))
    assert outcome.status == "validated"
    assert seen[0].timeout_ms == 50
    assert seen[0].content_timeout_ms == outcome.spec.content_timeout_ms == 30000


# 未配置验证导航时限时沿用候选规则的导航超时。
def test_validation_inherits_rule_navigation_timeout(candidate, settings, workflow, monkeypatch):
    candidate.timeout_ms = 30000
    settings.content_timeout_ms = 30000
    install_candidates(monkeypatch, workflow, [candidate])
    seen = []

    def run(self, spec):
        seen.append(spec)
        return ScrapeResult(
            records=[{"title": "A", "price": "1"}, {"title": "B", "price": "2"}],
            stop_reason="max_items",
        )

    monkeypatch.setattr(ScraperExecutor, "run", run)
    outcome = run_task(URL, TASK, settings)
    assert outcome.status == "validated"
    assert seen[0].timeout_ms == 30000
    assert seen[0].content_timeout_ms == 30000


# CLI 未传验证导航超时参数时保留 None，传入时正确转交。
@pytest.mark.parametrize(
    "flags, expected", [([], None), (["--validation-timeout-ms", "5000"], 5000)]
)
def test_agent_cli_navigation_timeout_is_optional(monkeypatch, tmp_path, flags, expected):
    import autoscraper.cli.agent as cli
    from autoscraper.workflow.outcome import TaskOutcome

    seen = []
    monkeypatch.setenv("AUTOSCRAPER_API_KEY", "fake")

    def run(*args, **kwargs):
        seen.append(kwargs["validation"].timeout_ms)
        return TaskOutcome(status="unfulfilled")

    monkeypatch.setattr(cli, "run_task", run)
    monkeypatch.setattr(
        "sys.argv",
        ["autoscraper-agent", URL, TASK, "--spec-output", str(tmp_path / "spec.json"), *flags],
    )
    assert cli.main() == 1
    assert seen == [expected]


# 语义等待模型在多轮修复中保持同一实例和独立预算。
def test_wait_model_budget_shared_across_repairs(candidate, settings, workflow, monkeypatch):
    broken = candidate.model_copy(deep=True)
    broken.fields.pop("price")
    received = install_candidates(monkeypatch, workflow, [broken, candidate])
    monkeypatch.setattr(
        ScraperExecutor,
        "run",
        lambda *args: ScrapeResult(
            records=[{"title": "A", "price": "1"}, {"title": "B", "price": "2"}],
            stop_reason="max_items",
        ),
    )
    outcome = run_task(URL, TASK, settings)
    assert outcome.status == "validated"
    assert received[0]["wait_llm"] is received[1]["wait_llm"]
    assert received[0]["wait_llm"] is not workflow[1]
    assert outcome.summary()["wait_llm_calls"] == 0


# 剩余调用不足以导航、测试和提交时不创建修复轮，保留最初的缺字段诊断。
def test_repair_is_not_started_with_only_one_call_remaining(candidate, settings, workflow, monkeypatch):
    settings.max_llm_calls = 2
    settings.repair_reserve_calls = 0
    received = install_candidates(monkeypatch, workflow, [candidate])
    result = run_task(URL, "前2本，字段名为title、price、description", settings)
    assert len(received) == result.attempts == result.llm_calls == 1
    assert result.feedback[0].code == "budget_exhausted"
    assert result.feedback[1].code == "missing_required_field"
    assert "description" in result.feedback[1].fields
    assert len(result.history) == 1


# 真实请求计数贯穿需求重试、首轮阶段边界和修复；Step 配置不参与请求额度换算。
@pytest.mark.parametrize("failure", ["request_limit", "invalid_spec", "repair_exhausted"])
def test_reserved_requests_survive_requirements_retries_and_repair(
    candidate, settings, monkeypatch, failure,
):
    from unittest.mock import AsyncMock
    from autoscraper.agent.explorer import InvalidSpecError
    from autoscraper.agent.llm import make_llm
    from autoscraper.agent.responses import ChatOpenAIResponses
    import autoscraper.agent.llm as models
    import autoscraper.workflow.runner as module

    settings.max_steps = 2
    settings.max_llm_calls = 8
    settings.repair_reserve_calls = 3
    model = make_llm(settings)
    monkeypatch.setattr(models, "make_llm", lambda settings: model)
    create = AsyncMock(side_effect=[
        SimpleNamespace(status="completed", output_text=text, usage=None)
        for text in ["invalid", '{"fields":["title","price"],"count":2}'] + ["ok"] * 6
    ])
    monkeypatch.setattr(ChatOpenAIResponses, "get_client", lambda self: SimpleNamespace(
        responses=SimpleNamespace(create=create)
    ))
    attempts = []

    async def generate(url, task, run_settings, **kwargs):
        attempts.append(kwargs)
        assert run_settings.max_steps == 2
        assert kwargs["llm"] is model
        if len(attempts) == 1:
            assert model.calls == 2  # 需求解析的失败响应也已扣费。
            assert model.call_limit == 5
            for _ in range(3):
                await model.ainvoke([])
            if failure != "request_limit":
                raise InvalidSpecError("Submission rejected: missing detail-page evidence")
            await model.ainvoke([])  # 阶段边界拒绝请求，工作流再决定是否启动修复。
            pytest.fail("Initial exploration must not spend the reserve")
        assert model.calls == 5
        assert model.call_limit == 8
        assert kwargs["feedback"][0].code == "invalid_spec"
        for _ in range(3 if failure == "repair_exhausted" else 2):
            await model.ainvoke([])
        if failure == "repair_exhausted":
            raise InvalidSpecError("Submission rejected: detail-page evidence still missing")
        return candidate

    monkeypatch.setattr(module, "generate_spec", generate)
    monkeypatch.setattr(ScraperExecutor, "run", lambda *args: ScrapeResult(
        records=[{"title": "A", "price": "1"}, {"title": "B", "price": "2"}],
        stop_reason="max_items",
    ))
    result = run_task(URL, "抓取书名和价格", settings)
    if failure == "request_limit":
        assert result.status == "unfulfilled"
        assert result.attempts == 1
        assert result.llm_calls == create.await_count == 5
        assert result.repair_not_started_reason == "no_recoverable_feedback"
        assert result.feedback[0].code == "budget_exhausted"
        assert not result.feedback[0].recoverable
        return
    if failure == "repair_exhausted":
        assert result.status == "unfulfilled"
        assert result.repair_not_started_reason == "insufficient_llm_requests"
        assert [item.code for item in result.feedback] == ["budget_exhausted", "invalid_spec"]
        assert "detail-page evidence" in result.feedback[1].message
    else:
        assert result.status == "validated"
    assert result.attempts == 2
    assert result.llm_calls == create.await_count == (8 if failure == "repair_exhausted" else 7)
    assert result.summary()["agent_step_limit"] == 2
    assert result.summary()["max_llm_calls"] == 8
    assert result.summary()["repair_reserve_calls"] == 3
    assert result.history[0]["feedback"][0]["code"] == "invalid_spec"


# 需求解析重试耗尽首轮额度时保留修复预留，不创建无请求可用的浏览器。
def test_requirements_cannot_consume_repair_reserve(settings, monkeypatch):
    from unittest.mock import AsyncMock
    from autoscraper.agent.llm import make_llm
    from autoscraper.agent.responses import ChatOpenAIResponses
    import autoscraper.agent.llm as models
    import autoscraper.workflow.runner as module

    settings.max_llm_calls = 5
    settings.repair_reserve_calls = 3
    model = make_llm(settings)
    monkeypatch.setattr(models, "make_llm", lambda settings: model)
    create = AsyncMock(return_value=SimpleNamespace(
        status="completed", output_text="invalid", usage=None,
    ))
    monkeypatch.setattr(ChatOpenAIResponses, "get_client", lambda self: SimpleNamespace(
        responses=SimpleNamespace(create=create)
    ))
    monkeypatch.setattr(module, "generate_spec", lambda *args, **kwargs: pytest.fail("No browser"))
    result = run_task(URL, "抓取书名和价格", settings)
    assert result.status == "unfulfilled"
    assert result.attempts == 0
    assert result.feedback[0].code == "budget_exhausted"
    assert model.calls == create.await_count == 2
    assert model.max_calls - model.calls == 3


# 禁用修复时不扣留额度；即使失败也不开放额外请求，不会把候选规则标成成功。
def test_no_repairs_uses_full_hard_limit(candidate, settings, workflow, monkeypatch):
    module, model = workflow
    received = install_candidates(monkeypatch, workflow, [candidate])
    result = run_task(URL, "前2本，字段名为title、price、description", settings,
                      validation=ValidationSettings(max_repairs=0))
    assert model.call_limit == settings.max_llm_calls
    assert result.repair_reserve_calls == 0
    assert len(received) == result.attempts == 1
    assert result.status == "unfulfilled"


# 被提交门槛拒绝的候选必须传给修复，但未修复时不能作为正式规则落地。
@pytest.mark.parametrize("mode", ["repair", "no_repairs", "insufficient_budget"])
def test_rejected_candidate_and_evidence_reach_repair(
    candidate, settings, workflow, monkeypatch, mode,
):
    from autoscraper.agent.submission import SubmissionChecks
    from autoscraper.workflow.validation import Feedback

    module, model = workflow
    checks = SubmissionChecks()
    rejected = candidate.model_copy(update={"item_selector": ".wrong-root"})
    checks.last_candidate = rejected
    checks.last_issues = [Feedback(code="selector_failure", message="Item root changed")]
    checks.remember({
        "url": URL, "item_selector": "article", "count": 2, "status": "sample_passed",
        "samples": [{"matches": 1, "values": ["A"]}],
    }, "h2")
    received = []

    async def generate(url, task, run_settings, **kwargs):
        received.append(kwargs)
        model.calls += 1
        if len(received) == 1:
            assert kwargs["previous_spec"] is None and kwargs["evidence_summary"] is None
            raise checks.rejection_error()
        assert kwargs["previous_spec"] == rejected
        assert kwargs["evidence_summary"] == checks.repair_summary()
        assert kwargs["feedback"][0].code == "invalid_spec"
        return candidate

    monkeypatch.setattr(module, "generate_spec", generate)
    monkeypatch.setattr(ScraperExecutor, "run", lambda *args: ScrapeResult(
        records=[{"title": "A", "price": "1"}, {"title": "B", "price": "2"}],
        stop_reason="max_items",
    ))
    if mode == "insufficient_budget":
        settings.max_llm_calls = 2
        settings.repair_reserve_calls = 0
    policy = ValidationSettings(max_repairs=0 if mode == "no_repairs" else 2)
    result = run_task(URL, TASK, settings, validation=policy)
    if mode == "repair":
        assert result.status == "validated"
        assert result.spec == candidate
        assert len(received) == result.llm_calls == 2
    else:
        assert result.status == "unfulfilled"
        assert result.spec is None
        assert len(received) == 1
        assert any(item.code == "invalid_spec" for item in result.feedback)
    assert result.history[0]["feedback"][0]["code"] == "invalid_spec"


# 首轮参考协议不带失败反馈；候选仍须满足当前需求，缺字段后沿用原修复流程。
@pytest.mark.parametrize("with_reference", [False, True])
def test_initial_spec_is_reference_then_repair_uses_candidate(
    candidate, settings, workflow, monkeypatch, with_reference
):
    reference = candidate.model_copy(deep=True)
    reference.fields["price"].selector = ".old-price"
    original = reference.model_dump_json()
    broken = candidate.model_copy(deep=True)
    del broken.fields["price"]
    received = install_candidates(monkeypatch, workflow, [broken, candidate])
    monkeypatch.setattr(
        ScraperExecutor, "run", lambda *args: ScrapeResult(
            records=[{"title": "A", "price": "1"}, {"title": "B", "price": "2"}],
            stop_reason="max_items",
        )
    )
    outcome = run_task(URL, TASK, settings, initial_spec=reference if with_reference else None)
    assert outcome.status == "validated"
    assert received[0]["initial_spec"] == (reference if with_reference else None)
    assert received[0]["previous_spec"] is None
    assert not received[0]["feedback"]
    assert received[0]["requirements"].fields == ["title", "price"]
    assert received[1]["initial_spec"] is None
    assert received[1]["previous_spec"] == broken
    assert received[1]["feedback"][0].code == "missing_required_field"
    assert reference.model_dump_json() == original


# CLI 输入可以与正式输出同名；失败及候选另存都不能破坏原始协议。
@pytest.mark.parametrize("input_name", ["rule.json", "rule.unvalidated.json", "reference.json"])
@pytest.mark.parametrize("status", ["validated", "unfulfilled"])
def test_cli_initial_spec_and_input_preservation(
    candidate, monkeypatch, tmp_path, capsys, input_name, status
):
    import autoscraper.cli.agent as cli
    from autoscraper.workflow.outcome import TaskOutcome

    input_path = tmp_path / input_name
    output_path = tmp_path / "rule.json"
    original = candidate.model_dump_json(indent=2)
    input_path.write_text(original, encoding="utf-8-sig")
    original_bytes = input_path.read_bytes()
    updated = candidate.model_copy(deep=True)
    updated.fields["price"].selector = ".updated-price"
    received = []

    def run(*args, **kwargs):
        received.append(kwargs["initial_spec"])
        return TaskOutcome(status=status, spec=updated, failure_stage="validation")

    monkeypatch.setenv("AUTOSCRAPER_API_KEY", "fake")
    monkeypatch.setattr(cli, "run_task", run)
    monkeypatch.setattr("sys.argv", [
        "autoscraper-agent", URL, TASK, "--spec-input", str(input_path),
        "--spec-output", str(output_path),
    ])
    assert cli.main() == (0 if status == "validated" else 1)
    report = json.loads(capsys.readouterr().out)
    assert received == [candidate]
    if status == "validated":
        assert ScraperSpec.model_validate_json(output_path.read_text()) == updated
    else:
        assert Path(report["unvalidated_spec"]).resolve() != input_path.resolve()
    if status != "validated" or input_path != output_path:
        assert input_path.read_bytes() == original_bytes


# 非法参考输入必须在启动模型前失败，也不能触碰正式输出。
@pytest.mark.parametrize("payload", [None, "{bad json", "{}"])
def test_cli_invalid_initial_spec_fails_before_workflow(monkeypatch, tmp_path, payload):
    import autoscraper.cli.agent as cli

    input_path, output_path = tmp_path / "input.json", tmp_path / "rule.json"
    if payload is not None:
        input_path.write_text(payload)
    output_path.write_text("original")
    monkeypatch.setattr(cli, "settings_from_env", lambda **kwargs: pytest.fail("Do not call model"))
    monkeypatch.setattr("sys.argv", [
        "autoscraper-agent", URL, TASK, "--spec-input", str(input_path),
        "--spec-output", str(output_path),
    ])
    assert cli.main() == 1
    assert output_path.read_text() == "original"


# 数据导出和认证状态会独立写文件，不能借这些路径覆盖参考协议。
@pytest.mark.parametrize("flag", ["--output", "--auth-state"])
def test_cli_initial_spec_rejects_other_writers(candidate, monkeypatch, tmp_path, flag):
    import autoscraper.cli.agent as cli

    input_path = tmp_path / "input.json"
    input_path.write_text(candidate.model_dump_json())
    original = input_path.read_bytes()
    monkeypatch.setattr(cli, "settings_from_env", lambda **kwargs: pytest.fail("Do not call model"))
    monkeypatch.setattr("sys.argv", [
        "autoscraper-agent", URL, TASK, "--spec-input", str(input_path),
        "--spec-output", str(tmp_path / "rule.json"), flag, str(input_path),
    ])
    assert cli.main() == 1
    assert input_path.read_bytes() == original


# 工作流必填检查支持合法列表及零计数，仍拒绝空列表、空文本和无效类型。
@pytest.mark.parametrize("value,valid", [
    (0, True), (2, True), (["A", "B"], True), ([], True), ([" "], True),
    (["A", ""], True), (None, True), (False, True), (-1.25, True), (float("inf"), False), (" ", True),
])
def test_validate_multi_element_values(value, valid):
    result = ScrapeResult(records=[{"value": value}], stop_reason="no_pagination")
    issues = validate_data(result, TaskRequirements(fields=["value"]))
    assert (not issues) is valid
    if not valid:
        assert issues[0].code == "missing_data"
