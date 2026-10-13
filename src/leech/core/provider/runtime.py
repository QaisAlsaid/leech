from typing import TYPE_CHECKING

from .model import ProviderType

if TYPE_CHECKING:
    from leech.core.command.runtime import Argument

from .model import ProviderType


class Provider:
    def __init__(
        self,
        type: ProviderType,
        target: list[str],
        arguments: list["Argument"] | None = None,
        bot: int | None = None,
    ):
        self.type: ProviderType = type
        self.target: list[str] = target
        self.arguments: list["Argument"] | None = arguments
        self.bot: int | None = bot
