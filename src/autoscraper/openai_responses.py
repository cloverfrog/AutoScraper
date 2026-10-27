# 模型接口适配：把 browser-use 的消息和结构化输出类型转换为 Responses API 请求。
# 负责序列化消息、生成 JSON Schema、解析输出及 token 用量，并统一第三方 API 异常。
# 调用次数上限由 agent_tools.py 的 LimitedChatOpenAI 管理，本模块负责实际请求。
"""OpenAI-compatible Responses API adapter for browser-use structured output."""

from dataclasses import dataclass
from typing import Any, Literal, TypeVar, overload

import httpx
from openai import APIConnectionError, APIStatusError, AsyncOpenAI, RateLimitError
from pydantic import BaseModel

from browser_use.llm.base import BaseChatModel
from browser_use.llm.exceptions import (
    ModelOutputTruncatedError,
    ModelProviderError,
    ModelRateLimitError,
)
from browser_use.llm.messages import BaseMessage
from browser_use.llm.openai.responses_serializer import ResponsesAPIMessageSerializer
from browser_use.llm.schema import SchemaOptimizer
from browser_use.llm.views import ChatInvokeCompletion, ChatInvokeUsage

from .tracing import capture_usage, traced_llm

T = TypeVar("T", bound=BaseModel)


# 实现 browser-use 的聊天模型接口，将底层调用路由到兼容 Responses 的服务。
@dataclass
class ChatOpenAIResponses(BaseChatModel):
    """A browser-use chat model backed by an OpenAI-compatible Responses API."""

    model: str
    api_key: str | None = None
    base_url: str | httpx.URL | None = None
    timeout: float | httpx.Timeout | None = None
    max_retries: int = 0
    max_output_tokens: int | None = 4096
    temperature: float | None = 0
    top_p: float | None = None
    reasoning_effort: Literal["none", "low", "high", "max"] | None = None
    remove_min_items_from_schema: bool = False
    remove_defaults_from_schema: bool = False

    # 向框架声明 OpenAI 兼容接口类型；实际服务地址仍由 base_url 决定。
    @property
    def provider(self) -> str:
        return "openai"

    # 提供框架用于日志和异常的模型标识。
    @property
    def name(self) -> str:
        return self.model

    # 按当前连接参数创建异步 API 客户端，统一超时与 SDK 重试设置。
    def get_client(self) -> AsyncOpenAI:
        return AsyncOpenAI(
            api_key=self.api_key,
            base_url=self.base_url,
            timeout=self.timeout,
            max_retries=self.max_retries,
        )

    # 把 Responses 的 token 用量转换成 browser-use 的统计结构；缺失用量保留为 None。
    @staticmethod
    def _usage(response: Any) -> ChatInvokeUsage | None:
        usage = response.usage
        if usage is None:
            return None
        details = usage.input_tokens_details
        return ChatInvokeUsage(
            prompt_tokens=usage.input_tokens,
            prompt_cached_tokens=getattr(details, "cached_tokens", None),
            prompt_cache_creation_tokens=None,
            prompt_image_tokens=None,
            completion_tokens=usage.output_tokens,
            total_tokens=usage.total_tokens,
        )

    # 拒绝服务端失败或不完整响应；输出 token 用尽单独报告为截断错误。
    def _check_response(self, response: Any) -> None:
        if response.status == "failed":
            error = getattr(response, "error", None)
            message = getattr(error, "message", None) or "Responses API request failed"
            raise ModelProviderError(message=message, model=self.name)
        if response.status == "incomplete":
            details = getattr(response, "incomplete_details", None)
            reason = getattr(details, "reason", None)
            if reason == "max_output_tokens":
                raise ModelOutputTruncatedError(
                    message=(
                        f"Model response was truncated at max_output_tokens="
                        f"{self.max_output_tokens}"
                    ),
                    model=self.name,
                )
            raise ModelProviderError(
                message=f"Model response was incomplete: {reason or 'unknown reason'}",
                status_code=500,
                model=self.name,
            )

    # 文本调用的类型重载：未给 output_format 时，completion 是字符串。
    @overload
    async def ainvoke(
        self, messages: list[BaseMessage], output_format: None = None, **kwargs: Any
    ) -> ChatInvokeCompletion[str]: ...

    # 结构化调用的类型重载：给定 Pydantic 类型时，completion 是对应模型实例。
    @overload
    async def ainvoke(
        self, messages: list[BaseMessage], output_format: type[T], **kwargs: Any
    ) -> ChatInvokeCompletion[T]: ...

    # 将消息和可选 Pydantic 输出模型转成 Responses 请求，再解析文本或结构化结果。
    # 上方重载只描述返回类型；此处实际发送请求、记录用量并转换 API 异常。
    @traced_llm
    async def ainvoke(
        self,
        messages: list[BaseMessage],
        output_format: type[T] | None = None,
        **kwargs: Any,
    ) -> ChatInvokeCompletion[T] | ChatInvokeCompletion[str]:
        params: dict[str, Any] = {
            "model": self.model,
            "input": ResponsesAPIMessageSerializer.serialize_messages(messages),
        }
        if self.reasoning_effort is not None:
            params["reasoning"] = {"effort": self.reasoning_effort}
        if self.max_output_tokens is not None:
            params["max_output_tokens"] = self.max_output_tokens
        if self.temperature is not None:
            params["temperature"] = self.temperature
        if self.top_p is not None:
            params["top_p"] = self.top_p
        if output_format is not None:
            schema = SchemaOptimizer.create_optimized_json_schema(
                output_format,
                remove_min_items=self.remove_min_items_from_schema,
                remove_defaults=self.remove_defaults_from_schema,
            )
            params["text"] = {
                "format": {
                    "type": "json_schema",
                    "name": "agent_output",
                    "schema": schema,
                }
            }

        try:
            response = await self.get_client().responses.create(**params)
            usage = self._usage(response)
            capture_usage(usage)
            self._check_response(response)
            if output_format is None:
                return ChatInvokeCompletion(
                    completion=response.output_text or "",
                    usage=usage,
                    stop_reason=response.status,
                )
            if not response.output_text:
                raise ModelProviderError(
                    message="Responses API returned no structured output text",
                    status_code=500,
                    model=self.name,
                )
            return ChatInvokeCompletion(
                completion=output_format.model_validate_json(response.output_text),
                usage=usage,
                stop_reason=response.status,
            )
        except ModelProviderError:
            raise
        except RateLimitError as exc:
            raise ModelRateLimitError(message=exc.message, model=self.name) from exc
        except APIConnectionError as exc:
            raise ModelProviderError(message=str(exc), model=self.name) from exc
        except APIStatusError as exc:
            raise ModelProviderError(
                message=exc.message,
                status_code=exc.status_code,
                model=self.name,
            ) from exc
        except Exception as exc:
            raise ModelProviderError(message=str(exc), model=self.name) from exc
