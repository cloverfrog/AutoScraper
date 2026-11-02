# 单次运行子进程：复用生产 CLI/Trace，示例服务、浏览器与输出均局限在本次运行。
import contextlib
import io
import json
import os
import sys
import traceback
from pathlib import Path

from .common import read_json, sha256, write_json
from .oracle import task_site


# 从 CLI stdout 取最后一个 JSON 对象；框架普通日志不能成为业务状态。
def last_json(text):
    for line in reversed(text.splitlines()):
        try:
            value = json.loads(line)
            if isinstance(value, dict):
                return value
        except ValueError:
            pass
    return {}


# 同时保留 CLI 最终 JSON 与实时管道输出；超时杀进程时已输出的诊断仍由父进程保存。
class LiveCapture(io.StringIO):
    def __init__(self, stream):
        super().__init__()
        self.stream = stream

    def write(self, text):
        self.stream.write(text)
        self.stream.flush()
        return super().write(text)


def sanitizer():
    from autoscraper.observability.tracing import Sanitizer

    return Sanitizer((os.getenv("AUTOSCRAPER_API_KEY"), os.getenv("DEEPSEEK_API_KEY")))


def execute(directory):
    directory = Path(directory).resolve()
    request, task = read_json(directory / "request.json"), read_json(directory / "task.json")
    # 保存实际启动浏览器的版本；不安装或下载浏览器。
    from playwright.sync_api import sync_playwright

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(executable_path=request["browser_path"])
        write_json(
            directory / "browser.json",
            {
                "version": browser.version,
                "executable_path": request["browser_path"] or playwright.chromium.executable_path,
            },
        )
        browser.close()
    with task_site(task) as url:
        if request["mode"] == "native":
            from .native import run

            return run(directory, task, request, url)
        if request["mode"] == "agent":
            from autoscraper.cli.agent import main as cli

            arguments = [
                "autoscraper-agent",
                url,
                task["task"],
                "--non-interactive",
                "--spec-output",
                str(directory / "spec.json"),
                "--output",
                str(directory / "records.json"),
                "--runs-dir",
                str(directory / "trace"),
                "--model",
                request["model"],
                "--base-url",
                request["base_url"],
                "--fields",
                *task["fields"],
                "--count",
                str(task["count"]),
            ]
            for key, value in request["budget"].items():
                arguments.extend(["--" + key.replace("_", "-"), str(value)])
            if request["browser_path"]:
                arguments.extend(["--browser-path", request["browser_path"]])
        else:
            from autoscraper.cli.scraper import main as cli
            from autoscraper.observability.tracing import FileSink, Trace

            if sha256(directory / "spec.json") != request["spec_sha256"]:
                raise ValueError("Input Spec changed")
            # 只绑定本地示例的临时端口；原输入副本与哈希继续保留。
            spec_path = directory / "spec.json"
            if task["id"] == "local-network":
                spec = read_json(spec_path)
                spec["start_url"] = url
                spec["network"]["url_pattern"] = url + "api/items"
                spec_path = directory / "bound-spec.json"
                write_json(spec_path, spec)
            arguments = ["autoscraper", str(spec_path), "--output", str(directory / "records.json")]
            if request["browser_path"]:
                arguments.extend(["--browser-path", request["browser_path"]])
        sys.argv = arguments
        captured = LiveCapture(sys.stdout)
        errors = LiveCapture(sys.stderr)
        with contextlib.redirect_stdout(captured), contextlib.redirect_stderr(errors):
            if request["mode"] == "executor":
                trace = Trace(FileSink(directory / "trace"))
                with trace.run(url=url, task=task["task"]):
                    code = cli()
                    trace.exit_code = code
            else:
                code = cli()
        raw = captured.getvalue()
        outcome = last_json(raw) or last_json(errors.getvalue())
        complete = code == 0 and (
            outcome.get("status") == "success" and not outcome.get("sample_only")
            if request["mode"] == "agent"
            else not outcome.get("errors")
            and not outcome.get("resource_errors")
            and outcome.get("stop_reason")
            in {"max_items", "no_next", "end_marker", "no_more_content", "no_pagination"}
        )
        write_json(
            directory / "execution.json",
            sanitizer().clean({"complete": complete, "outcome": outcome}),
        )
        return code


# 启动浏览器等阶段也可能失败；该层只补齐 Benchmark 元数据，不修复或重试 Agent。
def main(directory):
    directory = Path(directory).resolve()
    try:
        return execute(directory)
    except Exception as exc:
        error = sanitizer().text(f"{type(exc).__name__}: {exc}")
        write_json(
            directory / "execution.json",
            {"complete": False, "error": error, "outcome": {"status": "worker_error"}},
        )
        print(sanitizer().text(traceback.format_exc()), file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1]))
