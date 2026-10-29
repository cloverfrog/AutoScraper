# 执行器的滚动动作：参考 browser-use，页面发送 CDP 手势，CSS 容器发送真实滚轮输入。
# 只负责一次动作和短暂稳定等待；加载、记录推进与结束判断仍由执行器管理。
"""Deterministic browser input for one bounded scroll step."""

from playwright.sync_api import Page


# 固定脚本只读取目标几何信息；容器取可见部分的中心，防止滚轮落到容器之外。
GEOMETRY_SCRIPT = """(selector) => {
    if (!selector) return {x: innerWidth / 2, y: innerHeight / 2,
        height: visualViewport?.height || document.documentElement.clientHeight};
    const elements = document.querySelectorAll(selector);
    if (elements.length !== 1) throw new Error('Scroll container must match exactly one element');
    const el = elements[0], rect = el.getBoundingClientRect();
    const left = Math.max(0, rect.left), right = Math.min(innerWidth, rect.right);
    const top = Math.max(0, rect.top), bottom = Math.min(innerHeight, rect.bottom);
    if (right <= left || bottom <= top) throw new Error('Scroll container must be visible');
    return {x: (left + right) / 2, y: (top + bottom) / 2, height: el.clientHeight};
}"""


# 页面复用 browser-use 的 CDP 滚动手势参数；容器先定位到可见区域再发送滚轮。
# CDP 连接仅属于当前动作，结束时 detach，避免重复动作累积会话。
def scroll_step(page: Page, container_selector: str | None):
    if container_selector:
        page.locator(f"css={container_selector}").scroll_into_view_if_needed()
    geometry = page.evaluate(GEOMETRY_SCRIPT, container_selector)
    if container_selector:
        page.mouse.move(geometry["x"], geometry["y"])
        page.mouse.wheel(0, max(1, geometry["height"]))
    else:
        session = page.context.new_cdp_session(page)
        try:
            session.send(
                "Input.synthesizeScrollGesture",
                {
                    "x": geometry["x"],
                    "y": geometry["y"],
                    "xDistance": 0,
                    "yDistance": -max(1, geometry["height"]),
                    "speed": 50000,
                },
            )
        finally:
            session.detach()
    # 与 browser-use 的连续滚动节奏一致，给滚动事件和布局更新留出短暂时间。
    page.wait_for_timeout(150)
