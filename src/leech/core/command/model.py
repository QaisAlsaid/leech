from pydantic import BaseModel, Field, field_validator
from leech.core.parser.types.definition import Types
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from leech.core.provider.model import ProviderDescriptor


class ArgumentDescriptor(BaseModel):
    name: str
    description: str = ""
    type: Types
    required: bool = True
    usage_example: str = ""
    help_content: str = ""

    @field_validator("name")
    @classmethod
    def validate_identifier(cls, v: str) -> str:
        if not v.isidentifier():
            raise ValueError(f"'{v}' is not a valid identifier.")
        return v


class CommandDescriptor(BaseModel):
    name: str
    description: str = ""
    arguments: list[ArgumentDescriptor] = Field(default_factory=list)
    subcommands: dict[str, "CommandDescriptor"] = Field(default_factory=dict)
    provider: "ProviderDescriptor"
    defaults: dict[str, Any] = Field(default_factory=dict)
    usage_example: str = ""
    help_content: str = ""
    is_root: bool = False

    @field_validator("name")
    @classmethod
    def validate_identifier(cls, v: str) -> str:
        if not v.isidentifier():
            raise ValueError(f"'{v}' is not a valid identifier.")
        return v


from leech.core.provider.model import ProviderDescriptor

ProviderDescriptor.model_rebuild(
    _types_namespace={
        "ArgumentDescriptor": ArgumentDescriptor,
    }
)

CommandDescriptor.model_rebuild(
    _types_namespace={
        "ProviderDescriptor": ProviderDescriptor,
        "ArgumentDescriptor": ArgumentDescriptor,
    }
)
