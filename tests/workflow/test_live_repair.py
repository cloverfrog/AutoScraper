# 可选付费修复验收：对真实网站和 DeepSeek 制造一次选择器失败，验证实际修复闭环。
# 默认不运行，只有显式设置 RUN_LIVE_REPAIR 才会调用真实模型。
"""Opt-in paid DeepSeek repair test against Books to Scrape, without LLM mocks."""

import json
import os

import pytest

from autoscraper.agent.explorer import save_spec
from autoscraper.cli.agent import settings_from_env
from autoscraper.workflow.validation import ValidationSettings, validate_data, validate_spec
from autoscraper.workflow.runner import run_task


# 保留真实生成器、模型和执行器，确认反馈可驱动后续规则修复并完成抓取。
@pytest.mark.llm
@pytest.mark.skipif(
    os.getenv("RUN_LIVE_REPAIR") != "1",
    reason="Set RUN_LIVE_REPAIR=1 for paid DeepSeek repair test",
)
def test_real_deepseek_repairs_broken_selector(monkeypatch, tmp_path):
    import autoscraper.workflow.runner as workflow

    settings = settings_from_env(
        base_url="https://api.deepseek.com", max_steps=24, timeout_seconds=480
    )
    assert settings.model.startswith("deepseek-"), "Use a DeepSeek model for this test"
    real_generate_spec = workflow.generate_spec
    candidates = []
    repair_inputs = []
    broken_selector = '[data-autoscraper-intentionally-missing-price="true"]'

    async def generate_with_fault(url, task, settings, **kwargs):
        # Keep the production generator, LLM, browser and Validator real. The only
        # intervention is changing one selector after the first paid generation.
        if candidates:
            repair_inputs.append(kwargs)
        candidate = await real_generate_spec(url, task, settings, **kwargs)
        candidates.append(candidate.model_copy(deep=True))
        save_spec(candidate, tmp_path / f"candidate-{len(candidates)}.json")
        if len(candidates) == 1:
            assert not validate_spec(candidate, kwargs["requirements"])
            broken = candidate.model_copy(deep=True)
            broken.fields["price"].selector = broken_selector
            save_spec(broken, tmp_path / "broken.json")
            return broken
        return candidate

    monkeypatch.setattr(workflow, "generate_spec", generate_with_fault)
    outcome = run_task(
        "https://books.toscrape.com/",
        "Extract the first 25 books with fields named title, price, url. "
        "Use full book titles, prices including currency, and absolute detail URLs. "
        "Include pagination.",
        settings,
        fields=["title", "price", "url"],
        count=25,
        validation=ValidationSettings(max_repairs=1),
        execute=True,
    )
    report = outcome.summary()
    (tmp_path / "validation.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    assert outcome.status == "success", report
    assert outcome.attempts == len(candidates) == 2
    assert outcome.llm_calls >= 2
    assert len(repair_inputs) == 1
    repair = repair_inputs[0]
    assert repair["previous_spec"].fields["price"].selector == broken_selector
    assert any(
        issue.code == "selector_failure" and "price" in issue.message
        for issue in repair["feedback"]
    ), report
    assert outcome.history[0]["feedback"] == [
        issue.model_dump() for issue in repair["feedback"]
    ]
    assert outcome.history[1]["feedback"] == []
    assert outcome.spec.fields["price"].selector != broken_selector
    assert not validate_spec(outcome.spec, outcome.requirements)
    assert not validate_data(outcome.data, outcome.requirements)
    assert not outcome.data.errors
    records = outcome.data.records
    assert len(records) == len({row["url"] for row in records}) == 25
    assert outcome.data.pages_visited == 2
    assert records[0]["title"] == "A Light in the Attic"
    assert records[0]["price"] == "£51.77"
    assert all(row["price"].startswith("£") for row in records)
    assert all(row["url"].startswith("https://books.toscrape.com/") for row in records)
