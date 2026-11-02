# 独立评分与计量契约：错误输出必须扣分，程序自报成功不能替代冻结答案。
import copy
import json

import pytest

from benchmarks.common import HOME, read_json, sha256, tasks, validate_evidence, write_json
from benchmarks.scoring import (
    comparisons,
    normalize,
    score_batch,
    score_records,
    score_run,
    trace_metrics,
    validate_truth,
)


@pytest.fixture
def data():
    task = tasks()["local-network"]
    expected = read_json(HOME / "truth/local-network.json")["records"]
    return task, expected, copy.deepcopy(expected)


def test_exact_output_and_extra_non_target_fields(data):
    task, expected, actual = data
    actual[0]["extra"] = "ignored"
    result = score_records(task, expected, actual)
    assert result["task_success"]
    assert result["record_accuracy"] == 1
    assert set(result["field_accuracy"].values()) == {1}


@pytest.mark.parametrize(
    "change",
    [
        "missing",
        "extra",
        "duplicate",
        "order",
        "wrong_title",
        "wrong_id",
        "wrong_type",
        "null",
        "missing_field",
        "malformed_record",
        "missing_data",
    ],
)
def test_incorrect_output_is_not_success(data, change):
    task, expected, actual = data
    if change == "missing":
        actual.pop()
    elif change == "extra":
        actual.append({**actual[0], "id": 3})
    elif change == "duplicate":
        actual.append(actual[0])
    elif change == "order":
        actual.reverse()
    elif change == "wrong_title":
        actual[0]["title"] = "wrong"
    elif change == "wrong_id":
        actual[0]["id"] = 77
    elif change == "wrong_type":
        actual[1]["price"] = False
    elif change == "null":
        actual[0]["active"] = None
    elif change == "missing_field":
        del actual[0]["active"]
    elif change == "malformed_record":
        actual[0] = 5
    else:
        actual = None
    result = score_records(task, expected, actual)
    assert not result["task_success"]
    if change == "order":
        assert result["record_accuracy"] == 1
        assert not result["order_correct"]
    if change in {"extra", "duplicate"}:
        assert result["record_accuracy"] == pytest.approx(2 / 3)
    if change in {"null", "missing_field"}:
        assert result["field_accuracy"]["active"] == 0.5


def test_failures_do_not_become_success_even_with_correct_data(data):
    task, expected, actual = data
    result = score_records(task, expected, actual, execution_ok=False)
    assert result["data_correct"] and not result["task_success"]


def test_duplicate_identity_cannot_select_the_best_record(data):
    task, expected, actual = data
    actual.insert(0, {**actual[0], "title": "wrong"})
    result = score_records(task, expected, actual)
    assert result["correct_records"] == 1
    assert result["field_accuracy"]["title"] == pytest.approx(1 / 3)


def test_full_detail_text_is_required():
    task = tasks()["books-detail"]
    expected = read_json(HOME / "truth/books-detail.json")["records"]
    actual = copy.deepcopy(expected)
    actual[0]["description"] = actual[0]["description"][:100]
    assert not score_records(task, expected, actual)["task_success"]


def test_normalization_is_narrow():
    assert normalize("  cafe\u0301 \n 中文 ", "text") == "café 中文"
    assert normalize("HTTPS://EXAMPLE.COM:443/a?q=1#f", "url") == "https://example.com/a?q=1#f"
    assert normalize(0, "number") == normalize(0.0, "number")
    assert normalize(-1.25, "number") < 0
    assert normalize(False, "boolean") is False
    assert normalize("£51.77", "text") != normalize("51.77", "text")
    assert normalize("https://example.com/a?q=1", "url") != normalize(
        "https://example.com/a?q=2", "url"
    )
    assert normalize(["A", "B"], "text_list") != normalize(["B", "A"], "text_list")


@pytest.mark.parametrize(
    "value,kind",
    [
        (False, "number"),
        (0, "boolean"),
        ("0", "number"),
        (float("nan"), "number"),
        (float("inf"), "number"),
        (None, "text"),
        ("tags", "text_list"),
        ([1], "text_list"),
        ("/relative", "url"),
        ("https://u:p@example.com/", "url"),
    ],
)
def test_invalid_types_and_urls(value, kind):
    with pytest.raises(ValueError):
        normalize(value, kind)


def test_frozen_sources_and_truth_are_consistent():
    for task in tasks().values():
        truth = validate_evidence(task)
        validate_truth(task, truth["records"])
        assert truth["review"]["status"] == "source-reviewed"


def test_invalid_ground_truth_fails_loudly(data):
    task, expected, _ = data
    with pytest.raises(ValueError):
        score_records(task, expected[:1], expected)


def test_usage_missing_is_not_zero(tmp_path):
    write_json(
        tmp_path / "trace/x/summary.json",
        {
            "termination": "returned",
            "trace_errors": [],
            "metrics": {
                "llm_calls_by_source": {"requirements": 1, "main": 2, "wait": 1},
                "tokens": None,
                "usage_calls": 0,
                "usage_missing_calls": 4,
                "repairs": 1,
            },
        },
    )
    result = trace_metrics(tmp_path, "agent")
    assert result["tokens"] is None
    assert result["usage_missing_calls"] == 4
    assert sum(result["llm_calls_by_source"].values()) == 4
    assert result["repairs"] == 1


def test_partial_events_are_explicit_lower_bounds(tmp_path):
    path = tmp_path / "trace/x/events.jsonl"
    path.parent.mkdir(parents=True)
    path.write_text(
        "\n".join(
            [
                json.dumps({"event_type": "llm.started", "source": "main"}),
                json.dumps(
                    {
                        "event_type": "llm.finished",
                        "source": "main",
                        "payload": {"usage": {"prompt_tokens": 5, "completion_tokens": 2}},
                    }
                ),
                json.dumps({"event_type": "repair.started"}),
                json.dumps({"event_type": "llm.finished", "payload": []}),
                '{"truncated',
            ]
        )
    )
    result = trace_metrics(tmp_path, "agent")
    assert result["tokens"]["prompt_tokens"] == 5
    assert result["repairs"] == 1
    assert result["metric_basis"] == "events_lower_bound"
    assert result["corrupt_events"] and not result["trace_complete"]


def test_missing_agent_trace_is_unknown_but_executor_is_zero(tmp_path):
    assert trace_metrics(tmp_path, "agent")["llm_calls_by_source"] is None
    assert trace_metrics(tmp_path, "executor")["tokens"]["total_tokens"] == 0


# 构造独立导入目录，复用将来 browser-use 原始记录的评分契约。
def make_run(directory, *, complete=True, exit_code=0):
    task = tasks()["local-network"]
    write_json(directory / "task.json", task)
    (directory / "truth.json").write_bytes((HOME / "truth/local-network.json").read_bytes())
    write_json(directory / "records.json", read_json(directory / "truth.json")["records"])
    write_json(
        directory / "request.json",
        {
            "mode": "executor",
            "task_sha256": sha256(directory / "task.json"),
            "truth_sha256": sha256(directory / "truth.json"),
        },
    )
    write_json(
        directory / "process.json", {"exit_code": exit_code, "timed_out": False, "wall_seconds": 1}
    )
    write_json(
        directory / "execution.json", {"complete": complete, "outcome": {"status": "success"}}
    )


def test_score_files_fail_closed_and_detect_changed_truth(tmp_path):
    make_run(tmp_path, complete=False)
    assert not score_run(tmp_path)["task_success"]
    write_json(tmp_path / "execution.json", {"complete": True})
    assert score_run(tmp_path)["task_success"]
    (tmp_path / "records.json").write_text("broken")
    assert not score_run(tmp_path)["task_success"]
    write_json(tmp_path / "truth.json", {})
    with pytest.raises(ValueError, match="changed"):
        score_run(tmp_path)


def test_batch_keeps_failures_and_pending_separate(tmp_path):
    make_run(tmp_path / "good")
    make_run(tmp_path / "bad", exit_code=1)
    cells = [
        {
            "task": "local-network",
            "group": "fixture",
            "protocol": "test",
            "repeat": i,
            "directory": name,
            "state": state,
        }
        for i, (name, state) in enumerate(
            [("good", "finished"), ("bad", "running"), ("pending", "pending")]
        )
    ]
    write_json(tmp_path / "manifest.json", {"cells": cells})
    result = score_batch(tmp_path)
    assert result["planned"] == 3 and result["launched"] == 2
    assert not result["complete"]
    assert result["groups"][0]["success_rate"] == 0.5
    assert (tmp_path / "summary.csv").is_file() and (tmp_path / "summary.md").is_file()


def test_comparison_uses_matching_runs_and_excludes_unknown_tokens():
    rows = []
    for group, success, calls in [("total-no-repair", False, 10), ("total-repair", True, 14)]:
        rows.append(
            {
                "task": "a",
                "repeat": 1,
                "protocol": "equal-total",
                "group": group,
                "reuse_index": 0,
                "task_success": success,
                "llm_calls": calls,
                "wall_seconds": calls,
                "repairs": int(success),
                "trace_complete": True,
                "usage_missing_calls": 1,
                "tokens": {"total_tokens": 20},
            }
        )
    result = comparisons(rows)[0]
    assert result["success_uplift_percentage_points"] == 100
    assert result["mean_added_cost"]["llm_calls"]["value"] == 4
    assert result["mean_added_cost"]["total_tokens"]["value"] is None
