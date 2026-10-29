# 确定性抓取执行器：读取 ScraperSpec，用 Playwright 按 CSS 规则提取数据。
# 执行顺序是列表导航 → 逐条提取 → 可选详情补充 → 去重 → 下一页，整个过程不调用 LLM。
# 导航/内容等待失败与规则错误分别报告，供工作流决定是否值得让 Agent 修复。
"""Deterministic CSS-only execution. Never evaluates rule-provided code."""

from dataclasses import dataclass, field
from time import monotonic
from typing import Literal
from urllib.parse import urldefrag, urljoin, urlsplit

from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import Locator, Page, TimeoutError as PlaywrightTimeout, sync_playwright
from pydantic import HttpUrl, TypeAdapter

from .spec import FieldSpec, ScraperSpec
from .progress import STATE_SCRIPT, state_options, progressed
from .scrolling import scroll_step
from autoscraper.observability.tracing import emit, operation


# 抓取致命错误的基类；工作流捕获后转换为结构化验证反馈。
class ScrapeError(RuntimeError):
    """Fatal navigation, selector, pagination or extraction error."""


# 点击或滚动动作本身失败，与动作成功但列表未更新的内容超时分开反馈。
class PaginationActionError(ScrapeError):
    code = "pagination_action_failure"


# 单条记录的提取失败；由 on_item_error 决定跳过该条还是终止抓取。
class ItemError(ScrapeError):
    """An individual item could not be extracted."""


# 导航失败，保留目标 URL；网络或 HTTP 错误不能直接证明 CSS 规则有误。
class NavigationError(ItemError):
    """A navigation failed, without evidence that extraction selectors are wrong."""

    code = "navigation_failure"

    # 保存失败 URL 并附加可读诊断，供异常转换和最终报告使用。
    def __init__(self, url: str, message: str):
        self.url = url
        super().__init__(f"Navigation failed at {url}: {message}")


# 导航超过时限，与内容未出现的超时分开标记。
class NavigationTimeout(NavigationError):
    code = "navigation_timeout"


# 页面或详情字段在等待期限内未出现；工作流不会据此要求重写选择器。
class ContentTimeout(ItemError):
    """Missing content within the deadline is not evidence of invalid CSS."""

    code = "content_timeout"

    # 保留内容等待失败的位置，避免只剩一段无法定位的异常文本。
    def __init__(self, url: str, message: str):
        self.url = url
        super().__init__(message)


# 等待第一个匹配节点挂载到 DOM；只要求 attached，不要求节点可见。
# timeout_ms=None 时沿用页面默认超时，超时后转为业务异常 ContentTimeout。
def wait_for_content(locator: Locator, url: str, timeout_ms: int | None):
    try:
        locator.first.wait_for(state="attached", timeout=timeout_ms)
    except PlaywrightTimeout as exc:
        raise ContentTimeout(url, f"Content did not appear at {url}: {exc}") from exc


# 结构化的单条错误诊断，和 errors 中的兼容文本一起返回。
@dataclass
class ScrapeFailure:
    """Typed diagnostic accompanying a legacy string in ScrapeResult.errors."""

    code: Literal["navigation_failure", "navigation_timeout", "content_timeout", "selector_failure"]
    message: str
    url: str


# 执行器停止原因的静态取值约束；保留空字符串作为初始值，避免改变异常路径的报告。
# 此处只限制类型提示，不增加运行时校验，也不改变已有 JSON 中的字符串值。
ScrapeStopReason = Literal[
    "",                 # 尚未正常记录停止原因，例如执行中途发生异常。
    "max_items",        # 已达到记录数量上限。
    "max_pages",        # 列表页访问预算耗尽。
    "detail_limit",     # 详情导航尝试预算耗尽。
    "pagination_cycle",  # 下一页链接或重定向重复访问已处理页面。
    "no_next",          # 下一页链接缺失或已禁用。
    "end_marker",       # 站点声明的结束标志可见。
    "no_more_content",  # 到底后连续多轮无新增，属于启发式结束。
    "scroll_limit",     # 包括无新增重试在内的滚动动作预算耗尽。
    "no_pagination",    # 规则未声明分页，当前列表页处理完毕。
]


# 执行结果：记录、错误、访问计数和停止原因；有数据并不等于任务完整完成。
@dataclass
class ScrapeResult:
    records: list[dict[str, str | None]] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    pages_visited: int = 0
    detail_pages_visited: int = 0
    duplicates: int = 0
    # max_items/no_next/no_pagination 为数量或自然结束；max_pages/detail_limit 为预算截断。
    # pagination_cycle 表示重复分页链接，工作流将其视为规则错误。
    stop_reason: ScrapeStopReason = ""
    error_details: list[ScrapeFailure] = field(default_factory=list)


# 导航到 DOMContentLoaded，并在操作边界区分超时、浏览器错误和 HTTP 失败。
def navigate(page: Page, url: str, timeout_ms: int):
    """Classify errors at the operation boundary, not by matching error text."""
    try:
        response = page.goto(url, wait_until="domcontentloaded", timeout=timeout_ms)
    except PlaywrightTimeout as exc:
        raise NavigationTimeout(url, str(exc)) from exc
    except PlaywrightError as exc:
        raise NavigationError(url, str(exc)) from exc
    if response is not None and response.status >= 400:
        raise NavigationError(url, f"HTTP {response.status} at {page.url}")
    return response


# 基于当前页面补全相对链接，并验证目标是合法 HTTP(S) URL。
def http_url(base: str, value: str) -> str:
    try:
        if urlsplit(value).scheme in {"http", "https"}:
            TypeAdapter(HttpUrl).validate_python(value)
        url = urljoin(base, value)
        if urlsplit(url).scheme not in {"http", "https"}:
            raise ItemError(f"Unsupported URL scheme: {url!r}")
        TypeAdapter(HttpUrl).validate_python(url)
    except ValueError as exc:
        raise ItemError(f"Invalid HTTP(S) URL: {value!r}") from exc
    return url


# 统一限定 CSS 引擎，拒绝通过 Playwright 的 >> 链切换到其他选择器语法。
def css(root: Page | Locator, selector: str) -> Locator:
    # 阻止 Playwright 选择器链切换引擎，确保协议始终按 CSS 解释。
    if ">>" in selector:
        raise ScrapeError("Selector chains are not supported; use a CSS selector")
    return root.locator(f"css={selector}")


# 在给定记录或详情页面内提取字段：每个字段至多匹配一个元素。
# 必填值缺失/为空或匹配歧义会报错；可选值缺失记为 None，文本统一压缩空白。
def extract_fields(
    item: Page | Locator, fields: dict[str, FieldSpec], base_url: str, *, wait: bool = False
) -> dict[str, str | None]:
    record = {}
    for name, rule in fields.items():
        matches = css(item, rule.selector)
        count = matches.count()  # 即使字段可选，CSS 语法非法也属于致命规则错误。
        if wait and rule.required and count == 0:
            wait_for_content(matches, base_url, None)
        if matches.count() > 1:
            raise ItemError(f"Field {name!r} matched multiple elements")
        value = None
        if matches.count():
            value = (
                matches.inner_text()
                if rule.extract == "text"
                else matches.get_attribute(rule.attribute)
            )
            if value is not None:
                value = " ".join(value.split())
        if not value:
            if rule.required:
                raise ItemError(f"Required field {name!r} is missing or empty")
            value = None
        if value is not None and rule.transform == "absolute_url":
            value = http_url(base_url, value)
        record[name] = value
    return record


# 提取列表记录的字段；详情字段由后续独立页面补充。
def extract_item(item: Locator, spec: ScraperSpec, base_url: str) -> dict[str, str | None]:
    return extract_fields(item, spec.fields, base_url)


# 打开一条详情链接，按重定向后的 URL 处理相对链接，并等待必填字段。
def extract_detail(page: Page, url: str, spec: ScraperSpec) -> dict[str, str | None]:
    navigate(page, url, spec.timeout_ms)
    http_url(page.url, page.url)
    try:
        return extract_fields(page, spec.detail.fields, page.url, wait=True)
    except PlaywrightTimeout as exc:
        raise ContentTimeout(url, f"Detail field extraction timed out: {exc}") from exc


# 统一检查下一页控件的唯一性和可用性；缺失/禁用返回 None，匹配歧义仍是规则错误。
# 动态分页动作后的暂时不可用由调用方等待确认，不能立即当作结束。
def next_button(page, selector):
    button = css(page, selector)
    if button.count() == 0:
        return None
    if button.count() != 1:
        raise ScrapeError("Next selector must match exactly one element")
    if button.is_disabled() or button.get_attribute("aria-disabled") == "true":
        return None
    return button


# 推进一个动态列表批次，返回 (停止原因或 None, 本次消耗的滚动次数)。
# 点击只执行一次；滚动允许无新增重试，但每次都消耗独立动作预算。
def advance_dynamic(page, spec, options, before, scrolls):
    """Advance one batch; unsuccessful scrolls consume their own bounded budget."""
    pagination = spec.pagination
    is_scroll = pagination.type == "scroll"
    used = 0
    unchanged = 0
    while True:
        if is_scroll and scrolls + used >= pagination.max_scrolls:
            return "scroll_limit", used
        try:
            if is_scroll:
                position_before = page.evaluate(STATE_SCRIPT, options)
                scroll_step(page, pagination.container_selector)
                used += 1
                position_after = page.evaluate(STATE_SCRIPT, options)
                emit("pagination.scroll", scroll=scrolls + used,
                     before_top=position_before["scroll_top"],
                     after_top=position_after["scroll_top"],
                     count=position_after["count"], bottom=position_after["bottom"],
                     phase="waiting" if position_after["bottom"] or position_after["loading"]
                     else "moving")
            else:
                css(page, pagination.next_selector).click(timeout=spec.timeout_ms)
        except PlaywrightError as exc:
            raise PaginationActionError(f"Pagination action failed at {page.url}: {exc}") from exc
        # 每次动作有独立内容等待时限；翻页失败不受单条记录的 skip 策略豁免。
        deadline = monotonic() + (spec.content_timeout_ms or spec.timeout_ms) / 1000
        loading_observed = False
        while True:
            after = page.evaluate(STATE_SCRIPT, options)
            loading_observed = loading_observed or after["loading"]
            # 最后一批数据可能与结束标志同时到达，优先返回推进，让外层先提取这批记录。
            if not after["loading"]:
                if progressed(before, after, getattr(pagination, "mode", "append")):
                    emit("pagination.advance", type=pagination.type, before=before["count"],
                         after=after["count"], scrolls=used)
                    return None, used
                if after["end"]:
                    return "end_marker", used
            # 未到底且尚未观察到加载时继续短间隔推进，不能在半途等满内容超时。
            # 一旦到底或出现加载标志，就保留完整等待时限，避免跳过慢响应。
            if is_scroll and not after["bottom"] and not loading_observed:
                break
            if monotonic() >= deadline:
                break
            page.wait_for_timeout(min(50, max(1, (deadline - monotonic()) * 1000)))
        # 请求期间控件可能暂时消失或禁用；等完内容时限后，才能据此判断无下一批。
        if (not is_scroll and not after["loading"]
                and next_button(page, pagination.next_selector) is None):
            return "no_next", used
        if not is_scroll or after["loading"]:
            raise ContentTimeout(page.url, "Pagination produced no updated records before timeout")
        # 尚未到底时无新增不能证明结束；加载标志持续可见则已在上方按超时处理。
        unchanged = unchanged + 1 if after["bottom"] else 0
        if unchanged >= pagination.no_change_limit:
            return "no_more_content", used


# 规则执行入口；run 自行管理浏览器，run_page 可复用调用方已有页面。
class ScraperExecutor:
    # 浏览器启动配置属于本次运行，不写入可复用的 ScraperSpec。
    # 默认无头运行；工作流会传入与 Agent 探索相同的配置。
    def __init__(self, *, headless: bool = True, executable_path: str | None = None):
        self.headless = headless
        self.executable_path = executable_path

    # 执行规则并记录总体追踪指标；真正的浏览器创建与回收交给 _run。
    def run(self, spec: ScraperSpec) -> ScrapeResult:
        with operation("executor", start_url=str(spec.start_url)) as details:
            result = self._run(spec)
            details.update(items=len(result.records), pages=result.pages_visited,
                           detail_pages=result.detail_pages_visited, errors=len(result.errors),
                           stop_reason=result.stop_reason,
                           error_codes=[error.code for error in result.error_details])
            return result

    # 创建浏览器和列表页面，执行结束或异常退出时统一关闭浏览器。
    def _run(self, spec: ScraperSpec) -> ScrapeResult:
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(
                headless=self.headless, executable_path=self.executable_path
            )
            try:
                context = browser.new_context()
                page = context.new_page()
                return self.run_page(spec, page)
            finally:
                browser.close()

    # 串行处理列表、详情和分页，维护链接循环检测、记录去重及列表/详情/滚动预算。
    # 调用方拥有列表 Page，本函数只负责关闭自己创建的详情 Page。
    def run_page(self, spec: ScraperSpec, page: Page) -> ScrapeResult:
        """Accept a caller-owned page; detail rules need an explicit BrowserContext."""
        result = ScrapeResult()
        seen_records: set[tuple] = set()
        seen_pages: set[str] = set()
        page.set_default_timeout(spec.timeout_ms)
        next_url = str(spec.start_url)
        detail_page = None
        scrolls = 0
        try:
            # 动态批次的状态读取参数固定不变；提取前后必须使用同一组记录信号比较。
            options = None
            if spec.pagination and spec.pagination.type != "link":
                try:
                    options = state_options(spec)
                except ValueError as exc:
                    raise ScrapeError(str(exc)) from exc
            # 追加和滚动保留旧节点，记录已经处理的前缀；替换和链接分页每批仍从头提取。
            append_only = options is not None and getattr(spec.pagination, "mode", "append") == "append"
            processed_count = 0
            if spec.detail:
                # 详情使用同一 BrowserContext 的独立 Page，保留会话且不打断列表页面。
                detail_page = page.context.new_page()
                detail_page.set_default_timeout(spec.content_timeout_ms or spec.timeout_ms)

            while result.pages_visited < spec.max_pages:
                # 只有初始页和链接分页需要导航及 URL 循环检测；动态分页复用当前 Page。
                if next_url is not None:
                    identity = urldefrag(next_url).url
                    if identity in seen_pages:
                        result.stop_reason = "pagination_cycle"
                        break
                    navigate(page, next_url, spec.timeout_ms)
                    final_identity = urldefrag(page.url).url
                    if final_identity in seen_pages:
                        result.stop_reason = "pagination_cycle"
                        break
                    seen_pages.update((identity, final_identity))
                next_url = None
                result.pages_visited += 1
                items = css(page, spec.item_selector)
                wait_for_content(items, page.url, spec.content_timeout_ms or spec.timeout_ms)
                # 范围与比较基线来自同一份提取前快照，不能将提取期间到达的数据算作已处理。
                batch_state = page.evaluate(STATE_SCRIPT, options) if options else None
                batch_count = batch_state["count"] if batch_state is not None else items.count()
                start_index = processed_count if append_only else 0
                for index in range(start_index, batch_count):
                    detail_url = None
                    try:
                        item = items.nth(index)
                        record = extract_item(item, spec, page.url)
                        keys = spec.dedupe_by or list(spec.all_fields)
                        # 若去重键已完全包含在列表字段里，可提前跳过，节省详情页访问预算。
                        if all(key in record for key in keys):
                            if tuple(record[key] for key in keys) in seen_records:
                                result.duplicates += 1
                                continue
                        if spec.detail:
                            link = css(item, spec.detail.url_selector)
                            if link.count() != 1:
                                raise ItemError("Detail link must match exactly one element")
                            href = link.get_attribute("href")
                            if not href or not href.strip():
                                raise ItemError("Detail link requires a nonempty href")
                            detail_url = http_url(page.url, href.strip())
                            if result.detail_pages_visited >= spec.detail.max_pages:
                                result.stop_reason = "detail_limit"
                                return result
                            # 先扣预算再导航；失败的详情请求也计入尝试次数。
                            result.detail_pages_visited += 1
                            record.update(extract_detail(detail_page, detail_url, spec))
                    except ItemError as exc:
                        message = f"{page.url} item {index + 1} detail={detail_url!r}: {exc}"
                        if spec.on_item_error == "fail":
                            if isinstance(exc, (NavigationError, ContentTimeout)):
                                raise type(exc)(exc.url, message) from exc
                            raise ScrapeError(message) from exc
                        result.errors.append(message)
                        result.error_details.append(
                            ScrapeFailure(
                                code=(
                                    exc.code
                                    if isinstance(exc, (NavigationError, ContentTimeout))
                                    else "selector_failure"
                                ),
                                message=message,
                                url=exc.url
                                if isinstance(exc, (NavigationError, ContentTimeout))
                                else page.url,
                            )
                        )
                        continue
                    keys = spec.dedupe_by or list(spec.all_fields)
                    # 去重键含详情字段时，只能在合并后判重；仅完整记录才加入结果集合。
                    identity_record = tuple(record[key] for key in keys)
                    if identity_record in seen_records:
                        result.duplicates += 1
                        continue
                    seen_records.add(identity_record)
                    result.records.append(record)
                    if len(result.records) >= spec.max_items:
                        result.stop_reason = "max_items"
                        return result
                if append_only:
                    # 去重或按策略跳过的节点也已处理，不应在下一批隐式重试或再扣详情预算。
                    # 只推进到提取前的数量，提取期间自动到达的新节点留给下一批核对。
                    processed_count = batch_count
                if spec.pagination is None:
                    result.stop_reason = "no_pagination"
                    break
                pagination = spec.pagination
                if pagination.type == "link":
                    button = next_button(page, pagination.next_selector)
                    if button is None:
                        result.stop_reason = "no_next"
                        break
                    href = button.get_attribute("href")
                    if not href:
                        raise ScrapeError("Link pagination requires a Next link with href")
                    next_url = http_url(page.url, href)
                else:
                    before = page.evaluate(STATE_SCRIPT, options)
                    # 慢提取（尤其详情访问）期间也可能自动追加或替换列表。先处理新批次，
                    # 再认可结束标志/控件状态或建立下一次动作的基线，避免最后一批被静默丢弃。
                    # 返回外层循环仍消耗 max_pages；达到预算时保留截断状态，不冒充自然完成。
                    if progressed(batch_state, before, getattr(pagination, "mode", "append")):
                        emit("pagination.reconcile", type=pagination.type,
                             before=batch_state["count"], after=before["count"])
                        continue
                    # 先检查明确结束状态，再判断批次预算；预算耗尽后不再触发下一次动作。
                    # 无新增滚动不计入已处理批次，仍由 advance_dynamic 的动作预算约束。
                    if before["end"]:
                        result.stop_reason = "end_marker"
                        break
                    if pagination.type == "click":
                        if next_button(page, pagination.next_selector) is None:
                            result.stop_reason = "no_next"
                            break
                    if result.pages_visited >= spec.max_pages:
                        result.stop_reason = "max_pages"
                        break
                    reason, used = advance_dynamic(page, spec, options, before, scrolls)
                    scrolls += used
                    if reason:
                        result.stop_reason = reason
                        break
            else:
                result.stop_reason = "max_pages"
        except PlaywrightTimeout as exc:
            raise ContentTimeout(
                page.url, f"Content operation timed out at {page.url}: {exc}"
            ) from exc
        except PlaywrightError as exc:
            raise ScrapeError(f"Browser failure at {page.url}: {exc}") from exc
        finally:
            emit("executor.result", items=len(result.records), pages=result.pages_visited,
                 detail_pages=result.detail_pages_visited, errors=len(result.errors),
                 stop_reason=result.stop_reason,
                 error_codes=[error.code for error in result.error_details])
            if detail_page is not None:
                detail_page.close()
        return result
