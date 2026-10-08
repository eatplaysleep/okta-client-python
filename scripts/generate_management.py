"""Generate or verify only the isolated management package using local Fern."""

from __future__ import annotations

import argparse
import ast
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import yaml

from .fern_compat import adapt
from .native_fern import COMMIT, native_generate
from .prepare_management_spec import HTTP_METHODS, SpecError, encode, load_document, prepare

ROOT = Path(__file__).resolve().parents[1]
SPEC = ROOT / "fern/openapi/management.json"
GENERATED = ROOT / "src/okta_client/management/generated"


def validate_generated_operations(output: Path, document: dict) -> None:
    """Match every emitted request's verb/path to its input operation, not a count."""

    def identifier(name: str) -> str:
        return re.sub(r"[^a-z0-9]", "", name.lower())

    def route(path: str) -> str:
        return re.sub(r"\{[^}]+\}", "{parameter}", "/" + path.lstrip("/"))

    expected = {}
    for path, item in document["paths"].items():
        for method, operation in item.items():
            if method in HTTP_METHODS:
                key = identifier(operation["operationId"])
                if key in expected:
                    raise SpecError(f"Ambiguous generated operation name: {operation['operationId']}")
                expected[key] = (method.upper(), route(path))
    actual = {}
    for file in output.rglob("raw_client.py"):
        for definition in ast.parse(file.read_text()).body:
            if not isinstance(definition, ast.ClassDef) or not definition.name.startswith("Raw"):
                continue
            for function in definition.body:
                if not isinstance(function, ast.FunctionDef) or function.name.startswith("_"):
                    continue
                calls = [
                    node
                    for node in ast.walk(function)
                    if isinstance(node, ast.Call)
                    and isinstance(node.func, ast.Attribute)
                    and node.func.attr in {"request", "stream"}
                    and node.args
                ]
                if len(calls) != 1:
                    raise SpecError(f"Cannot verify generated request: {file.name}:{function.name}")
                call = calls[0]
                argument = call.args[0]
                if isinstance(argument, ast.Constant) and isinstance(argument.value, str):
                    path = argument.value
                elif isinstance(argument, ast.JoinedStr):
                    path = "".join(part.value if isinstance(part, ast.Constant) else "{parameter}" for part in argument.values)
                else:
                    raise SpecError(f"Cannot verify generated path: {function.name}")
                method = next((keyword.value for keyword in call.keywords if keyword.arg == "method"), None)
                if not isinstance(method, ast.Constant) or not isinstance(method.value, str):
                    raise SpecError(f"Cannot verify generated verb: {function.name}")
                key = identifier(function.name)
                if key in actual:
                    raise SpecError(f"Duplicate emitted operation: {function.name}")
                actual[key] = (method.value, route(path))
    if actual != expected:
        mismatches = sorted(key for key in actual.keys() | expected.keys() if actual.get(key) != expected.get(key))
        raise SpecError("Generated operation coverage or wire routes differ from the specification:\n" + "\n".join(mismatches))


def tree(path: Path) -> dict[str, bytes]:
    return {
        str(file.relative_to(path)): file.read_bytes()
        for file in sorted(path.rglob("*"))
        if file.is_file() and "__pycache__" not in file.parts and file.suffix != ".pyc"
    }


def replace_generated(output: Path) -> None:
    """Swap a completed generated tree, restoring the old tree if rename fails."""
    GENERATED.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".management-generation-", dir=GENERATED.parent) as directory:
        pending = Path(directory) / "pending"
        previous = Path(directory) / "previous"
        shutil.copytree(output, pending)
        had_previous = GENERATED.exists()
        if had_previous:
            GENERATED.rename(previous)
        try:
            pending.rename(GENERATED)
        except OSError:
            if had_previous:
                previous.rename(GENERATED)
            raise


def remove_definition_examples(definition: Path) -> int:
    """Remove only Fern declaration examples, including importer-synthesized ones."""
    count = 0
    for path in sorted(definition.rglob("*.yml")):
        document = load_document(path)
        declarations = [
            *document.get("types", {}).values(),
            *document.get("errors", {}).values(),
            *document.get("service", {}).get("endpoints", {}).values(),
        ]
        for declaration in declarations:
            if isinstance(declaration, dict) and "examples" in declaration:
                del declaration["examples"]
                count += 1
        path.write_text(yaml.safe_dump(document, sort_keys=False))
    return count


def validate_generated_imports(staging: Path, environment: dict[str, str]) -> None:
    """Import every generated module in isolation before replacing working code."""
    source = staging / "src"
    for package in (source / "okta_client", source / "okta_client/management"):
        (package / "__init__.py").write_text("")
    code = (
        "import importlib,pkgutil;"
        "from okta_client.management import generated;"
        "modules=list(pkgutil.walk_packages(generated.__path__,prefix=generated.__name__+'.'));"
        "[importlib.import_module(module.name) for module in modules];"
        "print(f'Validated {len(modules)} generated module imports')"
    )
    subprocess.run(
        [sys.executable, "-c", code], cwd=staging,
        env=dict(environment, PYTHONPATH=str(source), PYTHONDONTWRITEBYTECODE="1"), check=True,
    )


def run(source: Path, *, check: bool = False, runtime: str = "native") -> None:
    if runtime not in {"native", "docker"}:
        raise SpecError("Generator runtime must be native or docker")
    document, operations = prepare(source)
    content = encode(document)
    adapted, adjustments = adapt(document)
    cli = ROOT / "node_modules/.bin/fern"
    if not cli.is_file():
        raise SpecError("Pinned Fern CLI is missing; run npm ci first")
    config = yaml.safe_load((ROOT / "fern/generators.yml").read_text())
    generator = config["groups"]["management"]["generators"][0]
    if generator["output"]["location"] != "local-file-system":
        raise SpecError("Only local-file-system generation is allowed")
    if generator["output"]["path"] != "../src/okta_client/management/generated":
        raise SpecError("Generator output must remain inside the isolated management generated package")
    with tempfile.TemporaryDirectory(prefix="okta-management-") as directory:
        staging = Path(directory)
        (staging / "fern/openapi").mkdir(parents=True)
        (staging / "fern/openapi/management.json").write_bytes(encode(adapted))
        shutil.copyfile(ROOT / "fern/fern.config.json", staging / "fern/fern.config.json")
        (staging / "fern/generators.yml").write_text(yaml.safe_dump(config, sort_keys=False))
        environment = dict(os.environ, FERN_DISABLE_TELEMETRY="true", FERN_TELEMETRY_DISABLED="true")
        subprocess.run([str(cli), "write-definition"], cwd=staging, env=environment, check=True)
        definition = staging / "fern/definition"
        (staging / "fern/.definition").rename(definition)
        removed = remove_definition_examples(definition)
        adjustments.append(f"Removed synthesized Fern example metadata from {removed} declarations")
        # Feed the validated intermediate definition to the same stock generator,
        # avoiding the importer's second pass that would recreate invalid examples.
        del config["api"]
        (staging / "fern/generators.yml").write_text(yaml.safe_dump(config, sort_keys=False))
        subprocess.run([str(cli), "check", "--local", "--warnings"], cwd=staging, env=environment, check=True)
        if runtime == "native":
            native_generate(staging, cli, environment)
        else:
            subprocess.run(
                [str(cli), "generate", "--group", "management", "--local", "--no-replay"],
                cwd=staging,
                env=environment,
                check=True,
            )
        output = staging / "src/okta_client/management/generated"
        if not (output / "client.py").is_file():
            raise SpecError("Fern did not produce the expected package layout")
        for file in output.rglob("*.py"):
            ast.parse(file.read_text(encoding="utf-8"), filename=str(file), feature_version=(3, 10))
        validate_generated_operations(output, document)
        validate_generated_imports(staging, environment)
        provenance = {
            "spec_sha256": hashlib.sha256(content).hexdigest(),
            "fern_cli_version": json.loads((ROOT / "fern/fern.config.json").read_text())["version"],
            "generator": generator["name"],
            "generator_version": generator["version"],
            "generator_source_commit": COMMIT,
            "operations": operations,
            "compatibility_adjustments": adjustments,
        }
        (output / "_generation.json").write_bytes(encode(provenance))
        if check:
            expected, actual = tree(GENERATED), tree(output)
            differences = sorted(name for name in expected.keys() | actual.keys() if expected.get(name) != actual.get(name))
            if differences or SPEC.read_bytes() != content:
                raise SpecError("Generated management code is stale:\n" + "\n".join(differences))
            print(f"Management generation is reproducible ({len(operations)} operations)")
            return
        # Only this generated subtree is managed; never replace the SDK root.
        replace_generated(output)
        SPEC.parent.mkdir(parents=True, exist_ok=True)
        SPEC.write_bytes(content)
        print(f"Generated {len(operations)} management operations with {len(adjustments)} recorded adaptations")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--spec", type=Path, default=SPEC)
    parser.add_argument("--runtime", choices=["native", "docker"], default="native")
    parser.add_argument("--check", action="store_true", help="Compare a staged regeneration without changing tracked files")
    args = parser.parse_args()
    try:
        run(args.spec.resolve(), check=args.check, runtime=args.runtime)
    except (SpecError, RuntimeError, OSError, yaml.YAMLError, subprocess.CalledProcessError) as error:
        parser.exit(1, f"Management generation failed: {error}\n")


if __name__ == "__main__":
    main()
