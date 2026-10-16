from dataclasses import dataclass

from pydantic import BaseModel


class Grant(BaseModel):
    actor: str  # "user:<id>"
    scope: str  # "command:todo.*", "command:*", "message:*", "*"
    allow: bool = True


@dataclass(frozen=True)
class Decision:
    allowed: bool
    reason: str
