"""Management configuration validation must not change the authentication SDK."""

import pytest

from okta_client.management.utils import authorization, extra_headers, organization_url


@pytest.mark.parametrize("url", ["https://example.okta.com", "https://login.example.com/"])
def test_organization_origins(url):
    assert organization_url(url) == url.rstrip("/")


@pytest.mark.parametrize(
    "url",
    [
        "http://example.okta.com",
        "https://",
        "https://token@example.okta.com",
        "https://example.okta.com/api/v1",
        "https://example.okta.com?token=secret",
        "https://example.okta.com/#fragment",
        " https://example.okta.com",
        "https://exam\nple.okta.com",
    ],
)
def test_unsafe_organization_urls_fail(url):
    with pytest.raises(ValueError, match="HTTPS Okta organization origin"):
        organization_url(url)


def test_authentication_modes_are_exclusive():
    with pytest.raises(ValueError, match="exactly one"):
        authorization(None, None)
    with pytest.raises(ValueError, match="exactly one"):
        authorization("api", "oauth")
    assert authorization("api", None) == "SSWS api"
    assert authorization(None, "oauth") == "Bearer oauth"


@pytest.mark.parametrize("token", ["", " ", "SSWS api", "Bearer oauth", "token\r\nX-Injected: value"])
def test_invalid_tokens_are_rejected(token):
    with pytest.raises(ValueError, match="raw token"):
        authorization(token, None)


def test_dynamic_access_token_is_evaluated_each_time():
    tokens = iter(["first", "second"])
    provider = authorization(None, lambda: next(tokens))
    assert callable(provider)
    assert provider() == "Bearer first"
    assert provider() == "Bearer second"
    bad_provider = authorization(None, lambda: "")
    assert callable(bad_provider)
    with pytest.raises(ValueError, match="raw token"):
        bad_provider()


def test_auth_header_override_is_rejected_case_insensitively():
    with pytest.raises(ValueError, match="Authorization header"):
        extra_headers({"aUtHoRiZaTiOn": "injected"})
    headers = {"X-Application": "example"}
    assert extra_headers(headers) == headers
    assert extra_headers(headers) is not headers
