# 开发辅助功能通过真实页面和重载进程验证，避免只检查回调返回值。
import json
import os
import shutil
import signal
import socket
import subprocess
import sys
import time
from pathlib import Path

import pytest

pytest.importorskip("gradio")

from autoscraper.ui import app as ui_app
from autoscraper.ui.runtime import JobManager


# 让操作系统分配空闲端口，避免与本机正在运行的 UI 冲突。
def unused_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


# 普通启动保留原端口和运行目录参数，并允许覆盖监听地址。
@pytest.mark.parametrize("host_args, expected_host", [([], "127.0.0.1"),
                                                     (["--host", "100.64.0.1"], "100.64.0.1")])
def test_launch_arguments(monkeypatch, host_args, expected_host):
    calls = []
    monkeypatch.setattr(sys, "argv", ["autoscraper-ui", *host_args, "--port", "7861",
                                     "--runtime-dir", "custom-runtime"])
    monkeypatch.setattr(ui_app, "serve", lambda *args, **kwargs: calls.append((args, kwargs)))
    assert ui_app.main() == 0
    assert calls == [((expected_host, 7861, Path("custom-runtime")), {"inbrowser": True})]


# 重载参数直接交给函数子进程，Windows 的空格、反斜杠路径不经过 shell 转义。
def test_reload_launch_arguments(monkeypatch):
    import watchfiles

    runtime = r"C:\Users\开发 用户\AutoScraper\运行目录"
    calls = []
    monkeypatch.setattr(sys, "argv", ["autoscraper-ui", "--reload", "--host", "100.64.0.1",
                                     "--port", "7861", "--runtime-dir", runtime])
    monkeypatch.setattr(watchfiles, "run_process", lambda *args, **kw: calls.append((args, kw)))
    assert ui_app.main() == 0
    paths, options = calls[0]
    assert paths == (Path(ui_app.__file__).resolve().parents[1],)
    assert options["target"] is ui_app.serve
    assert options["args"] == ("100.64.0.1", 7861, Path(runtime))
    assert options["kwargs"] == {"inbrowser": False}


# 服务启动后及页面打开后修改真实 JSON，列表和内容都应无需重启即可更新。
def test_browser_presets_reload_from_file(tmp_path, monkeypatch):
    from playwright.sync_api import expect, sync_playwright

    monkeypatch.setattr(ui_app, "__file__", str(tmp_path / "src/autoscraper/ui/app.py"))
    presets_path = tmp_path / "examples/ui_tasks.json"
    presets_path.parent.mkdir()

    def write_presets(tasks):
        presets_path.write_text(json.dumps(tasks, ensure_ascii=False), encoding="utf-8")

    write_presets([{"name": "旧预设", "task": "旧任务"}])
    app = ui_app.build_app(JobManager(tmp_path / "runtime"))
    port = unused_port()
    try:
        app.launch(server_name="127.0.0.1", server_port=port, share=False,
                   prevent_thread_lock=True, quiet=True)
        # 页面尚未打开时修改，验证不是在构建应用时固定列表。
        write_presets([{"name": "启动后新增", "task": "新任务"}])
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch()
            page = browser.new_page()
            page.goto(f"http://127.0.0.1:{port}")
            page.wait_for_timeout(500)
            dropdown = page.get_by_label("测试任务预设", exact=True)
            dropdown.click()
            page.get_by_role("option", name="启动后新增", exact=True).click()
            expect(page.get_by_label("抓取任务", exact=True)).to_have_value("新任务")

            write_presets([
                {"name": "启动后新增", "task": "已修改的任务"},
                {"name": "页面打开后新增", "task": "新增任务"},
            ])
            page.get_by_label("抓取任务", exact=True).click()
            dropdown.click()
            page.get_by_role("option", name="页面打开后新增", exact=True).click()
            expect(page.get_by_label("抓取任务", exact=True)).to_have_value("新增任务")
            page.get_by_label("抓取任务", exact=True).click()
            dropdown.click()
            page.get_by_role("option", name="启动后新增", exact=True).click()
            expect(page.get_by_label("抓取任务", exact=True)).to_have_value("已修改的任务")

            write_presets([{"name": "刷新后的预设", "task": "刷新后的任务"}])
            page.reload()
            dropdown.click()
            page.get_by_role("option", name="刷新后的预设", exact=True).click()
            expect(page.get_by_label("抓取任务", exact=True)).to_have_value("刷新后的任务")
            browser.close()
    finally:
        app.close()


# 修改、预设、刷新和重置必须在真实浏览器中联动，包括零值及规则模式的表单。
def test_browser_form_memory_and_presets(tmp_path, monkeypatch):
    from playwright.sync_api import expect, sync_playwright

    monkeypatch.setattr(ui_app, "load_presets", lambda: {
        "测试用例": {"url": "https://example.com/books", "task": "列出图书", "count": 5},
    })
    app = ui_app.build_app(JobManager(tmp_path))
    port = unused_port()
    try:
        app.launch(server_name="127.0.0.1", server_port=port, share=False,
                   prevent_thread_lock=True, quiet=True)
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch()
            context = browser.new_context()
            page = context.new_page()
            page.goto(f"http://127.0.0.1:{port}")
            # Gradio 的组件初始化会在页面挂载后继续更新，等首轮渲染完成再填写。
            page.wait_for_timeout(500)
            page.get_by_label("目标 URL", exact=True).fill("https://example.com/manual")
            page.get_by_label("抓取任务", exact=True).fill("手动任务")
            page.get_by_text("高级设置", exact=True).click()
            page.wait_for_timeout(300)
            page.get_by_label("模型（留空使用环境配置）", exact=True).fill("dev-model")
            page.get_by_label("内容等待模型调用预算", exact=True).fill("0")
            page.get_by_role("tab", name="执行已有规则", exact=True).click()
            page.wait_for_timeout(300)
            page.get_by_label("规则 JSON（可直接粘贴或编辑）", exact=True).fill('{"test": true}')
            page.wait_for_function("""() => {
                const data = JSON.parse(localStorage.getItem('autoscraper.ui.form'));
                return data?.model === 'dev-model' && data?.max_wait_llm_calls === 0
                    && data?.spec_json === '{"test": true}';
            }""")
            page.reload()
            expect(page.get_by_label("目标 URL", exact=True)).to_have_value(
                "https://example.com/manual",
            )
            page.get_by_role("tab", name="执行已有规则", exact=True).click()
            expect(page.get_by_label("规则 JSON（可直接粘贴或编辑）", exact=True)).to_have_value(
                '{"test": true}',
            )
            page.get_by_role("tab", name="Agent 生成规则", exact=True).click()
            page.get_by_label("测试任务预设", exact=True).click()
            page.get_by_role("option", name="测试用例", exact=True).click()
            expect(page.get_by_label("目标 URL", exact=True)).to_have_value(
                "https://example.com/books",
            )
            expect(page.get_by_label("抓取任务", exact=True)).to_have_value("列出图书")
            page.get_by_label("抓取任务", exact=True).fill("修改预设任务")
            page.wait_for_function("""() => {
                const data = JSON.parse(localStorage.getItem('autoscraper.ui.form'));
                return data?.task === '修改预设任务' && data?.count === 5;
            }""")
            # 同一浏览器新标签页恢复最终表单；其他设置不被预设覆盖。
            other = context.new_page()
            other.goto(f"http://127.0.0.1:{port}")
            expect(other.get_by_label("抓取任务", exact=True)).to_have_value("修改预设任务")
            other.get_by_text("高级设置", exact=True).click()
            expect(other.get_by_label("模型（留空使用环境配置）", exact=True)).to_have_value(
                "dev-model",
            )
            expect(other.get_by_label("内容等待模型调用预算", exact=True)).to_have_value("0")
            other.get_by_role("button", name="恢复默认设置", exact=True).click()
            expect(other.get_by_label("目标 URL", exact=True)).to_have_value("")
            other.wait_for_function("""() => {
                const data = JSON.parse(localStorage.getItem('autoscraper.ui.form'));
                return !data || (!data.url && data.max_wait_llm_calls === 8);
            }""")
            other.reload()
            expect(other.get_by_label("抓取任务", exact=True)).to_have_value("")
            other.get_by_text("高级设置", exact=True).click()
            expect(other.get_by_label("内容等待模型调用预算", exact=True)).to_have_value("8")
            browser.close()
    finally:
        app.close()


# 使用临时源码启动真正的监听父进程，检查 UI/核心修改重启、非源码忽略和 Ctrl+C。
def test_reload_service(tmp_path):
    import httpx

    pytest.importorskip("watchfiles")
    source = tmp_path / "src" / "autoscraper"
    shutil.copytree(Path(ui_app.__file__).resolve().parents[1], source,
                    ignore=shutil.ignore_patterns("__pycache__"))
    port = unused_port()
    env = {**os.environ, "PYTHONPATH": str(source.parent), "GRADIO_ANALYTICS_ENABLED": "False"}
    with (tmp_path / "service.log").open("w+") as log:
        process = subprocess.Popen(
            [sys.executable, "-m", "autoscraper.ui.app", "--host", "127.0.0.1",
             "--port", str(port), "--runtime-dir", str(tmp_path / "runtime with spaces"),
             "--reload"],
            cwd=tmp_path, env=env, stdout=log, stderr=subprocess.STDOUT,
        )
        # 每次完整重启会生成新的 Gradio app_id，可验证仍在同一地址监听。
        def wait_for_app(previous=None):
            deadline = time.monotonic() + 30
            while time.monotonic() < deadline:
                try:
                    config = httpx.get(f"http://127.0.0.1:{port}/config", timeout=1,
                                       trust_env=False).json()
                    if config["app_id"] != previous:
                        return config["app_id"]
                except (httpx.HTTPError, ValueError, KeyError):
                    pass
                time.sleep(0.2)
            log.seek(0)
            pytest.fail(f"UI 未恢复监听：\n{log.read()}")

        try:
            first = wait_for_app()
            (source / "ui" / "app.py").touch()
            second = wait_for_app(first)
            (source / "scraping" / "executor.py").touch()
            third = wait_for_app(second)
            (source / "ignored.json").write_text(json.dumps({"test": True}))
            time.sleep(2)
            assert wait_for_app() == third
            assert (tmp_path / "runtime with spaces" / "artifacts").is_dir()
            process.send_signal(signal.SIGINT)
            assert process.wait(timeout=15) == 0
            with socket.socket() as sock:
                sock.bind(("127.0.0.1", port))
        finally:
            if process.poll() is None:
                process.send_signal(signal.SIGINT)
                process.wait(timeout=15)
