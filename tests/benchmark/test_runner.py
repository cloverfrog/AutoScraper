# 编排测试不启动付费模型或真实浏览器；验证隔离、预算配置和失败处理。
import copy
import sys

import pytest

from benchmarks import runner
from benchmarks.common import HOME, read_json, write_json


def smoke():
    config = read_json(HOME / "configs/smoke.json")
    config["tasks"] = ["local-network"]
    config["groups"][0]["specs"].pop("books-detail")
    return config


def test_two_budget_protocols_and_alternating_order():
    config = runner.validate_config(read_json(HOME / "configs/repair.json"))
    values = [
        (
            g["budget"]["max_llm_calls"],
            g["budget"]["repair_reserve_calls"],
            g["budget"]["max_repairs"],
        )
        for g in config["groups"]
    ]
    assert values == [(24, 0, 0), (24, 0, 2), (18, 0, 0), (24, 6, 2)]
    cells = runner.expand_cells(config)
    assert len(cells) == 72
    assert [c["group"] for c in cells[:4]] == [g["id"] for g in config["groups"]]
    assert [c["group"] for c in cells[24:28]] == [g["id"] for g in reversed(config["groups"])]
    assert len({c["directory"] for c in cells}) == 72


@pytest.mark.parametrize(
    "change",
    ["unknown", "duplicate", "bad_repeat", "reserve", "missing_spec", "native", "typo", "negative"],
)
def test_bad_config_rejected(change):
    config = smoke()
    if change == "unknown":
        config["tasks"] = ["unknown"]
    elif change == "duplicate":
        config["groups"] *= 2
    elif change == "bad_repeat":
        config["repeats"] = 0
    elif change == "reserve":
        config["groups"][0]["budget"] = {"repair_reserve_calls": 6}
    elif change == "missing_spec":
        config["groups"][0]["specs"] = {}
    elif change == "native":
        config["groups"][0]["mode"] = "browser-use"
    elif change == "typo":
        config["budget"] = {"max_call": 3}
    else:
        config["budget"] = {"max_llm_calls": -1}
    with pytest.raises(ValueError):
        runner.validate_config(config)


def test_paid_guard_runs_before_any_network_or_git(monkeypatch, tmp_path):
    monkeypatch.setattr(runner, "verify_commit", lambda *a: pytest.fail("must not start"))
    with pytest.raises(ValueError, match="allow-llm"):
        runner.run_batch(read_json(HOME / "configs/repair.json"), tmp_path, commit="a" * 40)
    assert list(tmp_path.iterdir()) == []


def test_commit_and_clean_checkout_required(monkeypatch):
    monkeypatch.setattr(
        runner, "git", lambda *args: "a" * 40 if args[0] == "rev-parse" else "?? file"
    )
    with pytest.raises(ValueError, match="full"):
        runner.verify_commit("HEAD")
    with pytest.raises(ValueError, match="match"):
        runner.verify_commit("b" * 40)
    with pytest.raises(ValueError, match="clean"):
        runner.verify_commit("a" * 40)
    assert not runner.verify_commit("a" * 40, True)["formal"]


def test_timeout_kills_worker_and_preserves_partial_output():
    result = runner.launch(["-c", "import time; print('started', flush=True); time.sleep(10)"], 0.1)
    assert result["timed_out"]
    assert result["exit_code"] != 0 and "started" in result["stdout"]
    assert result["wall_seconds"] < 5


def test_credentials_are_sanitized(monkeypatch):
    monkeypatch.setenv("AUTOSCRAPER_API_KEY", "private-key-value")
    result = runner.launch(["-c", "import os; print(os.environ['AUTOSCRAPER_API_KEY'])"], 10)
    assert "private-key-value" not in result["stdout"]


# 执行替身只负责落原始文件；评分仍使用真实独立评分器。
def fake_worker(arguments, timeout):
    from pathlib import Path

    directory = Path(arguments[-1])
    write_json(directory / "records.json", read_json(directory / "truth.json")["records"])
    write_json(directory / "execution.json", {"complete": True})
    return {"exit_code": 0, "wall_seconds": 0.1, "timed_out": False}


def stub_environment(monkeypatch):
    monkeypatch.setattr(
        runner, "verify_commit", lambda *args: {"commit": "a" * 40, "formal": False}
    )
    monkeypatch.setattr(runner, "environment", lambda: {})
    monkeypatch.setattr(runner, "check_task", lambda *args, **kwargs: {"passed": True})
    monkeypatch.setattr(runner, "launch", fake_worker)


def test_independent_batches_repeats_and_reuse(monkeypatch, tmp_path):
    stub_environment(monkeypatch)
    config = smoke()
    config["repeats"] = 2
    config["groups"][0]["reuse_runs"] = 1
    batches = [runner.run_batch(config, tmp_path, commit="a" * 40) for _ in range(2)]
    assert batches[0] != batches[1]
    for batch in batches:
        summary = read_json(batch / "summary.json")
        assert summary["complete"] and summary["launched"] == 4
        assert len(summary["groups"]) == 2
        manifest = read_json(batch / "manifest.json")
        for cell in manifest["cells"]:
            path = batch / cell["directory"]
            assert read_json(path / "score.json")["task_success"]
            assert (path / "spec.json").exists()


def test_source_drift_blocks_every_group_without_running(monkeypatch, tmp_path):
    stub_environment(monkeypatch)
    monkeypatch.setattr(runner, "check_task", lambda *args, **kwargs: {"passed": False})
    monkeypatch.setattr(runner, "launch", lambda *args: pytest.fail("blocked task must not run"))
    config = smoke()
    extra = copy.deepcopy(config["groups"][0])
    extra["id"] = "other"
    config["groups"].append(extra)
    batch = runner.run_batch(config, tmp_path, commit="a" * 40)
    summary = read_json(batch / "summary.json")
    assert summary["launched"] == 0 and summary["states"] == {"blocked": 2}


def test_failed_generation_never_falls_back_for_reuse(monkeypatch, tmp_path):
    stub_environment(monkeypatch)
    config = read_json(HOME / "configs/repair.json")
    config["tasks"] = ["local-network"]
    config["repeats"] = 1
    config["groups"] = config["groups"][:1]
    config["groups"][0]["protocol"] = "fixture"
    config["groups"][0]["reuse_runs"] = 2
    monkeypatch.setattr(runner, "launch", lambda *args: {"exit_code": 1, "timed_out": False})
    batch = runner.run_batch(config, tmp_path, commit="a" * 40, allow_llm=True)
    result = read_json(batch / "summary.json")
    assert result["launched"] == 1 and result["states"] == {"finished": 1, "blocked": 2}


def test_offline_scorer_does_not_import_browser_or_agent():
    import subprocess

    code = "import sys; import benchmarks.scoring; assert not any(n.startswith(('browser_use','playwright','autoscraper')) for n in sys.modules)"
    subprocess.run([sys.executable, "-c", code], check=True)


# 四组预算必须能经真实 CLI 参数解析；替换执行函数，禁止进入任何模型调用。
@pytest.mark.parametrize("group_index", range(4))
def test_worker_passes_budget_to_existing_agent_cli(monkeypatch, tmp_path, group_index):
    from contextlib import contextmanager, nullcontext
    from types import SimpleNamespace

    from autoscraper.cli import agent as cli
    from benchmarks import worker
    from benchmarks.common import tasks

    config = runner.validate_config(read_json(HOME / "configs/repair.json"))
    group = config["groups"][group_index]
    directory = tmp_path / "run"
    runner.prepare(directory, tasks()["books-detail"], group, config)
    browser = SimpleNamespace(version="test-browser", close=lambda: None)
    playwright = SimpleNamespace(
        chromium=SimpleNamespace(launch=lambda **kwargs: browser, executable_path="test-chromium")
    )

    @contextmanager
    def fake_playwright():
        yield playwright

    monkeypatch.setattr("playwright.sync_api.sync_playwright", fake_playwright)
    monkeypatch.setattr(worker, "task_site", lambda task: nullcontext(task["url"]))
    monkeypatch.setattr(sys, "argv", sys.argv.copy())

    def execute(args, trace):
        assert args.non_interactive
        assert args.spec_input is None
        assert "truth.json" not in str(args) and "candidate.json" not in str(args)
        assert args.model == config["model"]
        assert args.max_llm_calls == group["budget"]["max_llm_calls"]
        assert args.repair_reserve_calls == group["budget"]["repair_reserve_calls"]
        assert args.max_repairs == group["budget"]["max_repairs"]
        assert args.timeout_seconds == 240
        assert args.fields == ["title", "price", "url", "description"]
        assert args.count == 5
        print('{"status":"success","sample_only":false}')
        return 0

    monkeypatch.setattr(cli, "_execute", execute)
    assert worker.main(directory) == 0
    assert read_json(directory / "execution.json")["complete"]
