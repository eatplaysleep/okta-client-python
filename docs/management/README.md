# Use the Okta Management API SDK

The Management API administers an Okta organization: users, groups,
applications, policies, and the other API groups in the supplied management
specification. It is separate from signing users in. The existing
[authentication SDK](../../README.md#usage-guide) remains available unchanged.

## Install

Python **3.10+** is required:

```sh
python -m pip install "okta-client-python[management]"
```

For an editable checkout, use `python -m pip install -e ".[management]"`.
The management extra installs HTTPX and Pydantic v2. You do **not** need Fern,
Node.js, Docker, or the generation toolchain to use the SDK.

## Credentials and permissions

Supply your organization's HTTPS origin, such as
`https://example.okta.com` or an HTTPS custom domain. Do not append `/api/v1`.
Provide **exactly one** of:

- `api_token`: a raw Okta API token. The SDK sends `Authorization: SSWS ...`.
- `access_token`: a raw OAuth access token. The SDK sends `Authorization: Bearer ...`.

Do not include the header prefix in a token. Keep credentials in a secret
manager or environment variables, never source control.

OAuth access tokens for Okta APIs must come from your **org authorization
server**, not a custom authorization server such as `/oauth2/default`.
Request the relevant `okta.*` scopes, obtain the required grants, and configure
appropriate administrative roles. A token or requested scope alone does not
grant permission to an operation. The management client presents credentials;
it does not acquire tokens or automatically refresh them.

## Synchronous quick start

Set `OKTA_ORG_URL` and `OKTA_API_TOKEN` in your environment:

```python
import os

from okta_client.management import ManagementClient

with ManagementClient(
    base_url=os.environ["OKTA_ORG_URL"],
    api_token=os.environ["OKTA_API_TOKEN"],
) as client:
    users = client.user.list_users(limit=10)
    for user in users:
        print(user.id, user.profile.login if user.profile else None)
```

Use a context manager or call `client.close()` to release the client's HTTP
connections. Methods are grouped by the specification's tags, normally using
singular names: `client.user`, `client.group`, `client.application`,
`client.system_log`, and the other generated API groups.

```python
user = client.user.get_user("00u-example")
groups = client.group.list_groups(limit=10)
applications = client.application.list_applications(limit=10)
events = client.system_log.list_log_events(limit=10)
```

These calls must run while the client is open. Use your own resource IDs.
Explore the generated methods with your IDE or `help(client.user.list_users)`;
parameters, return types, and API descriptions come from the specification.

## Asynchronous quick start

```python
import asyncio
import os

from okta_client.management import AsyncManagementClient


async def main():
    async with AsyncManagementClient(
        base_url=os.environ["OKTA_ORG_URL"],
        access_token=os.environ["OKTA_ACCESS_TOKEN"],
    ) as client:
        users = await client.user.list_users(limit=10)
        for user in users:
            print(user.id)


asyncio.run(main())
```

Async clients expose the same groups and operation arguments. Use
`async with` or `await client.aclose()`.

### Rotating tokens

Either credential can also be a synchronous callable returning the current
raw token. It is evaluated for each request, including calls through an
already-created API subclient:

```python
with ManagementClient(
    base_url=org_url,
    access_token=lambda: current_token.access_token,
) as client:
    user = client.user.get_user("00u-example")
```

Here `current_token` is a token you maintain, for example with the existing
authentication SDK. Refresh it before making management calls. Async token
acquisition should be awaited outside this synchronous callback; the async
management client accepts the same synchronous provider.

## Typed models and request bodies

Models are generated Pydantic classes. Python attributes use snake case;
wire JSON aliases retain the names in the specification.

```python
from okta_client.management.generated.types.user_profile import UserProfile

profile = UserProfile(
    login="dev@example.com",
    email="dev@example.com",
    first_name="Dev",
    last_name="Example",
)
created = client.user.create_user(profile=profile, activate=False)
print(created.id)
```

Creating a user changes your organization; run this only in an authorized
test organization. `activate=False` avoids immediate activation. Check the
operation's documentation before enabling activation or other lifecycle
actions.

Custom profile fields and additional response fields are retained. A custom
profile field must already exist in the organization's user profile schema:

```python
profile = UserProfile(login="dev@example.com", customDepartment="Engineering")
payload = profile.model_dump(by_alias=True, exclude_unset=True)
```

For JSON-ready wire data (including dates and discriminated models), use the
same encoder as the generated transport:

```python
from okta_client.management.generated.core.jsonable_encoder import jsonable_encoder

wire_payload = jsonable_encoder(profile)
```

With the pinned Fern serializer, Pydantic's `mode="json"` and
`model_dump_json()` do not consistently honor alias/exclusion options.
The SDK's HTTP request encoder does honor wire aliases.

Unset optional body arguments are omitted. Passing `None` explicitly to a
nullable body argument sends JSON `null`; consult its generated signature.
Not every property is present in every response, so check optional attributes
before using them.

## Pagination and response metadata

List operations return **one page**, not an automatic iterator. Parameters
such as `limit` and `after` are exposed directly. Obtain headers through the
operation's `with_raw_response` interface:

```python
from urllib.parse import parse_qs, urlsplit
import re

response = client.user.with_raw_response.list_users(limit=100)
print(response.status_code, response.headers.get("x-okta-request-id"))
users = response.data

match = re.search(r'<([^>]+)>;\s*rel="next"', response.headers.get("link", ""))
if match:
    cursor = parse_qs(urlsplit(match.group(1)).query).get("after", [None])[0]
    if cursor is None:
        raise ValueError("The next-page link did not contain an after cursor")
    next_page = client.user.list_users(limit=100, after=cursor)
```

Preserve your original filter/search arguments when requesting another page.
Use the server's cursor rather than deriving it from the last user's ID.
The example extracts a cursor and calls the same organization; it does not
forward credentials to an arbitrary URL from a response.

For async calls, await the corresponding raw operation. Its `.data`,
`.status_code`, and `.headers` have the same meaning.

## Errors, retries, and timeouts

HTTP failures raise `ApiError` or a generated subclass, with status, headers,
and the error body. They are not converted into empty lists or successful
results:

```python
from okta_client.management import ApiError

try:
    user = client.user.get_user("00u-example")
except ApiError as error:
    print(error.status_code)
    print(error.headers.get("x-okta-request-id"))
    raise
```

Malformed success responses raise the generated parsing error instead of
silently discarding fields. HTTPX transport errors propagate separately.
Do not log token-bearing request headers or sensitive error bodies.

The default timeout is **60 seconds**, and Fern retries retryable failures
up to **two times**. Configure defaults when constructing a client, or
override them per request:

```python
users = client.user.list_users(
    request_options={"timeout": 10.0, "max_retries": 0},
)
```

Rate-limit/retry response headers influence backoff. Consider whether retries
are appropriate for non-idempotent administrative operations; disable them
when your application's recovery policy requires it.

## Custom HTTPX transport

Inject an HTTPX client for proxies, TLS configuration, connection limits, or
offline testing. The injected transport remains **your responsibility**;
closing the management client does not close it:

```python
import httpx

with httpx.Client(timeout=15.0) as transport:
    with ManagementClient(
        base_url=org_url,
        api_token=api_token,
        httpx_client=transport,
        timeout=15.0,
    ) as client:
        users = client.user.list_users(limit=10)
```

Use `httpx.AsyncClient` with the asynchronous facade. SDK-owned transports do
not follow redirects. Set any custom transport redirect policy deliberately.
Additional non-authentication headers can be supplied with `headers=...`;
provide credentials through the explicit token arguments.

## Coverage and updates

This iteration ships **850 management operations** from the supplied Okta
management source. It excludes OIN submissions, following the upstream
`management-sdk.yaml` bundle scope. It does not generate the Authentication
API or replace the existing OAuth flows.

Generated clients and models are shipped with the package. Updating API
coverage is a contributor task, not an application startup step. See the
[contributor README](../contributing/README.md) for replacing the OAS3 input,
generation, provenance, and tests.
