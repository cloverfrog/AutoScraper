# 动态分页离线回归：覆盖追加、替换和滚动的推进、结束、失败及预算边界。
# 复用基础抓取测试的浏览器与路由夹具，以真实页面行为验证等待逻辑。
"""Offline browser regressions for append, replacement and scroll termination."""

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
    assert result.duplicates == 2


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
