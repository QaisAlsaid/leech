from abc import ABC, abstractmethod

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .invocation import Invocation


class Binding(ABC):
    @abstractmethod
    async def invoke(self, invocation: "Invocation") -> "Invocation | None":
        pass
