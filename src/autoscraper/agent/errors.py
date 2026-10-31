# Agent 领域共享异常：供探索器、模型调用和工作流识别失败原因。
# 本模块不依赖业务实现或第三方模型库，避免错误处理引入反向依赖。
"""Shared agent errors without model or browser dependencies."""


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


# 请求被任务或阶段预算拒绝；与模型服务本身的故障分开处理。
class LLMBudgetExceeded(RuntimeError):
    """No request was sent because the current task/phase limit was reached."""
