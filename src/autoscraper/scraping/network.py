# 网络 JSON 提取的纯逻辑：URL 匹配、有限路径和值检查，不依赖浏览器或 Agent。
"""Bounded declarative JSON extraction shared by exploration and execution."""

import math
from fnmatch import fnmatchcase
from urllib.parse import urlsplit, urlunsplit

from .errors import ItemError, ScrapeError
from .spec import NetworkSource
from .values import has_field_value


class NetworkError(ScrapeError):
    # 默认响应读取失败不可据此推断规则错误；路径和重复批次由子类单独标记。
    code = "response_failure"
    recoverable = False


class JsonPathError(NetworkError):
    code = "json_path_failure"
    recoverable = True


class NetworkProgressError(NetworkError):
    code = "network_progress_failure"
    recoverable = True


# 查询参数与 fragment 不参与匹配，也不向模型或日志暴露；请求方法仍精确匹配。
def endpoint(url: str) -> str:
    parts = urlsplit(url)
    host = parts.hostname or ""
    if ":" in host:
        host = f"[{host}]"
    if parts.port:
        host += f":{parts.port}"
    return urlunsplit((parts.scheme, host, parts.path, "", ""))


def matches(source: NetworkSource, url: str, method: str) -> bool:
    return source.method == method and fnmatchcase(endpoint(url), source.url_pattern.replace("[", "[[]"))


# 路径只允许键与非负下标，缺失与显式 null 都交给字段 required 约束处理。
def at_path(value, path):
    for part in path:
        if isinstance(part, str) and isinstance(value, dict) and part in value:
            value = value[part]
        elif type(part) is int and isinstance(value, list) and part < len(value):
            value = value[part]
        else:
            raise JsonPathError("JSON path does not exist or traverses an incompatible type")
    return value


# 首版只输出标量及文本列表；对象和复杂数组不隐式转成字符串。
def json_value(value):
    if value is None or isinstance(value, (str, bool)) or type(value) is int:
        return value
    if type(value) is float and math.isfinite(value):
        return value
    if isinstance(value, list) and all(isinstance(item, str) for item in value):
        return value
    raise ItemError("JSON field requires a scalar or a text list")


def extract_json_record(item, source: NetworkSource):
    record = {}
    for name, field in source.fields.items():
        try:
            value = at_path(item, field.path)
        except JsonPathError:
            if field.required:
                raise ItemError(f"Required JSON field {name!r} is missing") from None
            value = None
        value = json_value(value)
        if field.required and not has_field_value(value):
            raise ItemError(f"Required JSON field {name!r} is empty")
        record[name] = value
    return record


def record_items(body, source: NetworkSource):
    items = at_path(body, source.records_path)
    if not isinstance(items, list):
        raise JsonPathError("records_path must resolve to an array")
    return items
