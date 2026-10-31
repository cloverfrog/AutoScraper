# 列表字段筛选：仅解释协议内置操作，临时值与原始记录分离。
# 不满足与无法判断分别返回；AND 中已有确定不匹配时，不将其他未知条件当作整条未知。
"""Deterministic, comparison-only transformations and listing filters."""

import math
import re
from dataclasses import dataclass

from .spec import (
    FilterSpec, FilterTransform, FilterValue, MapValue, RegexExtract, RegexReplace, ToNumber,
)
from .values import FieldValue


class FilterValueError(ValueError):
    """A valid rule cannot judge this particular field value."""


# 转换只处理单值，不隐式遍历列表，也不清洗货币符号或千位分隔符。
def transform_value(value: FieldValue | float, transform: FilterTransform) -> str | int | float:
    if isinstance(transform, ToNumber):
        if type(value) in {int, float}:
            number = value
        elif isinstance(value, str) and re.fullmatch(
            r"[+-]?(?:[0-9]+(?:\.[0-9]*)?|\.[0-9]+)(?:[eE][+-]?[0-9]+)?", value.strip()
        ):
            text = value.strip()
            number = float(text) if any(char in text.lower() for char in ".e") else int(text)
        else:
            raise FilterValueError("to_number requires a numeric string or number")
        if isinstance(number, float) and not math.isfinite(number):
            raise FilterValueError("to_number requires a finite number")
        return number
    if not isinstance(value, str):
        raise FilterValueError(f"{transform.op} requires a string")
    if isinstance(transform, RegexExtract):
        match = re.search(transform.pattern, value)
        if match is None or match.group(transform.group) is None:
            raise FilterValueError("regex_extract did not match the requested group")
        return match.group(transform.group)
    if isinstance(transform, RegexReplace):
        return re.sub(transform.pattern, transform.replacement, value)
    if isinstance(transform, MapValue):
        if value not in transform.values:
            raise FilterValueError("map has no entry for the extracted value")
        return transform.values[value]
    raise FilterValueError("Unknown transform")


# contains 对文本表示子串，对文本列表表示完整成员；大小比较只接受有限数值。
def compare(value: FieldValue | float, operator: str, target: FilterValue) -> bool:
    if value is None:
        raise FilterValueError("Filter field is missing")
    if operator == "contains":
        if not isinstance(value, (str, list)):
            raise FilterValueError("contains requires a string or text list")
        return target in value
    numeric = type(value) in {int, float} and type(target) in {int, float}
    if not numeric and not (isinstance(value, str) and isinstance(target, str)):
        raise FilterValueError("Comparison value types do not match")
    if operator == "eq":
        return value == target
    if operator == "ne":
        return value != target
    if not numeric:
        raise FilterValueError("Ordering comparisons require numbers")
    if operator == "lt":
        return value < target
    if operator == "lte":
        return value <= target
    if operator == "gt":
        return value > target
    if operator == "gte":
        return value >= target
    raise FilterValueError("Unknown comparison operator")


@dataclass(frozen=True)
class FilterDecision:
    matched: bool | None
    reason: str | None = None


# 逐条件建立临时值，绝不写回 record；诊断只描述字段和原因，不保存原始内容。
def evaluate_filters(record: dict[str, FieldValue], filters: list[FilterSpec]) -> FilterDecision:
    unknown = None
    for condition in filters:
        try:
            value = record.get(condition.field)
            for transform in condition.transforms:
                value = transform_value(value, transform)
            if not compare(value, condition.operator, condition.value):
                return FilterDecision(False)
        except (ValueError, OverflowError) as exc:
            unknown = unknown or f"{condition.field}: {exc}"
    return FilterDecision(None, unknown) if unknown else FilterDecision(True)
