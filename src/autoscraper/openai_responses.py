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

    @property
    def provider(self) -> str:
        return "openai"

    @property
    def name(self) -> str:
        return self.model

    def get_client(self) -> AsyncOpenAI:
        return AsyncOpenAI(
            api_key=self.api_key,
            base_url=self.base_url,
            timeout=self.timeout,
            max_retries=self.max_retries,
        )

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

    @overload
    async def ainvoke(
        self, messages: list[BaseMessage], output_format: None = None, **kwargs: Any
    ) -> ChatInvokeCompletion[str]: ...

    @overload
    async def ainvoke(
        self, messages: list[BaseMessage], output_format: type[T], **kwargs: Any
    ) -> ChatInvokeCompletion[T]: ...

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
