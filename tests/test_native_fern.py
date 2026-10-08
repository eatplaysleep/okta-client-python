"""Offline native-runner cache, configuration, and privacy-boundary tests."""

import json
import subprocess
from pathlib import Path

import pytest
import yaml
from scripts import native_fern as native


@pytest.fixture
def workspace(tmp_path, monkeypatch):
    source = tmp_path / "cache/source"
    generator = source / "generators/python"
    generator.mkdir(parents=True)
    lock = generator / "poetry.lock"
    lock.write_text("pinned public dependencies")
    python = tmp_path / "cache/venv/bin/python"
    python.parent.mkdir(parents=True)
    python.touch()
    v2 = source / "generators/python-v2/sdk/dist/cli.cjs"
    v2.parent.mkdir(parents=True)
    v2.touch()
    requirements = tmp_path / "native-requirements.txt"
    requirements.write_text("pinned native adapter dependencies")
    marker = tmp_path / "cache/ready.json"
    for name, path in {
        "SOURCE": source,
        "GENERATOR": generator,
        "PYTHON": python,
        "VENV": python.parents[1],
        "V2": v2,
        "REQUIREMENTS": requirements,
        "MARKER": marker,
    }.items():
        monkeypatch.setattr(native, name, path)
    marker.write_text(json.dumps(native._identity()))
    commands = []

    def check_output(command, **kwargs):
        commands.append(command)
        if command[0] == str(python):
            return native.PYTHON_VERSION + "\n"
        if command[-2:] == ["rev-parse", "HEAD"]:
            return native.COMMIT + "\n"
        if command[-3:] == ["status", "--porcelain", "--untracked-files=no"]:
            return ""
        pytest.fail(f"Unexpected subprocess: {command}")

    monkeypatch.setattr(native.subprocess, "check_output", check_output)
    staging = tmp_path / "staging"
    (staging / "fern/definition").mkdir(parents=True)
    generator_config = {
        "name": "fernapi/fern-python-sdk",
        "version": native.VERSION,
        "smart-casing": True,
        "disable-examples": True,
        "output": {"location": "local-file-system", "path": "../src/okta_client/management/generated"},
        "config": {"package_name": "okta_client.management.generated", "output_directory": "source-root"},
    }
    config = {"groups": {"management": {"generators": [generator_config]}}}
    (staging / "fern/generators.yml").write_text(yaml.safe_dump(config))
    (staging / "fern/fern.config.json").write_text(json.dumps({"organization": "okta", "version": "5.147.8"}))
    return staging, config, commands


@pytest.mark.parametrize("missing", ["MARKER", "PYTHON", "SOURCE", "V2", "lock"])
def test_missing_cache_is_actionable_before_any_subprocess(workspace, missing):
    staging, _, commands = workspace
    path = native.GENERATOR / "poetry.lock" if missing == "lock" else getattr(native, missing)
    if missing == "SOURCE":
        path.rename(path.with_name("absent-source"))
    else:
        path.unlink()
    with pytest.raises(RuntimeError, match=r"scripts\.native_fern --setup"):
        native.native_generate(staging, Path("mock-cli"), {})
    assert commands == []


@pytest.mark.parametrize("content", ["invalid json", json.dumps({"commit": "wrong"})])
def test_bad_marker_is_actionable_before_any_subprocess(workspace, content):
    staging, _, commands = workspace
    native.MARKER.write_text(content)
    with pytest.raises(RuntimeError, match=r"scripts\.native_fern --setup"):
        native.native_generate(staging, Path("mock-cli"), {})
    assert commands == []


def test_dependency_lock_change_invalidates_cache(workspace):
    staging, _, commands = workspace
    (native.GENERATOR / "poetry.lock").write_text("modified dependency lock")
    with pytest.raises(RuntimeError, match="setup missing/stale"):
        native.native_generate(staging, Path("mock-cli"), {})
    assert commands == []


def test_python_runtime_must_match_exact_pin(workspace, monkeypatch):
    staging, _, _ = workspace
    commands = []

    def wrong_python(command, **kwargs):
        commands.append(command)
        return "3.13.13\n"

    monkeypatch.setattr(native.subprocess, "check_output", wrong_python)
    with pytest.raises(RuntimeError, match=r"3\.13\.13, expected 3\.13\.12"):
        native.native_generate(staging, Path("mock-cli"), {})
    assert len(commands) == 1
    assert commands[0][0] == str(native.PYTHON)


@pytest.mark.parametrize("failure", ["revision", "dirty"])
def test_source_integrity_is_verified(workspace, monkeypatch, failure):
    staging, _, _ = workspace

    def source_output(command, **kwargs):
        if command[0] == str(native.PYTHON):
            return native.PYTHON_VERSION
        if command[-2:] == ["rev-parse", "HEAD"]:
            return "wrong-commit" if failure == "revision" else native.COMMIT
        return " M generators/python/src/changed.py\n"

    monkeypatch.setattr(native.subprocess, "check_output", source_output)
    with pytest.raises(RuntimeError, match=r"expected|tracked modifications"):
        native.native_generate(staging, Path("mock-cli"), {})


@pytest.mark.parametrize(
    "field,value",
    [
        ("name", "different-generator"),
        ("version", "5.25.0"),
        ("location", "pypi"),
        ("path", "../../../outside"),
        ("output_directory", "project"),
        ("package_name", "outside.generated"),
        ("api", {"specs": [{"openapi": "./private.json"}]}),
        ("definition", None),
    ],
)
def test_unsafe_or_unpinned_config_is_rejected_before_generation(workspace, monkeypatch, field, value):
    staging, config, _ = workspace
    generator = config["groups"]["management"]["generators"][0]
    if field in {"name", "version"}:
        generator[field] = value
    elif field in {"location", "path"}:
        generator["output"][field] = value
    elif field == "api":
        config["api"] = value
    elif field == "definition":
        (staging / "fern/definition").rmdir()
    else:
        generator["config"][field] = value
    (staging / "fern/generators.yml").write_text(yaml.safe_dump(config))
    monkeypatch.setattr(native, "_run", lambda *args, **kwargs: pytest.fail("Generation must not run"))
    with pytest.raises(ValueError, match="Native runner requires"):
        native.native_generate(staging, Path("mock-cli"), {})


def test_local_generation_preserves_config_and_enforces_privacy(workspace, monkeypatch):
    staging, config, _ = workspace
    captured = []
    original_environment = {"PATH": "/caller/bin", "FERN_DISABLE_TELEMETRY": "false", "SENTRY_DSN": "private-dsn"}
    original = original_environment.copy()
    config_before = (staging / "fern/generators.yml").read_bytes()

    def run(command, *, cwd, environment):
        captured.append((command, environment))
        assert cwd == staging
        if len(captured) == 1:
            Path(command[2]).write_text("{}")
        else:
            native_config = json.loads(Path(command[-1]).read_text())
            output = Path(native_config["output"]["path"])
            output.mkdir(parents=True)
            (output / "client.py").write_text("class OktaManagementClient: pass\n")

    monkeypatch.setattr(native, "_run", run)
    native.native_generate(staging, Path("mock-cli"), original_environment)
    assert original_environment == original
    assert (staging / "fern/generators.yml").read_bytes() == config_before
    ir_command, _ = captured[0]
    assert ir_command[1] == "ir"
    assert ir_command[ir_command.index("--version") + 1] == "v67"
    assert "--from-openapi" not in ir_command
    assert ir_command[ir_command.index("--smart-casing") + 1] == "true"
    for _, environment in captured:
        assert environment["FERN_DISABLE_TELEMETRY"] == "true"
        assert environment["FERN_TELEMETRY_DISABLED"] == "true"
        assert environment["SENTRY_DSN"] == ""
        assert environment["PYTHON_V2_PATH"] == str(native.V2)
        assert environment["TMPDIR"] == str(staging / ".native-fern")
        guard = staging / ".native-fern/deny-network.cjs"
        assert json.dumps(str(guard)) in environment["NODE_OPTIONS"]
        assert "net.Socket.prototype.connect = deny" in guard.read_text()
    native_config = json.loads((staging / ".native-fern/config.json").read_text())
    assert native_config["environment"] == {"_type": "local"}
    assert native_config["output"]["mode"] == {"type": "downloadFiles"}
    assert native_config["customConfig"] == config["groups"]["management"]["generators"][0]["config"]
    assert captured[1][0][0] == str(native.PYTHON)


def test_ir_failure_is_not_hidden_or_followed_by_generation(workspace, monkeypatch):
    staging, _, _ = workspace
    calls = []

    def fail(command, **kwargs):
        calls.append(command)
        raise subprocess.CalledProcessError(1, command)

    monkeypatch.setattr(native, "_run", fail)
    with pytest.raises(subprocess.CalledProcessError):
        native.native_generate(staging, Path("mock-cli"), {})
    assert len(calls) == 1
