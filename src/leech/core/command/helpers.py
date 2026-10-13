from .binding import Binding
from .native.binding import NativeBinding
from .self.binding import SelfBinding
from .slash.binding import SlashBinding


def is_invocable(binding: Binding) -> bool:
    if isinstance(binding, NativeBinding):
        return binding.handler is not None

    if isinstance(binding, SelfBinding):
        return is_invocable(binding.target.binding)

    if isinstance(binding, SlashBinding):
        return binding.bot is not None

    return False
