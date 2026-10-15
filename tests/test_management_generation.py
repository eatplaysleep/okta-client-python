"""Regeneration safety tests; no Docker or network access required."""

import json
import subprocess
from pathlib import Path

import pytest
from scripts import generate_management as generation
from scripts.prepare_management_spec import SpecError, encode


@pytest.fixture
def workspace(tmp_path, monkeypatch):
    root = Path(__file__).parents[1]
    (tmp_path / "fern").mkdir()
    (tmp_path / "fern/generators.yml").write_bytes((root / "fern/generators.yml").read_bytes())
    (tmp_path / "fern/fern.config.json").write_bytes((root / "fern/fern.config.json").read_bytes())
    cli = tmp_path / "node_modules/.bin/fern"
    cli.parent.mkdir(parents=True)
    cli.write_text("mock CLI")
    spec = tmp_path / "fern/openapi/management.json"
    spec.parent.mkdir()
    spec.write_bytes(
        encode(
            {
                "openapi": "3.0.3",
                "info": {"title": "Fixture", "version": "1"},
                "paths": {"/api/v1/users": {"get": {"operationId": "listUsers", "responses": {"204": {"description": "OK"}}}}},
            }
        )
    )
    generated = tmp_path / "src/okta_client/management/generated"
    generated.mkdir(parents=True)
    (generated / "client.py").write_text("old = True\n")
    facade = generated.parent / "client.py"
    facade.write_text("custom = True\n")
    monkeypatch.setattr(generation, "ROOT", tmp_path)
    monkeypatch.setattr(generation, "SPEC", spec)
    monkeypatch.setattr(generation, "GENERATED", generated)
    return spec, generated, facade


def fake_generate(command, *, cwd, env, check):
    assert env["FERN_DISABLE_TELEMETRY"] == "true"
    assert env["FERN_TELEMETRY_DISABLED"] == "true"
    assert check is True
    if command[1] == "-c":
        assert env["PYTHONPATH"] == str(cwd / "src")
        assert env["PYTHONDONTWRITEBYTECODE"] == "1"
        return
    if command[1] == "write-definition":
        definition = cwd / "fern/.definition"
        definition.mkdir()
        (definition / "api.yml").write_text("name: fixture\n")
        return
    assert "--local" in command
    if command[1] == "check":
        return
    assert "--no-replay" in command
    output = cwd / "src/okta_client/management/generated"
    output.mkdir(parents=True)
    (output / "client.py").write_text("generated = True\n")
    (output / "raw_client.py").write_text(
        "class RawUserClient:\n"
        "    def list_users(self):\n"
        "        return self.httpx_client.request('api/v1/users', method='GET')\n"
    )


def test_successful_generation_is_scoped_and_records_input(workspace, monkeypatch):
    spec, generated, facade = workspace
    monkeypatch.setattr(generation.subprocess, "run", fake_generate)
    generation.run(spec, runtime="docker")
    assert (generated / "client.py").read_text() == "generated = True\n"
    assert facade.read_text() == "custom = True\n"
    metadata = json.loads((generated / "_generation.json").read_bytes())
    assert metadata["operations"] == ["listUsers"]
    assert metadata["generator_version"] == "5.26.0"


def test_failed_generation_does_not_modify_existing_code_or_input(workspace, monkeypatch):
    spec, generated, facade = workspace
    original_spec = spec.read_bytes()

    def fail(command, **kwargs):
        raise subprocess.CalledProcessError(1, command)

    monkeypatch.setattr(generation.subprocess, "run", fail)
    with pytest.raises(subprocess.CalledProcessError):
        generation.run(spec, runtime="docker")
    assert spec.read_bytes() == original_spec
    assert (generated / "client.py").read_text() == "old = True\n"
    assert facade.read_text() == "custom = True\n"


def test_check_detects_stale_output_without_changing_files(workspace, monkeypatch):
    spec, generated, _facade = workspace
    monkeypatch.setattr(generation.subprocess, "run", fake_generate)
    with pytest.raises(SpecError, match="stale"):
        generation.run(spec, check=True, runtime="docker")
    assert (generated / "client.py").read_text() == "old = True\n"
    generation.run(spec, runtime="docker")
    generation.run(spec, check=True, runtime="docker")
    (generated / "obsolete.py").write_text("obsolete = True\n")
    with pytest.raises(SpecError, match=r"obsolete\.py"):
        generation.run(spec, check=True, runtime="docker")
    generation.run(spec, runtime="docker")
    assert not (generated / "obsolete.py").exists()


def test_generation_refuses_hosted_output(workspace):
    spec, _generated, _facade = workspace
    import yaml

    config_path = spec.parents[1] / "generators.yml"
    config = yaml.safe_load(config_path.read_text())
    config["groups"]["management"]["generators"][0]["output"]["location"] = "pypi"
    config_path.write_text(yaml.safe_dump(config))
    with pytest.raises(SpecError, match="Only local"):
        generation.run(spec)


def test_missing_cli_is_actionable(workspace):
    spec, _generated, _facade = workspace
    (spec.parents[2] / "node_modules/.bin/fern").unlink()
    with pytest.raises(SpecError, match="npm ci"):
        generation.run(spec)


def test_definition_cleanup_removes_only_annotation_examples(tmp_path):
    definition = tmp_path / "definition"
    definition.mkdir()
    path = definition / "users.yml"
    path.write_text(
        "types:\n"
        "  User:\n"
        "    properties:\n"
        "      examples: string\n"
        "    examples: [{value: {examples: preserved}}]\n"
        "errors:\n"
        "  Forbidden:\n"
        "    status-code: 403\n"
        "    examples: [{value: forbidden}]\n"
        "service:\n"
        "  auth: true\n"
        "  base-path: /api/v1\n"
        "  endpoints:\n"
        "    listUsers:\n"
        "      method: GET\n"
        "      path: /users\n"
        "      examples: [{response: []}]\n"
    )
    assert generation.remove_definition_examples(definition) == 3
    import yaml

    document = yaml.safe_load(path.read_text())
    assert document["types"]["User"]["properties"]["examples"] == "string"
    assert "examples" not in document["types"]["User"]
    assert "examples" not in document["errors"]["Forbidden"]
    assert document["service"]["endpoints"]["listUsers"] == {"method": "GET", "path": "/users"}


def test_generated_operation_verification_catches_omissions_and_wrong_routes(workspace, monkeypatch):
    spec, generated, _ = workspace
    monkeypatch.setattr(generation.subprocess, "run", fake_generate)
    generation.run(spec, runtime="docker")
    document = json.loads(spec.read_bytes())
    generation.validate_generated_operations(generated, document)
    raw = generated / "raw_client.py"
    raw.write_text(raw.read_text().replace("api/v1/users", "api/v1/wrong"))
    with pytest.raises(SpecError, match="wire routes differ"):
        generation.validate_generated_operations(generated, document)
    raw.unlink()
    with pytest.raises(SpecError, match="operation coverage"):
        generation.validate_generated_operations(generated, document)
