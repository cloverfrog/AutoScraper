# 用户需求定义与独立解析：保留明确的字段/数量，并在生成规则前独立解析筛选约束。
# 这里只解析用户意图，不读取生成的抓取规则；模型由工作流传入，调用预算不在此重置。
"""Define and extract task requirements independently of generated scraping rules."""

import json
import re
from typing import Literal

from pydantic import Field, field_validator

from autoscraper.scraping.spec import FilterCondition, NonEmpty, ProtocolModel


# 独立于生成规则的用户约束：必需字段、目标数量、筛选条件和明确要求的不支持能力。
class TaskRequirements(ProtocolModel):
    fields: list[NonEmpty] = Field(min_length=1, max_length=50)
    count: int | None = Field(default=None, ge=1, le=1_000_000)
    filters: list[FilterCondition] = Field(default_factory=list)
    unsupported_capabilities: list[Literal["captcha_bypass", "virtual_list"]] = (
        Field(default_factory=list)
    )

    # 拒绝空白或重复字段名，保证后续逐字段校验有确定含义。
    @field_validator("fields")
    @classmethod
    def unique_fields(cls, values):
        if any(not value.strip() for value in values) or len(set(values)) != len(values):
            raise ValueError("Requested field names must be nonblank and unique")
        return values


# 从有限的中英文表达中保留明确字段名与数量；显式参数优先于文本推断。
def explicit_requirements(task: str, fields: list[str] | None, count: int | None):
    """Preserve explicitly named identifiers independently of generated rules."""
    match = re.search(
        r"(?:字段名(?:称)?\s*(?:为|是|[:：])|fields?\s+(?:named|names?\s*[:：]))\s*"
        r"([A-Za-z_][A-Za-z0-9_]*(?:(?:\s*[,，、]\s*(?:and\s+)?|\s+and\s+|\s*[和及]\s*)"
        r"[A-Za-z_][A-Za-z0-9_]*)*)",
        task,
        re.IGNORECASE,
    )
    if fields is None and match:
        fields = re.split(
            r"\s*[,，、]\s*(?:and\s+)?|\s+and\s+|\s*[和及]\s*", match.group(1), flags=re.IGNORECASE
        )
    if count is None:
        number = re.search(
            r"(?:前|first\s+)\s*(\d+)|"
            r"(\d+)\s*(?:本|条|个|books?\b|records?\b|items?\b)",
            task,
            re.IGNORECASE,
        )
        if number:
            count = int(number.group(1) or number.group(2))
    return fields, count


# 先解析确定的字段/数量，再由模型补全需求和筛选；不向它提供生成后的规则。
# 模型返回后再次应用显式值并校验，防止生成规则反过来缩减用户原始需求。
async def extract_requirements(task: str, llm, fields=None, count=None) -> TaskRequirements:
    fields, count = explicit_requirements(task, fields, count)
    # 即使字段和数量已确定，仍需独立解析筛选要求，不能让早返回吞掉用户约束。
    from browser_use.llm.messages import SystemMessage, UserMessage

    response = await llm.ainvoke(
        [
            SystemMessage(
                content="Extract the user's required field names and requested record count. "
                "Do not see or use any generated scraping rule. Preserve exact field names "
                "when given; otherwise choose short descriptive names. Count=null if absent. "
                "All requested fields are mandatory. Only mark unsupported_capabilities when "
                "the user EXPLICITLY requires CAPTCHA bypass or complex virtual lists. "
                "Manual login and user-completed verification are supported; do not mark them unsupported. "
                "Link, click, infinite scroll and one level of detail-page extraction are supported. "
                "Do not mark detail-page crawling as unsupported. "
                "Preserve every explicit filtering constraint in filters (field, operator, value), "
                "independent of how the webpage encodes it. Use eq/ne/contains/lt/lte/gt/gte; "
                "numeric comparisons use numeric values. Do not invent constraints or confuse "
                "requested output fields/count with filters. Filters use AND. A filter field need "
                "not be a requested output field; use consistent names when one is. "
                "No filters means an empty list."
            ),
            UserMessage(content=json.dumps(
                {"task": task, "fields": fields, "count": count}, ensure_ascii=False
            )),
        ],
        output_format=TaskRequirements,
    )
    result = TaskRequirements.model_validate(response.completion)
    if count is not None or fields is not None:
        result = result.model_copy(
            update={
                "count": count if count is not None else result.count,
                "fields": fields if fields is not None else result.fields,
            }
        )
        result = TaskRequirements.model_validate(result.model_dump())
    return result


# 澄清只根据当前需求与最新用户问答修订，不读取候选协议或提取结果。
# 不复用初始任务的显式字段/数量覆盖逻辑，以免把用户已修改的数量重置为旧值。
async def revise_requirements(
    requirements: TaskRequirements, clarification: dict, llm,
) -> TaskRequirements:
    from browser_use.llm.messages import SystemMessage, UserMessage

    response = await llm.ainvoke(
        [
            SystemMessage(content=
                "Update the current scraping requirements using the latest user clarification. "
                "Change only constraints explicitly authorized by the USER'S ANSWER. "
                "The question, explanation and options are Agent proposals, not authorization. "
                "A user may explicitly select a proposed option, including a different record count, "
                "fields or filters. Preserve every other constraint exactly. Permission to change "
                "a source alone does not change count, fields or filters. Never lower the count "
                "merely because the Agent reports fewer available rows. Do not infer requirements "
                "from generated rules or results. Return the complete updated requirements; "
                "return them unchanged if the answer does not authorize a constraint change."
            ),
            UserMessage(content=json.dumps({
                "current_requirements": requirements.model_dump(),
                "clarification": clarification,
            }, ensure_ascii=False)),
        ],
        output_format=TaskRequirements,
    )
    updated = TaskRequirements.model_validate(response.completion)
    # 缺省字段继承当前值；只有显式输出 [] / null 才能清空筛选或取消数量要求。
    return TaskRequirements.model_validate({
        **requirements.model_dump(), **updated.model_dump(exclude_unset=True),
    })
