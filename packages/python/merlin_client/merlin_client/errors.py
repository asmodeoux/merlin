"""Exception hierarchy for the Merlin HTTP client."""

from __future__ import annotations


class MerlinClientError(Exception):
    """Base class for every error this SDK raises."""


class MerlinHTTPError(MerlinClientError):
    """Non-2xx response from the API."""

    def __init__(self, status_code: int, message: str, *, detail: object | None = None) -> None:
        """Capture the HTTP status code, a message, and optional structured detail payload."""
        super().__init__(f"{status_code}: {message}")
        self.status_code = status_code
        self.message = message
        self.detail = detail


class MerlinTimeoutError(MerlinClientError):
    """Request timed out before the API responded."""
