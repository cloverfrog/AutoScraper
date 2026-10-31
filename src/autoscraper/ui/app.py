# Gradio 表单入口：短回调启动线程、轮询快照、提交人工决定，避免阻塞事件队列。
# 启动地址固定为本机；不接收任意输出或认证路径，不开放共享链接。
"""Optional local Web UI for the existing scraping interfaces."""

import argparse
from pathlib import Path

from autoscraper.ui.runtime import JobManager, RunConfig


# 延迟导入 Gradio，纯 CLI 安装仍可导入本入口并查看启动帮助。
def build_app(manager):
    import gradio as gr

    # 控件值统一转换为入口配置；空白模型配置沿用 CLI 环境默认值。
    def start(mode, *values):
        keys = list(controls)
        data = dict(zip(keys, values, strict=True))
        # 可选正整数的 0/空值表示不覆盖默认配置；不能影响允许为零的调用和修复预算。
        for key in ("count", "timeout_ms", "content_timeout_ms", "navigation_timeout_ms"):
            if data[key] in (None, "", 0):
                data[key] = None
        agent_options = {
            key: (None if data[key] == "" else data[key]) for key in (
                "model", "base_url", "max_steps", "max_llm_calls", "repair_reserve_calls",
                "timeout_seconds", "llm_timeout_seconds",
                "max_output_tokens", "max_wait_llm_calls",
            )
        }
        config = RunConfig(
            mode=mode, url=data["url"] or "", task=data["task"] or "",
            spec_json=data["spec_json"] or "",
            execute=data["execute"], headed=data["headed"],
            login=data["login"] if mode == "spec" else False,
            auth_profile=(data["auth_profile"] or "").strip(),
            browser_path=(data["browser_path"] or "").strip(),
            fields=data["fields"] or "", count=data["count"], agent_options=agent_options,
            validation_options={
                key: data[key] for key in (
                    "max_repairs", "max_items", "max_pages", "max_detail_pages", "timeout_ms",
                ) if data[key] is not None
            },
            content_timeout_ms=data["content_timeout_ms"],
            navigation_timeout_ms=data["navigation_timeout_ms"],
        )
        try:
            return manager.start(config), "任务已启动"
        except ValueError as exc:
            raise gr.Error(str(exc)) from exc

    # 文件组件接收上传字节，不把浏览器传入值当作本地文件路径读取。
    def load_rule(content):
        if content is None:
            return ""
        if len(content) > 1_000_000:
            raise gr.Error("规则文件不能超过 1 MB。")
        try:
            return content.decode("utf-8-sig")
        except UnicodeError:
            raise gr.Error("请上传 UTF-8 编码的 JSON 文件。") from None

    # 仅在请求 ID 改变时重置选项，定时刷新不会覆盖用户正在填写的回答。
    def poll(job_id, previous_request):
        job = manager.get(job_id)
        if job is None:
            return [gr.skip()] * 10
        state = job.snapshot()
        pending = state["pending"]
        request_id = pending["id"] if pending else ""
        changed = request_id != previous_request
        kind = pending["kind"] if pending else ""
        prompt = (
            pending.get("question", pending.get("reason", "")) + "\n\n"
            + pending.get("explanation", "")
        ) if pending else "当前没有待处理问题。"
        status = state["status"] + ("（任务已结束）" if state["done"] else "")
        return (
            status, state["summary"], state["logs"], state["preview"], state["files"],
            gr.Group(visible=pending is not None), prompt,
            gr.Radio(choices=pending.get("options", []) if pending else [], value=None,
                     visible=kind == "clarification") if changed else gr.skip(),
            gr.Textbox(value="", visible=kind == "clarification") if changed else gr.skip(),
            request_id,
        )

    # 浏览器只提交当前任务和请求的随机标识，过期点击不能回答后续问题。
    def respond(job_id, request_id, option, answer, cancel=False):
        job = manager.get(job_id)
        if job is None:
            raise gr.Error("任务已结束，请重新运行。")
        with job.condition:
            pending = job.pending
            if not pending or pending["id"] != request_id:
                raise gr.Error("问题已更新，请等待页面刷新。")
            value = (
                "continue" if pending["kind"] == "handoff" else ((answer or "").strip() or option)
            )
            try:
                job.respond(request_id, value, cancel=cancel)
            except ValueError as exc:
                raise gr.Error(str(exc)) from exc
        return "已提交，等待任务继续。"

    with gr.Blocks(title="AutoScraper · 本地抓取", analytics_enabled=False) as app:
        gr.Markdown("# AutoScraper\n生成可复用规则，或执行已有规则。浏览器在独立窗口运行。")
        job_id = gr.State("")
        request_id = gr.State("")
        controls = {}
        with gr.Tabs():
            with gr.Tab("Agent 生成规则"):
                controls["url"] = gr.Textbox(label="目标 URL", placeholder="https://example.com")
                controls["task"] = gr.Textbox(label="抓取任务", lines=3)
                controls["execute"] = gr.Checkbox(label="验证规则后执行完整抓取", value=False)
                gr.Markdown("关闭完整抓取时仍会试抓取验证；样本结果不代表完整任务已完成。")
                agent_start = gr.Button("生成规则", variant="primary")
            with gr.Tab("执行已有规则"):
                upload = gr.File(label="上传 ScraperSpec JSON", file_types=[".json"], type="binary")
                controls["spec_json"] = gr.Textbox(label="规则 JSON（可直接粘贴或编辑）", lines=10)
                controls["login"] = gr.Checkbox(label="抓取前人工登录（需启用可见浏览器）")
                spec_start = gr.Button("执行规则", variant="primary")
        with gr.Row():
            controls["headed"] = gr.Checkbox(label="显示浏览器，允许人工接管", value=False)
            controls["auth_profile"] = gr.Textbox(
                label="认证状态名称（可选）", placeholder="例如 demo-account",
                info="同名复用登录状态；凭据只保存在本机，不提供下载。",
            )
        with gr.Accordion("高级设置", open=False):
            with gr.Row():
                controls["model"] = gr.Textbox(label="模型（留空使用环境配置）")
                controls["base_url"] = gr.Textbox(label="模型 API 地址（留空使用环境配置）")
            controls["browser_path"] = gr.Textbox(label="浏览器程序路径（留空使用环境配置）")
            with gr.Row():
                controls["fields"] = gr.Textbox(label="必需字段（空格分隔，仅 Agent）")
                controls["count"] = gr.Number(
                    label="请求条数（可选，仅 Agent）", precision=0, minimum=0,
                    placeholder="不额外限定条数", info="0 或留空表示不覆盖。",
                )
            with gr.Row():
                for key, label, value in (
                    ("max_steps", "每轮 Agent 步数上限", 12),
                    ("max_llm_calls", "任务主模型请求硬上限", 24),
                    ("repair_reserve_calls", "修复预留请求数", 6),
                    ("timeout_seconds", "Agent 超时（秒）", 240),
                    ("llm_timeout_seconds", "模型调用超时（秒）", 45),
                    ("max_output_tokens", "最大输出 Token", 3000),
                    ("max_wait_llm_calls", "内容等待模型调用预算", 8),
                ):
                    controls[key] = gr.Number(label=label, value=value, precision=0)
            with gr.Row():
                for key, label, value in (
                    ("max_repairs", "最大修复次数", 2),
                    ("max_items", "验证条数上限", 100),
                    ("max_pages", "验证列表页上限", 5),
                    ("max_detail_pages", "验证详情页上限", 100),
                    ("timeout_ms", "验证超时（毫秒，可选）", None),
                ):
                    controls[key] = gr.Number(
                        label=label, value=value, precision=0,
                        placeholder="沿用默认" if key == "timeout_ms" else None,
                        info="0 或留空表示不覆盖。" if key == "timeout_ms" else None,
                        minimum=0 if key == "timeout_ms" else None,
                    )
            with gr.Row():
                controls["content_timeout_ms"] = gr.Number(
                    label="内容等待超时（毫秒，留空沿用默认）", precision=0,
                    minimum=0, placeholder="Agent 默认 30000；执行规则时沿用规则",
                    info="0 或留空表示不覆盖。",
                )
                controls["navigation_timeout_ms"] = gr.Number(
                    label="规则导航超时（毫秒，留空沿用规则）", precision=0,
                    minimum=0, placeholder="沿用规则", info="0 或留空表示不覆盖。",
                )
        status = gr.Textbox(label="任务状态", value="就绪", interactive=False)
        with gr.Group(visible=False) as interaction:
            prompt = gr.Textbox(label="需要你处理", lines=5, interactive=False)
            options = gr.Radio(label="建议选项", choices=[])
            answer = gr.Textbox(label="自由回答（填写后优先于选项）")
            gr.Markdown("人工接管时，请在独立浏览器完成操作，再点击继续。等待 15 分钟未处理将结束任务。")
            with gr.Row():
                submit = gr.Button("提交回答 / 继续", variant="primary")
                cancel = gr.Button("结束等待 / 取消接管")
        summary = gr.JSON(label="执行摘要与验证反馈")
        preview = gr.JSON(label="数据预览（最多 100 条）")
        files = gr.File(label="下载规则和 JSON / CSV", file_count="multiple", interactive=False)
        logs = gr.Textbox(label="实时运行日志", lines=18, max_lines=30, interactive=False)
        timer = gr.Timer(1)
        upload.change(load_rule, upload, controls["spec_json"], queue=False, api_visibility="private")
        agent_start.click(
            lambda *args: start("agent", *args), list(controls.values()), [job_id, status],
            queue=False, api_visibility="private",
        )
        spec_start.click(
            lambda *args: start("spec", *args), list(controls.values()), [job_id, status],
            queue=False, api_visibility="private",
        )
        timer.tick(
            poll, [job_id, request_id],
            [status, summary, logs, preview, files, interaction, prompt, options, answer, request_id],
            queue=False, show_progress="hidden", api_visibility="private",
        )
        submit.click(
            respond, [job_id, request_id, options, answer], status,
            queue=False, api_visibility="private",
        )
        cancel.click(
            lambda *args: respond(*args, cancel=True), [job_id, request_id, options, answer], status,
            queue=False, api_visibility="private",
        )
    return app


# 不提供远程监听开关；认证目录和 Trace 明确禁止被 Gradio 文件路由读取。
def main():
    parser = argparse.ArgumentParser(description="Start the local AutoScraper Web UI")
    parser.add_argument("--port", type=int, default=7860)
    parser.add_argument("--runtime-dir", type=Path, default=Path(".agent-runtime/ui"))
    args = parser.parse_args()
    try:
        manager = JobManager(args.runtime_dir)
        app = build_app(manager)
    except ImportError:
        parser.exit(1, '请先安装 Web UI 依赖：uv sync --extra ui（Agent 另加 --extra agent）\n')
    app.launch(
        server_name="127.0.0.1", server_port=args.port, share=False,
        allowed_paths=[str(manager.root / "artifacts")],
        blocked_paths=[str(manager.root / "auth"), str(manager.root / "traces")],
        max_file_size="1mb", inbrowser=True,
    )
    return 0


if __name__ == "__main__":
    main()
