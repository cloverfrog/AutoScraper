# 动态分页离线回归：覆盖追加、替换和滚动的推进、结束、失败及预算边界。
# 复用基础抓取测试的浏览器与路由夹具，以真实页面行为验证等待逻辑。
"""Offline browser regressions for append, replacement and scroll termination."""

from html import escape

import pytest
from pydantic import ValidationError

from autoscraper.scraping.executor import ContentTimeout, PaginationActionError, ScraperExecutor
from autoscraper.scraping.spec import ScraperSpec
import test_scraper
from test_scraper import route_pages

page = test_scraper.page


# 构造最小动态分页规则，允许用例单独覆盖等待和预算参数。
def rule(pagination, **updates):
    data = dict(
        start_url="https://fixture.test/1",
        item_selector="article",
        fields={"title": {"selector": "span"}},
        pagination=pagination,
        timeout_ms=300,
        content_timeout_ms=180,
        max_items=20,
    )
    data.update(updates)
    return ScraperSpec.model_validate(data)


# 将 HTML 挂到离线路由并运行真实 Chromium，避免依赖外部站点。
def run(page, body, pagination, **updates):
    route_pages(page, {"https://fixture.test/1": body})
    return ScraperExecutor().run_page(rule(pagination, **updates), page)


# 页码先变、数据延迟更新时，追加和替换模式都必须等到真实记录变化。
@pytest.mark.parametrize("mode", ["append", "replace"])
def test_click_delayed_records_and_early_page_number(page, mode):
    body = """<main><article><span>A</span></article></main><b>1</b>
    <button onclick="document.querySelector('b').textContent='2';
    document.querySelector('#loading').hidden=false;
    setTimeout(() => {document.querySelector('main').MODE = 'RECORDS';
    document.querySelector('#loading').hidden=true; this.disabled=true}, 90)">Next</button>
    <i id="loading" hidden>Loading</i>"""
    body = body.replace("MODE", "innerHTML" if mode == "replace" else "innerHTML +")
    body = body.replace("innerHTML + =", "innerHTML +=")
    body = body.replace("RECORDS", "<article><span>B</span></article>")
    result = run(
        page,
        body,
        dict(type="click", next_selector="button", mode=mode, loading_selector="#loading"),
    )
    assert [r["title"] for r in result.records] == ["A", "B"]
    assert result.pages_visited == 2
    assert result.stop_reason == "no_next"


# 按钮文字变化不等于记录推进；控件仍可用但无新内容时必须超时。
def test_click_animation_is_not_progress(page):
    body = """<article><span>A</span></article>
    <button onclick="this.textContent='Loading...'">Next</button>"""
    with pytest.raises(ContentTimeout):
        run(page, body, dict(type="click", next_selector="button"))


# 不可点击的控件应报告动作失败，与点击成功后的内容等待超时区分。
def test_click_action_failure(page):
    with pytest.raises(PaginationActionError):
        run(
            page,
            "<article><span>A</span></article><button hidden>Next</button>",
            dict(type="click", next_selector="button"),
        )


# 无新增记录时，区分控件消失/禁用与显式结束标志这两类自然结束。
@pytest.mark.parametrize(
    "terminal",
    ["this.remove()", "this.disabled=true", "document.querySelector('#end').hidden=false"],
)
def test_click_end_without_growth(page, terminal):
    body = f'''<article><span>A</span></article><i id="end" hidden>End</i>
    <button onclick="{terminal}">Next</button>'''
    result = run(page, body, dict(type="click", next_selector="button", end_selector="#end"))
    assert result.stop_reason == ("end_marker" if "#end" in terminal else "no_next")


# 已处理批次达到上限后，不得为了判断下一页而额外点击。
def test_click_budget_prevents_extra_action(page):
    result = run(
        page,
        """<article><span>A</span></article>
    <button onclick="window.clicked=true">Next</button>""",
        dict(type="click", next_selector="button"),
        max_pages=1,
    )
    assert result.stop_reason == "max_pages"
    assert page.evaluate("window.clicked") is None


# 页面和指定容器均能滚动追加；结束标志与最后一批同时到达时不能漏提取。
@pytest.mark.parametrize("container", [False, True])
def test_scroll_append_and_explicit_end(page, container):
    style = "<style>article {height: 900px}</style>"
    opening = '<main style="height:200px;overflow:auto">' if container else "<main>"
    body = style + opening + '<article><span>A</span></article></main><i id="end" hidden>End</i>'
    target = "document.querySelector('main')" if container else "window"
    body += f"""<script>let n=0; {target}.addEventListener('scroll', () => {{
    const root = {"document.querySelector('main')" if container else "document.scrollingElement"};
    if (root.scrollTop + root.clientHeight < root.scrollHeight-2 || n>=2) return;
    n++; setTimeout(() => {{document.querySelector('main').insertAdjacentHTML('beforeend',
    '<article><span>'+ (n===1?'B':'C') +'</span></article>');
    if(n===2) document.querySelector('#end').hidden=false}}, 60);
    }});</script>"""
    pagination = dict(type="scroll", end_selector="#end")
    if container:
        pagination["container_selector"] = "main"
    result = run(page, body, pagination, content_timeout_ms=100)
    assert [r["title"] for r in result.records] == ["A", "B", "C"]
    assert result.stop_reason == "end_marker"


# 连续无新增达到阈值才推断结束；动作预算先耗尽时必须报告预算截断。
@pytest.mark.parametrize(
    "updates,reason", [({}, "no_more_content"), ({"max_scrolls": 2}, "scroll_limit")]
)
def test_scroll_no_growth_threshold(page, updates, reason):
    result = run(
        page, "<article><span>A</span></article>", dict(type="scroll", no_change_limit=3, **updates)
    )
    assert result.stop_reason == reason
    assert result.pages_visited == 1


# 加载标志始终可见应按超时失败，不能累积无新增次数后误判完成。
def test_scroll_loading_timeout(page):
    with pytest.raises(ContentTimeout):
        run(
            page,
            "<article><span>A</span></article><i>Loading</i>",
            dict(type="scroll", loading_selector="i"),
        )


# 记录数增长证明批次推进，即使新批次全是重复记录，结果仍须去重。
def test_duplicate_batches_advance_but_are_deduplicated(page):
    body = """<main><article><span>A</span></article></main>
    <button onclick="document.querySelector('main').insertAdjacentHTML('beforeend',
    '<article><span>A</span></article>');this.disabled=true">More</button>"""
    result = run(page, body, dict(type="click", next_selector="button"))
    assert result.pages_visited == 2
    assert len(result.records) == 1
    assert result.duplicates == 1


# 拒绝旧格式、未知类型、跨类型字段和无效阈值，防止静默忽略规则。
@pytest.mark.parametrize(
    "pagination",
    [
        {"next_selector": "a"},
        {"type": "unknown"},
        {"type": "link", "next_selector": "a", "mode": "replace"},
        {"type": "scroll", "next_selector": "a"},
        {"type": "click", "next_selector": "a", "max_scrolls": 5},
        {"type": "scroll", "no_change_limit": 1},
    ],
)
def test_strict_pagination_schema(pagination):
    with pytest.raises(ValidationError):
        rule(pagination)


# 尚未到底的空滚动不能作为结束证据，只能继续到动作预算耗尽。
def test_scroll_without_reaching_bottom_does_not_infer_end(page):
    result = run(
        page,
        '<article style="height:6000px"><span>A</span></article>',
        dict(type="scroll", max_scrolls=2, no_change_limit=2),
    )
    assert result.stop_reason == "scroll_limit"


# 替换期间短暂清空字段不代表新批次就绪，必须等待必填值恢复。
def test_replace_waits_through_empty_field_placeholder(page):
    body = """<article><span>A</span></article>
    <button onclick="document.querySelector('span').textContent='';
    setTimeout(() => {document.querySelector('span').textContent='B';this.remove()}, 80)">
    Next</button>"""
    result = run(page, body, dict(type="click", mode="replace", next_selector="button"))
    assert [r["title"] for r in result.records] == ["A", "B"]
    assert not result.errors



# 请求期间暂时禁用按钮时仍等待数据，避免在响应返回前误判结束。
def test_temporary_disabled_button_does_not_hide_pending_records(page):
    body = """<main><article><span>A</span></article></main>
    <button onclick="this.disabled=true;setTimeout(() => {
    document.querySelector('main').insertAdjacentHTML('beforeend',
    '<article><span>B</span></article>');this.remove()}, 80)">More</button>"""
    result = run(page, body, dict(type="click", next_selector="button"))
    assert [r["title"] for r in result.records] == ["A", "B"]
    assert result.stop_reason == "no_next"


# 多屏空白距离应连续推进到加载触发点，不能每滚一屏就消耗完整内容等待时限。
@pytest.mark.parametrize("container", [False, True])
def test_scroll_moves_through_multiple_viewports_without_long_wait(page, container):
    import time

    target = "document.querySelector('main')" if container else "document.scrollingElement"
    source = "document.querySelector('main')" if container else "window"
    wrapper = '<main style="height:200px;overflow:auto">' if container else '<main>'
    body = '<!doctype html><style>article {height:2200px}</style>' + wrapper
    body += '<article><span>A</span></article></main><i id="end" hidden>End</i>'
    body += f'''<script>{source}.addEventListener('scroll', () => {{
        const root = {target};
        if (root.scrollTop + root.clientHeight >= root.scrollHeight-2) {{
            document.querySelector('main').insertAdjacentHTML('beforeend', '<article style="height:20px"><span>B</span></article>');
            document.querySelector('#end').hidden=false;
        }}
    }});</script>'''
    pagination = dict(type='scroll', end_selector='#end', max_scrolls=20)
    if container:
        pagination['container_selector'] = 'main'
    started = time.monotonic()
    result = run(page, body, pagination, content_timeout_ms=5000, max_items=2)
    assert [r['title'] for r in result.records] == ['A', 'B']
    # 容器需要十余次滚动；只要一次半途空等五秒，这个上限就会失败。
    assert time.monotonic() - started < 5


# 底部触发的慢加载必须使用内容等待预算，不能因短间隔推进而提前推断结束。
def test_scroll_waits_for_slow_loading_marker(page):
    body = '''<!doctype html><style>article {height:1000px}</style>
    <article><span>A</span></article><i id="loading" hidden>Loading</i>
    <script>let requested=false; addEventListener('scroll', () => {
      if (requested) return; requested=true;
      document.querySelector('#loading').hidden=false;
      setTimeout(() => {document.body.insertAdjacentHTML('beforeend', '<article><span>B</span></article>');
        document.querySelector('#loading').hidden=true}, 500);
    });</script>'''
    result = run(page, body, dict(type='scroll', loading_selector='#loading'),
                 max_items=2, content_timeout_ms=1000)
    assert [r['title'] for r in result.records] == ['A', 'B']


# 详情抓取需要可创建多个 Page 的显式 Context，与纯列表测试的快捷 Page 分开。
@pytest.fixture
def listing_page(page):
    context = page.context.browser.new_context()
    try:
        yield context.new_page()
    finally:
        context.close()


# 详情字段延迟出现时，让列表在同一时间追加下一批，确定性复现 Review 指出的竞态。
# 只安排页面的异步更新，详情导航、字段等待和记录合并仍走真实执行器。
def pending_batch_during_detail(
    page, monkeypatch, terminal="marker", replace=False, duplicate=False, **updates
):
    import autoscraper.scraping.executor as executor

    body = """<!doctype html><main><article><span>A</span><a href="/a">Detail</a></article></main>
    <i id="end" hidden>End</i><button id="next">Next</button>"""
    detail_body = """<script>setTimeout(() => {
        document.body.innerHTML='<p>Description</p>';
    }, 120)</script>"""
    route_pages(
        page.context,
        {
            "https://fixture.test/1": body,
            "https://fixture.test/a": detail_body,
            "https://fixture.test/b": detail_body,
        },
    )
    pagination = {"type": "scroll", "end_selector": "#end", "no_change_limit": 2}
    if terminal in {"disabled", "missing"} or replace:
        pagination = {
            "type": "click",
            "next_selector": "#next",
            "end_selector": "#end",
            "mode": "replace" if replace else "append",
        }
    settings = dict(
        version="2",
        on_item_error="fail",
        detail={"url_selector": "a", "fields": {"description": {"selector": "p"}}},
        dedupe_by=["title"],
    )
    settings.update(updates)
    spec = rule(pagination, **settings)
    original = executor.extract_detail
    scheduled = False

    # 第一次详情提取期间新增 B；结束状态与 B 同时出现，不能先结束后漏提取。
    def extract_detail(detail_page, url, candidate):
        nonlocal scheduled
        if not scheduled:
            scheduled = True
            page.evaluate(
                """(options) => setTimeout(() => {
                const main = document.querySelector('main');
                const card = '<article><span>' + (options.duplicate ? 'A' : 'B') + '</span><a href="/b">Detail</a></article>';
                if (options.replace) main.innerHTML = card;
                else main.insertAdjacentHTML('beforeend', card);
                if (options.terminal === 'marker') document.querySelector('#end').hidden=false;
                if (options.terminal === 'disabled') document.querySelector('#next').disabled=true;
                if (options.terminal === 'missing') document.querySelector('#next').remove();
            }, 20)""",
                {"terminal": terminal, "replace": replace, "duplicate": duplicate},
            )
        return original(detail_page, url, candidate)

    monkeypatch.setattr(executor, "extract_detail", extract_detail)
    return ScraperExecutor().run_page(spec, page)


# 结束标志、控件不可用和没有显式结束标志时，都应先补提取已到达的记录。
@pytest.mark.parametrize(
    "terminal,reason",
    [
        ("marker", "end_marker"),
        ("disabled", "no_next"),
        ("missing", "no_next"),
        ("none", "no_more_content"),
    ],
)
def test_pending_batch_during_detail_is_processed_before_termination(
    listing_page, monkeypatch, terminal, reason
):
    result = pending_batch_during_detail(listing_page, monkeypatch, terminal)
    assert [r["title"] for r in result.records] == ["A", "B"]
    assert result.pages_visited == result.detail_pages_visited == 2
    assert result.stop_reason == reason
    assert not result.errors


# 替换列表数量不变，也应按字段快照识别提取期间到达的新批次。
def test_pending_replacement_during_detail_is_not_hidden_by_end_marker(listing_page, monkeypatch):
    result = pending_batch_during_detail(listing_page, monkeypatch, replace=True)
    assert [r["title"] for r in result.records] == ["A", "B"]
    assert result.stop_reason == "end_marker"


# 自动追加的补提取仍消耗批次预算；预算不足不能借结束标志冒充自然完成。
def test_pending_batch_respects_page_budget(listing_page, monkeypatch):
    from autoscraper.workflow.requirements import TaskRequirements
    from autoscraper.workflow.validation import validate_execution_data

    result = pending_batch_during_detail(listing_page, monkeypatch, max_pages=1)
    assert [r["title"] for r in result.records] == ["A"]
    assert result.pages_visited == result.detail_pages_visited == 1
    assert result.stop_reason == "max_pages"
    feedback, complete = validate_execution_data(result, TaskRequirements(fields=["title"]))
    assert not complete
    assert feedback[0].code == "validation_limit"


# 达到用户数量就停止，不因补提取而多访问详情或超出记录上限。
def test_pending_batch_respects_item_budget(listing_page, monkeypatch):
    result = pending_batch_during_detail(listing_page, monkeypatch, max_items=1)
    assert [r["title"] for r in result.records] == ["A"]
    assert result.stop_reason == "max_items"
    assert result.detail_pages_visited == 1


# 补提取不得绕过详情预算，且已成功抓到的记录应保留，停止原因明确为详情预算耗尽。
def test_pending_batch_respects_detail_budget(listing_page, monkeypatch):
    result = pending_batch_during_detail(
        listing_page,
        monkeypatch,
        detail={"url_selector": "a", "fields": {"description": {"selector": "p"}}, "max_pages": 1},
    )
    assert [r["title"] for r in result.records] == ["A"]
    assert result.stop_reason == "detail_limit"
    assert result.detail_pages_visited == 1


# 新到达的批次即使全是重复记录也必须核对；列表去重键命中时不重复访问详情。
def test_pending_duplicate_batch_preserves_dedupe(listing_page, monkeypatch):
    result = pending_batch_during_detail(listing_page, monkeypatch, duplicate=True)
    assert [r["title"] for r in result.records] == ["A"]
    assert result.pages_visited == 2
    assert result.detail_pages_visited == 1
    assert result.duplicates == 1
    assert result.stop_reason == "end_marker"


# 两条不同记录的预算恰好为二，空去重键和详情字段去重都不能重复访问旧前缀。
# 替换和链接分页也使用相同夹具，确保新增游标不会跳过下一页的第一个记录。
@pytest.mark.parametrize("kind", ["append", "scroll", "replace", "link"])
@pytest.mark.parametrize("keys", [[], ["description"]])
def test_dynamic_detail_budget_covers_distinct_records(listing_page, kind, keys):
    first = '<article style="height:900px"><span>A</span><a href="/a">Detail</a></article>'
    second = '<article><span>B</span><a href="/b">Detail</a></article>'
    update = (
        (
            "document.querySelector('main').innerHTML = "
            if kind == "replace"
            else "document.querySelector('main').innerHTML += "
        )
        + repr(second)
        + ";document.querySelector('#end').hidden=false"
    )
    body = "<!doctype html><main>" + first + '</main><i id="end" hidden>End</i>'
    if kind == "link":
        body += '<a id="next" href="/2">Next</a>'
        pagination = dict(type="link", next_selector="#next")
    elif kind == "scroll":
        body += (
            "<script>window.addEventListener('scroll', () => {"
            + update
            + "}, {once:true})</script>"
        )
        pagination = dict(type="scroll", end_selector="#end")
    else:
        body += '<button id="next" onclick="' + escape(update, quote=True) + '">Next</button>'
        pagination = dict(type="click", mode=kind, next_selector="#next", end_selector="#end")
    pages = {
        "https://fixture.test/1": body,
        "https://fixture.test/2": second,
        "https://fixture.test/a": "<p>Description A</p>",
        "https://fixture.test/b": "<p>Description B</p>",
    }
    requests = []

    # 记录实际网络访问，避免只检查结果去重而漏掉重复详情请求。
    def serve(route):
        requests.append(route.request.url)
        route.fulfill(content_type="text/html; charset=utf-8", body=pages[route.request.url])

    listing_page.context.route("**/*", serve)
    spec = rule(
        pagination,
        version="2",
        dedupe_by=keys,
        max_items=2,
        timeout_ms=1000,
        content_timeout_ms=1000,
        detail={"url_selector": "a", "fields": {"description": {"selector": "p"}}, "max_pages": 2},
    )
    result = ScraperExecutor().run_page(spec, listing_page)
    assert [record["title"] for record in result.records] == ["A", "B"]
    assert result.detail_pages_visited == result.pages_visited == 2
    assert result.stop_reason == "max_items" and not result.errors
    assert requests.count("https://fixture.test/a") == requests.count("https://fixture.test/b") == 1
    assert result.duplicates == 0


# 详情提取期间自动到达的数据仍只处理新后缀，结束标志和按钮状态不能提前终止。
@pytest.mark.parametrize("terminal", ["marker", "disabled", "missing"])
def test_pending_append_with_default_dedupe_uses_each_detail_once(
    listing_page, monkeypatch, terminal
):
    result = pending_batch_during_detail(
        listing_page,
        monkeypatch,
        terminal,
        dedupe_by=[],
        detail={"url_selector": "a", "fields": {"description": {"selector": "p"}}, "max_pages": 2},
    )
    assert [record["title"] for record in result.records] == ["A", "B"]
    assert result.detail_pages_visited == 2 and result.duplicates == 0
    assert result.stop_reason == ("end_marker" if terminal == "marker" else "no_next")


# 新追加节点中的重复数据仍参与正常去重；旧节点则不会被重新计入重复数或详情预算。
def test_new_appended_duplicate_still_uses_full_record_dedupe(listing_page):
    appended = '<article><span>A</span><a href="/a">Detail</a></article><article><span>B</span><a href="/b">Detail</a></article>'
    update = (
        "document.querySelector('main').insertAdjacentHTML('beforeend', "
        + repr(appended)
        + ");this.remove()"
    )
    body = '<main><article><span>A</span><a href="/a">Detail</a></article></main>'
    body += '<button onclick="' + escape(update, quote=True) + '">More</button>'
    route_pages(
        listing_page.context,
        {
            "https://fixture.test/1": body,
            "https://fixture.test/a": "<p>A</p>",
            "https://fixture.test/b": "<p>B</p>",
        },
    )
    result = ScraperExecutor().run_page(
        rule(
            dict(type="click", next_selector="button"),
            version="2",
            max_items=2,
            detail={
                "url_selector": "a",
                "fields": {"description": {"selector": "p"}},
                "max_pages": 3,
            },
        ),
        listing_page,
    )
    assert [record["title"] for record in result.records] == ["A", "B"]
    assert result.detail_pages_visited == 3 and result.duplicates == 1
    assert result.stop_reason == "max_items"


# 已按 skip 处理的旧节点不应在下一批重复失败；失败访问仍正常扣一次详情预算。
def test_skipped_append_prefix_is_not_retried(listing_page):
    body = """<main><article><span>A</span><a href="/a">Detail</a></article></main>
    <button onclick="document.querySelector('main').insertAdjacentHTML('beforeend',
    '<article><span>B</span><a href=/b>Detail</a></article>');this.remove()">More</button>"""
    route_pages(
        listing_page.context,
        {
            "https://fixture.test/1": body,
            "https://fixture.test/a": "<p></p>",
            "https://fixture.test/b": "<p>Description B</p>",
        },
    )
    result = ScraperExecutor().run_page(
        rule(
            dict(type="click", next_selector="button"),
            version="2",
            on_item_error="skip",
            detail={
                "url_selector": "a",
                "fields": {"description": {"selector": "p"}},
                "max_pages": 2,
            },
        ),
        listing_page,
    )
    assert [record["title"] for record in result.records] == ["B"]
    assert result.detail_pages_visited == 2 and len(result.errors) == 1
    assert result.stop_reason == "no_next"


# 替换分页必须看到整个文本列表变化；计数字段从一变零也构成有效推进。
@pytest.mark.parametrize("extract", ["texts", "count"])
def test_replace_multi_element_snapshot(page, extract):
    body = """<article><span>A</span><span>B</span></article>
    <button onclick="document.querySelector('article').innerHTML='REPLACEMENT';
    this.disabled=true">Next</button>"""
    body = body.replace("REPLACEMENT", "<span>A</span><span>C</span>" if extract == "texts" else "")
    result = run(page, body, dict(type="click", next_selector="button", mode="replace"),
                 fields={"value": {"selector": "span", "extract": extract}})
    assert result.errors == [] and result.pages_visited == 2
    assert [row["value"] for row in result.records] == (
        [["A", "B"], ["A", "C"]] if extract == "texts" else [2, 0])
