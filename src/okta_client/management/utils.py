"""Credential and organization URL validation shared by both management clients."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from urllib.parse import urlsplit

Credential = str | Callable[[], str]


def organization_url(base_url: str) -> str:
    """Require an HTTPS organization origin, never a credential-bearing URL."""
    parsed = urlsplit(base_url)
    if (
        any(character.isspace() for character in base_url)
        or parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path not in {"", "/"}
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError("base_url must be an HTTPS Okta organization origin, without credentials, a path, query, or fragment")
    return base_url.rstrip("/")


def _raw_token(value: str) -> str:
    if not isinstance(value, str) or not value or any(character.isspace() for character in value):
        raise ValueError("Credentials must be non-empty raw token strings without whitespace or an authorization prefix")
    return value


def authorization(api_token: Credential | None, access_token: Credential | None) -> Credential:
    """Format SSWS or Bearer credentials, evaluating providers for each request."""
    if (api_token is None) == (access_token is None):
        raise ValueError("Provide exactly one of api_token or access_token")
    credential = api_token if api_token is not None else access_token
    prefix = "SSWS" if api_token is not None else "Bearer"
    if callable(credential):
        return lambda: f"{prefix} {_raw_token(credential())}"
    if not isinstance(credential, str):
        raise ValueError("Credentials must be a raw token string or a callable returning one")
    return f"{prefix} {_raw_token(credential)}"


def extra_headers(headers: Mapping[str, str] | None) -> dict[str, str]:
    """Prevent competing auth headers from overriding the selected credentials."""
    result = dict(headers or {})
    if any(name.lower() == "authorization" for name in result):
        raise ValueError("Use api_token or access_token instead of an Authorization header")
    return result
