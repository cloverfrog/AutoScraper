# 原生 browser-use 对照：只复用供应商适配器、请求预算和文件追踪，不复用产品探索流程。
import asyncio
import importlib.metadata
import os
from pathlib import Path

from pydantic import (
    ConfigDict,
    StrictBool,
    StrictFloat,
    StrictInt,
    StrictStr,
    ValidationError,
    create_model,
)

from .common import write_json


# Schema 只来自任务字段定义，不包含真值、选择器、记录数约束或网站专用规则。
def output_schema(task):
    types = {
        "text": StrictStr,
        "url": StrictStr,
        "number": StrictInt | StrictFloat,
        "boolean": StrictBool,
        "text_list": list[StrictStr],
    }
    record = create_model(
        "NativeRecord",
        __config__=ConfigDict(extra="forbid", strict=True),
        **{name: (types[kind], ...) for name, kind in task["fields"].items()},
    )
    return create_model(
        "NativeResult",
        __config__=ConfigDict(extra="forbid", strict=True),
        records=(list[record], ...),
    )


# 直接使用与生产相同的 Responses 类和参数；全部原生辅助调用共用此实例的硬预算。
def make_model(request):
    from autoscraper.agent.llm import LimitedChatOpenAI

    key = os.getenv("AUTOSCRAPER_API_KEY") or os.getenv("DEEPSEEK_API_KEY")
    if not key:
        raise ValueError("Set AUTOSCRAPER_API_KEY or DEEPSEEK_API_KEY")
    budget = request["budget"]
    return LimitedChatOpenAI(
        model=request["model"],
        api_key=key,
        base_url=request["base_url"],
        timeout=budget["llm_timeout"],
        max_retries=0,
        structured_output_retries=2,
        max_calls=budget["max_llm_calls"],
        max_output_tokens=budget["max_output_tokens"],
        temperature=0,
        reasoning_effort="none",
    )


# 原生 done 返回 JSON，宿主只解包 records；不删重、不补字段、不重排、不再次调用模型。
def save_output(directory, task, history):
    raw = history.final_result()
    (directory / "native-final.txt").write_text(raw or "", encoding="utf-8")
    result = output_schema(task).model_validate_json(raw or "")
    write_json(directory / "records.json", result.model_dump(mode="json")["records"])
    return history.is_done() is True and history.is_successful() is True


async def execute(directory, task, request, url):
    from browser_use import Agent, BrowserSession
    from autoscraper.observability.tracing import StepObserver, emit, labels, operation

    if importlib.metadata.version("browser-use") != "0.13.11":
        raise ValueError("Native baseline requires locked browser-use 0.13.11")
    llm = make_model(request)
    schema = output_schema(task)
    observer = StepObserver()
    browser = BrowserSession(
        headless=True, executable_path=request["browser_path"], enable_default_extensions=False
    )
    agent = None
    finished = False
    try:
        with labels(attempt=1), operation("attempt"), operation("native"):
            async with asyncio.timeout(request["budget"]["timeout"]):
                await browser.start()
                version = await browser.cdp_client.send.Browser.getVersion()
                write_json(directory / "native-browser.json", version)
                # 使用原生 Agent/默认工具及默认规划、评判、压缩和失败恢复能力。
                # 只关闭 DeepSeek 不支持的视觉输入和由外层接管的信号处理。
                agent = Agent(
                    task=f"URL: {url}\n{task['task']}",
                    llm=llm,
                    page_extraction_llm=llm,
                    judge_llm=llm,
                    browser_session=browser,
                    output_model_schema=schema,
                    use_vision=False,
                    llm_timeout=request["budget"]["llm_timeout"],
                    file_system_path=str(directory / "native-files"),
                    available_file_paths=[],
                    calculate_cost=False,
                    generate_gif=False,
                    enable_signal_handler=False,
                    register_new_step_callback=observer.planned,
                )
                write_json(
                    directory / "native-settings.json",
                    {
                        "agent": "browser_use.Agent",
                        "browser_use": "0.13.11",
                        "model": request["model"],
                        "base_url": request["base_url"],
                        "temperature": 0,
                        "reasoning_effort": "none",
                        "sdk_retries": 0,
                        "structured_output_retries": 2,
                        "max_output_tokens": llm.max_output_tokens,
                        "request_limit": llm.max_calls,
                        "budget_scope": "all-native-model-requests",
                        "max_steps": request["budget"]["max_steps"],
                        "timeout_seconds": request["budget"]["timeout"],
                        "tool_names": sorted(agent.tools.registry.registry.actions),
                        "schema": schema.model_json_schema(),
                        "native_defaults": {
                            name: getattr(agent.settings, name)
                            for name in (
                                "use_thinking",
                                "flash_mode",
                                "enable_planning",
                                "use_judge",
                                "max_actions_per_step",
                                "max_failures",
                                "max_history_items",
                                "max_clickable_elements_length",
                                "final_response_after_failure",
                            )
                        },
                        "message_compaction_enabled": bool(agent.settings.message_compaction),
                    },
                )
                history = await agent.run(
                    max_steps=request["budget"]["max_steps"],
                    on_step_start=observer.start,
                    on_step_end=observer.end,
                )
                write_json(
                    directory / "native-status.json",
                    {
                        "done": history.is_done(),
                        "successful": history.is_successful(),
                        "judgement": history.judgement(),
                        "history_errors": history.errors(),
                    },
                )
                complete = save_output(directory, task, history)
                finished = True
                outcome = {
                    "status": "success" if complete else "unfulfilled",
                    "attempts": 1,
                    "llm_calls": llm.calls,
                    "feedback": []
                    if complete
                    else [
                        {"code": "native_history_error", "message": message}
                        for message in history.errors()
                        if message
                    ],
                    "native_history_errors": [message for message in history.errors() if message],
                    "native_judgement": history.judgement(),
                }
                emit("native.result", complete=complete, **outcome)
                return {"complete": complete, "outcome": outcome}, 0 if complete else 1
    finally:
        if agent is not None:
            observer.finish(agent, interrupted=not finished)
            # 超时、异常也保留原始末次输出；只有合法 done JSON 才进入 records.json。
            if not (directory / "native-final.txt").exists():
                (directory / "native-final.txt").write_text(
                    agent.history.final_result() or "", encoding="utf-8"
                )
        try:
            await asyncio.wait_for(browser.kill(), timeout=20)
        except Exception as exc:
            emit("native.cleanup_error", exception=type(exc).__name__, message=str(exc))


def run(directory, task, request, url):
    from autoscraper.observability.tracing import FileSink, Trace, emit

    directory = Path(directory)
    trace = Trace(
        FileSink(directory / "trace"),
        secrets=(os.getenv("AUTOSCRAPER_API_KEY"), os.getenv("DEEPSEEK_API_KEY")),
    )
    with trace.run(url=url, task=task["task"], model=request["model"]):
        try:
            result, code = asyncio.run(execute(directory, task, request, url))
        except Exception as exc:
            # 业务失败仍完整收尾计量；异常原文与堆栈由既有 native/attempt span 保存。
            reason = (
                "native_timeout"
                if isinstance(exc, TimeoutError)
                else "native_output_invalid"
                if isinstance(exc, ValidationError)
                else "native_failure"
            )
            result = {
                "complete": False,
                "error": trace.sanitizer.text(str(exc)),
                "outcome": {
                    "status": "unfulfilled",
                    "feedback": [{"code": reason, "message": str(exc)}],
                },
            }
            code = 1
            emit("native.failed", exception=type(exc).__name__, message=str(exc))
        trace.exit_code = code
        trace.outcome = result["outcome"]
        write_json(directory / "execution.json", trace.sanitizer.clean(result))
        return code
