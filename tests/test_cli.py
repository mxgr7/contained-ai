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


def test_run_invokes_runtime(tmp_path: Path, capsys, monkeypatch):
    monkeypatch.chdir(tmp_path)
    from contained import runtime

    called: dict[str, object] = {}

    def fake_run(resolved, cwd):
        called["agent"] = resolved.agent.name
        called["cwd"] = cwd
        return 42

    monkeypatch.setattr(runtime, "run", fake_run)
    rc = cli.main(["run", "claude"])
    assert rc == 42
    assert called["agent"] == "claude"
    assert called["cwd"] == tmp_path


def test_run_surfaces_runtime_error(tmp_path: Path, capsys, monkeypatch):
    monkeypatch.chdir(tmp_path)
    from contained import runtime

    def boom(resolved, cwd):
        raise runtime.RuntimeError("docker daemon is not reachable: nope")

    monkeypatch.setattr(runtime, "run", boom)
    rc = cli.main(["run", "claude"])
    assert rc == 1
    assert "docker daemon is not reachable" in capsys.readouterr().err


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


def test_build_command_invokes_runtime(monkeypatch, capsys):
    from contained import runtime

    called: dict[str, object] = {}

    def fake_build_base(tag, *, rebuild):
        called["base_tag"] = tag
        called["base_rebuild"] = rebuild
        return tag or "ghcr.io/contained-ai/contained-base:edge"

    def fake_build_proxy(tag=None, *, rebuild):
        called["proxy_rebuild"] = rebuild
        return "ghcr.io/contained-ai/contained-proxy:edge"

    monkeypatch.setattr(runtime, "build_base", fake_build_base)
    monkeypatch.setattr(runtime, "build_proxy", fake_build_proxy)
    rc = cli.main(["build"])
    assert rc == 0
    assert called["base_tag"] is None
    assert called["base_rebuild"] is False
    assert called["proxy_rebuild"] is False
    out = capsys.readouterr().out
    assert "contained-base" in out
    assert "contained-proxy" in out


def test_build_command_custom_tag_rebuild(monkeypatch, capsys):
    from contained import runtime

    seen: dict[str, object] = {}

    def fake_build_base(tag, *, rebuild):
        seen["base_tag"] = tag
        seen["rebuild"] = rebuild
        return tag

    def fake_build_proxy(tag=None, *, rebuild):
        seen["proxy_rebuild"] = rebuild
        return "proxy"

    monkeypatch.setattr(runtime, "build_base", fake_build_base)
    monkeypatch.setattr(runtime, "build_proxy", fake_build_proxy)
    rc = cli.main(["build", "--tag", "local/base:dev", "--rebuild"])
    assert rc == 0
    assert seen == {
        "base_tag": "local/base:dev",
        "rebuild": True,
        "proxy_rebuild": True,
    }


def test_run_errors_on_unset_required_env(tmp_path: Path, capsys, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("TOTALLY_UNSET_VAR_XYZ", raising=False)
    rc = cli.main(["run", "claude", "--env", "TOTALLY_UNSET_VAR_XYZ"])
    assert rc == 2
    err = capsys.readouterr().err
    assert "TOTALLY_UNSET_VAR_XYZ" in err
    assert "not set" in err


def test_no_state_flag_reaches_runtime(tmp_path: Path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "xdg"))
    from contained import runtime

    seen: dict[str, object] = {}

    def fake_run(resolved, cwd):
        seen["no_state"] = resolved.no_state
        seen["has_claude_mount"] = any(
            m.container == "/home/agent/.claude" for m in resolved.mounts
        )
        return 0

    monkeypatch.setattr(runtime, "run", fake_run)
    rc = cli.main(["run", "claude", "--no-state"])
    assert rc == 0
    assert seen == {"no_state": True, "has_claude_mount": False}


def test_doctor_runs_even_without_docker(capsys, monkeypatch):
    # Don't hit the live network during tests.
    import socket
    def fake_connect(addr, timeout=None):
        raise OSError("blocked in test")
    monkeypatch.setattr(socket, "create_connection", fake_connect)
    rc = cli.main(["doctor"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "contained doctor" in out
    assert "allowlist " in out  # reachability check ran


def test_doctor_reachability_passes_when_reachable(capsys, monkeypatch):
    import socket
    class FakeSock:
        def __enter__(self): return self
        def __exit__(self, *a): return False
    monkeypatch.setattr(socket, "create_connection", lambda *a, **k: FakeSock())
    rc = cli.main(["doctor"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "reachable" in out
