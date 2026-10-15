"""Execute Fern Python 5.26.0 locally from pinned public OSS source.

Setup: .venv/bin/python -m scripts.native_fern --setup
Smoke test: .venv/bin/python -m scripts.native_fern --smoke-test

Requires uv, pnpm, git and Node >=22. Public dependencies are downloaded during
setup only. IR export and generation deny network connections, including in
Node child processes. No Docker, remote generator, publishing, or SDK-runtime
dependency installation is involved.
"""

from __future__ import annotations

import argparse
import ast
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
VERSION = "5.26.0"
COMMIT = "55cd3e8a1e688fda195ed855977b35b05e80894d"
IR_VERSION = "67"
PYTHON_VERSION = "3.13.12"
CACHE = ROOT / "node_modules/.cache"
SOURCE = CACHE / f"fern-python-{VERSION}"
GENERATOR = SOURCE / "generators/python"
VENV = CACHE / "fern-native-venv"
PYTHON = VENV / "bin/python"
V2 = SOURCE / "generators/python-v2/sdk/dist/cli.cjs"
REQUIREMENTS = ROOT / "fern/native-requirements.txt"
MARKER = CACHE / "fern-native-ready.json"

# This is an application privacy boundary, not a container/runtime workaround.
# Blocking sockets also protects against unexpected telemetry or remote parsing.
NETWORK_GUARD = """
'use strict';
const net = require('node:net');
const tls = require('node:tls');
const deny = () => { throw new Error('Native Fern generation forbids network connections'); };
net.Socket.prototype.connect = deny;
net.connect = deny;
net.createConnection = deny;
tls.connect = deny;
"""


def _run(command: list[str], *, cwd: Path, environment: dict[str, str]) -> None:
    subprocess.run(command, cwd=cwd, env=environment, check=True)


def _identity() -> dict[str, str]:
    return {
        "commit": COMMIT,
        "requirements_sha256": hashlib.sha256(REQUIREMENTS.read_bytes()).hexdigest(),
        "poetry_lock_sha256": hashlib.sha256((GENERATOR / "poetry.lock").read_bytes()).hexdigest(),
        "ir_version": IR_VERSION,
        "python_version": PYTHON_VERSION,
    }


def _check_python(environment: dict[str, str]) -> None:
    version = subprocess.check_output(
        [str(PYTHON), "-I", "-S", "-c", "import sys; print(sys.version.split()[0])"],
        env=environment,
        text=True,
    ).strip()
    if version != PYTHON_VERSION:
        raise RuntimeError(
            f"Native Fern Python is {version}, expected {PYTHON_VERSION}; "
            f"recreate the isolated {VENV} environment with "
            ".venv/bin/python -m scripts.native_fern --setup"
        )


def _check_setup(environment: dict[str, str]) -> None:
    message = "Native Fern setup missing/stale; run .venv/bin/python -m scripts.native_fern --setup"
    if (
        not MARKER.is_file()
        or not PYTHON.is_file()
        or not SOURCE.is_dir()
        or not (GENERATOR / "poetry.lock").is_file()
        or not V2.is_file()
    ):
        raise RuntimeError(message)
    try:
        identity = json.loads(MARKER.read_text())
    except json.JSONDecodeError as error:
        raise RuntimeError(message) from error
    if identity != _identity():
        raise RuntimeError(message)
    _check_python(environment)
    _check_source(environment)


def _check_source(environment: dict[str, str]) -> None:
    revision = subprocess.check_output(["git", "-C", str(SOURCE), "rev-parse", "HEAD"], env=environment, text=True).strip()
    if revision != COMMIT:
        raise RuntimeError(f"Native Fern source is {revision}, expected {COMMIT}")
    status = subprocess.check_output(
        ["git", "-C", str(SOURCE), "status", "--porcelain", "--untracked-files=no"],
        env=environment,
        text=True,
    )
    if status:
        raise RuntimeError(f"Pinned native Fern source has tracked modifications:\n{status}")


def setup() -> None:
    """Download public source/tooling, using upstream's frozen dependency locks."""
    for tool in ("git", "uv", "pnpm", "node"):
        if shutil.which(tool) is None:
            raise RuntimeError(f"Native Fern setup requires {tool} on PATH")
    CACHE.mkdir(parents=True, exist_ok=True)
    environment = dict(
        os.environ,
        FERN_DISABLE_TELEMETRY="true",
        FERN_TELEMETRY_DISABLED="true",
        TURBO_TELEMETRY_DISABLED="1",
        DO_NOT_TRACK="1",
        UV_CACHE_DIR=str(CACHE / "uv"),
        UV_PYTHON_INSTALL_DIR=str(CACHE / "python"),
        POETRY_CACHE_DIR=str(CACHE / "poetry"),
        PNPM_HOME=str(CACHE / "pnpm"),
    )
    if not SOURCE.exists():
        _run(
            ["git", "clone", "--filter=blob:none", "--no-checkout", "https://github.com/fern-api/fern.git", str(SOURCE)],
            cwd=ROOT,
            environment=environment,
        )
        _run(
            [
                "git",
                "-C",
                str(SOURCE),
                "sparse-checkout",
                "set",
                "generators/python",
                "generators/python-v2",
                "generators/commons",
                "generators/base",
                "generators/browser-compatible-base",
                "generators/dynamic-snippets",
                "packages",
                "patches",
            ],
            cwd=ROOT,
            environment=environment,
        )
        _run(["git", "-C", str(SOURCE), "checkout", "--detach", COMMIT], cwd=ROOT, environment=environment)
    _check_source(environment)
    if not PYTHON.exists():
        _run(
            ["uv", "venv", "--python", PYTHON_VERSION, "--managed-python", str(VENV)],
            cwd=ROOT,
            environment=environment,
        )
    _check_python(environment)
    # Bootstrap Poetry and the native adapter in their own requirements-managed
    # venv; the generator itself remains managed by upstream Poetry.
    _run(
        ["uv", "pip", "install", "--python", str(PYTHON), "-r", str(REQUIREMENTS)],
        cwd=ROOT,
        environment=environment,
    )
    environment.update(
        VIRTUAL_ENV=str(VENV),
        PATH=f"{VENV / 'bin'}{os.pathsep}{environment['PATH']}",
        POETRY_VIRTUALENVS_CREATE="false",
    )
    _run(
        [str(VENV / "bin/poetry"), "-C", str(GENERATOR), "install", "--only", "main", "--no-root", "--no-interaction"],
        cwd=ROOT,
        environment=environment,
    )
    _run(
        [
            "pnpm",
            "--filter",
            "@fern-api/python-sdk...",
            "install",
            "--frozen-lockfile",
            "--ignore-scripts",
            "--store-dir",
            str(CACHE / "pnpm-store"),
        ],
        cwd=SOURCE,
        environment=environment,
    )
    _run(
        ["pnpm", "exec", "turbo", "run", "dist:cli", "--filter=@fern-api/python-sdk", "--no-daemon"],
        cwd=SOURCE,
        environment=environment,
    )
    if not V2.is_file():
        raise RuntimeError("Stock Python v2 companion build did not produce cli.cjs")
    _check_source(environment)
    MARKER.write_text(json.dumps(_identity(), indent=2) + "\n")
    print(f"Native Fern {VERSION} ready: {COMMIT}; IR {IR_VERSION}; isolated environment {VENV}")


def native_generate(staging: Path, cli: Path, environment: dict) -> None:
    """Emit the prepared definition's SDK into staging, with no network access."""
    staging, cli = staging.resolve(), cli.resolve()
    _check_setup(environment)
    config = yaml.safe_load((staging / "fern/generators.yml").read_text())
    generator = config["groups"]["management"]["generators"][0]
    expected = {
        "name": "fernapi/fern-python-sdk",
        "version": VERSION,
    }
    if any(generator.get(key) != value for key, value in expected.items()):
        raise ValueError(f"Native runner requires {expected}")
    custom = generator["config"]
    if (
        "api" in config
        or not (staging / "fern/definition").is_dir()
        or generator["output"]["location"] != "local-file-system"
        or generator["output"]["path"] != "../src/okta_client/management/generated"
        or custom.get("output_directory") != "source-root"
        or custom.get("package_name") != "okta_client.management.generated"
    ):
        raise ValueError("Native runner requires a prepared local definition and the isolated management source-root output")
    working = staging / ".native-fern"
    working.mkdir(exist_ok=True)
    guard = working / "deny-network.cjs"
    guard.write_text(NETWORK_GUARD)
    private_environment = dict(
        environment,
        FERN_DISABLE_TELEMETRY="true",
        FERN_TELEMETRY_DISABLED="true",
        FERN_NO_VERSION_REDIRECTION="true",
        SENTRY_DSN="",
        NODE_OPTIONS=f"--require={json.dumps(str(guard))}",
        PATH=f"{VENV / 'bin'}{os.pathsep}{environment.get('PATH', os.defpath)}",
        PYTHONPATH=str(GENERATOR / "src"),
        FERN_CORE_UTILITIES_PATH=os.pathsep.join(
            str(GENERATOR / f"core_utilities/{directory}") for directory in ("sdk", "shared")
        ),
        PYTHON_V2_PATH=str(V2),
        TMPDIR=str(working),
        TMP=str(working),
        TEMP=str(working),
    )
    ir_path = working / "ir.json"
    # Audited installed CLI: `ir` -> H2p -> J1A -> JOt (definition-to-IR)
    # -> eUe (bundled version migration) -> pC (local file write).
    # Neither remote `generate` nor the remote OpenAPI parser is invoked.
    _run(
        [
            str(cli),
            "ir",
            str(ir_path),
            "--version",
            f"v{IR_VERSION}",
            "--language",
            "python",
            "--smart-casing",
            str(generator.get("smart-casing", False)).lower(),
            "--disable-examples",
            str(generator.get("disable-examples", False)).lower(),
        ],
        cwd=staging,
        environment=private_environment,
    )
    output = staging / "src/okta_client/management/generated"
    native_config = {
        "dryRun": False,
        "irFilepath": str(ir_path),
        "output": {"path": str(output), "mode": {"type": "downloadFiles"}},
        "workspaceName": "management",
        "organization": json.loads((staging / "fern/fern.config.json").read_text())["organization"],
        "customConfig": custom,
        "environment": {"_type": "local"},
        "whitelabel": False,
        "writeUnitTests": False,
        "generatePaginatedClients": True,
        "generateOauthClients": True,
    }
    config_path = working / "config.json"
    config_path.write_text(json.dumps(native_config, indent=2) + "\n")
    # Keep the stock Python generator and mandatory stock v2 postrun. Only
    # translate Docker's hardcoded Ruff mount/cwd into equivalent native paths.
    # Calling main directly avoids the upstream Typer 0.6 / Click 8.4 help bug.
    _run(
        [str(PYTHON), str(Path(__file__).resolve()), "--execute-config", str(config_path)],
        cwd=staging,
        environment=private_environment,
    )
    if not (output / "client.py").is_file():
        raise RuntimeError("Native Fern did not emit the expected management client")
    files = list(output.rglob("*.py"))
    for file in files:
        ast.parse(file.read_text(encoding="utf-8"), filename=str(file))
    print(f"Native Fern {VERSION} generated {len(files)} valid Python files (IR {IR_VERSION}; network denied)")


def _execute(config_path: Path) -> None:
    def deny_network(event: str, arguments: tuple) -> None:
        if event == "socket.connect":
            raise RuntimeError("Native Fern generation forbids network connections")

    sys.addaudithook(deny_network)
    from fern_python.cli.publisher import Publisher
    from fern_python.generators.sdk.cli import main as stock_main

    stock_run_command = Publisher._run_command

    def native_run_command(self, *, command, safe_command, cwd=None, env=None):
        if command[0] == "ruff" and "/fern/output" in command:
            command = [self._generator_config.output.path if part == "/fern/output" else part for part in command]
            command.extend(["--config", str(GENERATOR / "pyproject.toml")])
            safe_command = " ".join(command)
            cwd = str(GENERATOR)
        return stock_run_command(self, command=command, safe_command=safe_command, cwd=cwd, env=env)

    Publisher._run_command = native_run_command
    # Stock v2's postrun reads the config filepath from sys.argv[1].
    sys.argv = [sys.argv[0], str(config_path)]
    stock_main(str(config_path))


def smoke_test() -> None:
    """Generate an in-memory public/example definition with the real config."""
    CACHE.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="fern-native-smoke-", dir=CACHE) as directory:
        staging = Path(directory)
        definition = staging / "fern/definition"
        definition.mkdir(parents=True)
        shutil.copyfile(ROOT / "fern/fern.config.json", staging / "fern/fern.config.json")
        config = yaml.safe_load((ROOT / "fern/generators.yml").read_text())
        del config["api"]
        (staging / "fern/generators.yml").write_text(yaml.safe_dump(config, sort_keys=False))
        (definition / "api.yml").write_text(
            "name: native-smoke\nenvironments:\n  Production: https://example.com\ndefault-environment: Production\n"
        )
        (definition / "users.yml").write_text(
            "types:\n  User:\n    properties:\n      id: string\n      displayName: optional<string>\n"
            "service:\n  base-path: /users\n  auth: false\n  endpoints:\n"
            "    get:\n      path: /{userId}\n      method: GET\n"
            "      path-parameters:\n        userId: string\n      response: User\n"
        )
        native_generate(staging, ROOT / "node_modules/.bin/fern", dict(os.environ))
        output = staging / "src/okta_client/management/generated"
        client = output / "client.py"
        if "OktaManagementClient" not in client.read_text():
            raise RuntimeError("Smoke test did not emit the configured client class")
        user_file = next(output.rglob("user.py"))
        user_module = ".".join(
            ("okta_client", "management", "generated", *user_file.relative_to(output).with_suffix("").parts)
        )
        for parent in (output.parent, output.parent.parent):
            (parent / "__init__.py").touch()
        _run(
            [
                str(PYTHON),
                "-c",
                "from okta_client.management.generated.client import OktaManagementClient; "
                f"from {user_module} import User; "
                "assert User(id='smoke', displayName='Smoke').id == 'smoke'; "
                "assert OktaManagementClient(base_url='https://example.com').users is not None; "
                "import inspect; "
                "from okta_client.management.generated.client import AsyncOktaManagementClient; "
                "print('Sync constructor:', inspect.signature(OktaManagementClient)); "
                "print('Async constructor:', inspect.signature(AsyncOktaManagementClient)); "
                "print('Generated client and Pydantic v2 model import/instantiation passed')",
            ],
            cwd=staging,
            environment=dict(
                os.environ,
                PYTHONPATH=str(staging / "src"),
                FERN_DISABLE_TELEMETRY="true",
                FERN_TELEMETRY_DISABLED="true",
            ),
        )
        print("Native Fern public smoke test passed")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    actions = parser.add_mutually_exclusive_group(required=True)
    actions.add_argument("--setup", action="store_true")
    actions.add_argument("--smoke-test", action="store_true")
    actions.add_argument("--execute-config", type=Path, help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args.setup:
        setup()
    elif args.smoke_test:
        smoke_test()
    else:
        _execute(args.execute_config)


if __name__ == "__main__":
    main()
