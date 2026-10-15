# Developer contributor guide

This guide covers working on the SDK, not contribution policy. Follow the
[contribution process and CLA requirements](../../.github/CONTRIBUTING.md)
before submitting changes.

## Local setup

Use Python **3.10 or newer**. From the repository root:

```sh
python -m venv .venv
source .venv/bin/activate
python -m pip install -e ".[dev,codegen]"
```

On Windows, activate with `.venv\Scripts\Activate.ps1`. The existing `make deps`
target also installs development dependencies.

Management runtime dependencies are optional:

```sh
python -m pip install -e ".[management]"
```

Keep a base install working without the management extra. HTTPX/Pydantic
dependencies must not become mandatory for existing authentication users.
Dependency groups and package discovery are defined in
[pyproject.toml](../../pyproject.toml).

SDK consumers need only Python and the appropriate package extras, not a
generation toolchain. Contributors regenerating the SDK need **Node.js 22 or
newer**, npm, git, uv, and pnpm. Run `npm ci` at the repository root to install
the pinned **Fern CLI 5.147.8** from
[`package.json`](../../package.json) and
[`package-lock.json`](../../package-lock.json); do not use a global CLI.

The default generation runtime is **native**. Perform its one-time setup:

```sh
.venv/bin/python -m scripts.native_fern --setup
```

[`scripts/native_fern.py`](../../scripts/native_fern.py) downloads the stock
public Fern **5.26.0** source at commit
`55cd3e8a1e688fda195ed855977b35b05e80894d` into the ignored
`node_modules/.cache` tree. It provisions a managed Python **3.13.12** generator
environment, upstream Poetry/pnpm locked dependencies, and the stock
Python-v2 companion. This generator Python version does not change the SDK's
Python 3.10 minimum. The native portability adapter runs the pinned generator;
export and generation block network access after setup.

Alternatively, use the Docker runtime with an **authorized, running Docker**
installation. Follow your organization's Docker policy; do not change or
bypass it. Both runtimes generate locally without uploading specifications.

## Architecture and compatibility

- [`authfoundation`](../../src/okta_client/authfoundation/) provides shared
  authentication, configuration, token, and networking foundations.
- [`oauth2auth`](../../src/okta_client/oauth2auth/) implements OAuth flows.
  These existing APIs remain unchanged by the additive management SDK.
- The management SDK uses Fern, following the generated-client approach of
  the Auth0 SDK: synchronous and asynchronous HTTPX clients with Pydantic
  models, isolated under
  [`src/okta_client/management/generated/`](../../src/okta_client/management/generated/).
- Hand-written integration belongs in
  [`src/okta_client/management/client.py`](../../src/okta_client/management/client.py)
  and other custom management modules, not in the generated tree.

Use the facade's `ManagementClient` and `AsyncManagementClient` for integration.
The raw generated roots are `OktaManagementClient` and
`AsyncOktaManagementClient`; tagged API subclients include singular `.user`,
`.group`, `.application`, and `.system_log`. Import generated models from
`okta_client.management.generated.types`. See the
[management usage guide](../management/README.md) for consumer examples.

Authentication obtains and presents credentials. OAuth scopes describe
authorization requested or granted; they are not authentication flows and do
not themselves grant administrative access. Management calls perform Okta
administrative operations, subject to the server's scopes, roles, and policy.
Reuse authentication foundations through the facade where appropriate without
changing existing token or OAuth behavior. Keep sync and async behavior
consistent, including client lifetime and error handling.

Preserve existing imports, configuration, tests, and base-install behavior.
Do not eagerly import optional management dependencies from base SDK modules.

## Specification input

[`scripts.prepare_management_spec`](../../scripts/prepare_management_spec.py)
accepts:

- A supplied local Okta source checkout root containing
  `specs/monolith/management/spec/base.yaml` and its `v1` fragments.
- The corresponding management specification directory.
- A bundled OpenAPI **3.0** YAML or JSON file.

For example:

```sh
python -m scripts.prepare_management_spec /path/to/okta-source
```

The default output is the deterministic, canonical
[`fern/openapi/management.json`](../../fern/openapi/management.json).
Preparation merges only management definitions, excluding submissions to
match the upstream management SDK scope, including the Go SDK. It rejects
duplicate definitions, missing or duplicate `operationId` values, and
unresolved or nonlocal references. Bundle external references before supplying
a file. Do not suppress validation failures or silently discard operations.
Preparation also preserves fragment-level global security and additional
security schemes. Repeated OAuth scope descriptions retain the base
description with a visible warning.
The initial generated inventory contains **850 operations**; review changes
against the input rather than treating that count as a permanent API limit.

## Regeneration and review

Fern configuration lives under [`fern/`](../../fern/); the local npm toolchain
is locked, and the Python generator is pinned to **5.26.0**.
Change pins deliberately and review the resulting diff.

From the repository root, with the virtual environment activated:

```sh
make generate-management SPEC=/path/to/spec
make check-management
```

Both targets default to native execution. For authorized Docker execution,
append `GENERATOR_RUNTIME=docker`. If `make` is unavailable, use:

```sh
.venv/bin/python -m scripts.generate_management --spec /path/to/spec --runtime native
.venv/bin/python -m scripts.generate_management --runtime native --check
```

The check defaults to the committed canonical input; pass `SPEC=...` to the
Make target or `--spec ...` to the script to check another source.
[`scripts.generate_management`](../../scripts/generate_management.py) stages
configuration and the adapted OpenAPI input, runs `fern write-definition`,
creates an ephemeral `fern/definition`, removes only declaration example
metadata, and runs `fern check --local --warnings` before generation.
Generated Python is checked for Python 3.10 syntax, every operation's HTTP
verb and route is matched to the input, and every generated module is imported
in an isolated staging package. Only successful, validated generation
replaces the managed subtree; generated packaging never overwrites
`pyproject.toml` or custom modules. The check regenerates in staging and
compares the package, canonical input, and provenance without changing
tracked files.

The raw canonical source, including examples, remains in
[`fern/openapi/management.json`](../../fern/openapi/management.json).
[`_generation.json`](../../src/okta_client/management/generated/_generation.json)
records the input SHA-256, pinned versions, source commit, operation inventory,
and the compatibility adjustments applied to the input. Review that
record with the generated diff, not as a substitute for tests.

### Audited compatibility adapters

[`scripts/fern_compat.py`](../../scripts/fern_compat.py) owns specification
compatibility fixes: stripping documentation examples from generator input,
inlining unconstrained object inheritance and single-`allOf` inheritance
aliases, preserving common object fields without copying dispatch metadata
into discriminated leaves, removing three known prose defaults for integer
`policy.priority` fields, and lifting inline `OINApplication.profile` and
`User.type` schemas to avoid type-name collisions. The full `UserType` resource
retains its required fields; the embedded user's type reference retains its
original ID-only shape. Actual `oneOf`/`anyOf` constraints and public root
discriminators remain intact. These
adjustments are audited in provenance; they neither discard operations nor
patch generated runtime code. Fix adapter behavior and its tests when an
upstream modeling issue changes, rather than editing generated files.

The transport's `jsonable_encoder` is the wire serialization contract. The
pinned Fern universal serializer has limitations with Pydantic JSON-mode
alias/exclusion options; the [consumer guide](../management/README.md#typed-models-and-request-bodies)
documents the supported encoder. Keep regression fixtures for flat
discriminated objects and custom profile fields when changing generator pins.

The input produces **39 Fern endpoint route-overlap warnings** for literal
versus variable path patterns. All methods are retained, and clients use
explicit endpoints. Native upstream nonfatal docstring-format warnings also
remain visible. Investigate new warnings; do not suppress diagnostics or
silently drop models, methods, or errors to make generation succeed.

To add an endpoint or update API coverage:

1. Supply an updated management source or bundled spec; do not patch an
   endpoint directly into generated Python.
2. Prepare the canonical input, regenerate with the pinned tools, and inspect
   changes to paths, operation IDs, models, and provenance.
3. Add or update hand-written facade behavior and focused offline tests as
   needed. Verify both synchronous and asynchronous interfaces.
4. Run the regeneration check and applicable tests, lint, and package build.
   Include the specification, configuration changes, generated output, and
   provenance together in the PR.

**Never hand-edit the generated tree.** Fix the source specification, generator
configuration, or custom integration instead. A generator error, unsupported
feature, or lost operation is a failure to investigate, not a warning to hide.

### OpenAPI and model pitfalls

Review `allOf` inheritance, `oneOf`/`anyOf` unions and discriminators, recursive
references, nullable versus optional fields, defaults, and read-only/write-only
properties. Check JSON aliases, enums, date/time formats, and additional
properties against actual request/response contracts. Pydantic validation and
serialization must not drop meaningful fields or require fields that the
server can omit. Use fixtures for edge cases rather than live production data.

Check generated operation names and signatures, body/content types, pagination
and response headers, and non-success responses. Verify errors reach callers
with useful status/context; do not turn failures into empty successful results.
Document intentional coverage limitations instead of silently omitting APIs.

## Tests and packaging

Existing commands in the [Makefile](../../Makefile) are:

```sh
make test       # pytest tests -m "not integration"
make test-cov   # unit tests with coverage
make lint
make build
```

Use focused tests first, then the applicable broader checks before submitting.
The existing [tests](../../tests/) cover authentication foundations and OAuth
flows; preserve them while adding management-specific offline coverage.
Management tests include structural operation inventory checks and actual
HTTPX `MockTransport` requests through synchronous and asynchronous generated
clients. Lint and coverage exclude **only the machine-generated tree**;
hand-written modules retain existing checks and coverage thresholds.
[CircleCI](../../.circleci/config.yml) runs lint and unit coverage. Local unit
tests must not need an Okta org, Docker, or live credentials.

`make integration` runs live-service tests and is **opt-in**. Do not run it
without an explicitly configured test environment and authorization to use
that environment. Never commit tokens, private keys, org credentials, or
publishing credentials; redact sensitive data from fixtures and logs.

Build using the existing packaging configuration. Review wheel/sdist contents
for generated modules, typing information, and intended provenance, and verify
base installs plus `.[management]` installs. Generation must not replace the
package's metadata or existing authentication modules. Building and checking a
distribution does not require publishing; do not invoke publish targets or
request release credentials as part of ordinary contribution work.

## Reference implementations

- [Auth0 Python SDK](https://github.com/auth0/auth0-python) demonstrates a
  Fern-generated Management API alongside preserved custom authentication
  modules. Its [preservation rules](https://github.com/auth0/auth0-python/blob/master/.fernignore)
  inform this repository's custom/generated boundary.
- [Okta Go SDK](https://github.com/okta/okta-sdk-golang) demonstrates keeping a
  management specification, generator configuration, and regeneration workflow
  together. It uses OpenAPI Generator; this Python SDK uses Fern instead.
