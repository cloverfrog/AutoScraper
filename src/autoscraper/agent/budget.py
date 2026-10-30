# 请求预算异常独立于模型和浏览器，供探索层与工作流识别真实停止原因。
"""Typed request-budget failure shared by the model and workflow."""


class LLMBudgetExceeded(RuntimeError):
    """No request was sent because the current task/phase limit was reached."""
