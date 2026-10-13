from leech.core.error import BaseError


class ParseError(BaseError):
    pass


class LexError(BaseError):
    pass


class TriggeredLexError(BaseError):
    pass
