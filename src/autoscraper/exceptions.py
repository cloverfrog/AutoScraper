# 项目自定义异常的统一入口：集中维护继承关系、错误码和附加诊断字段。
# 请求模型只用于类型提示；普通执行器和人工交互不需要加载可选 Agent 依赖。
"""Shared application exceptions, independent of workflow and browser state."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from autoscraper.agent.clarification import ClarificationRequest
    from autoscraper.browser.access import HandoffRequest


# 抓取致命错误的基类；工作流捕获后转换为结构化验证反馈。
class ScrapeError(RuntimeError):
    """Fatal navigation, selector, pagination or extraction error."""


# 点击或滚动动作本身失败，与动作成功但列表未更新的内容超时分开反馈。
class PaginationActionError(ScrapeError):
    code = "pagination_action_failure"


# 单条记录的提取失败；由 on_item_error 决定跳过该条还是终止抓取。
class ItemError(ScrapeError):
    """An individual item could not be extracted."""


# 导航失败，保留目标 URL；网络或 HTTP 错误不能直接证明 CSS 规则有误。
class NavigationError(ItemError):
    """A navigation failed, without evidence that extraction selectors are wrong."""

    code = "navigation_failure"

    # 保存失败 URL 并附加可读诊断，供异常转换和最终报告使用。
    def __init__(self, url: str, message: str):
        self.url = url
        super().__init__(f"Navigation failed at {url}: {message}")


# 导航超过时限，与内容未出现的超时分开标记。
class NavigationTimeout(NavigationError):
    code = "navigation_timeout"


# 页面或详情字段在等待期限内未出现；工作流不会据此要求重写选择器。
class ContentTimeout(ItemError):
    """Missing content within the deadline is not evidence of invalid CSS."""

    code = "content_timeout"

    # 保留内容等待失败的位置，避免只剩一段无法定位的异常文本。
    def __init__(self, url: str, message: str):
        self.url = url
        super().__init__(message)


# 探索层统一异常，表示 Agent 无法在能力或预算范围内生成候选规则。
class AgentError(RuntimeError):
    pass


# 模型最终输出无法解析成合法抓取协议；工作流可尝试让 Agent 修复。
class InvalidSpecError(AgentError):
    pass


# Agent 明确报告协议无法表达需求，携带原因和受影响字段。
class UnsupportedTaskError(AgentError):
    # 保留不能满足的字段，使工作流能输出机器可读的失败反馈。
    def __init__(self, reason: str, fields: list[str]):
        super().__init__(reason)
        self.fields = fields


# 没有有效回答时抛出的控制流异常，携带原问题供 CLI 返回待澄清状态。
class ClarificationRequired(Exception):
    # 保留完整结构化问题，避免上层只拿到异常文本而丢失选项和说明。
    def __init__(self, request: ClarificationRequest):
        super().__init__(request.question)
        self.request = request


# 通过控制流异常区分暂时无法交互与用户主动取消，由 CLI 映射不同退出码。
class HandoffRequired(Exception):
    def __init__(self, request: HandoffRequest, *, cancelled: bool = False):
        super().__init__(
            "Browser operation cancelled" if cancelled else "User browser operation required"
        )
        self.request = request
        self.cancelled = cancelled


# 请求被任务或阶段预算拒绝，供工作流区分预算终止与模型服务故障。
class LLMBudgetExceeded(RuntimeError):
    """No request was sent because the current task/phase limit was reached."""


# 只有模型适配器需要这个供应商异常基类；延迟定义并缓存，保持跨模块捕获的类型一致。
def __getattr__(name: str) -> type[Exception]:
    if name != "StructuredOutputError":
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    from browser_use.llm.exceptions import ModelProviderError

    class StructuredOutputError(ModelProviderError):
        """Only a completed response that failed structured-output validation."""

    StructuredOutputError.__qualname__ = name
    globals()[name] = StructuredOutputError
    return StructuredOutputError
