import asyncio
import json
from types import SimpleNamespace

import pytest

from autoscraper.agent import AgentSettings, UnsupportedTaskError
from autoscraper.executor import ScrapeResult, ScraperExecutor
from autoscraper.spec import ScraperSpec
from autoscraper.validation import (
    UnsupportedResult,
    TaskRequirements,
    ValidationSettings,
    validate_data,
    validate_spec,
)
from autoscraper.workflow import explicit_requirements, extract_requirements, run_task


URL = "https://fixture.test/"
TASK = "抓取前2本书，字段名为title、price，分别为书名、价格"


@pytest.fixture
def candidate():
    return ScraperSpec.model_validate(
        {
            "start_url": URL,
            "item_selector": "article",
            "max_items": 2,
            "timeout_ms": 500,
            "fields": {"title": {"selector": "h2"}, "price": {"selector": ".price"}},
            "on_item_error": "fail",
        }
    )


@pytest.fixture
def settings():
    return AgentSettings(
        api_key="fake", model="test", base_url="https://llm.test", max_steps=6, timeout_seconds=30
    )


@pytest.fixture
def workflow(monkeypatch):
    pytest.importorskip("browser_use")
    import autoscraper.workflow as module
    import autoscraper.agent_tools as tools

    model = SimpleNamespace(calls=0)
    monkeypatch.setattr(tools, "make_llm", lambda settings: model)
    return module, model


@pytest.fixture
def executor_page(monkeypatch):
    from playwright.sync_api import sync_playwright

    original = ScraperExecutor.run_page

    def run(self, spec):
        with sync_playwright() as p:
            browser = p.chromium.launch()
            try:
                page = browser.new_page()
                page.route(
                    "**/*",
                    lambda route: route.fulfill(
                        content_type="text/html; charset=utf-8",
                        body='<article><h2>A</h2><b class="price">£1</b></article>'
                        '<article><h2>B</h2><b class="price">£2</b></article>',
                    ),
                )
                return original(self, spec, page)
            finally:
                browser.close()

    monkeypatch.setattr(ScraperExecutor, "run", run)


def install_candidates(monkeypatch, workflow, values):
    module, model = workflow
    received = []

    async def generate(url, task, settings, **kwargs):
        received.append(kwargs)
        model.calls += 1
        value = values[len(received) - 1]
        if isinstance(value, Exception):
            raise value
        return value

    monkeypatch.setattr(module, "generate_spec", generate)
    return received


def test_explicit_fields_and_count_cannot_be_dropped():
    fields, count = explicit_requirements(
        "抓取前25本书，字段名为title、price、description，分别为完整书名、价格、详情描述",
        None,
        None,
    )
    assert fields == ["title", "price", "description"]
    assert count == 25
    assert explicit_requirements(TASK, ["override"], 3) == (["override"], 3)


@pytest.mark.parametrize(
    "task",
    [
        "first 25 books, fields named title, price and description",
        "first 25 books, fields named title, price, and description",
        "前25本书，字段名为title、price和description",
    ],
)
def test_explicit_conjunctions_do_not_drop_last_field(task):
    fields, count = explicit_requirements(task, None, None)
    assert fields == ["title", "price", "description"]
    assert count == 25


def test_bounded_natural_language_requirements():
    pytest.importorskip("browser_use")

    class LLM:
        calls = 0

        async def ainvoke(self, messages, output_format):
            self.calls += 1
            assert "selector" not in messages[-1].content
            assert output_format is TaskRequirements
            return SimpleNamespace(
                completion=TaskRequirements(fields=["title", "price"], count=999)
            )

    model = LLM()
    result = asyncio.run(extract_requirements("前25本书的名称和价格", model))
    assert result.fields == ["title", "price"]
    assert result.count == 25
    assert model.calls == 1


def test_inferred_count_cannot_change_explicit_fields():
    pytest.importorskip("browser_use")

    class LLM:
        async def ainvoke(self, *args, **kwargs):
            return SimpleNamespace(completion=TaskRequirements(fields=["title"], count=25))

    result = asyncio.run(
        extract_requirements("抓取二十五本书，字段名为title、price、description", LLM())
    )
    assert result.fields == ["title", "price", "description"]
    assert result.count == 25


def test_spec_validation_is_independent(candidate):
    requirements = TaskRequirements(fields=["title", "price", "description"], count=25)
    issues = validate_spec(candidate, requirements)
    assert {item.code for item in issues} == {"missing_required_field", "count_mismatch"}
    assert issues[0].fields == ["description"]
    candidate.fields["price"].required = False
    assert "price" in validate_spec(candidate, requirements)[0].fields


@pytest.mark.parametrize(
    "rows,code",
    [
        ([], "empty_extraction"),
        ([{"title": "A"}], "missing_data"),
        ([{"title": "A", "price": " "}], "missing_data"),
        ([{"title": "A", "price": "£1"}], "insufficient_items"),
    ],
)
def test_data_checks(rows, code):
    issues = validate_data(
        ScrapeResult(records=rows), TaskRequirements(fields=["title", "price"], count=2)
    )
    assert code in [item.code for item in issues]


def test_supported_task(candidate, settings, workflow, monkeypatch, executor_page):
    received = install_candidates(monkeypatch, workflow, [candidate])
    result = run_task(URL, TASK, settings, execute=True)
    assert result.status == "success"
    assert result.summary()["task_fulfilled"]
    assert result.attempts == result.llm_calls == 1
    assert len(result.data.records) == 2
    assert received[0]["requirements"].fields == ["title", "price"]


def test_repair_real_selector_failure(candidate, settings, workflow, monkeypatch, executor_page):
    broken = candidate.model_copy(deep=True)
    broken.fields["price"].selector = ".not-there"
    received = install_candidates(monkeypatch, workflow, [broken, candidate])
    result = run_task(URL, TASK, settings, execute=True)
    assert result.status == "success"
    assert result.attempts == 2
    assert received[1]["feedback"][0].code == "selector_failure"
    assert received[1]["previous_spec"] == broken
    assert len(result.data.records) == 2


def test_missing_fields_exhaust_repair_without_execution(
    candidate, settings, workflow, monkeypatch
):
    received = install_candidates(monkeypatch, workflow, [candidate] * 3)
    monkeypatch.setattr(
        ScraperExecutor, "run", lambda *args: pytest.fail("Do not execute incomplete spec")
    )
    result = run_task(URL, "抓取前2本书，字段名为title、price、description", settings)
    assert result.status == "unfulfilled"
    assert result.attempts == 3
    assert not result.summary()["task_fulfilled"]
    assert result.feedback[0].fields == ["description"]
    assert all(
        call["requirements"].fields == ["title", "price", "description"] for call in received
    )


def test_known_unsupported_stops_without_repairs(settings, workflow, monkeypatch):
    received = install_candidates(
        monkeypatch,
        workflow,
        [UnsupportedTaskError("description exists only on detail pages", ["description"])],
    )
    result = run_task(URL, "抓取前25本书，字段名为title、price、description", settings)
    assert result.status == "unsupported"
    assert len(received) == 1
    assert result.feedback[0].code == "unsupported_capability"
    assert not result.feedback[0].recoverable


def test_explorer_unsupported_tool_is_a_failure_terminal(settings, monkeypatch):
    bu = pytest.importorskip("browser_use")
    from autoscraper.agent import generate_spec

    class Browser:
        killed = False

        def __init__(self, **kwargs):
            pass

        async def kill(self):
            Browser.killed = True

    class Agent:
        def __init__(self, **kwargs):
            assert kwargs["output_model_schema"] is ScraperSpec
            assert "report_unsupported" in kwargs["tools"].registry.registry.actions
            assert "description" in kwargs["task"]

        async def run(self, **kwargs):
            return SimpleNamespace(
                is_successful=lambda: False,
                final_result=lambda: json.dumps(
                    {
                        "status": "unsupported",
                        "reason": "description is only on detail pages",
                        "unsupported_fields": ["description"],
                    }
                ),
            )

    monkeypatch.setattr(bu, "BrowserSession", Browser)
    monkeypatch.setattr(bu, "Agent", Agent)
    with pytest.raises(UnsupportedTaskError, match="detail pages"):
        asyncio.run(
            generate_spec(
                URL,
                TASK,
                settings,
                llm=object(),
                requirements=TaskRequirements(fields=["description"]),
            )
        )
    assert Browser.killed


def test_explicit_unsupported_requirements_skip_browser(settings, workflow, monkeypatch):
    module, model = workflow

    async def requirements(*args):
        return TaskRequirements(fields=["description"], unsupported_capabilities=["detail_pages"])

    monkeypatch.setattr(module, "extract_requirements", requirements)
    monkeypatch.setattr(
        module, "generate_spec", lambda *args, **kwargs: pytest.fail("No browser needed")
    )
    result = run_task(URL, "Go to detail pages", settings)
    assert result.status == "unsupported"
    assert result.attempts == 0


def test_zero_repair_limit(candidate, settings, workflow, monkeypatch):
    install_candidates(monkeypatch, workflow, [candidate])
    result = run_task(
        URL,
        "前2本，字段名为title、price、description",
        settings,
        validation=ValidationSettings(max_repairs=0),
    )
    assert result.attempts == 1
    assert result.status == "unfulfilled"


def test_shared_call_budget(candidate, settings, workflow, monkeypatch):
    settings.max_steps = 1
    received = install_candidates(monkeypatch, workflow, [candidate])
    result = run_task(URL, "前2本，字段名为title、price、description", settings)
    assert result.status == "unfulfilled"
    assert len(received) == result.llm_calls == 1
    assert result.feedback[0].code == "budget_exhausted"


def test_rule_only_sampling_does_not_claim_full_task(candidate, settings, workflow, monkeypatch):
    larger = candidate.model_copy(update={"max_items": 200})
    install_candidates(monkeypatch, workflow, [larger])
    monkeypatch.setattr(
        ScraperExecutor,
        "run",
        lambda self, spec: ScrapeResult(
            records=[{"title": "A", "price": "£1"}] * 100, stop_reason="max_items"
        ),
    )
    result = run_task(URL, "字段名为title、price，前200本", settings)
    assert result.status == "validated"
    assert result.sample_only
    assert not result.summary()["task_fulfilled"]


def test_output_requires_full_execution_after_sample(candidate, settings, workflow, monkeypatch):
    larger = candidate.model_copy(update={"max_items": 200})
    install_candidates(monkeypatch, workflow, [larger])
    seen = []

    def execute(self, spec):
        seen.append(spec.max_items)
        return ScrapeResult(
            records=[{"title": "A", "price": "£1"}] * spec.max_items, stop_reason="max_items"
        )

    monkeypatch.setattr(ScraperExecutor, "run", execute)
    result = run_task(URL, "字段名为title、price，前200本", settings, execute=True)
    assert seen == [100, 200]
    assert result.summary()["task_fulfilled"]


def test_validation_budget_exhaustion_is_not_repaired(candidate, settings, workflow, monkeypatch):
    install_candidates(monkeypatch, workflow, [candidate])
    monkeypatch.setattr(
        ScraperExecutor,
        "run",
        lambda self, spec: ScrapeResult(
            records=[{"title": "A", "price": "£1"}], stop_reason="max_pages"
        ),
    )
    result = run_task(URL, TASK, settings, validation=ValidationSettings(max_pages=1))
    assert result.status == "unfulfilled"
    assert result.attempts == 1
    assert result.feedback[0].code == "validation_limit"


def test_bad_requirements_stop_without_exploration(settings, workflow, monkeypatch):
    result = run_task(URL, TASK, settings, count=0)
    assert result.status == "unfulfilled"
    assert result.feedback[0].code == "invalid_requirements"
    assert result.attempts == result.llm_calls == 0


def test_unsupported_envelope_requires_reason():
    with pytest.raises(ValueError):
        UnsupportedResult()
    with pytest.raises(ValueError):
        UnsupportedResult(status="candidate", reason="unsupported")


def test_issue_regression_cli_does_not_save_partial_spec(
    candidate, workflow, monkeypatch, tmp_path, capsys
):
    import autoscraper.agent_cli as cli

    install_candidates(monkeypatch, workflow, [candidate])
    monkeypatch.setenv("AUTOSCRAPER_API_KEY", "fake")
    spec_path, data_path = tmp_path / "spec.json", tmp_path / "data.json"
    spec_path.write_text("existing rule")
    data_path.write_text("existing data")
    monkeypatch.setattr(
        "sys.argv",
        [
            "autoscraper-agent",
            URL,
            "抓取前25本书，字段名为title、price、description，分别为完整书名、价格、详情描述",
            "--spec-output",
            str(spec_path),
            "--output",
            str(data_path),
            "--max-repairs",
            "0",
        ],
    )
    assert cli.main() == 1
    report = json.loads(capsys.readouterr().out)
    assert not report["task_fulfilled"]
    assert report["feedback"][0]["fields"] == ["description"]
    assert spec_path.read_text() == "existing rule"
    assert data_path.read_text() == "existing data"


@pytest.mark.parametrize("status,exit_code", [("unsupported", 3), ("unfulfilled", 1)])
def test_failed_cli_preserves_existing_files(monkeypatch, tmp_path, status, exit_code):
    import autoscraper.agent_cli as cli
    from autoscraper.validation import Feedback
    from autoscraper.workflow import TaskOutcome

    paths = [tmp_path / "rule.json", tmp_path / "data.json", tmp_path / "data.csv"]
    for path in paths:
        path.write_text("previous successful output", encoding="utf-8")
    monkeypatch.setenv("AUTOSCRAPER_API_KEY", "fake")
    monkeypatch.setattr(
        cli,
        "run_task",
        lambda *args, **kwargs: TaskOutcome(
            status=status,
            feedback=[
                Feedback(
                    code="missing_required_field",
                    fields=["description"],
                    message="missing description",
                )
            ],
        ),
    )
    monkeypatch.setattr(
        "sys.argv",
        [
            "autoscraper-agent",
            URL,
            TASK,
            "--spec-output",
            str(paths[0]),
            "--output",
            str(paths[1]),
            "--output",
            str(paths[2]),
        ],
    )
    assert cli.main() == exit_code
    assert all(path.read_text(encoding="utf-8") == "previous successful output" for path in paths)
