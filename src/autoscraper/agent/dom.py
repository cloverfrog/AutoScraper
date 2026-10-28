# 探索阶段共享的固定 DOM 脚本：提供页面摘要和 CSS 采样检查。
# 工具注册与内容等待共同依赖本模块，脚本不接受模型生成的可执行源码。
"""Shared, application-authored DOM inspection scripts."""

# 下方 JavaScript 由应用固定编写；模型输入只作为 JSON 参数传入，不拼接进脚本源码。
# Agent 不拥有任意 evaluate、代码执行或文件操作工具。
# 页面检查脚本：按 CSS 根节点采样标签、属性和文本，限制节点数及每段文本长度。
INSPECT_SCRIPT = """(selector) => {
    const root = selector ? document.querySelector(selector) : document.body;
    if (!root) return {error: 'No root matched'};
    const attrs = el => Object.fromEntries([...el.attributes]
        .filter(a => ['id','class','href','src','title','aria-label','aria-disabled'].includes(a.name))
        .map(a => [a.name, a.value.slice(0, 250)]));
    const nodes = [root, ...root.querySelectorAll('*')]
        .filter(el => !['SCRIPT','STYLE','NOSCRIPT','SVG','PATH'].includes(el.tagName));
    return {url: location.href, title: document.title, total_nodes: nodes.length,
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
        const matches = fieldSelector ? [...item.querySelectorAll(fieldSelector)] : [item];
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
