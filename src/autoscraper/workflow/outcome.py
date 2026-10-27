# 任务结果与对外摘要：集中保存状态、候选规则、数据、反馈和执行计数。
# 本模块不编排任务，也不重新判定验证结果；summary 保留 CLI 和追踪共用的既有字段语义。
"""Task outcome data and the shared CLI/tracing summary."""

from dataclasses import dataclass, field

from autoscraper.scraping.executor import ScrapeResult
from autoscraper.scraping.spec import ScraperSpec
from .validation import Feedback, TaskRequirements


# 任务全过程的结果容器：状态、候选规则、数据、反馈、预算使用和澄清记录。
# spec 可能是在执行失败后保留的候选，是否完成任务应结合 status 和 sample_only 判断。
@dataclass
class TaskOutcome:
    # status 是任务终态；spec/data 可以保留失败现场，不能仅凭它们存在判断成功。
    status: str
    requirements: TaskRequirements | None = None
    spec: ScraperSpec | None = None
    data: ScrapeResult | None = None
    feedback: list[Feedback] = field(default_factory=list)
    attempts: int = 0
    llm_calls: int = 0
    wait_llm_calls: int = 0
    sample_only: bool = False
    failure_stage: str | None = None
    # history 记录各轮验证反馈；clarifications 是跨修复轮次共享的用户问答。
    history: list[dict] = field(default_factory=list)
    original_url: str | None = None
    clarifications: list[dict] = field(default_factory=list)
    pending_clarification: dict | None = None

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
            and self.data.stop_reason != "detail_limit",
            "task_fulfilled": self.status in {"success", "validated"} and not self.sample_only,
            "fulfillment_basis": "clarified_task" if self.clarifications else "original_task",
            "sample_only": self.sample_only,
            "requirements": self.requirements.model_dump() if self.requirements else None,
            "attempts": self.attempts,
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
            "validation_scope": "protocol, required fields, count and extraction; "
            "source equivalence and semantic constraints are not independently verified",
        }
