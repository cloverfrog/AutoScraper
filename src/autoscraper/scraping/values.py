# 字段值的共同约定：供执行器、验证与去重使用，不依赖浏览器或 Agent。
"""Supported extracted values and their required-value semantics."""

FieldValue = str | int | list[str] | None


# 数字零是有效计数；文本列表必须非空且每个文本非空，才能满足必填要求。
def has_field_value(value: FieldValue) -> bool:
    if isinstance(value, str):
        return bool(value.strip())
    if type(value) is int:
        return value >= 0
    if isinstance(value, list):
        return bool(value) and all(isinstance(text, str) and text.strip() for text in value)
    return False


# 列表按原始顺序和重复值参与去重；仅冻结键，不改变保存与导出的字段值。
def record_identity(record: dict[str, FieldValue], keys: list[str]) -> tuple:
    return tuple(tuple(record[key]) if isinstance(record[key], list) else record[key] for key in keys)
