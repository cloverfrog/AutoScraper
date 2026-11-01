# 可选 Gradio 集成：检查真实组件回调、人工交互更新，以及受控下载边界。
import socket
import threading
from pathlib import Path

import pytest

pytest.importorskip("gradio")

from autoscraper.agent.clarification import ClarificationRequest
from autoscraper.ui.app import build_app
from autoscraper.ui.runtime import Job, JobManager, RunConfig


# 按实际绑定的函数寻找回调，避免依赖 Gradio 的内部组件编号。
def callback(app, name):
    return next(fn.fn for fn in app.fns.values() if getattr(fn.fn, "__name__", None) == name)


# 验证问题和说明能输出到网页，选项/自由回答能经真实 UI 回调返回工作线程。
def test_app_clarification_round_trip(tmp_path):
    manager = JobManager(tmp_path)
    job = Job(RunConfig())
    manager.jobs[job.id] = job
    app = build_app(manager)
    result = []
    request = ClarificationRequest(question="抓哪个？", explanation="两个列表", options=["A", "B"])
    thread = threading.Thread(target=lambda: result.append(job.ask("clarification", request)))
    thread.start()
    with job.condition:
        # 等待线程发布请求；Condition 在 ask 等待时释放锁。
        job.condition.wait_for(lambda: job.pending is not None, timeout=2)
    pending = job.snapshot()["pending"]
    state = callback(app, "poll")(job.id, "")
    assert state[0] == "needs_clarification"
    assert state[6] == "抓哪个？\n\n两个列表"
    assert state[7].choices == [("A", "A"), ("B", "B")]
    assert state[9] == pending["id"]
    # 轮询同一个问题不会重置控件，用户可安心填写答案。
    unchanged = callback(app, "poll")(job.id, pending["id"])
    assert unchanged[7] == {"__type__": "update"}
    callback(app, "respond")(job.id, pending["id"], "A", "自由回答")
    thread.join(2)
    assert result == ["自由回答"]
    assert not thread.is_alive()


# 从实际控件默认值调用启动回调，保证表单字段和配置顺序一致，并保留零预算。
@pytest.mark.parametrize("optional_value", [None, 0, 1234])
def test_app_start_defaults_and_zero_wait_budget(tmp_path, monkeypatch, optional_value):
    manager = JobManager(tmp_path)
    configs = []
    monkeypatch.setattr(manager, "start", lambda config: configs.append(config) or "job-id")
    app = build_app(manager)
    start_fn = next(
        fn for fn in app.fns.values() if getattr(fn.fn, "__name__", None) == "start_agent"
    )
    values = [component.value for component in start_fn.inputs]
    index = next(i for i, c in enumerate(start_fn.inputs) if c.label == "内容等待模型调用预算")
    values[index] = 0
    optional_labels = ("每次抓取上限", "验证超时", "内容等待超时", "规则导航超时")
    for i, component in enumerate(start_fn.inputs):
        if component.label.startswith(optional_labels):
            values[i] = optional_value
        if component.label == "最大修复次数":
            values[i] = 0
    assert start_fn.fn(*values) == ("job-id", "任务已启动")
    assert configs[0].agent_options["max_wait_llm_calls"] == 0
    assert configs[0].agent_options["max_steps"] == 12
    assert configs[0].agent_options["max_llm_calls"] == 24
    assert configs[0].agent_options["repair_reserve_calls"] == 6
    assert configs[0].validation_options["max_items"] == 100
    expected = optional_value or None
    assert configs[0].content_timeout_ms == expected
    assert configs[0].navigation_timeout_ms == expected
    assert configs[0].count == expected
    assert configs[0].validation_options.get("timeout_ms") == expected
    assert configs[0].validation_options["max_repairs"] == 0


# 实际 HTTP 文件路由仅开放产物，认证和 Trace 即使知道路径也不能下载。
def test_file_routes_block_credentials(tmp_path):
    import httpx

    manager = JobManager(tmp_path)
    artifact = tmp_path / "artifacts" / "rule.json"
    artifact.write_text("{}")
    private = [tmp_path / "auth" / "demo.json", tmp_path / "traces" / "events.json"]
    for path in private:
        path.write_text("private")
    app = build_app(manager)
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    try:
        app.launch(
            server_name="127.0.0.1", server_port=port, share=False, prevent_thread_lock=True,
            quiet=True, allowed_paths=[str(tmp_path / "artifacts")],
            blocked_paths=[str(tmp_path / "auth"), str(tmp_path / "traces")],
        )
        with httpx.Client(base_url=f"http://127.0.0.1:{port}", trust_env=False) as client:
            assert client.get(f"/gradio_api/file={artifact}").status_code == 200
            for path in private:
                assert client.get(f"/gradio_api/file={path}").status_code == 403
    finally:
        app.close()


# 真正浏览器点击页面：澄清回答、人工接管、日志刷新均在同一次后台任务中完成。
def test_browser_interaction_and_live_logs(tmp_path, monkeypatch):
    import logging
    import re

    from playwright.sync_api import expect, sync_playwright

    from autoscraper.browser.access import HandoffRequest

    manager = JobManager(tmp_path)
    answers = []
    logger = logging.getLogger("browser_use.Agent")
    monkeypatch.setattr(logger, "level", logging.INFO)

    def execute(job, access, output, trace):
        logger.info("Step 3: Memory: inspect inventory")
        answers.append(job.ask("clarification", ClarificationRequest(
            question="请选择栏目", explanation="页面包含两个列表", options=["书籍", "电影"],
        )))
        access.pending = HandoffRequest(reason="请在浏览器完成登录")
        access.resume()
        logger.info("Step 4: test_fields completed")
        job.summary = {"status": "success", "answer": answers[0]}

    monkeypatch.setattr(manager, "_execute", execute)
    app = build_app(manager)
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    try:
        app.launch(server_name="127.0.0.1", server_port=port, share=False,
                   prevent_thread_lock=True, quiet=True)
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch()
            page = browser.new_page(viewport={"width": 1280, "height": 1000})
            page.goto(f"http://127.0.0.1:{port}")
            page.get_by_role("tab", name="执行已有规则", exact=True).click()
            page.get_by_label("显示浏览器，允许人工接管", exact=True).check()
            page.get_by_role("button", name="执行规则", exact=True).click()
            expect(page.get_by_label("需要你处理", exact=True)).to_have_value(
                "请选择栏目\n\n页面包含两个列表", timeout=15000,
            )
            expect(page.get_by_label("实时运行日志", exact=True)).to_have_value(
                re.compile("Step 3: Memory"), timeout=10000,
            )
            page.get_by_label("书籍", exact=True).check()
            page.get_by_role("button", name="提交回答 / 继续", exact=True).click()
            expect(page.get_by_label("需要你处理", exact=True)).to_have_value(
                "请在浏览器完成登录\n\n", timeout=10000,
            )
            page.get_by_role("button", name="提交回答 / 继续", exact=True).click()
            expect(page.get_by_label("任务状态", exact=True)).to_have_value(
                "success（任务已结束）", timeout=10000,
            )
            expect(page.get_by_label("实时运行日志", exact=True)).to_have_value(
                re.compile("Step 4: test_fields completed"), timeout=10000,
            )
            page.screenshot(path="/tmp/autoscraper-issue23-ui.png", full_page=True)
            browser.close()
        assert answers == ["书籍"]
    finally:
        app.close()


# 上传规则后，通过真实执行器抓取本地 HTTP 页面，验证 UI 到数据导出的完整链路。
def test_browser_upload_and_real_scrape(tmp_path):
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    import json

    from playwright.sync_api import expect, sync_playwright

    class FixturePage(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write('<article><h1>示例商品</h1></article>'.encode())

        def log_message(self, *args):
            pass

    fixture = ThreadingHTTPServer(("127.0.0.1", 0), FixturePage)
    fixture_thread = threading.Thread(target=fixture.serve_forever, daemon=True)
    fixture_thread.start()
    spec = json.dumps({
        "start_url": f"http://127.0.0.1:{fixture.server_port}/",
        "item_selector": "article", "fields": {"title": {"selector": "h1"}},
    })
    manager = JobManager(tmp_path)
    app = build_app(manager)
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    try:
        app.launch(
            server_name="127.0.0.1", server_port=port, share=False,
            prevent_thread_lock=True, quiet=True, allowed_paths=[str(tmp_path / "artifacts")],
            blocked_paths=[str(tmp_path / "auth"), str(tmp_path / "traces")],
        )
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch()
            page = browser.new_page()
            page.goto(f"http://127.0.0.1:{port}")
            page.get_by_role("tab", name="执行已有规则", exact=True).click()
            page.locator('input[type="file"]').set_input_files({
                "name": "rule.json", "mimeType": "application/json", "buffer": spec.encode(),
            })
            expect(page.get_by_label("规则 JSON（可直接粘贴或编辑）", exact=True)).to_have_value(
                spec, timeout=10000,
            )
            page.get_by_role("button", name="执行规则", exact=True).click()
            expect(page.get_by_label("任务状态", exact=True)).to_have_value(
                "success（任务已结束）", timeout=20000,
            )
            expect(page.get_by_role("cell", name="data.json", exact=True)).to_be_visible(timeout=10000)
            expect(page.get_by_role("cell", name="data.csv", exact=True)).to_be_visible(timeout=10000)
            preview = page.get_by_text("示例商品", exact=False)
            expect(preview.first).to_be_visible()
            with page.expect_download() as download:
                page.locator('a[href$="/data.json"]').click()
            assert json.loads(Path(download.value.path()).read_text(encoding="utf-8")) == [{"title": "示例商品"}]
            browser.close()
    finally:
        app.close()
        fixture.shutdown()
        fixture.server_close()
        fixture_thread.join(2)


# 从真实网页提交空值、零及正数，覆盖组件序列化后到 AgentSettings 的配置链路。
def test_browser_optional_numbers_use_defaults(tmp_path, monkeypatch):
    import re
    import time

    from playwright.sync_api import expect, sync_playwright

    from autoscraper.cli.agent import settings_from_env
    from autoscraper.workflow.validation import ValidationSettings

    monkeypatch.setenv("AUTOSCRAPER_API_KEY", "test-only-key")
    manager = JobManager(tmp_path)
    configs = []

    def start(config):
        # 直接使用实际模型校验，不启动浏览器探索或产生模型费用。
        settings = settings_from_env(content_timeout_ms=config.content_timeout_ms)
        validation = ValidationSettings(**config.validation_options)
        configs.append((config, settings, validation))
        return f"job-{len(configs)}"

    monkeypatch.setattr(manager, "start", start)
    app = build_app(manager)
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    try:
        app.launch(server_name="127.0.0.1", server_port=port, share=False,
                   prevent_thread_lock=True, quiet=True)
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch()
            page = browser.new_page()
            page.goto(f"http://127.0.0.1:{port}")
            page.get_by_role("button", name=re.compile("高级设置")).click()
            labels = ("每次抓取上限", "验证超时", "内容等待超时", "规则导航超时")
            for value in (None, 0, 1234):
                if value is not None:
                    for label in labels:
                        page.get_by_label(re.compile("^" + label)).fill(str(value))
                page.get_by_role("button", name="生成规则", exact=True).click()
                expect(page.get_by_label("任务状态", exact=True)).to_have_value("任务已启动")
                # 页面状态可能与上一轮相同，通过任务 ID 确认本次提交已完成。
                deadline = time.monotonic() + 5
                expected_count = (None, 0, 1234).index(value) + 1
                while len(configs) < expected_count and time.monotonic() < deadline:
                    page.wait_for_timeout(20)
                assert len(configs) == expected_count
                config, settings, validation = configs[-1]
                expected = value or None
                assert config.count == expected
                assert config.content_timeout_ms == expected
                assert config.navigation_timeout_ms == expected
                assert validation.timeout_ms == expected
                assert settings.content_timeout_ms == (expected or 30000)
            browser.close()
    finally:
        app.close()


# 上传参考规则和直接粘贴共用同一文本框，生成入口与执行入口保留独立协议。
def test_agent_reference_upload_and_paste(tmp_path, monkeypatch):
    manager = JobManager(tmp_path)
    configs = []
    monkeypatch.setattr(manager, "start", lambda config: configs.append(config) or "job-id")
    app = build_app(manager)
    start_fn = next(
        fn for fn in app.fns.values() if getattr(fn.fn, "__name__", None) == "start_agent"
    )
    reference = '{"start_url": "https://example.com"}'
    upload = next(
        fn for fn in app.fns.values()
        if getattr(fn.fn, "__name__", None) == "load_rule"
        and fn.inputs[0].label == "上传参考 ScraperSpec JSON"
    )
    loaded = upload.fn(reference.encode("utf-8-sig"))
    assert loaded == reference
    assert upload.outputs[0].label == "参考规则 JSON（可粘贴或编辑）"
    for value in (loaded, ""):
        values = [component.value for component in start_fn.inputs]
        index = next(i for i, c in enumerate(start_fn.inputs) if c is upload.outputs[0])
        values[index] = value
        start_fn.fn(*values)
        assert configs[-1].initial_spec_json == value
        assert configs[-1].spec_json == ""
