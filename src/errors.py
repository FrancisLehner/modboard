"""Exception types shared across the package."""

from __future__ import annotations


class MoodleAuthError(RuntimeError):
    """Recoverable failure in the login or token handshake."""


class MoodleApiError(RuntimeError):
    """Moodle web-service error, returned by the API with HTTP 200."""

    def __init__(
        self,
        message: str,
        *,
        errorcode: str | None = None,
        exception: str | None = None,
    ) -> None:
        """Store the Moodle ``errorcode`` and ``exception`` alongside the message."""
        super().__init__(message)
        self.errorcode = errorcode
        self.exception = exception

    @property
    def is_invalid_token(self) -> bool:
        """True when a fresh login is the right remedy."""
        return self.errorcode in {"invalidtoken", "accessexception"}
