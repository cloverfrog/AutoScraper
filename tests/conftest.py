# 测试共用的运行日志隔离：默认日志写入 pytest 临时目录，避免污染真实抓取记录。
# 只在测试进程内替换 FileSink 初始化；显式指定的其他目录继续由各用例自行管理。
"""Shared test fixtures keeping runtime traces out of the project log directory."""

from pathlib import Path

import pytest

from autoscraper.observability.tracing import FileSink


# 自动覆盖 CLI 及直接创建 FileSink 的用例，防止新增测试遗漏 --runs-dir 后再次污染。
# 不改变工作目录，保证 examples 等相对路径测试仍按原有方式读取项目文件。
@pytest.fixture(autouse=True)
def isolated_run_logs(tmp_path_factory, monkeypatch):
    default_root = Path(".agent-runtime/runs")
    project_root = Path(__file__).resolve().parents[1] / default_root
    # 日志使用独立临时目录，不混入用例的 tmp_path，以保留产物目录的精确断言。
    temporary_root = tmp_path_factory.mktemp("runtime-logs") / "runs"
    original_init = FileSink.__init__

    # 同时识别默认相对路径和正式目录的绝对路径；其他显式路径保持不变。
    def isolated_init(self, root=default_root, *, max_bytes=20_000_000):
        if Path(root) == default_root or Path(root).resolve() == project_root:
            root = temporary_root
        original_init(self, root, max_bytes=max_bytes)

    monkeypatch.setattr(FileSink, "__init__", isolated_init)
    return temporary_root


# 非需求解析测试显式固定无筛选条件，避免其模型替身被用来解析自然语言。
# 字段/数量不完整时仍走真实解析；实际解析、筛选保留和调用预算另有专用覆盖。
@pytest.fixture
def explicit_unfiltered_requirements(monkeypatch):
    from autoscraper.workflow import runner
    from autoscraper.workflow.requirements import TaskRequirements, explicit_requirements

    original = runner.extract_requirements

    async def extract(task, llm, fields=None, count=None):
        fields, count = explicit_requirements(task, fields, count)
        if fields is not None and count is not None:
            return TaskRequirements(fields=fields, count=count)
        return await original(task, llm, fields, count)

    monkeypatch.setattr(runner, "extract_requirements", extract)
