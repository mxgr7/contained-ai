from pathlib import Path

import pytest

from contained.config import ConfigError, discover, load


def write(tmp_path: Path, name: str, body: str) -> Path:
    p = tmp_path / name
    p.write_text(body)
    return p


def test_load_none_returns_empty(tmp_path: Path):
    cfg = load(None, cwd=tmp_path)
    assert cfg.path is None
    assert cfg.default_agent is None
    assert cfg.defaults.env == []
    assert cfg.agents == {}


def test_load_full_schema(tmp_path: Path):
    path = write(
        tmp_path,
        "contained.yaml",
        """
default_agent: claude
defaults:
  env: [TERM, LANG]
  mounts: [./:/workspace]
  network: allowlist
  allowlist: [github.com:443]
agents:
  claude:
    image: ghcr.io/example/contained-claude:edge
    env: [ANTHROPIC_API_KEY, CLAUDE_MODEL=claude-opus-4-6]
    mounts_ro: ["~/.gitconfig:/root/.gitconfig"]
    allowlist: [api.anthropic.com:443]
""",
    )
    cfg = load(path, cwd=tmp_path)
    assert cfg.default_agent == "claude"
    assert cfg.defaults.env == ["TERM", "LANG"]
    assert cfg.defaults.network == "allowlist"
    assert "claude" in cfg.agents
    assert cfg.agents["claude"].image == "ghcr.io/example/contained-claude:edge"
    assert cfg.agents["claude"].env == ["ANTHROPIC_API_KEY", "CLAUDE_MODEL=claude-opus-4-6"]
    # ~ expanded
    assert not cfg.agents["claude"].mounts_ro[0].startswith("~")


def test_unknown_top_level_key_fails(tmp_path: Path):
    path = write(tmp_path, "contained.yaml", "bogus_key: 1\n")
    with pytest.raises(ConfigError, match="unknown top-level"):
        load(path, cwd=tmp_path)


def test_unknown_section_key_fails(tmp_path: Path):
    path = write(
        tmp_path,
        "contained.yaml",
        "defaults:\n  typo: true\n",
    )
    with pytest.raises(ConfigError, match="unknown keys in defaults"):
        load(path, cwd=tmp_path)


def test_invalid_network_value(tmp_path: Path):
    path = write(tmp_path, "contained.yaml", "defaults:\n  network: wifi\n")
    with pytest.raises(ConfigError, match="network"):
        load(path, cwd=tmp_path)


def test_discover_walks_upward(tmp_path: Path):
    (tmp_path / "a" / "b").mkdir(parents=True)
    root_cfg = write(tmp_path, "contained.yaml", "default_agent: claude\n")
    found = discover(tmp_path / "a" / "b")
    assert found == root_cfg.resolve()


def test_discover_returns_none_when_absent(tmp_path: Path):
    assert discover(tmp_path) is None


def test_env_var_expansion(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("SOMEDIR", "/opt/thing")
    path = write(
        tmp_path,
        "contained.yaml",
        "defaults:\n  mounts: ['${SOMEDIR}:/thing']\n",
    )
    cfg = load(path, cwd=tmp_path)
    assert cfg.defaults.mounts == ["/opt/thing:/thing"]
