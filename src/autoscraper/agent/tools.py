# Agent 工具注册与适配：限制可用动作，将浏览器操作和等待结果包装成工具反馈。
# DOM 检查由 dom.py 提供，分页状态脚本与执行器共享；内容等待模型由调用方注入。
"""Restricted browser-use tools and their tracing adapter."""

import json
import asyncio
from time import monotonic

from autoscraper.scraping.progress import STATE_SCRIPT, SCROLL_SCRIPT

from browser_use import ActionResult, Tools
from browser_use.browser.session import BrowserSession

from .clarification import ClarificationRequest, ClarificationSession
from .dom import INSPECT_SCRIPT, SELECTOR_SCRIPT, BATCH_SELECTOR_SCRIPT
from .submission import SubmissionChecks, page_key
from autoscraper.scraping.spec import FieldSpec, ScraperSpec
from .waiting import ContentWaiter
from autoscraper.observability.tracing import current_trace, operation, summarize_tool_output, emit


# 为 browser-use 工具分发增加追踪，保留原工具执行和返回语义。
class TracedTools(Tools):
    # 记录工具输入及经过摘要处理的反馈；未启用追踪时直接调用框架实现。
    async def act(self, action, **kwargs):
        if current_trace() is None:
            return await self._perform(action, **kwargs)
        params = action.model_dump(exclude_unset=True, mode="json")
        with operation("tool", tool=next(iter(params), "unknown"), inputs=params) as details:
            result = await self._perform(action, **kwargs)
            details.update(
                outcome="error" if result.error else "returned",
                error=result.error,
                is_done=result.is_done,
                success=result.success,
                output_chars=len(result.extracted_content or ""),
                feedback=summarize_tool_output(result.extracted_content),
            )
            return result

    # 提交前检查发生在真实 done 动作之前，拒绝后保留同一 Agent/浏览器继续修复。
    # 反馈作为普通工具结果返回，不用 error 消耗框架的连续失败上限。
    async def _perform(self, action, **kwargs):
        checks = getattr(self, "submission", None)
        params = {
            name: value
            for name, value in action.model_dump(exclude_unset=True, mode="json").items()
            if value is not None
        }
        if not params:
            return await super().act(action, **kwargs)
        name, inputs = next(iter(params.items()))
        if checks and name == "done" and inputs.get("success") is not False and inputs.get("data"):
            candidate = ScraperSpec.model_validate(inputs["data"])
            control = await self._refresh_control(candidate, kwargs.get("browser_session"))
            refreshed = await self._refresh_progress(candidate, kwargs.get("browser_session"))
            issues = checks.check(candidate)
            emit(
                "submission.checked",
                feedback=[issue.model_dump() for issue in issues],
                progression=refreshed,
                control=control,
            )
            checks.last_issues = issues
            if issues:
                return ActionResult(
                    extracted_content=json.dumps(
                        {
                            "status": "candidate_rejected",
                            "feedback": [issue.model_dump() for issue in issues],
                        },
                        ensure_ascii=False,
                    )
                )
        url = ""
        if checks and name in {"navigate", "click", "scroll", "explore_scroll"}:
            url = await kwargs["browser_session"].get_current_page_url()
            if name in {"click", "scroll"}:
                checks.begin_action(name, url)
        result = await super().act(action, **kwargs)
        if checks and not result.error:
            if name == "navigate":
                actual = await kwargs["browser_session"].get_current_page_url()
                checks.aliases[page_key(inputs["url"])] = page_key(actual)
            try:
                data = json.loads(result.extracted_content or "null")
            except (ValueError, TypeError):
                data = None
            if isinstance(data, dict):
                if name == "test_selector":
                    checks.remember(
                        data, inputs.get("field_selector", ""), inputs.get("attribute", "")
                    )
                elif name == "test_fields":
                    for field in data.get("results", {}).values():
                        checks.remember(
                            field, field.get("field_selector", ""), field.get("attribute", "")
                        )
                elif name == "wait_for_selector":
                    checks.remember_wait(data)
                elif name == "explore_scroll" and data.get("count", 0) > data.get(
                    "initial_count", 0
                ):
                    checks.advances.add(
                        (checks.actual_url(url), inputs["item_selector"], "scroll", "append")
                    )
        elif checks and name in {"click", "scroll"}:
            checks.pending = None
        return result

    # 分页控件只需精确 CSS 计数，提交时自动完成这类确定性检查，节省模型调用。
    # 只在当前列表页取证；无匹配或多匹配仍拒绝，不替模型寻找或修正选择器。
    async def _refresh_control(self, candidate, browser_session):
        pagination = candidate.pagination
        if not browser_session or not pagination or pagination.type not in {"click", "link"}:
            return None
        url = self.submission.actual_url(candidate.start_url)
        if page_key(await browser_session.get_current_page_url()) != url:
            return None
        attribute = "href" if pagination.type == "link" else ""
        previous = self.submission.result(url, pagination.next_selector, "", attribute)
        if self.submission.passed(previous, pagination.type == "link"):
            return None
        page = await browser_session.must_get_current_page()
        result = json.loads(
            await page.evaluate(SELECTOR_SCRIPT, pagination.next_selector, "", attribute)
        )
        self.submission.remember(result, "", attribute)
        return result

    # 模型可能漏掉计划中的等待。只对动作前已通过的同一列表规则自动复核，
    # 不替模型猜选择器或改数据来源；未推进时使用有界数量等待，仍不调用 LLM。
    async def _refresh_progress(self, candidate, browser_session):
        checks = self.submission
        pagination = candidate.pagination
        if not browser_session or not checks.pending or not pagination:
            return None
        kind, url, previous = checks.pending
        if kind != pagination.type or url != checks.actual_url(candidate.start_url):
            return None
        mode = getattr(pagination, "mode", "append")
        if (url, candidate.item_selector, kind, mode) in checks.advances:
            return None
        if page_key(await browser_session.get_current_page_url()) != url:
            return None
        # 只有动作前的精确字段证据全部有效，才允许自动刷新；改过的选择器必须先测试。
        prior = [
            previous.get((url, candidate.item_selector, field.selector, field.attribute or ""))
            for field in candidate.fields.values()
        ]
        if not all(
            checks.passed(result, field.required)
            for result, field in zip(prior, candidate.fields.values())
        ):
            return {"skipped": "listing_fields_not_verified_before_action"}
        page = await browser_session.must_get_current_page()
        data = json.loads(
            await page.evaluate(
                BATCH_SELECTOR_SCRIPT,
                candidate.item_selector,
                {name: field.model_dump() for name, field in candidate.fields.items()},
            )
        )
        for result in data["results"].values():
            checks.remember(result, result["field_selector"], result["attribute"])
        count = next(iter(data["results"].values()))["count"]
        if mode == "append" and (url, candidate.item_selector, kind, mode) not in checks.advances:
            waited = await self.content_waiter.selector(
                browser_session,
                candidate.item_selector,
                min_count=max(r["count"] for r in prior) + 1,
            )
            checks.remember_wait(waited)
            return waited
        return {"url": url, "count": count, "source": "deterministic_field_recheck"}


# 创建探索工具集合：保留导航和分页动作，再注册 DOM 检查、有界等待与澄清工具。
# 闭包共享等待器和澄清会话；工具说明中的英文字符串会实际发送给模型。
def build_tools(
    *,
    allow_unsupported: bool = False,
    content_timeout_ms: int = 30000,
    wait_llm=None,
    clarification: ClarificationSession | None = None,
    check_submission: bool = False,
    requirements=None,
) -> Tools:
    tools = TracedTools()
    tools.submission = SubmissionChecks(requirements) if check_submission else None
    # 只开放导航与分页探索相关动作；继续禁止输入、文件和任意代码执行。
    for name in list(tools.registry.registry.actions):
        if name not in {"navigate", "done", "click", "scroll"}:
            tools.registry.exclude_action(name)

    # 保存内置滚动入口：普通页面复用框架动作，自定义工具只补充记录检查和有界循环。
    builtin_scroll = tools.registry.registry.actions["scroll"].function

    # 一次 Agent 调用内完成多轮滚动、等待和记录数检查，减少每滚一次就消耗一个 Step。
    # 只返回探索证据和停止原因，正式去重抓取仍由 Executor 执行。
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
        # 总调用时限与单轮等待共同约束探索；不能因记录不断增长而重置总预算。
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
                # 内置动作按元素索引滚动；CSS 容器用共享的固定脚本补足精确定位。
                if container_selector:
                    await page.evaluate(SCROLL_SCRIPT, container_selector)
                else:
                    action = await builtin_scroll(
                        params=ScrollAction(down=True, pages=1), browser_session=browser_session
                    )
                    if action.error:
                        return ActionResult(error=action.error)
                used += 1
                # 单轮等待不能突破本次工具调用剩余的总时限。
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
                # 仅在到底、无新增且加载已结束时累积阈值；结果明确标记为启发式结束。
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
        description="Inspect DOM tags, classes and attributes for ALL matching CSS roots "
        "(up to 50). Reports root_count, sampled_roots and truncation. Use root=button to "
        "inspect all buttons; do not infer absence from a truncated sample."
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

    # 一次调用测试同一页面上的多字段，复用单字段的匹配和样本语义，减少探索 Step。
    # 详情页用 html 根；详情链接可作为额外的 attribute/href 规则一起检查。
    @tools.action(
        description="Test up to 50 named FieldSpec rules on the current page in ONE call. "
        "Returns current URL and per-field counts, exact selectors, values and status. "
        "Use listing item selector for listing fields/detail href, html for detail fields. "
        "Do not test listing rules while on a detail page or rename requested fields."
    )
    async def test_fields(
        browser_session: BrowserSession, item_selector: str, fields: dict[str, FieldSpec]
    ) -> ActionResult:
        if not 1 <= len(fields) <= 50:
            return ActionResult(
                extracted_content=json.dumps(
                    {"status": "invalid_parameters", "message": "Use 1–50 fields"}
                )
            )
        page = await browser_session.must_get_current_page()
        return ActionResult(
            extracted_content=await page.evaluate(
                BATCH_SELECTOR_SCRIPT,
                item_selector,
                {
                    name: FieldSpec.model_validate(field).model_dump()
                    for name, field in fields.items()
                },
            )
        )

    waiter = ContentWaiter(content_timeout_ms, wait_llm)
    tools.content_waiter = waiter

    # 已知 CSS 直接等待 min_count 条记录；点击追加前记录为 N 时，传 N+1 验证增长。
    # 返回真实初始/最终数量，语义等待不参与这种已知条件的判断。
    @tools.action(
        description="Wait for at least min_count CSS matches (default 1), then return actual "
        "initial_count/count and status (DOM only for default min_count=1). "
        "After Load More with N records, use min_count=N+1. "
        "Deterministic, no LLM; prefer this over semantic waiting for known record selectors. "
        "Bounded by content timeout; returns ready/timeout/error."
    )
    async def wait_for_selector(
        browser_session: BrowserSession, selector: str, timeout_ms: int | None = None,
        min_count: int = 1,
    ) -> ActionResult:
        return ActionResult(
            extracted_content=json.dumps(
                await waiter.selector(browser_session, selector, timeout_ms, min_count), ensure_ascii=False
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
