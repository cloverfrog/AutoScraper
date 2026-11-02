# 原生 Baseline 离线契约：保留原生工具、共用真实请求预算，宿主不改写抓取结果。
import asyncio
import json
import subprocess
import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from pydantic import ValidationError

from benchmarks import native, runner
from benchmarks.common import HOME, read_json, tasks


@pytest.fixture(autouse=True)
def disable_native_telemetry(monkeypatch):
    monkeypatch.setenv("ANONYMIZED_TELEMETRY", "false")
    monkeypatch.setenv("BROWSER_USE_CLOUD_SYNC", "false")


def config():
    return read_json(HOME / "configs/native-pilot.json")


def request():
    value = runner.validate_config(config())
    return {
        "mode": "native",
        "model": value["model"],
        "base_url": value["base_url"],
        "budget": value["groups"][0]["budget"],
        "browser_path": None,
    }


def test_native_config_is_only_two_real_runs_and_one_shared_budget(monkeypatch, tmp_path):
    value = runner.validate_config(config())
    cells = runner.expand_cells(value)
    assert len(cells) == 2 and {cell["task"] for cell in cells} == {"books-list", "cars-load-more"}
    assert value["groups"][0]["budget"]["max_llm_calls"] == 32
    monkeypatch.setattr(runner, "verify_commit", lambda *args: pytest.fail("no work before opt in"))
    with pytest.raises(ValueError, match="allow-llm"):
        runner.run_batch(config(), tmp_path, commit="a" * 40)


@pytest.mark.parametrize(
    "key,value", [("max_repairs", 1), ("max_wait_llm_calls", 8), ("repair_reserve_calls", 3)]
)
def test_native_rejects_hidden_repair_and_extra_model_pools(key, value):
    value_config = config()
    value_config["budget"][key] = value
    with pytest.raises(ValueError):
        runner.validate_config(value_config)


def test_native_rejects_seed_spec_or_automatic_reuse():
    value = config()
    value["groups"][0]["specs"] = {}
    with pytest.raises(ValueError):
        runner.validate_config(value)
    value = config()
    value["groups"][0]["reuse_runs"] = 1
    with pytest.raises(ValueError):
        runner.validate_config(value)


def test_schema_is_only_the_requested_data_not_answers_or_selectors():
    task = tasks()["books-list"]
    schema = native.output_schema(task)
    parsed = schema.model_validate({"records": []})
    assert parsed.records == []  # 数量错误交给独立评分，不在 schema 中强凑25条。
    text = json.dumps(schema.model_json_schema())
    for forbidden in ("ScraperSpec", "selector", "A Light in the Attic", "51.77"):
        assert forbidden not in text
    with pytest.raises(ValidationError):
        schema.model_validate(
            {"records": [{"title": "A", "price": 5, "url": "https://example.test"}]}
        )


def history(raw, *, successful=True):
    return SimpleNamespace(
        final_result=lambda: raw,
        is_done=lambda: True,
        is_successful=lambda: successful,
        errors=lambda: [None, "temporary"],
        judgement=lambda: {"verdict": True},
        history=[],
    )


def test_save_output_preserves_order_duplicates_and_values(tmp_path):
    task = tasks()["books-list"]
    rows = [
        {"title": "B", "price": "wrong", "url": "/b"},
        {"title": "B", "price": "wrong", "url": "/b"},
        {"title": "A", "price": "£1", "url": "/a"},
    ]
    raw = json.dumps({"records": rows})
    assert native.save_output(tmp_path, task, history(raw))
    assert read_json(tmp_path / "records.json") == rows
    assert (tmp_path / "native-final.txt").read_text() == raw


def test_no_llm_repair_or_guessing_of_invalid_final_json(tmp_path):
    with pytest.raises(ValidationError):
        native.save_output(tmp_path, tasks()["books-list"], history("not JSON"))
    assert (tmp_path / "native-final.txt").read_text() == "not JSON"
    assert not (tmp_path / "records.json").exists()


def test_same_responses_parameters_and_actual_retry_budget(monkeypatch, tmp_path):
    from autoscraper.agent.llm import make_llm
    from autoscraper.agent.errors import LLMBudgetExceeded
    from autoscraper.observability.tracing import FileSink, Trace

    monkeypatch.setenv("AUTOSCRAPER_API_KEY", "fake-native-key")
    req = request()
    req["budget"]["max_llm_calls"] = 2
    model = native.make_model(req)
    settings = SimpleNamespace(
        model=req["model"],
        api_key=SimpleNamespace(get_secret_value=lambda: "fake-native-key"),
        base_url=req["base_url"],
        llm_timeout_seconds=45,
        max_llm_calls=2,
        max_output_tokens=3000,
    )
    production = make_llm(settings)
    for key in (
        "model",
        "base_url",
        "temperature",
        "reasoning_effort",
        "max_retries",
        "structured_output_retries",
        "max_output_tokens",
        "timeout",
    ):
        assert getattr(model, key) == getattr(production, key)
    usage = SimpleNamespace(
        input_tokens=10,
        output_tokens=2,
        total_tokens=12,
        input_tokens_details=SimpleNamespace(cached_tokens=5),
    )
    create = AsyncMock(
        side_effect=[
            SimpleNamespace(status="completed", usage=usage, output_text=text)
            for text in ["invalid JSON", '{"records":[]}']
        ]
    )
    monkeypatch.setattr(
        type(model),
        "get_client",
        lambda self: SimpleNamespace(responses=SimpleNamespace(create=create)),
    )
    with Trace(FileSink(tmp_path / "trace")).run():
        asyncio.run(model.ainvoke([], output_format=native.output_schema(tasks()["books-list"])))
        with pytest.raises(LLMBudgetExceeded):
            asyncio.run(model.ainvoke([]))
    assert model.calls == create.await_count == 2
    summary = read_json(next((tmp_path / "trace").glob("*/summary.json")))
    assert summary["metrics"]["llm_calls_by_source"] == {"main": 2}
    assert summary["metrics"]["tokens"]["total_tokens"] == 24


def test_native_tools_are_not_autoscraper_tools():
    from browser_use import Tools

    tools = Tools(output_model=native.output_schema(tasks()["books-list"]))
    names = set(tools.registry.registry.actions)
    assert {"evaluate", "extract", "read_file", "write_file", "navigate", "done"} <= names
    assert not names & {
        "inspect_page",
        "test_fields",
        "test_selector",
        "test_network",
        "report_unsupported",
    }


def test_module_does_not_import_production_workflow_or_explorer():
    subprocess.run(
        [
            sys.executable,
            "-c",
            "import sys; import benchmarks.native; assert 'autoscraper.agent.explorer' not in sys.modules; assert 'autoscraper.workflow.runner' not in sys.modules",
        ],
        check=True,
    )


def test_execute_uses_native_defaults_and_one_model_for_auxiliary_calls(monkeypatch, tmp_path):
    captured = {}
    raw = '{"records":[{"title":"B","price":"£1","url":"https://example.test/b"}]}'
    result_history = history(raw)
    model = SimpleNamespace(max_calls=32, max_output_tokens=3000, calls=0)
    monkeypatch.setattr(native, "make_model", lambda req: model)

    class Browser:
        def __init__(self, **kwargs):
            captured["browser"] = kwargs
            self.cdp_client = SimpleNamespace(
                send=SimpleNamespace(
                    Browser=SimpleNamespace(
                        getVersion=AsyncMock(return_value={"product": "scripted-browser"})
                    )
                )
            )

        async def start(self):
            pass

        async def kill(self):
            captured["killed"] = True

    class Agent:
        def __init__(self, **kwargs):
            captured["agent"] = kwargs
            self.history = result_history
            self.tools = SimpleNamespace(
                registry=SimpleNamespace(
                    registry=SimpleNamespace(actions={"evaluate": None, "extract": None})
                )
            )
            self.settings = SimpleNamespace(
                use_thinking=True,
                flash_mode=False,
                enable_planning=True,
                use_judge=True,
                max_actions_per_step=5,
                max_failures=5,
                max_history_items=None,
                max_clickable_elements_length=40000,
                final_response_after_failure=True,
                message_compaction=True,
            )

        async def run(self, **kwargs):
            captured["run"] = kwargs
            return self.history

    monkeypatch.setattr("browser_use.Agent", Agent)
    monkeypatch.setattr("browser_use.BrowserSession", Browser)
    result, code = asyncio.run(
        native.execute(tmp_path, tasks()["books-list"], request(), "https://example.test/")
    )
    kwargs = captured["agent"]
    assert kwargs["llm"] is kwargs["judge_llm"] is kwargs["page_extraction_llm"] is model
    assert (
        not {
            "tools",
            "controller",
            "extend_system_message",
            "override_system_message",
            "initial_actions",
            "use_judge",
            "use_thinking",
            "flash_mode",
            "message_compaction",
            "max_actions_per_step",
        }
        & kwargs.keys()
    )
    assert kwargs["available_file_paths"] == [] and kwargs["use_vision"] is False
    assert "ScraperSpec" not in kwargs["task"] and "truth" not in kwargs["task"]
    assert captured["run"]["max_steps"] == 12 and captured["killed"]
    assert result["complete"] and code == 0 and result["outcome"]["feedback"] == []
    assert read_json(tmp_path / "records.json")[0]["title"] == "B"
    assert not (tmp_path / "spec.json").exists()


def test_failure_still_closes_trace_and_saves_metadata(monkeypatch, tmp_path):
    async def fail(*args):
        raise TimeoutError("native deadline")

    monkeypatch.setattr(native, "execute", fail)
    assert native.run(tmp_path, tasks()["books-list"], request(), "https://example.test/") == 1
    result = read_json(tmp_path / "execution.json")
    assert not result["complete"] and result["outcome"]["feedback"][0]["code"] == "native_timeout"
    trace = read_json(next((tmp_path / "trace").glob("*/summary.json")))
    assert trace["termination"] == "returned" and trace["exit_code"] == 1
