"""Small, hand-written management facade over Fern's typed clients."""

from __future__ import annotations

from collections.abc import Callable, Mapping

import httpx

from okta_client import __version__

from .generated.client import AsyncOktaManagementClient, OktaManagementClient
from .generated.core.client_wrapper import AsyncClientWrapper, SyncClientWrapper
from .utils import Credential, authorization, extra_headers, organization_url


def _headers(headers: Mapping[str, str] | None) -> dict[str, str]:
    result = extra_headers(headers)
    if not any(name.lower() == "user-agent" for name in result):
        result["User-Agent"] = f"okta-management-python/{__version__}"
    return result


class _ProviderSyncWrapper(SyncClientWrapper):
    def __init__(
        self,
        *,
        provider: Callable[[], str],
        base_url: str,
        headers: dict[str, str],
        timeout: float,
        max_retries: int,
        httpx_client: httpx.Client,
    ) -> None:
        self._provider = provider
        super().__init__(
            api_key="",
            base_url=base_url,
            headers=headers,
            timeout=timeout,
            max_retries=max_retries,
            httpx_client=httpx_client,
        )

    def get_headers(self) -> dict[str, str]:
        return {**super().get_headers(), "Authorization": self._provider()}


class _ProviderAsyncWrapper(AsyncClientWrapper):
    def __init__(
        self,
        *,
        provider: Callable[[], str],
        base_url: str,
        headers: dict[str, str],
        timeout: float,
        max_retries: int,
        httpx_client: httpx.AsyncClient,
    ) -> None:
        self._provider = provider
        super().__init__(
            api_key="",
            base_url=base_url,
            headers=headers,
            timeout=timeout,
            max_retries=max_retries,
            httpx_client=httpx_client,
        )

    def get_headers(self) -> dict[str, str]:
        return {**super().get_headers(), "Authorization": self._provider()}


class ManagementClient(OktaManagementClient):
    """Synchronous Okta Management API client.

    Supply exactly one raw ``api_token`` (SSWS) or ``access_token`` (Bearer).
    Either credential can be a synchronous callable returning the current token.
    ``base_url`` must be the HTTPS origin of your Okta organization.

    Own transports are closed by ``close()`` or a context manager. An injected
    HTTPX client remains owned by the caller. Request-specific options and all
    API groups/models come directly from the generated client.
    """

    def __init__(
        self,
        *,
        base_url: str,
        api_token: Credential | None = None,
        access_token: Credential | None = None,
        headers: Mapping[str, str] | None = None,
        timeout: float = 60.0,
        max_retries: int = 2,
        httpx_client: httpx.Client | None = None,
    ) -> None:
        url = organization_url(base_url)
        credential = authorization(api_token, access_token)
        request_headers = _headers(headers)
        self._owns_transport = httpx_client is None
        self._transport = httpx_client if httpx_client is not None else httpx.Client(timeout=timeout, follow_redirects=False)
        super().__init__(
            base_url=url,
            api_key=credential if isinstance(credential, str) else "",
            headers=request_headers,
            timeout=timeout,
            max_retries=max_retries,
            follow_redirects=False,
            httpx_client=self._transport,
        )
        if callable(credential):
            # Fern's apiKey parameter is static; use a typed wrapper extension
            # for providers without editing generated code or caller transports.
            self._client_wrapper = _ProviderSyncWrapper(
                provider=credential,
                base_url=url,
                headers=request_headers,
                timeout=timeout,
                max_retries=max_retries,
                httpx_client=self._transport,
            )

    def close(self) -> None:
        """Release an SDK-owned transport; injected transports are not closed."""
        if self._owns_transport:
            self._transport.close()

    def __enter__(self) -> ManagementClient:
        return self

    def __exit__(self, exc_type, exc_value, traceback) -> None:
        self.close()


class AsyncManagementClient(AsyncOktaManagementClient):
    """Asynchronous counterpart of :class:`ManagementClient`.

    Use ``async with`` or ``await client.aclose()`` for an SDK-owned transport.
    Token providers are synchronous; asynchronously refresh tokens before use.
    """

    def __init__(
        self,
        *,
        base_url: str,
        api_token: Credential | None = None,
        access_token: Credential | None = None,
        headers: Mapping[str, str] | None = None,
        timeout: float = 60.0,
        max_retries: int = 2,
        httpx_client: httpx.AsyncClient | None = None,
    ) -> None:
        url = organization_url(base_url)
        credential = authorization(api_token, access_token)
        request_headers = _headers(headers)
        self._owns_transport = httpx_client is None
        self._transport = (
            httpx_client if httpx_client is not None else httpx.AsyncClient(timeout=timeout, follow_redirects=False)
        )
        super().__init__(
            base_url=url,
            api_key=credential if isinstance(credential, str) else "",
            headers=request_headers,
            timeout=timeout,
            max_retries=max_retries,
            follow_redirects=False,
            httpx_client=self._transport,
        )
        if callable(credential):
            self._client_wrapper = _ProviderAsyncWrapper(
                provider=credential,
                base_url=url,
                headers=request_headers,
                timeout=timeout,
                max_retries=max_retries,
                httpx_client=self._transport,
            )

    async def aclose(self) -> None:
        """Release an SDK-owned transport; injected transports are not closed."""
        if self._owns_transport:
            await self._transport.aclose()

    async def __aenter__(self) -> AsyncManagementClient:
        return self

    async def __aexit__(self, exc_type, exc_value, traceback) -> None:
        await self.aclose()
