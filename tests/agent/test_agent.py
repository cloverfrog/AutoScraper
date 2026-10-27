# Agent 与模型适配测试：覆盖输出解析、DOM 工具、调用预算、探索清理和 CLI 保存。
# 大部分使用模拟模型或本地 Chromium；真实模型测试需显式开启 RUN_LLM。
import asyncio
import json
import os
from importlib import import_module
from types import SimpleNamespace

import pytest

from autoscraper.agent.explorer import (
    AgentError,
    AgentSettings,
    build_task,
    generate_spec,
    parse_spec_output,
)
from autoscraper.cli.agent import main, settings_from_env
from autoscraper.scraping.executor import ScraperExecutor


# 供解析、执行和 CLI 测试复用的最小合法规则。
@pytest.fixture
def rule():
    return {
        "start_url": "https://fixture.test/",
        "item_selector": ".record",
        "fields": {"title": {"selector": "h2"}},
        "max_items": 1,
        "on_item_error": "fail",
    }


# 验证纯 JSON 和单个代码块都能解析成协议模型。
@pytest.mark.parametrize("fenced", [False, True])
def test_parse_output(rule, fenced):
    text = json.dumps(rule)
    spec = parse_spec_output(f"```json\n{text}\n```" if fenced else text)
    assert spec.fields["title"].selector == "h2"


# 拒绝缺失、超长及不符合约定格式的模型输出。
@pytest.mark.parametrize(
    "text",
    [
        None,
        "",
        "print('hello')",
        "{}",
        "[]",
        'before {"start_url": "https://example.com"}',
        "```python\nprint(1)\n```",
        "x" * 100001,
    ],
    ids=["none", "empty", "code", "object", "array", "prose", "python", "oversized"],
)
def test_reject_output(text):
    with pytest.raises(AgentError):
        parse_spec_output(text)


# 验证不受协议支持的扩展字段不会被静默接受。
def test_reject_protocol_extensions(rule):
    with pytest.raises(AgentError, match="Invalid ScraperSpec"):
        parse_spec_output(json.dumps({**rule, "script": "alert(1)"}))
    rule["fields"]["title"]["extract"] = "python"
    with pytest.raises(AgentError):
        parse_spec_output(json.dumps(rule))


# 检查环境变量读取、默认值和显式覆盖的优先级。
def test_env_settings(monkeypatch):
    monkeypatch.delenv("AUTOSCRAPER_API_KEY", raising=False)
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    with pytest.raises(AgentError, match="API_KEY"):
        settings_from_env()
    monkeypatch.setenv("DEEPSEEK_API_KEY", "test-secret")
    settings = settings_from_env(model="example", max_steps=3)
    assert settings.model == "example"
    assert settings.max_steps == 3
    assert str(settings.base_url) == "https://api.deepseek.com/"
    assert "test-secret" not in repr(settings)


# 提示词应说明通用协议，不写死示例站点选择器。
def test_prompt_has_protocol_without_site_selectors():
    task = build_task("https://books.toscrape.com/", "Get 25 titles")
    assert "Get 25 titles" in task
    assert "product_pod" not in task
    assert "li.next" not in task
    with pytest.raises(ValueError):
        build_task("file:///tmp/data", "Get titles")


# 验证 Agent 格式的规则可直接交给独立执行器使用。
def test_generated_rule_with_existing_executor(rule):
    from playwright.sync_api import sync_playwright

    spec = parse_spec_output(json.dumps(rule))
    with sync_playwright() as p:
        browser = p.chromium.launch()
        try:
            page = browser.new_page()
            page.route(
                "**/*",
                lambda route: route.fulfill(
                    content_type="text/html; charset=utf-8",
                    body='<div class="record"><h2>Generated rule works</h2></div>',
                ),
            )
            result = ScraperExecutor().run_page(spec, page)
        finally:
            browser.close()
    assert result.records == [{"title": "Generated rule works"}]


# DOM 工具应相对每条记录测试字段，并按字面读取属性名。
def test_dom_helpers_use_relative_css_and_literal_attributes():
    pytest.importorskip("browser_use")
    from autoscraper.agent.dom import INSPECT_SCRIPT, SELECTOR_SCRIPT
    from playwright.sync_api import sync_playwright

    with sync_playwright() as p:
        browser = p.chromium.launch()
        try:
            page = browser.new_page()
            page.set_content(
                '<main><article class="entry"><a href="/1" '
                'title="Full title">Short</a></article>'
                '<article class="entry"><a href="/2">Second</a></article></main>'
            )
            dom = page.evaluate(INSPECT_SCRIPT, "main")
            assert any(node["attrs"].get("class") == "entry" for node in dom["nodes"])
            result = page.evaluate(
                f"(args) => ({SELECTOR_SCRIPT})(...args)", ["article.entry", "a", "title"]
            )
            assert result["count"] == 2
            assert result["samples"][0] == {
                "status": "single_match",
                "matches": 1,
                "values": ["Full title"],
            }
            assert result["samples"][1]["values"] == [None]
            # Attribute input is data, including when it looks like source code.
            injection = page.evaluate(
                f"(args) => ({SELECTOR_SCRIPT})(...args)",
                ["article.entry", "a", "'); alert(1); //"],
            )
            assert injection["samples"][0]["values"] == [None]
        finally:
            browser.close()


# 创建带有多种匹配情况的本地页面，用于真实执行选择器测试脚本。
@pytest.fixture
def selector_page():
    pytest.importorskip("browser_use")
    from playwright.sync_api import sync_playwright

    with sync_playwright() as p:
        browser = p.chromium.launch()
        try:
            page = browser.new_page()
            page.set_content(
                '<article class="entry"><p class="card-text">One</p>'
                '<p class="card-text">Two</p></article>'
                '<article class="entry"><p class="card-text">Three</p></article>'
            )
            yield page
        finally:
            browser.close()


# 在测试页面中执行项目固定的选择器检查脚本。
def evaluate_selector(page, item, field="", attribute=""):
    from autoscraper.agent.dom import SELECTOR_SCRIPT

    return page.evaluate(f"(args) => ({SELECTOR_SCRIPT})(...args)", [item, field, attribute])


# 无效的列表或字段 CSS 必须准确标明出错参数。
@pytest.mark.parametrize(
    "item,field,parameter,count",
    [
        ("article:nth-of--type(2)", "p", "item_selector", None),
        ("article", "p.card-text:nth-of--type(2)", "field_selector", 2),
        ("article", "p.card-text:nth-of-type((3)", "field_selector", 2),
        (".missing", "p:nth-of-type((3)", "field_selector", 0),
        ("", "p", "item_selector", None),
        ("   ", "p", "item_selector", None),
    ],
)
def test_selector_invalid_css(selector_page, item, field, parameter, count):
    result = evaluate_selector(selector_page, item, field, "title")
    assert result["status"] == "invalid_css"
    assert result["error_parameter"] == parameter
    assert result["message"]
    assert result["count"] == count
    assert result["item_selector"] == item
    assert result["field_selector"] == field
    assert result["attribute"] == "title"
    assert result["samples"] == []
    assert result["tested_count"] == 0
    assert result["all_items_tested"] is False


# 区分字段零匹配、唯一匹配和多匹配，保留每条样本的结果。
@pytest.mark.parametrize(
    "field,status,matches,sample_statuses",
    [
        ("p.cardd-text:nth-of-type(1)", "sample_failed", [0, 0], ["no_match", "no_match"]),
        ("p.card-text", "sample_failed", [2, 1], ["multiple_matches", "single_match"]),
        ("p.card-text:nth-of-type(1)", "sample_passed", [1, 1], ["single_match"] * 2),
        ("", "sample_passed", [1, 1], ["single_match"] * 2),
        ("p.card-text:nth-of-type(2)", "sample_failed", [1, 0], ["single_match", "no_match"]),
    ],
)
def test_selector_field_cardinality(selector_page, field, status, matches, sample_statuses):
    result = evaluate_selector(selector_page, "article.entry", field)
    assert result["status"] == status
    assert result["count"] == 2
    assert result["item_selector"] == "article.entry"
    assert result["field_selector"] == field
    assert result["attribute"] == ""
    assert result["tested_count"] == 2
    assert result["all_items_tested"] is True
    assert [sample["matches"] for sample in result["samples"]] == matches
    assert [sample["status"] for sample in result["samples"]] == sample_statuses
    if field == "p.card-text:nth-of-type(1)":
        assert [sample["values"] for sample in result["samples"]] == [["One"], ["Three"]]


# 列表选择器无结果时返回明确状态，而不是误报成功。
def test_selector_no_items(selector_page):
    result = evaluate_selector(selector_page, ".missing", "p")
    assert result["status"] == "no_items"
    assert result["count"] == result["tested_count"] == 0
    assert result["samples"] == []


# 检查采样范围、是否遍历全部记录和样本值长度上限。
def test_selector_sample_scope_and_value_limits(selector_page):
    selector_page.set_content(
        '<article><p title="'
        + "a" * 350
        + '">One</p><p>Two</p><p>Three</p></article>'
        + "<article><p>Text</p></article>" * 4
        + "<article><span>Unsampled missing field</span></article>"
    )
    multiple = evaluate_selector(selector_page, "article", "p", "title")
    assert multiple["samples"][0]["matches"] == 3
    assert multiple["samples"][0]["values"] == ["a" * 300, None]
    result = evaluate_selector(selector_page, "article", "p:first-child", "title")
    assert result["status"] == "sample_passed"  # Cardinality only, including missing attributes.
    assert result["count"] == 6
    assert result["tested_count"] == len(result["samples"]) == 5
    assert result["all_items_tested"] is False
    assert result["samples"][1]["values"] == [None]


# 工具应原样传递待测试参数，并保留运行错误供 Agent 处理。
def test_selector_tool_passes_exact_inputs_and_keeps_runtime_errors():
    pytest.importorskip("browser_use")
    from autoscraper.agent.dom import SELECTOR_SCRIPT
    from autoscraper.agent.tools import build_tools

    action = build_tools().registry.registry.actions["test_selector"].function
    raw = '{"status":"invalid_css","item_selector":"article"}'

    class Page:
        async def evaluate(self, *args):
            assert args == (SELECTOR_SCRIPT, "article", "p:nth-of--type(2)", "href")
            return raw

    class Session:
        async def must_get_current_page(self):
            return Page()

    result = asyncio.run(
        action(
            browser_session=Session(),
            item_selector="article",
            field_selector="p:nth-of--type(2)",
            attribute="href",
        )
    )
    assert result.extracted_content == raw

    class BrokenSession:
        async def must_get_current_page(self):
            raise RuntimeError("Browser disconnected")

    with pytest.raises(RuntimeError, match="Browser disconnected"):
        asyncio.run(action(browser_session=BrokenSession(), item_selector="article"))


# 提示词要求测试最终提交的原样选择器，避免测试与提交不一致。
def test_prompt_requires_exact_selector_tests():
    task = build_task("https://fixture.test/", "Get titles")
    assert "Retest changed selectors verbatim" in task
    assert "Never claim failed or untested rules are verified" in task


# 输出 Schema 由框架注入，任务提示词不再重复整份 Schema。
def test_browser_use_supplies_schema_without_manual_duplicate():
    bu = pytest.importorskip("browser_use")
    from autoscraper.agent.tools import build_tools
    from autoscraper.scraping.spec import ScraperSpec

    task = build_task("https://fixture.test/", "Get titles")
    # Contract with pinned browser-use: removing our copy must not remove the
    # schema from the model's task or from the structured done action.
    assert '"$defs"' not in task
    enhanced = bu.Agent._enhance_task_with_schema(SimpleNamespace(), task, ScraperSpec)
    prefix, schema = enhanced.split("\nExpected output format: ScraperSpec\n")
    assert prefix == task
    assert json.loads(schema) == ScraperSpec.model_json_schema()
    tools = build_tools()
    tools.use_structured_output_action(ScraperSpec)
    done_schema = tools.registry.registry.actions["done"].param_model.model_json_schema()
    assert done_schema["properties"]["data"]["$ref"] == "#/$defs/ScraperSpec"
    assert "detail" in done_schema["$defs"]["ScraperSpec"]["properties"]


# 初始探索和修复都应保留任务约束、历史候选及剩余等待预算。
@pytest.mark.parametrize("repair", [False, True])
def test_exploration_prompt_preserves_task_context(monkeypatch, rule, repair):
    bu = pytest.importorskip("browser_use")
    from autoscraper.scraping.spec import ScraperSpec
    from autoscraper.workflow.validation import Feedback, TaskRequirements

    captured = {}

    class Browser:
        def __init__(self, **kwargs):
            pass

        async def kill(self):
            captured["killed"] = True

    class Agent:
        def __init__(self, **kwargs):
            captured.update(kwargs)

        async def run(self, **kwargs):
            return SimpleNamespace(
                is_successful=lambda: True, final_result=lambda: json.dumps(rule)
            )

    requirements = TaskRequirements(fields=["title"], count=1)
    previous = ScraperSpec.model_validate(rule) if repair else None
    feedback = [Feedback(code="selector_failure", message="wrong title CSS", fields=["title"])]
    task = 'Get title; preserve literal input "example"'
    settings = AgentSettings(
        api_key="fake",
        model="test-model",
        base_url="https://fixture.test/",
        content_timeout_ms=4000,
    )
    monkeypatch.setattr(bu, "BrowserSession", Browser)
    monkeypatch.setattr(bu, "Agent", Agent)
    asyncio.run(
        generate_spec(
            rule["start_url"],
            task,
            settings,
            requirements=requirements,
            previous_spec=previous,
            feedback=feedback if repair else None,
            llm=object(),
            wait_llm=SimpleNamespace(max_calls=8, calls=3),
        )
    )
    prompt = captured["task"]
    assert json.dumps(task, ensure_ascii=False) in prompt
    assert requirements.model_dump_json() in prompt
    assert ("Previous failed candidate:" in prompt) is repair
    assert ("Repair these validation failures and retest:" in prompt) is repair
    if repair:
        assert previous.model_dump_json() in prompt
        assert json.dumps([item.model_dump() for item in feedback], ensure_ascii=False) in prompt
    assert "4000 ms per tool call; 5 semantic LLM calls remaining task-wide" in prompt
    assert captured["output_model_schema"] is ScraperSpec
    assert "report_unsupported" in captured["tools"].registry.registry.actions
    assert captured["killed"]


# 覆盖成功、非法输出、未完成和超时退出，确认浏览器总会清理。
@pytest.mark.parametrize("outcome", ["success", "invalid", "unfinished", "timeout"])
def test_runner_limits_validation_cleanup(monkeypatch, rule, outcome):
    bu = pytest.importorskip("browser_use")
    import autoscraper.agent.llm as helpers
    # 先加载工具模块，避免 BrowserSession 替身被缓存为工具参数注解。
    import_module("autoscraper.agent.tools")

    class Browser:
        killed = False

        def __init__(self, **kwargs):
            pass

        async def kill(self):
            Browser.killed = True

    class Agent:
        def __init__(self, **kwargs):
            assert kwargs["use_judge"] is False
            assert kwargs["use_thinking"] is False
            assert kwargs["flash_mode"] is True
            assert kwargs["message_compaction"] is False
            assert kwargs["max_failures"] == 1

        async def run(self, max_steps):
            assert max_steps == 2
            if outcome == "timeout":
                await asyncio.sleep(5)
            return SimpleNamespace(
                is_successful=lambda: outcome != "unfinished",
                final_result=lambda: "{}" if outcome == "invalid" else json.dumps(rule),
            )

    monkeypatch.setattr(bu, "BrowserSession", Browser)
    monkeypatch.setattr(bu, "Agent", Agent)
    monkeypatch.setattr(helpers, "make_llm", lambda settings: object())
    settings = AgentSettings(
        api_key="fake",
        model="deepseek-flash",
        base_url="https://api.deepseek.com",
        max_steps=2,
        timeout_seconds=1,
    )
    if outcome == "success":
        assert asyncio.run(generate_spec(rule["start_url"], "title", settings)).max_items == 1
    else:
        with pytest.raises(AgentError):
            asyncio.run(generate_spec(rule["start_url"], "title", settings))
    assert Browser.killed


# 检查受限工具集合和模型调用硬上限，禁止越过预算继续请求。
def test_tools_and_llm_budget(monkeypatch):
    pytest.importorskip("browser_use")
    from browser_use.llm.views import ChatInvokeCompletion
    from autoscraper.agent.tools import build_tools
    from autoscraper.agent.llm import make_llm
    from autoscraper.agent.responses import ChatOpenAIResponses

    assert set(build_tools().registry.registry.actions) == {
        "navigate",
        "done",
        "inspect_page",
        "test_selector",
        "wait_for_selector",
        "wait_for_content",
    }
    settings = AgentSettings(
        api_key="fake",
        model="deepseek-flash",
        base_url="https://api.deepseek.com",
        max_steps=1,
    )
    llm = make_llm(settings)
    assert isinstance(llm, ChatOpenAIResponses)
    assert llm.max_retries == 0
    assert llm.max_output_tokens == 3000

    async def fake_create(**kwargs):
        return SimpleNamespace(
            status="completed",
            output_text="ok",
            usage=None,
        )

    monkeypatch.setattr(
        llm,
        "get_client",
        lambda: SimpleNamespace(responses=SimpleNamespace(create=fake_create)),
    )
    assert asyncio.run(llm.ainvoke([])) == ChatInvokeCompletion(
        completion="ok", usage=None, stop_reason="completed"
    )
    with pytest.raises(RuntimeError, match="budget"):
        asyncio.run(llm.ainvoke([]))


# 检查消息、JSON Schema 和输出参数如何映射到 Responses 请求。
def test_openai_responses_structured_request(monkeypatch):
    pytest.importorskip("browser_use")
    from browser_use.llm.messages import SystemMessage, UserMessage
    from pydantic import BaseModel

    from autoscraper.agent.responses import ChatOpenAIResponses

    class Output(BaseModel):
        answer: str

    captured = {}

    async def fake_create(**kwargs):
        captured.update(kwargs)
        return SimpleNamespace(
            status="completed",
            output_text='{"answer":"ok"}',
            usage=SimpleNamespace(
                input_tokens=10,
                input_tokens_details=SimpleNamespace(cached_tokens=4),
                output_tokens=3,
                total_tokens=13,
            ),
        )

    llm = ChatOpenAIResponses(model="test-model", api_key="fake", reasoning_effort="none")
    monkeypatch.setattr(
        llm,
        "get_client",
        lambda: SimpleNamespace(responses=SimpleNamespace(create=fake_create)),
    )
    result = asyncio.run(
        llm.ainvoke(
            [SystemMessage(content="Return JSON"), UserMessage(content="answer")],
            Output,
            session_id="browser-use-only",
        )
    )

    assert result.completion == Output(answer="ok")
    assert result.usage.prompt_cached_tokens == 4
    assert captured["input"] == [
        {"role": "system", "content": "Return JSON"},
        {"role": "user", "content": "answer"},
    ]
    assert captured["text"]["format"]["type"] == "json_schema"
    assert captured["text"]["format"]["name"] == "agent_output"
    assert "strict" not in captured["text"]["format"]
    assert captured["reasoning"] == {"effort": "none"}
    assert "session_id" not in captured
    assert "tools" not in captured


# 自定义兼容接口地址仍应使用项目的 Responses 适配器。
def test_custom_endpoint_uses_responses_adapter():
    pytest.importorskip("browser_use")
    from autoscraper.agent.llm import LimitedChatOpenAI, make_llm

    settings = AgentSettings(api_key="fake", model="compatible", base_url="https://llm.example/v1")
    llm = make_llm(settings)
    assert isinstance(llm, LimitedChatOpenAI)
    assert str(llm.base_url) == "https://llm.example/v1"


# CLI 成功时应保存规则并导出工作流返回的数据。
def test_cli_save_and_execute(monkeypatch, rule, tmp_path):
    import autoscraper.cli.agent as cli
    from autoscraper.scraping.executor import ScrapeResult
    from autoscraper.workflow.validation import TaskRequirements
    from autoscraper.workflow.runner import TaskOutcome

    monkeypatch.setenv("AUTOSCRAPER_API_KEY", "fake")
    monkeypatch.setattr(
        cli,
        "run_task",
        lambda *args, **kwargs: TaskOutcome(
            status="success",
            requirements=TaskRequirements(fields=["title"], count=1),
            spec=parse_spec_output(json.dumps(rule)),
            data=ScrapeResult(records=[{"title": "works"}], stop_reason="max_items"),
        ),
    )
    path = tmp_path / "rule.json"
    output = tmp_path / "data.json"
    monkeypatch.setattr(
        "sys.argv",
        [
            "autoscraper-agent",
            rule["start_url"],
            "titles",
            "--spec-output",
            str(path),
            "--output",
            str(output),
        ],
    )
    assert main() == 0
    assert parse_spec_output(path.read_text()).max_items == 1
    assert json.loads(output.read_text()) == [{"title": "works"}]


# 规则和数据路径冲突必须在付费模型调用前被拒绝。
def test_cli_rejects_collision_before_llm(monkeypatch, tmp_path):
    path = str(tmp_path / "same.json")
    monkeypatch.setattr(
        "sys.argv",
        [
            "autoscraper-agent",
            "https://example.com",
            "titles",
            "--spec-output",
            path,
            "--output",
            path,
        ],
    )
    assert main() == 1


# 可选付费端到端测试：真实探索 Books to Scrape 并验证生成规则。
@pytest.mark.llm
@pytest.mark.skipif(os.getenv("RUN_LLM") != "1", reason="Set RUN_LLM=1 for paid LLM test")
def test_real_books_agent(tmp_path):
    """No reference selectors/spec supplied to the model; prove actual end-to-end extraction."""
    from autoscraper.agent.explorer import save_spec

    settings = settings_from_env(max_steps=10, timeout_seconds=240)
    spec = asyncio.run(
        generate_spec(
            "https://books.toscrape.com/",
            "Extract the first 25 books with title, price, and url. Use these exact field names. "
            "Include pagination and full absolute detail URLs.",
            settings,
        )
    )
    path = tmp_path / "generated.json"
    save_spec(spec, path)
    reloaded = parse_spec_output(path.read_text(encoding="utf-8"))
    result = ScraperExecutor().run(reloaded)
    assert reloaded.max_items == 25
    assert set(reloaded.fields) == {"title", "price", "url"}
    assert len(result.records) == len({row["url"] for row in result.records}) == 25
    assert result.pages_visited == 2
    assert not result.errors
    assert result.records[0]["title"] == "A Light in the Attic"
    assert all(
        row["price"].startswith("£") and row["url"].startswith("https://") for row in result.records
    )
