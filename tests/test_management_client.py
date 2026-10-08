"""Exercise shipped generated sync/async APIs without contacting an Okta org."""

import asyncio
import importlib
import json
import pkgutil
from pathlib import Path

import httpx
import pytest
from pydantic import TypeAdapter, ValidationError
from scripts.generate_management import validate_generated_operations

from okta_client.management import ApiError, AsyncManagementClient, ManagementClient, generated
from okta_client.management.generated.core.jsonable_encoder import jsonable_encoder
from okta_client.management.generated.types.authenticator_base import AuthenticatorBase
from okta_client.management.generated.types.authenticator_method_base import AuthenticatorMethodBase
from okta_client.management.generated.types.user import User
from okta_client.management.generated.types.user_factor_push_transaction import UserFactorPushTransaction
from okta_client.management.generated.types.user_profile import UserProfile
from okta_client.management.generated.types.user_risk_get_response import UserRiskGetResponse
from okta_client.management.generated.types.user_type import UserType

USER = {
    "id": "00u-example",
    "status": "ACTIVE",
    "profile": {"login": "dev@example.com", "firstName": "Dev", "customDepartment": "SDK"},
    "type": {"id": "oty-example"},
    "_links": {"self": {"href": "https://example.okta.com/api/v1/users/00u-example"}},
    "futureField": "preserved",
}


def test_sync_ssws_query_parameters_and_typed_response():
    requests = []

    def respond(request):
        requests.append(request)
        return httpx.Response(200, json=[USER])

    with httpx.Client(transport=httpx.MockTransport(respond)) as transport:
        with ManagementClient(
            base_url="https://example.okta.com/", api_token="test-api-token", httpx_client=transport
        ) as client:
            users = client.user.list_users(limit=5, search='profile.login eq "dev@example.com"', sort_by="profile.lastName")
        assert not transport.is_closed
    assert isinstance(users[0], User)
    assert users[0].profile.first_name == "Dev"
    assert users[0].profile.model_extra["customDepartment"] == "SDK"
    assert users[0].model_extra["futureField"] == "preserved"
    request = requests[0]
    assert request.method == "GET"
    assert request.url.path == "/api/v1/users"
    assert request.url.params["limit"] == "5"
    assert request.url.params["sortBy"] == "profile.lastName"
    assert request.url.params["search"] == 'profile.login eq "dev@example.com"'
    assert request.headers["Authorization"] == "SSWS test-api-token"
    assert request.headers["User-Agent"].startswith("okta-management-python/")
    assert not any(name.startswith("x-fern") for name in request.headers)


def test_create_user_serializes_aliases_extras_and_omitted_fields():
    requests = []

    def respond(request):
        requests.append(request)
        return httpx.Response(200, json=USER)

    profile = UserProfile(login="dev@example.com", email="dev@example.com", first_name="Dev", customDepartment="SDK")
    with httpx.Client(transport=httpx.MockTransport(respond)) as transport:
        client = ManagementClient(base_url="https://example.okta.com", api_token="token", httpx_client=transport)
        result = client.user.create_user(profile=profile, activate=False, group_ids=["00g-example"])
    assert result.id == "00u-example"
    request = requests[0]
    assert request.method == "POST"
    assert request.url.params["activate"] == "false"
    body = json.loads(request.content)
    assert body["profile"]["firstName"] == "Dev"
    assert body["profile"]["customDepartment"] == "SDK"
    assert body["groupIds"] == ["00g-example"]
    assert "credentials" not in body
    assert "realmId" not in body
    assert "first_name" not in body["profile"]


def test_path_identifiers_are_encoded():
    requests = []

    def respond(request):
        requests.append(request)
        return httpx.Response(200, json=USER)

    with httpx.Client(transport=httpx.MockTransport(respond)) as transport:
        client = ManagementClient(base_url="https://example.okta.com", access_token="oauth-token", httpx_client=transport)
        client.user.get_user("name/+?x")
    assert requests[0].url.raw_path == b"/api/v1/users/name%2F%2B%3Fx"
    assert requests[0].headers["Authorization"] == "Bearer oauth-token"


def test_provider_token_rotation_is_applied_to_existing_subclients():
    requests = []
    tokens = iter(["first", "second"])

    def respond(request):
        requests.append(request)
        return httpx.Response(200, json=USER)

    with httpx.Client(transport=httpx.MockTransport(respond)) as transport:
        client = ManagementClient(
            base_url="https://example.okta.com",
            access_token=lambda: next(tokens),
            httpx_client=transport,
        )
        users = client.user
        users.get_user("00u-example")
        users.get_user("00u-example")
    assert [request.headers["Authorization"] for request in requests] == ["Bearer first", "Bearer second"]


def test_invalid_provider_token_fails_before_request():
    requests = []
    with httpx.Client(transport=httpx.MockTransport(lambda request: requests.append(request))) as transport:
        client = ManagementClient(base_url="https://example.okta.com", access_token=lambda: "", httpx_client=transport)
        with pytest.raises(ValueError, match="raw token"):
            client.user.list_users()
    assert not requests


def test_raw_response_exposes_pagination_and_request_id_headers():
    headers = {
        "Link": '<https://example.okta.com/api/v1/users?after=next-cursor>; rel="next"',
        "X-Okta-Request-Id": "request-123",
        "X-Rate-Limit-Remaining": "99",
    }
    with httpx.Client(
        transport=httpx.MockTransport(lambda request: httpx.Response(200, headers=headers, json=[USER]))
    ) as transport:
        client = ManagementClient(base_url="https://example.okta.com", api_token="token", httpx_client=transport)
        response = client.user.with_raw_response.list_users(limit=1)
    assert response.status_code == 200
    assert response.data[0].id == "00u-example"
    assert response.headers["link"] == headers["Link"]
    assert response.headers["x-okta-request-id"] == "request-123"


@pytest.mark.parametrize("status", [401, 403, 404, 429, 500])
def test_http_errors_are_not_success_shaped(status):
    body = {"errorCode": "E0000006", "errorSummary": "Access denied", "errorId": "error-123", "errorCauses": []}
    with httpx.Client(
        transport=httpx.MockTransport(
            lambda request: httpx.Response(status, headers={"X-Okta-Request-Id": "request-123"}, json=body)
        )
    ) as transport:
        client = ManagementClient(
            base_url="https://example.okta.com", api_token="token", max_retries=0, httpx_client=transport
        )
        with pytest.raises(ApiError) as raised:
            client.user.get_user("00u-example")
    assert raised.value.status_code == status
    assert raised.value.headers["x-okta-request-id"] == "request-123"
    assert raised.value.body is not None


def test_retry_policy_can_be_disabled_per_request(monkeypatch):
    requests = []

    def respond(request):
        requests.append(request)
        return httpx.Response(429, headers={"Retry-After": "0"}, json={"errorCode": "E0000047"})

    monkeypatch.setattr("okta_client.management.generated.core.http_client.time.sleep", lambda delay: None)
    with httpx.Client(transport=httpx.MockTransport(respond)) as transport:
        client = ManagementClient(
            base_url="https://example.okta.com", api_token="token", max_retries=1, httpx_client=transport
        )
        with pytest.raises(ApiError):
            client.user.list_users()
        assert len(requests) == 2
        requests.clear()
        with pytest.raises(ApiError):
            client.user.list_users(request_options={"max_retries": 0})
        assert len(requests) == 1


def test_sdk_owned_sync_transport_is_closed():
    client = ManagementClient(base_url="https://example.okta.com", api_token="token")
    assert not client._transport.is_closed
    with client:
        assert not client._transport.is_closed
    assert client._transport.is_closed


def test_async_requests_and_injected_transport_lifetime():
    requests = []

    def respond(request):
        requests.append(request)
        return httpx.Response(200, json=[USER])

    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as transport:
            async with AsyncManagementClient(
                base_url="https://example.okta.com",
                access_token=lambda: "current-oauth-token",
                httpx_client=transport,
            ) as client:
                users = await client.user.list_users(after="cursor")
                assert users[0].id == "00u-example"
            assert not transport.is_closed
        assert transport.is_closed

    asyncio.run(run())
    assert requests[0].headers["Authorization"] == "Bearer current-oauth-token"
    assert requests[0].url.params["after"] == "cursor"


def test_sdk_owned_async_transport_is_closed():
    async def run():
        client = AsyncManagementClient(base_url="https://example.okta.com", api_token="token")
        async with client:
            assert not client._transport.is_closed
        assert client._transport.is_closed

    asyncio.run(run())


def test_generated_wire_contract_covers_the_entire_specification():
    root = Path(__file__).parents[1]
    document = json.loads((root / "fern/openapi/management.json").read_bytes())
    validate_generated_operations(root / "src/okta_client/management/generated", document)


def test_every_generated_module_imports():
    for module in pkgutil.walk_packages(generated.__path__, prefix=generated.__name__ + "."):
        importlib.import_module(module.name)


@pytest.mark.parametrize(
    ("model", "payload"),
    [
        (AuthenticatorBase, {"key": "okta_password", "id": "aut-example", "created": "2026-10-07T12:00:00Z"}),
        (AuthenticatorMethodBase, {"type": "email", "status": "ACTIVE"}),
        (UserFactorPushTransaction, {"factorResult": "SUCCESS"}),
        (UserRiskGetResponse, {"riskLevel": "NONE"}),
    ],
)
def test_discriminated_models_preserve_flat_wire_shapes(model, payload):
    result = TypeAdapter(model).validate_python(payload)
    serialized = jsonable_encoder(result)
    assert "value" not in serialized
    assert payload.keys() <= serialized.keys()


def test_discriminator_and_full_user_type_validation_are_not_relaxed():
    with pytest.raises(ValidationError):
        TypeAdapter(AuthenticatorBase).validate_python({"key": "not-a-known-authenticator"})
    with pytest.raises(ValidationError):
        TypeAdapter(AuthenticatorBase).validate_python({"key": "okta_password", "created": "invalid-date"})
    with pytest.raises(ValidationError):
        UserType(id="oty-example")
    assert User.model_validate(USER).type.id == "oty-example"
