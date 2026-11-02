# 分页探针离线检查：使用模拟页码窗口，验证顺序证据与歧义控件处理。
"""Offline browser checks for the opt-in pagination probe."""

import importlib.util
from pathlib import Path

import test_scraper


module_spec = importlib.util.spec_from_file_location(
    "pagination_probe", Path(__file__).resolve().parents[2] / "scripts/probe/probe_pagination.py"
)
probe_module = importlib.util.module_from_spec(module_spec)
module_spec.loader.exec_module(probe_module)
page = test_scraper.page


# 页码窗口在第 4 页后移；末页箭头故意跳到第 12 页，不能当作相邻推进。
WINDOW_PAGE = """<main></main><ul class="Pagination"></ul><script>
function render(n) {
    window.visited = window.visited || [];
    window.visited.push(n);
    document.querySelector('main').innerHTML = '<article class="Story"><a href="https://news.ycombinator.com/item?id=' + n + '">Record</a></article>';
    const start = Math.max(1, n - 2), end = Math.min(12, n + 3);
    let controls = '';
    for (let i = start; i <= end; i++) {
        controls += '<li class="Pagination_item' + (i === n ? ' Pagination_item-current' : '') + '"><button onclick="render(' + i + ')">' + i + '</button></li>';
    }
    if (n < 12) controls += '<li class="Pagination_item Pagination_next"><button onclick="render(12)">»</button></li>';
    document.querySelector('ul').innerHTML = controls;
}
render(1);
</script>"""


def test_numeric_probe_crosses_window_and_detects_last_page_jump(page, tmp_path):
    page.set_content(WINDOW_PAGE)
    probe = probe_module.Probe(page, "hn", tmp_path, wait_ms=20)
    probe.save("initial", probe.snapshot())
    for expected in range(2, 9):
        state = probe.action("next_numeric", probe_module.HN_NEXT)
        assert state["active"] == [str(expected)]
        assert probe.events[-1]["delta"]["adjacent_page"] is True
        assert len(probe.events[-1]["delta"]["new_ids"]) == 1
    assert len(probe.seen) == 8
    # 窗口已不含第 1 页；同一动态 CSS 仍定位相邻页，双箭头则跳过三页。
    assert "1" not in [x["text"] for x in state["controls"][0]["controls"]]
    probe.action("last_page", ".Pagination_next button")
    assert probe.events[-1]["state"]["active"] == ["12"]
    assert probe.events[-1]["delta"]["ids_changed"] is True
    assert probe.events[-1]["delta"]["adjacent_page"] is False
    assert page.locator(f"css={probe_module.HN_NEXT}").count() == 0


def test_probe_refuses_repeated_controls(page, tmp_path):
    page.set_content('<button onclick="window.clicked=true">Next</button>' * 3)
    probe = probe_module.Probe(page, "hn", tmp_path, wait_ms=20)
    assert probe.action("next", "button") is None
    assert probe.events[-1]["outcome"] == "ambiguous_or_missing_control"
    assert probe.events[-1]["matches"] == 3
    assert page.evaluate("window.clicked === undefined")


def test_reordered_duplicate_batch_is_not_new_records():
    before = {"ids": ["a", "b"], "active": ["1"]}
    after = {"ids": ["b", "a"], "active": ["2"]}
    delta = probe_module.compare(before, after, {"a", "b"})
    assert delta["adjacent_page"] is True
    assert delta["ids_changed"] is True
    assert delta["new_ids"] == []
    assert delta["overlap_previous"] == 2


def test_conflicting_active_pages_have_no_order_evidence():
    before = {"ids": ["a"], "active": ["1", "1"]}
    after = {"ids": ["b"], "active": ["2", "5"]}
    assert probe_module.compare(before, after, {"a"})["adjacent_page"] is None


# 使用生产执行器遍历全部数字页码，确认窗口移动后不跳页，并在末页自然结束。
def test_executor_traverses_numeric_pages_through_window_to_natural_end(page):
    from autoscraper.scraping.executor import ScraperExecutor
    from autoscraper.scraping.spec import ScraperSpec

    page.route(
        "https://fixture.test/**",
        lambda route: route.fulfill(body=WINDOW_PAGE, content_type="text/html; charset=utf-8"),
    )
    spec = ScraperSpec.model_validate(
        {
            "start_url": "https://fixture.test/list",
            "item_selector": "article.Story",
            "fields": {"id": {"selector": "a", "extract": "attribute", "attribute": "href"}},
            "dedupe_by": ["id"],
            "max_items": 20,
            "max_pages": 20,
            "pagination": {
                "type": "click",
                "mode": "replace",
                "next_selector": probe_module.HN_NEXT,
            },
            "timeout_ms": 1000,
            "content_timeout_ms": 500,
        }
    )
    result = ScraperExecutor().run_page(spec, page)
    assert [record["id"] for record in result.records] == [
        f"https://news.ycombinator.com/item?id={n}" for n in range(1, 13)
    ]
    assert page.evaluate("window.visited") == list(range(1, 13))
    assert result.pages_visited == 12
    assert result.stop_reason == "no_next"
    assert result.duplicates == 0
    assert result.errors == []
