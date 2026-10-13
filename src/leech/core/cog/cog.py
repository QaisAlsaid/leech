from .context import CogContext


class Cog:
    def __init__(self, context: CogContext):
        self.context: CogContext = context
