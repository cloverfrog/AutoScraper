"""Issue #21: opt-in, local-only browser authentication interoperability probe.

No LLM, real accounts, or production auth support. State stays in memory.
The login button simulates a completed user login; terminal takeover is not tested.
"""

import argparse
import asyncio
from contextlib import contextmanager
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib.metadata import version
import json
import os
from pathlib import Path
import secrets
import sys
from threading import Thread
from urllib.parse import parse_qs, urlsplit

from playwright.async_api import async_playwright
from playwright.sync_api import Error as PlaywrightError, sync_playwright

from autoscraper.scraping.executor import ContentTimeout, ScraperExecutor
from autoscraper.scraping.spec import ScraperSpec


# Each fixture has an independent origin and synthetic token. Nothing is persisted.
@contextmanager
def auth_site(mode):
    token = secrets.token_urlsafe(24)
    storage = "localStorage" if mode == "local" else "sessionStorage"

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def send(self, body, status=200, cookie=None):
            self.send_response(status)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            if cookie:
                self.send_header("Set-Cookie", cookie)
            self.end_headers()
            self.wfile.write(body.encode())

        def do_GET(self):
            url = urlsplit(self.path)
            if url.path == "/login":
                action = (
                    "await fetch('/authenticate')"
                    if mode == "cookie"
                    else f"{storage}.setItem('probe_auth', {json.dumps(token)})"
                )
                self.send(f"""<button onclick="login()">Complete fixture login</button>
                    <script>async function login() {{ {action};
                    location.href='/list'; }}</script>""")
                return
            if url.path == "/authenticate" and mode == "cookie":
                self.send("ok", cookie=f"probe_auth={token}; HttpOnly; SameSite=Lax; Path=/")
                return
            if url.path == "/list":
                number = int(parse_qs(url.query).get("page", ["1"])[0])
                content = (
                    f'<article><h2>Record {number}</h2><a href="/detail?id={number}">Detail</a>'
                    "</article>"
                    + ('<a class="next" href="/list?page=2">Next</a>' if number == 1 else "")
                )
            elif url.path == "/detail":
                number = int(parse_qs(url.query).get("id", ["1"])[0])
                content = f'<p class="description">Description {number}</p>'
            else:
                self.send("Not found", 404)
                return
            if mode == "cookie":
                cookies = SimpleCookie(self.headers.get("Cookie", ""))
                authenticated = cookies.get("probe_auth")
                # A 200 login page reproduces the current content_timeout behavior.
                self.send(
                    content
                    if authenticated and authenticated.value == token
                    else '<main id="login-required">Please sign in</main>'
                )
            else:
                self.send(f"""<main id="login-required">Please sign in</main><script>
                    if ({storage}.getItem('probe_auth') === {json.dumps(token)})
                        document.body.innerHTML = {json.dumps(content)};
                    </script>""")

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()

    def expire():
        nonlocal token
        token = secrets.token_urlsafe(24)

    try:
        yield f"http://127.0.0.1:{server.server_port}", expire
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def rule(base, count):
    return ScraperSpec.model_validate(
        {
            "version": "2",
            "start_url": base + "/list",
            "item_selector": "article",
            "fields": {"title": {"selector": "h2"}},
            "detail": {
                "url_selector": "a",
                "fields": {"description": {"selector": ".description"}},
            },
            "pagination": {"type": "link", "next_selector": ".next"},
            "max_items": count,
            "max_pages": 2,
            "timeout_ms": 5000,
            "content_timeout_ms": 800,
            "on_item_error": "fail",
        }
    )


# Use the real Executor's caller-owned Page entry, without adding production APIs.
def extract(base, state, executable_path, count):
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(executable_path=executable_path)
        try:
            try:
                context = browser.new_context(storage_state=state)
            except PlaywrightError as exc:
                # Session-only browser-use origins lack Playwright's required localStorage array.
                # Do not print exception text: other state errors could contain token values.
                if "localStorage: expected array, got undefined" not in str(exc):
                    raise
                return {"status": "invalid_storage_state", "records": 0}
            try:
                result = ScraperExecutor().run_page(rule(base, count), context.new_page())
            except ContentTimeout:
                return {"status": "content_timeout", "records": 0}
            expected = [
                {"title": f"Record {i}", "description": f"Description {i}"}
                for i in range(1, count + 1)
            ]
            assert not result.errors and result.records == expected
            return {
                "status": "success",
                "records": len(result.records),
                "pages": result.pages_visited,
                "details": result.detail_pages_visited,
            }
        finally:
            browser.close()


# Attach Playwright to a browser-use-owned browser; only browser-use closes it.
async def capture(base, executable_path):
    from browser_use import BrowserSession

    browser = BrowserSession(
        headless=True,
        executable_path=executable_path,
        enable_default_extensions=False,
        keep_alive=True,
    )
    try:
        await browser.start()
        async with async_playwright() as playwright:
            attached = await playwright.chromium.connect_over_cdp(browser.cdp_url)
            context = attached.contexts[0]
            page = context.pages[0] if context.pages else await context.new_page()
            await page.goto(base + "/login")
            await page.get_by_role("button").click()
            await page.locator("article").wait_for(timeout=5000)
            return {
                "browser_use_export": await browser.export_storage_state(),
                "browser_use_internal": await browser._cdp_get_storage_state(),
                "playwright_cdp": await context.storage_state(),
            }
    finally:
        await asyncio.wait_for(browser.kill(), timeout=20)


# Repair starts a fresh browser-use session. Verify the reverse import separately.
async def restore_agent(base, state, executable_path):
    from browser_use import BrowserSession

    browser = BrowserSession(
        headless=True,
        executable_path=executable_path,
        enable_default_extensions=False,
        keep_alive=True,
        storage_state=state,
    )
    try:
        await browser.start()
        async with async_playwright() as playwright:
            attached = await playwright.chromium.connect_over_cdp(browser.cdp_url)
            context = attached.contexts[0]
            page = context.pages[0] if context.pages else await context.new_page()
            await page.goto(base + "/list")
            return await page.locator("article").count() == 1
    finally:
        await asyncio.wait_for(browser.kill(), timeout=20)


async def probe(executable_path):
    report = {
        "browser_use": version("browser-use"),
        "playwright": version("playwright"),
        "modes": {},
    }
    for mode in ("cookie", "local", "session"):
        print(f"Probing {mode}", file=sys.stderr, flush=True)
        with auth_site(mode) as (base, expire):
            states = await capture(base, executable_path)
            results = {}
            # Source browser has already been killed before any independent extraction.
            for name, state in {"empty": None, **states}.items():
                preview = await asyncio.to_thread(extract, base, state, executable_path, 1)
                full = await asyncio.to_thread(extract, base, state, executable_path, 2)
                expected = mode != "session" and name != "empty"
                if mode == "local" and name == "browser_use_export":
                    expected = False
                assert (preview["status"] == "success") == expected, (mode, name, preview)
                assert (full["status"] == "success") == expected, (mode, name, full)
                if mode == "session" and name == "browser_use_internal":
                    assert preview["status"] == full["status"] == "invalid_storage_state"
                elif not expected:
                    assert preview["status"] == full["status"] == "content_timeout"
                results[name] = {"preview": preview, "full": full}
            restored = await restore_agent(base, states["playwright_cdp"], executable_path)
            assert restored == (mode != "session")
            # Revoke at the same origin, leaving the captured snapshot untouched.
            expire()
            expired = await asyncio.to_thread(
                extract, base, states["playwright_cdp"], executable_path, 1
            )
            assert expired["status"] == "content_timeout"
            report["modes"][mode] = {
                "exports": results,
                "expired_state": expired["status"],
                "agent_restore": restored,
            }
            print(f"Verified {mode}", file=sys.stderr, flush=True)
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--browser-path", default=os.getenv("AUTOSCRAPER_BROWSER_PATH"))
    parser.add_argument("--report", type=Path, help="Write only non-sensitive probe results")
    args = parser.parse_args()
    os.environ.setdefault("ANONYMIZED_TELEMETRY", "false")
    os.environ.setdefault("BROWSER_USE_CLOUD_SYNC", "false")
    # browser-use needs full Chromium, rather than Playwright's headless shell.
    with sync_playwright() as playwright:
        executable_path = args.browser_path or playwright.chromium.executable_path
    report = asyncio.run(probe(executable_path))
    output = json.dumps(report, ensure_ascii=False, indent=2) + "\n"
    if args.report:
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(output, encoding="utf-8")
    print(output)


if __name__ == "__main__":
    main()
