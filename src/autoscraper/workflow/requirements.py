# 独立需求解析：先保留文本和参数中明确的字段/数量，再由模型补充缺失部分。
# 这里只解析用户意图，不读取生成的抓取规则；模型由工作流传入，调用预算不在此重置。
"""Extract task requirements independently of generated scraping rules."""

import json
import re

from .validation import TaskRequirements


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


# 先解析确定的字段/数量，只把缺失部分交给模型；不向它提供生成后的规则。
# 模型返回后再次应用显式值并校验，防止生成规则反过来缩减用户原始需求。
async def extract_requirements(task: str, llm, fields=None, count=None) -> TaskRequirements:
    fields, count = explicit_requirements(task, fields, count)
    if fields is not None and count is not None:
        return TaskRequirements(fields=fields, count=count)
    from browser_use.llm.messages import SystemMessage, UserMessage

    response = await llm.ainvoke(
        [
            SystemMessage(
                content="Extract the user's required field names and requested record count. "
                "Do not see or use any generated scraping rule. Preserve exact field names "
                "when given; otherwise choose short descriptive names. Count=null if absent. "
                "All requested fields are mandatory. Only mark unsupported_capabilities when "
                "the user EXPLICITLY requires JS-only pagination, infinite scroll or login. "
                "One level of detail-page extraction is supported. "
                "Do not mark detail-page crawling as unsupported."
            ),
            UserMessage(content=json.dumps({"task": task}, ensure_ascii=False)),
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
