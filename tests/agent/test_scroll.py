"""Bounded scroll exploration reuses built-in scrolling and checks record growth."""

import asyncio
import json
from types import SimpleNamespace

import pytest

from autoscraper.scraping.progress import STATE_SCRIPT


@pytest.mark.parametrize("container", ["", "main"])
def test_scroll_tool_checks_multiple_rounds_in_one_call(monkeypatch, container):
    pytest.importorskip("browser_use")
    from browser_use import ActionResult
    from browser_use.tools.service import Tools
    from autoscraper.agent.tools import build_tools

    calls = []
    original_init = Tools.__init__

    async def scroll(**kwargs):
        calls.append(kwargs)
        return ActionResult(extracted_content="scrolled")

    def init(self, *args, **kwargs):
        original_init(self, *args, **kwargs)
        self.registry.registry.actions["scroll"].function = scroll

    monkeypatch.setattr(Tools, "__init__", init)
    tools = build_tools(content_timeout_ms=1000)
    states = iter([1, 2, 3, 3, 3])

    class Page:
        async def evaluate(self, script, options):
            if script != STATE_SCRIPT:
                calls.append("container")
                return ""
            count = next(states, 3)
            return json.dumps(dict(count=count, loading=False, end=False, bottom=True))

    async def current():
        return Page()

    session = SimpleNamespace(must_get_current_page=current)
    action = tools.registry.registry.actions["explore_scroll"].function
    result = asyncio.run(
        action(
            browser_session=session,
            item_selector="article",
            container_selector=container,
            no_change_limit=2,
            wait_ms=1,
        )
    )
    data = json.loads(result.extracted_content)
    assert data == dict(scrolls=4, initial_count=1, count=3, stop_reason="no_more_content")
    assert len(calls) == 4
    assert (calls[0] == "container") == bool(container)
