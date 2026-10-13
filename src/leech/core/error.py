class BaseError(Exception):
    def __init__(self, message: str, is_user_error: bool = True):
        self.is_user_error = is_user_error
        self.message = message
        super().__init__(
            f"{'user error' if self.is_user_error else 'error'}: {message}"
        )
