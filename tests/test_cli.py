from __future__ import annotations

import os
from pathlib import Path

import pytest

from contained import cli


def test_version(capsys: pytest.CaptureFixture[str]):
    with pytest.raises(SystemExit) as exc:
        cli.main(["--version"])
    assert exc.value.code == 0
    assert "contained" in capsys.readouterr().out


def test_help_when_no_command(capsys: pytest.CaptureFixture[str]):
    rc = cli.main([])
    assert rc == 0
    assert "usage" in capsys.readouterr().out.lower()


def test_run_dry_run_no_config(tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch):
    monkeypatch.chdir(tmp_path)
    rc = cli.main(["run", "claude", "--dry-run"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "resolved config" in out
    assert "docker" in out
    assert "/workspace" in out


def test_run_dry_run_with_config(tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch):
    (tmp_path / "contained.yaml").write_text(
        "default_agent: claude\n"
        "agents:\n"
        "  claude:\n"
        "    allowlist: [special.example.com:443]\n"
    )
    monkeypatch.chdir(tmp_path)
    rc = cli.main(["run", "--dry-run"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "special.example.com:443" in out


def test_run_without_dry_run_errors_until_phase2(tmp_path: Path, capsys, monkeypatch):
    monkeypatch.chdir(tmp_path)
    rc = cli.main(["run", "claude"])
    assert rc == 1
    assert "not implemented" in capsys.readouterr().err


def test_passthrough_args(tmp_path: Path, capsys, monkeypatch):
    monkeypatch.chdir(tmp_path)
    rc = cli.main(["run", "claude", "--dry-run", "--", "--help"])
    assert rc == 0
    out = capsys.readouterr().out
    # Passthrough arg should show up in the docker preview line
    assert "--help" in out


def test_unknown_agent_clear_error(tmp_path: Path, capsys, monkeypatch):
    monkeypatch.chdir(tmp_path)
    rc = cli.main(["run", "nosuch", "--dry-run"])
    assert rc == 2
    assert "unknown agent" in capsys.readouterr().err


def test_no_config_flag_ignores_discovered(tmp_path: Path, capsys, monkeypatch):
    (tmp_path / "contained.yaml").write_text(
        "agents:\n  claude:\n    image: from-config\n"
    )
    monkeypatch.chdir(tmp_path)
    rc = cli.main(["run", "claude", "--dry-run", "--no-config"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "from-config" not in out


def test_doctor_runs_even_without_docker(capsys):
    rc = cli.main(["doctor"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "contained doctor" in out
