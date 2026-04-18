"""Built-in agent profile defaults.

A profile provides the starting layer for config merging: image, entrypoint,
env vars to forward, mounts, allowlist entries. PRD 05 defines the shape.

Profiles are deliberately data-only here — no Docker interaction.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .config import ConfigError


@dataclass(frozen=True)
class FileSeed:
    """A host file to seed into state on first run.

    ``sources`` is a list of source specs tried in order; the first one
    that resolves wins. A spec is either a host filesystem path (may
    start with ``~``) or ``keychain:<service>`` for the macOS login
    keychain (via ``security find-generic-password``).

    ``state_rel`` is the destination, relative to the per-project
    state dir (the parent of the agent state dir) — or, when
    ``is_global`` is set, relative to the tool-wide global state dir
    shared across all projects. ``container_path`` is where the file
    should appear inside the container.

    If ``container_path`` is under the agent's ``state_mount`` and the
    seed is per-project, it is already covered by the directory-level
    state mount and no extra bind is needed. Otherwise (or always for
    ``is_global=True``) a file-level bind is added so the global file
    overlays the per-project state mount.

    ``fallback_content`` is written as a placeholder when no source
    resolves AND a file-level bind is required — so the mount target
    exists and the container's writes persist across runs. For files
    that live under ``state_mount`` (per-project only) the container
    can create them itself, so the fallback is unused.
    """

    sources: tuple[str, ...]
    state_rel: str
    container_path: str
    fallback_content: bytes = b""
    is_global: bool = False


@dataclass(frozen=True)
class AgentProfile:
    name: str
    image: str
    entrypoint: list[str] = field(default_factory=list)
    env: list[str] = field(default_factory=list)
    required_env: list[str] = field(default_factory=list)
    mounts: list[str] = field(default_factory=list)
    mounts_ro: list[str] = field(default_factory=list)
    args: list[str] = field(default_factory=list)
    allowlist: list[str] = field(default_factory=list)
    workdir: str = "/workspace"
    state_mount: str | None = None
    file_seeds: list[FileSeed] = field(default_factory=list)


BASE_IMAGE = "ghcr.io/contained-ai/contained-base:edge"
PROXY_IMAGE = "ghcr.io/contained-ai/contained-proxy:edge"


# Claude Code OAuth token, shared tool-wide so every container (every
# project, every concurrent run, every agent profile) reads and writes
# the same credential — token refreshes propagate automatically. A
# file-level bind overlays the per-project state_mount at the same
# path. macOS Claude Code stores the token in the login keychain; the
# Linux distribution stores it at ~/.claude/.credentials.json. Either
# source works — whichever the host has.
CLAUDE_CREDENTIALS_SEED = FileSeed(
    sources=(
        "keychain:Claude Code-credentials",
        "~/.claude/.credentials.json",
    ),
    state_rel="claude/.credentials.json",
    container_path="/home/agent/.claude/.credentials.json",
    is_global=True,
)

# pi coding-agent OAuth token, shared tool-wide the same way as
# CLAUDE_CREDENTIALS_SEED. pi writes its login state to
# ~/.pi/agent/auth.json (0600) after `pi /login`; all other files in
# ~/.pi/ (settings, session history) stay per-project via the
# state_mount.
PI_CREDENTIALS_SEED = FileSeed(
    sources=("~/.pi/agent/auth.json",),
    state_rel="pi/agent/auth.json",
    container_path="/home/agent/.pi/agent/auth.json",
    is_global=True,
)


CLAUDE = AgentProfile(
    name="claude",
    image=BASE_IMAGE,
    entrypoint=["claude"],
    env=["ANTHROPIC_API_KEY", "CLAUDE_MODEL"],
    # The container is already sandboxed (no host fs access, egress
    # allowlist, non-root, cap-drop all), so Claude's per-call permission
    # gating adds friction without meaningful protection. Default to
    # bypassPermissions; override in contained.yaml if a project wants
    # the interactive prompts back.
    args=["--permission-mode", "bypassPermissions"],
    allowlist=["api.anthropic.com:443", "platform.claude.com:443"],
    state_mount="/home/agent/.claude",
    file_seeds=[
        CLAUDE_CREDENTIALS_SEED,
        PI_CREDENTIALS_SEED,
        # Onboarding / per-project config (theme, tips history,
        # hasCompletedOnboarding, etc.) lives at ~/.claude.json —
        # outside ~/.claude/ — so it needs its own file-level bind.
        # Falls back to an empty JSON object if the host has none yet,
        # so the container's first-run state persists for next time.
        FileSeed(
            sources=("~/.claude.json",),
            state_rel="claude.json",
            container_path="/home/agent/.claude.json",
            fallback_content=b"{}\n",
        ),
    ],
)

PI = AgentProfile(
    name="pi",
    image=BASE_IMAGE,
    entrypoint=["pi"],
    env=["OPENAI_API_KEY", "ANTHROPIC_API_KEY"],
    allowlist=["api.openai.com:443", "api.anthropic.com:443"],
    state_mount="/home/agent/.pi",
    # Both credentials shared tool-wide: pi may drive Claude as a
    # sub-agent, and the user may want to reuse a host-side `pi /login`
    # across projects and parallel sessions.
    file_seeds=[CLAUDE_CREDENTIALS_SEED, PI_CREDENTIALS_SEED],
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


TOOL_DEFAULT_ENV: list[str] = [
    "TERM",
    # Terminal-identification vars that agents (Claude Code in
    # particular) sniff to decide which input-parsing mode to enable.
    # Without TERM_PROGRAM set, Claude Code ignores the \e\r sequence
    # that iTerm2 / tmux / VSCode send for Shift-Enter, even when
    # shiftEnterKeyBindingInstalled is true in ~/.claude.json.
    "TERM_PROGRAM",
    "TERM_PROGRAM_VERSION",
    "COLORTERM",
    "LANG",
    "LC_ALL",
    "TZ",
]

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
