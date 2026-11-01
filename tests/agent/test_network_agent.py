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


def test_agent_tools_generate_validated_reusable_network_rule(monkeypatch, local_site, explicit_unfiltered_requirements, tmp_path):
    bu = pytest.importorskip("browser_use")
    from browser_use import ActionResult
    import autoscraper.agent.llm as models
    from autoscraper.scraping.export import export_records

    model = SimpleNamespace(calls=0)
    monkeypatch.setattr(models, "make_llm", lambda settings: model)
    monkeypatch.setattr(models, "make_wait_llm", lambda settings: SimpleNamespace(calls=0, max_calls=0))
    spec_data = json.loads(Path("examples/books.network.json").read_text())
    spec_data.update(start_url=local_site, max_items=10)
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
                await page.goto(local_site)
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

            await act({"navigate": {"url": local_site}})
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
            await act({"click": {"index": 1}})
            await page.wait_for_function("document.querySelector('main').textContent === 'Book 2'")
            tested = json.loads((await act({"test_network": params})).extracted_content)
            assert tested["status"] == "passed" and tested["responses"] == 2, (tested, [(e.id, e.page_url) for e in capture.buffer.entries.values()], capture.actions)
            done = await act({"done": {"data": spec_data}})
            assert done.is_done and done.success, done.extracted_content
            return SimpleNamespace(is_successful=lambda: True, final_result=lambda: done.extracted_content)

    monkeypatch.setattr(bu, "Agent", Agent)
    settings = AgentSettings(api_key="fake", model="test", base_url="https://llm.test", content_timeout_ms=1000)
    outcome = run_task(local_site, "抓取标题和 DOM 不可见的库存、价格及启用状态", settings,
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
def test_network_evidence_uses_exact_paths_and_dedupe_keys():
    from autoscraper.agent.tools import build_tools
    capture = AgentResponseCapture()
    fields = {"id": {"path": ["id"]}, "stock": {"path": ["stock"]}}
    source = dict(url_pattern="https://fixture.test/api", records_path=[], fields=fields,
                  pagination=dict(type="click", next_selector="button"))
    for number in (1, 2):
        capture.buffer.entries[number] = CapturedResponse(number, "https://fixture.test/api", "GET",
            page_url="https://fixture.test/", body=[dict(id=1, stock=number)], complete=True, status=200)
    capture.actions = [("click", 1)]
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
