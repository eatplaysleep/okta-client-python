"""Auditable OpenAPI-to-Fern compatibility adaptations, never runtime patches."""

from __future__ import annotations

import copy
from typing import Any

from .prepare_management_spec import IDENTIFIER_MAPS, SpecError, _pointer, validate_document


def adapt(document: dict[str, Any]) -> tuple[dict[str, Any], list[str]]:
    """Preserve wire schemas while accommodating Fern's inheritance importer."""
    result = copy.deepcopy(document)
    changes = []
    schemas = result.get("components", {}).get("schemas", {})
    example_count = 0

    def remove_documentation_examples(value: Any, *, identifier_map: bool = False) -> None:
        nonlocal example_count
        if isinstance(value, dict):
            for key in list(value):
                if not identifier_map and key in {"example", "examples", "x-fern-examples"}:
                    del value[key]
                    example_count += 1
                elif not identifier_map and (key in {"default", "enum"} or key.startswith("x-")):
                    continue
                else:
                    remove_documentation_examples(value[key], identifier_map=not identifier_map and key in IDENTIFIER_MAPS)
        elif isinstance(value, list):
            for item in value:
                remove_documentation_examples(item)

    remove_documentation_examples(result)
    if example_count:
        changes.append(f"Omitted {example_count} documentation example annotations (raw input retained)")

    # These upstream fields contain documentation, not an integer default.
    for name in ("PolicyCommon", "Policy", "CreateOrUpdatePolicy"):
        priority = schemas.get(name, {}).get("properties", {}).get("priority", {})
        if priority.get("default") == "Last / Lowest Priority, for example `1`":
            del priority["default"]
            changes.append(f"Removed invalid prose integer default: {name}.priority")

    def inheritance_target(reference: str, seen: frozenset[str] = frozenset()) -> dict:
        if reference in seen:
            raise SpecError(f"Circular inheritance alias: {reference}")
        target = _pointer(result, reference)
        keys = {key for key in target if not key.startswith("x-")} - {"title", "description", "example", "examples"}
        if keys == {"$ref"}:
            return inheritance_target(target["$ref"], seen | {reference})
        branches = target.get("allOf", [])
        constraints = set(target) - {"allOf", "title", "description", "example", "examples"}
        constraints = {key for key in constraints if not key.startswith("x-")}
        if len(branches) == 1 and set(branches[0]) == {"$ref"} and not constraints:
            return inheritance_target(branches[0]["$ref"], seen | {reference})
        return target

    def dispatch_object(schema: dict) -> bool:
        return (
            schema.get("type") == "object" and "discriminator" in schema
            and "oneOf" not in schema and "anyOf" not in schema
        )

    # Dispatch metadata belongs to the public root, not to a mapped leaf's
    # inherited object shape. Copying it creates self-referential union aliases.
    for root in list(schemas.values()):
        if not dispatch_object(root):
            continue
        for reference in root["discriminator"].get("mapping", {}).values():
            leaf = _pointer(result, reference)
            if leaf is root or inheritance_target(reference) is not root:
                continue
            projection = copy.deepcopy(root)
            del projection["discriminator"]
            projection.update({key: value for key, value in leaf.items() if key not in {"$ref", "allOf"}})
            leaf.clear()
            leaf.update(projection)
            changes.append(f"Materialized mapped object alias without recursive dispatch: {reference}")

    for name, schema in schemas.items():
        branches = schema.get("allOf", [])
        for index, branch in enumerate(branches):
            if set(branch) != {"$ref"}:
                continue
            target = inheritance_target(branch["$ref"])
            structural_keys = {key for key in target if not key.startswith("x-")} - {
                "title",
                "description",
                "example",
                "examples",
            }
            if dispatch_object(target):
                inherited = copy.deepcopy(target)
                del inherited["discriminator"]
                branches[index] = inherited
                changes.append(f"Inherited common object fields without copied discriminator in {name}")
            elif structural_keys <= {"type"} and target.get("type") == "object":
                # An unconstrained object is not an extendable class in Fern.
                branches[index] = {"type": "object"}
                changes.append(f"Inlined unconstrained object base in {name}")
            elif target is not _pointer(result, branch["$ref"]):
                branches[index] = copy.deepcopy(target)
                changes.append(f"Resolved single-allOf inheritance alias in {name}")

    profile = schemas.get("OINApplication", {}).get("properties", {}).get("profile", {})
    profile_entry = profile.get("additionalProperties")
    if isinstance(profile_entry, dict) and "$ref" not in profile_entry:
        name = "OinApplicationProfileEntry"
        if name in schemas and schemas[name] != profile_entry:
            raise SpecError(f"Cannot lift OINApplication.profile entry: component {name} already exists")
        schemas[name] = profile_entry
        profile["additionalProperties"] = {"$ref": f"#/components/schemas/{name}"}
        changes.append("Lifted inline OINApplication.profile entries to avoid a Fern type-name collision")

    embedded_type = schemas.get("User", {}).get("properties", {}).get("type")
    if isinstance(embedded_type, dict) and embedded_type.get("type") == "object" and "properties" in embedded_type:
        # Fern names the inline User.type as UserType, colliding with the full
        # UserType resource and incorrectly importing its required fields.
        projection = copy.deepcopy(embedded_type)
        name = "UserTypeReference"
        if name in schemas and schemas[name] != projection:
            raise SpecError(f"Cannot model embedded User.type: component {name} already exists")
        schemas[name] = projection
        schemas["User"]["properties"]["type"] = {"$ref": f"#/components/schemas/{name}"}
        changes.append("Lifted inline User.type to avoid collision with the full UserType resource")

    validate_document(result)
    return result, sorted(changes)
