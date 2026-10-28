# 工作流辅助模块的依赖边界：读取结果或解析需求不应加载任务编排及浏览器 Agent。
# 使用新进程检查，避免其他测试提前导入模块后掩盖依赖问题。
import os
import subprocess
import sys

import pytest


# 结果模型和需求解析均可单独导入，不启动模型或浏览器，也不依赖 runner 的初始化。
@pytest.mark.parametrize("component", ["outcome", "requirements"])
def test_workflow_helpers_import_without_runner_or_agent(component):
    script = """
import importlib
import sys

component = sys.argv[1]
module = importlib.import_module('autoscraper.workflow.' + component)
if component == 'outcome':
    module.TaskOutcome(status='unfulfilled').summary()
else:
    module.explicit_requirements('first 2 records, fields named title', None, None)
    assert 'playwright.sync_api' not in sys.modules
    assert 'autoscraper.workflow.validation' not in sys.modules
assert 'autoscraper.workflow.runner' not in sys.modules
assert 'autoscraper.agent.explorer' not in sys.modules
assert 'autoscraper.agent.llm' not in sys.modules
assert 'browser_use' not in sys.modules
"""
    result = subprocess.run(
        [sys.executable, "-c", script, component],
        capture_output=True,
        text=True,
        encoding="utf-8",
        env={**os.environ, "PYTHONUTF8": "1"},
        timeout=30,
    )
    assert result.returncode == 0, result.stderr
