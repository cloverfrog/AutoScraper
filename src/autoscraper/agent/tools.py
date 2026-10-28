# Agent 工具注册与适配：限制可用动作，将浏览器操作和等待结果包装成工具反馈。
# 固定 DOM 脚本由 dom.py 提供，等待逻辑由 waiting.py 提供；模型由调用方注入。
"""Restricted browser-use tools and their tracing adapter."""

import json
import asyncio
from time import monotonic

from autoscraper.scraping.progress import STATE_SCRIPT, SCROLL_SCRIPT

from browser_use import ActionResult, Tools
from browser_use.browser.session import BrowserSession

from .clarification import ClarificationRequest, ClarificationSession
from .dom import INSPECT_SCRIPT, SELECTOR_SCRIPT
from .waiting import ContentWaiter
from autoscraper.observability.tracing import current_trace, operation, summarize_tool_output


# 为 browser-use 工具分发增加追踪，保留原工具执行和返回语义。
class TracedTools(Tools):
    # 记录工具输入及经过摘要处理的反馈；未启用追踪时直接调用框架实现。
    async def act(self, action, **kwargs):
        if current_trace() is None:
            return await super().act(action, **kwargs)
        params = action.model_dump(exclude_unset=True, mode="json")
        with operation("tool", tool=next(iter(params), "unknown"), inputs=params) as details:
            result = await super().act(action, **kwargs)
            details.update(
                outcome="error" if result.error else "returned",
                error=result.error,
                is_done=result.is_done,
                success=result.success,
                output_chars=len(result.extracted_content or ""),
                feedback=summarize_tool_output(result.extracted_content),
            )
            return result


# 创建当前探索可用的工具集合；先移除默认动作，再按参数注册只读辅助工具。
# 闭包共享等待器和澄清会话；工具说明中的英文字符串会实际发送给模型。
def build_tools(
    *,
    allow_unsupported: bool = False,
    content_timeout_ms: int = 30000,
    wait_llm=None,
    clarification: ClarificationSession | None = None,
) -> Tools:
    tools = TracedTools()
    # 只开放导航与分页探索相关动作；继续禁止输入、文件和任意代码执行。
    for name in list(tools.registry.registry.actions):
        if name not in {"navigate", "done", "click", "scroll"}:
            tools.registry.exclude_action(name)

    builtin_scroll = tools.registry.registry.actions["scroll"].function

    @tools.action(
        description="Explore infinite scroll with bounded scroll/wait/record-count checks. "
        "Use built-in scroll for plain scrolling; this tool checks loading/end CSS and "
        "record growth over multiple rounds in one step. Supports a CSS scroll container. "
        "Returns counts, scrolls and stop_reason. Does not extract the final dataset."
    )
    async def explore_scroll(
        browser_session: BrowserSession,
        item_selector: str,
        container_selector: str = "",
        loading_selector: str = "",
        end_selector: str = "",
        max_scrolls: int = 10,
        no_change_limit: int = 3,
        wait_ms: int = 1000,
    ) -> ActionResult:
        from browser_use.tools.views import ScrollAction

        if not (1 <= max_scrolls <= 30 and 2 <= no_change_limit <= 10
                and 1 <= wait_ms <= content_timeout_ms):
            return ActionResult(error="Invalid bounded scroll parameters")
        if not item_selector.strip() or any(">>" in value for value in (
            item_selector, container_selector, loading_selector, end_selector
        )):
            return ActionResult(error="Use nonempty CSS selectors without selector chains")
        page = await browser_session.must_get_current_page()
        options = dict(item_selector=item_selector, container_selector=container_selector,
                       loading_selector=loading_selector, end_selector=end_selector)
        used = 0
        unchanged = 0
        deadline = monotonic() + content_timeout_ms / 1000
        try:
            before = json.loads(await page.evaluate(STATE_SCRIPT, options))
            initial = before["count"]
            reason = "scroll_limit"
            while used < max_scrolls:
                if before["end"]:
                    reason = "end_marker"
                    break
                if monotonic() >= deadline:
                    reason = "content_timeout"
                    break
                if container_selector:
                    await page.evaluate(SCROLL_SCRIPT, container_selector)
                else:
                    action = await builtin_scroll(
                        params=ScrollAction(down=True, pages=1), browser_session=browser_session
                    )
                    if action.error:
                        return ActionResult(error=action.error)
                used += 1
                round_deadline = min(deadline, monotonic() + wait_ms / 1000)
                while True:
                    after = json.loads(await page.evaluate(STATE_SCRIPT, options))
                    changed = after["count"] > before["count"]
                    if not after["loading"] and (changed or after["end"]):
                        break
                    if monotonic() >= round_deadline:
                        break
                    await asyncio.sleep(min(0.05, max(0, round_deadline - monotonic())))
                if after["loading"] or monotonic() >= deadline:
                    reason = "content_timeout"
                    before = after
                    break
                unchanged = unchanged + 1 if not changed and after["bottom"] else 0
                before = after
                if after["end"]:
                    reason = "end_marker"
                    break
                if unchanged >= no_change_limit:
                    reason = "no_more_content"
                    break
            return ActionResult(extracted_content=json.dumps(dict(
                scrolls=used, initial_count=initial, count=before["count"], stop_reason=reason
            )))
        except Exception as exc:
            return ActionResult(error=f"Scroll exploration failed: {exc}")

    if clarification is not None:

        # 登记澄清请求并结束当前动作批次；真正的人机问答在 Agent 运行循环外进行。
        @tools.action(
            description="Ask the user to clarify a material ambiguity in task intent. "
            "Explain observed evidence, alternative plans and uncertainties in explanation. "
            "Use optional suggestions, never assume a default. Stops this action batch; "
            "wait for the user's answer before executing a dependent plan."
        )
        async def ask_user(
            question: str, explanation: str, options: list[str] | None = None
        ) -> ActionResult:
            clarification.pending = ClarificationRequest(
                question=question, explanation=explanation, options=options or []
            )
            return ActionResult(
                is_done=True,
                success=False,
                extracted_content=json.dumps(
                    {
                        "status": "needs_clarification",
                        "request": clarification.pending.model_dump(),
                    },
                    ensure_ascii=False,
                ),
            )

    # 读取当前页面或指定 CSS 根节点的 DOM 摘要，限制返回长度以控制上下文大小。
    @tools.action(
        description="Inspect DOM tags, classes and attributes. Optional CSS root narrows output."
    )
    async def inspect_page(
        browser_session: BrowserSession, root_selector: str = ""
    ) -> ActionResult:
        page = await browser_session.must_get_current_page()
        content = await page.evaluate(INSPECT_SCRIPT, root_selector)
        return ActionResult(extracted_content=content[:30000])

    # 测试原样传入的 CSS：记录总数、逐条字段匹配数及少量样本值。
    # 样本通过只说明匹配数量符合预期，不保证所有记录或值的语义正确。
    @tools.action(
        description="Test CSS item selector and optional field selector relative to each item. "
        "Echo inputs; return invalid_css (error_parameter), no_items, sample_passed or sample_failed. "
        "count counts records; each sample's matches/status reports zero/one/multiple field matches. "
        "Test up to 5 records (tested_count/all_items_tested); sample_passed checks uniqueness only. "
        "Inspect values too: attributes may be null or text empty. attribute optionally selects "
        "an attribute; empty field_selector selects the record itself."
    )
    async def test_selector(
        browser_session: BrowserSession,
        item_selector: str,
        field_selector: str = "",
        attribute: str = "",
    ) -> ActionResult:
        page = await browser_session.must_get_current_page()
        return ActionResult(
            extracted_content=await page.evaluate(
                SELECTOR_SCRIPT, item_selector, field_selector, attribute
            )
        )

    waiter = ContentWaiter(content_timeout_ms, wait_llm)

    # 将已知 CSS 的等待结果序列化为 Agent 可读取的工具反馈。
    @tools.action(
        description="Wait for a known CSS selector to attach, then return DOM. "
        "Bounded by content timeout; returns ready/timeout/error."
    )
    async def wait_for_selector(
        browser_session: BrowserSession, selector: str, timeout_ms: int | None = None
    ) -> ActionResult:
        return ActionResult(
            extracted_content=json.dumps(
                await waiter.selector(browser_session, selector, timeout_ms), ensure_ascii=False
            )
        )

    # 用描述性的成功条件等待未知动态内容，复用同一语义模型预算。
    @tools.action(
        description="Wait for unknown dynamic content using semantic success_criteria. "
        "Checks DOM immediately then every interval_seconds (0.1–10), at most "
        "max_checks (1–30). Separate limited LLM calls on changed DOM only; "
        "bounded by content timeout. Returns DOM and ready/timeout/error status."
    )
    async def wait_for_content(
        browser_session: BrowserSession,
        success_criteria: str,
        interval_seconds: float = 1.0,
        max_checks: int = 10,
    ) -> ActionResult:
        return ActionResult(
            extracted_content=json.dumps(
                await waiter.content(
                    browser_session, success_criteria, interval_seconds, max_checks
                ),
                ensure_ascii=False,
            )
        )

    if allow_unsupported:

        # 明确结束不受协议支持的任务，返回独立失败对象，避免冒充不完整的抓取规则。
        @tools.action(
            description="Finish instead of done when required fields/capabilities cannot fit "
            "ScraperSpec. Give observed evidence in reason and affected field names in fields."
        )
        async def report_unsupported(reason: str, fields: list[str]) -> ActionResult:
            from autoscraper.workflow.validation import UnsupportedResult

            output = UnsupportedResult(reason=reason, unsupported_fields=fields)
            return ActionResult(
                is_done=True, success=False, extracted_content=output.model_dump_json()
            )

    return tools
