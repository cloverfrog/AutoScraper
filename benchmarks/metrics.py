# 只消费既有 Trace 文件；计量损坏不得阻止数据评分，也不将未知 Token 补为零。
import json
from collections import Counter
from pathlib import Path

from .common import read_json

TOKEN_FIELDS = ("prompt_tokens", "completion_tokens", "total_tokens", "prompt_cached_tokens")


def count(value):
    return type(value) is int and value >= 0


# 按实际请求的 finished 事件计算每个 Usage 字段的覆盖率，兼容旧日志缺少 call_id。
def event_metrics(root):
    sources, tokens, coverage = Counter(), Counter(), Counter()
    repairs = usage_calls = event_count = 0
    corrupt = False
    ended = False
    seen = set()
    started_calls, finished_calls = set(), set()
    correlated = True
    paths = list(root.glob("*/events.jsonl"))
    for path in paths:
        try:
            lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            corrupt = True
            continue
        for line in lines:
            try:
                event = json.loads(line)
                if not isinstance(event, dict) or not isinstance(event.get("payload", {}), dict):
                    raise ValueError("invalid event")
                if type(event.get("seq")) is int:
                    identity = (event.get("run_id"), event["seq"])
                    if identity in seen:
                        raise ValueError("duplicate event sequence")
                    seen.add(identity)
                kind = event["event_type"]
                event_count += 1
                if kind == "llm.started":
                    source = event.get("source", "other")
                    if not isinstance(source, str):
                        raise ValueError("invalid source")
                    call_id = event.get("call_id")
                    if isinstance(call_id, str):
                        if call_id in started_calls:
                            raise ValueError("duplicate LLM request")
                        started_calls.add(call_id)
                    else:
                        correlated = False
                    sources[source] += 1
                elif kind == "repair.started":
                    repairs += 1
                elif kind == "run.finished":
                    ended = True
                if kind == "llm.finished":
                    call_id = event.get("call_id")
                    if isinstance(call_id, str):
                        if call_id not in started_calls or call_id in finished_calls:
                            raise ValueError("unmatched or duplicate LLM result")
                        finished_calls.add(call_id)
                    else:
                        correlated = False
                    usage = event.get("payload", {}).get("usage")
                    if isinstance(usage, dict):
                        usage_calls += 1
                        for field in TOKEN_FIELDS:
                            value = usage.get(field)
                            if value is not None:
                                if not count(value):
                                    raise ValueError("invalid token count")
                                tokens[field] += value
                                coverage[field] += 1
            except (ValueError, TypeError, KeyError):
                corrupt = True
    return {
        "llm_calls_by_source": dict(sources) if event_count else None,
        "tokens": dict(tokens) if tokens else None,
        "repairs": repairs if event_count else None,
        "usage_calls": usage_calls,
        "usage_missing_calls": max(0, sum(sources.values()) - usage_calls),
        "token_usage_calls": {field: coverage[field] for field in TOKEN_FIELDS},
        "corrupt_events": corrupt or len(paths) > 1,
        "event_requests": sum(sources.values()),
        "events_finished": ended and len(paths) == 1,
        "call_ids_complete": correlated
        and len(started_calls) == len(finished_calls) == sum(sources.values()),
    }


def trace_metrics(run_dir, mode):
    root = Path(run_dir) / "trace"
    events = event_metrics(root)
    result = {**events, "trace_complete": False, "metric_basis": "events_lower_bound"}
    summaries = list(root.glob("*/summary.json"))
    if len(summaries) == 1:
        try:
            summary = read_json(summaries[0])
            metrics = summary["metrics"]
            sources = metrics["llm_calls_by_source"]
            if not isinstance(sources, dict) or any(not count(v) for v in sources.values()):
                raise ValueError("invalid requests")
            for key in ("repairs", "usage_calls", "usage_missing_calls"):
                if not count(metrics[key]):
                    raise ValueError("invalid metric")
            if metrics.get("tokens") is not None and (
                not isinstance(metrics["tokens"], dict)
                or any(not count(v) for v in metrics["tokens"].values())
            ):
                raise ValueError("invalid tokens")
            result.update(metrics)
            result.update(
                metric_basis="summary",
                trace_complete=not summary.get("trace_errors")
                and summary.get("termination") == "returned",
            )
        except (OSError, ValueError, TypeError, KeyError):
            pass
    requests = sum((result["llm_calls_by_source"] or {}).values())
    result["token_usage_complete"] = {
        field: result["trace_complete"]
        and not events["corrupt_events"]
        and events["events_finished"]
        and events["call_ids_complete"]
        and events["event_requests"] == requests
        and events["token_usage_calls"][field] == requests
        for field in TOKEN_FIELDS
    }
    if mode == "executor":
        result.update(
            llm_calls_by_source={},
            tokens={field: 0 for field in TOKEN_FIELDS},
            repairs=0,
            usage_calls=0,
            usage_missing_calls=0,
            token_usage_complete={field: True for field in TOKEN_FIELDS},
        )
    result["request_counts_complete"] = mode == "executor" or result["trace_complete"]
    return result
