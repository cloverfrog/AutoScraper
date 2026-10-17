"""Versioned, JSON-serializable protocol. No browser or agent dependencies."""

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, HttpUrl, field_validator, model_validator

NonEmpty = Annotated[str, Field(min_length=1)]


class ProtocolModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class FieldSpec(ProtocolModel):
    selector: NonEmpty
    extract: Literal["text", "attribute"] = "text"
    attribute: NonEmpty | None = None
    transform: Literal["none", "absolute_url"] = "none"
    required: bool = True

    @model_validator(mode="after")
    def check_attribute(self):
        if (self.extract == "attribute") != (self.attribute is not None):
            raise ValueError("attribute must be set exactly when extract='attribute'")
        return self


class PaginationSpec(ProtocolModel):
    next_selector: NonEmpty


class ScraperSpec(ProtocolModel):
    version: Literal["1"] = "1"
    start_url: HttpUrl
    item_selector: NonEmpty
    fields: dict[NonEmpty, FieldSpec] = Field(min_length=1)
    pagination: PaginationSpec | None = None
    max_items: int = Field(default=100, ge=1, le=1_000_000)
    max_pages: int = Field(default=100, ge=1, le=100_000)
    timeout_ms: int = Field(default=15_000, ge=1, le=120_000)
    dedupe_by: list[NonEmpty] = Field(default_factory=list)
    on_item_error: Literal["skip", "fail"] = "skip"

    @field_validator("fields")
    @classmethod
    def check_names(cls, fields):
        if any(not name.strip() for name in fields):
            raise ValueError("field names must not be whitespace")
        return fields

    @model_validator(mode="after")
    def check_dedupe(self):
        if set(self.dedupe_by) - self.fields.keys():
            raise ValueError("dedupe_by must reference declared fields")
        if any(not self.fields[name].required for name in self.dedupe_by):
            raise ValueError("dedupe_by fields must be required")
        return self
