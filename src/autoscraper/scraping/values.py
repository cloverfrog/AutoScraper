# 字段值的共同约定：供执行器、验证与去重使用，不依赖浏览器或 Agent。
"""Supported extracted values and their required-value semantics."""

import math

FieldValue = str | int | float | bool | list[str] | None


# 合法提取值允许真实空值，但仍拒绝非有限数字、对象及复杂数组。
def is_supported_value(value) -> bool:
    return (value is None or isinstance(value, (str, bool)) or type(value) is int
            or type(value) is float and math.isfinite(value)
            or isinstance(value, list) and all(isinstance(item, str) for item in value))


# 判断值是否适合作为标识或导航目标，不用于判定字段数据是否合法；空值也可合法保存。
def has_field_value(value: FieldValue) -> bool:
    if isinstance(value, str):
        return bool(value.strip())
    if type(value) in {int, bool}:
        return True
    if type(value) is float:
        return math.isfinite(value)
    if isinstance(value, list):
        return bool(value) and all(isinstance(text, str) and text.strip() for text in value)
    return False


# 列表按原始顺序和重复值参与去重；仅冻结键，不改变保存与导出的字段值。
def record_identity(record: dict[str, FieldValue], keys: list[str]) -> tuple:
    # 空标识不能把不同记录合并；回退到整条记录去重，保留原始空值。
    if any(not has_field_value(record.get(key)) for key in keys):
        keys = list(record)
    return tuple((type(record[key]).__name__,
                  tuple(record[key]) if isinstance(record[key], list) else record[key])
                 for key in keys)
