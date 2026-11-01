# 澄清后的需求同步：复现用户同意 21 条，但旧验证仍要求 25 条的失败路径。
# 浏览器和模型使用脚本替身；真实生成恢复、提交门槛、预算和工作流验证继续执行。
import asyncio
import json
from importlib import import_module
from types import SimpleNamespace

import pytest

from autoscraper.agent.clarification import ClarificationRequest
from autoscraper.agent.errors import LLMBudgetExceeded
from autoscraper.agent.explorer import AgentSettings
from autoscraper.observability.tracing import Trace
from autoscraper.scraping.executor import ScrapeError, ScrapeResult, ScraperExecutor
from autoscraper.scraping.spec import ScraperSpec
from autoscraper.workflow.requirements import TaskRequirements, revise_requirements
from autoscraper.workflow.runner import run_task
from autoscraper.workflow.validation import ValidationSettings

URL = "https://fixture.test/?q=Boston+Bruins"
TASK = "抓取球队为Boston Bruins的前25条，字段名为name、year、win_rate"
BASE = TaskRequirements(fields=["name", "year", "win_rate"], count=25,
    filters=[{"field": "team", "operator": "eq", "value": "Boston Bruins"}])
REQUEST = ClarificationRequest(
    question="Only 21 Boston Bruins records are available. Return all 21 or keep requesting 25?",
    explanation="The filtered listing reports 21 records and has no next page.",
    options=["Return all 21 available Boston Bruins records", "Keep requesting 25 records"],
)
ROWS = [{"name": "Boston Bruins", "year": str(1990 + i), "win_rate": "0.55"} for i in range(21)]


# 记录真实追踪事件，验证修订后的需求及共享调用量可供诊断。
class MemorySink:
    def __init__(self):
        self.events = []
        self.summary_data = None

    def event(self, event):
        self.events.append(event)

    def history(self, attempt, entry):
        pass

    def summary(self, data):
        self.summary_data = data


# 脚本只替换外部模型/浏览器边界，ask_user 后仍由真实 Explorer 执行独立需求更新。
def execute_scenario(monkeypatch, *, answer=REQUEST.options[0], count=21, failure=None, repairs=0):
    bu = pytest.importorskip("browser_use")
    # 工具参数类型在替换 BrowserSession 前注册，避免替身污染注解缓存。
    import_module("autoscraper.agent.tools")
    import autoscraper.agent.llm as models

    captured = {"agents": [], "killed": 0, "executions": 0, "updates": []}

    class Model:
        calls = 0
        budget_blocked = False

        def consume(self):
            if self.calls >= self.call_limit:
                self.budget_blocked = True
                raise LLMBudgetExceeded("Shared task request budget exhausted")
            self.calls += 1

        async def ainvoke(self, messages, output_format):
            self.consume()
            assert output_format is TaskRequirements
            payload = json.loads(messages[-1].content)
            if "current_requirements" not in payload:
                return SimpleNamespace(completion=BASE.model_copy(deep=True))
            captured["updates"].append(payload)
            assert payload["current_requirements"] == BASE.model_dump()
            assert payload["clarification"]["answer"] == answer
            assert "candidate" not in payload and "spec" not in payload
            if failure == "invalid":
                return SimpleNamespace(completion={"fields": [], "count": count})
            if failure == "timeout":
                await asyncio.sleep(2)
            if failure == "unsupported":
                return SimpleNamespace(completion=TaskRequirements.model_validate(
                    {**BASE.model_dump(), "unsupported_capabilities": ["captcha_bypass"]}))
            return SimpleNamespace(completion=TaskRequirements.model_validate(
                {**BASE.model_dump(), "count": count}))

    model = Model()

    class Browser:
        def __init__(self, **kwargs):
            pass

        async def kill(self):
            captured["killed"] += 1

    class Agent:
        def __init__(self, **kwargs):
            self.tools = kwargs["tools"]
            self.history = SimpleNamespace(history=[])
            self.prompt = kwargs["task"]
            self.followups = []
            self.runs = 0
            captured["agents"].append(self)

        def add_new_task(self, text):
            self.followups.append(text)

        async def run(self, max_steps, **kwargs):
            self.runs += 1
            model.consume()
            ask_here = (
                len(captured["agents"]) == 2 and self is captured["agents"][1]
                if failure == "ask_on_repair" else self is captured["agents"][0]
            )
            if ask_here and self.runs == 1:
                await self.tools.registry.registry.actions["ask_user"].function(**REQUEST.model_dump())
                return SimpleNamespace(is_successful=lambda: False, final_result=lambda: None)
            requirements = self.tools.submission.requirements
            # 同一 Agent 恢复及后续修复都必须立即使用当前数量，而不是原始 25。
            expected = 25 if failure == "ask_on_repair" and self is captured["agents"][0] else count
            assert requirements.count == expected
            if self.followups:
                assert requirements.model_dump_json() in self.followups[-1]
            else:
                assert requirements.model_dump_json() in self.prompt
            spec = ScraperSpec(start_url=URL, item_selector="tr.team", max_items=expected,
                fields={"name": {"selector": "td.name"}, "year": {"selector": "td.year"},
                        "win_rate": {"selector": "td.pct"}}, on_item_error="fail")
            # 检查真实提交门槛，确保恢复时没有旧的 count_mismatch 拦截候选。
            for name, field in spec.fields.items():
                self.tools.submission.remember({"url": URL, "item_selector": "tr.team",
                    "count": 21, "samples": [{"matches": 1, "values": [ROWS[0][name]]}]},
                    field.selector)
            assert self.tools.submission.check(spec) == []
            return SimpleNamespace(is_successful=lambda: True, final_result=spec.model_dump_json)

    monkeypatch.setattr(bu, "Agent", Agent)
    monkeypatch.setattr(bu, "BrowserSession", Browser)
    monkeypatch.setattr(models, "make_llm", lambda settings: model)
    monkeypatch.setattr(models, "make_wait_llm", lambda settings: SimpleNamespace(calls=0, max_calls=0))

    def scrape(self, spec):
        captured["executions"] += 1
        if failure == "repair" and captured["executions"] == 1:
            raise ScrapeError("Selector failure to exercise a subsequent repair")
        return ScrapeResult(records=ROWS, pages_visited=1, items_extracted=21, stop_reason="no_pagination")

    monkeypatch.setattr(ScraperExecutor, "run", scrape)
    settings = AgentSettings(api_key="fake", model="fixture", base_url="https://llm.test",
        max_llm_calls=2 if failure == "budget" else 24,
        repair_reserve_calls=0, timeout_seconds=1 if failure == "timeout" else 30)
    sink = MemorySink()
    outcome = run_task(URL, TASK, settings, execute=True, ask_user=lambda request: answer,
        validation=ValidationSettings(max_repairs=repairs), trace=Trace(sink))
    return outcome, captured, model, sink


# 用户明确选择 21 条后，提交、执行、摘要都按 21 校验；原始需求与筛选条件完整保留。
def test_approved_count_reaches_submission_and_execution(monkeypatch):
    outcome, captured, model, sink = execute_scenario(monkeypatch)
    assert outcome.status == "success" and outcome.attempts == 1
    assert outcome.spec.max_items == outcome.requirements.count == 21
    assert outcome.original_requirements.count == 25
    assert outcome.requirements.filters == outcome.original_requirements.filters == BASE.filters
    assert len(outcome.data.records) == 21 and captured["executions"] == 1
    assert len(captured["agents"]) == 1 and captured["agents"][0].runs == 2
    assert captured["agents"][0].tools.submission.requirements is outcome.requirements
    assert model.calls == outcome.llm_calls == 4  # 初始解析、两次 Agent 运行及一次澄清解析。
    assert captured["killed"] == 1 and len(captured["updates"]) == 1
    summary = outcome.summary()
    assert summary["task_fulfilled"] and summary["fulfillment_basis"] == "clarified_task"
    assert summary["original_requirements"]["count"] == 25 and summary["requirements"]["count"] == 21
    event = next(e for e in sink.events if e["event_type"] == "requirements_update.finished")
    assert event["payload"]["before"]["count"] == 25 and event["payload"]["after"]["count"] == 21


# 拒绝降低数量或只允许替代入口，都不代表接受 Agent 提议的 21 条。
@pytest.mark.parametrize("answer", [REQUEST.options[1], "You may use another listing URL, but keep 25 records"])
def test_unapproved_count_is_not_lowered(monkeypatch, answer):
    outcome, captured, _, _ = execute_scenario(monkeypatch, answer=answer, count=25)
    assert outcome.status == "unfulfilled" and outcome.requirements.count == 25
    assert outcome.original_requirements.count == 25
    assert [f.code for f in outcome.feedback] == ["insufficient_items"]
    assert outcome.spec.max_items == 25 and captured["executions"] == 1


# 未回答不调用更新模型，也不恢复执行；问题和原始数量保留。
def test_unanswered_clarification_keeps_requirements(monkeypatch):
    outcome, captured, model, _ = execute_scenario(monkeypatch, answer=None)
    assert outcome.status == "needs_clarification"
    assert outcome.requirements.count == outcome.original_requirements.count == 25
    assert not captured["updates"] and captured["executions"] == 0 and model.calls == 2
    assert captured["agents"][0].runs == 1 and captured["killed"] == 1


# 预算不足、超时或解析失败时不能带着旧需求继续抓取，已回答的澄清历史仍保留。
@pytest.mark.parametrize("failure", ["invalid", "budget", "timeout"])
def test_update_failure_does_not_execute_stale_requirements(monkeypatch, failure):
    outcome, captured, _, _ = execute_scenario(monkeypatch, failure=failure)
    assert outcome.status == "unfulfilled" and not captured["executions"]
    assert outcome.requirements.count == outcome.original_requirements.count == 25
    assert outcome.clarifications[0]["answer"] == REQUEST.options[0]
    assert captured["agents"][0].runs == 1 and captured["killed"] == 1
    assert outcome.feedback[0].code == ("budget_exhausted" if failure == "budget" else "agent_failure")


# 后续选择器修复重建 Agent 时仍沿用更新后的基准，不把用户数量重置为原来的 25。
def test_updated_requirements_survive_repair(monkeypatch):
    outcome, captured, _, _ = execute_scenario(monkeypatch, failure="repair", repairs=1)
    assert outcome.status == "success" and outcome.attempts == 2
    assert outcome.requirements.count == 21 and outcome.original_requirements.count == 25
    assert len(captured["agents"]) == 2 and captured["executions"] == 2
    assert len(captured["updates"]) == 1 and captured["killed"] == 2


# 修订解析独立于候选协议，支持用户明确更换字段/取消筛选，且不会重新覆盖初始显式数量。
def test_revision_preserves_explicit_user_changes():
    captured = []
    changed = TaskRequirements(fields=["title"], count=None, filters=[])
    class Model:
        async def ainvoke(self, messages, output_format):
            captured.append(messages)
            return SimpleNamespace(completion=changed)
    result = asyncio.run(revise_requirements(BASE,
        {"request": REQUEST.model_dump(), "answer": "抓取所有球队，字段改为title，不限制数量"}, Model()))
    assert result == changed and BASE.count == 25
    payload = json.loads(captured[0][-1].content)
    assert set(payload) == {"current_requirements", "clarification"}
    assert "Agent proposals, not authorization" in captured[0][0].content


# 精确复现日志：先因 21/25 不足进入修复，再由用户同意 21，当前轮即可通过验证。
def test_count_changed_during_repair_is_used_immediately(monkeypatch):
    outcome, captured, model, _ = execute_scenario(monkeypatch, failure="ask_on_repair", repairs=1)
    assert outcome.status == "success" and outcome.attempts == 2
    assert outcome.history[0]["feedback"][0]["code"] == "insufficient_items"
    assert outcome.history[1]["feedback"] == []
    assert outcome.spec.max_items == outcome.requirements.count == 21
    assert outcome.original_requirements.count == 25 and model.calls == 5
    assert len(captured["updates"]) == 1 and captured["executions"] == 2


# 澄清若新增协议不支持的要求，应停止而不是继续执行不完整任务。
def test_unsupported_clarified_requirement_stops_execution(monkeypatch):
    outcome, captured, _, _ = execute_scenario(monkeypatch,
        answer="Also bypass CAPTCHA", count=25, failure="unsupported")
    assert outcome.status == "unsupported" and captured["executions"] == 0
    assert outcome.feedback[0].code == "unsupported_capability"
    assert captured["agents"][0].runs == 1 and captured["killed"] == 1


# 模型省略可选字段不表示用户取消了筛选，默认值不能覆盖仍有效的约束。
def test_revision_omitted_fields_preserve_current_values():
    class Model:
        async def ainvoke(self, *args, **kwargs):
            return SimpleNamespace(completion=TaskRequirements(fields=BASE.fields, count=21))
    result = asyncio.run(revise_requirements(BASE,
        {"request": REQUEST.model_dump(), "answer": REQUEST.options[0]}, Model()))
    assert result.count == 21 and result.filters == BASE.filters
    assert BASE.count == 25
