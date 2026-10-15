"""Transport-independent limits and public remote command errors."""
DEFAULT_PORT = 8765
MAX_UPLOAD = 10 * 1024 * 1024
MAX_REQUEST = 15 * 1024 * 1024


class RemoteError(Exception):
    def __init__(self, message, status=400):
        super().__init__(message)
        self.status = status
