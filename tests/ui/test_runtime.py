# Web 入口适配测试：验证跨线程人工交互、全局互斥、产物和凭据隔离。
import json
import logging
from pathlib import Path
import threading
import time

import pytest

from autoscraper.agent.clarification import ClarificationRequest, ClarificationSession
from autoscraper.browser.access import BrowserAccess, HandoffRequest, HandoffRequired
from autoscraper.scraping.executor import ScrapeResult
from autoscraper.ui.runtime import Job, JobLogHandler, JobManager, RunConfig


# 有界等待后台状态，失败时提供快照而不是挂住测试进程。
def wait_for(predicate):
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.01)
    raise AssertionError("后台任务未到达预期状态")


# 使用真实澄清会话确认网页答案返回给原调用，旧请求不能误答下一问题。
def test_clarification_resumes_same_session_and_rejects_stale_response():
    job = Job(RunConfig())
    request = ClarificationRequest(question="哪个栏目？", explanation="有两个栏目", options=["书籍", "电影"])
    session = ClarificationSession(handler=lambda request: job.ask("clarification", request))
    session.pending = request
    thread = threading.Thread(target=session.answer_pending)
    thread.start()
    wait_for(lambda: job.snapshot()["pending"] is not None)
    pending = job.snapshot()["pending"]
    assert pending["explanation"] == "有两个栏目"
    with pytest.raises(ValueError):
        job.respond(pending["id"], "")
    job.respond(pending["id"], "书籍")
    thread.join(2)
    assert not thread.is_alive()
    assert session.history[0]["answer"] == "书籍"
    assert session.waiting_seconds > 0
    with pytest.raises(ValueError):
        job.respond(pending["id"], "电影")


# 人工接管仍沿用 BrowserAccess 的继续/取消语义。
@pytest.mark.parametrize("cancel", [False, True])
def test_handoff_decision(cancel):
    job = Job(RunConfig())
    access = BrowserAccess(handler=lambda request: job.ask("handoff", request))
    access.pending = HandoffRequest(reason="请登录")
    errors = []

    def resume():
        try:
            access.resume()
        except HandoffRequired as exc:
            errors.append(exc)

    thread = threading.Thread(target=resume)
    thread.start()
    wait_for(lambda: job.snapshot()["pending"] is not None)
    pending = job.snapshot()["pending"]
    assert pending["reason"] == "请登录"
    job.respond(pending["id"], "continue", cancel=cancel)
    thread.join(2)
    assert not thread.is_alive()
    assert bool(errors) == cancel
    if cancel:
        assert errors[0].cancelled
    else:
        assert access.handed_off


# 网页丢失后等待会超时并保留待处理终态，不会默认替用户选择。
def test_wait_timeout():
    job = Job(RunConfig())
    assert job.ask("handoff", HandoffRequest(reason="登录"), timeout=0.01) is None
    assert job.snapshot()["pending"] is None


# 页面拿到的日志保留 Agent 信息，但不会重复展示或暴露已知密钥。
def test_live_logs_are_bounded_and_sanitized(monkeypatch):
    monkeypatch.setenv("AUTOSCRAPER_API_KEY", "test-private-key")
    job = Job(RunConfig())
    handler = JobLogHandler(job)
    record = logging.LogRecord("browser_use.Agent", logging.INFO, "", 0,
                               "Step 3: Memory test-private-key", (), None)
    handler.emit(record)
    handler.emit(record)
    assert job.snapshot()["logs"] == "INFO [browser_use.Agent] Step 3: Memory [redacted]"
    for _ in range(1600):
        handler.emit(logging.LogRecord("browser_use.Agent", logging.INFO, "", 0, "step", (), None))
    assert len(job.logs) == 1500


# 不同页面共享活动任务限制，失败后释放；认证名称不能穿越目录。
def test_global_single_job_and_path_validation(tmp_path, monkeypatch):
    manager = JobManager(tmp_path)
    gate = threading.Event()

    def execute(job, access, output, trace):
        gate.wait(3)
        raise ValueError("故意失败")

    monkeypatch.setattr(manager, "_execute", execute)
    with pytest.raises(ValueError):
        manager.start(RunConfig(mode="spec", auth_profile="../secret"))
    job_id = manager.start(RunConfig(mode="spec"))
    with pytest.raises(ValueError, match="已有任务"):
        manager.start(RunConfig(mode="spec"))
    gate.set()
    wait_for(lambda: manager.active is None)
    assert manager.get(job_id).snapshot()["summary"]["error"] == "故意失败"
    second = manager.start(RunConfig(mode="spec"))
    wait_for(lambda: manager.get(second).done)


# 规则模式直接调用执行器、应用超时覆盖，并导出数据，不依赖模型。
def test_spec_execution_exports_only_artifacts(tmp_path, monkeypatch):
    seen = {}

    class Executor:
        def __init__(self, **kwargs):
            seen.update(kwargs)

        def run(self, spec):
            assert spec.content_timeout_ms == 1234
            assert spec.timeout_ms == 4321
            seen["access"].state = {"cookies": [], "origins": []}
            return ScrapeResult(records=[{"title": "示例"}], pages_visited=1)

    monkeypatch.setattr("autoscraper.ui.runtime.ScraperExecutor", Executor)
    manager = JobManager(tmp_path)
    config = RunConfig(
        mode="spec", spec_json=json.dumps({
            "start_url": "https://example.com", "item_selector": "article",
            "fields": {"title": {"selector": "h1"}},
        }), auth_profile="demo", content_timeout_ms=1234, navigation_timeout_ms=4321,
    )
    job_id = manager.start(config)
    wait_for(lambda: manager.get(job_id).done)
    state = manager.get(job_id).snapshot()
    assert state["status"] == "success", state
    assert state["preview"] == [{"title": "示例"}]
    assert {Path(path).name for path in state["files"]} == {"rule.json", "data.json", "data.csv"}
    assert all(Path(path).is_relative_to(tmp_path / "artifacts") for path in state["files"])
    assert (tmp_path / "auth" / "demo.json").stat().st_mode & 0o777 == 0o600
    assert json.loads(Path(state["files"][1]).read_text()) == [{"title": "示例"}]


# Agent 模式复用环境配置和工作流；样本与未验证候选使用不同产物语义。
@pytest.mark.parametrize("status,sample_only", [("validated", True), ("success", False), ("unfulfilled", False)])
@pytest.mark.parametrize("with_reference", [False, True])
def test_agent_adapter_preserves_outcome_semantics(
    tmp_path, monkeypatch, status, sample_only, with_reference
):
    import sys
    from types import ModuleType

    from autoscraper.scraping.spec import ScraperSpec
    from autoscraper.workflow.outcome import TaskOutcome

    monkeypatch.setenv("AUTOSCRAPER_API_KEY", "private-key")
    monkeypatch.setenv("AUTOSCRAPER_BROWSER_PATH", "/custom/chromium")
    monkeypatch.setitem(sys.modules, "browser_use", ModuleType("browser_use"))
    spec = ScraperSpec.model_validate({
        "start_url": "https://example.com", "item_selector": "article",
        "fields": {"title": {"selector": "h1"}},
    })
    data = ScrapeResult(records=[{"title": "示例"}], pages_visited=1)

    def run_task(url, task, settings, **kwargs):
        assert url == "https://example.com"
        assert task == "抓标题"
        assert settings.executable_path == "/custom/chromium"
        assert settings.max_wait_llm_calls == 0
        assert kwargs["execute"]
        assert kwargs["fields"] == ["title"]
        assert kwargs["count"] == 1
        assert kwargs["initial_spec"] == (spec if with_reference else None)
        assert callable(kwargs["ask_user"])
        return TaskOutcome(status=status, spec=spec, data=data, sample_only=sample_only)

    monkeypatch.setattr("autoscraper.workflow.runner.run_task", run_task)
    manager = JobManager(tmp_path)
    job_id = manager.start(RunConfig(
        url="https://example.com", task="抓标题", execute=True, fields="title", count=1,
        agent_options={"max_wait_llm_calls": 0},
        initial_spec_json=spec.model_dump_json() if with_reference else " ",
    ))
    wait_for(lambda: manager.get(job_id).done)
    state = manager.get(job_id).snapshot()
    assert state["status"] == status, state
    assert state["summary"]["sample_only"] == sample_only
    names = {Path(path).name for path in state["files"]}
    if status == "unfulfilled":
        assert names == {"rule.unvalidated.json"}
        assert state["preview"] == []
    else:
        assert names == {"rule.json", "data.json", "data.csv"}


# 非空参考协议先做结构校验，无效输入不启动模型，也不发布规则产物。
@pytest.mark.parametrize("payload", ["{bad json", "{}"])
def test_agent_invalid_reference_does_not_start_model(tmp_path, monkeypatch, payload):
    import sys
    from types import ModuleType

    monkeypatch.setitem(sys.modules, "browser_use", ModuleType("browser_use"))
    monkeypatch.setattr(
        "autoscraper.cli.agent.settings_from_env",
        lambda **kwargs: pytest.fail("Do not call model"),
    )
    manager = JobManager(tmp_path)
    job_id = manager.start(RunConfig(
        url="https://example.com", task="抓标题", initial_spec_json=payload,
    ))
    wait_for(lambda: manager.get(job_id).done)
    state = manager.get(job_id).snapshot()
    assert state["status"] == "unfulfilled"
    assert state["files"] == []
    assert "error" in state["summary"]


# 网络滚动/批次预算耗尽须显示部分结果，同时保留导出文件及旧 DOM 状态语义。
@pytest.mark.parametrize("network", [True, False])
@pytest.mark.parametrize("stop_reason", ["scroll_limit", "max_pages", "no_more_content"])
def test_network_budget_is_partial_in_spec_ui(tmp_path, monkeypatch, network, stop_reason):
    data = dict(start_url="https://fixture.test/", item_selector="article",
                fields=dict(title=dict(selector="h1")))
    if network:
        data = dict(version="3", start_url="https://fixture.test/", network=dict(
            url_pattern="https://fixture.test/api", records_path=[], fields=dict(title=dict(path=["title"])),
            pagination=dict(type="scroll")))
    monkeypatch.setattr("autoscraper.ui.runtime.ScraperExecutor.run", lambda self, spec: ScrapeResult(
        records=[dict(title="Book")], stop_reason=stop_reason))
    manager = JobManager(tmp_path)
    job_id = manager.start(RunConfig(mode="spec", spec_json=json.dumps(data)))
    wait_for(lambda: manager.get(job_id).done)
    state = manager.get(job_id).snapshot()
    assert state["status"] == ("partial" if network and stop_reason != "no_more_content" else "success")
    assert state["preview"] == [dict(title="Book")]
    assert {Path(path).name for path in state["files"]} == {"rule.json", "data.json", "data.csv"}
