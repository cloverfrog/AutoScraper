# 探索阶段共享的固定 DOM 脚本：提供页面摘要和 CSS 采样检查。
# 工具注册与内容等待共同依赖本模块，脚本不接受模型生成的可执行源码。
"""Shared, application-authored DOM inspection scripts."""

# 下方 JavaScript 由应用固定编写；模型输入只作为 JSON 参数传入，不拼接进脚本源码。
# Agent 不拥有任意 evaluate、代码执行或文件操作工具。
# 页面检查脚本：按 CSS 根节点采样标签、属性和文本，限制节点数及每段文本长度。
INSPECT_SCRIPT = """(selector) => {
    const roots = selector ? [...document.querySelectorAll(selector)] : [document.body];
    const attrs = el => Object.fromEntries([...el.attributes]
        .filter(a => ['id','class','href','src','title','aria-label','aria-disabled'].includes(a.name))
        .map(a => [a.name, a.value.slice(0, 250)]));
    // 匹配根节点优先返回，再补充子节点；按钮类选择器不会被第一个按钮的子树遮蔽。
    const sampledRoots = roots.length ? roots.slice(0, 50) : [document.body];
    // 根选择器猜错时附上全页采样；优先显示按钮和链接，让模型能直接依据实际类名修正。
    const controls = sampledRoots.flatMap(root => [...root.querySelectorAll('button,a')]).slice(0, 50);
    const nodes = [...new Set([...sampledRoots,
        ...controls,
        ...sampledRoots.flatMap(root => [...root.querySelectorAll('*')])])]
        .filter(el => !['SCRIPT','STYLE','NOSCRIPT','SVG','PATH'].includes(el.tagName));
    return {url: location.href, title: document.title, root_selector: selector,
        root_count: roots.length, sampled_roots: roots.length ? sampledRoots.length : 0,
        fallback_root: roots.length ? null : 'body', error: roots.length ? null : 'No root matched',
        truncated: roots.length > sampledRoots.length || nodes.length > 180,
        scope: 'sampled matching roots and descendants; omitted nodes may exist', total_nodes: nodes.length,
        nodes: nodes.slice(0, 180).map(el => ({tag: el.tagName.toLowerCase(),
            attrs: attrs(el), children: el.children.length,
            text: (el.innerText || '').replace(/\\s+/g, ' ').trim().slice(0, 120)}))};
}"""

# 选择器测试脚本：先检查 CSS 语法，再在最多五条记录内检查字段匹配数量和值。
# 即使没有列表记录也检查字段 CSS；sample_passed 只说明样本内匹配唯一，仍需独立验证。
SELECTOR_SCRIPT = """(itemSelector, fieldSelector, attribute) => {
    const result = {url: location.href, item_selector: itemSelector,
        field_selector: fieldSelector, attribute, count: null, samples: [],
        tested_count: 0, all_items_tested: false};
    const invalid = (parameter, error) => {
        if (error.name !== 'SyntaxError') throw error;
        return {...result, status: 'invalid_css', error_parameter: parameter,
            message: error.message.slice(0, 1000)};
    };
    let items;
    try { items = [...document.querySelectorAll(itemSelector)]; }
    catch (error) { return invalid('item_selector', error); }
    result.count = items.length;
    // Parse field CSS even when no records matched. Never use its document-level
    // match count: fields are evaluated relative to each sampled record below.
    if (fieldSelector) {
        try { document.createElement('div').querySelectorAll(fieldSelector); }
        catch (error) { return invalid('field_selector', error); }
    }
    result.samples = items.slice(0, 5).map(item => {
        const matches = fieldSelector && fieldSelector !== ':scope' ? [...item.querySelectorAll(fieldSelector)] : [item];
        const status = matches.length === 0 ? 'no_match'
            : matches.length === 1 ? 'single_match' : 'multiple_matches';
        return {status, matches: matches.length, values: matches.slice(0, 2).map(el =>
            attribute ? el.getAttribute(attribute)?.slice(0, 300) ?? null
                      : (el.innerText || '').trim().slice(0, 300))};
    });
    result.tested_count = result.samples.length;
    result.all_items_tested = result.tested_count === result.count;
    result.status = !items.length ? 'no_items'
        : result.samples.every(sample => sample.matches === 1)
            ? 'sample_passed' : 'sample_failed';
    return result;
}"""


# 批量测试复用单字段脚本，保留每个字段的原样参数、样本和页面 URL，避免两套匹配语义。
# 全部脚本由应用固定组合；模型只提供字段规则数据。
BATCH_SELECTOR_SCRIPT = """(itemSelector, fields) => {
    const test = """ + SELECTOR_SCRIPT + """;
    return {url: location.href, item_selector: itemSelector,
        results: Object.fromEntries(Object.entries(fields).map(([name, rule]) =>
            [name, test(itemSelector, rule.selector, rule.extract === 'attribute' ? rule.attribute : '')]))};
}"""
