# 网络 JSON 提取的纯逻辑：URL 匹配、有限路径和值检查，不依赖浏览器或 Agent。
"""Bounded declarative JSON extraction shared by exploration and execution."""

from fnmatch import fnmatchcase
from urllib.parse import urlsplit, urlunsplit

from .errors import ItemError, ScrapeError
from .spec import NetworkSource
from .values import is_supported_value


class NetworkError(ScrapeError):
    # 默认响应读取失败不可据此推断规则错误；路径和重复批次由子类单独标记。
    code = "response_failure"
    recoverable = False


class JsonPathError(NetworkError):
    code = "json_path_failure"
    recoverable = True


# 缺键/越界与穿越不兼容类型分开：前者允许单条缺失，后者仍是规则/结构错误。
class MissingJsonPath(JsonPathError):
    pass


class JsonFieldError(ItemError):
    def __init__(self, field, path, reason):
        self.field, self.path, self.reason = field, path, reason
        super().__init__(f"JSON field {field!r}: {reason}; path={path!r}")


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


# 显式 null（包括可空父对象）忠实保留；缺失和类型不兼容分别反馈。
def at_path(value, path):
    for part in path:
        if value is None:
            return None
        if isinstance(part, str) and isinstance(value, dict):
            if part not in value:
                raise MissingJsonPath("JSON path is missing")
            value = value[part]
        elif type(part) is int and isinstance(value, list):
            if not 0 <= part < len(value):
                raise MissingJsonPath("JSON path index is missing")
            value = value[part]
        else:
            raise JsonPathError("JSON path traverses an incompatible type")
    return value


# 首版只输出标量及文本列表；对象和复杂数组不隐式转成字符串。
def json_value(value):
    if is_supported_value(value):
        return value
    raise ItemError("JSON field requires a scalar or a text list")


def extract_json_record(item, source: NetworkSource, *, fields_found=None):
    record = {}
    found = []
    for name, field in source.fields.items():
        try:
            value = at_path(item, field.path)
        except MissingJsonPath:
            record[name] = None
            continue
        except JsonPathError:
            raise JsonFieldError(name, field.path, "incompatible_path_type") from None
        try:
            record[name] = json_value(value)
        except ItemError:
            raise JsonFieldError(name, field.path, "unsupported_value_type") from None
        found.append(name)
    if fields_found is not None:
        for name in found:
            fields_found[name] = fields_found.get(name, 0) + 1
    return record


def record_items(body, source: NetworkSource):
    items = at_path(body, source.records_path)
    if not isinstance(items, list):
        raise JsonPathError("records_path must resolve to an array")
    return items
