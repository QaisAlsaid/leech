from leech.core.error import BaseError


class CogError(BaseError):
    def __init__(self, message: str, is_user_error: bool = False):
        super().__init__(message=message, is_user_error=is_user_error)
