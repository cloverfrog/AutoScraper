# 浏览器运行配置回归：检查执行器实际启动参数，以及独立 CLI 的选项和环境变量优先级。
# 有窗口模式只验证参数传递，不在测试时弹出真实浏览器。
import json
import sys
from contextlib import nullcontext
from types import SimpleNamespace

import pytest

from autoscraper.scraping.executor import ScrapeResult, ScraperExecutor
from autoscraper.scraping.spec import ScraperSpec


# 最小抓取规则不包含显示模式或机器上的浏览器路径。
@pytest.fixture
def spec():
    return ScraperSpec(
        start_url="https://fixture.test/", item_selector="article",
        fields={"title": {"selector": "h2"}}, max_items=1,
    )


# 直接检查启动边界，防止执行器继续把 headless 写死或丢掉可执行文件路径。
@pytest.mark.parametrize("headless", [True, False])
@pytest.mark.parametrize("path", [None, "C:/Custom Browser/chrome.exe"])
def test_executor_uses_configured_browser(monkeypatch, spec, headless, path):
    import autoscraper.scraping.executor as executor

    launches = []
    closed = []
    browser = SimpleNamespace(
        new_context=lambda: SimpleNamespace(new_page=lambda: object()),
        close=lambda: closed.append(True),
    )

    def launch(**kwargs):
        launches.append(kwargs)
        return browser

    monkeypatch.setattr(
        executor, "sync_playwright",
        lambda: nullcontext(SimpleNamespace(chromium=SimpleNamespace(launch=launch))),
    )
    expected = ScrapeResult(stop_reason="no_pagination")
    monkeypatch.setattr(ScraperExecutor, "run_page", lambda *args: expected)
    assert ScraperExecutor(headless=headless, executable_path=path).run(spec) is expected
    assert launches == [{"headless": headless, "executable_path": path}]
    assert closed == [True]


# CLI 显式路径优先于环境变量；不传 --headed 时所有路径组合都应无头运行。
@pytest.mark.parametrize(
    "flags, environment_path, expected_headless, expected_path",
    [
        ([], None, True, None),
        (["--headed"], None, False, None),
        (["--browser-path", "cli/chrome.exe"], None, True, "cli/chrome.exe"),
        ([], "env/chrome.exe", True, "env/chrome.exe"),
        (["--headed"], "env/chrome.exe", False, "env/chrome.exe"),
        (["--headed", "--browser-path", "cli/chrome.exe"], "env/chrome.exe", False,
         "cli/chrome.exe"),
    ],
)
def test_scraper_cli_browser_settings(
    monkeypatch, tmp_path, capsys, spec, flags, environment_path, expected_headless, expected_path
):
    from autoscraper.cli.scraper import main

    if environment_path is None:
        monkeypatch.delenv("AUTOSCRAPER_BROWSER_PATH", raising=False)
    else:
        monkeypatch.setenv("AUTOSCRAPER_BROWSER_PATH", environment_path)
    rule = tmp_path / "rule.json"
    rule.write_text(spec.model_dump_json(), encoding="utf-8")
    output = tmp_path / "data.json"
    seen = []

    def run(self, rule):
        seen.append((self.headless, self.executable_path))
        return ScrapeResult(records=[{"title": "A"}], stop_reason="max_items")

    monkeypatch.setattr(ScraperExecutor, "run", run)
    monkeypatch.setattr(
        sys, "argv", ["autoscraper", str(rule), "--output", str(output), *flags]
    )
    assert main() == 0
    assert seen == [(expected_headless, expected_path)]
    assert json.loads(output.read_text(encoding="utf-8")) == [{"title": "A"}]
    assert json.loads(capsys.readouterr().out)["stop_reason"] == "max_items"
