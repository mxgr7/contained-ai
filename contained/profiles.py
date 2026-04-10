"""Built-in agent profile defaults.

A profile provides the starting layer for config merging: image, entrypoint,
env vars to forward, mounts, allowlist entries. PRD 05 defines the shape.

Profiles are deliberately data-only here — no Docker interaction.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .config import ConfigError


@dataclass(frozen=True)
class AgentProfile:
    name: str
    image: str
    entrypoint: list[str] = field(default_factory=list)
    env: list[str] = field(default_factory=list)
    mounts: list[str] = field(default_factory=list)
    mounts_ro: list[str] = field(default_factory=list)
    allowlist: list[str] = field(default_factory=list)
    workdir: str = "/workspace"
    state_mount: str | None = None
    # Host files to copy into the per-project state dir on first run so the
    # agent finds its credentials without the container being able to
    # rewrite the host's canonical copy. Maps host path (may start with ~)
    # to a destination relative to the state mount root.
    credential_seeds: dict[str, str] = field(default_factory=dict)


BASE_IMAGE = "ghcr.io/contained-ai/contained-base:edge"
PROXY_IMAGE = "ghcr.io/contained-ai/contained-proxy:edge"

CLAUDE = AgentProfile(
    name="claude",
    image=BASE_IMAGE,
    entrypoint=["claude"],
    env=["ANTHROPIC_API_KEY", "CLAUDE_MODEL"],
    allowlist=["api.anthropic.com:443"],
    state_mount="/home/agent/.claude",
    credential_seeds={"~/.claude/.credentials.json": ".credentials.json"},
)

PI = AgentProfile(
    name="pi",
    image=BASE_IMAGE,
    entrypoint=["pi"],
    env=["OPENAI_API_KEY", "ANTHROPIC_API_KEY"],
    allowlist=["api.openai.com:443", "api.anthropic.com:443"],
    state_mount="/home/agent/.pi",
)


_PROFILES: dict[str, AgentProfile] = {
    CLAUDE.name: CLAUDE,
    PI.name: PI,
}


def get(name: str) -> AgentProfile:
    try:
        return _PROFILES[name]
    except KeyError as e:
        available = ", ".join(sorted(_PROFILES)) or "(none)"
        raise ConfigError(f"unknown agent '{name}'. available: {available}") from e


def names() -> list[str]:
    return sorted(_PROFILES)


TOOL_DEFAULT_ENV: list[str] = ["TERM", "LANG", "LC_ALL", "TZ"]

TOOL_DEFAULT_ALLOWLIST: list[str] = [
    "github.com:443",
    "codeload.github.com:443",
    "raw.githubusercontent.com:443",
    "objects.githubusercontent.com:443",
    "registry.npmjs.org:443",
    "pypi.org:443",
    "files.pythonhosted.org:443",
    "proxy.golang.org:443",
    "sum.golang.org:443",
    "crates.io:443",
    "static.crates.io:443",
]
