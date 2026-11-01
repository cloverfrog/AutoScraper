# 动态分页的共享状态检测：供执行器和 Agent 探索工具读取记录、加载和结束状态。
# 脚本由应用固定提供；选择器和字段规则只作为数据传入，不接受模型生成的源码。
"""Application-owned scripts shared by deterministic execution and bounded exploration."""

# 追加列表只需记录数；替换列表才生成声明字段的快照，避免把动画等无关 DOM 变化当作推进。
# 快照归一化与提取规则一致；真实空值也可推进，异步加载由显式 loading 标志区分。
# 加载/结束标志按可见状态判断；bottom 仅作为连续无新增推断的前提，不独立证明结束。
STATE_SCRIPT = """(o) => {
    const visible = selector => selector && [...document.querySelectorAll(selector)]
        .some(e => e.getClientRects().length && getComputedStyle(e).visibility !== 'hidden');
    const items = o.item_selector ? [...document.querySelectorAll(o.item_selector)] : [];
    const root = o.container_selector ? document.querySelector(o.container_selector)
        : document.scrollingElement;
    if (!root) throw new Error('Scroll container not found');
    const snapshot = o.fields ? items.map(item => Object.values(o.fields).map(f => {
        const matches = f.selector === ':scope' ? [item] : [...item.querySelectorAll(f.selector)];
        if (f.extract === 'count') return matches.length;
        if (f.extract === 'texts') return matches.map(el =>
            (el.innerText ?? el.textContent).replace(/\\s+/g, ' ').trim());
        const el = matches[0];
        const value = el ? (f.extract === 'attribute' ? el.getAttribute(f.attribute) : (el.innerText ?? el.textContent)) : null;
        return value === null ? null : value.replace(/\\s+/g, ' ').trim();
    })) : [];
    // 真实空值不代表尚未就绪；异步占位须通过显式 loading 标志区分。
    const ready = true;
    return {ready, count: items.length, snapshot: JSON.stringify(snapshot),
        loading: !!visible(o.loading_selector), end: !!visible(o.end_selector),
        scroll_top: root.scrollTop, scroll_height: root.scrollHeight,
        client_height: root.clientHeight,
        bottom: root.scrollTop + root.clientHeight >= root.scrollHeight - 2};
}"""

# 每次滚动一个容器视口，支持页面或唯一 CSS 容器；使用即时滚动便于后续有界等待。
SCROLL_SCRIPT = """(selector) => {
    const roots = selector ? document.querySelectorAll(selector) : [document.scrollingElement];
    if (roots.length !== 1) throw new Error('Scroll container must match exactly one element');
    const root = roots[0];
    root.scrollBy({top: Math.max(root.clientHeight, 1), behavior: 'instant'});
}"""


# 将协议转换为固定脚本的数据参数；与提取器一致拒绝 Playwright 选择器引擎链。
# 本模块只负责状态与比较，动作预算、轮询时限及停止原因由调用方管理。
def state_options(spec):
    pagination = spec.pagination
    selectors = [spec.item_selector, *(f.selector for f in spec.fields.values())]
    options = {
        "item_selector": spec.item_selector,
        "fields": (
            {name: f.model_dump() for name, f in spec.fields.items()}
            if getattr(pagination, "mode", None) == "replace"
            else None
        ),
    }
    for key in ("container_selector", "loading_selector", "end_selector"):
        value = getattr(pagination, key, None)
        options[key] = value
        if value:
            selectors.append(value)
    if any(">>" in selector for selector in selectors):
        raise ValueError("Use CSS selectors without selector chains")
    return options


# 区分列表推进和去重后的新增：重复记录仍可能构成有效的新批次，去重交给执行器。
# 替换模式要求存在记录且字段快照变化；字段可为空，页码变化不参与完成判定。
def progressed(before, after, mode):
    return (
        after["count"] > before["count"]
        if mode == "append"
        else after["count"] > 0 and after["ready"] and after["snapshot"] != before["snapshot"]
    )


# 网络滚动只读取同一份几何/加载/结束状态，不查询或比较 DOM 记录。
def network_scroll_options(pagination):
    options = {"item_selector": "", "fields": None}
    for key in ("container_selector", "loading_selector", "end_selector"):
        value = getattr(pagination, key, None)
        if value and ">>" in value:
            raise ValueError("Use CSS selectors without selector chains")
        options[key] = value
    return options
