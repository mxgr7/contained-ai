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


def test_env_value_masked_for_cli_supplied(tmp_path: Path):
    loaded = load(None, cwd=tmp_path)
    r = resolve(
        "claude",
        loaded,
        CliOverrides(env=["ANTHROPIC_API_KEY=supersecret", "LANG=en_US.UTF-8"]),
        cwd=tmp_path,
    )
    out = render_dry_run(r, host_env={})
    assert "supersecret" not in out
    # Non-sensitive-looking LANG is *also* masked when user-supplied.
    assert "en_US.UTF-8" not in out
    assert "***" in out


def test_env_from_file_masked(tmp_path: Path):
    envfile = tmp_path / ".env"
    envfile.write_text("DATABASE_URL=postgres://pw@host/db\n")
    loaded = load(None, cwd=tmp_path)
    r = resolve(
        "claude", loaded, CliOverrides(env_from=[envfile]), cwd=tmp_path
    )
    out = render_dry_run(r, host_env={})
    assert "postgres://pw@host/db" not in out
    assert "***" in out


def test_env_from_malformed_does_not_echo_value(tmp_path: Path):
    envfile = tmp_path / ".env"
    envfile.write_text("GOOD=ok\nBADLINEVALUE\n")
    loaded = load(None, cwd=tmp_path)
    with pytest.raises(ConfigError) as ei:
        resolve(
            "claude", loaded, CliOverrides(env_from=[envfile]), cwd=tmp_path
        )
    msg = str(ei.value)
    assert "BADLINEVALUE" not in msg
    assert ":2" in msg


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


def test_wildcard_allowlist_rejected(tmp_path: Path):
    loaded = load(None, cwd=tmp_path)
    with pytest.raises(ConfigError, match="wildcards are not supported"):
        resolve(
            "claude",
            loaded,
            CliOverrides(allow=["*.github.com:443"]),
            cwd=tmp_path,
        )


def test_bad_allowlist_host_rejected(tmp_path: Path):
    loaded = load(None, cwd=tmp_path)
    with pytest.raises(ConfigError, match="invalid hostname"):
        resolve(
            "claude",
            loaded,
            CliOverrides(allow=["not a host:443"]),
            cwd=tmp_path,
        )


def test_bad_allowlist_port_rejected(tmp_path: Path):
    loaded = load(None, cwd=tmp_path)
    with pytest.raises(ConfigError, match="port must be numeric"):
        resolve(
            "claude",
            loaded,
            CliOverrides(allow=["example.com:https"]),
            cwd=tmp_path,
        )


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


def test_global_env_file_forwards_provider_keys(tmp_path: Path, monkeypatch):
    """~/.local/share/contained/global/env applies to every run."""
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "xdg"))
    from contained import state
    gfile = state.global_env_file()
    gfile.parent.mkdir(parents=True, exist_ok=True)
    gfile.write_text(
        "# global provider keys\n"
        "OPENAI_API_KEY=sk-global\n"
        "CUSTOM_KEY=value\n"
        "\n"
    )
    loaded = load(None, cwd=tmp_path)
    r = resolve("claude", loaded, CliOverrides(), cwd=tmp_path)
    by_key = {e.key: e for e in r.env}
    assert by_key["OPENAI_API_KEY"].value == "sk-global"
    assert by_key["OPENAI_API_KEY"].from_host is False
    assert by_key["OPENAI_API_KEY"].source == "global env file"
    assert by_key["CUSTOM_KEY"].value == "value"


def test_global_env_file_missing_is_fine(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "xdg"))
    loaded = load(None, cwd=tmp_path)
    r = resolve("claude", loaded, CliOverrides(), cwd=tmp_path)
    # Profile-forwarded key reverts to from-host (no global override).
    by_key = {e.key: e for e in r.env}
    assert by_key["OPENAI_API_KEY"].from_host is True


def test_cli_env_overrides_global_env_file(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "xdg"))
    from contained import state
    gfile = state.global_env_file()
    gfile.parent.mkdir(parents=True, exist_ok=True)
    gfile.write_text("OPENAI_API_KEY=sk-global\n")
    loaded = load(None, cwd=tmp_path)
    r = resolve(
        "claude",
        loaded,
        CliOverrides(env=["OPENAI_API_KEY=sk-flag"]),
        cwd=tmp_path,
    )
    by_key = {e.key: e for e in r.env}
    assert by_key["OPENAI_API_KEY"].value == "sk-flag"
    assert by_key["OPENAI_API_KEY"].source == "--env flag"


def test_contained_yaml_overrides_global_env_file(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "xdg"))
    from contained import state
    gfile = state.global_env_file()
    gfile.parent.mkdir(parents=True, exist_ok=True)
    gfile.write_text("OPENAI_API_KEY=sk-global\n")
    (tmp_path / "contained.yaml").write_text(
        "defaults:\n  env: [OPENAI_API_KEY=sk-project]\n"
    )
    loaded = load(tmp_path / "contained.yaml", cwd=tmp_path)
    r = resolve("claude", loaded, CliOverrides(), cwd=tmp_path)
    by_key = {e.key: e for e in r.env}
    assert by_key["OPENAI_API_KEY"].value == "sk-project"


def test_global_env_file_rejects_malformed_line(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "xdg"))
    from contained import state
    gfile = state.global_env_file()
    gfile.parent.mkdir(parents=True, exist_ok=True)
    gfile.write_text("OPENAI_API_KEY=ok\nnot-an-env-line\n")
    loaded = load(None, cwd=tmp_path)
    with pytest.raises(ConfigError, match="malformed env line"):
        resolve("claude", loaded, CliOverrides(), cwd=tmp_path)


def test_tmux_config_requires_tmux(tmp_path: Path):
    cfg = tmp_path / "tmux"
    cfg.mkdir()
    (cfg / "tmux.conf").write_text("set -g prefix C-b\n")
    loaded = load(None, cwd=tmp_path)
    with pytest.raises(ConfigError, match="--tmux-config and --tmux-prefix"):
        resolve("claude", loaded, CliOverrides(tmux_config=cfg), cwd=tmp_path)


def test_tmux_prefix_requires_tmux(tmp_path: Path):
    loaded = load(None, cwd=tmp_path)
    with pytest.raises(ConfigError, match="--tmux-config and --tmux-prefix"):
        resolve("claude", loaded, CliOverrides(tmux_prefix="C-b"), cwd=tmp_path)


def test_tmux_config_adds_ro_mount(tmp_path: Path):
    cfg = tmp_path / "tmux"
    cfg.mkdir()
    (cfg / "tmux.conf").write_text("set -g prefix C-b\n")
    loaded = load(None, cwd=tmp_path)
    r = resolve(
        "claude", loaded,
        CliOverrides(tmux=True, tmux_config=cfg),
        cwd=tmp_path,
    )
    m = next(m for m in r.mounts if m.container == "/home/agent/.config/tmux")
    assert m.host == cfg.resolve()
    assert m.read_only is True


def test_tmux_config_missing_dir_errors(tmp_path: Path):
    loaded = load(None, cwd=tmp_path)
    with pytest.raises(ConfigError, match="no such directory"):
        resolve(
            "claude", loaded,
            CliOverrides(tmux=True, tmux_config=tmp_path / "nope"),
            cwd=tmp_path,
        )


def test_tmux_config_missing_tmux_conf_errors(tmp_path: Path):
    cfg = tmp_path / "tmux"
    cfg.mkdir()
    loaded = load(None, cwd=tmp_path)
    with pytest.raises(ConfigError, match="does not contain a tmux.conf"):
        resolve(
            "claude", loaded,
            CliOverrides(tmux=True, tmux_config=cfg),
            cwd=tmp_path,
        )


def test_tmux_prefix_dry_run_shows_wrapper(tmp_path: Path):
    cfg = tmp_path / "tmux"
    cfg.mkdir()
    (cfg / "tmux.conf").write_text("set -g prefix C-a\n")
    loaded = load(None, cwd=tmp_path)
    r = resolve(
        "claude", loaded,
        CliOverrides(tmux=True, tmux_config=cfg, tmux_prefix="C-b"),
        cwd=tmp_path,
    )
    out = render_dry_run(r, host_env={})
    assert "tmux_wrapper" in out
    assert "source-file /home/agent/.config/tmux/tmux.conf" in out
    assert "set -g prefix C-b" in out
    assert "bind C-b send-prefix" in out


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
