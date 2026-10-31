# 表单记忆只在浏览器中读写，不占用任务状态或参与定时轮询。
"""Client-side persistence for the development UI form."""

import json


# 统一从控件默认值生成恢复、保存和重置脚本，新增表单字段无需另维护列表。
def form_scripts(defaults):
    setup = (
        f"const defaults = {json.dumps(defaults, ensure_ascii=False)};"
        'const key = "autoscraper.ui.form";'
    )
    load = """() => {
        SETUP
        let saved = {};
        try { saved = JSON.parse(localStorage.getItem(key)) || {}; } catch {}
        return Object.keys(defaults).map(name => Object.hasOwn(saved, name)
            ? saved[name] : defaults[name]);
    }""".replace("SETUP", setup)
    save = """(...values) => {
        SETUP
        const data = Object.fromEntries(Object.keys(defaults).map((name, i) => [name, values[i]]));
        try { localStorage.setItem(key, JSON.stringify(data)); } catch {}
        return [];
    }""".replace("SETUP", setup)
    reset = """() => {
        SETUP
        try { localStorage.removeItem(key); } catch {}
        return [...Object.values(defaults), null];
    }""".replace("SETUP", setup)
    return load, save, reset
