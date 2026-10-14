from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class Chain:
    out: list[Any] = field(default_factory=list)
