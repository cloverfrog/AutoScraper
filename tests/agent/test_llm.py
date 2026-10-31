# 模型模块边界与预算回归：不访问 API，验证拆分后不会重新引入浏览器工具依赖。
# 主模型失败仍消耗其预算，语义等待模型的调用次数保持独立。
import asyncio
import os
import subprocess
import sys

import pytest


# 用新进程检查实际导入链，避免其他测试提前加载模块掩盖反向依赖。
@pytest.mark.parametrize("component", ["llm", "waiting", "agent_errors", "scraping_errors"])
def test_model_and_waiting_modules_do_not_load_tools(component):
    if component == "llm":
        pytest.importorskip("browser_use")
    script = """
import importlib
import sys
from types import SimpleNamespace
from pydantic import SecretStr

component = sys.argv[1]
module_name = {
    'agent_errors': 'autoscraper.agent.errors',
    'scraping_errors': 'autoscraper.scraping.errors',
}.get(component, 'autoscraper.agent.' + component)
module = importlib.import_module(module_name)
if component == 'llm':
    settings = SimpleNamespace(
        model='fixture', api_key=SecretStr('fake'), base_url='https://llm.test',
        llm_timeout_seconds=45, max_llm_calls=2, max_output_tokens=3000, max_wait_llm_calls=1,
    )
    module.make_llm(settings)
    module.make_wait_llm(settings)
    assert 'autoscraper.agent.waiting' not in sys.modules
    assert 'autoscraper.agent.dom' not in sys.modules
elif component == 'waiting':
    module.ContentWaiter()
    assert 'autoscraper.agent.llm' not in sys.modules
    assert 'autoscraper.agent.responses' not in sys.modules
    assert 'browser_use' not in sys.modules
if component.endswith('_errors'):
    assert 'browser_use' not in sys.modules
    assert 'playwright' not in sys.modules
    assert 'autoscraper.agent.explorer' not in sys.modules
    assert 'autoscraper.agent.responses' not in sys.modules
    assert 'autoscraper.scraping.executor' not in sys.modules
    assert 'autoscraper.workflow.runner' not in sys.modules
assert 'autoscraper.agent.tools' not in sys.modules
"""
    result = subprocess.run(
        [sys.executable, "-c", script, component],
        capture_output=True,
        text=True,
        encoding="utf-8",
        env={
            **os.environ,
            "ANONYMIZED_TELEMETRY": "false",
            "BROWSER_USE_CLOUD_SYNC": "false",
            "PYTHONUTF8": "1",
        },
        timeout=30,
    )
    assert result.returncode == 0, result.stderr


# 模拟供应商失败，确认失败请求不能绕过调用上限，也不扣减另一模型的预算。
def test_failed_main_calls_exhaust_only_main_budget(monkeypatch):
    pytest.importorskip("browser_use")
    from autoscraper.agent.explorer import AgentSettings
    from autoscraper.agent.llm import make_llm, make_wait_llm
    from autoscraper.agent.responses import ChatOpenAIResponses

    calls = []

    async def fail(self, *args, **kwargs):
        calls.append(self)
        raise RuntimeError("provider unavailable")

    monkeypatch.setattr(ChatOpenAIResponses, "_invoke_once", fail)
    settings = AgentSettings(
        model="fixture", api_key="fake", base_url="https://llm.test",
        max_steps=10, max_llm_calls=1, repair_reserve_calls=0, max_wait_llm_calls=1,
    )
    main = make_llm(settings)
    waiting = make_wait_llm(settings)

    async def scenario():
        with pytest.raises(RuntimeError, match="provider unavailable"):
            await main.ainvoke([])
        assert main.calls == 1
        assert waiting.calls == 0
        with pytest.raises(RuntimeError, match="budget exhausted"):
            await main.ainvoke([])
        assert calls == [main]
        with pytest.raises(RuntimeError, match="provider unavailable"):
            await waiting.ainvoke([])
        assert waiting.calls == 1
        assert main.calls == 1
        assert calls == [main, waiting]

    asyncio.run(scenario())
