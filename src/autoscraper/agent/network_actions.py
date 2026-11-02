# 网络分页动作旁路取证：复用原 navigate/click/scroll，不注册或改变模型工具。
# 只读取动作前的真实控件身份；验证失败不阻止原动作，也不伪造分页成功。
"""Bind ordinary browser actions to observed CSS controls without new tools."""

import json
from urllib.parse import urldefrag


# 新动作固定请求开始序号，同时关闭上一动作的响应窗口。
def begin_action(capture, kind, page, baseline):
    for previous in capture.actions:
        if isinstance(previous, dict) and previous["end"] is None:
            previous["end"] = baseline
    action = dict(kind=kind, page=urldefrag(page).url, baseline=baseline, end=None,
                  destination=urldefrag(page).url, requested="", selectors=[], success=False)
    capture.actions.append(action)
    capture.actions = capture.actions[-40:]
    return action


# 只复核已经测试过的分页候选，不为了证据扫描页面或替模型寻找控件。
def known_selectors(tools, capture, kind, page):
    selectors = [selector for action_type, selector in capture.pagination_targets
                 if action_type == kind and selector]
    checks = tools.submission
    if checks is not None and kind in {"click", "link"}:
        for (url, root, field, attribute, _), result in checks.tests.items():
            if url == page and not field and result.get("count") == 1:
                if (kind == "link") == (attribute == "href"):
                    selectors.append(root)
    return list(dict.fromkeys(selectors))[:40]


# CSS 和目标 URL 作为固定脚本参数传入；返回值只含匹配过的选择器，不含页面内容。
LINK_MATCH_SCRIPT = """(selectors, target) => JSON.stringify(selectors.filter(selector => {
    try {
        const all = document.querySelectorAll(selector), el = all[0];
        if (all.length !== 1 || !el || el.href !== target) return false;
        const rect = el.getBoundingClientRect(), style = getComputedStyle(el);
        return !el.matches(':disabled') && el.getAttribute('aria-disabled') !== 'true'
            && style.visibility !== 'hidden' && style.visibility !== 'collapse'
            && rect.width > 0 && rect.height > 0;
    } catch { return false; }
}))"""

NODE_MATCH_FUNCTION = """function(selectors, kind) { return selectors.filter(selector => {
    try {
        const all = document.querySelectorAll(selector), el = all[0];
        if (all.length !== 1 || !el) return false;
        const rect = el.getBoundingClientRect(), style = getComputedStyle(el);
        if (rect.width <= 0 || rect.height <= 0 || style.visibility === 'hidden'
            || style.visibility === 'collapse') return false;
        if (kind === 'scroll') return this === el;
        if (el.matches(':disabled') || el.getAttribute('aria-disabled') === 'true') return false;
        return this === el || (el.matches('button,a,[role="button"]') && el.contains(this));
    } catch { return false; }
}); }"""


# 索引对应 browser-use 实际点击/滚动节点；解析失败时拒绝该动作的验证证据。
async def node_selectors(browser, inputs, selectors, kind):
    if not selectors:
        return []
    if inputs.get("index") is not None:
        node = await browser.get_element_by_index(inputs["index"])
    elif inputs.get("coordinate_x") is not None and inputs.get("coordinate_y") is not None:
        node = await browser.get_dom_element_at_coordinates(inputs["coordinate_x"], inputs["coordinate_y"])
    else:
        return []
    if node is None or not node.session_id:
        return []
    client = browser.cdp_client
    resolved = await client.send.DOM.resolveNode(
        params={"backendNodeId": node.backend_node_id}, session_id=node.session_id,
    )
    object_id = resolved.get("object", {}).get("objectId")
    if not object_id:
        return []
    result = await client.send.Runtime.callFunctionOn(params={
        "objectId": object_id, "functionDeclaration": NODE_MATCH_FUNCTION,
        "arguments": [{"value": selectors}, {"value": kind}], "returnByValue": True,
    }, session_id=node.session_id)
    if "exceptionDetails" in result:
        return []
    return result.get("result", {}).get("value", [])


# 无额外等待、导航或模型调用；网络探索尚未开始时，只记录既有动作边界。
async def observe_action(tools, capture, browser, kind, inputs):
    page_url = urldefrag(await browser.get_current_page_url()).url
    action = begin_action(capture, kind, page_url, capture.buffer.sequence)
    try:
        if kind == "navigate":
            action["requested"] = inputs["url"]
            selectors = known_selectors(tools, capture, "link", page_url)
            if selectors:
                page = await browser.must_get_current_page()
                action["selectors"] = json.loads(await page.evaluate(
                    LINK_MATCH_SCRIPT, selectors, inputs["url"],
                ))
        elif kind == "scroll" and inputs.get("down", True):
            if inputs.get("index") is None:
                action["selectors"] = [""]
            else:
                selectors = known_selectors(tools, capture, "scroll", page_url)
                action["selectors"] = await node_selectors(browser, inputs, selectors, "scroll")
        elif kind == "click":
            selectors = known_selectors(tools, capture, "click", page_url)
            action["selectors"] = await node_selectors(browser, inputs, selectors, "click")
    except Exception:
        # 旁路取证失败不能改变原工具动作的执行与返回；后续验证明确缺少分页证据。
        action["selectors"] = []
    return action
