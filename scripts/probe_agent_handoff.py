"""Paid, opt-in Agent takeover experiment for #21; local fixture, real LLM.

Adds a probe-only wait_for_user tool using the existing clarification continuation
transport. This does not implement production authentication or terminal takeover.
Each scenario is bounded to 12 main-model calls and two synthetic interventions.
"""

import argparse
import asyncio
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib.metadata import version
import json
import os
from pathlib import Path
from threading import Thread
import time
from unittest.mock import patch
from urllib.parse import urlsplit

from playwright.async_api import async_playwright
from playwright.sync_api import sync_playwright

from autoscraper.agent.clarification import (
    ClarificationRequired,
    ClarificationRequest,
    ClarificationSession,
)
from autoscraper.agent.explorer import generate_spec, build_task
from autoscraper.agent.llm import make_llm, make_wait_llm
from autoscraper.cli.agent import settings_from_env
from autoscraper.scraping.executor import ScraperExecutor
from autoscraper.workflow.requirements import TaskRequirements


TASK = (
    "Extract exactly 2 reports from the Research workspace, fields named title, price. "
    "The Personal workspace is outside the requested scope. Preserve the requested workspace."
)


@contextmanager
def fixture():
    requests = []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_GET(self):
            path = urlsplit(self.path).path
            cookie = SimpleCookie(self.headers.get("Cookie", ""))
            account = cookie.get("fixture_workspace")
            active = account.value if account else None
            requests.append({"path": path, "workspace": active})
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            if not active:
                html = (
                    '<h1>Reports</h1><main class="login-wall">Sign in required. '
                    "Research workspace reports are available after signing in.</main>"
                    '<a href="/signin">Sign in</a>'
                )
            else:
                workspace = (
                    "research"
                    if path.startswith("/workspace/research")
                    else "personal"
                    if path.startswith("/workspace/personal")
                    else active
                )
                label = workspace.title()
                html = (
                    f"<header>Signed in. Active workspace: {label}</header>"
                    '<nav><a href="/workspace/research/reports">Research reports</a> '
                    '<a href="/workspace/personal/reports">Personal reports</a></nav>'
                )
                if path == "/home":
                    html += "<h1>Welcome home</h1><p>Choose a workspace to view reports.</p>"
                else:
                    html += f"<h1>{label} workspace reports</h1>"
                    for title, price in [("Alpha", "$10"), ("Beta", "$20")]:
                        html += (
                            f'<article class="report"><h2>{label} {title}</h2>'
                            f'<span class="price">{price}</span></article>'
                        )
            self.wfile.write(html.encode())

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}", requests
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


# Only the simulated user mutates authentication. The Agent cannot access this code.
def intervene(browser, base, scenario):
    with sync_playwright() as playwright:
        attached = playwright.chromium.connect_over_cdp(browser.cdp_url)
        context = attached.contexts[0]
        page = context.pages[0]
        if scenario != "blocked":
            workspace = "personal" if scenario == "wrong_workspace" else "research"
            context.add_cookies([{"name": "fixture_workspace", "value": workspace, "url": base}])
        target = {
            "target": "/reports",
            "home": "/home",
            "blocked": "/signin",
            "wrong_workspace": "/workspace/personal/reports",
        }[scenario]
        page.goto(base + target)
        return context.storage_state()


def verify(spec, state, executable_path):
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(executable_path=executable_path)
        try:
            page = browser.new_context(storage_state=state).new_page()
            result = ScraperExecutor().run_page(spec, page)
            expected = [
                {"title": "Research Alpha", "price": "$10"},
                {"title": "Research Beta", "price": "$20"},
            ]
            return {
                "records": result.records,
                "errors": result.errors,
                "correct_scope_and_values": result.records == expected and not result.errors,
            }
        finally:
            browser.close()


async def trial(base, scenario, settings, resume_policy):
    from browser_use import ActionResult, BrowserSession
    import autoscraper.agent.tools as tool_module

    actions = []
    handoffs = []
    holder = {}
    state = {"cookies": [], "origins": []}
    real_build_tools = tool_module.build_tools
    llm = make_llm(settings)
    started = time.monotonic()

    class CapturedBrowserSession(BrowserSession):
        def __init__(self, **kwargs):
            super().__init__(**kwargs)
            holder["browser"] = self

    def handler(request):
        nonlocal state
        handoffs.append({"reason": request.question, "action_count": len(actions)})
        if len(handoffs) > 2:
            return None
        # Existing clarification invokes a synchronous handler outside agent.run.
        # Use a separate thread for Playwright's synchronous API.
        with ThreadPoolExecutor(max_workers=1) as pool:
            state = pool.submit(intervene, holder["browser"], base, scenario).result(timeout=30)
        return (
            "Continue."
            if resume_policy == "minimal"
            else "Continue. Browser state may have changed. Inspect the current page again, "
            "verify authentication and the Research workspace before resuming. "
            "Do not assume the operation succeeded or reuse old element indices."
        )

    clarification = ClarificationSession(handler=handler)

    def tools_factory(**kwargs):
        tools = real_build_tools(**kwargs)
        if scenario != "baseline":

            @tools.action(
                description="Request human browser takeover when authentication "
                "blocks the task. Explain observed evidence and what the user needs "
                "to do in reason. Stops all actions until the user selects Continue."
            )
            async def wait_for_user(reason: str):
                clarification.pending = ClarificationRequest(
                    question=reason, explanation="Experimental browser takeover request"
                )
                return ActionResult(
                    is_done=True,
                    success=False,
                    extracted_content="Waiting for the user to operate the browser.",
                )

        real_act = tools.act

        async def recorded_act(action, **kw):
            item = {
                "action": action.model_dump(exclude_unset=True, mode="json"),
                "handoffs_completed": len(handoffs),
            }
            actions.append(item)
            result = await real_act(action, **kw)
            item.update(
                error=result.error,
                success=result.success,
                is_done=result.is_done,
                output=(result.extracted_content or "")[:12000],
            )
            return result

        tools.act = recorded_act
        return tools

    def prompt(url, task):
        text = build_task(url, task)
        if scenario != "baseline":
            text = text.replace(
                "no login, form submission or purchases.",
                "no automated login, form submission or purchases. "
                "When authentication blocks access, use wait_for_user to let "
                "the user operate the browser. Never request credentials in chat.",
            )
        return text

    report = {
        "scenario": scenario,
        "resume_policy": resume_policy,
        "model": settings.model,
        "browser_use": version("browser-use"),
        "max_llm_calls": settings.max_steps,
        "handoffs": handoffs,
        "actions": actions,
    }
    try:
        with (
            patch("browser_use.BrowserSession", CapturedBrowserSession),
            patch.object(tool_module, "build_tools", tools_factory),
            patch("autoscraper.agent.explorer.build_task", prompt),
        ):
            spec = await generate_spec(
                base + "/reports",
                TASK,
                settings,
                requirements=TaskRequirements(fields=["title", "price"], count=2),
                llm=llm,
                wait_llm=make_wait_llm(settings),
                clarification=clarification,
            )
        report["status"] = "submitted"
        report["spec"] = spec.model_dump(mode="json")
        report["verification"] = await asyncio.to_thread(
            verify, spec, state, settings.executable_path
        )
    except ClarificationRequired:
        report["status"] = "needs_human"
    except Exception as exc:
        report.update(status="stopped", error_type=type(exc).__name__, error=str(exc)[:1500])
    report.update(llm_calls=llm.calls, seconds=round(time.monotonic() - started, 2))
    return report


async def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--scenario",
        nargs="+",
        default=["baseline", "target", "home", "blocked", "wrong_workspace"],
        choices=["baseline", "target", "home", "blocked", "wrong_workspace"],
    )
    parser.add_argument("--resume-policy", choices=["minimal", "reobserve"], default="minimal")
    parser.add_argument("--output", type=Path, default=Path("output/agent-handoff-probe"))
    args = parser.parse_args()
    os.environ.setdefault("ANONYMIZED_TELEMETRY", "false")
    os.environ.setdefault("BROWSER_USE_CLOUD_SYNC", "false")
    async with async_playwright() as p:
        executable_path = os.getenv("AUTOSCRAPER_BROWSER_PATH") or p.chromium.executable_path
    settings = settings_from_env(
        max_steps=12,
        timeout_seconds=240,
        content_timeout_ms=2000,
        max_wait_llm_calls=0,
        executable_path=executable_path,
    )
    args.output.mkdir(parents=True, exist_ok=True)
    for scenario in args.scenario:
        print(f"Starting {scenario} ({args.resume_policy})", flush=True)
        with fixture() as (base, requests):
            report = await trial(base, scenario, settings, args.resume_policy)
            report["requests"] = requests
        path = args.output / f"{scenario}-{args.resume_policy}.json"
        path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        print(
            json.dumps(
                {k: v for k, v in report.items() if k not in {"actions", "requests", "spec"}},
                ensure_ascii=False,
            ),
            flush=True,
        )


if __name__ == "__main__":
    asyncio.run(main())
