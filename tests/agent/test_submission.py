# 探索工具与提交证据回归：用真实离线 DOM 检查多根、批量字段及页面范围约束。
# 提交门槛只验证采样证据，不把它当作全量数据或自然语言语义的证明。
import asyncio
import json
from types import SimpleNamespace

import pytest
from playwright.sync_api import sync_playwright

from autoscraper.agent.dom import INSPECT_SCRIPT, SELECTOR_SCRIPT, BATCH_SELECTOR_SCRIPT
from autoscraper.agent.submission import SubmissionChecks
from autoscraper.scraping.spec import ScraperSpec
from autoscraper.workflow.requirements import TaskRequirements


# 为证据测试提供真实 CSS 引擎；不同页面通过 URL 区分，而非依靠测试名称标记。
@pytest.fixture
def page():
    with sync_playwright() as p:
        browser = p.chromium.launch()
        context = browser.new_context()
        result = context.new_page()
        result.route(
            "**/*",
            lambda route: route.fulfill(
                body='<article><p>A</p><a href="/detail">Detail</a></article><button class="more">More</button>'
            ),
        )
        result.goto("https://fixture.test/")
        yield result
        browser.close()


# 适配 browser-use 的多参数 evaluate 协议，脚本与参数保持分离。
def evaluate(page, script, *args):
    return page.evaluate("(args) => (" + script + ")(...args)", list(args))


# 所有匹配根优先返回，页面第一个关闭按钮不会遮蔽最后的 Load More。
def test_inspection_reports_all_matching_buttons_and_truncation(page):
    page.set_content('<button>Close</button><button class="more">Load More</button>')
    data = evaluate(page, INSPECT_SCRIPT, "button")
    assert data["root_count"] == data["sampled_roots"] == 2
    assert [node["text"] for node in data["nodes"]] == ["Close", "Load More"]
    fallback = evaluate(page, INSPECT_SCRIPT, ".unknown")
    assert fallback["root_count"] == 0 and fallback["fallback_root"] == "body"
    assert any(node["attrs"].get("class") == "more" for node in fallback["nodes"])
    page.set_content("<button>X</button>" * 55)
    data = evaluate(page, INSPECT_SCRIPT, "button")
    assert data["root_count"] == 55 and data["sampled_roots"] == 50
    assert data["truncated"]


# 批量测试保留单字段的精确参数、失败和样本语义，包括属性及自身节点。
def test_batch_fields_keep_individual_diagnostics(page):
    data = evaluate(
        page,
        BATCH_SELECTOR_SCRIPT,
        "article",
        {
            "title": {"selector": "p", "extract": "text"},
            "link": {"selector": "a", "extract": "attribute", "attribute": "href"},
            "bad": {"selector": ".missing", "extract": "text"},
            "self": {"selector": ":scope", "extract": "text"},
        },
    )
    assert data["url"] == page.url
    assert data["results"]["title"]["status"] == "sample_passed"
    assert data["results"]["link"]["attribute"] == "href"
    assert data["results"]["bad"]["status"] == "sample_failed"
    assert data["results"]["self"]["status"] == "sample_passed"


# 复用真实工具结果登记证据，覆盖读取参数和字段名独立这一约束。
def remember(page, checks, item="article", field="p", attribute=""):
    data = evaluate(page, SELECTOR_SCRIPT, item, field, attribute)
    checks.remember(data, field, attribute)
    return data


def candidate(**updates):
    data = dict(
        start_url="https://fixture.test/",
        item_selector="article",
        fields={"description": {"selector": "p"}},
        max_items=2,
    )
    data.update(updates)
    return ScraperSpec.model_validate(data)


# 用户字段可以直接放在详情中；详情测试必须来自观察过的详情链接，而非列表页。
def test_detail_fields_preserve_names_and_page_scope(page):
    checks = SubmissionChecks(TaskRequirements(fields=["description"], count=2))
    spec = ScraperSpec.model_validate(
        dict(
            version="2",
            start_url=page.url,
            item_selector="article",
            fields={"title": {"selector": "p"}},
            max_items=2,
            detail={"url_selector": "a", "fields": {"description": {"selector": "p"}}},
        )
    )
    remember(page, checks)
    remember(page, checks, field="a", attribute="href")
    remember(page, checks, item="html")
    assert checks.check(spec)  # 列表页的 html 根不能证明详情字段。
    page.goto("https://fixture.test/detail")
    remember(page, checks, item="html")
    assert checks.check(spec) == []
    renamed = spec.model_copy(
        update={
            "detail": spec.detail.model_copy(
                update={"fields": {"detail_description": spec.detail.fields["description"]}}
            )
        }
    )
    assert any(issue.code == "missing_required_field" for issue in checks.check(renamed))


# 后来的失败必须覆盖旧成功；只在正确 URL 上按最终字符串重新通过才能提交。
def test_latest_failure_and_changed_selector_are_rejected(page):
    checks = SubmissionChecks()
    remember(page, checks)
    assert not checks.check(candidate())
    page.set_content("<article><span>A</span></article>")
    remember(page, checks)
    assert checks.check(candidate())
    spec = ScraperSpec.model_validate(
        dict(
            start_url=page.url,
            item_selector="article",
            fields={"description": {"selector": "span"}},
        )
    )
    assert checks.check(spec)
    remember(page, checks, field="span")
    assert checks.check(spec) == []


# 分页控件唯一性与真实推进缺一不可；等待开始前已增长时也能用点击前计数确认。
def test_click_requires_control_test_and_record_progress(page):
    checks = SubmissionChecks()
    spec = candidate(pagination={"type": "click", "next_selector": "button.more"})
    remember(page, checks)
    remember(page, checks, item="button.more", field="")
    assert checks.check(spec)
    checks.begin_action("click", page.url)
    checks.remember_wait(
        {"url": page.url, "selector": "article", "status": "ready", "initial_count": 2, "count": 2}
    )
    assert checks.check(spec) == []
    remember(page, checks, item="button.wrong", field="")
    assert checks.check(
        spec.model_copy(
            update={
                "pagination": spec.pagination.model_copy(update={"next_selector": "button.wrong"})
            }
        )
    )


# 真正的 Tools.act 必须拦截 done，拒绝结果不会结束 Agent，也不会消耗连续工具失败上限。
def test_done_guard_rejects_before_builtin_completion():
    pytest.importorskip("browser_use")
    from autoscraper.agent.tools import build_tools

    tools = build_tools(
        check_submission=True, requirements=TaskRequirements(fields=["description"])
    )
    tools.use_structured_output_action(ScraperSpec)
    actions = tools.registry.create_action_model()
    result = asyncio.run(
        tools.act(actions.model_validate({"done": {"data": candidate().model_dump(mode="json")}}))
    )
    assert not result.is_done and not result.error
    payload = json.loads(result.extracted_content)
    assert payload["status"] == "candidate_rejected"
    assert payload["candidate"] == candidate().model_dump(mode="json")
    assert "evidence_summary" in payload
    error = tools.submission.rejection_error()
    assert error.candidate == candidate()
    assert error.evidence_summary == payload["evidence_summary"]


# 批量工具只发送数据参数，真实 Actor 返回结构能被提交证据正常登记。
def test_batch_tool_passes_rules_without_generated_code():
    pytest.importorskip("browser_use")
    from autoscraper.agent.tools import build_tools
    from autoscraper.scraping.spec import FieldSpec

    tools = build_tools()

    async def evaluate(script, selector, rules):
        assert script == BATCH_SELECTOR_SCRIPT and selector == "article"
        assert rules["title"]["selector"] == "p"
        return '{"url":"https://fixture.test/","results":{}}'

    async def current():
        return SimpleNamespace(evaluate=evaluate)

    action = tools.registry.registry.actions["test_fields"].function
    result = asyncio.run(
        action(
            browser_session=SimpleNamespace(must_get_current_page=current),
            item_selector="article",
            fields={"title": FieldSpec(selector="p")},
        )
    )
    assert json.loads(result.extracted_content)["url"] == "https://fixture.test/"


# 失败的 CSS 解析在点击后也只能变为拒绝证据，不能让证据登记本身抛异常。
def test_invalid_css_after_click_is_recorded_as_failed_evidence(page):
    checks = SubmissionChecks()
    remember(page, checks)
    checks.begin_action("click", page.url)
    data = remember(page, checks, field="[")
    assert data["status"] == "invalid_css"
    assert checks.result(page.url, "article", "[")["status"] == "invalid_css"


# 真实动作分发链：模型只点击后就 done 时，程序自动复核已知数量，不采信模型记忆。
@pytest.mark.parametrize(
    "delay, control, tested, final, on_detail, accepted",
    [
        (0, "button.more", "p", "p", False, True),
        (1200, "button.more", "p", "p", False, True),
        (0, "button.missing", "p", "p", False, False),
        (0, "button", "p", "p", False, False),
        (0, "button.more", ".old", "p", False, True),
        (0, "button.more", "p", ".missing", False, False),
        (0, "button.more", "p", "p,b", False, False),
        (0, "button.more", "p", "i.empty", False, True),
        (0, "button.more", "p", "[", False, False),
        (0, "button.more", ".old", "p", True, False),
    ],
)
def test_done_rechecks_pending_click_before_accepting(
    delay, control, tested, final, on_detail, accepted, monkeypatch
):
    pytest.importorskip("browser_use")
    from browser_use import ActionResult
    from playwright.async_api import async_playwright
    from autoscraper.agent.tools import build_tools

    async def run():
        async with async_playwright() as p:
            browser = await p.chromium.launch()
            try:
                page = await browser.new_page()
                await page.route(
                    "**/*",
                    lambda route: route.fulfill(
                        body=f"""<article><p>A</p><b>Other</b><i class=empty></i></article><button>Close</button>
                    <button class="more" onclick="setTimeout(() => document.body.insertAdjacentHTML('beforeend',
                    '<article><p>B</p><b>Other</b><i class=empty></i></article>'), {delay})">More</button>"""
                    ),
                )
                await page.goto("https://fixture.test/")
                # 数量等待仍操作真实页面；仅替换针对 browser-use CDP 会话的连接适配层。
                from autoscraper.agent.waiting import wait_for_count

                async def selector_wait(session, selector, timeout_ms, min_count):
                    return await wait_for_count(page, selector, min_count, timeout_ms)

                monkeypatch.setattr("autoscraper.agent.waiting.selector_wait", selector_wait)

                class Session:
                    downloaded_files = []
                    cdp_client = None
                    agent_focus_target_id = None

                    async def get_current_page_url(self):
                        return page.url

                    async def must_get_current_page(self):
                        return self

                    async def evaluate(self, script, *args):
                        return json.dumps(
                            await page.evaluate("(args) => (" + script + ")(...args)", list(args))
                        )

                tools = build_tools(
                    check_submission=True,
                    requirements=TaskRequirements(fields=["description"], count=2),
                )
                tools.use_structured_output_action(ScraperSpec)

                # 浏览器元素索引由 browser-use 管理；替换索引适配层，仍真实点击本地按钮。
                async def click(**kwargs):
                    await page.locator("button.more").click()
                    return ActionResult(extracted_content="clicked")

                tools.registry.registry.actions["click"].function = click
                actions = tools.registry.create_action_model()

                async def act(data):
                    return await tools.act(
                        actions.model_validate(data),
                        browser_session=Session(),
                        file_system=SimpleNamespace(),
                    )

                await act(
                    {
                        "test_fields": {
                            "item_selector": "article",
                            "fields": {"description": {"selector": tested}},
                        }
                    }
                )
                # 故意省略按钮测试和数量等待，覆盖模型漏调工具后的自动复核。
                await act({"click": {"index": 1}})
                if on_detail:
                    await page.goto("https://fixture.test/detail")
                result = await act(
                    {
                        "done": {
                            "data": candidate(
                                fields={"description": {"selector": final}},
                                pagination={"type": "click", "next_selector": control},
                            ).model_dump(mode="json")
                        }
                    }
                )
                assert bool(result.is_done) == accepted, result.extracted_content
                if not accepted:
                    assert json.loads(result.extracted_content)["status"] == "candidate_rejected"
                assert (
                    (
                        "https://fixture.test/",
                        "article",
                        "click",
                        "append",
                    )
                    in tools.submission.advances
                ) == (not on_detail)
            finally:
                await browser.close()

    asyncio.run(run())


# 批量字段的数量能作为独立计数/等待的基线，不再要求字段参数完全相同。
@pytest.mark.parametrize("kind", ["click", "scroll"])
@pytest.mark.parametrize("source", ["selector", "fields", "wait"])
def test_count_progress_does_not_depend_on_field_parameters(page, kind, source):
    checks = SubmissionChecks()
    page.set_content("<article><p>A</p></article>" * 6)
    remember(page, checks)
    checks.begin_action(kind, page.url)
    page.set_content("<article><p>A</p></article>" * 12)
    if source == "wait":
        checks.remember_wait(
            {"url": page.url, "selector": "article", "count": 12, "status": "ready"}
        )
    else:
        remember(page, checks, field="" if source == "selector" else ":scope")
    assert (page.url, "article", kind, "append") in checks.advances
    assert checks.counts[(page.url, "article")] == 12


# 不同页面和不同记录根不能混用数量；字段参数变化则不影响同一列表的数量比较。
@pytest.mark.parametrize(
    "other_url, other_selector",
    [
        ("https://fixture.test/other", "article"),
        ("https://fixture.test/", "section"),
    ],
)
def test_count_evidence_stays_on_its_page_and_item(page, other_url, other_selector):
    checks = SubmissionChecks()
    remember(page, checks)
    checks.begin_action("click", page.url)
    checks.remember_count(other_url, other_selector, 12)
    assert not checks.advances


# 最近一次数量覆盖旧数量，动作快照保持不变，不取不同字段测试中的历史最大值。
def test_count_baseline_uses_latest_observation(page):
    checks = SubmissionChecks()
    page.set_content("<article><p>A</p></article>" * 12)
    remember(page, checks)
    page.set_content("<article><p>A</p></article>" * 6)
    remember(page, checks, field="")
    checks.begin_action("click", page.url)
    checks.remember_count(page.url, "article", 12)
    assert (page.url, "article", "click", "append") in checks.advances


# 替换模式需要同一字段内容前后变化；单独数量和未经测过的新字段均不能证明替换。
def test_replace_progress_requires_matching_field_values(page):
    checks = SubmissionChecks()
    remember(page, checks)
    checks.begin_action("click", page.url)
    checks.remember_count(page.url, "article", 12)
    assert (page.url, "article", "click", "replace") not in checks.advances
    page.set_content("<article><p>B</p><span>New</span></article>")
    remember(page, checks, field="span")
    assert (page.url, "article", "click", "replace") not in checks.advances
    remember(page, checks)
    assert (page.url, "article", "click", "replace") in checks.advances


# 复核替换后的详情链接仍保留原先实际观察过的详情目标，详情测试不能来自任意 URL。
def test_detail_evidence_survives_listing_link_recheck(page):
    checks = SubmissionChecks()
    spec = candidate(
        version="2",
        fields={"title": {"selector": "p"}},
        detail={"url_selector": "a", "fields": {"description": {"selector": "p"}}},
    )
    remember(page, checks)
    remember(page, checks, field="a", attribute="href")
    page.goto("https://fixture.test/detail")
    remember(page, checks, item="html")
    page.goto("https://fixture.test/")
    page.set_content('<article><p>B</p><a href="/other-detail">Detail</a></article>')
    remember(page, checks)
    remember(page, checks, field="a", attribute="href")
    assert checks.check(spec) == []


# 动作前 DOM 已自行增长时重新计数，不能把旧测试的数量误认成这次点击造成的增长。
def test_action_baseline_refreshes_stale_counts():
    pytest.importorskip("browser_use")
    from playwright.async_api import async_playwright
    from autoscraper.agent.tools import build_tools

    async def run():
        async with async_playwright() as p:
            browser = await p.chromium.launch()
            try:
                page = await browser.new_page()
                await page.set_content("<article><p>A</p></article>" * 2)
                tools = build_tools(check_submission=True)
                tools.submission.remember_count(page.url, "article", 1)

                class Session:
                    async def must_get_current_page(self):
                        return self

                    async def evaluate(self, script, *args):
                        return json.dumps(
                            await page.evaluate("(args) => (" + script + ")(...args)", list(args))
                        )

                await tools._begin_action("click", page.url, Session())
                tools.submission.remember_count(page.url, "article", 2)
                assert not tools.submission.advances
                tools.submission.remember_count(page.url, "article", 3)
                assert (page.url, "article", "click", "append") in tools.submission.advances
            finally:
                await browser.close()

    asyncio.run(run())


# 复现日志中的根选择器漂移：详情已测、分页已推进，反馈应指出证据关联变化而非重新发现。
def test_changed_root_reports_existing_detail_and_progress_evidence(page):
    checks = SubmissionChecks()
    listing_url = page.url
    card = ('<article><b class="status">Available</b><a class="detail" href="/detail">Detail</a>'
            '<div class="body"><h2>A</h2></div></article>')
    spec = candidate(
        version="2", fields={"name": {"selector": "h2"}, "status": {"selector": ".status"}},
        detail={"url_selector": "a.detail", "fields": {"desc": {"selector": "p.description"}}},
        pagination={"type": "click", "next_selector": "button.more"},
    )
    page.set_content(card + '<button class="more">More</button>')
    for selector in ("h2", ".status"):
        remember(page, checks, field=selector)
    remember(page, checks, field="a.detail", attribute="href")
    remember(page, checks, item="button.more", field="")
    checks.begin_action("click", listing_url)
    checks.remember_wait({"url": listing_url, "selector": "article", "status": "ready", "count": 2})
    page.goto("https://fixture.test/detail")
    page.set_content('<p class="description">Long description</p>')
    remember(page, checks, item="html", field="p.description")
    assert checks.check(spec) == []
    page.goto(listing_url)
    page.set_content(card * 2 + '<button class="more">More</button>')
    for selector, attribute in (("h2", ""), (".status", ""), ("a.detail", "href")):
        remember(page, checks, item=".body", field=selector, attribute=attribute)
    changed = spec.model_copy(update={"item_selector": ".body"})
    messages = [issue.message for issue in checks.check(changed)]
    assert any("latest test failed" in message and "different item roots ['article']" in message
               for message in messages)
    assert any("Detail fields already passed" in message and "not linked" in message
               for message in messages)
    assert any("Recorded progression uses different item roots ['article']" in message
               for message in messages)
    assert not any("Test every detail field" in message for message in messages)
    assert checks.check(spec) == []  # 有效的旧根仍可使用，新根不会继承它的证据。


# 修复摘要需要限制上下文量，且不能把字段样本原文带入下一轮。
def test_repair_summary_is_bounded_and_excludes_page_text():
    checks = SubmissionChecks()
    for index in range(40):
        checks.remember({
            "url": "https://fixture.test/", "item_selector": "article", "count": 1,
            "status": "sample_passed",
            "samples": [{"matches": 1, "values": ["PRIVATE_PAGE_TEXT"]}],
        }, f".field-{index}")
    summary = checks.repair_summary()
    assert len(summary["tests"]) == 32
    assert summary["omitted_tests"] == 8
    assert "PRIVATE_PAGE_TEXT" not in json.dumps(summary)
    assert all(test["passed"] for test in summary["tests"])
    fresh = SubmissionChecks()
    assert fresh.check(candidate())  # 摘要不构成新会话中的有效取证。


# 没有候选时不把空待办当作全部完成；原始检查也不能冒充字段测试。
def test_page_work_without_candidate_reports_inspection_only():
    checks = SubmissionChecks(listing_url="https://fixture.test/")
    listing = checks.page_work("https://fixture.test/")
    assert listing["role"] == "listing"
    assert listing["field_tests"] == 0 and listing["candidate_pending"] is None
    other = checks.page_work("https://fixture.test/detail")
    assert "does not verify" in other["guidance"]
    assert "item_selector='html'" in other["guidance"]
    assert checks.tests == {} and checks.advances == set()


# 页内待办帮助安排顺序但不阻止提前导航；证据齐备时可直接在详情页完成提交。
@pytest.mark.parametrize("finish_listing", [False, True])
def test_page_work_guides_page_grouping_without_navigation_gate(finish_listing):
    pytest.importorskip("browser_use")
    from browser_use import ActionResult
    from autoscraper.agent.tools import build_tools
    from autoscraper.observability.tracing import summarize_tool_output

    url = "https://fixture.test/"
    detail_url = url + "detail"
    spec = candidate(version="2", fields={"name": {"selector": "h2"}},
                     detail={"url_selector": "a", "fields": {"desc": {"selector": "p.description"}}},
                     pagination={"type": "click", "next_selector": "button.more"})
    tools = build_tools(check_submission=True, listing_url=url, reference_spec=spec)
    tools.use_structured_output_action(ScraperSpec)
    current_url = url
    evaluations = []

    class Session:
        downloaded_files = []
        cdp_client = None
        agent_focus_target_id = None

        async def get_current_page_url(self):
            return current_url

        async def must_get_current_page(self):
            return self

        async def evaluate(self, script, *args):
            evaluations.append(script)
            if script == INSPECT_SCRIPT:
                return json.dumps({"url": current_url, "nodes": [], "root_count": 1})

            def field(root, selector, attribute=""):
                return {"url": current_url, "item_selector": root, "field_selector": selector,
                        "attribute": attribute, "count": 1, "status": "sample_passed",
                        "samples": [{"matches": 1, "values": ["/detail" if attribute == "href" else "A"]}]}

            if script == BATCH_SELECTOR_SCRIPT:
                root, fields = args
                return json.dumps({"url": current_url, "item_selector": root, "results": {
                    name: field(root, rule["selector"], rule.get("attribute") or "")
                    for name, rule in fields.items()
                }})
            assert script == SELECTOR_SCRIPT
            return json.dumps(field(*args))

    async def navigate(params, **kwargs):
        nonlocal current_url
        current_url = params.url
        return ActionResult(extracted_content="navigated")

    tools.registry.registry.actions["navigate"].function = navigate
    actions = tools.registry.create_action_model()

    async def act(data):
        return await tools.act(actions.model_validate(data), browser_session=Session(),
                               file_system=SimpleNamespace())

    async def run():
        inspected = await act({"inspect_page": {"root_selector": "article"}})
        work = json.loads(inspected.extracted_content)["page_work"]
        assert work["field_tests"] == 0
        assert "field:name" in work["candidate_pending"]
        assert "pagination_progress:click/append" in work["candidate_pending"]
        assert len(evaluations) == 1  # 生成待办不增加浏览器操作或模型请求。
        assert summarize_tool_output(inspected.extracted_content)["page_work"] == work
        await act({"test_fields": {"item_selector": "article", "fields": {
            "name": {"selector": "h2"},
            "href": {"selector": "a", "extract": "attribute", "attribute": "href"},
        }}})
        if finish_listing:
            await act({"test_selector": {"item_selector": "button.more", "field_selector": ""}})
            tools.submission.begin_action("click", url)
            tools.submission.remember_wait({"url": url, "selector": "article", "status": "ready", "count": 2})
        navigated = await act({"navigate": {"url": detail_url}})
        assert not navigated.error and current_url == detail_url
        inspected = await act({"inspect_page": {"root_selector": "p"}})
        work = json.loads(inspected.extracted_content)["page_work"]
        assert work["role"] == "detail" and work["candidate_pending"] == ["field:desc"]
        tested = await act({"test_fields": {"item_selector": "html", "fields": {
            "desc": {"selector": "p.description"},
        }}})
        completed = json.loads(tested.extracted_content)["page_work"]
        assert completed["candidate_pending"] == []
        assert "already covered" in completed["guidance"]
        result = await act({"done": {"data": spec.model_dump(mode="json")}})
        assert bool(result.is_done) is finish_listing, result.extracted_content
        assert current_url == detail_url  # 不因提交而强制回列表；缺证据时仍保持拒绝。

    asyncio.run(run())


# 多元素字段必须有对应提取方式的证据，不能混用单值测试或其他多元素模式。
@pytest.mark.parametrize("extract,selector,required,valid", [
    ("texts", "b", True, True), ("texts", ".missing", True, True),
    ("texts", ".missing", False, True), ("texts", "i", True, True),
    ("count", ".missing", True, True), ("count", "b", True, True),
])
def test_multi_element_submission_evidence(page, extract, selector, required, valid):
    page.set_content('<article><b>A</b><b>B</b><b>C</b><i> </i></article>')
    fields = {"value": {"selector": selector, "extract": extract, "required": required}}
    spec = candidate(fields=fields)
    checks = SubmissionChecks(reference_spec=spec)
    remember(page, checks, field=selector)
    assert checks.check(spec)  # 原始单值测试不能作为多元素证据。
    data = evaluate(page, BATCH_SELECTOR_SCRIPT, "article", fields)
    field = data["results"]["value"]
    checks.remember(field, selector)
    assert (not checks.check(spec)) is valid
    assert (checks.page_work(page.url)["candidate_pending"] == []) is valid
    if selector == "b":
        assert field["samples"][0]["values"] == (["A", "B", "C"] if extract == "texts" else [3])
    assert bool(checks.check(candidate(fields={"value": {"selector": selector}}))) == (selector != "i")


# 详情多元素证据同样必须关联已观察到的详情链接。
def test_detail_multi_element_submission(page):
    spec = candidate(version="2", fields={"title": {"selector": "p"}}, detail={
        "url_selector": "a", "fields": {"tags": {"selector": "b", "extract": "texts"},
                                          "zero": {"selector": ".missing", "extract": "count"}}})
    checks = SubmissionChecks()
    remember(page, checks)
    remember(page, checks, field="a", attribute="href")
    page.goto("https://fixture.test/detail")
    page.set_content("<b>A</b><b>B</b>")
    data = evaluate(page, BATCH_SELECTOR_SCRIPT, "html",
                    {name: field.model_dump() for name, field in spec.detail.fields.items()})
    for field in data["results"].values():
        checks.remember(field, field["field_selector"])
    assert checks.check(spec) == []


# 同一选择器可同时声明文本、文本列表和计数，证据互不覆盖，失败也只覆盖同一模式。
def test_same_selector_evidence_isolated_by_extraction_mode(page):
    page.set_content('<article><b>A</b></article>')
    fields = {mode: {"selector": "b", "extract": mode} for mode in ("text", "texts", "count")}
    spec = candidate(fields=fields)
    checks = SubmissionChecks(reference_spec=spec)
    for field in evaluate(page, BATCH_SELECTOR_SCRIPT, "article", fields)["results"].values():
        checks.remember(field, "b")
    assert checks.check(spec) == []
    assert checks.page_work(page.url)["candidate_pending"] == []
    page.set_content('<article><b>A</b><b>B</b></article>')
    remember(page, checks, field="b")
    assert checks.check(spec)  # 单值多匹配失败，不影响其他模式的既有证据。
    assert checks.check(candidate(fields={mode: fields[mode] for mode in ("texts", "count")})) == []


# 切换提取方式不能把同一选择器的新测试误当成替换分页的内容推进。
def test_changed_extraction_mode_is_not_replace_progress(page):
    checks = SubmissionChecks()
    remember(page, checks)
    checks.begin_action("click", page.url)
    data = evaluate(page, BATCH_SELECTOR_SCRIPT, "article", {"n": {"selector": "p", "extract": "count"}})
    checks.remember(data["results"]["n"], "p")
    assert (page.url, "article", "click", "replace") not in checks.advances
