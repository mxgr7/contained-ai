"""contained.yaml discovery, parsing, validation.

PRD 01 defines the schema. Unknown keys are a hard error so typos fail
loudly. Host paths are resolved relative to the config file's directory
(or $PWD when config came from `--config`). `~` and `${VAR}` are expanded
at load time.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

CONFIG_FILENAME = "contained.yaml"

_TOP_LEVEL_KEYS = {"default_agent", "agents", "defaults", "ssh"}
_SECTION_KEYS = {"image", "env", "mounts", "mounts_ro", "allowlist", "network", "args"}
_SSH_KEYS = {"allowlist"}


class ConfigError(ValueError):
    """Raised for any malformed contained.yaml."""


@dataclass
class ConfigSection:
    """One layer of config (either a per-agent block or the defaults block)."""

    image: str | None = None
    env: list[str] = field(default_factory=list)
    mounts: list[str] = field(default_factory=list)
    mounts_ro: list[str] = field(default_factory=list)
    allowlist: list[str] = field(default_factory=list)
    network: str | None = None
    args: list[str] = field(default_factory=list)


@dataclass
class LoadedConfig:
    path: Path | None
    base_dir: Path
    default_agent: str | None
    defaults: ConfigSection
    agents: dict[str, ConfigSection]
    ssh_allowlist: list[str] = field(default_factory=list)

    @classmethod
    def empty(cls, base_dir: Path) -> LoadedConfig:
        return cls(
            path=None,
            base_dir=base_dir,
            default_agent=None,
            defaults=ConfigSection(),
            agents={},
            ssh_allowlist=[],
        )

    def for_agent(self, name: str) -> ConfigSection:
        return self.agents.get(name, ConfigSection())


def discover(start: Path) -> Path | None:
    """Walk upward from `start` looking for contained.yaml. Return path or None."""
    start = start.resolve()
    for d in (start, *start.parents):
        candidate = d / CONFIG_FILENAME
        if candidate.is_file():
            return candidate
    return None


def load(path: Path | None, *, cwd: Path) -> LoadedConfig:
    """Load and validate a contained.yaml. `path=None` returns an empty config."""
    if path is None:
        return LoadedConfig.empty(base_dir=cwd.resolve())

    path = path.resolve()
    try:
        raw = yaml.safe_load(path.read_text()) or {}
    except yaml.YAMLError as e:
        raise ConfigError(f"{path}: invalid YAML: {e}") from e

    if not isinstance(raw, dict):
        raise ConfigError(f"{path}: top-level must be a mapping")

    unknown = set(raw) - _TOP_LEVEL_KEYS
    if unknown:
        raise ConfigError(f"{path}: unknown top-level keys: {sorted(unknown)}")

    default_agent = raw.get("default_agent")
    if default_agent is not None and not isinstance(default_agent, str):
        raise ConfigError(f"{path}: default_agent must be a string")

    defaults = _parse_section(raw.get("defaults", {}), path, "defaults")

    agents_raw = raw.get("agents", {})
    if not isinstance(agents_raw, dict):
        raise ConfigError(f"{path}: agents must be a mapping")
    agents: dict[str, ConfigSection] = {}
    for name, block in agents_raw.items():
        if not isinstance(name, str):
            raise ConfigError(f"{path}: agent name must be a string, got {name!r}")
        agents[name] = _parse_section(block, path, f"agents.{name}")

    ssh_allowlist = _parse_ssh_block(raw.get("ssh"), path)

    return LoadedConfig(
        path=path,
        base_dir=path.parent,
        default_agent=default_agent,
        defaults=defaults,
        agents=agents,
        ssh_allowlist=ssh_allowlist,
    )


def _parse_ssh_block(block: Any, path: Path) -> list[str]:
    if block is None:
        return []
    if not isinstance(block, dict):
        raise ConfigError(f"{path}: ssh must be a mapping")
    unknown = set(block) - _SSH_KEYS
    if unknown:
        raise ConfigError(f"{path}: unknown keys in ssh: {sorted(unknown)}")
    return _str_list(block.get("allowlist"), path, "ssh.allowlist", expand=True)


def _parse_section(block: Any, path: Path, where: str) -> ConfigSection:
    if block is None:
        return ConfigSection()
    if not isinstance(block, dict):
        raise ConfigError(f"{path}: {where} must be a mapping")
    unknown = set(block) - _SECTION_KEYS
    if unknown:
        raise ConfigError(f"{path}: unknown keys in {where}: {sorted(unknown)}")

    image = block.get("image")
    if image is not None and not isinstance(image, str):
        raise ConfigError(f"{path}: {where}.image must be a string")

    network = block.get("network")
    if network is not None:
        if not isinstance(network, str) or network not in {"host", "none", "allowlist"}:
            raise ConfigError(
                f"{path}: {where}.network must be one of host|none|allowlist"
            )

    return ConfigSection(
        image=_expand(image) if isinstance(image, str) else None,
        env=_str_list(block.get("env"), path, f"{where}.env", expand=True),
        mounts=_str_list(block.get("mounts"), path, f"{where}.mounts", expand=True),
        mounts_ro=_str_list(
            block.get("mounts_ro"), path, f"{where}.mounts_ro", expand=True
        ),
        allowlist=_str_list(
            block.get("allowlist"), path, f"{where}.allowlist", expand=True
        ),
        network=network,
        args=_str_list(block.get("args"), path, f"{where}.args", expand=False),
    )


def _str_list(value: Any, path: Path, where: str, *, expand: bool) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, list):
        raise ConfigError(f"{path}: {where} must be a list")
    out: list[str] = []
    for i, item in enumerate(value):
        if not isinstance(item, str):
            raise ConfigError(f"{path}: {where}[{i}] must be a string")
        out.append(_expand(item) if expand else item)
    return out


def _expand(s: str) -> str:
    return os.path.expanduser(os.path.expandvars(s))
