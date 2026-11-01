# 网络闭环：脚本化模型决策配合真实 browser-use 浏览器、工具、提交检查与独立 Executor。
# 本地 HTTP 服务复用可运行示例；不调用付费 LLM，也不跳过首次导航监听。
import asyncio
import json
from pathlib import Path
import runpy
from threading import Thread
from http.server import ThreadingHTTPServer
from types import SimpleNamespace

import pytest

from autoscraper.agent.explorer import AgentSettings
from autoscraper.agent.network import sample_output
from autoscraper.browser.network import AgentResponseCapture, CapturedResponse
from autoscraper.scraping.executor import ScraperExecutor
from autoscraper.scraping.spec import ScraperSpec
from autoscraper.workflow.runner import run_task
from autoscraper.workflow.validation import ValidationSettings
from autoscraper.observability.tracing import summarize_tool_output


@pytest.fixture
def local_site():
    handler = runpy.run_path("examples/network_demo.py")["Handler"]
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}/"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


@pytest.mark.parametrize("pagination", ["click", "scroll"])
def test_agent_tools_generate_validated_reusable_network_rule(monkeypatch, local_site, explicit_unfiltered_requirements, tmp_path, pagination):
    bu = pytest.importorskip("browser_use")
    from browser_use import ActionResult
    import autoscraper.agent.llm as models
    from autoscraper.scraping.export import export_records

    model = SimpleNamespace(calls=0)
    monkeypatch.setattr(models, "make_llm", lambda settings: model)
    monkeypatch.setattr(models, "make_wait_llm", lambda settings: SimpleNamespace(calls=0, max_calls=0))
    spec_data = json.loads(Path("examples/books.network.json").read_text())
    start_url = local_site + "scroll" if pagination == "scroll" else local_site
    spec_data.update(start_url=start_url, max_items=10)
    if pagination == "scroll":
        spec_data["network"]["pagination"] = dict(type="scroll", loading_selector="#loading",
            end_selector="#end", max_scrolls=10, no_change_limit=2)
    spec_data["network"]["url_pattern"] = local_site + "api/items"
    observations = {}

    class Agent:
        def __init__(self, **kwargs):
            self.browser = kwargs["browser_session"]
            self.tools = kwargs["tools"]
            self.history = SimpleNamespace(history=[])
            self.tools.use_structured_output_action(ScraperSpec)

        async def run(self, **kwargs):
            model.calls += 1
            capture = self.tools.network_capture
            # 第一个浏览器动作之前上下文监听已经安装，且没有触发首次 fetch。
            assert capture.context is not None and not capture.buffer.entries
            page = capture.context.pages[0]

            async def navigate(**kwargs):
                await page.goto(start_url)
                return ActionResult(extracted_content="navigated")

            async def click(**kwargs):
                await page.locator("#next").click()
                return ActionResult(extracted_content="clicked")

            self.tools.registry.registry.actions["navigate"].function = navigate
            self.tools.registry.registry.actions["click"].function = click
            actions = self.tools.registry.create_action_model()

            async def act(data):
                return await self.tools.act(actions.model_validate(data), browser_session=self.browser,
                                            file_system=SimpleNamespace())

            await act({"navigate": {"url": start_url}})
            await page.wait_for_function("document.querySelector('main').textContent === 'Book 1'")
            # 首次加载已经被保留，第一次检查工具不负责启动监听。
            listed = json.loads((await act({"list_network_responses": {}})).extracted_content)
            assert len(listed["responses"]) == 1
            response_id = listed["responses"][0]["id"]
            inspected = json.loads((await act({"inspect_network_response": {"response_id": response_id}})).extracted_content)
            assert inspected["complete"] and "sample" in inspected
            assert inspected["sample"]["data"]["items"]["items"][0]["stock"] == 41
            observations["initial"] = inspected
            params = {"source": spec_data["network"], "dedupe_by": ["id"]}
            initial_test = json.loads((await act({"test_network": params})).extracted_content)
            assert initial_test["extracted"] == initial_test["added"] == 1
            assert not initial_test["pagination_verified"]
            # 没有推进证据不能提交，即使初次响应字段都可以提取。
            rejected = await act({"done": {"data": spec_data}})
            assert not rejected.is_done and 'candidate_rejected' in rejected.extracted_content
            if pagination == "scroll":
                explored = await act({"explore_scroll": {
                    "network": spec_data["network"], "dedupe_by": ["id"],
                    "max_scrolls": 6, "wait_ms": 700,
                }})
                assert not explored.error, explored.error
                feedback = json.loads(explored.extracted_content)
                assert feedback["count"] == 2 and feedback["added"] == 1, feedback
                assert feedback["stop_reason"] == "end_marker", feedback
            else:
                await act({"click": {"index": 1}})
            await page.wait_for_function("document.querySelector('main').textContent === 'Book 2'")
            tested = json.loads((await act({"test_network": params})).extracted_content)
            assert tested["status"] == "passed" and tested["responses"] == 2, (tested, [(e.id, e.page_url) for e in capture.buffer.entries.values()], capture.actions)
            done = await act({"done": {"data": spec_data}})
            assert done.is_done and done.success, done.extracted_content
            return SimpleNamespace(is_successful=lambda: True, final_result=lambda: done.extracted_content)

    monkeypatch.setattr(bu, "Agent", Agent)
    settings = AgentSettings(api_key="fake", model="test", base_url="https://llm.test", content_timeout_ms=1000)
    outcome = run_task(start_url, "抓取标题和 DOM 不可见的库存、价格及启用状态", settings,
                       fields=["id", "title", "stock", "price", "active"], count=10, execute=True,
                       validation=ValidationSettings(max_repairs=0))
    assert outcome.status == "success", outcome.model_dump()
    assert len(outcome.data.records) == 2 and not outcome.sample_only
    assert observations["initial"]["url"] == local_site+"api/items"
    # 保存后从新浏览器独立重跑，证明规则不依赖探索缓存或模型。
    rule_file = tmp_path / "rule.json"
    rule_file.write_text(outcome.spec.model_dump_json())
    restored = ScraperSpec.model_validate_json(rule_file.read_text())
    result = ScraperExecutor().run(restored)
    assert [row["stock"] for row in result.records] == [41, 42]
    export_records(result.records, tmp_path / "data.json", list(restored.all_fields))
    export_records(result.records, tmp_path / "data.csv", list(restored.all_fields))
    assert json.loads((tmp_path / "data.json").read_text())[0]["active"] is False
    assert "stock" in (tmp_path / "data.csv").read_text()


def test_bounded_redacted_samples_and_trace():
    sample = sample_output({"token": ["private"], "authorization": "Bearer private",
                            "url": "https://example.test/api?custom_secret=private",
                            "data": list(range(100)), "huge": "x"*10000})
    encoded = json.dumps(sample)
    assert "private" not in encoded and len(encoded) < 8000
    assert sample["data"]["length"] == 100 and len(sample["data"]["items"]) == 2
    assert "sample" not in summarize_tool_output(json.dumps({"network": True, "sample": sample}))


# 重复响应与新响应分开计量；去重键必须和最终规则一致，改路径后旧证据失效。
@pytest.mark.parametrize("kind", ["click", "scroll"])
def test_network_evidence_uses_exact_paths_and_dedupe_keys(kind):
    from autoscraper.agent.tools import build_tools
    capture = AgentResponseCapture()
    fields = {"id": {"path": ["id"]}, "stock": {"path": ["stock"]}}
    source = dict(url_pattern="https://fixture.test/api", records_path=[], fields=fields,
                  pagination=dict(type="click", next_selector="button") if kind == "click" else dict(type="scroll"))
    for number in (1, 2):
        capture.buffer.entries[number] = CapturedResponse(number, "https://fixture.test/api", "GET",
            page_url="https://fixture.test/", body=[dict(id=1, stock=number)], complete=True, status=200)
    capture.actions = [(kind, 1)]
    tools = build_tools(network_capture=capture, check_submission=True, listing_url="https://fixture.test/")
    action = tools.registry.registry.actions["test_network"].function
    async def run():
        repeated = json.loads((await action(source=source, dedupe_by=["id"])).extracted_content)
        assert repeated["responses"] == repeated["extracted"] == 2 and repeated["added"] == 1
        assert repeated["status"] == "failed"
        new = json.loads((await action(source=source)).extracted_content)
        assert new["added"] == 2 and new["status"] == "passed"
        source["fields"]["stock"]["path"] = ["missing"]
        invalid = json.loads((await action(source=source)).extracted_content)
        assert invalid["status"] == "failed"
    asyncio.run(run())


# 探索器也要区分到底、加载和在途正文；不能把尚未完成的响应算作滚动证据。
@pytest.mark.parametrize('mode,reason', [('pending', 'response_timeout'), ('loading', 'content_timeout'),
                                       ('quiet', 'no_more_content'), ('budget', 'scroll_limit')])
def test_network_explore_scroll_wait_and_budget_boundaries(mode, reason):
    from autoscraper.agent.tools import build_tools
    capture = AgentResponseCapture()
    capture.buffer.sequence = 1
    capture.buffer.entries[1] = CapturedResponse(1, 'https://fixture.test/api', 'GET',
        page_url='https://fixture.test/', body=[dict(id=1)], complete=True, status=200)
    source = dict(url_pattern='https://fixture.test/api', records_path=[],
                  fields=dict(id=dict(path=['id'])), pagination=dict(type='scroll',
                  max_scrolls=4, no_change_limit=2, container_selector='#scroller'))

    class Page:
        scrolled = False

        async def evaluate(self, script, options):
            from autoscraper.scraping.progress import SCROLL_SCRIPT
            if script == SCROLL_SCRIPT:
                self.scrolled = True
                if mode == 'pending':
                    capture.buffer.sequence = 2
                    capture.buffer.entries[2] = CapturedResponse(2, 'https://fixture.test/api', 'GET', status=200)
                return ''
            return json.dumps(dict(bottom=True, loading=self.scrolled and mode == 'loading', end=False))

    page = Page()
    async def current_page():
        return page

    tools = build_tools(network_capture=capture, check_submission=True,
                        listing_url='https://fixture.test/', content_timeout_ms=500)
    action = tools.registry.registry.actions['explore_scroll'].function
    result = asyncio.run(action(browser_session=SimpleNamespace(must_get_current_page=current_page),
        network=source, dedupe_by=['id'], max_scrolls=1 if mode == 'budget' else 4, wait_ms=30))
    assert not result.error
    data = json.loads(result.extracted_content)
    assert data['stop_reason'] == reason, data
    assert data.get('count', 1) == 1
    assert not capture.evidence
