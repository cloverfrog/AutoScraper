# 网络探索工具的有界反馈和提交证据；只测试已由浏览器获得的数据，不发起 API 请求。
"""Network discovery, bounded samples and exact-rule evidence."""

import json
import asyncio
from time import monotonic
from itertools import islice
from urllib.parse import urldefrag

from autoscraper.observability.tracing import Sanitizer
from autoscraper.scraping.network import (
    NetworkError, JsonFieldError, endpoint, extract_json_record, matches, record_items,
)
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
        if any(name not in source.fields for name in dedupe_by):
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
        fields_found = {name: 0 for name in source.fields}
        diagnostics = []
        for entry in entries:
            before = added
            if not entry.complete or entry.error:
                errors.append(entry.error or "body_pending")
                continue
            try:
                items = record_items(entry.body, source)
            except NetworkError as exc:
                errors.append("records_path_failure")
                if len(diagnostics) < 8:
                    diagnostics.append(dict(response_id=entry.id, path=source.records_path,
                                            reason=str(exc)))
                continue
            for index, item in enumerate(items, 1):
                try:
                    record = extract_json_record(item, source, fields_found=fields_found)
                except JsonFieldError as exc:
                    if "json_path_or_field_failure" not in errors:
                        errors.append("json_path_or_field_failure")
                    if len(diagnostics) < 8:
                        diagnostics.append(dict(response_id=entry.id, record_index=index,
                            field=exc.field, path=exc.path, reason=exc.reason))
                    continue
                extracted += 1
                identity = record_identity(record, dedupe_by or list(source.fields))
                if identity not in seen:
                    seen.add(identity)
                    added += 1
            batches.append(dict(id=entry.id, page=urldefrag(entry.page_url).url,
                                added=added - before))
        requested = set(tools.submission.requirements.fields) if (
            tools.submission and tools.submission.requirements
        ) else set()
        for name, field in source.fields.items():
            if extracted and not fields_found[name] and (field.required or name in requested):
                errors.append("field_never_resolved")
                if len(diagnostics) < 8:
                    diagnostics.append(dict(field=name, path=field.path, reason="path_missing_in_all_records"))
        initial_url = tools.submission.actual_url(tools.submission.listing_url) if tools.submission else ""
        initial = any(batch["page"] == initial_url and batch["added"] for batch in batches)
        progress = source.pagination is None or any(
            kind == ("navigate" if source.pagination.type == "link" else source.pagination.type)
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
                           errors=errors[:5], diagnostics=diagnostics,
                           field_coverage={name: dict(found=count, missing=extracted-count)
                                           for name, count in fields_found.items()}))


# 现有 explore_scroll 的网络分支：动作由调用方复用，状态不读取 DOM 记录数。
# 每轮在动作前固定请求序号；提取/判重沿用 test_network 的最终路径和去重键。
async def explore_network_scroll(tools, capture, page, source, dedupe_by, perform_scroll,
                                 max_scrolls, wait_ms, timeout_ms):
    from autoscraper.scraping.progress import STATE_SCRIPT, network_scroll_options

    options = network_scroll_options(source.pagination)
    test = tools.registry.registry.actions["test_network"].function

    deadline = monotonic() + timeout_ms / 1000

    async def tested():
        # 测试与响应读取共用本工具剩余时间，不能逐次重置独立等待预算。
        remaining = max(0, deadline - monotonic())
        try:
            result = await asyncio.wait_for(test(source=source, dedupe_by=dedupe_by), remaining)
            return json.loads(result.extracted_content)
        except TimeoutError:
            return {"errors": ["response_timeout"]}

    initial = await tested()
    if initial.get("errors"):
        return {"network": True, "stop_reason": "response_timeout" if "response_timeout" in initial["errors"]
                else "network_test_failure", "test": initial}
    initial_count = initial["added"]
    current = initial
    dropped = capture.buffer.dropped
    used = unchanged = 0
    reason = "scroll_limit"
    while used < min(max_scrolls, source.pagination.max_scrolls):
        # 动作前先收完已观察到的正文，再固定基线；旧在途请求不能冒充本轮推进。
        current = await tested()
        if current.get("errors"):
            return {"network": True, "stop_reason": "response_timeout" if "response_timeout" in current["errors"]
                    else "network_test_failure", "test": current}
        state = json.loads(await page.evaluate(STATE_SCRIPT, options))
        pending_before = any(not entry.complete for entry in capture.buffer.entries.values()
                             if matches(source, entry.url, entry.method))
        if pending_before:
            continue
        if state["end"] and not state["loading"]:
            reason = "end_marker"
            break
        if monotonic() >= deadline:
            reason = "content_timeout"
            break
        baseline = capture.buffer.sequence
        before = current["added"]
        capture.actions.append(("scroll", baseline))
        capture.actions = capture.actions[-40:]
        action = await perform_scroll()
        if action is not None and action.error:
            return {"network": True, "stop_reason": "pagination_action_failure"}
        used += 1
        started = monotonic()
        round_deadline = min(deadline, started + wait_ms / 1000)
        while True:
            state = json.loads(await page.evaluate(STATE_SCRIPT, options))
            pending = any(not entry.complete for entry in capture.buffer.entries.values()
                          if matches(source, entry.url, entry.method))
            if capture.buffer.dropped != dropped:
                return {"network": True, "stop_reason": "response_limit", "scrolls": used}
            # 在等待循环内只测试已收完的正文，避免 test_network 的独立等待延长本轮预算。
            if not pending:
                current = await tested()
                if current.get("errors"):
                    return {"network": True, "stop_reason": "network_test_failure", "test": current}
                if not state["loading"] and (current["added"] > before or state["end"]):
                    break
                if not state["loading"] and monotonic() - started >= min(0.25, wait_ms / 1000):
                    break
            if monotonic() >= round_deadline:
                reason = "response_timeout" if pending else "content_timeout"
                return {"network": True, "stop_reason": reason, "scrolls": used,
                        "initial_count": initial_count, "count": current["added"]}
            await asyncio.sleep(min(0.025, max(0, round_deadline - monotonic())))
        added = current["added"] - before
        unchanged = unchanged + 1 if not added and state["bottom"] else 0
        if state["end"] and not state["loading"]:
            reason = "end_marker"
            break
        if used >= min(max_scrolls, source.pagination.max_scrolls):
            reason = "scroll_limit"
            break
        if unchanged >= source.pagination.no_change_limit:
            reason = "no_more_content"
            break
    return {"network": True, "scrolls": used, "initial_count": initial_count,
            "count": current["added"], "responses": current["responses"],
            "extracted": current["extracted"], "added": current["added"] - initial_count,
            "stop_reason": reason}
