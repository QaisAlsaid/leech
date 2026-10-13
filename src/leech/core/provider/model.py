from enum import Enum

from pydantic import BaseModel, Field, field_validator

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from leech.core.command.model import ArgumentDescriptor


class ProviderType(str, Enum):
    SLASH = "slash"
    PREFIX = "prefix"
    SELF = "self"
    NATIVE = "native"


class ProviderDescriptor(BaseModel):
    type: ProviderType
    target: list[str] = Field(default_factory=list)
    bot: int | None = None
    arguments: list["ArgumentDescriptor"] = Field(default_factory=list)

    @field_validator("target")
    @classmethod
    def validate_target(cls, v: list[str]) -> list[str]:
        if not v:
            raise ValueError("provider target can't be empty.")
        return v
