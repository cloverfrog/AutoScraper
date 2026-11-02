# 批量编排只负责配置、进程隔离和文件产物；生产流程与请求计量继续复用现有入口。
import copy
import importlib.metadata
import os
import re
import signal
import subprocess
import sys
import time
import uuid
from pathlib import Path

from .common import ROOT, now, read_json, sha256, tasks, truth_path, validate_evidence, write_json
from .scoring import score_batch, score_records

BUDGET = {
    "max_steps": 12,
    "max_llm_calls": 24,
    "repair_reserve_calls": 0,
    "max_repairs": 0,
    "max_wait_llm_calls": 8,
    "max_output_tokens": 3000,
    "timeout": 240,
    "llm_timeout": 45,
    "content_timeout_ms": 30000,
    "validation_items": 100,
    "validation_pages": 5,
    "validation_detail_pages": 100,
}
LIMITS = {
    "max_steps": (1, 50),
    "max_llm_calls": (1, 200),
    "repair_reserve_calls": (0, 199),
    "max_repairs": (0, 3),
    "max_wait_llm_calls": (0, 30),
    "max_output_tokens": (256, 8192),
    "timeout": (1, 900),
    "llm_timeout": (1, 120),
    "content_timeout_ms": (1, 120000),
    "validation_items": (1, 1000),
    "validation_pages": (1, 20),
    "validation_detail_pages": (1, 1000),
}


# 拒绝拼错的配置键，预算只映射到已存在 CLI 参数，不接受任意命令模板。
def validate_config(value):
    config = copy.deepcopy(value)
    allowed = {
        "tasks",
        "groups",
        "repeats",
        "commit",
        "model",
        "base_url",
        "budget",
        "process_timeout",
        "browser_path",
    }
    if not isinstance(config, dict) or set(config) - allowed:
        raise ValueError("Unknown batch configuration keys")
    chosen = config.get("tasks")
    if (
        not isinstance(chosen, list)
        or not chosen
        or len(set(chosen)) != len(chosen)
        or set(chosen) - tasks().keys()
    ):
        raise ValueError("Select unique known tasks")
    for name, default in (("repeats", 1), ("process_timeout", 600)):
        config.setdefault(name, default)
        if type(config[name]) is not int or not 1 <= config[name] <= (
            100 if name == "repeats" else 3600
        ):
            raise ValueError(f"Invalid {name}")
    groups = config.get("groups")
    if not isinstance(groups, list) or not groups:
        raise ValueError("At least one group is required")
    group_ids = set()
    for group in groups:
        if not isinstance(group, dict) or set(group) - {
            "id",
            "mode",
            "protocol",
            "budget",
            "specs",
            "reuse_runs",
        }:
            raise ValueError("Unknown group configuration keys")
        name = group.get("id", "")
        if (
            not isinstance(name, str)
            or not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,63}", name)
            or name in group_ids
        ):
            raise ValueError("Group IDs must be unique safe names")
        group_ids.add(name)
        if group.get("mode") not in {"executor", "agent", "native"}:
            raise ValueError("Only executor, agent and native modes are implemented")
        group.setdefault("protocol", "verification")
        if not isinstance(group["protocol"], str) or not re.fullmatch(
            r"[a-z0-9-]{1,64}", group["protocol"]
        ):
            raise ValueError("Invalid protocol name")
        group.setdefault("reuse_runs", 0)
        if type(group["reuse_runs"]) is not int or not 0 <= group["reuse_runs"] <= 99:
            raise ValueError("reuse_runs must be 0..99")
        common_budget, group_budget = config.get("budget", {}), group.get("budget", {})
        if (
            not isinstance(common_budget, dict)
            or not isinstance(group_budget, dict)
            or (set(common_budget) | set(group_budget)) - BUDGET.keys()
        ):
            raise ValueError("Unknown budget configuration keys")
        budget = {**BUDGET, **common_budget, **group_budget}
        for key, (low, high) in LIMITS.items():
            if type(budget[key]) is not int or not low <= budget[key] <= high:
                raise ValueError(f"Invalid budget: {key}")
        if budget["repair_reserve_calls"] >= budget["max_llm_calls"]:
            raise ValueError("Reserve must be smaller than request limit")
        # 保存有效预留，不让 max_repairs=0 的静默取消掩盖配置错误。
        if budget["max_repairs"] == 0 and budget["repair_reserve_calls"]:
            raise ValueError("No-repair groups must explicitly set reserve to zero")
        group["budget"] = budget
        if group["mode"] == "native" and (
            budget["max_repairs"]
            or budget["repair_reserve_calls"]
            or budget["max_wait_llm_calls"]
            or group["reuse_runs"]
        ):
            raise ValueError(
                "Native direct-data runs have one shared budget, no Repair or Spec reuse"
            )
        if group["mode"] == "executor":
            specs = group.get("specs", {})
            if not isinstance(specs, dict) or set(specs) != set(chosen):
                raise ValueError("Executor requires an explicit Spec for every selected task")
            for source in specs.values():
                if not isinstance(source, str) or not (ROOT / source).is_file():
                    raise ValueError("Spec input must be an existing file")
        elif "specs" in group:
            raise ValueError("Agent groups generate from scratch; specs are executor inputs only")
    if any(group["mode"] in {"agent", "native"} for group in groups):
        if not isinstance(config.get("model"), str) or not config["model"].strip():
            raise ValueError("Agent runs require an explicit model")
        from urllib.parse import urlsplit

        parsed = urlsplit(config.get("base_url", ""))
        if (
            parsed.scheme not in {"http", "https"}
            or not parsed.hostname
            or parsed.username
            or parsed.password
            or parsed.query
            or parsed.fragment
        ):
            raise ValueError("Provide a base_url without credentials or query parameters")
    validate_protocols(groups)
    return config


# 已命名的对照协议必须符合预算口径，不能仅改标签就输出不公平的对照结果。
def validate_protocols(groups):
    for protocol, names in (
        ("equal-total", ("total-no-repair", "total-repair")),
        ("equal-initial", ("initial-no-repair", "initial-repair")),
    ):
        selected = {group["id"]: group for group in groups if group["protocol"] == protocol}
        if not selected:
            continue
        if set(selected) != set(names) or any(
            group["mode"] != "agent" for group in selected.values()
        ):
            raise ValueError(f"{protocol} requires both named Agent groups")
        off, on = (selected[name]["budget"] for name in names)
        if off["max_repairs"] != 0 or on["max_repairs"] == 0 or off["repair_reserve_calls"]:
            raise ValueError("Invalid repair control groups")
        if any(
            off[key] != on[key]
            for key in BUDGET
            if key not in {"max_llm_calls", "max_repairs", "repair_reserve_calls"}
        ):
            raise ValueError("Control groups must share non-repair budgets")
        if protocol == "equal-total":
            valid = off["max_llm_calls"] == on["max_llm_calls"] and on["repair_reserve_calls"] == 0
        else:
            valid = off["max_llm_calls"] == on["max_llm_calls"] - on["repair_reserve_calls"]
        if not valid:
            raise ValueError(f"Budgets do not satisfy {protocol}")


def git(*args):
    return subprocess.check_output(["git", *args], cwd=ROOT, text=True).strip()


# 正式运行只验证当前检出，不替用户切分支或重置工作区；开发验证明确标记 dirty。
def verify_commit(commit, allow_dirty=False):
    if not isinstance(commit, str) or not re.fullmatch(r"[0-9a-f]{40}", commit):
        raise ValueError("A full 40-character Git commit is required")
    if git("rev-parse", "HEAD") != commit:
        raise ValueError("Requested commit does not match HEAD")
    status = git("status", "--porcelain", "--untracked-files=all")
    if status and not allow_dirty:
        raise ValueError("Formal runs require a clean checkout; --allow-dirty is development only")
    return {"commit": commit, "dirty": bool(status), "formal": not bool(status)}


# 子进程超时杀掉整个进程组，避免 Chromium 后代继续运行；不自动重试。
def launch(arguments, timeout):
    started = time.monotonic()
    process = subprocess.Popen(
        [sys.executable, *arguments],
        cwd=ROOT,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
        start_new_session=os.name == "posix",
        env={**os.environ, "ANONYMIZED_TELEMETRY": "false", "BROWSER_USE_CLOUD_SYNC": "false"},
    )
    timed_out = False
    interrupted = False
    try:
        stdout, stderr = process.communicate(timeout=timeout)
    except (subprocess.TimeoutExpired, KeyboardInterrupt) as exc:
        timed_out = isinstance(exc, subprocess.TimeoutExpired)
        interrupted = isinstance(exc, KeyboardInterrupt)
        kill_process(process)
        stdout, stderr = process.communicate()
    except BaseException:
        kill_process(process)
        process.communicate()
        raise
    # 和既有追踪保持相同凭据清理口径；stdout/stderr 不成为第二套事件系统。
    from autoscraper.observability.tracing import Sanitizer

    sanitizer = Sanitizer((os.getenv("AUTOSCRAPER_API_KEY"), os.getenv("DEEPSEEK_API_KEY")))
    return {
        "exit_code": process.returncode,
        "timed_out": timed_out,
        "interrupted": interrupted,
        "wall_seconds": time.monotonic() - started,
        "stdout": sanitizer.text(stdout),
        "stderr": sanitizer.text(stderr),
    }


# 父进程取消时也必须关闭独立会话中的浏览器/模型进程，避免后台继续产生费用。
def kill_process(process):
    if os.name == "posix":
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
    else:
        process.kill()


# 每次预检重新采集源证据并与冻结答案严格比较；漂移不会写入正式答案。
def check_task(task, output, *, timeout=600, browser_path=None):
    from .evidence import audit_task

    result = {"task": task["id"], "checked_at": now(), "process": None, "passed": False}
    try:
        if not audit_task(task)["source_consistent"]:
            raise ValueError("Frozen truth differs from original evidence")
        arguments = ["-m", "benchmarks.oracle", "--task", task["id"], "--capture", str(output)]
        if browser_path:
            arguments += ["--browser-path", browser_path]
        process = launch(arguments, timeout)
        result["process"] = process
        candidate = read_json(output / "candidate.json")
        truth = validate_evidence(task)
        result["score"] = score_records(task, truth["records"], candidate["records"])
        result["passed"] = process["exit_code"] == 0 and result["score"]["task_success"]
    except (OSError, ValueError, KeyError, TypeError) as exc:
        result["error"] = str(exc)
    output.mkdir(parents=True, exist_ok=True)
    write_json(output / "check.json", result)
    if result["process"] and result["process"].get("interrupted"):
        raise KeyboardInterrupt
    return result


# 预先展开完整计划，重复轮次交替组顺序；每个单元都有固定关联和唯一目录。
def expand_cells(config):
    cells = []
    for repeat in range(1, config["repeats"] + 1):
        for task_index, task_id in enumerate(config["tasks"]):
            groups = (
                config["groups"] if (repeat + task_index) % 2 else list(reversed(config["groups"]))
            )
            for group in groups:
                parent = None
                for reuse in range(group["reuse_runs"] + 1):
                    directory = (
                        f"runs/{task_id}_{group['id']}_r{repeat}_u{reuse}_{uuid.uuid4().hex[:10]}"
                    )
                    cells.append(
                        {
                            "task": task_id,
                            "group": group["id"],
                            "protocol": group["protocol"],
                            "repeat": repeat,
                            "reuse_index": reuse,
                            "parent": parent,
                            "directory": directory,
                            "state": "pending",
                        }
                    )
                    if reuse == 0:
                        parent = directory
    return cells


def environment():
    versions = {}
    for package in ("autoscraper", "browser-use", "playwright", "pydantic"):
        try:
            versions[package] = importlib.metadata.version(package)
        except importlib.metadata.PackageNotFoundError:
            versions[package] = None
    return {
        "python": sys.version,
        "platform": sys.platform,
        "packages": versions,
        "uv_lock_sha256": sha256(ROOT / "uv.lock"),
        # 开发验证尚未提交时也留下工具源码指纹；formal 仍要求干净 Commit。
        "benchmark_sha256": {
            str(path.relative_to(ROOT)): sha256(path)
            for path in sorted((ROOT / "benchmarks").glob("*.py"))
        },
    }


# 每次任务保存独立输入副本、规则来源与哈希，历史评分不依赖之后的仓库文件。
def prepare(directory, task, group, config, source_spec=None):
    directory.mkdir(parents=True, exist_ok=False)
    write_json(directory / "task.json", task)
    (directory / "truth.json").write_bytes(truth_path(task).read_bytes())
    request = {
        "mode": "executor" if source_spec else group["mode"],
        "task_sha256": sha256(directory / "task.json"),
        "truth_sha256": sha256(directory / "truth.json"),
        "model": config.get("model"),
        "base_url": config.get("base_url"),
        "browser_path": config.get("browser_path") or os.getenv("AUTOSCRAPER_BROWSER_PATH"),
        "budget": group["budget"],
    }
    if source_spec is not None:
        (directory / "spec.json").write_bytes(Path(source_spec).read_bytes())
        request.update(spec_source=str(source_spec), spec_sha256=sha256(directory / "spec.json"))
    write_json(directory / "request.json", request)


def run_batch(config, output_root, *, commit, allow_llm=False, allow_dirty=False):
    config = validate_config(config)
    if not allow_llm and any(group["mode"] in {"agent", "native"} for group in config["groups"]):
        raise ValueError("Agent/native groups require --allow-llm; no model requests were started")
    provenance = verify_commit(commit or config.get("commit"), allow_dirty)
    if config.get("commit") and commit and config["commit"] != commit:
        raise ValueError("CLI and configuration commits differ")
    directory = Path(output_root).resolve() / (
        now().replace(":", "-") + "_" + uuid.uuid4().hex[:10]
    )
    directory.mkdir(parents=True, exist_ok=False)
    manifest = {
        "schema_version": 1,
        "created_at": now(),
        "provenance": provenance,
        "environment": environment(),
        "config": config,
        "cells": expand_cells(config),
    }
    write_json(directory / "manifest.json", manifest)
    known = tasks()
    # 所有组共享同一次任务源核验；不根据某组结果决定是否保留其他组。
    checks = {}
    for task_id in config["tasks"]:
        print(f"check {task_id}", flush=True)
        checks[task_id] = check_task(
            known[task_id],
            directory / "checks" / task_id,
            timeout=config["process_timeout"],
            browser_path=config.get("browser_path"),
        )["passed"]
    groups = {group["id"]: group for group in config["groups"]}
    for cell in manifest["cells"]:
        group, task = groups[cell["group"]], known[cell["task"]]
        run_dir = directory / cell["directory"]
        if not checks[cell["task"]]:
            cell.update(state="blocked", reason="source_check_failed")
        else:
            source = None
            if cell["reuse_index"]:
                parent = directory / cell["parent"]
                if not reusable(parent):
                    cell.update(state="blocked", reason="generation_or_source_run_failed")
                else:
                    source = parent / "spec.json"
            elif group["mode"] == "executor":
                source = ROOT / group["specs"][task["id"]]
            if cell["state"] != "blocked":
                prepare(run_dir, task, group, config, source)
                cell["state"] = "running"
                write_json(directory / "manifest.json", manifest)
                print(f"run {cell['directory']}", flush=True)
                try:
                    process = launch(
                        ["-m", "benchmarks.worker", str(run_dir)], config["process_timeout"]
                    )
                except OSError as exc:
                    process = {
                        "exit_code": None,
                        "timed_out": False,
                        "wall_seconds": None,
                        "error": str(exc),
                    }
                write_json(run_dir / "process.json", process)
                from .scoring import score_run

                score_run(run_dir)
                cell["state"] = "finished"
                if process.get("interrupted"):
                    write_json(directory / "manifest.json", manifest)
                    score_batch(directory)
                    raise KeyboardInterrupt
        write_json(directory / "manifest.json", manifest)
    score_batch(directory)
    return directory


# 调度只依据工作流是否发布了可执行规则，不读取 Ground Truth 的评分结论。
def reusable(directory):
    try:
        process = read_json(directory / "process.json")
        execution = read_json(directory / "execution.json")
        return (
            type(process.get("exit_code")) is int
            and process["exit_code"] == 0
            and process.get("timed_out") is False
            and not process.get("interrupted")
            and execution.get("complete") is True
            and (directory / "spec.json").is_file()
        )
    except (OSError, ValueError, TypeError, AttributeError):
        return False
