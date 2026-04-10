from pathlib import Path

import pytest

from contained.config import ConfigError, load
from contained.run import CliOverrides, render_dry_run, resolve


def test_resolve_with_no_config_uses_profile(tmp_path: Path):
    loaded = load(None, cwd=tmp_path)
    r = resolve("claude", loaded, CliOverrides(), cwd=tmp_path)
    assert r.agent.name == "claude"
    assert r.image.startswith("ghcr.io/")
    # Workspace mount defaulted in
    assert any(m.container == "/workspace" for m in r.mounts)
    # Tool-wide env defaults present
    keys = [e.key for e in r.env]
    assert "TERM" in keys
    assert "ANTHROPIC_API_KEY" in keys


def test_cli_flags_override_config(tmp_path: Path):
    (tmp_path / "contained.yaml").write_text(
        "agents:\n  claude:\n    image: from-config\n"
    )
    loaded = load(tmp_path / "contained.yaml", cwd=tmp_path)
    r = resolve(
        "claude",
        loaded,
        CliOverrides(image="from-flag"),
        cwd=tmp_path,
    )
    assert r.image == "from-flag"


def test_list_fields_union_across_layers(tmp_path: Path):
    (tmp_path / "contained.yaml").write_text(
        "defaults:\n  allowlist: [example.com:443]\n"
    )
    loaded = load(tmp_path / "contained.yaml", cwd=tmp_path)
    r = resolve(
        "claude",
        loaded,
        CliOverrides(allow=["extra.com:443"]),
        cwd=tmp_path,
    )
    assert "example.com:443" in r.allowlist
    assert "extra.com:443" in r.allowlist
    # Built-in defaults still present
    assert "github.com:443" in r.allowlist


def test_default_agent_from_config(tmp_path: Path):
    (tmp_path / "contained.yaml").write_text("default_agent: pi\n")
    loaded = load(tmp_path / "contained.yaml", cwd=tmp_path)
    r = resolve(None, loaded, CliOverrides(), cwd=tmp_path)
    assert r.agent.name == "pi"


def test_missing_agent_errors(tmp_path: Path):
    loaded = load(None, cwd=tmp_path)
    with pytest.raises(ConfigError, match="no agent specified"):
        resolve(None, loaded, CliOverrides(), cwd=tmp_path)


def test_env_value_masked_for_secrets(tmp_path: Path):
    loaded = load(None, cwd=tmp_path)
    r = resolve(
        "claude",
        loaded,
        CliOverrides(env=["ANTHROPIC_API_KEY=supersecret", "LANG=en_US.UTF-8"]),
        cwd=tmp_path,
    )
    out = render_dry_run(r, host_env={})
    assert "supersecret" not in out
    assert "***" in out
    assert "en_US.UTF-8" in out


def test_env_from_file(tmp_path: Path):
    envfile = tmp_path / ".env"
    envfile.write_text("# comment\nFOO=bar\nBAZ=qux\n")
    loaded = load(None, cwd=tmp_path)
    r = resolve(
        "claude",
        loaded,
        CliOverrides(env_from=[envfile]),
        cwd=tmp_path,
    )
    keys = {e.key: e.value for e in r.env}
    assert keys.get("FOO") == "bar"
    assert keys.get("BAZ") == "qux"


def test_network_override(tmp_path: Path):
    loaded = load(None, cwd=tmp_path)
    r = resolve("claude", loaded, CliOverrides(network="none"), cwd=tmp_path)
    assert r.network == "none"


def test_default_workspace_mount_refuses_root(tmp_path: Path):
    loaded = load(None, cwd=tmp_path)
    with pytest.raises(ConfigError, match="host root"):
        resolve("claude", loaded, CliOverrides(), cwd=Path("/"))


def test_default_workspace_mount_refuses_home(tmp_path: Path, monkeypatch):
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    monkeypatch.setenv("HOME", str(fake_home))
    loaded = load(None, cwd=fake_home)
    with pytest.raises(ConfigError, match="home directory"):
        resolve("claude", loaded, CliOverrides(), cwd=fake_home)


def test_user_mount_under_workspace_does_not_suppress_default(tmp_path: Path):
    sub = tmp_path / "fixtures"
    sub.mkdir()
    loaded = load(None, cwd=tmp_path)
    r = resolve(
        "claude",
        loaded,
        CliOverrides(mounts=[f"{sub}:/workspace/fixtures"]),
        cwd=tmp_path,
    )
    # Default $PWD -> /workspace mount is still present.
    assert any(
        m.container == "/workspace" and m.host == tmp_path for m in r.mounts
    )
    assert any(m.container == "/workspace/fixtures" for m in r.mounts)
