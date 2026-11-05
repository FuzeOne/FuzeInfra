from __future__ import annotations

import importlib.util
import pathlib
from types import SimpleNamespace

import pytest

REPO = pathlib.Path(__file__).resolve().parents[1]


def _load_module(relative_path: str, module_name: str):
    path = REPO / relative_path
    spec = importlib.util.spec_from_file_location(module_name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


setup_environment = _load_module("scripts-tools/setup_environment.py", "setup_environment")
migrate_volumes = _load_module("scripts-tools/migrate_volumes.py", "migrate_volumes")
run_tests = _load_module("scripts-tools/run_tests.py", "run_tests")


def test_quote_postgres_identifier_rejects_unsafe_names():
    with pytest.raises(ValueError):
        setup_environment.quote_postgres_identifier('bad-name"; DROP DATABASE prod; --', "POSTGRES_DB")


def test_quote_postgres_literal_escapes_single_quotes():
    assert setup_environment.quote_postgres_literal("pa'ss") == "'pa''ss'"


def test_setup_environment_runs_docker_without_shell(monkeypatch):
    captured = {}

    def fake_run(cmd, **kwargs):
        captured["cmd"] = cmd
        captured["kwargs"] = kwargs
        return SimpleNamespace(stdout="ok")

    monkeypatch.setattr(setup_environment.subprocess, "run", fake_run)

    success, output = setup_environment.run_docker_command(["docker", "ps"])

    assert success is True
    assert output == "ok"
    assert captured["cmd"] == ["docker", "ps"]
    assert captured["kwargs"].get("shell", False) is False


def test_volume_exists_uses_argument_list(monkeypatch):
    captured = {}

    def fake_run(cmd, **kwargs):
        captured["cmd"] = cmd
        captured["kwargs"] = kwargs
        return SimpleNamespace(stdout="")

    monkeypatch.setattr(migrate_volumes.subprocess, "run", fake_run)

    assert migrate_volumes.volume_exists("demo-volume") is True
    assert captured["cmd"] == ["docker", "volume", "inspect", "demo-volume"]
    assert captured["kwargs"].get("shell", False) is False


def test_migrate_volume_data_copies_without_shell(monkeypatch):
    commands = []

    def fake_run_command(cmd, description=""):
        commands.append((cmd, description))
        return True

    monkeypatch.setattr(migrate_volumes, "run_command", fake_run_command)
    monkeypatch.setattr(
        migrate_volumes,
        "volume_exists",
        lambda name: name in {"source-volume", "target-volume"},
    )

    assert migrate_volumes.migrate_volume_data("source-volume", "target-volume") is True
    assert commands == [
        (
            [
                "docker",
                "run",
                "--rm",
                "-v",
                "source-volume:/source",
                "-v",
                "target-volume:/target",
                "alpine",
                "cp",
                "-a",
                "/source/.",
                "/target/",
            ],
            "Copying data from source-volume to target-volume",
        )
    ]


def test_run_tests_splits_string_commands_without_shell(monkeypatch):
    captured = {}

    def fake_run(cmd, **kwargs):
        captured["cmd"] = cmd
        captured["kwargs"] = kwargs
        return SimpleNamespace(stdout="", returncode=0)

    monkeypatch.setattr(run_tests.subprocess, "run", fake_run)

    result = run_tests.run_command("docker --version", check=False)

    assert result.returncode == 0
    assert captured["cmd"] == ["docker", "--version"]
    assert captured["kwargs"].get("shell") is False
