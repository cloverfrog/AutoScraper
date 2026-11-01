# 网络探索工具的有界反馈和提交证据；只测试已由浏览器获得的数据，不发起 API 请求。
"""Network discovery, bounded samples and exact-rule evidence."""

import json
from itertools import islice
from urllib.parse import urldefrag

from autoscraper.observability.tracing import Sanitizer
from autoscraper.scraping.network import (
    NetworkError, endpoint, extract_json_record, matches, record_items,
)
from autoscraper.scraping.errors import ItemError
from autoscraper.scraping.spec import NetworkSource
from autoscraper.scraping.values import record_identity


# 先裁剪再脱敏；列表样本最多两个、层级最多四层、每层最多十二个键。
def bounded_sample(value, depth=0):
    if depth >= 4 and isinstance(value, (dict, list)):
        return {"type": type(value).__name__, "truncated": True}
    if isinstance(value, dict):
        result = {}
        for key, item in islice(value.items(), 12):
            key = str(key)
            sensitive = key.lower().replace("_", "").replace("-", "") in Sanitizer.CREDENTIAL_KEYS
            result[key[:100]] = "[redacted]" if sensitive else bounded_sample(item, depth + 1)
        return result
    if isinstance(value, list):
        return {"length": len(value), "items": [bounded_sample(item, depth + 1) for item in value[:2]]}
    if isinstance(value, str):
        return endpoint(value)[:100] if value.startswith(("http://", "https://")) else value[:100]
    return value


def sample_output(value):
    sample = Sanitizer().clean(bounded_sample(value))
    encoded = json.dumps(sample, ensure_ascii=False, allow_nan=False)
    return sample if len(encoded) <= 8000 else {"truncated": True, "reason": "sample_size_limit"}


def rule_key(source, dedupe_by=()):
    return source.model_dump_json(), tuple(dedupe_by)


def register_network_tools(tools, capture, timeout_ms):
    from browser_use import ActionResult

    def output(data):
        return ActionResult(extracted_content=json.dumps({"network": True, **data}, ensure_ascii=False))

    @tools.action(description="List bounded Fetch/XHR response metadata captured BEFORE initial navigation. "
                  "No new requests are sent. Query values, headers and request bodies are omitted. "
                  "Use inspect_network_response(id) for a bounded JSON sample, then test_network(source).")
    async def list_network_responses() -> ActionResult:
        return output(dict(responses=[entry.metadata() for entry in capture.buffer.entries.values()],
                           dropped=capture.buffer.dropped))

    @tools.action(description="Inspect one previously captured response id. Returns bounded, redacted JSON "
                  "structure/sample and total array lengths. Does not replay requests. Incomplete bodies "
                  "may become readable later; samples are truncated and do not prove full extraction.")
    async def inspect_network_response(response_id: int) -> ActionResult:
        entry = capture.buffer.entries.get(response_id)
        if entry is None:
            return output(dict(status="not_retained"))
        await capture.settle({response_id}, timeout_ms)
        data = entry.metadata()
        if entry.complete and not entry.error:
            try:
                data["sample"] = sample_output(entry.body)
            except (ValueError, RecursionError):
                data["error"] = "invalid_json_values"
        return output(data)

    @tools.action(description="Test an exact NetworkSource rule against retained complete JSON responses. "
                  "url_pattern matches URL WITHOUT query/fragment; method is exact. records_path and "
                  "field.path are arrays of keys/nonnegative indexes (empty path selects root). "
                  "Pass the final top-level dedupe_by too. Returns response/extracted/new counts separately. "
                  "Pagination evidence requires an "
                  "observed browser action and genuinely new deduplicated records, never DOM growth.")
    async def test_network(source: NetworkSource, dedupe_by: list[str] | None = None) -> ActionResult:
        source = NetworkSource.model_validate(source)
        # 改后重测失败必须移除旧证据，不能让过去的成功覆盖当前错误。
        dedupe_by = dedupe_by or []
        if any(name not in source.fields or not source.fields[name].required for name in dedupe_by):
            return output(dict(status="failed", errors=["invalid_dedupe_by"]))
        key = rule_key(source, dedupe_by)
        capture.evidence.discard(key)
        entries = [entry for entry in capture.buffer.entries.values()
                   if matches(source, entry.url, entry.method)]
        await capture.settle({entry.id for entry in entries}, timeout_ms)
        seen = set()
        extracted = added = 0
        batches = []
        errors = []
        for entry in entries:
            before = added
            if not entry.complete or entry.error:
                errors.append(entry.error or "body_pending")
                continue
            try:
                for item in record_items(entry.body, source):
                    record = extract_json_record(item, source)
                    extracted += 1
                    identity = record_identity(record, dedupe_by or list(source.fields))
                    if identity not in seen:
                        seen.add(identity)
                        added += 1
                batches.append(dict(id=entry.id, page=urldefrag(entry.page_url).url,
                                    added=added - before))
            except (NetworkError, ItemError):
                errors.append("json_path_or_field_failure")
        initial_url = tools.submission.actual_url(tools.submission.listing_url) if tools.submission else ""
        initial = any(batch["page"] == initial_url and batch["added"] for batch in batches)
        progress = source.pagination is None or any(
            kind == ("navigate" if source.pagination.type == "link" else "click")
            and any(batch["id"] <= baseline and batch["added"] for batch in batches)
            and any(batch["id"] > baseline and batch["added"] for batch in batches)
            for kind, baseline in capture.actions
        )
        if initial and added and progress and not errors:
            capture.evidence.add(key)
        return output(dict(status="passed" if key in capture.evidence else "failed",
                           responses=sum(entry.status > 0 for entry in entries),
                           extracted=extracted, added=added,
                           initial_page_verified=initial, pagination_verified=progress,
                           errors=errors[:5]))
