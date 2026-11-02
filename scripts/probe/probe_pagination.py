# Issue #40：真实网站分页探针；只观察公开列表，不调用模型或修改生产分页协议。
"""Opt-in Playwright probe for numeric and mixed pagination."""

import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
from time import monotonic

from playwright.sync_api import Error as PlaywrightError, sync_playwright

from autoscraper.scraping.executor import ScrapeError, ScraperExecutor
from autoscraper.scraping.scrolling import scroll_step
from autoscraper.scraping.spec import ScraperSpec


HN_URL = "https://hn.algolia.com/?q=Python"
INAT_URL = "https://www.inaturalist.org/observations?taxon_id=47126"
HN_NEXT = ".Pagination_item-current + .Pagination_item:not(.Pagination_next) button"

# 固定只读脚本：按稳定链接取身份，同时保留重复 DOM 节点数量与全部分页区域。
# iNaturalist 身份链接只是待验证的探针假设，页面不可达时不能作为有效规则取证。
SNAPSHOT = r"""site => {
    const visible = e => !!e.getClientRects().length && getComputedStyle(e).visibility !== 'hidden';
    const roots = [...document.querySelectorAll(site === 'hn' ? 'ul.Pagination' : 'ul.pagination')];
    const records = site === 'hn'
        ? [...document.querySelectorAll('article.Story')].map(e =>
            e.querySelector('a[href*="news.ycombinator.com/item?id="]')?.href).filter(Boolean)
        : [...document.querySelectorAll('a[href]')].map(e => e.href)
            .filter(h => /^https:\/\/www\.inaturalist\.org\/observations\/\d+(?:[?#]|$)/.test(h))
            .map(h => h.split(/[?#]/)[0]);
    const root = document.scrollingElement;
    const controls = roots.map((e, group) => ({group, visible: visible(e),
        active: [...e.querySelectorAll('.Pagination_item-current, .active, [aria-current="page"]')]
            .map(x => x.textContent.trim()),
        controls: [...e.querySelectorAll('a,button')].map(x => ({text: x.textContent.trim(),
            href: x.getAttribute('href'), class: x.parentElement.className,
            aria_label: x.getAttribute('aria-label'), visible: visible(x),
            disabled: x.matches(':disabled') || x.getAttribute('aria-disabled') === 'true',
            html: x.outerHTML.slice(0, 1500)}))}));
    const text = document.body?.innerText || '';
    return {url: location.href, title: document.title, record_nodes: records.length,
        ids: [...new Set(records)], controls,
        active: controls.flatMap(x => x.active),
        scroll_top: root.scrollTop, scroll_height: root.scrollHeight,
        client_height: root.clientHeight,
        bottom: root.scrollTop + root.clientHeight >= root.scrollHeight - 2,
        blocked: /Performing security verification|Verify you are human|Just a moment/i.test(text),
        loading: [...document.querySelectorAll('[aria-busy="true"], .loading, .spinner')]
            .filter(visible).slice(0, 10).map(e => ({tag: e.tagName, class: e.className})),
        body_excerpt: records.length ? null : text.slice(0, 1200)};
}"""


# 页码、身份新增和内容变化分开报告；不同记录不能独立证明顺序正确。
def compare(before, after, seen):
    old, current = set(before["ids"]), set(after["ids"])
    previous_page = page_number(before)
    current_page = page_number(after)
    return {
        "new_ids": sorted(current - seen),
        "overlap_previous": len(old & current),
        "removed_ids": sorted(old - current),
        "ids_changed": before["ids"] != after["ids"],
        "adjacent_page": (
            current_page == previous_page + 1
            if previous_page is not None and current_page is not None
            else None
        ),
    }


def page_number(state):
    numbers = {int(x) for x in state["active"] if x.isdigit()}
    return next(iter(numbers)) if len(numbers) == 1 else None


# 每个动作等待完整观察窗口，避免活动页码先变、旧记录尚未替换时提前判定成功。
# 该窗口仅用于有限实验；超时不推断自然结束，也不自动触发另一种分页动作。
class Probe:
    def __init__(self, page, site, output, wait_ms):
        self.page, self.site, self.output, self.wait_ms = page, site, output, wait_ms
        self.events = []
        self.seen = set()

    def snapshot(self):
        return self.page.evaluate(SNAPSHOT, self.site)

    def save(self, action, state, **details):
        event = {"step": len(self.events), "action": action, "state": state, **details}
        self.events.append(event)
        self.seen.update(state["ids"])
        with (self.output / f"{self.site}.jsonl").open("a") as stream:
            stream.write(json.dumps(event, ensure_ascii=False) + "\n")
        summary = {key: value for key, value in details.items() if key not in {"delta", "before"}}
        if "delta" in details:
            delta = details["delta"]
            summary.update(new_ids=len(delta["new_ids"]), adjacent_page=delta["adjacent_page"])
        print(
            json.dumps(
                {
                    "site": self.site,
                    "action": action,
                    "ids": len(state["ids"]),
                    "active": state["active"],
                    **summary,
                },
                ensure_ascii=False,
            ),
            flush=True,
        )
        return state

    def open(self, url):
        response = self.page.goto(url, wait_until="domcontentloaded", timeout=45000)
        self.page.wait_for_timeout(self.wait_ms)
        state = self.save(
            "initial", self.snapshot(), http_status=response.status if response else None
        )
        self.page.screenshot(path=str(self.output / f"{self.site}-initial.png"))
        return state

    def action(self, label, selector=None):
        before = self.snapshot()
        details = {}
        if selector:
            control = self.page.locator(f"css={selector}")
            details = {"selector": selector, "matches": control.count()}
            if control.count() != 1:
                self.save(label, before, **details, outcome="ambiguous_or_missing_control")
                return None
            if not control.is_visible() or not control.is_enabled():
                self.save(label, before, **details, outcome="unavailable_control")
                return None
        start = monotonic()
        if selector:
            control.click(timeout=10000)
        else:
            scroll_step(self.page, None)
        self.page.wait_for_timeout(self.wait_ms)
        after = self.snapshot()
        delta = compare(before, after, self.seen)
        self.save(
            label,
            after,
            **details,
            before=before,
            delta=delta,
            elapsed_ms=round((monotonic() - start) * 1000),
        )
        return after


# 数字分页连续跨越初始窗口；另建实验页面测试末页/首页，避免污染顺序遍历身份集合。
def probe_hn(browser, output, args):
    context = browser.new_context()
    try:
        probe = Probe(context.new_page(), "hn", output, args.wait_ms)
        initial = probe.open(HN_URL)
        if initial["blocked"] or not initial["ids"]:
            return {"status": "blocked_or_no_records", "events": probe.events}
        for _ in range(args.hn_steps):
            state = probe.action("next_numeric", HN_NEXT)
            if state is None or not probe.events[-1]["delta"]["adjacent_page"]:
                break
            if not probe.events[-1]["delta"]["new_ids"]:
                break
        probe.page.screenshot(path=str(output / "hn-sequential.png"))
        sequential = list(probe.events)
        unique_records = len(probe.seen)
        state = probe.open(HN_URL)
        if state["ids"] and not state["blocked"]:
            state = probe.action("double_arrow_right", ".Pagination_next button")
            if state is not None:
                probe.action("double_arrow_left", ".Pagination_previous button")
        probe.page.screenshot(path=str(output / "hn-final.png"))
        return {
            "status": "observed",
            "sequential_unique_records": unique_records,
            "sequential_events": sequential,
            "events": probe.events,
        }
    finally:
        context.close()


# 混合分页分别观察滚动、独立点击和滚动后点击；不预设网站的自动切换时机。
# 点击必须由显式选择器唯一定位，多个等价按钮也不静默取第一个。
def probe_inaturalist(browser, output, args):
    context = browser.new_context()
    try:
        probe = Probe(context.new_page(), "inaturalist", output, args.wait_ms)
        state = probe.open(INAT_URL)
        if state["blocked"] or not state["ids"]:
            return {
                "status": "blocked_or_no_records",
                "events": probe.events,
                "limitation": "未取得列表，不能推断滚动和页码衔接机制",
            }
        if args.inat_next_selector:
            probe.action("independent_click", args.inat_next_selector)
            probe.open(INAT_URL)
        # 重置独立点击对照的身份集合，滚动阶段只与自己的初始列表比较。
        probe.seen = set(probe.snapshot()["ids"])
        for _ in range(args.inat_scrolls):
            state = probe.action("scroll")
            if state["blocked"]:
                break
        if args.inat_next_selector and not state["blocked"]:
            probe.action("click_after_scroll", args.inat_next_selector)
        probe.page.screenshot(path=str(output / "inaturalist-final.png"))
        return {
            "status": "observed",
            "events": probe.events,
            "limitation": "无新增不证明自然结束；未配置唯一点击控件时仅观察滚动",
        }
    finally:
        context.close()


# 独立运行现有声明式执行器，检验动态 CSS 能否直接复用；不增加生产协议类型。
def verify_hn_executor(browser, args):
    spec = ScraperSpec.model_validate(
        {
            "version": "2",
            "start_url": HN_URL,
            "item_selector": "article.Story",
            "fields": {
                "id": {
                    "selector": ".Story_title > a:first-child",
                    "extract": "attribute",
                    "attribute": "href",
                },
                "title": {"selector": ".Story_title > a:first-child"},
            },
            "pagination": {"type": "click", "next_selector": HN_NEXT, "mode": "replace"},
            "dedupe_by": ["id"],
            "max_items": (args.hn_steps + 1) * 30,
            "max_pages": args.hn_steps + 1,
            "timeout_ms": 30000,
            "content_timeout_ms": 15000,
            "on_item_error": "fail",
        }
    )
    context = browser.new_context()
    try:
        page = context.new_page()
        result = ScraperExecutor().run_page(spec, page)
        return {
            "spec": spec.model_dump(mode="json", exclude_none=True),
            "records": len(result.records),
            "pages": result.pages_visited,
            "stop_reason": result.stop_reason,
            "duplicates": result.duplicates,
            "errors": result.errors,
            "final_active": page.evaluate(SNAPSHOT, "hn")["active"],
            "ids": [record["id"] for record in result.records],
        }
    finally:
        context.close()


# 每次运行用独立输出目录；失败保存已收集事件及错误，不将访问阻塞记为分页通过。
def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--site", choices=["all", "hn", "inaturalist"], default="all")
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("output/pagination-probe")
        / datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ"),
    )
    parser.add_argument("--browser-path", default=os.getenv("AUTOSCRAPER_BROWSER_PATH"))
    parser.add_argument("--headed", action="store_true")
    parser.add_argument("--wait-ms", type=int, default=4000)
    parser.add_argument("--hn-steps", type=int, default=8)
    parser.add_argument("--inat-scrolls", type=int, default=8)
    parser.add_argument("--inat-next-selector")
    parser.add_argument("--verify-executor", action="store_true")
    args = parser.parse_args()
    if not (
        1000 <= args.wait_ms <= 15000 and 1 <= args.hn_steps <= 15 and 1 <= args.inat_scrolls <= 15
    ):
        parser.error("wait-ms 须在 1000..15000，动作次数须在 1..15")
    args.output.mkdir(parents=True, exist_ok=False)
    report = {
        "started_at": datetime.now(timezone.utc).isoformat(),
        "settings": {**vars(args), "output": str(args.output)},
        "sites": {},
    }
    with sync_playwright() as p:
        browser = p.chromium.launch(executable_path=args.browser_path, headless=not args.headed)
        report["browser_version"] = browser.version
        try:
            for name, runner in [("hn", probe_hn), ("inaturalist", probe_inaturalist)]:
                if args.site not in {"all", name}:
                    continue
                try:
                    report["sites"][name] = runner(browser, args.output, args)
                    if (
                        name == "hn"
                        and args.verify_executor
                        and report["sites"][name]["status"] == "observed"
                    ):
                        report["sites"][name]["executor"] = verify_hn_executor(browser, args)
                except (PlaywrightError, ScrapeError) as exc:
                    report["sites"].setdefault(name, {}).update(
                        status="error",
                        error=str(exc)[:2000],
                        evidence=f"{name}.jsonl（若动作前已保存）",
                    )
        finally:
            browser.close()
    (args.output / "report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n"
    )
    print(f"报告：{args.output / 'report.json'}", flush=True)
    return 1 if any(s["status"] != "observed" for s in report["sites"].values()) else 0


if __name__ == "__main__":
    raise SystemExit(main())
