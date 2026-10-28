"""Application-owned scripts shared by deterministic execution and bounded exploration."""

# Inputs are data, never executable source. Compare declared list fields, not the whole DOM.
STATE_SCRIPT = """(o) => {
    const visible = selector => selector && [...document.querySelectorAll(selector)]
        .some(e => e.getClientRects().length && getComputedStyle(e).visibility !== 'hidden');
    const items = [...document.querySelectorAll(o.item_selector)];
    const root = o.container_selector ? document.querySelector(o.container_selector)
        : document.scrollingElement;
    if (!root) throw new Error('Scroll container not found');
    const snapshot = o.fields ? items.map(item => Object.values(o.fields).map(f => {
        const el = f.selector === ':scope' ? item : item.querySelector(f.selector);
        const value = el ? (f.extract === 'attribute' ? el.getAttribute(f.attribute) : (el.innerText ?? el.textContent)) : null;
        return value === null ? null : value.replace(/\\s+/g, ' ').trim();
    })) : [];
    const ready = !o.fields || snapshot.every(values => Object.values(o.fields)
        .every((f, i) => !f.required || !!values[i]));
    return {ready, count: items.length, snapshot: JSON.stringify(snapshot),
        loading: !!visible(o.loading_selector), end: !!visible(o.end_selector),
        bottom: root.scrollTop + root.clientHeight >= root.scrollHeight - 2};
}"""

SCROLL_SCRIPT = """(selector) => {
    const roots = selector ? document.querySelectorAll(selector) : [document.scrollingElement];
    if (roots.length !== 1) throw new Error('Scroll container must match exactly one element');
    const root = roots[0];
    root.scrollBy({top: Math.max(root.clientHeight, 1), behavior: 'instant'});
}"""


# Build data-only options for the fixed scripts; polling deadlines live in the callers.
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


def progressed(before, after, mode):
    return (
        after["count"] > before["count"]
        if mode == "append"
        else after["count"] > 0 and after["ready"] and after["snapshot"] != before["snapshot"]
    )
