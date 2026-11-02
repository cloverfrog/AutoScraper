# 独立源数据核验器：站点选择器只用于答案证据，绝不提供给被测 Agent。
# capture 只写新目录中的候选答案；正式答案的冻结必须显式审阅、复制并提交。
import argparse
import os
import runpy
from contextlib import contextmanager
from http.server import ThreadingHTTPServer
from threading import Thread
from urllib.request import urlopen

from .common import ROOT, now, sha256, tasks, write_json
from .scoring import normalize, validate_truth


# 本地任务直接复用仓库示例服务器，每次使用系统分配的新端口。
@contextmanager
def task_site(task):
    if task["id"] != "local-network":
        yield task["url"]
        return
    handler = runpy.run_path(str(ROOT / "examples/network_demo.py"))["Handler"]
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}/"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


# 每份证据保存原始响应或渲染 HTML，而非从 AutoScraper 输出反推的正确答案。
def evidence(output, entries, content, url, suffix="html"):
    filename = f"source-{len(entries) + 1}.{suffix}"
    path = output / filename
    path.write_text(content, encoding="utf-8")
    entries.append({"file": filename, "url": url, "sha256": sha256(path)})


# 根据公开页面真实 DOM 独立采集；只有核验器包含这些站点知识。
def collect(task, output, *, executable_path=None):
    from playwright.sync_api import sync_playwright

    output.mkdir(parents=True, exist_ok=False)
    entries, records = [], []
    with task_site(task) as url:
        if task["id"] == "local-network":
            # 不走 Executor，也不读取示例 Spec；直接检查两页源 JSON。
            import json

            for number in (1, 2):
                source = url + f"api/items?page={number}"
                with urlopen(source, timeout=10) as response:
                    raw = response.read().decode("utf-8")
                evidence(output, entries, raw, source, "json")
                records.extend(json.loads(raw)["data"]["items"])
        else:
            with sync_playwright() as playwright:
                browser = playwright.chromium.launch(
                    executable_path=executable_path or os.getenv("AUTOSCRAPER_BROWSER_PATH")
                )
                try:
                    page = browser.new_page()
                    page.set_default_timeout(20000)
                    response = page.goto(url, wait_until="domcontentloaded", timeout=30000)
                    if response is None or response.status >= 400:
                        raise ValueError("Source navigation failed")
                    if task["id"].startswith("books"):
                        records = books(page, task, output, entries)
                    elif task["id"].startswith("quotes"):
                        records = quotes(page, task, output, entries)
                    else:
                        records = cars(page, task, output, entries)
                finally:
                    browser.close()
    # 标准答案只做已声明的文本/URL归一化，保留数字和布尔值的 JSON 类型。
    for row in records:
        for field, kind in task["fields"].items():
            if kind in {"text", "url"}:
                row[field] = normalize(row[field], kind)
            elif kind == "text_list":
                row[field] = list(normalize(row[field], kind))
    validate_truth(task, records)
    result = {
        "schema_version": 1,
        "task": task["id"],
        "captured_at": now(),
        "method": "independent source DOM/JSON; no AutoScraper extraction",
        "review": "candidate; requires source review before freezing",
        "records": records,
        "evidence": entries,
    }
    write_json(output / "candidate.json", result)
    return result


def books(page, task, output, entries):
    rows = []
    while len(rows) < task["count"]:
        page.locator("article.product_pod").first.wait_for()
        evidence(output, entries, page.content(), page.url)
        batch = page.locator("article.product_pod").evaluate_all("""nodes => nodes.map(n => ({
            title: n.querySelector('h3 a').getAttribute('title'),
            price: n.querySelector('.price_color').textContent,
            url: n.querySelector('h3 a').href
        }))""")
        rows.extend(batch[: task["count"] - len(rows)])
        if len(rows) < task["count"]:
            page.locator("li.next a").click()
    if task["id"] == "books-detail":
        for row in rows:
            response = page.goto(row["url"], wait_until="domcontentloaded")
            if response is None or response.status >= 400:
                raise ValueError("Detail navigation failed")
            row["description"] = page.locator("#product_description + p").inner_text()
            evidence(output, entries, page.content(), page.url)
    return rows


def quotes(page, task, output, entries):
    rows = []
    while len(rows) < task["count"]:
        page.locator(".quote").first.wait_for()
        evidence(output, entries, page.content(), page.url)
        batch = page.locator(".quote").evaluate_all("""nodes => nodes.map(n => ({
            quote: n.querySelector('.text').textContent,
            author: n.querySelector('.author').textContent,
            tags: [...n.querySelectorAll('.tags .tag')].map(t => t.textContent)
        }))""")
        if task["id"] == "quotes-scroll":
            rows = batch[: task["count"]]
            if len(rows) < task["count"]:
                # 加载提示的收起动画会改变页面高度，须等它隐藏后再滚到底。
                # 否则滚动事件可能在页面尚未稳定时触发，未达到站点的底部阈值。
                page.locator("#loading").wait_for(state="hidden")
                page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
                page.wait_for_function(
                    "n => document.querySelectorAll('.quote').length > n", arg=len(batch)
                )
        else:
            rows.extend(batch[: task["count"] - len(rows)])
            if len(rows) < task["count"]:
                previous = batch[0]["quote"]
                page.locator("li.next a").click()
                page.wait_for_function(
                    "s => {const n = document.querySelector('.quote .text'); return n && n.textContent !== s}",
                    arg=previous,
                )
    return rows


def cars(page, task, output, entries):
    while page.locator(".test-sites-card").count() < task["count"]:
        before = page.locator(".test-sites-card").count()
        page.get_by_role("button", name="Load More", exact=True).click()
        page.wait_for_function(
            "n => document.querySelectorAll('.test-sites-card').length > n", arg=before
        )
    evidence(output, entries, page.content(), page.url)
    return page.locator(".test-sites-card").evaluate_all("""nodes => nodes.map(n => ({
        name: n.querySelector('h3 a').textContent,
        description: n.querySelector('.description').textContent,
        year: [...n.querySelectorAll('.card-text')].find(p => p.textContent.trim().startsWith('Year:')).textContent,
        status: n.querySelector('.availability .badge').textContent,
        url: n.querySelector('h3 a').href
    }))""")[: task["count"]]


def main():
    from pathlib import Path

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--task", required=True, choices=list(tasks()))
    parser.add_argument("--capture", required=True, type=Path)
    parser.add_argument("--browser-path")
    args = parser.parse_args()
    collect(tasks()[args.task], args.capture, executable_path=args.browser_path)


if __name__ == "__main__":
    main()
