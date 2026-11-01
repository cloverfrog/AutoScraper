# 网络数据源的批次执行：先监听再导航/点击，用提取和去重结果判断推进。
# DOM 仅提供分页控件，不参与网络记录数或进度判断。
"""Deterministic browser-driven JSON batches, without API replay."""

from time import monotonic
from urllib.parse import urldefrag

from playwright.sync_api import Error as PlaywrightError

from autoscraper.browser.network import SyncResponseCapture
from autoscraper.observability.tracing import emit
from .errors import ItemError, PaginationActionError
from .filtering import evaluate_filters
from .network import (
    JsonPathError, NetworkError, NetworkProgressError, extract_json_record, record_items,
)
from .values import record_identity


class ResponseTimeout(NetworkError):
    code = "response_timeout"


# 每个批次的输入窗口以请求开始序号划分；完成读取不等于字段提取或数据新增。
def run_network(spec, page):
    from .executor import ScrapeFailure, ScrapeResult, css, http_url, navigate, next_button

    source = spec.network
    result = ScrapeResult()
    seen = set()
    visited = set()
    capture = SyncResponseCapture(page, source).start()
    page.set_default_timeout(spec.timeout_ms)
    next_url = str(spec.start_url)
    try:
        while result.pages_visited < spec.max_pages:
            capture.buffer.clear()
            baseline = capture.buffer.sequence
            if next_url is not None:
                identity = urldefrag(next_url).url
                if identity in visited:
                    result.stop_reason = "pagination_cycle"
                    return result
                navigate(page, next_url, spec.timeout_ms)
                final = urldefrag(page.url).url
                if final in visited:
                    result.stop_reason = "pagination_cycle"
                    return result
                visited.update((identity, final))
            else:
                try:
                    css(page, source.pagination.next_selector).click(timeout=spec.timeout_ms)
                except PlaywrightError:
                    raise PaginationActionError("Network pagination click failed") from None
            result.pages_visited += 1
            processed = set()
            received = extracted = added = 0
            started = monotonic()
            deadline = started + (spec.content_timeout_ms or spec.timeout_ms) / 1000
            last_completion = started
            while True:
                if capture.buffer.dropped:
                    raise NetworkError("Matching response count exceeded the batch limit")
                entries = [entry for entry in capture.buffer.entries.values() if entry.id > baseline]
                received = sum(entry.status > 0 for entry in entries)
                for entry in entries:
                    if not entry.complete or entry.id in processed:
                        continue
                    processed.add(entry.id)
                    last_completion = monotonic()
                    if entry.error:
                        raise NetworkError(f"Matched response could not be read as JSON: {entry.error}")
                    for item in record_items(entry.body, source):
                        try:
                            record = extract_json_record(item, source)
                        except ItemError as exc:
                            if spec.on_item_error == "fail":
                                raise JsonPathError(str(exc)) from None
                            result.errors.append(str(exc))
                            result.error_details.append(ScrapeFailure(
                                code="json_path_failure", message=str(exc), url=source.url_pattern,
                            ))
                            continue
                        extracted += 1
                        result.items_extracted += 1
                        # 推进按提取后的原始记录判重；被筛除的数据仍证明网站返回了新批次。
                        identity = record_identity(record, spec.dedupe_by or list(spec.all_fields))
                        if identity in seen:
                            result.duplicates += 1
                            continue
                        seen.add(identity)
                        added += 1
                        decision = evaluate_filters(record, spec.filters)
                        if decision.matched is not True:
                            if decision.matched is False:
                                result.filtered_out += 1
                            else:
                                result.filter_unknown += 1
                                reason = decision.reason
                                result.filter_unknown_reasons[reason] = (
                                    result.filter_unknown_reasons.get(reason, 0) + 1
                                )
                            continue
                        result.records.append(record)
                        if len(result.records) >= spec.max_items:
                            result.stop_reason = "max_items"
                            emit("network.batch", responses=received, extracted=extracted, added=added)
                            return result
                # 收完已观察到的请求后保留短暂静默窗口，合并同一动作的多个响应。
                # 重复/空批次继续等满本批时限，不能仅凭新响应或按钮动画宣称推进。
                now = monotonic()
                all_complete = bool(entries) and all(entry.complete for entry in entries)
                if added and all_complete and now - last_completion >= 0.1:
                    break
                if now >= deadline:
                    if not processed or not all_complete:
                        raise ResponseTimeout("No complete matching response before the batch deadline")
                    break
                page.wait_for_timeout(min(25, max(1, (deadline - now) * 1000)))
            emit("network.batch", responses=received, extracted=extracted, added=added)
            pagination = source.pagination
            terminal = None
            if pagination is None:
                terminal = "no_pagination"
            elif getattr(pagination, "end_selector", None) and css(
                page, pagination.end_selector
            ).is_visible():
                terminal = "end_marker"
            elif next_button(page, pagination.next_selector) is None:
                terminal = "no_next"
            # 空数组只有明确结束条件才能自然结束；有记录但全部重复始终报告无推进。
            if not added and (extracted or not terminal):
                raise NetworkProgressError(
                    f"Network batch made no new records: responses={received}, "
                    f"extracted={extracted}, added={added}"
                )
            if terminal:
                result.stop_reason = terminal
                return result
            if result.pages_visited >= spec.max_pages:
                result.stop_reason = "max_pages"
                return result
            if pagination.type == "link":
                button = next_button(page, pagination.next_selector)
                href = button.get_attribute("href")
                if not href:
                    raise JsonPathError("Network link pagination requires a nonempty href")
                next_url = http_url(page.url, href)
            else:
                next_url = None
        result.stop_reason = "max_pages"
        return result
    finally:
        capture.close()
        emit("executor.result", items=len(result.records), pages=result.pages_visited,
             errors=len(result.errors), stop_reason=result.stop_reason)
