"""Shared error types."""


class XearchError(Exception):
    """Base error for Xearch."""


class InvalidInput(XearchError, ValueError):
    """The caller supplied something Xearch cannot use."""


class TweetUnavailable(XearchError):
    """The tweet is deleted, protected, age-gated, or does not exist."""


class UpstreamError(XearchError):
    """An upstream endpoint failed."""

    def __init__(self, message: str, *, status: int | None = None, url: str | None = None) -> None:
        super().__init__(message)
        self.status = status
        self.url = url


class SearchError(XearchError):
    """Search failed."""


class SearchBlocked(SearchError):
    """The search backend refused the request or served a challenge."""


class SearchConfigError(SearchError):
    """The search backend is misconfigured (for example, a bad API key)."""


class ProfileUnavailable(XearchError):
    """The profile does not exist or could not be read."""
