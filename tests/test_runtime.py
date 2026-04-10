from __future__ import annotations

import subprocess
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from contained import profiles, runtime
from contained.config import LoadedConfig, ConfigSection
from contained.run import CliOverrides, resolve


def _resolved(tmp_path: Path, **cli_kwargs):
    loaded = LoadedConfig(
        path=None,
        base_dir=tmp_path,
        default_agent=None,
        defaults=ConfigSection(),
        agents={},
    )
    return resolve("claude", loaded, CliOverrides(**cli_kwargs), cwd=tmp_path)


def test_build_argv_has_security_flags(tmp_path: Path):
    r = _resolved(tmp_path)
    argv = runtime.build_argv(r)
    assert argv[:4] == ["docker", "run", "--rm", "-it"]
    assert "--cap-drop" in argv
    assert argv[argv.index("--cap-drop") + 1] == "ALL"
    assert "--security-opt" in argv
    assert "no-new-privileges" in argv
    assert "--user" in argv
    assert "--init" in argv


def test_build_argv_workdir_and_workspace_mount(tmp_path: Path):
    r = _resolved(tmp_path)
    argv = runtime.build_argv(r)
    assert "--workdir" in argv
    assert argv[argv.index("--workdir") + 1] == "/workspace"
    joined = " ".join(argv)
    assert f"src={tmp_path}" in joined
    assert "dst=/workspace" in joined


def test_build_argv_network_modes(tmp_path: Path):
    host = runtime.build_argv(_resolved(tmp_path, network="host"))
    none = runtime.build_argv(_resolved(tmp_path, network="none"))
    allow = runtime.build_argv(_resolved(tmp_path, network="allowlist"))
    assert "--network" in host and host[host.index("--network") + 1] == "host"
    assert "--network" in none and none[none.index("--network") + 1] == "none"
    # allowlist: proxy is PRD 04, so no --network flag for now
    assert "--network" not in allow


def test_build_argv_entrypoint_and_passthrough(tmp_path: Path):
    r = _resolved(tmp_path, passthrough=["--foo", "bar"])
    argv = runtime.build_argv(r)
    image_idx = argv.index(r.image)
    tail = argv[image_idx + 1 :]
    assert tail[0] == "claude"  # profile entrypoint
    assert tail[-2:] == ["--foo", "bar"]


def test_build_argv_env_masking(tmp_path: Path):
    r = _resolved(tmp_path, env=["MY_SECRET_KEY=hunter2"])
    masked = " ".join(runtime.build_argv(r, mask_secrets=True))
    unmasked = " ".join(runtime.build_argv(r, mask_secrets=False))
    assert "hunter2" in unmasked
    assert "hunter2" not in masked
    assert "***" in masked


def test_ensure_daemon_missing_binary(monkeypatch):
    monkeypatch.setattr(runtime.shutil, "which", lambda _: None)
    with pytest.raises(runtime.RuntimeError, match="docker binary not found"):
        runtime.ensure_daemon()


def test_ensure_daemon_daemon_down(monkeypatch):
    monkeypatch.setattr(runtime.shutil, "which", lambda _: "/usr/bin/docker")
    result = MagicMock(returncode=1, stderr="Cannot connect to the Docker daemon")
    monkeypatch.setattr(runtime.subprocess, "run", lambda *a, **k: result)
    with pytest.raises(runtime.RuntimeError, match="not reachable"):
        runtime.ensure_daemon()


def test_ensure_daemon_ok(monkeypatch):
    monkeypatch.setattr(runtime.shutil, "which", lambda _: "/usr/bin/docker")
    result = MagicMock(returncode=0, stdout="24.0.7\n", stderr="")
    monkeypatch.setattr(runtime.subprocess, "run", lambda *a, **k: result)
    runtime.ensure_daemon()  # no raise


def test_find_overlay_prefers_config_dir(tmp_path: Path):
    cfg_dir = tmp_path / "sub"
    cfg_dir.mkdir()
    (cfg_dir / "Dockerfile.contained").write_text("FROM contained-base\n")
    (cfg_dir / "contained.yaml").write_text("")
    loaded = LoadedConfig(
        path=cfg_dir / "contained.yaml",
        base_dir=cfg_dir,
        default_agent=None,
        defaults=ConfigSection(),
        agents={},
    )
    r = resolve("claude", loaded, CliOverrides(), cwd=tmp_path)
    assert runtime.find_overlay(r, tmp_path) == cfg_dir / "Dockerfile.contained"


def test_find_overlay_falls_back_to_cwd(tmp_path: Path):
    (tmp_path / "Dockerfile.contained").write_text("FROM contained-base\n")
    r = _resolved(tmp_path)
    assert runtime.find_overlay(r, tmp_path) == tmp_path / "Dockerfile.contained"


def test_find_overlay_absent(tmp_path: Path):
    r = _resolved(tmp_path)
    assert runtime.find_overlay(r, tmp_path) is None


def test_overlay_tag_changes_when_file_changes(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(runtime, "_base_image_id", lambda img: img)
    df = tmp_path / "Dockerfile.contained"
    df.write_text("FROM contained-base\nRUN echo a\n")
    t1 = runtime.overlay_tag("claude", df, "base:1")
    df.write_text("FROM contained-base\nRUN echo b\n")
    t2 = runtime.overlay_tag("claude", df, "base:1")
    assert t1 != t2


def test_overlay_tag_changes_when_base_changes(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(runtime, "_base_image_id", lambda img: img)
    df = tmp_path / "Dockerfile.contained"
    df.write_text("FROM contained-base\n")
    t1 = runtime.overlay_tag("claude", df, "base:1")
    t2 = runtime.overlay_tag("claude", df, "base:2")
    assert t1 != t2


def test_build_overlay_uses_cache_when_image_exists(tmp_path: Path, monkeypatch):
    df = tmp_path / "Dockerfile.contained"
    df.write_text("FROM contained-base\n")
    monkeypatch.setattr(runtime, "_base_image_id", lambda img: img)
    monkeypatch.setattr(runtime, "_image_exists", lambda tag: True)
    called = []
    monkeypatch.setattr(
        runtime.subprocess, "run",
        lambda *a, **k: called.append(a) or MagicMock(returncode=0),
    )
    runtime.build_overlay("claude", df, "base:1", rebuild=False)
    assert called == []  # no build invoked


def test_build_overlay_rebuilds_when_forced(tmp_path: Path, monkeypatch):
    df = tmp_path / "Dockerfile.contained"
    df.write_text("FROM contained-base\n")
    monkeypatch.setattr(runtime, "_base_image_id", lambda img: img)
    monkeypatch.setattr(runtime, "_image_exists", lambda tag: True)
    calls: list[list[str]] = []

    def fake_run(cmd, **kw):
        calls.append(cmd)
        return MagicMock(returncode=0)

    monkeypatch.setattr(runtime.subprocess, "run", fake_run)
    runtime.build_overlay("claude", df, "base:1", rebuild=True)
    assert calls and calls[0][:2] == ["docker", "build"]


def test_build_overlay_rejects_bad_from(tmp_path: Path, monkeypatch):
    df = tmp_path / "Dockerfile.contained"
    df.write_text("FROM debian:bookworm\n")
    monkeypatch.setattr(runtime, "_base_image_id", lambda img: img)
    monkeypatch.setattr(runtime, "_image_exists", lambda tag: False)
    with pytest.raises(runtime.RuntimeError, match="FROM contained-base"):
        runtime.build_overlay("claude", df, "base:1", rebuild=False)


def test_run_end_to_end(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(runtime, "ensure_daemon", lambda: None)
    captured: dict[str, list[str]] = {}
    def fake_execute(argv):
        captured["argv"] = argv
        return 0
    monkeypatch.setattr(runtime, "_execute", fake_execute)
    r = _resolved(tmp_path)
    rc = runtime.run(r, tmp_path)
    assert rc == 0
    assert captured["argv"][0] == "docker"


def test_build_base_default_tag(monkeypatch):
    monkeypatch.setattr(runtime.shutil, "which", lambda _: "/usr/bin/docker")
    calls: list[list[str]] = []

    def fake_run(cmd, **kw):
        calls.append(cmd)
        return MagicMock(returncode=0)

    monkeypatch.setattr(runtime.subprocess, "run", fake_run)
    tag = runtime.build_base()
    assert tag == profiles.BASE_IMAGE
    assert calls[0][:2] == ["docker", "build"]
    assert "-t" in calls[0]
    assert profiles.BASE_IMAGE in calls[0]
    assert "--no-cache" not in calls[0]


def test_build_base_custom_tag_and_rebuild(monkeypatch):
    monkeypatch.setattr(runtime.shutil, "which", lambda _: "/usr/bin/docker")
    calls: list[list[str]] = []
    monkeypatch.setattr(
        runtime.subprocess, "run",
        lambda cmd, **kw: calls.append(cmd) or MagicMock(returncode=0),
    )
    tag = runtime.build_base("my/tag:dev", rebuild=True)
    assert tag == "my/tag:dev"
    assert "my/tag:dev" in calls[0]
    assert "--no-cache" in calls[0]


def test_build_base_missing_docker(monkeypatch):
    monkeypatch.setattr(runtime.shutil, "which", lambda _: None)
    with pytest.raises(runtime.RuntimeError, match="docker binary not found"):
        runtime.build_base()


def test_build_base_failed(monkeypatch):
    monkeypatch.setattr(runtime.shutil, "which", lambda _: "/usr/bin/docker")
    monkeypatch.setattr(
        runtime.subprocess, "run", lambda *a, **k: MagicMock(returncode=2)
    )
    with pytest.raises(runtime.RuntimeError, match="build failed"):
        runtime.build_base()


def test_base_dockerfile_asset_present():
    path = runtime.base_dockerfile_path()
    assert path.is_file()
    assert "FROM debian" in path.read_text().splitlines()[10] or "FROM" in path.read_text()


def test_run_builds_overlay_when_present(tmp_path: Path, monkeypatch):
    (tmp_path / "Dockerfile.contained").write_text("FROM contained-base\n")
    monkeypatch.setattr(runtime, "ensure_daemon", lambda: None)
    monkeypatch.setattr(runtime, "build_overlay", lambda *a, **k: "overlay:abc")
    captured: dict[str, list[str]] = {}
    def fake_execute(argv):
        captured["argv"] = argv
        return 0
    monkeypatch.setattr(runtime, "_execute", fake_execute)
    r = _resolved(tmp_path)
    runtime.run(r, tmp_path)
    assert "overlay:abc" in captured["argv"]
