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
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
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
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    rc = cli.main(["run", "--dry-run"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "special.example.com:443" in out


def test_run_invokes_runtime(tmp_path: Path, capsys, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
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
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    from contained import runtime

    def boom(resolved, cwd):
        raise runtime.DockerError("docker daemon is not reachable: nope")

    monkeypatch.setattr(runtime, "run", boom)
    rc = cli.main(["run", "claude"])
    assert rc == 1
    assert "docker daemon is not reachable" in capsys.readouterr().err


def test_passthrough_args(tmp_path: Path, capsys, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
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
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
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
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    monkeypatch.delenv("TOTALLY_UNSET_VAR_XYZ", raising=False)
    rc = cli.main(["run", "claude", "--env", "TOTALLY_UNSET_VAR_XYZ"])
    assert rc == 2
    err = capsys.readouterr().err
    assert "TOTALLY_UNSET_VAR_XYZ" in err
    assert "not set" in err
    assert "--env flag" in err


def _patch_claude_required_env(monkeypatch, keys: list[str]) -> None:
    import dataclasses

    from contained import profiles

    patched = dataclasses.replace(
        profiles.CLAUDE,
        env=list(profiles.CLAUDE.env) + keys,
        required_env=keys,
    )
    monkeypatch.setitem(profiles._PROFILES, "claude", patched)


def test_run_errors_on_unset_profile_env(tmp_path: Path, capsys, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("CONTAINED_TEST_REQUIRED", raising=False)
    _patch_claude_required_env(monkeypatch, ["CONTAINED_TEST_REQUIRED"])
    rc = cli.main(["run", "claude"])
    assert rc == 2
    err = capsys.readouterr().err
    assert "CONTAINED_TEST_REQUIRED" in err
    assert "agent profile 'claude'" in err


def test_run_errors_on_unset_profile_env_dry_run(tmp_path, capsys, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("CONTAINED_TEST_REQUIRED", raising=False)
    _patch_claude_required_env(monkeypatch, ["CONTAINED_TEST_REQUIRED"])
    rc = cli.main(["run", "claude", "--dry-run"])
    assert rc == 2
    assert "CONTAINED_TEST_REQUIRED" in capsys.readouterr().err


def test_run_claude_without_api_key_uses_oauth(tmp_path: Path, capsys, monkeypatch):
    """Claude profile must not require ANTHROPIC_API_KEY — OAuth is valid."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    rc = cli.main(["run", "claude", "--dry-run"])
    assert rc == 0
    assert "resolved config" in capsys.readouterr().out


def test_tmux_flag_dry_run(tmp_path: Path, capsys, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    rc = cli.main(["run", "claude", "--tmux", "--dry-run"])
    assert rc == 0
    out = capsys.readouterr().out
    # Tmux wrapper is visible in the docker invocation preview.
    assert "tmux new-session" in out
    assert "-s contained" in out


def test_no_state_flag_reaches_runtime(tmp_path: Path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "xdg"))
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
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


def _fake_run_info(tmp_path: Path, run_id: str, project: str | None = None):
    from contained import proxy
    fp = proxy.write_filter_file(
        ["api.anthropic.com:443"], state_dir=tmp_path,
    )
    return proxy.RunInfo(
        run_id=run_id,
        container=f"contained-proxy-{run_id}",
        project=project,
        filter_path=fp,
    )


def test_allow_requires_hosts_or_list(capsys, monkeypatch):
    from contained import proxy
    monkeypatch.setattr(proxy, "discover_runs", lambda: [])
    rc = cli.main(["allow"])
    assert rc == 2
    assert "specify one or more hosts" in capsys.readouterr().err


def test_allow_list_and_hosts_mutually_exclusive(capsys, monkeypatch):
    from contained import proxy
    monkeypatch.setattr(proxy, "discover_runs", lambda: [])
    rc = cli.main(["allow", "--list", "example.com:443"])
    assert rc == 2
    assert "mutually exclusive" in capsys.readouterr().err


def test_allow_errors_when_no_runs(capsys, monkeypatch):
    from contained import proxy
    monkeypatch.setattr(proxy, "discover_runs", lambda: [])
    rc = cli.main(["allow", "example.com:443"])
    assert rc == 1
    assert "no running contained sessions" in capsys.readouterr().err


def test_allow_adds_host_to_unique_run(tmp_path, capsys, monkeypatch):
    from contained import proxy
    info = _fake_run_info(tmp_path, "abc1234")
    try:
        monkeypatch.setattr(proxy, "discover_runs", lambda: [info])
        reloaded: list[str] = []
        monkeypatch.setattr(proxy, "reload", lambda c: reloaded.append(c))
        # Prevent the post-add probe from touching the network.
        import socket
        def fake_connect(addr, timeout=None):
            raise OSError("blocked in test")
        monkeypatch.setattr(socket, "create_connection", fake_connect)

        rc = cli.main(["allow", "example.com:443"])
        assert rc == 0
        out = capsys.readouterr().out
        assert "added example.com to run abc1234" in out
        assert "1 → 2 entries" in out
        assert reloaded == ["contained-proxy-abc1234"]
        assert "example.com" in proxy.read_filter_hosts(info.filter_path)
    finally:
        info.filter_path.unlink(missing_ok=True)


def test_allow_disambiguates_multiple_runs(tmp_path, capsys, monkeypatch):
    from contained import proxy
    a = _fake_run_info(tmp_path, "aaa", project="/home/me/a")
    b = _fake_run_info(tmp_path, "bbb", project="/home/me/b")
    try:
        monkeypatch.setattr(proxy, "discover_runs", lambda: [a, b])
        rc = cli.main(["allow", "example.com:443"])
        assert rc == 1
        err = capsys.readouterr().err
        assert "2 running sessions" in err
        assert "aaa" in err and "bbb" in err
    finally:
        a.filter_path.unlink(missing_ok=True)
        b.filter_path.unlink(missing_ok=True)


def test_allow_targets_specific_run(tmp_path, capsys, monkeypatch):
    from contained import proxy
    a = _fake_run_info(tmp_path, "aaa")
    b = _fake_run_info(tmp_path, "bbb")
    try:
        monkeypatch.setattr(proxy, "discover_runs", lambda: [a, b])
        monkeypatch.setattr(proxy, "reload", lambda c: None)
        import socket
        monkeypatch.setattr(
            socket, "create_connection",
            lambda *a, **k: (_ for _ in ()).throw(OSError("blocked")),
        )
        rc = cli.main(["allow", "--run", "bbb", "example.com:443"])
        assert rc == 0
        assert "example.com" in proxy.read_filter_hosts(b.filter_path)
        assert "example.com" not in proxy.read_filter_hosts(a.filter_path)
    finally:
        a.filter_path.unlink(missing_ok=True)
        b.filter_path.unlink(missing_ok=True)


def test_allow_list_prints_current_allowlist(tmp_path, capsys, monkeypatch):
    from contained import proxy
    info = _fake_run_info(tmp_path, "ccc")
    try:
        monkeypatch.setattr(proxy, "discover_runs", lambda: [info])
        rc = cli.main(["allow", "--list"])
        assert rc == 0
        out = capsys.readouterr().out
        assert "run ccc" in out
        assert "api.anthropic.com" in out
    finally:
        info.filter_path.unlink(missing_ok=True)


def test_allow_rejects_invalid_host(tmp_path, capsys, monkeypatch):
    from contained import proxy
    info = _fake_run_info(tmp_path, "ddd")
    try:
        monkeypatch.setattr(proxy, "discover_runs", lambda: [info])
        rc = cli.main(["allow", "not a host"])
        assert rc == 2
        assert "invalid hostname" in capsys.readouterr().err
    finally:
        info.filter_path.unlink(missing_ok=True)


def test_allow_idempotent(tmp_path, capsys, monkeypatch):
    from contained import proxy
    info = _fake_run_info(tmp_path, "eee")
    try:
        monkeypatch.setattr(proxy, "discover_runs", lambda: [info])
        reloaded: list[str] = []
        monkeypatch.setattr(proxy, "reload", lambda c: reloaded.append(c))
        rc = cli.main(["allow", "api.anthropic.com:443"])
        assert rc == 0
        assert reloaded == []  # no reload when nothing changed
        assert "no new hosts" in capsys.readouterr().out
    finally:
        info.filter_path.unlink(missing_ok=True)


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
