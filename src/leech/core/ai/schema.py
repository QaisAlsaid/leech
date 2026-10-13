from __future__ import annotations

import json
from typing import Any, Callable

from pydantic import BaseModel, Field, model_validator

from leech.core.command.runtime import Command
from leech.core.parser.token import Token, TokenType


class CatalogArgument(BaseModel):
    name: str
    type: str
    required: bool
    description: str = ""


class CatalogEntry(BaseModel):
    command: str # dotted path: "todo.add"
    description: str
    arguments: list[CatalogArgument] = Field(default_factory=list)


class AiAction(BaseModel):
    """the only shapes the model is allowed to answer with."""
    command: str | None = None
    arguments: dict[str, Any] = Field(default_factory=dict)
    reply: str | None = None

    @model_validator(mode="after")
    def exactly_one(self) -> "AiAction":
        if self.command is None and self.reply is None:
            raise ValueError("set either 'command' or 'reply'.")
        return self


Visibility = Callable[[str, Command], bool]


def build_catalog(registry, visible: Visibility) -> list[CatalogEntry]:
    return [
        CatalogEntry(
            command=path,
            description=command.description,
            arguments=[
                CatalogArgument(
                    name=a.name, type=a.type.type.name,
                    required=a.required, description=a.description,
                )
                for a in command.arguments
            ],
        )
        for path, command in registry.registered_commands.items()
        if visible(path, command)
    ]


def action_tokens(action: AiAction) -> list[Token]:
    tokens: list[Token] = []
    for name, value in action.arguments.items():
        tokens.append(Token(TokenType.RAW, name))
        tokens.append(Token(TokenType.ASSIGN, "="))
        tokens.append(Token(TokenType.STR, str(value)))
    return tokens


def strip_fences(text: str) -> str:
    text = text.strip()
    if text.startswith("```"):
        text = text.strip("`")
        if text[:4].lower() == "json":
            text = text[4:]
    return text.strip()


def system_prompt(persona: str, catalog: list[CatalogEntry]) -> str:
    return (
        f"{persona}\n\n"
        "you respond with exactly ONE json object and nothing else:\n"
        '- chat: {"reply": "text"}\n'
        '- run a command: {"command": "todo.add", '
        '"arguments": {"task": "buy milk"}, "reply": "optional confirmation"}\n'
        "rules:\n"
        "- 'command' is an exact dotted path from the catalog; argument names are exact.\n"
        "- argument values are strings.\n"
        "- never invent commands or argument names; if nothing fits, just reply.\n"
        "- never claim you did something unless you emitted its command.\n\n"
        "catalog:\n"
        + json.dumps([e.model_dump() for e in catalog], separators=(",", ":"))
    )
