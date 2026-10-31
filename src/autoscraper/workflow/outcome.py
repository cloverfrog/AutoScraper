# 任务结果与对外摘要：集中保存状态、候选规则、数据、反馈和执行计数。
# 本模块不编排任务，也不重新判定验证结果；summary 保留 CLI 和追踪共用的既有字段语义。
"""Task outcome data and the shared CLI/tracing summary."""

from dataclasses import dataclass, field
from typing import Literal

from autoscraper.scraping.executor import ScrapeResult
from autoscraper.scraping.spec import ScraperSpec
from .validation import Feedback
from .requirements import TaskRequirements


# 业务任务状态：供编辑器和静态类型检查使用，序列化时仍是原来的字符串。
# 不与 DOM 工具的 ready/timeout 或模型接口的 completed/failed 状态混用。
TaskStatus = Literal[
    "success",              # 抓取结果已通过完整检查。
    "validated",            # 规则已通过试抓取验证，可能只覆盖样本。
    "unfulfilled",          # 任务未完成或验证失败。
    "unsupported",          # 当前协议无法满足需求。
    "needs_clarification",  # 等待用户补充任务意图。
    "needs_user",           # 需要用户在可见浏览器中操作。
    "cancelled",            # 用户取消人工接管。
]

# 工作流中的活动阶段；failure_stage=None 保留原有“未标记失败阶段”的含义。
TaskStage = Literal["exploration", "validation", "execution"]


# 任务全过程的结果容器：状态、候选规则、数据、反馈、预算使用和澄清记录。
# spec 可能是在执行失败后保留的候选，是否完成任务应结合 status 和 sample_only 判断。
@dataclass
class TaskOutcome:
    # status 是任务终态；spec/data 可以保留失败现场，不能仅凭它们存在判断成功。
    status: TaskStatus
    requirements: TaskRequirements | None = None
    spec: ScraperSpec | None = None
    data: ScrapeResult | None = None
    feedback: list[Feedback] = field(default_factory=list)
    attempts: int = 0
    llm_calls: int = 0
    agent_step_limit: int = 0
    max_llm_calls: int = 0
    repair_reserve_calls: int = 0
    repair_not_started_reason: str | None = None
    wait_llm_calls: int = 0
    sample_only: bool = False
    failure_stage: TaskStage | None = None
    # history 记录各轮验证反馈；clarifications 是跨修复轮次共享的用户问答。
    history: list[dict] = field(default_factory=list)
    original_url: str | None = None
    clarifications: list[dict] = field(default_factory=list)
    pending_clarification: dict | None = None
    pending_handoff: dict | None = None  # 只报告待操作原因，不附带认证快照。

    # 生成 CLI/追踪共用的 JSON 摘要；区分 Agent 返回、实际执行和任务是否完成。
    # spec_valid 目前仅表示已得到协议模型，不能单凭它判断需求和数据验证是否通过。
    def summary(self) -> dict:
        return {
            "status": self.status,
            "agent_status": "completed"
            if self.spec is not None or self.status == "unsupported"
            else "not_completed",
            "failure_stage": self.failure_stage,
            "spec_valid": self.spec is not None,
            "execution_success": self.data is not None
            and not self.data.errors
            and self.data.stop_reason not in {"detail_limit", "max_pages", "scroll_limit"},
            "task_fulfilled": self.status in {"success", "validated"} and not self.sample_only,
            "fulfillment_basis": "clarified_task" if self.clarifications else "original_task",
            "sample_only": self.sample_only,
            "requirements": self.requirements.model_dump() if self.requirements else None,
            "attempts": self.attempts,
            "agent_step_limit": self.agent_step_limit,
            "max_llm_calls": self.max_llm_calls,
            "repair_reserve_calls": self.repair_reserve_calls,
            "repair_not_started_reason": self.repair_not_started_reason,
            "llm_calls": self.llm_calls,
            "wait_llm_calls": self.wait_llm_calls,
            "items": len(self.data.records) if self.data else 0,
            "pages": self.data.pages_visited if self.data else 0,
            "detail_pages": self.data.detail_pages_visited if self.data else 0,
            "stop_reason": self.data.stop_reason if self.data else None,
            "feedback": [item.model_dump() for item in self.feedback],
            "validation_history": self.history,
            "original_url": self.original_url,
            "actual_start_url": str(self.spec.start_url) if self.spec else None,
            "clarifications": self.clarifications,
            "pending_clarification": self.pending_clarification,
            "pending_handoff": self.pending_handoff,
            "validation_scope": "protocol, required fields, count and extraction; "
            "source equivalence and semantic constraints are not independently verified",
        }
