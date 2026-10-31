# 抓取协议的数据模型：定义列表字段、详情字段、分页和执行上限。
# Agent 输出的 JSON 与手写规则都在这里解析；执行器只解释这些声明，不执行规则中的代码。
# 本模块只依赖 Pydantic；跨字段约束也在模型校验器中统一检查。
"""Versioned, JSON-serializable protocol. No browser or agent dependencies."""

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, HttpUrl, field_validator, model_validator

NonEmpty = Annotated[str, Field(min_length=1)]


# 协议模型的共同基类：拒绝未知字段，防止不受支持的能力被悄悄忽略。
class ProtocolModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


# 单个字段的提取规则：相对 CSS、文本/属性读取、URL 转换和必填标记。
class FieldSpec(ProtocolModel):
    # 列表字段相对当前记录匹配；详情字段相对整个详情文档匹配。
    selector: NonEmpty
    extract: Literal["text", "attribute", "texts", "count"] = "text"
    attribute: NonEmpty | None = None
    transform: Literal["none", "absolute_url"] = "none"
    required: bool = True

    # 约束属性名只在 extract='attribute' 时出现，避免规则含义不明确。
    @model_validator(mode="after")
    def check_attribute(self):
        if (self.extract == "attribute") != (self.attribute is not None):
            raise ValueError("attribute must be set exactly when extract='attribute'")
        if self.extract in {"texts", "count"} and self.transform != "none":
            raise ValueError("texts/count do not support transforms")
        return self


# 链接分页：唯一 Next 元素提供 href，执行器继续使用 URL 导航和循环检测。
class LinkPagination(ProtocolModel):
    type: Literal["link"]
    next_selector: NonEmpty


# 动态分页共用页面状态标志；加载标志消失只表示本轮加载结束，不代表已到末尾。
class DynamicPagination(ProtocolModel):
    loading_selector: NonEmpty | None = None
    end_selector: NonEmpty | None = None


# 点击分页覆盖追加和整批替换；mode 决定使用记录数还是字段快照确认推进。
class ClickPagination(DynamicPagination):
    type: Literal["click"]
    next_selector: NonEmpty
    mode: Literal["append", "replace"] = "append"


# 滚动分页默认作用于页面，也可指定唯一容器；动作预算与已处理批次预算相互独立。
class ScrollPagination(DynamicPagination):
    type: Literal["scroll"]
    container_selector: NonEmpty | None = None
    # 无新增的重试也消耗动作预算；仅到底后连续无新增才累计启发式结束阈值。
    max_scrolls: int = Field(default=100, ge=1, le=1000)
    no_change_limit: int = Field(default=3, ge=2, le=10)


# 显式 type 选择对应模型，拒绝未知类型和跨类型字段；旧 JSON 必须迁移，不静默兼容。
PaginationSpec = Annotated[
    LinkPagination | ClickPagination | ScrollPagination, Field(discriminator="type")
]


# 单层详情规则：从每条列表记录取得链接，再相对详情文档提取额外字段。
class DetailSpec(ProtocolModel):
    url_selector: NonEmpty
    fields: dict[NonEmpty, FieldSpec] = Field(min_length=1)
    # 详情导航尝试次数上限，失败也消耗；与列表页 max_pages 分开计算。
    max_pages: int = Field(default=100, ge=1, le=100_000)

    # 字段名不能只包含空白；长度限制本身无法排除这类无效名称。
    @field_validator("fields")
    @classmethod
    def check_names(cls, fields):
        if any(not name.strip() for name in fields):
            raise ValueError("field names must not be whitespace")
        return fields


# 一份可序列化、可重复执行的完整规则；v1 处理列表，v2 可额外声明一层详情。
class ScraperSpec(ProtocolModel):
    version: Literal["1", "2"] = "1"
    start_url: HttpUrl
    item_selector: NonEmpty
    fields: dict[NonEmpty, FieldSpec] = Field(min_length=1)
    detail: DetailSpec | None = None
    pagination: PaginationSpec | None = None
    # max_pages 统计已处理列表批次；滚动重试另受 max_scrolls 约束。
    max_items: int = Field(default=100, ge=1, le=1_000_000)
    max_pages: int = Field(default=100, ge=1, le=100_000)
    timeout_ms: int = Field(default=15_000, ge=1, le=120_000)
    # 未指定独立内容等待上限时，执行器回退到 timeout_ms。
    content_timeout_ms: int | None = Field(default=None, ge=1, le=120_000)
    # 空列表表示用所有字段的组合去重；指定字段必须必填，避免缺失值误合并记录。
    dedupe_by: list[NonEmpty] = Field(default_factory=list)
    # skip 保留其他成功记录并累计错误；fail 立即终止本次执行。
    on_item_error: Literal["skip", "fail"] = "skip"

    # 校验列表字段名，使用与详情字段相同的命名约束。
    @field_validator("fields")
    @classmethod
    def check_names(cls, fields):
        if any(not name.strip() for name in fields):
            raise ValueError("field names must not be whitespace")
        return fields

    # 按列表在前、详情在后的顺序合并字段，供去重、验证和导出共同使用。
    @property
    def all_fields(self) -> dict[str, FieldSpec]:
        return {**self.fields, **(self.detail.fields if self.detail else {})}

    # 统一检查版本、字段重名和去重键；去重键只能引用已声明且必填的字段。
    @model_validator(mode="after")
    def check_dedupe(self):
        if self.detail is not None:
            if self.version != "2":
                raise ValueError("detail requires protocol version '2'")
            if self.fields.keys() & self.detail.fields.keys():
                raise ValueError("Listing and detail field names must not overlap")
        if set(self.dedupe_by) - self.all_fields.keys():
            raise ValueError("dedupe_by must reference declared fields")
        if any(not self.all_fields[name].required for name in self.dedupe_by):
            raise ValueError("dedupe_by fields must be required")
        return self
