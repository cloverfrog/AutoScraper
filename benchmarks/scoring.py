# 独立评分：只使用标准库与冻结数据，不导入 AutoScraper、浏览器或模型。
import csv
import json
import unicodedata
from collections import Counter, defaultdict
from decimal import Decimal
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

from .common import read_json, sha256, write_json
from .metrics import trace_metrics


# 只消除无意义的表示差异；不修正截断文本、错误货币或相对链接。
def normalize(value, kind):
    if kind in {"text", "url"}:
        if not isinstance(value, str):
            raise ValueError("expected string")
        value = " ".join(unicodedata.normalize("NFC", value).split())
        if kind == "text":
            return value
        parts = urlsplit(value)
        if parts.scheme.lower() not in {"http", "https"} or not parts.hostname:
            raise ValueError("expected absolute HTTP(S) URL")
        if (
            parts.username is not None
            or parts.password is not None
            or any(c.isspace() for c in value)
        ):
            raise ValueError("invalid URL")
        host = parts.hostname.lower()
        host = f"[{host}]" if ":" in host else host
        port = parts.port
        if port and (parts.scheme.lower(), port) not in {("http", 80), ("https", 443)}:
            host += f":{port}"
        return urlunsplit((parts.scheme.lower(), host, parts.path, parts.query, parts.fragment))
    if kind == "text_list":
        if not isinstance(value, list):
            raise ValueError("expected list")
        return tuple(normalize(item, "text") for item in value)
    if kind == "boolean":
        if type(value) is not bool:
            raise ValueError("expected boolean")
        return value
    if kind == "number":
        if type(value) not in {int, float} or not Decimal(str(value)).is_finite():
            raise ValueError("expected finite JSON number")
        return Decimal(str(value))
    raise ValueError(f"Unknown field type: {kind}")


# 真值内部错误直接终止评分，避免将损坏标准答案解释为被测系统失败。
def validate_truth(task, records):
    if not isinstance(records, list) or len(records) != task["count"] or not records:
        raise ValueError("Ground truth count mismatch")
    keys = []
    for row in records:
        if not isinstance(row, dict) or set(row) != set(task["fields"]):
            raise ValueError("Ground truth fields mismatch")
        for field, kind in task["fields"].items():
            normalize(row[field], kind)
        keys.append(normalize(row[task["key"]], task["fields"][task["key"]]))
    if len(keys) != len(set(keys)):
        raise ValueError("Ground truth identities must be unique")


# 同一身份最多匹配一次；重复项不参与挑优，确保增加重复记录不能提高得分。
def score_records(task, expected, actual, *, execution_ok=True):
    validate_truth(task, expected)
    missing_data = not isinstance(actual, list)
    actual = actual if isinstance(actual, list) else []
    key, fields = task["key"], task["fields"]
    truth = {normalize(row[key], fields[key]): row for row in expected}
    keys, differences = [], []
    correct_fields = Counter({field: 0 for field in fields})
    correct_records = 0
    seen = set()
    for index, row in enumerate(actual):
        try:
            identity = normalize(row[key], fields[key])
        except (ValueError, TypeError, KeyError):
            keys.append(None)
            differences.append({"index": index, "reason": "invalid_identity"})
            continue
        keys.append(identity)
        if identity in seen or identity not in truth:
            differences.append({"index": index, "reason": "duplicate_or_extra"})
            continue
        seen.add(identity)
        bad = []
        for field, kind in fields.items():
            try:
                matches = normalize(row[field], kind) == normalize(truth[identity][field], kind)
            except (ValueError, TypeError, KeyError):
                matches = False
            if matches:
                correct_fields[field] += 1
            else:
                bad.append(field)
        correct_records += not bad
        if bad:
            differences.append({"index": index, "reason": "field_mismatch", "fields": bad})
    expected_keys = [normalize(row[key], fields[key]) for row in expected]
    denominator = max(len(expected), len(actual))
    data_correct = not missing_data and keys == expected_keys and correct_records == len(expected)
    return {
        "task_success": bool(execution_ok and data_correct),
        "data_correct": data_correct,
        "execution_ok": bool(execution_ok),
        "missing_data": missing_data,
        "expected_records": len(expected),
        "output_records": len(actual),
        "correct_records": correct_records,
        "record_denominator": denominator,
        "record_accuracy": correct_records / denominator,
        "record_precision": correct_records / len(actual) if actual else 0.0,
        "record_recall": correct_records / len(expected),
        "field_accuracy": {field: correct_fields[field] / denominator for field in fields},
        "order_correct": keys == expected_keys,
        "missing_records": len(set(expected_keys) - seen),
        "differences": differences,
    }


# 对批次自身保存的任务/真值副本评分，后续仓库更新不会悄悄改变历史成绩。
def score_run(directory):
    directory = Path(directory)
    request = read_json(directory / "request.json")
    task = read_json(directory / "task.json")
    truth_file = directory / "truth.json"
    if (
        sha256(truth_file) != request["truth_sha256"]
        or sha256(directory / "task.json") != request["task_sha256"]
    ):
        raise ValueError("Frozen evaluation inputs changed")
    expected = read_json(truth_file)["records"]
    try:
        actual = read_json(directory / "records.json")
    except (OSError, ValueError):
        actual = None
    try:
        execution = read_json(directory / "execution.json")
        if not isinstance(execution, dict):
            execution = {}
    except (OSError, ValueError):
        execution = {}
    if not isinstance(execution.get("outcome"), dict):
        execution["outcome"] = {}
    try:
        process = read_json(directory / "process.json")
        if not isinstance(process, dict):
            process = {}
    except (OSError, ValueError):
        process = {}
    execution_ok = (
        type(process.get("exit_code")) is int
        and process["exit_code"] == 0
        and process.get("timed_out") is False
        and not process.get("interrupted")
        and execution.get("complete") is True
    )
    result = score_records(task, expected, actual, execution_ok=execution_ok)
    result["metrics"] = {
        **trace_metrics(directory, request["mode"]),
        "wall_seconds": process.get("wall_seconds")
        if type(process.get("wall_seconds")) in {int, float}
        and Decimal(str(process["wall_seconds"])).is_finite()
        and process["wall_seconds"] >= 0
        else None,
    }
    result["execution"] = execution
    result["process"] = process
    result["failure_reasons"] = failure_reasons(result, execution, process)
    write_json(directory / "score.json", result)
    return result


# 将业务反馈和外层进程失败共同保存；缺少元数据时也能解释失败，不能把无结果当成功。
def failure_reasons(score, execution, process):
    reasons = []
    outcome = execution.get("outcome") or {}
    if not isinstance(outcome, dict):
        outcome = {}
    if process.get("interrupted"):
        reasons.append({"code": "process_interrupted"})
    elif process.get("timed_out"):
        reasons.append({"code": "process_timeout"})
    elif (
        type(process.get("exit_code")) is not int
        or process["exit_code"] != 0
        or process.get("timed_out") is not False
    ):
        reasons.append(
            {
                "code": "process_failed",
                "exit_code": process.get("exit_code"),
                "message": process.get("error") or process.get("stderr"),
            }
        )
    if execution.get("complete") is not True:
        reasons.append(
            {
                "code": "incomplete_execution",
                "status": outcome.get("status"),
                "message": execution.get("error"),
            }
        )
    if isinstance(outcome.get("feedback"), list):
        reasons.extend(outcome["feedback"])
    if score["missing_data"]:
        reasons.append({"code": "missing_data"})
    elif not score["data_correct"]:
        reasons.append(
            {
                "code": "data_mismatch",
                "missing_records": score["missing_records"],
                "order_correct": score["order_correct"],
            }
        )
    return reasons


# 每组和每任务分别汇总；只计已启动单元，未启动与阻塞单独列出。
def score_batch(directory):
    directory = Path(directory)
    manifest = read_json(directory / "manifest.json")
    rows, grouped = [], defaultdict(list)
    for cell in manifest["cells"]:
        path = directory / cell["directory"]
        if cell["state"] not in {"running", "finished"}:
            continue
        score = score_run(path)
        metrics = score["metrics"]
        sources = metrics.get("llm_calls_by_source")
        row = {
            "task": cell["task"],
            "group": cell["group"],
            "protocol": cell["protocol"],
            "repeat": cell["repeat"],
            "reuse_index": cell.get("reuse_index", 0),
            "directory": cell["directory"],
            "task_success": score["task_success"],
            "record_accuracy": score["record_accuracy"],
            "record_precision": score["record_precision"],
            "record_recall": score["record_recall"],
            "field_accuracy": score["field_accuracy"],
            "llm_calls": sum(sources.values()) if sources is not None else None,
            "llm_calls_by_source": sources,
            "tokens": metrics.get("tokens"),
            "usage_calls": metrics.get("usage_calls"),
            "usage_missing_calls": metrics.get("usage_missing_calls"),
            "token_usage_complete": metrics.get("token_usage_complete"),
            "phase_seconds": metrics.get("phase_seconds"),
            "repairs": metrics.get("repairs"),
            "wall_seconds": metrics.get("wall_seconds"),
            "trace_complete": metrics.get("trace_complete"),
            "metric_basis": metrics.get("metric_basis"),
            "request_counts_complete": metrics.get("request_counts_complete"),
            "exit_code": score["process"].get("exit_code"),
            "timed_out": score["process"].get("timed_out"),
            "interrupted": score["process"].get("interrupted", False),
            "status": (score["execution"].get("outcome") or {}).get("status"),
            "repair_not_started_reason": (score["execution"].get("outcome") or {}).get(
                "repair_not_started_reason"
            ),
            "failure_reasons": score["failure_reasons"],
        }
        rows.append(row)
        # 复用记录不混进生成组成功率，单独作为同源规则的后续执行结果。
        grouped[(row["protocol"], row["group"], row["reuse_index"] > 0)].append(row)
    groups = []
    for (protocol, group, reuse), items in grouped.items():
        counts = Counter(item["task"] for item in items)
        successes = Counter(item["task"] for item in items if item["task_success"])
        costs = {}
        for key in ("llm_calls", "repairs", "wall_seconds"):
            known = [
                item[key]
                for item in items
                if item[key] is not None
                and (key == "wall_seconds" or item["request_counts_complete"])
            ]
            lower_bounds = [
                item[key]
                for item in items
                if item[key] is not None
                and key != "wall_seconds"
                and not item["request_counts_complete"]
            ]
            costs[key] = {
                "observed_sum": sum(known) if known else None,
                "known_runs": len(known),
                "runs": len(items),
                "lower_bound_sum": sum(lower_bounds) if lower_bounds else None,
            }
        groups.append(
            {
                "protocol": protocol,
                "group": group,
                "reuse": reuse,
                "successes": sum(successes.values()),
                "runs": len(items),
                "success_rate": sum(successes.values()) / len(items),
                "by_task": {
                    task: {"successes": successes[task], "runs": count}
                    for task, count in counts.items()
                },
                "costs": costs,
                "mean_record_accuracy": sum(item["record_accuracy"] for item in items) / len(items),
                "mean_field_accuracy": {
                    field: sum(
                        item["field_accuracy"][field]
                        for item in items
                        if field in item["field_accuracy"]
                    )
                    / sum(field in item["field_accuracy"] for item in items)
                    for field in sorted(
                        {field for item in items for field in item["field_accuracy"]}
                    )
                },
            }
        )
    summary = {
        "complete": all(cell["state"] == "finished" for cell in manifest["cells"]),
        "planned": len(manifest["cells"]),
        "launched": len(rows),
        "states": dict(Counter(cell["state"] for cell in manifest["cells"])),
        "groups": groups,
        "runs": rows,
        "comparisons": comparisons(
            rows, complete=all(cell["state"] == "finished" for cell in manifest["cells"])
        ),
    }
    write_json(directory / "summary.json", summary)
    with (directory / "summary.csv").open("w", encoding="utf-8", newline="") as stream:
        writer = csv.DictWriter(
            stream, fieldnames=list(rows[0]) if rows else ["task", "group", "task_success"]
        )
        writer.writeheader()
        for row in rows:
            writer.writerow(
                {
                    key: json.dumps(value, ensure_ascii=False)
                    if isinstance(value, (dict, list))
                    else value
                    for key, value in row.items()
                }
            )
    lines = [
        "# Benchmark 结果",
        "",
        f"完整批次：{summary['complete']}；计划 {summary['planned']}；已启动 {len(rows)}。",
        "",
        "| 协议 | 组 | 复用 | 成功/运行 | 成功率 |",
        "|---|---|---|---|---|",
    ]
    lines.extend(
        f"| {g['protocol']} | {g['group']} | {g['reuse']} | {g['successes']}/{g['runs']} | {g['success_rate']:.1%} |"
        for g in groups
    )
    lines.extend(
        [
            "",
            "以上为已启动运行的观测结果；任务、模式和预算见 manifest.json。小样本不能证明泛化能力或因果收益。",
        ]
    )
    (directory / "summary.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    return summary


# 只有相同任务/重复编号的完整配对才计算提升，未知成本不按零补齐。
def comparisons(rows, *, complete=True):
    results = []
    for protocol, off, on in (
        ("equal-total", "total-no-repair", "total-repair"),
        ("equal-initial", "initial-no-repair", "initial-repair"),
    ):
        by_group = {
            name: {
                (row["task"], row["repeat"]): row
                for row in rows
                if row["protocol"] == protocol and row["group"] == name and not row["reuse_index"]
            }
            for name in (off, on)
        }
        if not by_group[off] and not by_group[on]:
            continue
        pairs = sorted(by_group[off].keys() & by_group[on].keys())
        result = {
            "protocol": protocol,
            "paired_runs": len(pairs),
            "balanced": by_group[off].keys() == by_group[on].keys(),
            "success_uplift_percentage_points": None,
            "mean_added_cost": {},
        }
        # 不完整或不平衡批次只报配对覆盖，不生成易被误读为正式提升的数值。
        if pairs and complete and result["balanced"]:
            result["success_uplift_percentage_points"] = (
                100
                * sum(
                    int(by_group[on][key]["task_success"]) - int(by_group[off][key]["task_success"])
                    for key in pairs
                )
                / len(pairs)
            )
            for cost in ("llm_calls", "wall_seconds", "repairs"):
                known = [
                    key
                    for key in pairs
                    if all(
                        by_group[name][key][cost] is not None
                        and (cost == "wall_seconds" or by_group[name][key]["trace_complete"])
                        for name in (off, on)
                    )
                ]
                result["mean_added_cost"][cost] = {
                    "paired_runs": len(known),
                    "value": sum(
                        by_group[on][key][cost] - by_group[off][key][cost] for key in known
                    )
                    / len(known)
                    if known
                    else None,
                }
            for token in (
                "prompt_tokens",
                "completion_tokens",
                "total_tokens",
                "prompt_cached_tokens",
            ):
                known = [
                    key
                    for key in pairs
                    if all(
                        by_group[name][key]["trace_complete"]
                        and (by_group[name][key].get("token_usage_complete") or {}).get(
                            token, False
                        )
                        and token in (by_group[name][key]["tokens"] or {})
                        for name in (off, on)
                    )
                ]
                result["mean_added_cost"][token] = {
                    "paired_runs": len(known),
                    "value": sum(
                        by_group[on][key]["tokens"][token] - by_group[off][key]["tokens"][token]
                        for key in known
                    )
                    / len(known)
                    if known
                    else None,
                }
        results.append(result)
    return results
