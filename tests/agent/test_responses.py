"""Structured regeneration stays within request, tracing and timeout budgets."""

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from pydantic import BaseModel

pytest.importorskip("browser_use")

from browser_use.llm.exceptions import ModelOutputTruncatedError, ModelProviderError
from openai import APIConnectionError, APIStatusError, RateLimitError

from autoscraper.agent.llm import LimitedChatOpenAI
from autoscraper.agent.responses import ChatOpenAIResponses, StructuredOutputError
from autoscraper.observability.tracing import FileSink, Trace, labels


class Output(BaseModel):
    count: int


def response(text='{"count":25}', **overrides):
    return SimpleNamespace(
        **{
            "status": "completed",
            "output_text": text,
            "usage": SimpleNamespace(
                input_tokens=10,
                input_tokens_details=None,
                output_tokens=3,
                total_tokens=13,
            ),
            **overrides,
        }
    )


@pytest.fixture
def provider(monkeypatch):
    create = AsyncMock()
    monkeypatch.setattr(
        ChatOpenAIResponses,
        "get_client",
        lambda self: SimpleNamespace(responses=SimpleNamespace(create=create)),
    )
    return create


@pytest.mark.parametrize(
    "invalid", ['{"count":', '{"count":"bad"}', '{"count":25}\n{"count":25}', "", None]
)
@pytest.mark.parametrize("source", ["main", "requirements", "wait"])
def test_regeneration_logs_failure_and_counts_each_request(provider, tmp_path, invalid, source):
    provider.side_effect = [response(invalid), response()]
    model = LimitedChatOpenAI(model="fixture", max_calls=3)
    trace = Trace(FileSink(tmp_path))
    with trace.run(), labels(source=source):
        result = asyncio.run(model.ainvoke([], Output))
    assert result.completion.count == 25
    assert result.usage.total_tokens == 13
    assert provider.await_count == model.calls == 2
    assert provider.call_args_list[0] == provider.call_args_list[1]
    events = [
        json.loads(line)
        for line in next(tmp_path.glob("*/events.jsonl")).read_text(encoding="utf-8").splitlines()
    ]
    (invalid_event,) = [e for e in events if e["event_type"] == "llm.output_invalid"]
    assert invalid_event["source"] == source
    assert invalid_event["payload"]["raw_output"] == (invalid or "")
    assert invalid_event["payload"]["attempt"] == 1
    assert invalid_event["payload"]["retry_index"] == 0
    assert invalid_event["payload"]["exception"] == "ValidationError"
    finished = [e for e in events if e["event_type"] == "llm.finished"]
    assert [e["payload"]["outcome"] for e in finished] == ["raised", "returned"]
    assert invalid_event["call_id"] == finished[0]["call_id"]
    summary = json.loads(next(tmp_path.glob("*/summary.json")).read_text(encoding="utf-8"))
    assert summary["metrics"]["llm_calls_by_source"] == {source: 2}
    assert summary["metrics"]["tokens"]["total_tokens"] == 26
    assert summary["metrics"]["usage_missing_calls"] == 0


@pytest.mark.parametrize("budget,expected_calls", [(0, 0), (1, 1), (2, 2), (8, 3)])
def test_regeneration_stops_at_retry_or_request_limit(provider, budget, expected_calls):
    provider.return_value = response("invalid")
    model = LimitedChatOpenAI(model="fixture", max_calls=budget)
    error = RuntimeError if budget < 3 else StructuredOutputError
    with pytest.raises(error):
        asyncio.run(model.ainvoke([], Output))
    assert provider.await_count == model.calls == expected_calls


def test_disabled_retries_and_plain_text(provider):
    provider.return_value = response("invalid")
    model = ChatOpenAIResponses(model="fixture", structured_output_retries=0)
    with pytest.raises(StructuredOutputError):
        asyncio.run(model.ainvoke([], Output))
    assert provider.await_count == 1
    provider.reset_mock()
    assert asyncio.run(model.ainvoke([])).completion == "invalid"
    assert provider.await_count == 1


@pytest.mark.parametrize(
    "kind", ["network", "rate_limit", "http", "failed", "truncated", "incomplete"]
)
def test_other_provider_errors_are_not_retried(provider, kind):
    request = httpx.Request("POST", "https://fixture.test/v1/responses")
    http_response = httpx.Response(429 if kind == "rate_limit" else 500, request=request)
    if kind == "network":
        provider.side_effect = APIConnectionError(request=request)
    elif kind in {"rate_limit", "http"}:
        error = RateLimitError if kind == "rate_limit" else APIStatusError
        provider.side_effect = error("unavailable", response=http_response, body=None)
    else:
        provider.return_value = response(
            "invalid",
            status="failed" if kind == "failed" else "incomplete",
            incomplete_details=SimpleNamespace(
                reason="max_output_tokens" if kind == "truncated" else "content_filter"
            ),
        )
    model = LimitedChatOpenAI(model="fixture")
    with pytest.raises((ModelProviderError, ModelOutputTruncatedError)):
        asyncio.run(model.ainvoke([], Output))
    assert provider.await_count == model.calls == 1


def test_invalid_output_is_sanitized_and_bounded(provider, tmp_path):
    raw = "KNOWN_SECRET " + "页面数据" * 10000
    provider.return_value = response(raw)
    model = ChatOpenAIResponses(model="fixture")
    with Trace(FileSink(tmp_path), secrets=("KNOWN_SECRET",)).run():
        with pytest.raises(StructuredOutputError):
            asyncio.run(model.ainvoke([], Output))
    content = next(tmp_path.glob("*/events.jsonl")).read_text(encoding="utf-8")
    assert "KNOWN_SECRET" not in content
    failures = [
        json.loads(line)["payload"]
        for line in content.splitlines()
        if json.loads(line)["event_type"] == "llm.output_invalid"
    ]
    assert [event["attempt"] for event in failures] == [1, 2, 3]
    for event in failures:
        assert "[redacted]" in event["raw_output"]
        assert event["raw_output_chars"] == len(raw)
        assert event["raw_output_truncated"] is True
        assert len(json.dumps(event, ensure_ascii=False).encode()) <= 12000


def test_outer_timeout_cancels_regeneration_without_another_request(provider):
    async def create(**kwargs):
        if provider.await_count == 1:
            return response("invalid")
        await asyncio.Event().wait()

    provider.side_effect = create
    model = LimitedChatOpenAI(model="fixture")

    async def scenario():
        with pytest.raises(TimeoutError):
            async with asyncio.timeout(0.05):
                await model.ainvoke([], Output)

    asyncio.run(scenario())
    assert provider.await_count == model.calls == 2


# 阶段额度拦住同一次 ainvoke 内的结构化重试；开放预留后累计计数仍受硬上限约束。
def test_phase_limit_blocks_regeneration_without_resetting_hard_count(provider):
    from autoscraper.agent.errors import LLMBudgetExceeded

    provider.return_value = response("invalid")
    model = LimitedChatOpenAI(model="fixture", max_calls=4, call_limit=1)
    with pytest.raises(LLMBudgetExceeded):
        asyncio.run(model.ainvoke([], Output))
    assert provider.await_count == model.calls == 1
    model.call_limit = 100  # 阶段配置即使过大，也不能突破任务硬上限。
    provider.return_value = response()
    for _ in range(3):
        assert asyncio.run(model.ainvoke([], Output)).completion.count == 25
    with pytest.raises(LLMBudgetExceeded):
        asyncio.run(model.ainvoke([], Output))
    assert provider.await_count == model.calls == 4
