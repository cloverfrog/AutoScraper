# 网络探索工具的有界反馈和提交证据；只测试已由浏览器获得的数据，不发起 API 请求。
"""Network discovery, bounded samples and exact-rule evidence."""

import json
import asyncio
from time import monotonic
from itertools import islice
from urllib.parse import urldefrag, urljoin, urlsplit

from autoscraper.observability.tracing import Sanitizer
from autoscraper.scraping.network import (
    NetworkError, JsonFieldError, at_path, endpoint, extract_json_record, matches, record_items,
)
from autoscraper.scraping.spec import NetworkSource, JsonPath
from browser_use.browser.session import BrowserSession
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


def rule_key(source, dedupe_by=(), start_url=""):
    return source.model_dump_json(), tuple(dedupe_by), urldefrag(str(start_url)).url


# 页面查询参数参与内部精确匹配，对外仅返回端点与不含值的查询键。
def page_diagnostic(url):
    from urllib.parse import parse_qsl
    return dict(url=endpoint(url)[:2048],
                query_keys=[key[:100] for key, _ in parse_qsl(urlsplit(url).query)][:20])


# 只有执行过最终规则的实际动作，才记录该动作窗口内的响应推进证据。
def pagination_action(source, page, baseline):
    return dict(pagination=source.pagination.model_dump(), page=urldefrag(page).url,
                baseline=baseline, end=baseline, destination=urldefrag(page).url)


# 提交反馈沿用网络测试的真实失败原因，避免 HTTP/来源错误被误导为字段路径修复。
def network_feedback(test):
    from autoscraper.workflow.validation import Feedback
    result = []
    for error in (test or {}).get("errors", []) or ["network_evidence_missing"]:
        if error in {"records_path_failure", "json_path_or_field_failure", "field_never_resolved"}:
            code = "json_path_failure"
        elif error == "pagination_evidence_missing":
            code = "network_progress_failure"
        elif error == "body_pending":
            code = "response_timeout"
        elif error in {"initial_page_mismatch", "initial_page_unverified", "invalid_dedupe_by",
                       "invalid_response_boundary"}:
            code = "invalid_spec"
        elif error == "empty_extraction":
            code = "empty_extraction"
        else:
            code = "response_failure"
        result.append(Feedback(code=code, message=error + ": " + (test or {}).get(
            "message", "See network_test for bounded page/response diagnostics and next steps.")))
    return result


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
                  "may become readable later. Use path (keys/indexes) to inspect a nested subtree. "
                  "Identical parameters return identical detail; use test_network to verify field paths.")
    async def inspect_network_response(response_id: int, path: JsonPath | None = None) -> ActionResult:
        entry = capture.buffer.entries.get(response_id)
        if entry is None:
            return output(dict(status="not_retained"))
        await capture.settle({response_id}, timeout_ms)
        data = entry.metadata()
        if entry.complete and not entry.error:
            try:
                # 不能通过指定敏感键路径绕过父级脱敏；路径本身也不回显。
                if any(isinstance(key, str) and key.lower().replace("_", "").replace("-", "")
                       in Sanitizer.CREDENTIAL_KEYS for key in (path or [])):
                    data["sample"] = "[redacted]"
                else:
                    data["sample"] = sample_output(at_path(entry.body, path or []))
                data["guidance"] = "Same id/path gives the same bounded detail; change path or use test_network."
            except NetworkError:
                data["error"] = "inspection_path_failure"
            except (ValueError, RecursionError):
                data["error"] = "invalid_json_values"
        return output(data)

    @tools.action(description="Test an exact NetworkSource rule against retained complete JSON responses. "
                  "url_pattern matches URL WITHOUT query/fragment; method is exact. records_path and "
                  "field.path are arrays of keys/nonnegative indexes (empty path selects root). "
                  "Pass the final top-level dedupe_by too. Returns response/extracted/new counts separately. "
                  "start_url must be the final candidate's listing/search page. dedupe_by must reference "
                  "declared output field names, not raw JSON keys. since_response_id starts a fresh "
                  "evidence window (exclusive); recapture the initial batch after that boundary. "
                  "Use advance_network for click/link or explore_scroll for exact pagination evidence.")
    async def test_network(source: NetworkSource, dedupe_by: list[str] | None = None,
                           start_url: str | None = None, since_response_id: int | None = None) -> ActionResult:
        source = NetworkSource.model_validate(source)
        checks = tools.submission
        start_url = start_url or (str(checks.last_candidate.start_url) if checks and checks.last_candidate
                                  else checks.listing_url if checks else "")
        initial_url = checks.actual_url(start_url) if checks else urldefrag(start_url).url
        dedupe_by = dedupe_by or []
        key = rule_key(source, dedupe_by, initial_url)
        # 每次重测都撤销同一规则旧成功；窗口只前进，后续新失败仍进入验证。
        capture.evidence.discard(key)
        if any(name not in source.fields for name in dedupe_by):
            return output(dict(status="failed", errors=["invalid_dedupe_by"],
                message="dedupe_by must reference declared output fields, not raw JSON keys. "
                        "Declare an id output field with its JSON path before using it for deduplication.",
                declared_fields=list(source.fields)))
        previous = capture.test_windows.get(key, 0)
        if since_response_id is not None and not previous <= since_response_id <= capture.buffer.sequence:
            return output(dict(status="failed", errors=["invalid_response_boundary"],
                               message="Boundary must move forward and not exceed the latest request id."))
        boundary = previous if since_response_id is None else since_response_id
        capture.test_windows[key] = boundary
        matching = [entry for entry in capture.buffer.entries.values()
                    if entry.id > boundary and matches(source, entry.url, entry.method)]
        actions = [action for action in capture.actions if isinstance(action, dict)
                   and source.pagination and action["pagination"] == source.pagination.model_dump()
                   and action["page"] == initial_url and action["baseline"] > boundary]
        allowed_pages = {initial_url, *(action["destination"] for action in actions)}
        entries = [entry for entry in matching if urldefrag(entry.page_url).url in allowed_pages]
        await capture.settle({entry.id for entry in entries}, timeout_ms)
        seen = set()
        extracted = added = 0
        batches = []
        errors = []
        documents = [entry.metadata() for entry in capture.json_documents
                     if matches(source, entry.url, entry.method)]
        if not matching:
            errors.append("no_matching_responses")
            if documents:
                errors.append("top_level_json_not_captured")
        elif not entries:
            errors.append("initial_page_mismatch")
        if capture.buffer.dropped and capture.buffer.entries and boundary < min(capture.buffer.entries) - 1:
            errors.append("response_limit")
        fields_found = {name: 0 for name in source.fields}
        diagnostics = []
        for entry in entries:
            before = added
            if not entry.complete or entry.error:
                errors.append(entry.error or "body_pending")
                if len(diagnostics) < 8:
                    diagnostics.append(dict(response_id=entry.id, status=entry.status,
                        error=entry.error or "body_pending", page=page_diagnostic(entry.page_url)))
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
        initial = any(batch["page"] == initial_url and batch["added"] for batch in batches)
        progress = source.pagination is None or any(
            any(boundary < batch["id"] <= action["baseline"] and batch["page"] == initial_url
                and batch["added"] for batch in batches)
            and any(action["baseline"] < batch["id"] <= action["end"] and batch["added"]
                    and batch["page"] == action["destination"] for batch in batches)
            for action in actions
        )
        if not initial and not errors:
            errors.append("initial_page_unverified")
        if not progress:
            errors.append("pagination_evidence_missing")
        if entries and not extracted and not any(error in errors for error in (
            "records_path_failure", "json_path_or_field_failure", "http_status", "body_pending",
            "not_json", "invalid_json", "body_unavailable", "request_failed", "body_limit",
        )):
            errors.append("empty_extraction")
        errors = list(dict.fromkeys(errors))
        signature = (key, boundary, tuple((entry.id, entry.complete, entry.error, entry.status,
                                           entry.page_url) for entry in matching),
                     json.dumps(actions, sort_keys=True), tuple(errors))
        repeated = signature == capture.last_test_signature
        capture.last_test_signature = signature
        if initial and added and progress and not errors:
            capture.evidence.add(key)
        return output(dict(status="passed" if key in capture.evidence else "failed",
                           responses=sum(entry.status > 0 for entry in entries),
                           extracted=extracted, added=added,
                           initial_page_verified=initial, pagination_verified=progress,
                           errors=errors, diagnostics=diagnostics, unchanged_evidence=repeated,
                           uncaptured_json_documents=documents,
                           expected_page=page_diagnostic(initial_url),
                           observed_pages=[page_diagnostic(url) for url in
                                           dict.fromkeys(entry.page_url for entry in matching)][:8],
                           since_response_id=boundary, latest_response_id=capture.buffer.sequence,
                           guidance=("Only Fetch/XHR responses are captured; top-level JSON navigation "
                                     "is not a supported source. For a fresh retry, pass since_response_id "
                                     "and recapture initial-page and pagination evidence. Identical rules "
                                     "and unchanged evidence cannot fix a deterministic failure."),
                           field_coverage={name: dict(found=count, missing=extracted-count)
                                           for name, count in fields_found.items()}))

    @tools.action(description="Perform the final network rule's exact click/link pagination control. "
                  "Use after test_network has verified the initial batch. Records a bounded action "
                  "window; arbitrary clicks/navigations do not verify this control. No API replay.")
    async def advance_network(browser_session: BrowserSession, source: NetworkSource) -> ActionResult:
        source = NetworkSource.model_validate(source)
        pagination = source.pagination
        if not pagination or pagination.type not in {"click", "link"}:
            return output(dict(status="failed", errors=["pagination_action_failure"]))
        page = await browser_session.must_get_current_page()
        url = await browser_session.get_current_page_url()
        action = pagination_action(source, url, capture.buffer.sequence)
        try:
            selector = pagination.next_selector
            if ">>" in selector:
                raise ValueError("CSS selector chains are unsupported")
            elements = await page.get_elements_by_css_selector(selector)
            if len(elements) != 1:
                raise ValueError("Pagination control must match exactly one element")
            # 和执行器一致：隐藏、原生禁用和 aria-disabled 控件不能提供分页证据。
            usable = await elements[0].evaluate("""() => {
                const style = getComputedStyle(this), rect = this.getBoundingClientRect();
                return JSON.stringify(!this.matches(':disabled') && this.getAttribute('aria-disabled') !== 'true'
                    && style.visibility !== 'hidden' && style.visibility !== 'collapse'
                    && rect.width > 0 && rect.height > 0);
            }""")
            if json.loads(usable) is not True:
                raise ValueError("Pagination control must be visible and enabled")
            if pagination.type == "link":
                href = await elements[0].get_attribute("href")
                destination = urljoin(url, href or "")
                if not href or urlsplit(destination).scheme not in {"http", "https"}:
                    raise ValueError("Pagination link must have an HTTP(S) href")
                await page.goto(destination)
            else:
                await elements[0].click()
            # 给延迟发起的 Fetch/XHR 留出有界观察窗口；旧在途请求无法跨越基线。
            deadline = monotonic() + timeout_ms / 1000
            last_sequence = capture.buffer.sequence
            quiet_since = monotonic()
            while monotonic() < deadline:
                received = [entry for entry in capture.buffer.entries.values()
                            if entry.id > action["baseline"] and matches(source, entry.url, entry.method)]
                if last_sequence != capture.buffer.sequence:
                    last_sequence = capture.buffer.sequence
                    quiet_since = monotonic()
                if received and all(entry.complete for entry in received) and monotonic() - quiet_since >= 0.25:
                    break
                await asyncio.sleep(min(0.025, max(0, deadline - monotonic())))
            action["end"] = capture.buffer.sequence
            action["destination"] = urldefrag(await browser_session.get_current_page_url()).url
            capture.actions.append(action)
            capture.actions = capture.actions[-40:]
            return output(dict(status="observed", baseline=action["baseline"],
                               latest_response_id=action["end"], guidance="Run test_network again."))
        except Exception:
            return output(dict(status="failed", errors=["pagination_action_failure"],
                               message="Exact CSS control could not be used; verify a unique enabled control."))


# 现有 explore_scroll 的网络分支：动作由调用方复用，状态不读取 DOM 记录数。
# 每轮在动作前固定请求序号；提取/判重沿用 test_network 的最终路径和去重键。
async def explore_network_scroll(tools, capture, page, source, dedupe_by, perform_scroll,
                                 max_scrolls, wait_ms, timeout_ms, get_page_url):
    from autoscraper.scraping.progress import STATE_SCRIPT, network_scroll_options

    options = network_scroll_options(source.pagination)
    test = tools.registry.registry.actions["test_network"].function

    deadline = monotonic() + timeout_ms / 1000
    initial_url = await get_page_url()

    # 只等待本次页面和请求窗口，其他页面的旧在途请求不能阻塞本次滚动。
    def active(entry):
        boundary = capture.test_windows.get(rule_key(source, dedupe_by, initial_url), 0)
        return (entry.id > boundary and urldefrag(entry.page_url).url == initial_url
                and matches(source, entry.url, entry.method))

    async def tested():
        # 测试与响应读取共用本工具剩余时间，不能逐次重置独立等待预算。
        remaining = max(0, deadline - monotonic())
        try:
            result = await asyncio.wait_for(test(source=source, dedupe_by=dedupe_by,
                                                start_url=initial_url), remaining)
            data = json.loads(result.extracted_content)
            data["errors"] = [error for error in data.get("errors", [])
                              if error != "pagination_evidence_missing"]
            return data
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
                             if active(entry))
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
        action_evidence = pagination_action(source, await get_page_url(), baseline)
        action = await perform_scroll()
        if action is not None and action.error:
            return {"network": True, "stop_reason": "pagination_action_failure"}
        used += 1
        started = monotonic()
        round_deadline = min(deadline, started + wait_ms / 1000)
        while True:
            state = json.loads(await page.evaluate(STATE_SCRIPT, options))
            pending = any(not entry.complete for entry in capture.buffer.entries.values()
                          if active(entry))
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
        action_evidence["end"] = capture.buffer.sequence
        action_evidence["destination"] = urldefrag(await get_page_url()).url
        capture.actions.append(action_evidence)
        capture.actions = capture.actions[-40:]
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
