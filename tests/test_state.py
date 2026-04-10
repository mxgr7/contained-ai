from __future__ import annotations

import os
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


def test_dry_run_shows_no_state_note(tmp_path: Path, monkeypatch):
    _redirect_state(monkeypatch, tmp_path / "xdg")
    r = resolve(
        "claude", _loaded(tmp_path), CliOverrides(no_state=True), cwd=tmp_path
    )
    out = render_dry_run(r, host_env={})
    assert "state persistence disabled" in out
