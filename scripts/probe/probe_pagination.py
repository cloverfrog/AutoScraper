# Issue #40：数字页码探针；只观察公开列表，不调用模型或修改生产分页协议。
"""Opt-in Playwright probe for numeric pagination."""

import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
from time import monotonic

from playwright.sync_api import Error as PlaywrightError, sync_playwright

from autoscraper.scraping.executor import ScrapeError, ScraperExecutor
from autoscraper.scraping.spec import ScraperSpec


HN_URL = "https://hn.algolia.com/?q=Python"
HN_NEXT = ".Pagination_item-current + .Pagination_item:not(.Pagination_next) button"

# 固定只读脚本：按稳定链接取身份，同时保留重复 DOM 节点数量与全部分页区域。
SNAPSHOT = r"""site => {
    const visible = e => !!e.getClientRects().length && getComputedStyle(e).visibility !== 'hidden';
    const roots = [...document.querySelectorAll('ul.Pagination')];
    const records = [...document.querySelectorAll('article.Story')].map(e =>
        e.querySelector('a[href*="news.ycombinator.com/item?id="]')?.href).filter(Boolean);
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
    // 验证循环可能把临时挑战参数放入 URL；报告只保留业务查询参数。
    const url = new URL(location.href);
    for (const key of [...url.searchParams.keys()]) {
        if (key.startsWith('__cf_chl')) url.searchParams.delete(key);
    }
    return {url: url.href, title: document.title, record_nodes: records.length,
        ids: [...new Set(records)], controls,
        active: controls.flatMap(x => x.active),
        scroll_top: root.scrollTop, scroll_height: root.scrollHeight,
        client_height: root.clientHeight,
        bottom: root.scrollTop + root.clientHeight >= root.scrollHeight - 2,
        blocked: /Performing security verification|Verify you are human|Just a moment|正在进行安全验证|请稍候/i
            .test(document.title + '\n' + text),
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

    def action(self, label, selector):
        before = self.snapshot()
        control = self.page.locator(f"css={selector}")
        details = {"selector": selector, "matches": control.count()}
        if control.count() != 1:
            self.save(label, before, **details, outcome="ambiguous_or_missing_control")
            return None
        if not control.is_visible() or not control.is_enabled():
            self.save(label, before, **details, outcome="unavailable_control")
            return None
        start = monotonic()
        control.click(timeout=10000)
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
    parser.add_argument("--verify-executor", action="store_true")
    args = parser.parse_args()
    if not (1000 <= args.wait_ms <= 15000 and 1 <= args.hn_steps <= 15):
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
            try:
                report["sites"]["hn"] = probe_hn(browser, args.output, args)
                if args.verify_executor and report["sites"]["hn"]["status"] == "observed":
                    report["sites"]["hn"]["executor"] = verify_hn_executor(browser, args)
            except (PlaywrightError, ScrapeError) as exc:
                report["sites"].setdefault("hn", {}).update(
                    status="error", error=str(exc)[:2000], evidence="hn.jsonl（若动作前已保存）"
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
