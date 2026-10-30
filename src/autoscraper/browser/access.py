"""Manual browser handoff and task-local authentication snapshots.

Snapshots never enter prompts or tracing. Only an explicit caller path persists them.
"""

from collections.abc import Callable
from dataclasses import dataclass, field
import json
import os
from pathlib import Path
import tempfile
import time
from typing import Literal

from autoscraper.scraping.spec import NonEmpty, ProtocolModel


class HandoffRequest(ProtocolModel):
    reason: NonEmpty


class HandoffRequired(Exception):
    def __init__(self, request: HandoffRequest, *, cancelled: bool = False):
        super().__init__(
            "Browser operation cancelled" if cancelled else "User browser operation required"
        )
        self.request = request
        self.cancelled = cancelled


HandoffHandler = Callable[[HandoffRequest], Literal["continue", "cancel"] | None]


@dataclass
class BrowserAccess:
    handler: HandoffHandler | None = field(default=None, repr=False)
    state: dict | None = field(default=None, repr=False)
    pending: HandoffRequest | None = None
    waiting_seconds: float = 0.0
    handed_off: bool = False

    def resume(self) -> None:
        """Run outside Agent timeouts and action batches. Continue is not login proof."""
        assert self.pending is not None
        started = time.monotonic()
        try:
            answer = self.handler(self.pending) if self.handler else None
        except EOFError:
            answer = None
        except KeyboardInterrupt:
            answer = "cancel"
        finally:
            self.waiting_seconds += time.monotonic() - started
        if answer != "continue":
            raise HandoffRequired(self.pending, cancelled=answer == "cancel")
        self.handed_off = True
        self.pending = None

    async def capture(self, cdp_url: str) -> None:
        """Use Playwright's standard state format; browser-use export omits localStorage."""
        from playwright.async_api import Error as PlaywrightError, async_playwright

        async with async_playwright() as playwright:
            attached = await playwright.chromium.connect_over_cdp(cdp_url)
            if not attached.contexts:
                raise RuntimeError("Browser context unavailable after user operation")
            state = await attached.contexts[0].storage_state()
            # A newly attached Playwright client may not know origins navigated before
            # it connected. Read live frame storage without navigating or reloading.
            origins = {entry["origin"]: entry for entry in (self.state or {}).get("origins", [])}
            origins.update({entry["origin"]: entry for entry in state["origins"]})
            for page in attached.contexts[0].pages:
                for frame in page.frames:
                    try:
                        entry = await frame.evaluate("""() => {
                            if (!/^https?:$/.test(location.protocol)) return null;
                            return {origin: location.origin, localStorage:
                                Object.entries(localStorage).map(([name, value]) => ({name, value}))};
                        }""")
                    except PlaywrightError:
                        continue  # A frame may detach during capture.
                    if entry is not None:
                        origins[entry["origin"]] = entry
            state["origins"] = list(origins.values())
            self.state = state
            # Stopping this Playwright connection detaches; the caller owns the browser.

    def load(self, path: Path) -> None:
        try:
            state = json.loads(path.read_text(encoding="utf-8"))
            if (
                not isinstance(state, dict)
                or not isinstance(state.get("cookies"), list)
                or not isinstance(state.get("origins"), list)
            ):
                raise ValueError
            for cookie in state["cookies"]:
                if not isinstance(cookie, dict) or any(
                    not isinstance(cookie.get(key), str)
                    for key in ("name", "value", "domain", "path")
                ):
                    raise ValueError
            for origin in state["origins"]:
                if (
                    not isinstance(origin, dict)
                    or not isinstance(origin.get("origin"), str)
                    or not isinstance(origin.get("localStorage"), list)
                ):
                    raise ValueError
                if any(
                    not isinstance(item, dict)
                    or not isinstance(item.get("name"), str)
                    or not isinstance(item.get("value"), str)
                    for item in origin["localStorage"]
                ):
                    raise ValueError
        except (ValueError, UnicodeError):
            raise ValueError("Invalid authentication state file") from None
        self.state = state

    def save(self, path: Path) -> None:
        if self.state is None:
            return
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        temporary = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="w", encoding="utf-8", dir=path.parent, delete=False
            ) as stream:
                temporary = Path(stream.name)
                os.chmod(temporary, 0o600)
                json.dump(self.state, stream, ensure_ascii=False)
                stream.write("\n")
            os.replace(temporary, path)
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)
