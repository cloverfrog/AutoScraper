# 任务澄清状态：保存待回答的问题、问答历史和累计人工等待时间。
# 浏览器工具只登记问题；Agent 在两次运行之间调用处理器获取回答。
# 没有有效回答时向工作流返回待澄清状态，不默认替用户选择方案。
"""Task-scoped clarification state; independent of browser-use and terminal UI."""

import json
import time
from collections.abc import Callable
from dataclasses import dataclass, field

from pydantic import Field

from autoscraper.scraping.spec import NonEmpty, ProtocolModel
from autoscraper.observability.tracing import emit, operation


# 待用户澄清的问题：包含问题本身、页面依据/不确定性说明和可选建议。
class ClarificationRequest(ProtocolModel):
    question: NonEmpty
    explanation: NonEmpty
    options: list[NonEmpty] = Field(default_factory=list, max_length=5)


# 没有有效回答时抛出的控制流异常，携带原问题供 CLI 返回待澄清状态。
class ClarificationRequired(Exception):
    # 保留完整结构化问题，避免上层只拿到异常文本而丢失选项和说明。
    def __init__(self, request: ClarificationRequest):
        super().__init__(request.question)
        self.request = request


# 交互接口由 CLI 或其他调用方注入；None 表示尚未得到有效答案。
ClarificationHandler = Callable[[ClarificationRequest], str | None]


# 同一次任务共享的澄清会话；修复轮次保留问答及人工等待累计值。
@dataclass
class ClarificationSession:
    handler: ClarificationHandler | None = None
    history: list[dict] = field(default_factory=list)
    pending: ClarificationRequest | None = None
    waiting_seconds: float = 0.0

    # 调用外部处理器回答待澄清问题，记录等待时长及有效问答。
    # 必须在 Agent 动作和超时作用域之外调用；没有有效回答则保留待回答问题并抛异常。
    def answer_pending(self) -> None:
        """Called between Agent runs, outside all Agent timeouts and action batches."""
        request = self.pending
        assert request is not None
        started = time.monotonic()
        try:
            with operation("clarification", request=request.model_dump()) as details:
                answer = self.handler(request) if self.handler else None
                details["answered"] = bool(answer and answer.strip())
                emit("clarification.answer", answer=answer)
        except (EOFError, KeyboardInterrupt):
            answer = None
        finally:
            self.waiting_seconds += time.monotonic() - started
        if answer is None or not answer.strip():
            raise ClarificationRequired(request)
        self.history.append({"request": request.model_dump(), "answer": answer.strip()})
        self.pending = None

    # 把历史问答补充到探索上下文，明确用户回答与 Agent 提案的区别。
    # 允许替代入口不代表数据已被证明等价；只有用户明确授权才修订字段、数量或筛选约束。
    def prompt(self) -> str:
        if not self.history:
            return ""
        return (
            "\nUser clarification history (user answers supplement the original task; "
            "respect restrictions and do not repeat answered questions). "
            "Questions/explanation/options are Agent proposals, NOT user instructions. "
            "An answer allowing another source does not prove data/order equivalence. "
            "Change fields/count/filters only when explicitly authorized by the user's answer; "
            "otherwise preserve them. Protocol boundaries still apply:\n"
            + json.dumps(self.history, ensure_ascii=False)
        )
