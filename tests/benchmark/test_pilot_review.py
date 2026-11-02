# 正式 Pilot 前的离线回归：不连接网站或模型，验证审查发现的问题与四次运行配置。
import copy

import pytest

from benchmarks import runner, worker
from benchmarks.common import HOME, read_json, sha256, tasks, write_json
from benchmarks.evidence import audit_all, audit_task
from benchmarks.metrics import TOKEN_FIELDS, trace_metrics
from benchmarks.scoring import comparisons, score_batch, score_run


def pilot():
    return read_json(HOME / "configs/pilot.json")


def test_pilot_has_four_counterbalanced_equal_budget_cells():
    config = runner.validate_config(pilot())
    cells = runner.expand_cells(config)
    assert [(cell["task"], cell["group"]) for cell in cells] == [
        ("books-list", "total-no-repair"),
        ("books-list", "total-repair"),
        ("cars-load-more", "total-repair"),
        ("cars-load-more", "total-no-repair"),
    ]
    assert config["repeats"] == 1
    for group in config["groups"]:
        assert group["budget"]["max_llm_calls"] == 24
        assert group["budget"]["repair_reserve_calls"] == 0
        assert group["reuse_runs"] == 0 and "specs" not in group


@pytest.mark.parametrize(
    "key,value",
    [
        ("max_llm_calls", 23),
        ("repair_reserve_calls", 3),
        ("max_steps", 13),
        ("max_wait_llm_calls", 9),
        ("validation_pages", 6),
        ("max_repairs", 0),
    ],
)
def test_protocol_labels_cannot_hide_unfair_budgets(key, value):
    config = pilot()
    config["groups"][1]["budget"][key] = value
    with pytest.raises(ValueError):
        runner.validate_config(config)


@pytest.mark.parametrize(
    "key,value",
    [("validation_items", 1001), ("validation_pages", 21), ("validation_detail_pages", 1001)],
)
def test_validation_limits_match_production(key, value):
    config = pilot()
    config["budget"][key] = value
    with pytest.raises(ValueError, match="Invalid budget"):
        runner.validate_config(config)


def test_pilot_requires_paid_opt_in_before_any_work(monkeypatch, tmp_path):
    monkeypatch.setattr(runner, "verify_commit", lambda *args: pytest.fail("no work before opt in"))
    with pytest.raises(ValueError, match="allow-llm"):
        runner.run_batch(pilot(), tmp_path, commit="a" * 40)


def test_all_107_records_and_380_fields_match_frozen_source():
    results = audit_all()
    assert len(results) == 6 and all(result["source_consistent"] for result in results)
    assert sum(result["score"]["expected_records"] for result in results) == 107
    assert sum(task["count"] * len(task["fields"]) for task in tasks().values()) == 380
    assert all(result["human_review_required"] for result in results)


def test_source_audit_detects_wrong_truth_without_changing_it(monkeypatch):
    task = tasks()["books-list"]
    truth_file = HOME / "truth/books-list.json"
    before = sha256(truth_file)
    changed = copy.deepcopy(read_json(truth_file))
    changed["records"][0]["price"] = "£0.01"
    monkeypatch.setattr("benchmarks.evidence.validate_evidence", lambda _: changed)
    assert not audit_task(task)["source_consistent"]
    assert sha256(truth_file) == before


# 使用真实 Trace 写入局部缺失的 Usage，避免测试替身的格式与核心日志契约不一致。
def partial_usage(directory):
    from autoscraper.observability.tracing import FileSink, Trace, emit, labels

    trace = Trace(FileSink(directory / "trace"))
    with trace.run():
        with labels(source="main"):
            for index, usage in enumerate(
                (
                    {"prompt_tokens": 10, "completion_tokens": None},
                    {"prompt_tokens": 20, "completion_tokens": 5},
                )
            ):
                with labels(call_id=f"call-{index}"):
                    emit("llm.started")
                    emit("llm.finished", usage=usage)
        trace.llm_sources["main"] = 2
        trace.usage_calls = 2
        trace.tokens.update(prompt_tokens=30, completion_tokens=5)


def test_partial_token_field_is_not_full_usage(tmp_path):
    partial_usage(tmp_path)
    metrics = trace_metrics(tmp_path, "agent")
    assert metrics["trace_complete"] and metrics["usage_missing_calls"] == 0
    assert metrics["token_usage_complete"]["prompt_tokens"]
    assert not metrics["token_usage_complete"]["completion_tokens"]
    assert metrics["token_usage_calls"]["completion_tokens"] == 1


def test_corrupt_summary_tokens_fall_back_to_events(tmp_path):
    partial_usage(tmp_path)
    path = next((tmp_path / "trace").glob("*/summary.json"))
    summary = read_json(path)
    summary["metrics"]["tokens"]["prompt_tokens"] = -1
    write_json(path, summary)
    metrics = trace_metrics(tmp_path, "agent")
    assert metrics["tokens"]["prompt_tokens"] == 30
    assert metrics["metric_basis"] == "events_lower_bound"
    assert not metrics["trace_complete"]


def test_duplicate_call_id_does_not_inflate_event_lower_bound(tmp_path):
    import json

    partial_usage(tmp_path)
    next((tmp_path / "trace").glob("*/summary.json")).unlink()
    path = next((tmp_path / "trace").glob("*/events.jsonl"))
    with path.open("a") as stream:
        stream.write(
            json.dumps(
                {
                    "seq": 999,
                    "event_type": "llm.started",
                    "source": "main",
                    "call_id": "call-0",
                    "payload": {},
                }
            )
            + "\n"
        )
    result = trace_metrics(tmp_path, "agent")
    assert result["llm_calls_by_source"]["main"] == 2
    assert result["corrupt_events"] and not result["token_usage_complete"]["prompt_tokens"]


def paired_rows():
    return [
        {
            "task": "books-list",
            "repeat": 1,
            "protocol": "equal-total",
            "group": group,
            "reuse_index": 0,
            "task_success": success,
            "llm_calls": calls,
            "wall_seconds": calls,
            "repairs": int(success),
            "trace_complete": True,
            "usage_missing_calls": 0,
            "token_usage_complete": {key: key == "prompt_tokens" for key in TOKEN_FIELDS},
            "tokens": {"prompt_tokens": calls, "completion_tokens": calls},
        }
        for group, success, calls in [("total-no-repair", False, 10), ("total-repair", True, 14)]
    ]


def test_token_delta_only_uses_complete_fields():
    result = comparisons(paired_rows())[0]
    assert result["mean_added_cost"]["prompt_tokens"]["value"] == 4
    assert result["mean_added_cost"]["completion_tokens"]["value"] is None


def test_unbalanced_or_incomplete_batch_has_no_uplift():
    assert comparisons(paired_rows(), complete=False)[0]["success_uplift_percentage_points"] is None
    rows = paired_rows()
    rows.append({**rows[0], "repeat": 2})
    assert comparisons(rows)[0]["success_uplift_percentage_points"] is None


def make_run(directory):
    task = tasks()["local-network"]
    write_json(directory / "task.json", task)
    (directory / "truth.json").write_bytes((HOME / "truth/local-network.json").read_bytes())
    write_json(
        directory / "request.json",
        {
            "mode": "executor",
            "task_sha256": sha256(directory / "task.json"),
            "truth_sha256": sha256(directory / "truth.json"),
        },
    )
    write_json(directory / "records.json", read_json(directory / "truth.json")["records"])
    write_json(directory / "process.json", {"exit_code": 0, "timed_out": False, "wall_seconds": 1})
    write_json(directory / "execution.json", {"complete": True, "outcome": {"status": "success"}})


@pytest.mark.parametrize(
    "process",
    [
        {"exit_code": False, "timed_out": False},
        {"exit_code": 0},
        {"exit_code": -9, "timed_out": True},
    ],
)
def test_invalid_exit_metadata_is_never_success(tmp_path, process):
    make_run(tmp_path)
    write_json(tmp_path / "process.json", process)
    result = score_run(tmp_path)
    assert not result["task_success"] and result["failure_reasons"]


def test_nonstandard_json_nan_cannot_hide_in_extra_fields(tmp_path):
    import json

    make_run(tmp_path)
    records = read_json(tmp_path / "records.json")
    records[0]["extra"] = float("nan")
    (tmp_path / "records.json").write_text(json.dumps(records))
    result = score_run(tmp_path)
    assert result["missing_data"] and not result["task_success"]


def test_failure_metadata_and_accuracy_survive_offline_export(tmp_path):
    make_run(tmp_path / "run")
    write_json(
        tmp_path / "run/process.json", {"exit_code": -9, "timed_out": True, "wall_seconds": 5}
    )
    write_json(
        tmp_path / "run/execution.json",
        {
            "complete": False,
            "outcome": {
                "status": "unfulfilled",
                "repair_not_started_reason": "insufficient_llm_requests",
                "feedback": [{"code": "budget_exhausted", "message": "exhausted"}],
            },
        },
    )
    write_json(
        tmp_path / "manifest.json",
        {
            "cells": [
                {
                    "directory": "run",
                    "state": "finished",
                    "task": "local-network",
                    "group": "fixture",
                    "protocol": "fixture",
                    "repeat": 1,
                }
            ]
        },
    )
    row = score_batch(tmp_path)["runs"][0]
    assert row["exit_code"] == -9 and row["timed_out"]
    assert row["record_accuracy"] == 1 and not row["task_success"]
    assert {reason["code"] for reason in row["failure_reasons"]} >= {
        "process_timeout",
        "budget_exhausted",
    }
    assert row["repair_not_started_reason"] == "insufficient_llm_requests"
    assert "process_timeout" in (tmp_path / "summary.csv").read_text()


def test_live_capture_preserves_output_before_timeout():
    code = "import contextlib,sys,time; from benchmarks.worker import LiveCapture; cap=LiveCapture(sys.stdout)\nwith contextlib.redirect_stdout(cap):\n print('partial CLI output'); time.sleep(10)"
    result = runner.launch(["-c", code], 0.3)
    assert result["timed_out"] and "partial CLI output" in result["stdout"]


def test_worker_startup_failure_is_recorded_and_sanitized(monkeypatch, tmp_path):
    monkeypatch.setenv("AUTOSCRAPER_API_KEY", "fake-private-key")

    def broken(_):
        raise RuntimeError("startup fake-private-key")

    monkeypatch.setattr(worker, "execute", broken)
    assert worker.main(tmp_path) == 1
    result = read_json(tmp_path / "execution.json")
    assert not result["complete"] and result["outcome"]["status"] == "worker_error"
    assert "fake-private-key" not in result["error"]


def test_worker_keeps_structured_cli_stderr_without_leaking_key(monkeypatch, tmp_path):
    import sys
    from contextlib import contextmanager, nullcontext
    from types import SimpleNamespace
    from autoscraper.cli import agent as cli

    config = runner.validate_config(pilot())
    directory = tmp_path / "run"
    runner.prepare(directory, tasks()["books-list"], config["groups"][0], config)
    browser = SimpleNamespace(version="scripted", close=lambda: None)
    playwright = SimpleNamespace(
        chromium=SimpleNamespace(launch=lambda **kwargs: browser, executable_path="scripted")
    )

    @contextmanager
    def fake_playwright():
        yield playwright

    monkeypatch.setattr("playwright.sync_api.sync_playwright", fake_playwright)
    monkeypatch.setattr(worker, "task_site", lambda task: nullcontext(task["url"]))
    monkeypatch.setattr(sys, "argv", sys.argv.copy())
    monkeypatch.setenv("AUTOSCRAPER_API_KEY", "fake-cli-private-key")

    def execute(args, trace):
        print(
            '{"status":"unfulfilled","feedback":[{"code":"workflow_failure","message":"fake-cli-private-key"}]}',
            file=sys.stderr,
        )
        return 1

    monkeypatch.setattr(cli, "_execute", execute)
    assert worker.main(directory) == 1
    result = read_json(directory / "execution.json")
    assert result["outcome"]["feedback"][0]["code"] == "workflow_failure"
    assert "fake-cli-private-key" not in (directory / "execution.json").read_text()


def test_replay_does_not_consult_ground_truth_score(tmp_path, monkeypatch):
    make_run(tmp_path)
    write_json(tmp_path / "spec.json", {})
    write_json(tmp_path / "score.json", {"task_success": False})
    original = runner.read_json

    def read(path):
        assert path.name != "score.json"
        return original(path)

    monkeypatch.setattr(runner, "read_json", read)
    assert runner.reusable(tmp_path)


def test_bad_source_evidence_blocks_before_network_and_persists_reason(monkeypatch, tmp_path):
    def bad(_):
        raise ValueError("evidence hash mismatch")

    monkeypatch.setattr("benchmarks.evidence.audit_task", bad)
    monkeypatch.setattr(runner, "launch", lambda *args: pytest.fail("must not contact source"))
    result = runner.check_task(tasks()["books-list"], tmp_path / "check")
    assert not result["passed"] and result["process"] is None
    assert read_json(tmp_path / "check/check.json")["error"] == "evidence hash mismatch"


def test_keyboard_interrupt_kills_child_and_returns_auditable_status(monkeypatch):
    from types import SimpleNamespace

    calls = []

    def communicate(timeout=None):
        if timeout is not None:
            raise KeyboardInterrupt
        return "partial stdout", "partial stderr"

    process = SimpleNamespace(pid=123, returncode=-9, communicate=communicate)
    monkeypatch.setattr(runner.subprocess, "Popen", lambda *args, **kwargs: process)
    monkeypatch.setattr(runner, "kill_process", lambda child: calls.append(child.pid))
    result = runner.launch(["unused"], 10)
    assert calls == [123] and result["interrupted"] and result["exit_code"] == -9
    assert "partial stdout" in result["stdout"]


# 走完整四单元 Runner、既有 Trace 和独立评分，仅替换网络预检与模型子进程。
def test_four_run_pilot_exports_all_metrics_without_a_model(monkeypatch, tmp_path):
    import asyncio
    from pathlib import Path
    from types import SimpleNamespace
    from autoscraper.observability.tracing import FileSink, Trace, capture_usage, labels, traced_llm
    from benchmarks.evidence import records_from_evidence
    from benchmarks.common import validate_evidence

    monkeypatch.setattr(
        runner, "verify_commit", lambda *args: {"commit": "a" * 40, "formal": False}
    )
    monkeypatch.setattr(runner, "environment", lambda: {})
    monkeypatch.setattr(runner, "check_task", lambda *args, **kwargs: {"passed": True})

    class ScriptedLLM:
        model = "scripted-not-real"

        @traced_llm
        async def ainvoke(self, messages):
            capture_usage(
                SimpleNamespace(
                    prompt_tokens=20, completion_tokens=5, total_tokens=25, prompt_cached_tokens=0
                )
            )

    def fake_process(arguments, timeout):
        directory = Path(arguments[-1])
        task = read_json(directory / "task.json")
        request = read_json(directory / "request.json")
        assert request["budget"]["repair_reserve_calls"] == 0
        trace = Trace(FileSink(directory / "trace"))
        with trace.run():
            with labels(source="main"):
                asyncio.run(ScriptedLLM().ainvoke([]))
        write_json(directory / "records.json", records_from_evidence(task, validate_evidence(task)))
        write_json(
            directory / "execution.json", {"complete": True, "outcome": {"status": "success"}}
        )
        return {"exit_code": 0, "timed_out": False, "wall_seconds": 1.5}

    monkeypatch.setattr(runner, "launch", fake_process)
    batch = runner.run_batch(pilot(), tmp_path, commit="a" * 40, allow_llm=True)
    summary = read_json(batch / "summary.json")
    assert summary["complete"] and len(summary["runs"]) == 4
    for row in summary["runs"]:
        assert row["task_success"] and row["record_accuracy"] == 1
        assert set(row["field_accuracy"].values()) == {1}
        assert row["exit_code"] == 0 and row["llm_calls"] == 1
        assert row["tokens"]["total_tokens"] == 25
        assert row["wall_seconds"] == 1.5 and row["repairs"] == 0
        assert row["failure_reasons"] == []
    assert summary["comparisons"][0]["mean_added_cost"]["total_tokens"]["value"] == 0


def test_interrupted_batch_stops_before_remaining_cells(monkeypatch, tmp_path):
    monkeypatch.setattr(runner, "verify_commit", lambda *args: {"commit": "a" * 40})
    monkeypatch.setattr(runner, "environment", lambda: {})
    monkeypatch.setattr(runner, "check_task", lambda *args, **kwargs: {"passed": True})
    launches = []

    def interrupted(arguments, timeout):
        launches.append(arguments)
        return {"exit_code": -9, "timed_out": False, "interrupted": True, "wall_seconds": 0.1}

    monkeypatch.setattr(runner, "launch", interrupted)
    with pytest.raises(KeyboardInterrupt):
        runner.run_batch(pilot(), tmp_path, commit="a" * 40, allow_llm=True)
    assert len(launches) == 1
    result = read_json(next(tmp_path.glob("*/summary.json")))
    assert not result["complete"] and result["states"] == {"finished": 1, "pending": 3}
    assert result["runs"][0]["failure_reasons"][0]["code"] == "process_interrupted"
