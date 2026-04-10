"""Resolve configuration for a `contained run` invocation and render it.

Merge precedence (high → low) from PRD 01:
  1. CLI flags
  2. agents.<name>.* in contained.yaml
  3. defaults.* in contained.yaml
  4. Agent profile built-in defaults
  5. Tool-wide built-in defaults

List-valued fields union across layers.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from . import profiles
from .config import ConfigError, LoadedConfig
from .profiles import AgentProfile


@dataclass
class Mount:
    host: Path
    container: str
    read_only: bool

    def format(self) -> str:
        suffix = ":ro" if self.read_only else ""
        return f"{self.host}:{self.container}{suffix}"


@dataclass
class EnvVar:
    key: str
    value: str | None  # None => forward from host at run time
    from_host: bool

    def resolve_value(self, host_env: dict[str, str]) -> str | None:
        if self.from_host:
            return host_env.get(self.key)
        return self.value


@dataclass
class ResolvedRun:
    agent: AgentProfile
    image: str
    mounts: list[Mount] = field(default_factory=list)
    env: list[EnvVar] = field(default_factory=list)
    network: str = "allowlist"
    allowlist: list[str] = field(default_factory=list)
    workdir: str = "/workspace"
    passthrough_args: list[str] = field(default_factory=list)
    config_path: Path | None = None


@dataclass
class CliOverrides:
    """Raw flag values from argparse, pre-merge."""

    mounts: list[str] = field(default_factory=list)
    mounts_ro: list[str] = field(default_factory=list)
    env: list[str] = field(default_factory=list)
    env_from: list[Path] = field(default_factory=list)
    network: str | None = None
    allow: list[str] = field(default_factory=list)
    image: str | None = None
    passthrough: list[str] = field(default_factory=list)


def resolve(
    agent_name: str | None,
    loaded: LoadedConfig,
    overrides: CliOverrides,
    cwd: Path,
) -> ResolvedRun:
    """Fold every layer into a single ResolvedRun."""
    if agent_name is None:
        agent_name = loaded.default_agent
    if agent_name is None:
        raise ConfigError(
            "no agent specified and no default_agent in contained.yaml. "
            "pass one: `contained run <agent>`"
        )

    profile = profiles.get(agent_name)

    # Layer 5: tool-wide defaults
    env_strs: list[str] = list(profiles.TOOL_DEFAULT_ENV)
    allow: list[str] = list(profiles.TOOL_DEFAULT_ALLOWLIST)
    mounts_rw: list[str] = []
    mounts_ro: list[str] = []
    network = "allowlist"
    image = profile.image

    # Layer 4: agent profile
    env_strs = _merge_env(env_strs, profile.env)
    allow = _union(allow, profile.allowlist)
    mounts_rw = _union(mounts_rw, profile.mounts)
    mounts_ro = _union(mounts_ro, profile.mounts_ro)
    # Default workspace mount: $PWD → /workspace rw. Added only if nothing
    # already mounts /workspace from a higher-priority layer.
    workspace_default = f"{cwd}:/workspace"

    # Layer 3: defaults section
    d = loaded.defaults
    env_strs = _merge_env(env_strs, d.env)
    allow = _union(allow, d.allowlist)
    mounts_rw = _union(mounts_rw, d.mounts)
    mounts_ro = _union(mounts_ro, d.mounts_ro)
    if d.network is not None:
        network = d.network
    if d.image is not None:
        image = d.image

    # Layer 2: agents.<name>
    a = loaded.for_agent(agent_name)
    env_strs = _merge_env(env_strs, a.env)
    allow = _union(allow, a.allowlist)
    mounts_rw = _union(mounts_rw, a.mounts)
    mounts_ro = _union(mounts_ro, a.mounts_ro)
    if a.network is not None:
        network = a.network
    if a.image is not None:
        image = a.image

    # Layer 1: CLI flags
    env_strs = _merge_env(env_strs, overrides.env)
    env_strs = _merge_env(env_strs, _load_env_files(overrides.env_from))
    allow = _union(allow, overrides.allow)
    mounts_rw = _union(mounts_rw, overrides.mounts)
    mounts_ro = _union(mounts_ro, overrides.mounts_ro)
    if overrides.network is not None:
        network = overrides.network
    if overrides.image is not None:
        image = overrides.image

    mounts: list[Mount] = []
    has_workspace = any(":/workspace" in m or m.endswith(":/workspace") for m in mounts_rw + mounts_ro)
    if not has_workspace:
        mounts.append(_parse_mount(workspace_default, loaded.base_dir, read_only=False))
    for m in mounts_rw:
        mounts.append(_parse_mount(m, loaded.base_dir, read_only=False))
    for m in mounts_ro:
        mounts.append(_parse_mount(m, loaded.base_dir, read_only=True))

    env: list[EnvVar] = [_parse_env(e) for e in env_strs]

    return ResolvedRun(
        agent=profile,
        image=image,
        mounts=mounts,
        env=env,
        network=network,
        allowlist=allow,
        workdir=profile.workdir,
        passthrough_args=list(overrides.passthrough),
        config_path=loaded.path,
    )


def _union(a: list[str], b: list[str]) -> list[str]:
    """Append items from b that aren't already in a. Order-preserving."""
    seen = set(a)
    out = list(a)
    for item in b:
        if item not in seen:
            seen.add(item)
            out.append(item)
    return out


def _merge_env(a: list[str], b: list[str]) -> list[str]:
    """Merge env specs, keyed by KEY. Later layer (b) wins on conflict."""
    def key_of(spec: str) -> str:
        return spec.split("=", 1)[0]

    b_keys = {key_of(s) for s in b}
    out = [s for s in a if key_of(s) not in b_keys]
    for s in b:
        if s not in out:
            out.append(s)
    return out


def _parse_mount(spec: str, base_dir: Path, *, read_only: bool) -> Mount:
    if ":" not in spec:
        raise ConfigError(f"mount spec must be host:container, got {spec!r}")
    host_raw, container = spec.split(":", 1)
    host = Path(os.path.expanduser(os.path.expandvars(host_raw)))
    if not host.is_absolute():
        host = (base_dir / host).resolve()
    return Mount(host=host, container=container, read_only=read_only)


def _parse_env(spec: str) -> EnvVar:
    if "=" in spec:
        key, value = spec.split("=", 1)
        return EnvVar(key=key, value=value, from_host=False)
    return EnvVar(key=spec, value=None, from_host=True)


def _load_env_files(paths: list[Path]) -> list[str]:
    out: list[str] = []
    for p in paths:
        if not p.is_file():
            raise ConfigError(f"--env-from: no such file: {p}")
        for raw in p.read_text().splitlines():
            line = raw.strip()
            if not line or line.startswith("#"):
                continue
            if "=" not in line:
                raise ConfigError(f"{p}: malformed env line: {raw!r}")
            out.append(line)
    return out


_SENSITIVE_HINTS = ("KEY", "TOKEN", "SECRET", "PASSWORD", "PASS", "CREDENTIAL")


def _mask(key: str, value: str | None) -> str:
    if value is None:
        return "<from host>"
    if any(h in key.upper() for h in _SENSITIVE_HINTS):
        return "***"
    return value


def render_dry_run(run: ResolvedRun, host_env: dict[str, str]) -> str:
    lines: list[str] = []
    lines.append(f"# contained run {run.agent.name} — resolved config")
    if run.config_path:
        lines.append(f"# config: {run.config_path}")
    else:
        lines.append("# config: (none — built-in defaults)")
    lines.append("")
    lines.append(f"image:   {run.image}")
    lines.append(f"workdir: {run.workdir}")
    lines.append(f"network: {run.network}")
    lines.append("")
    lines.append("mounts:")
    for m in run.mounts:
        tag = "ro" if m.read_only else "rw"
        lines.append(f"  - [{tag}] {m.host} -> {m.container}")
    lines.append("")
    lines.append("env:")
    for e in run.env:
        value = e.resolve_value(host_env)
        lines.append(f"  - {e.key}={_mask(e.key, value)}")
    lines.append("")
    lines.append(f"allowlist ({len(run.allowlist)}):")
    for entry in run.allowlist:
        lines.append(f"  - {entry}")
    lines.append("")
    lines.append("docker invocation (preview):")
    lines.append("  " + " ".join(_docker_preview(run)))
    return "\n".join(lines)


def _docker_preview(run: ResolvedRun) -> list[str]:
    argv = ["docker", "run", "--rm", "-it", "--init"]
    argv += ["--workdir", run.workdir]
    for m in run.mounts:
        argv += ["--mount", f"type=bind,src={m.host},dst={m.container}" + (",ro" if m.read_only else "")]
    for e in run.env:
        if e.from_host:
            argv += ["--env", e.key]
        else:
            argv += ["--env", f"{e.key}={_mask(e.key, e.value)}"]
    if run.network == "none":
        argv += ["--network", "none"]
    elif run.network == "host":
        argv += ["--network", "host"]
    else:
        argv += ["--network", "contained-net"]
    argv.append(run.image)
    if run.agent.entrypoint:
        argv += run.agent.entrypoint
    argv += run.passthrough_args
    return argv
