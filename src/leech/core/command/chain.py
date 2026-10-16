from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Awaitable, Callable, Optional

if TYPE_CHECKING:
    from leech.core.helpers.interactions.model import HarvestedMessage


@dataclass
class Execution:
    kind: str  # "slash" | "prefix"
    note: str
    settle: Callable[..., Awaitable[Optional["HarvestedMessage"]]]


@dataclass
class Chain:
    out: list[Any] = field(default_factory=list)
    executions: list[Execution] = field(default_factory=list)
