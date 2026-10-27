# Agent 命令行入口：读取模型配置，调用完整工作流，再保存规则和数据。
# 终端问答走 stderr，正常结果以 JSON 写入 stdout；文件只在验证成功后覆盖。
# 退出码：0 成功；1 未完成；3 协议不支持；4 等待用户澄清。
"""Generate a reusable rule; optionally hand it to the existing executor."""

import argparse
import json
import os
import sys
from pathlib import Path

from autoscraper.agent.explorer import AgentError, AgentSettings, save_spec
from autoscraper.agent.clarification import ClarificationRequest
from autoscraper.scraping.export import export_records
from autoscraper.workflow.validation import ValidationSettings
from autoscraper.workflow.runner import run_task
from autoscraper.observability.tracing import FileSink, Trace, emit, operation


# 从环境读取密钥和模型默认配置，再应用非 None 的命令行覆盖值。
# 供应商默认值留在 CLI 层，AgentSettings 本身不绑定 DeepSeek。
def settings_from_env(**overrides) -> AgentSettings:
    """Apply CLI provider defaults without coupling the agent runtime to them."""
    key = os.getenv("AUTOSCRAPER_API_KEY") or os.getenv("DEEPSEEK_API_KEY")
    if not key:
        raise AgentError("Set AUTOSCRAPER_API_KEY or DEEPSEEK_API_KEY")
    data = {
        "api_key": key,
        "model": os.getenv("AUTOSCRAPER_MODEL", "deepseek-flash"),
        "base_url": os.getenv("AUTOSCRAPER_BASE_URL", "https://api.deepseek.com"),
        "executable_path": os.getenv("AUTOSCRAPER_BROWSER_PATH"),
    }
    data.update({key: value for key, value in overrides.items() if value is not None})
    return AgentSettings.model_validate(data)


# 将执行超时等场景下的候选规则另存为 .unvalidated[-编号].json。
# 排除保留路径，并用独占创建防止覆盖已有规则或数据。
def save_unvalidated_spec(spec, output: Path, reserved: set[Path]) -> Path:
    """Keep a reusable candidate without replacing verified rules or requested data paths."""
    output.parent.mkdir(parents=True, exist_ok=True)
    payload = spec.model_dump_json(indent=2) + "\n"
    for index in range(1, 1001):
        suffix = ".unvalidated" if index == 1 else f".unvalidated-{index}"
        candidate = output.with_name(output.stem + suffix + ".json")
        if candidate.resolve() in reserved:
            continue
        try:
            with candidate.open("x", encoding="utf-8") as stream:
                stream.write(payload)
            return candidate
        except FileExistsError:
            continue
    raise FileExistsError("No available unvalidated rule filename")


# 终端澄清处理器：支持编号或自由回答，空输入/中断/非交互环境返回 None。
# 问题写入 stderr，避免污染最终 stdout JSON；不会默认选择某个选项。
def ask_user(request: ClarificationRequest) -> str | None:
    """No default selection; prompts stay separate from the final stdout JSON."""
    if not sys.stdin.isatty():
        return None
    print(f"\n需要澄清任务：{request.question}\n{request.explanation}", file=sys.stderr)
    for index, option in enumerate(request.options, 1):
        print(f"  {index}. {option}", file=sys.stderr)
    print("请输入回答或选项编号（留空结束并返回待澄清状态）：", file=sys.stderr, flush=True)
    try:
        answer = sys.stdin.readline()
    except (EOFError, KeyboardInterrupt):
        return None
    answer = answer.strip()
    if not answer:
        return None
    if answer.isascii() and answer.isdigit() and request.options:
        index = int(answer)
        if 1 <= index <= len(request.options):
            return request.options[index - 1]
        # 超范围编号不构成有效用户决定，也不自动回退到某个选项。
        return None
    return answer


# 解析 Agent CLI 参数，建立文件追踪，再交给 _execute 执行业务流程。
def main() -> int:
    parser = argparse.ArgumentParser(description="Explore a website and generate a ScraperSpec")
    parser.add_argument("url")
    parser.add_argument("task")
    parser.add_argument("--spec-output", required=True, type=Path)
    parser.add_argument("--output", type=Path, action="append", default=[])
    parser.add_argument("--model")
    parser.add_argument("--base-url")
    parser.add_argument("--max-steps", type=int)
    parser.add_argument("--timeout", type=int, dest="timeout_seconds")
    parser.add_argument("--llm-timeout", type=int, dest="llm_timeout_seconds")
    parser.add_argument("--max-output-tokens", type=int)
    parser.add_argument("--content-timeout-ms", type=int, default=30000)
    parser.add_argument("--max-wait-llm-calls", type=int, default=8)
    parser.add_argument("--browser-path", dest="executable_path")
    parser.add_argument("--headed", action="store_true")
    parser.add_argument(
        "--non-interactive",
        action="store_true",
        help="Return needs_clarification instead of prompting for task ambiguities",
    )
    parser.add_argument("--fields", nargs="+", help="Authoritative required field names")
    parser.add_argument("--count", type=int, help="Authoritative requested record count")
    parser.add_argument("--max-repairs", type=int, default=2)
    parser.add_argument("--validation-items", type=int, default=100)
    parser.add_argument("--validation-pages", type=int, default=5)
    parser.add_argument("--validation-detail-pages", type=int, default=100)
    parser.add_argument("--validation-timeout-ms", type=int)
    parser.add_argument(
        "--structured-output",
        action="store_true",
        help="Legacy compatibility flag; Responses output always uses json_schema",
    )
    parser.add_argument("--runs-dir", type=Path, default=Path(".agent-runtime/runs"),
                        help="Local execution traces (default: .agent-runtime/runs)")
    args = parser.parse_args()
    # 在读取详细配置前先注册可能用到的密钥，避免初始化失败时日志泄漏凭据。
    trace = Trace(FileSink(args.runs_dir), secrets=(
        os.getenv("AUTOSCRAPER_API_KEY"), os.getenv("DEEPSEEK_API_KEY"),
    ))
    with trace.run(url=args.url, task=args.task, model=args.model):
        code = _execute(args, trace)
        trace.exit_code = code
        return code


# 检查输出路径，构造运行配置并调用工作流，根据状态保存候选或验证后的产物。
# 只有 success/validated 才写正式输出；其他状态映射为约定退出码。
def _execute(args, trace) -> int:
    try:
        paths = [args.spec_output.resolve(), *(p.resolve() for p in args.output)]
        if len(paths) != len(set(paths)):
            raise ValueError("Rule and data output paths must be distinct")
        if args.spec_output.suffix.lower() != ".json":
            raise ValueError("Rule output must be .json")
        if any(p.suffix.lower() not in {".json", ".csv"} for p in args.output):
            raise ValueError("Data output must be .json or .csv")
        settings = settings_from_env(
            **{
                name: getattr(args, name)
                for name in (
                    "model",
                    "base_url",
                    "max_steps",
                    "timeout_seconds",
                    "llm_timeout_seconds",
                    "max_output_tokens",
                    "content_timeout_ms",
                    "max_wait_llm_calls",
                    "executable_path",
                )
            },
            headless=not args.headed,
            structured_output=args.structured_output,
        )
        validation = ValidationSettings(
            max_repairs=args.max_repairs,
            max_items=args.validation_items,
            max_pages=args.validation_pages,
            max_detail_pages=args.validation_detail_pages,
            timeout_ms=args.validation_timeout_ms,
        )
        outcome = run_task(
            args.url,
            args.task,
            settings,
            validation=validation,
            fields=args.fields,
            count=args.count,
            execute=bool(args.output),
            ask_user=None if args.non_interactive else ask_user,
        )
        summary = outcome.summary()
        trace.outcome = summary
        emit("cli.result", **summary)
        if outcome.status not in {"success", "validated"}:
            # 导航/内容等待失败时保留未验证规则用于排查，正式规则和数据仍由成功分支写入。
            if outcome.spec is not None and outcome.failure_stage in {"validation", "execution"}:
                try:
                    candidate_path = save_unvalidated_spec(
                        outcome.spec, args.spec_output, set(paths)
                    )
                    summary["unvalidated_spec"] = str(candidate_path)
                except OSError as exc:
                    summary["unvalidated_spec_error"] = str(exc)
            print(json.dumps(summary, ensure_ascii=False))
            if outcome.status == "needs_clarification":
                return 4
            return 3 if outcome.status == "unsupported" else 1
        # 需求约束、执行和数据检查通过后，才写正式规则与数据文件。
        with operation("artifacts"):
            save_spec(outcome.spec, args.spec_output)
            if args.output:
                for output in args.output:
                    export_records(outcome.data.records, output, list(outcome.spec.all_fields))
        summary["spec"] = str(args.spec_output)
        print(json.dumps(summary, ensure_ascii=False))
        return 0
    except Exception as exc:
        emit("cli.failed", exception=type(exc).__name__, message=str(exc))
        print(
            json.dumps(
                {
                    "status": "unfulfilled",
                    "task_fulfilled": False,
                    "feedback": [{"code": "workflow_failure", "message": str(exc)}],
                },
                ensure_ascii=False,
            ),
            file=sys.stderr,
        )
        return 1


if __name__ == "__main__":
    sys.exit(main())
