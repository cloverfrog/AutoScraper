# 模型创建与调用预算：主模型和语义等待模型各自维护共享计数。
# 本模块只依赖 Responses 适配器，不导入浏览器工具、DOM 脚本或内容等待器。
"""Model factories and task-wide paid-call limits."""

from dataclasses import dataclass, field

from .responses import ChatOpenAIResponses


# Responses 适配器上的调用次数限制；同一实例可跨需求解析和多轮修复使用。
@dataclass
class LimitedChatOpenAI(ChatOpenAIResponses):
    """OpenAI-compatible Responses provider with a hard paid-call budget."""

    max_calls: int = 12
    calls: int = field(default=0, init=False)
    trace_source: str | None = None

    # 请求发出前检查并递增计数；失败请求也消耗预算，避免无限重试造成额外费用。
    async def ainvoke(self, messages, output_format=None, **kwargs):
        if self.calls >= self.max_calls:
            raise RuntimeError("LLM call budget exhausted")
        self.calls += 1
        return await super().ainvoke(messages, output_format=output_format, **kwargs)


# 按统一供应商参数构造受限模型；禁用 SDK 自动重试，让调用预算可预测。
def _make_llm(settings, max_calls, max_output_tokens) -> LimitedChatOpenAI:
    return LimitedChatOpenAI(
        model=settings.model,
        api_key=settings.api_key.get_secret_value(),
        base_url=str(settings.base_url),
        timeout=settings.llm_timeout_seconds,
        max_retries=0,
        max_calls=max_calls,
        max_output_tokens=max_output_tokens,
        temperature=0,
        reasoning_effort="none",
    )


# 创建需求解析/探索/修复共享的主模型，使用主步骤上限和输出 token 配置。
def make_llm(settings) -> LimitedChatOpenAI:
    return _make_llm(settings, settings.max_steps, settings.max_output_tokens)


# 创建独立的语义等待模型，使用单独调用上限和较小输出长度，并标记追踪来源。
def make_wait_llm(settings) -> LimitedChatOpenAI:
    llm = _make_llm(settings, settings.max_wait_llm_calls, 256)
    llm.trace_source = "wait"
    return llm
