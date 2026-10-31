# 浏览器人工接管与任务内认证快照：在探索、修复和执行之间复用登录状态。
# 状态不进入提示词或追踪；只有调用方显式指定文件时才持久化。
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

from autoscraper.exceptions import HandoffRequired
from autoscraper.scraping.spec import NonEmpty, ProtocolModel


class HandoffRequest(ProtocolModel):
    reason: NonEmpty


HandoffHandler = Callable[[HandoffRequest], Literal["continue", "cancel"] | None]


@dataclass
class BrowserAccess:
    handler: HandoffHandler | None = field(default=None, repr=False)
    state: dict | None = field(default=None, repr=False)  # 凭据不得出现在对象的诊断表示中。
    pending: HandoffRequest | None = None
    waiting_seconds: float = 0.0
    handed_off: bool = False

    # 在 Agent 动作批次和超时作用域之外接收控制决定，等待时长供工作流扣除。
    # continue 只表示用户交还控制权，页面是否已就绪仍由恢复后的 Agent 检查。
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

    # 采用 Playwright 标准快照；browser-use 的公开导出接口会遗漏 localStorage。
    async def capture(self, cdp_url: str) -> None:
        """Use Playwright's standard state format; browser-use export omits localStorage."""
        from playwright.async_api import Error as PlaywrightError, async_playwright

        async with async_playwright() as playwright:
            attached = await playwright.chromium.connect_over_cdp(cdp_url)
            if not attached.contexts:
                raise RuntimeError("Browser context unavailable after user operation")
            state = await attached.contexts[0].storage_state()
            # 晚接入的 Playwright 客户端可能不知道页面已有的 Origin，需补读现有 frame。
            # 合并本任务先前捕获的 Origin，保留已关闭或跳离页面的状态；当前值覆盖旧值。
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
                        continue  # 导出期间 frame 可能已被页面移除。
                    if entry is not None:
                        origins[entry["origin"]] = entry
            state["origins"] = list(origins.values())
            self.state = state
            # 这里只断开临时 Playwright 连接；浏览器的关闭仍由创建它的调用方负责。

    # 先校验快照的基础结构，再交给浏览器；统一错误信息，避免带出包含凭据的原文。
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

    # 在同目录私有临时文件中写完后原子替换，避免凭据文件权限过宽或只写入一部分。
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
