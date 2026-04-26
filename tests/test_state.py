from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

from contained import state
from contained.config import ConfigError, LoadedConfig, ConfigSection
from contained.run import CliOverrides, render_dry_run, resolve


def _loaded(tmp_path: Path) -> LoadedConfig:
    return LoadedConfig(
        path=None,
        base_dir=tmp_path,
        default_agent=None,
        defaults=ConfigSection(),
        agents={},
    )


def _redirect_state(monkeypatch, root: Path) -> None:
    monkeypatch.setenv("XDG_DATA_HOME", str(root))


def test_project_id_is_stable_and_path_sensitive(tmp_path: Path):
    a = tmp_path / "alpha"
    b = tmp_path / "beta"
    a.mkdir()
    b.mkdir()
    id_a1 = state.project_id(a)
    id_a2 = state.project_id(a)
    id_b = state.project_id(b)
    assert id_a1 == id_a2
    assert id_a1 != id_b
    assert id_a1.startswith("alpha-")
    assert id_b.startswith("beta-")


def test_two_projects_have_independent_state_dirs(tmp_path: Path, monkeypatch):
    _redirect_state(monkeypatch, tmp_path / "xdg")
    a = tmp_path / "proj_a"
    b = tmp_path / "proj_b"
    a.mkdir()
    b.mkdir()
    da = state.agent_state_dir(a, "claude")
    db = state.agent_state_dir(b, "claude")
    assert da != db


def test_ensure_agent_state_dir_creates_0700(tmp_path: Path, monkeypatch):
    _redirect_state(monkeypatch, tmp_path / "xdg")
    proj = tmp_path / "proj"
    proj.mkdir()
    d = state.ensure_agent_state_dir(proj, "claude")
    assert d.is_dir()
    if os.name == "posix":
        mode = d.stat().st_mode & 0o777
        assert mode == 0o700


def test_resolve_adds_state_mount_for_claude(tmp_path: Path, monkeypatch):
    _redirect_state(monkeypatch, tmp_path / "xdg")
    r = resolve("claude", _loaded(tmp_path), CliOverrides(), cwd=tmp_path)
    state_mounts = [m for m in r.mounts if m.container == "/home/agent/.claude"]
    assert len(state_mounts) == 1
    assert state_mounts[0].read_only is False
    # Points into the per-project state dir
    assert "projects" in state_mounts[0].host.parts
    assert state_mounts[0].host.name == "claude"


def test_no_state_flag_suppresses_state_mount(tmp_path: Path, monkeypatch):
    _redirect_state(monkeypatch, tmp_path / "xdg")
    r = resolve(
        "claude", _loaded(tmp_path), CliOverrides(no_state=True), cwd=tmp_path
    )
    assert not any(m.container == "/home/agent/.claude" for m in r.mounts)
    assert r.no_state is True


def test_pi_profile_has_its_own_state_mount(tmp_path: Path, monkeypatch):
    _redirect_state(monkeypatch, tmp_path / "xdg")
    r = resolve("pi", _loaded(tmp_path), CliOverrides(), cwd=tmp_path)
    pi_mounts = [m for m in r.mounts if m.container == "/home/agent/.pi"]
    assert len(pi_mounts) == 1
    assert pi_mounts[0].host.name == "pi"


def test_state_mounts_are_cross_mounted_across_agents(
    tmp_path: Path, monkeypatch
):
    """Both ~/.claude and ~/.pi state dirs are bound regardless of agent."""
    _force_keychain_miss(monkeypatch)
    _redirect_state(monkeypatch, tmp_path / "xdg")
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: fake_home))
    monkeypatch.setenv("HOME", str(fake_home))
    proj = tmp_path / "proj"
    proj.mkdir()
    for agent in ("claude", "pi"):
        r = resolve(agent, _loaded(proj), CliOverrides(), cwd=proj)
        mounts_by_container = {m.container: m for m in r.mounts}
        claude_mount = mounts_by_container["/home/agent/.claude"]
        pi_mount = mounts_by_container["/home/agent/.pi"]
        assert claude_mount.host == state.agent_state_dir(proj, "claude"), agent
        assert pi_mount.host == state.agent_state_dir(proj, "pi"), agent


def test_pi_profile_shares_claude_credentials(tmp_path: Path, monkeypatch):
    """pi containers get the same global Claude credential bind as claude."""
    _force_keychain_miss(monkeypatch)
    _redirect_state(monkeypatch, tmp_path / "xdg")
    fake_home = tmp_path / "home"
    (fake_home / ".claude").mkdir(parents=True)
    (fake_home / ".claude" / ".credentials.json").write_bytes(b'{"k":"v"}')
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: fake_home))
    monkeypatch.setenv("HOME", str(fake_home))
    proj = tmp_path / "proj"
    proj.mkdir()
    r = resolve("pi", _loaded(proj), CliOverrides(), cwd=proj)
    mounts_by_container = {m.container: m for m in r.mounts}
    creds_mount = mounts_by_container["/home/agent/.claude/.credentials.json"]
    assert creds_mount.host == state.global_state_dir() / "claude/.credentials.json"
    assert creds_mount.read_only is False


def test_pi_auth_is_shared_globally_across_both_profiles(
    tmp_path: Path, monkeypatch
):
    """pi's auth.json lives in the global dir and binds into both agents."""
    _force_keychain_miss(monkeypatch)
    _redirect_state(monkeypatch, tmp_path / "xdg")
    fake_home = tmp_path / "home"
    (fake_home / ".pi" / "agent").mkdir(parents=True)
    (fake_home / ".pi" / "agent" / "auth.json").write_bytes(b'{"t":"pi"}')
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: fake_home))
    monkeypatch.setenv("HOME", str(fake_home))
    proj = tmp_path / "proj"
    proj.mkdir()
    expected_host = state.global_state_dir() / "pi/agent/auth.json"
    for agent in ("claude", "pi"):
        r = resolve(agent, _loaded(proj), CliOverrides(), cwd=proj)
        mounts_by_container = {m.container: m for m in r.mounts}
        pi_auth = mounts_by_container["/home/agent/.pi/agent/auth.json"]
        assert pi_auth.host == expected_host, agent
        assert pi_auth.read_only is False, agent


def test_refuse_root_mount(tmp_path: Path):
    with pytest.raises(ConfigError, match="root"):
        resolve(
            "claude",
            _loaded(tmp_path),
            CliOverrides(mounts=["/:/mnt/root"]),
            cwd=tmp_path,
        )


def test_refuse_home_mount_without_flag(tmp_path: Path, monkeypatch):
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: fake_home))
    with pytest.raises(ConfigError, match="home directory"):
        resolve(
            "claude",
            _loaded(tmp_path),
            CliOverrides(mounts=[f"{fake_home}:/mnt/home"]),
            cwd=tmp_path,
        )


def test_allow_home_mount_flag_permits(tmp_path: Path, monkeypatch):
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: fake_home))
    r = resolve(
        "claude",
        _loaded(tmp_path),
        CliOverrides(
            mounts=[f"{fake_home}:/mnt/home"], allow_home_mount=True, no_state=True
        ),
        cwd=tmp_path,
    )
    assert any(m.container == "/mnt/home" for m in r.mounts)


def test_refuse_symlink_to_root_mount(tmp_path: Path):
    link = tmp_path / "link-to-root"
    link.symlink_to("/")
    with pytest.raises(ConfigError, match="root"):
        resolve(
            "claude",
            _loaded(tmp_path),
            CliOverrides(mounts=[f"{link}:/mnt/x"]),
            cwd=tmp_path,
        )


def test_refuse_symlink_to_home_mount(tmp_path: Path, monkeypatch):
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: fake_home))
    link = tmp_path / "link-to-home"
    link.symlink_to(fake_home)
    with pytest.raises(ConfigError, match="home directory"):
        resolve(
            "claude",
            _loaded(tmp_path),
            CliOverrides(mounts=[f"{link}:/mnt/home"]),
            cwd=tmp_path,
        )


def test_nonexistent_mount_source_errors(tmp_path: Path):
    missing = tmp_path / "nope"
    with pytest.raises(ConfigError, match="does not exist"):
        resolve(
            "claude",
            _loaded(tmp_path),
            CliOverrides(mounts=[f"{missing}:/mnt/x"]),
            cwd=tmp_path,
        )


def test_workspace_default_still_ok_even_without_extra_mounts(tmp_path: Path, monkeypatch):
    _redirect_state(monkeypatch, tmp_path / "xdg")
    r = resolve("claude", _loaded(tmp_path), CliOverrides(), cwd=tmp_path)
    assert any(m.container == "/workspace" for m in r.mounts)


def test_sensitive_dir_warning_on_rw_mount(tmp_path: Path, monkeypatch):
    _redirect_state(monkeypatch, tmp_path / "xdg")
    src = tmp_path / "src"
    src.mkdir()
    (src / ".ssh").mkdir()
    r = resolve(
        "claude",
        _loaded(tmp_path),
        CliOverrides(mounts=[f"{src}:/mnt/src"]),
        cwd=tmp_path,
    )
    assert any(".ssh" in w for w in r.warnings)


def test_sensitive_dir_warning_suppressed_when_ro(tmp_path: Path, monkeypatch):
    _redirect_state(monkeypatch, tmp_path / "xdg")
    src = tmp_path / "src"
    src.mkdir()
    (src / ".ssh").mkdir()
    r = resolve(
        "claude",
        _loaded(tmp_path),
        CliOverrides(mounts_ro=[f"{src}:/mnt/src"]),
        cwd=tmp_path,
    )
    assert not any(".ssh" in w for w in r.warnings)


def _force_keychain_miss(monkeypatch):
    """Make keychain reads return None without prompting the user."""
    monkeypatch.setattr(state, "_keychain_cache", {}, raising=False)
    monkeypatch.setattr(state, "_keychain_read", lambda service: None)


def test_plan_seeds_reads_host_file(tmp_path: Path, monkeypatch):
    _redirect_state(monkeypatch, tmp_path / "xdg")
    _force_keychain_miss(monkeypatch)
    from contained import profiles
    fake_home = tmp_path / "home"
    (fake_home / ".claude").mkdir(parents=True)
    (fake_home / ".claude" / ".credentials.json").write_bytes(b'{"k":"v"}')
    (fake_home / ".claude.json").write_bytes(b'{"onboarded": true}')
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: fake_home))
    monkeypatch.setenv("HOME", str(fake_home))
    pstate = tmp_path / "pstate"
    pstate.mkdir()
    plans = state.plan_seeds(profiles.CLAUDE, pstate)
    assert len(plans) == 3
    by_rel = {p.seed.state_rel: p for p in plans}
    creds = by_rel["claude/.credentials.json"]
    assert creds.data == b'{"k":"v"}'
    # Credentials are a global seed — always file-bound and stored
    # outside the per-project state dir.
    assert creds.needs_mount is True
    assert creds.host_path == state.global_state_dir() / "claude/.credentials.json"
    assert pstate not in creds.host_path.parents
    assert by_rel["claude.json"].data == b'{"onboarded": true}'
    assert by_rel["claude.json"].needs_mount is True


def test_plan_seeds_uses_keychain_on_darwin(tmp_path: Path, monkeypatch):
    _redirect_state(monkeypatch, tmp_path / "xdg")
    from contained import profiles
    monkeypatch.setattr(sys, "platform", "darwin")
    monkeypatch.setattr(state, "_keychain_cache", {}, raising=False)
    monkeypatch.setattr(
        state,
        "_keychain_read",
        lambda service: '{"token":"from-keychain"}' if service == "Claude Code-credentials" else None,
    )
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: fake_home))
    monkeypatch.setenv("HOME", str(fake_home))
    pstate = tmp_path / "pstate"
    pstate.mkdir()
    plans = state.plan_seeds(profiles.CLAUDE, pstate)
    creds = next(p for p in plans if p.seed.state_rel == "claude/.credentials.json")
    assert creds.source == "keychain:Claude Code-credentials"
    assert creds.data == b'{"token":"from-keychain"}'


def test_plan_seeds_fallback_for_needs_mount(tmp_path: Path, monkeypatch):
    _redirect_state(monkeypatch, tmp_path / "xdg")
    _force_keychain_miss(monkeypatch)
    from contained import profiles
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: fake_home))
    monkeypatch.setenv("HOME", str(fake_home))
    pstate = tmp_path / "pstate"
    pstate.mkdir()
    plans = state.plan_seeds(profiles.CLAUDE, pstate)
    cj = next(p for p in plans if p.seed.state_rel == "claude.json")
    assert cj.source is None
    assert cj.data == b"{}\n"
    assert cj.needs_mount is True
    # Credentials: no host source. Global seeds are file-bound, so a
    # fallback placeholder is written (empty bytes) so the bind target
    # exists and the container's writes persist globally.
    creds = next(p for p in plans if p.seed.state_rel == "claude/.credentials.json")
    assert creds.source is None
    assert creds.data == b""
    assert creds.needs_mount is True


def test_plan_seeds_reuses_cached_state(tmp_path: Path, monkeypatch):
    _redirect_state(monkeypatch, tmp_path / "xdg")
    _force_keychain_miss(monkeypatch)
    from contained import profiles
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: fake_home))
    monkeypatch.setenv("HOME", str(fake_home))
    pstate = tmp_path / "pstate"
    pstate.mkdir()
    # Credentials are global — stash prior file in the global dir.
    gstate = state.global_state_dir()
    (gstate / "claude").mkdir(parents=True)
    (gstate / "claude" / ".credentials.json").write_bytes(b"prior")
    plans = state.plan_seeds(profiles.CLAUDE, pstate)
    creds = next(p for p in plans if p.seed.state_rel == "claude/.credentials.json")
    assert creds.source == "(cached)"
    assert creds.data is None


def test_apply_seeds_writes_pending(tmp_path: Path):
    from contained.profiles import FileSeed
    seed = FileSeed(
        sources=(),
        state_rel="foo.json",
        container_path="/home/agent/foo.json",
    )
    plan = state.PlannedSeed(
        seed=seed,
        host_path=tmp_path / "foo.json",
        source=None,
        data=b"hello",
        needs_mount=True,
    )
    written = state.apply_seeds([plan])
    assert written == [plan]
    assert (tmp_path / "foo.json").read_bytes() == b"hello"
    if os.name == "posix":
        assert (tmp_path / "foo.json").stat().st_mode & 0o777 == 0o600


def test_claude_profile_has_file_seeds():
    from contained import profiles
    assert profiles.CLAUDE.file_seeds
    paths = {s.container_path for s in profiles.CLAUDE.file_seeds}
    assert "/home/agent/.claude/.credentials.json" in paths
    assert "/home/agent/.claude.json" in paths


def test_resolve_adds_bind_mount_for_claude_json(tmp_path: Path, monkeypatch):
    _force_keychain_miss(monkeypatch)
    _redirect_state(monkeypatch, tmp_path / "xdg")
    fake_home = tmp_path / "home"
    (fake_home / ".claude").mkdir(parents=True)
    (fake_home / ".claude" / ".credentials.json").write_bytes(b'{"k":"v"}')
    (fake_home / ".claude.json").write_bytes(b'{"onboarded": true}')
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: fake_home))
    monkeypatch.setenv("HOME", str(fake_home))
    proj = tmp_path / "proj"
    proj.mkdir()
    r = resolve("claude", _loaded(proj), CliOverrides(), cwd=proj)
    mounts_by_container = {m.container: m for m in r.mounts}
    assert "/home/agent/.claude.json" in mounts_by_container
    # Credentials are global and always get their own file bind that
    # overlays the per-project state_mount.
    creds_mount = mounts_by_container["/home/agent/.claude/.credentials.json"]
    assert creds_mount.host == state.global_state_dir() / "claude/.credentials.json"
    assert "projects" not in creds_mount.host.parts


def test_runtime_prints_seed_notice(tmp_path: Path, monkeypatch, capsys):
    from contained import runtime
    _force_keychain_miss(monkeypatch)
    _redirect_state(monkeypatch, tmp_path / "xdg")
    fake_home = tmp_path / "home"
    (fake_home / ".claude").mkdir(parents=True)
    (fake_home / ".claude" / ".credentials.json").write_text('{"k":"v"}')
    (fake_home / ".claude.json").write_text('{"onboarded": true}')
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: fake_home))
    monkeypatch.setenv("HOME", str(fake_home))
    monkeypatch.setattr(runtime, "ensure_daemon", lambda: None)
    monkeypatch.setattr(runtime, "_execute", lambda argv: 0)
    proj = tmp_path / "proj"
    proj.mkdir()
    r = resolve(
        "claude", _loaded(proj), CliOverrides(network="none"), cwd=proj
    )
    runtime.run(r, proj)
    err = capsys.readouterr().err
    assert "contained: seeded" in err
    assert "-> " in err
    assert "mode 600" in err


def test_read_source_rejects_symlink_file(tmp_path: Path):
    real = tmp_path / "real.json"
    real.write_text("secret")
    link = tmp_path / "link.json"
    link.symlink_to(real)
    assert state._read_source(str(link)) is None
    assert state._read_source(str(real)) == b"secret"


def test_runtime_seeds_credentials_before_launch(tmp_path: Path, monkeypatch, mock_proxy):
    from contained import runtime
    _force_keychain_miss(monkeypatch)
    _redirect_state(monkeypatch, tmp_path / "xdg")
    fake_home = tmp_path / "home"
    (fake_home / ".claude").mkdir(parents=True)
    (fake_home / ".claude" / ".credentials.json").write_text('{"k":"v"}')
    (fake_home / ".claude.json").write_text('{"onboarded": true}')
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: fake_home))
    monkeypatch.setenv("HOME", str(fake_home))
    monkeypatch.setattr(runtime, "ensure_daemon", lambda: None)
    monkeypatch.setattr(runtime, "_execute", lambda argv: 0)
    proj = tmp_path / "proj"
    proj.mkdir()
    r = resolve("claude", _loaded(proj), CliOverrides(), cwd=proj)
    runtime.run(r, proj)
    pstate = state.project_state_dir(proj)
    gstate = state.global_state_dir()
    # Credentials land in the tool-wide global dir, shared across projects.
    assert (gstate / "claude" / ".credentials.json").read_text() == '{"k":"v"}'
    import json as _json
    data = _json.loads((pstate / "claude.json").read_text())
    # Host-seeded keys are preserved and the shift-enter flag is added.
    assert data == {"onboarded": True, "shiftEnterKeyBindingInstalled": True}


def test_resolve_warns_when_no_credentials(tmp_path: Path, monkeypatch):
    _force_keychain_miss(monkeypatch)
    _redirect_state(monkeypatch, tmp_path / "xdg")
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: fake_home))
    monkeypatch.setenv("HOME", str(fake_home))
    proj = tmp_path / "proj"
    proj.mkdir()
    r = resolve("claude", _loaded(proj), CliOverrides(), cwd=proj)
    # credentials.json has no source and no fallback → warning
    assert any("claude/.credentials.json" in w for w in r.warnings)
    # claude.json has a fallback → no warning, just a placeholder
    assert not any("claude.json " in w for w in r.warnings)


def test_patch_claude_json_on_missing_file(tmp_path: Path):
    p = tmp_path / "claude.json"
    assert state.patch_claude_json_shift_enter(p) is True
    import json
    assert json.loads(p.read_text()) == {"shiftEnterKeyBindingInstalled": True}


def test_patch_claude_json_preserves_existing_keys(tmp_path: Path):
    p = tmp_path / "claude.json"
    p.write_text('{"theme": "dark", "tips": 3}')
    assert state.patch_claude_json_shift_enter(p) is True
    import json
    data = json.loads(p.read_text())
    assert data == {
        "theme": "dark",
        "tips": 3,
        "shiftEnterKeyBindingInstalled": True,
    }


def test_patch_claude_json_is_idempotent(tmp_path: Path):
    p = tmp_path / "claude.json"
    p.write_text('{"shiftEnterKeyBindingInstalled": true, "x": 1}')
    assert state.patch_claude_json_shift_enter(p) is False
    import json
    assert json.loads(p.read_text())["x"] == 1


def test_patch_claude_json_leaves_malformed_file_alone(tmp_path: Path):
    p = tmp_path / "claude.json"
    p.write_text("{not json")
    assert state.patch_claude_json_shift_enter(p) is False
    assert p.read_text() == "{not json"


def test_patch_claude_json_leaves_non_object_alone(tmp_path: Path):
    p = tmp_path / "claude.json"
    p.write_text("[1,2,3]")
    assert state.patch_claude_json_shift_enter(p) is False
    assert p.read_text() == "[1,2,3]"


def test_dry_run_shows_no_state_note(tmp_path: Path, monkeypatch):
    _redirect_state(monkeypatch, tmp_path / "xdg")
    r = resolve(
        "claude", _loaded(tmp_path), CliOverrides(no_state=True), cwd=tmp_path
    )
    out = render_dry_run(r, host_env={})
    assert "state persistence disabled" in out
