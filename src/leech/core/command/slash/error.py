from leech.core.error import BaseError


class SlashError(BaseError):
    def __init__(self, message: str, is_user_error: bool = True):
        super().__init__(message=message, is_user_error=is_user_error)
