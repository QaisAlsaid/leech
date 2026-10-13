from leech.core.error import BaseError


class TypesError(BaseError):
    def __init__(self, message: str):
        super().__init__(message=message, is_user_error=False)
