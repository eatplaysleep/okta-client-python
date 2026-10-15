"""Prepare a deterministic, self-contained management OAS3 input for Fern.

Accepts a bundled YAML/JSON file or the Okta OAS3 source checkout. The latter
stores management paths and components in fragments rather than a conventional
root document with references to each path.
"""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
import re
import warnings
from pathlib import Path
from typing import Any, ClassVar
from urllib.parse import unquote, urlsplit

import yaml

HTTP_METHODS = frozenset({"get", "put", "post", "delete", "options", "head", "patch", "trace"})
IDENTIFIER_MAPS = frozenset(
    {
        "properties",
        "schemas",
        "paths",
        "responses",
        "parameters",
        "headers",
        "requestBodies",
        "callbacks",
        "links",
        "securitySchemes",
        "content",
    }
)


class SpecError(ValueError):
    """An input cannot be safely converted to a management API definition."""


class UniqueKeyLoader(yaml.SafeLoader):
    """Reject duplicate YAML keys instead of silently losing operations."""

    yaml_implicit_resolvers: ClassVar[dict] = {
        key: [
            (tag, pattern)
            for tag, pattern in resolvers
            if tag
            not in {
                "tag:yaml.org,2002:timestamp",
                "tag:yaml.org,2002:bool",
            }
        ]
        for key, resolvers in yaml.SafeLoader.yaml_implicit_resolvers.items()
    }


UniqueKeyLoader.add_implicit_resolver(
    "tag:yaml.org,2002:bool", re.compile(r"^(?:true|True|TRUE|false|False|FALSE)$"), list("tTfF")
)


def _mapping(loader: UniqueKeyLoader, node: yaml.MappingNode) -> dict:
    keys = set()
    for key_node, _value_node in node.value:
        if key_node.tag == "tag:yaml.org,2002:merge":
            continue
        key = loader.construct_object(key_node)
        if key in keys:
            raise SpecError(f"Duplicate YAML key: {key!r} at line {key_node.start_mark.line + 1}")
        keys.add(key)
    loader.flatten_mapping(node)
    return loader.construct_mapping(node)


UniqueKeyLoader.add_constructor(yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, _mapping)


def load_document(path: Path) -> dict[str, Any]:
    try:
        text = path.read_text(encoding="utf-8")
        # js-yaml accepts tabs separating a mapping key from its scalar value.
        text = re.sub(
            r"(?m)^([ ]*[^ \t:#][^\n:]*:)([ \t]*\t[ \t]*)",
            lambda match: match[1] + match[2].replace("\t", " "),
            text,
        )
        document = yaml.load(text, Loader=UniqueKeyLoader)
    except (SpecError, yaml.YAMLError) as error:
        raise SpecError(f"{path}: {error}") from error
    if not isinstance(document, dict):
        raise SpecError(f"Expected an OpenAPI object in {path}")
    return document


def _merge(target: dict, incoming: dict, location: str, *, merge_objects: bool = False) -> None:
    for key, value in incoming.items():
        if merge_objects and isinstance(target.get(key), dict) and isinstance(value, dict):
            _merge(target[key], value, f"{location}/{key}", merge_objects=True)
            continue
        if key in target and target[key] != value:
            if merge_objects and location.endswith("/scopes") and isinstance(target[key], str) and isinstance(value, str):
                warnings.warn(f"Using the base specification's description for repeated OAuth scope {key}", stacklevel=2)
                continue
            raise SpecError(f"Conflicting definition at {location}/{key}")
        target[key] = copy.deepcopy(value)


def merge_checkout(root: Path) -> dict[str, Any]:
    """Merge only management fragments, following the upstream SDK bundle scope."""
    candidates = [
        root / "specs/monolith/management/spec",
        root / "spec",
        root,
    ]
    source = next((p for p in candidates if (p / "base.yaml").is_file() and (p / "v1").is_dir()), None)
    if source is None:
        raise SpecError("Expected an Okta OAS3 checkout, management directory, or management spec directory")
    document = load_document(source / "base.yaml")
    for path in sorted((source / "v1").rglob("*.yaml")):
        # Upstream management-sdk.yaml excludes OIN submissions.
        if path.name.startswith("submissions"):
            continue
        fragment = load_document(path)
        for route, item in fragment.get("paths", {}).items():
            item = copy.deepcopy(item)
            if "security" in fragment:
                for method, operation in item.items():
                    if method in HTTP_METHODS:
                        operation.setdefault("security", copy.deepcopy(fragment["security"]))
            _merge(document.setdefault("paths", {}).setdefault(route, {}), item, f"paths/{route}")
        for group, definitions in fragment.get("components", {}).items():
            # Fragment security declarations refer back to base.yaml.
            if group == "securitySchemes" and set(definitions) == {"$ref"}:
                if definitions["$ref"].split("#")[-1] != "/components/securitySchemes":
                    raise SpecError(f"Unsupported security scheme reference in {path}: {definitions['$ref']}")
                continue
            if group == "securitySchemes":
                definitions = {
                    name: definition
                    for name, definition in definitions.items()
                    if not (
                        set(definition) == {"$ref"}
                        and definition["$ref"].split("#")[-1] == f"/components/securitySchemes/{name}"
                        and name in document.get("components", {}).get("securitySchemes", {})
                    )
                }
            _merge(
                document.setdefault("components", {}).setdefault(group, {}),
                definitions,
                f"components/{group}",
                merge_objects=group == "securitySchemes",
            )

    def reference(value: str) -> str:
        if "#/components/" in value:
            file, pointer = value.split("#", 1)
            if file and not urlsplit(file).scheme and file.endswith((".yaml", ".yml", ".json")):
                return f"#{pointer}"
        return value

    def internalize(value: Any, parent: str = "") -> Any:
        if isinstance(value, dict):
            result = {}
            for key, item in value.items():
                if key == "$ref" and isinstance(item, str):
                    result[key] = reference(item)
                elif key == "mapping" and parent == "discriminator":
                    result[key] = {name: reference(target) for name, target in item.items()}
                else:
                    result[key] = internalize(item, key)
            return result
        if isinstance(value, list):
            return [internalize(item) for item in value]
        return value

    return internalize(document)


def _pointer(document: dict, reference: str) -> Any:
    if not reference.startswith("#/"):
        raise SpecError(f"Only bundled local references are accepted; bundle this reference first: {reference}")
    target: Any = document
    for part in unquote(reference[2:]).split("/"):
        part = part.replace("~1", "/").replace("~0", "~")
        try:
            target = target[int(part)] if isinstance(target, list) else target[part]
        except (KeyError, IndexError, ValueError, TypeError) as error:
            raise SpecError(f"Unresolved reference: {reference}") from error
    return target


def validate_document(document: dict[str, Any]) -> list[str]:
    """Validate scope, references and stable, unique operation identifiers."""
    if not str(document.get("openapi", "")).startswith("3.0."):
        raise SpecError("This generation pipeline requires an OpenAPI 3.0.x management specification")
    operations = []
    for route, item in document.get("paths", {}).items():
        if not route.startswith("/"):
            raise SpecError(f"Invalid API path: {route}")
        for method, operation in item.items():
            if method not in HTTP_METHODS:
                continue
            identifier = operation.get("operationId")
            if not isinstance(identifier, str) or not identifier:
                raise SpecError(f"Missing operationId for {method.upper()} {route}")
            if identifier in operations:
                raise SpecError(f"Duplicate operationId: {identifier}")
            operations.append(identifier)
    if not operations:
        raise SpecError("The specification contains no operations")

    def walk(value: Any, *, example_object: bool = False, identifier_map: bool = False) -> None:
        if isinstance(value, dict):
            if identifier_map:
                for item in value.values():
                    walk(item)
                return
            if isinstance(value.get("$ref"), str):
                _pointer(document, value["$ref"])
            if "discriminator" in value:
                for reference in value["discriminator"].get("mapping", {}).values():
                    if "/" in reference or "#" in reference:
                        _pointer(document, reference)
            for key, item in value.items():
                if key in {"example", "default", "enum"} or key.startswith("x-"):
                    continue
                if example_object and key == "value":
                    continue
                if key == "examples":
                    if isinstance(item, dict):
                        for example in item.values():
                            walk(example, example_object=True)
                    continue
                walk(item, identifier_map=key in IDENTIFIER_MAPS)
        elif isinstance(value, list):
            for item in value:
                walk(item)

    walk(document)
    return sorted(operations)


def prepare(source: Path) -> tuple[dict[str, Any], list[str]]:
    document = merge_checkout(source) if source.is_dir() else load_document(source)
    return document, validate_document(document)


def encode(document: dict[str, Any]) -> bytes:
    return (json.dumps(document, indent=2, sort_keys=True, ensure_ascii=True) + "\n").encode("utf-8")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", type=Path, help="Bundled YAML/JSON spec or local Okta OAS3 checkout")
    parser.add_argument("--output", type=Path, default=Path("fern/openapi/management.json"))
    args = parser.parse_args()
    try:
        document, operations = prepare(args.source)
        content = encode(document)
    except (SpecError, OSError, yaml.YAMLError) as error:
        parser.exit(1, f"Spec preparation failed: {error}\n")
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_bytes(content)
    print(f"Prepared {len(operations)} management operations; sha256={hashlib.sha256(content).hexdigest()}")


if __name__ == "__main__":
    main()
