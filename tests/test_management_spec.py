"""Offline coverage for replacing and preparing the management OAS3 input."""

import copy
import json
from pathlib import Path

import pytest
from scripts.fern_compat import adapt
from scripts.prepare_management_spec import SpecError, encode, load_document, prepare, validate_document


@pytest.fixture
def spec():
    return {
        "openapi": "3.0.3",
        "info": {"title": "Management", "version": "1.0"},
        "paths": {
            "/api/v1/users": {
                "get": {
                    "operationId": "listUsers",
                    "responses": {
                        "200": {
                            "description": "Users",
                            "content": {"application/json": {"schema": {"$ref": "#/components/schemas/User"}}},
                        }
                    },
                }
            }
        },
        "components": {"schemas": {"User": {"type": "object", "properties": {"id": {"type": "string"}}}}},
    }


def test_bundled_yaml_and_json_have_identical_output(tmp_path, spec):
    import yaml

    yaml_path = tmp_path / "spec.yaml"
    json_path = tmp_path / "spec.json"
    yaml_path.write_text(yaml.safe_dump(spec))
    json_path.write_bytes(encode(spec))
    assert prepare(yaml_path) == prepare(json_path)
    assert encode(prepare(yaml_path)[0]) == encode(spec)


def test_checkout_merges_management_only(tmp_path, spec):
    source = tmp_path / "specs/monolith/management/spec"
    fragments = source / "v1"
    fragments.mkdir(parents=True)
    base = copy.deepcopy(spec)
    base["paths"] = {}
    base["components"] = {"securitySchemes": {"apiToken": {"type": "apiKey", "name": "Authorization", "in": "header"}}}
    (source / "base.yaml").write_text(json.dumps(base))
    fragment = copy.deepcopy(spec)
    fragment["paths"]["/api/v1/users"]["get"]["responses"]["200"]["content"]["application/json"]["schema"]["$ref"] = (
        "./users.yaml#/components/schemas/User"
    )
    (fragments / "users.yaml").write_text(json.dumps(fragment))
    excluded = copy.deepcopy(spec)
    excluded["paths"] = {"/oin/submissions": {"get": {"operationId": "listSubmissions"}}}
    (fragments / "submissions.yaml").write_text(json.dumps(excluded))
    (tmp_path / "oauth.yaml").write_text("not part of management")
    document, operations = prepare(tmp_path)
    assert operations == ["listUsers"]
    assert document["components"]["schemas"] == spec["components"]["schemas"]
    assert document["components"]["securitySchemes"] == base["components"]["securitySchemes"]
    assert document["paths"] == spec["paths"]


def test_conflicting_fragment_definitions_fail(tmp_path, spec):
    (tmp_path / "v1").mkdir()
    (tmp_path / "base.yaml").write_text(json.dumps(spec))
    fragment = copy.deepcopy(spec)
    fragment["components"]["schemas"]["User"]["type"] = "string"
    (tmp_path / "v1/users.yaml").write_text(json.dumps(fragment))
    with pytest.raises(SpecError, match="Conflicting definition"):
        prepare(tmp_path)


def test_fragment_global_security_is_applied_and_local_schemes_retained(tmp_path, spec):
    (tmp_path / "v1").mkdir()
    base = copy.deepcopy(spec)
    base["paths"] = {}
    (tmp_path / "base.yaml").write_text(json.dumps(base))
    fragment = copy.deepcopy(spec)
    fragment["security"] = [{"bearer": []}]
    fragment["components"]["securitySchemes"] = {"bearer": {"type": "http", "scheme": "bearer"}}
    (tmp_path / "v1/users.yaml").write_text(json.dumps(fragment))
    document, _ = prepare(tmp_path)
    assert document["paths"]["/api/v1/users"]["get"]["security"] == [{"bearer": []}]
    assert document["components"]["securitySchemes"] == fragment["components"]["securitySchemes"]


def test_checkout_rewrites_only_reference_fields(tmp_path, spec):
    (tmp_path / "v1").mkdir()
    spec["info"]["description"] = "See users.yaml#/components/schemas/User"
    spec["components"]["schemas"]["User"]["properties"]["friend"] = {"$ref": "users.yaml#/components/schemas/User"}
    (tmp_path / "base.yaml").write_text(json.dumps(spec))
    document, _ = prepare(tmp_path)
    assert document["info"]["description"] == spec["info"]["description"]
    assert document["components"]["schemas"]["User"]["properties"]["friend"] == {"$ref": "#/components/schemas/User"}


@pytest.mark.parametrize("version", ["2.0", "3.1.0", ""])
def test_rejects_unsupported_spec_versions(spec, version):
    spec["openapi"] = version
    with pytest.raises(SpecError, match=r"OpenAPI 3\.0"):
        validate_document(spec)


def test_rejects_missing_and_duplicate_operation_ids(spec):
    spec["paths"]["/other"] = copy.deepcopy(spec["paths"]["/api/v1/users"])
    with pytest.raises(SpecError, match="Duplicate operationId"):
        validate_document(spec)
    del spec["paths"]["/other"]["get"]["operationId"]
    with pytest.raises(SpecError, match="Missing operationId"):
        validate_document(spec)


def test_rejects_empty_spec(spec):
    spec["paths"] = {}
    with pytest.raises(SpecError, match="no operations"):
        validate_document(spec)


@pytest.mark.parametrize(
    "reference", ["#/components/schemas/Missing", "https://example.com/spec.json#/User", "User.yaml#/User"]
)
def test_rejects_unresolved_and_external_refs(spec, reference):
    spec["components"]["schemas"]["User"]["properties"]["friend"] = {"$ref": reference}
    with pytest.raises(SpecError, match="reference"):
        validate_document(spec)


def test_literal_examples_are_not_openapi_references(spec):
    spec["components"]["examples"] = {"ProfileSchema": {"value": {"$ref": "#/definitions/base"}}}
    spec["components"]["schemas"]["User"]["properties"]["$ref"] = {"type": "string"}
    spec["components"]["schemas"]["User"]["example"] = {"$ref": "#/definitions/example"}
    assert validate_document(spec) == ["listUsers"]


def test_json_pointer_escaping(spec):
    spec["components"]["schemas"]["a/b~c"] = {"type": "string"}
    spec["components"]["schemas"]["User"]["properties"]["other"] = {"$ref": "#/components/schemas/a~1b~0c"}
    assert validate_document(spec) == ["listUsers"]


@pytest.mark.parametrize("name", ["example", "examples", "default", "x-custom"])
def test_property_names_are_not_mistaken_for_annotations(spec, name):
    spec["components"]["schemas"]["User"]["properties"][name] = {"$ref": "#/components/schemas/Missing"}
    with pytest.raises(SpecError, match="Unresolved reference"):
        validate_document(spec)


def test_duplicate_yaml_keys_fail_but_merge_overrides_work(tmp_path):
    path = tmp_path / "spec.yaml"
    path.write_text("paths: {}\npaths: {}\n")
    with pytest.raises(SpecError, match="Duplicate YAML key"):
        load_document(path)
    path.write_text("base: &base\n  status: ACTIVE\nexample:\n  <<: *base\n  status: INACTIVE\n")
    assert load_document(path)["example"]["status"] == "INACTIVE"


def test_yaml_json_compatible_scalar_types(tmp_path):
    path = tmp_path / "spec.yaml"
    path.write_text("date: 2026-10-07\nstatus: on\nenabled: true\ndescription: \tA string\n")
    assert load_document(path) == {"date": "2026-10-07", "status": "on", "enabled": True, "description": "A string"}


def test_checked_in_spec_is_self_contained_and_complete():
    path = Path(__file__).parents[1] / "fern/openapi/management.json"
    document = json.loads(path.read_bytes())
    operations = validate_document(document)
    assert {"listUsers", "getUser", "createUser", "listGroups", "listApplications", "listLogEvents"} <= set(operations)


def test_fern_inheritance_adaptation_preserves_properties_and_raw_input(spec):
    schemas = spec["components"]["schemas"]
    schemas["EmptyObject"] = {"type": "object"}
    schemas["UserAlias"] = {"allOf": [{"$ref": "#/components/schemas/User"}]}
    schemas["Derived"] = {
        "allOf": [
            {"$ref": "#/components/schemas/EmptyObject"},
            {"$ref": "#/components/schemas/UserAlias"},
            {"type": "object", "properties": {"active": {"type": "boolean"}}},
        ]
    }
    original = copy.deepcopy(spec)
    document, adjustments = adapt(spec)
    assert spec == original
    assert document["components"]["schemas"]["Derived"]["allOf"] == [
        {"type": "object"},
        schemas["User"],
        {"type": "object", "properties": {"active": {"type": "boolean"}}},
    ]
    assert len(adjustments) == 2
    assert validate_document(document) == validate_document(spec)


def test_fern_adapter_does_not_remove_constrained_object_bases(spec):
    schemas = spec["components"]["schemas"]
    schemas["Restricted"] = {"type": "object", "additionalProperties": False}
    schemas["Derived"] = {"allOf": [{"$ref": "#/components/schemas/Restricted"}, {"type": "object"}]}
    document, adjustments = adapt(spec)
    assert document == spec
    assert adjustments == []


def test_fern_adapter_rejects_circular_inheritance(spec):
    schemas = spec["components"]["schemas"]
    schemas["A"] = {"allOf": [{"$ref": "#/components/schemas/B"}]}
    schemas["B"] = {"allOf": [{"$ref": "#/components/schemas/A"}]}
    with pytest.raises(SpecError, match="Circular inheritance alias"):
        adapt(spec)


def test_only_known_prose_default_is_removed(spec):
    schemas = spec["components"]["schemas"]
    schemas["PolicyCommon"] = {
        "type": "object",
        "properties": {"priority": {"type": "integer", "default": "Last / Lowest Priority, for example `1`"}},
    }
    document, adjustments = adapt(spec)
    assert "default" not in document["components"]["schemas"]["PolicyCommon"]["properties"]["priority"]
    assert adjustments == ["Removed invalid prose integer default: PolicyCommon.priority"]
    schemas["PolicyCommon"]["properties"]["priority"]["default"] = 1
    assert adapt(spec) == (spec, [])


def test_fern_example_cleanup_preserves_wire_fields_and_literals(spec):
    user = spec["components"]["schemas"]["User"]
    user["example"] = {"id": "example"}
    user["properties"]["example"] = {"type": "string", "example": "demo"}
    user["properties"]["examples"] = {"type": "object", "default": {"example": "a literal field"}}
    document, changes = adapt(spec)
    adapted_user = document["components"]["schemas"]["User"]
    assert "example" not in adapted_user
    assert adapted_user["properties"]["example"] == {"type": "string"}
    assert adapted_user["properties"]["examples"]["default"] == {"example": "a literal field"}
    assert changes == ["Omitted 2 documentation example annotations (raw input retained)"]


def test_embedded_user_type_does_not_relax_full_resource_validation(spec):
    schemas = spec["components"]["schemas"]
    schemas["UserType"] = {
        "type": "object",
        "required": ["name", "displayName"],
        "properties": {"id": {"type": "string"}, "name": {"type": "string"}, "displayName": {"type": "string"}},
    }
    schemas["User"]["properties"]["type"] = {"type": "object", "properties": {"id": {"type": "string"}}}
    document, changes = adapt(spec)
    adapted = document["components"]["schemas"]
    assert adapted["UserType"] == schemas["UserType"]
    assert adapted["User"]["properties"]["type"] == {"$ref": "#/components/schemas/UserTypeReference"}
    assert adapted["UserTypeReference"] == schemas["User"]["properties"]["type"]
    assert changes == ["Lifted inline User.type to avoid collision with the full UserType resource"]


def test_dispatch_metadata_is_not_copied_into_inherited_leaf_shapes(spec):
    schemas = spec["components"]["schemas"]
    schemas["Root"] = {
        "type": "object",
        "required": ["kind", "name"],
        "properties": {"kind": {"type": "string", "enum": ["email"]}, "name": {"type": "string"}},
        "discriminator": {"propertyName": "kind", "mapping": {"email": "#/components/schemas/Leaf"}},
    }
    schemas["Alias"] = {"allOf": [{"$ref": "#/components/schemas/Root"}]}
    schemas["Leaf"] = {"allOf": [{"$ref": "#/components/schemas/Alias"}]}
    document, changes = adapt(spec)
    adapted = document["components"]["schemas"]
    assert adapted["Root"] == schemas["Root"]
    assert adapted["Leaf"]["properties"] == schemas["Root"]["properties"]
    assert adapted["Leaf"]["required"] == ["kind", "name"]
    assert "discriminator" not in adapted["Leaf"]
    assert "Alias" in adapted
    assert changes


def test_actual_union_constraints_are_not_discarded(spec):
    schemas = spec["components"]["schemas"]
    schemas["Root"] = {
        "type": "object",
        "oneOf": [{"type": "object", "properties": {"kind": {"type": "string"}}}],
        "discriminator": {"propertyName": "kind"},
    }
    schemas["Alias"] = {"allOf": [{"$ref": "#/components/schemas/Root"}]}
    schemas["Leaf"] = {"allOf": [{"$ref": "#/components/schemas/Alias"}]}
    document, _ = adapt(spec)
    assert document["components"]["schemas"]["Leaf"]["allOf"][0] == schemas["Root"]
