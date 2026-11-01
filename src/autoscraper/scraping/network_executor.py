# 网络数据源的批次执行：先监听再导航/点击，用提取和去重结果判断推进。
# DOM 仅提供分页控件，不参与网络记录数或进度判断。
"""Deterministic browser-driven JSON batches, without API replay."""

from time import monotonic
from urllib.parse import urldefrag

from playwright.sync_api import Error as PlaywrightError

from autoscraper.browser.network import SyncResponseCapture
from autoscraper.observability.tracing import emit
from .errors import ItemError, PaginationActionError, ScrapeError
from .filtering import evaluate_filters
from .network import (
    JsonPathError, NetworkError, NetworkProgressError, extract_json_record, record_items,
)
from .values import record_identity
from .progress import STATE_SCRIPT, network_scroll_options
from .scrolling import scroll_step


class ResponseTimeout(NetworkError):
    code = "response_timeout"


# 网络响应完成后控件可能仍在渲染；隐藏控件和缺失/禁用控件一样需要等待稳定。
def next_control(page, pagination):
    from .executor import next_button

    button = next_button(page, pagination.next_selector)
    return button if button is not None and button.is_visible() else None


# 每个批次的输入窗口以请求开始序号划分；完成读取不等于字段提取或数据新增。
def run_network(spec, page, *, result=None, resources=None):
    from .executor import ScrapeFailure, ScrapeResult, css, http_url, navigate

    source = spec.network
    result = result if result is not None else ScrapeResult(fields_found={name: 0 for name in spec.all_fields})
    seen = set()
    visited = set()
    page.set_default_timeout(spec.timeout_ms)
    next_url = str(spec.start_url)
    pagination = source.pagination
    is_scroll = pagination is not None and pagination.type == "scroll"
    try:
        options = network_scroll_options(pagination) if is_scroll else None
    except ValueError as exc:
        raise ScrapeError(str(exc)) from None
    capture = SyncResponseCapture(page, source).start()
    scrolls = unchanged = 0
    processed = set()
    try:
        while result.pages_visited < spec.max_pages:
            # 滚动间保留已观察但尚未提取的响应，先补收再发起新动作，避免丢掉在途数据。
            for key in processed:
                entry = capture.buffer.entries.pop(key, None)
                if entry is not None:
                    capture.buffer.total_bytes -= entry.size
            processed = set()
            if not is_scroll:
                capture.buffer.clear()
            initial_batch = result.pages_visited == 0
            reconcile = is_scroll and bool(capture.buffer.entries) and next_url is None
            baseline = (min(capture.buffer.entries) - 1) if reconcile else capture.buffer.sequence
            scrolled = False
            if reconcile:
                emit("network.reconcile", responses=len(capture.buffer.entries))
            elif next_url is not None:
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
            elif is_scroll:
                if scrolls >= pagination.max_scrolls:
                    result.stop_reason = "scroll_limit"
                    return result
                try:
                    scroll_step(page, pagination.container_selector)
                    scrolls += 1
                    scrolled = True
                except PlaywrightError:
                    raise PaginationActionError("Network pagination scroll failed") from None
            else:
                try:
                    css(page, source.pagination.next_selector).click(timeout=spec.timeout_ms)
                except PlaywrightError:
                    raise PaginationActionError("Network pagination click failed") from None
            result.pages_visited += 1
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
                    for record_index, item in enumerate(record_items(entry.body, source), 1):
                        try:
                            record = extract_json_record(item, source, fields_found=result.fields_found)
                        except ItemError as exc:
                            message = f"response={entry.id} record={record_index}: {exc}"
                            if spec.on_item_error == "fail":
                                raise JsonPathError(message) from None
                            result.errors.append(message)
                            result.error_details.append(ScrapeFailure(
                                code="json_path_failure", message=message, url=source.url_pattern,
                            ))
                            continue
                        extracted += 1
                        result.items_extracted += 1
                        # 推进按提取后的原始记录判重；被筛除的数据仍证明网站返回了新批次。
                        identity = record_identity(record, spec.dedupe_by or list(spec.all_fields))
                        duplicate = identity in seen
                        if duplicate:
                            result.duplicates += 1
                        else:
                            seen.add(identity)
                            added += 1
                        # 筛选统计覆盖每条成功提取记录，包括重复项，与 items_extracted 一致。
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
                        if duplicate:
                            continue
                        if resources:
                            resources.save_record(record, len(result.records) + 1, page)
                        result.records.append(record)
                        if len(result.records) >= spec.max_items:
                            result.stop_reason = "max_items"
                            emit("network.batch", responses=received, extracted=extracted, added=added)
                            return result
                # 收完已观察到的请求后保留短暂静默窗口，合并同一动作的多个响应。
                # 重复/空批次继续等满本批时限，不能仅凭新响应或按钮动画宣称推进。
                state = page.evaluate(STATE_SCRIPT, options) if is_scroll else None
                # 状态读取也会分发浏览器事件；结束前必须处理这期间新完成的响应。
                entries = [entry for entry in capture.buffer.entries.values() if entry.id > baseline]
                if any(entry.complete and entry.id not in processed for entry in entries):
                    continue
                now = monotonic()
                pending = any(not entry.complete for entry in entries)
                all_complete = bool(entries) and not pending
                busy = state is not None and state["loading"]
                if added and all_complete and not busy and now - last_completion >= 0.1:
                    # 响应静默不代表分页控件已经就绪；沿用本批截止时间，不另开等待预算。
                    if is_scroll or pagination is None:
                        break
                    ended = (
                        getattr(pagination, "end_selector", None)
                        and css(page, pagination.end_selector).is_visible()
                    )
                    if ended or next_control(page, pagination) is not None or now >= deadline:
                        break
                # 无请求或全重复的滚动只记为一次安静观察，允许继续滚动/重试。
                # 已观察到加载或在途请求时必须等到完成，不能将超时累计为启发式无新增。
                if is_scroll and (scrolls or reconcile) and not added and not busy and not pending:
                    if now - started >= 0.25:
                        break
                if now >= deadline:
                    if busy or pending or (not processed and not (is_scroll and scrolls)):
                        raise ResponseTimeout("No complete matching response before the batch deadline")
                    break
                page.wait_for_timeout(min(25, max(1, (deadline - now) * 1000)))
            emit("network.batch", responses=received, extracted=extracted, added=added)
            if is_scroll:
                # max_pages 统计数据批次；空滚动/重复响应只消耗独立的 max_scrolls。
                if not initial_batch and not added:
                    result.pages_visited -= 1
                if state["end"] and not state["loading"]:
                    result.stop_reason = "end_marker"
                    return result
                if result.pages_visited >= spec.max_pages:
                    result.stop_reason = "max_pages"
                    return result
                if added or not state["bottom"]:
                    unchanged = 0
                elif scrolled:
                    unchanged += 1
                # 明确结束优先；动作预算耗尽不能降格为启发式自然结束。
                if scrolls >= pagination.max_scrolls:
                    result.stop_reason = "scroll_limit"
                    return result
                if unchanged >= pagination.no_change_limit:
                    result.stop_reason = "no_more_content"
                    return result
                next_url = None
                emit("network.scroll", scrolls=scrolls, added=added, bottom=state["bottom"],
                     unchanged=unchanged, loading=state["loading"])
                continue
            terminal = None
            if pagination is None:
                terminal = "no_pagination"
            elif getattr(pagination, "end_selector", None) and css(
                page, pagination.end_selector
            ).is_visible():
                terminal = "end_marker"
            elif next_control(page, pagination) is None:
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
                button = next_control(page, pagination)
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
