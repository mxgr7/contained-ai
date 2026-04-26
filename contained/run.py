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
import re
from dataclasses import dataclass, field
from pathlib import Path

from . import profiles, proxy, state
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
    source: str = "tool defaults"

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
    rebuild: bool = False
    no_state: bool = False
    warnings: list[str] = field(default_factory=list)
    planned_seeds: list["state.PlannedSeed"] = field(default_factory=list)
    # PRD 09 — Git over SSH
    ssh_allowlist: list[str] = field(default_factory=list)
    ssh_key_host_path: Path | None = None
    ssh_auth_sock_host_path: str | None = None
    tmux: bool = False
    tmux_prefix: str | None = None


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
    rebuild: bool = False
    no_state: bool = False
    allow_home_mount: bool = False
    # PRD 09 — Git over SSH
    allow_ssh: list[str] = field(default_factory=list)
    ssh_key: Path | None = None
    tmux: bool = False
    tmux_config: Path | None = None
    tmux_prefix: str | None = None


def resolve(
    agent_name: str | None,
    loaded: LoadedConfig,
    overrides: CliOverrides,
    cwd: Path,
    host_env: dict[str, str] | None = None,
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
    env_specs: list[tuple[str, str]] = [
        (s, "tool defaults") for s in profiles.TOOL_DEFAULT_ENV
    ]
    allow: list[str] = list(profiles.TOOL_DEFAULT_ALLOWLIST)
    mounts_rw: list[str] = []
    mounts_ro: list[str] = []
    args: list[str] = []
    network = "allowlist"
    image = profile.image

    # Layer 4: agent profile
    env_specs = _merge_env(env_specs, profile.env, f"agent profile {profile.name!r}")
    allow = _union(allow, profile.allowlist)
    mounts_rw = _union(mounts_rw, profile.mounts)
    mounts_ro = _union(mounts_ro, profile.mounts_ro)
    args = args + list(profile.args)
    # Default workspace mount: $PWD → /workspace rw. Added only if nothing
    # already mounts /workspace from a higher-priority layer.
    workspace_default = f"{cwd}:/workspace"

    # Layer 3b: user-wide global env file. Applied to every
    # ``contained run`` so provider API keys (or any env vars) can be
    # set once in ~/.local/share/contained/global/env and shared
    # across every project. Sits above the built-in profile defaults
    # (so users can override them without touching profile code) and
    # below contained.yaml (so per-project config wins).
    global_env_specs = _load_global_env_file()
    env_specs = _merge_env(env_specs, global_env_specs, "global env file")

    # Layer 3: defaults section
    d = loaded.defaults
    env_specs = _merge_env(env_specs, d.env, "contained.yaml defaults")
    allow = _union(allow, d.allowlist)
    mounts_rw = _union(mounts_rw, d.mounts)
    mounts_ro = _union(mounts_ro, d.mounts_ro)
    args = args + list(d.args)
    if d.network is not None:
        network = d.network
    if d.image is not None:
        image = d.image

    # Layer 2: agents.<name>
    a = loaded.for_agent(agent_name)
    env_specs = _merge_env(
        env_specs, a.env, f"contained.yaml agents.{agent_name}"
    )
    allow = _union(allow, a.allowlist)
    mounts_rw = _union(mounts_rw, a.mounts)
    mounts_ro = _union(mounts_ro, a.mounts_ro)
    args = args + list(a.args)
    if a.network is not None:
        network = a.network
    if a.image is not None:
        image = a.image

    # Layer 1: CLI flags
    env_specs = _merge_env(env_specs, overrides.env, "--env flag")
    env_specs = _merge_env(
        env_specs, _load_env_files(overrides.env_from), "--env-from file"
    )
    allow = _union(allow, overrides.allow)
    mounts_rw = _union(mounts_rw, overrides.mounts)
    mounts_ro = _union(mounts_ro, overrides.mounts_ro)
    if overrides.network is not None:
        network = overrides.network
    if overrides.image is not None:
        image = overrides.image

    for entry in allow:
        validate_allowlist_entry(entry)

    ssh_allow: list[str] = _union(list(loaded.ssh_allowlist), overrides.allow_ssh)
    for entry in ssh_allow:
        validate_ssh_allowlist_entry(entry)

    parsed_rw = [_parse_mount(m, loaded.base_dir, read_only=False) for m in mounts_rw]
    parsed_ro = [_parse_mount(m, loaded.base_dir, read_only=True) for m in mounts_ro]
    has_workspace = any(
        m.container == "/workspace" for m in parsed_rw + parsed_ro
    )

    mounts: list[Mount] = []
    if not has_workspace:
        mounts.append(_parse_mount(workspace_default, loaded.base_dir, read_only=False))
    mounts.extend(parsed_rw)
    mounts.extend(parsed_ro)

    for m in mounts:
        _validate_mount_safety(m, allow_home=overrides.allow_home_mount)
        _require_host_path_exists(m)

    if (overrides.tmux_config is not None or overrides.tmux_prefix is not None) \
            and not overrides.tmux:
        raise ConfigError(
            "--tmux-config and --tmux-prefix require --tmux"
        )

    if overrides.tmux_config is not None:
        tc = _resolve_tmux_config_path(overrides.tmux_config)
        mounts.append(
            Mount(host=tc, container="/home/agent/.config/tmux", read_only=True)
        )

    ssh_key_host_path: Path | None = None
    ssh_auth_sock_host_path: str | None = None
    if ssh_allow:
        _reject_dotssh_mounts(parsed_rw + parsed_ro)
        if overrides.ssh_key is not None:
            ssh_key_host_path = _resolve_ssh_key_path(overrides.ssh_key)
        env_source = host_env if host_env is not None else os.environ
        sock = env_source.get("SSH_AUTH_SOCK")
        if sock:
            ssh_auth_sock_host_path = sock

    planned_seeds: list[state.PlannedSeed] = []
    if not overrides.no_state and profile.state_mount is not None:
        # Cross-mount every profile's state dir into every container:
        # all agent CLIs live in the same base image (see
        # Dockerfile.base), so either agent may invoke the other and
        # should find the other's per-project state where it expects
        # it. Only the entrypoint differs between `contained run
        # claude` and `contained run pi`.
        seen_containers: set[str] = set()
        for p in profiles.all_profiles():
            if p.state_mount is None or p.state_mount in seen_containers:
                continue
            host = state.agent_state_dir(cwd, p.name)
            mounts.append(
                Mount(host=host, container=p.state_mount, read_only=False)
            )
            seen_containers.add(p.state_mount)

        if profile.file_seeds:
            pstate = state.project_state_dir(cwd)
            planned_seeds = state.plan_seeds(profile, pstate)
            for p in planned_seeds:
                if p.needs_mount and p.available:
                    mounts.append(
                        Mount(
                            host=p.host_path,
                            container=p.seed.container_path,
                            read_only=False,
                        )
                    )

    env: list[EnvVar] = [_parse_env(spec, src) for spec, src in env_specs]

    warnings = _collect_warnings(parsed_rw + parsed_ro)
    warnings.extend(_seed_warnings(planned_seeds))

    if ssh_allow and ssh_key_host_path is None and ssh_auth_sock_host_path is None:
        warnings.append(
            "warning: --allow-ssh is set but neither SSH_AUTH_SOCK nor --ssh-key "
            "was provided. the agent will have no credentials to authenticate "
            "with. start ssh-agent on the host (ssh-add) or pass --ssh-key."
        )

    return ResolvedRun(
        agent=profile,
        image=image,
        mounts=mounts,
        env=env,
        network=network,
        allowlist=allow,
        workdir=profile.workdir,
        passthrough_args=args + list(overrides.passthrough),
        config_path=loaded.path,
        rebuild=overrides.rebuild,
        no_state=overrides.no_state,
        warnings=warnings,
        planned_seeds=planned_seeds,
        ssh_allowlist=ssh_allow,
        ssh_key_host_path=ssh_key_host_path,
        ssh_auth_sock_host_path=ssh_auth_sock_host_path,
        tmux=overrides.tmux,
        tmux_prefix=overrides.tmux_prefix,
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


def _merge_env(
    a: list[tuple[str, str]], b: list[str], source: str
) -> list[tuple[str, str]]:
    """Merge env specs, keyed by KEY. Later layer (b) wins on conflict."""
    def key_of(spec: str) -> str:
        return spec.split("=", 1)[0]

    b_keys = {key_of(s) for s in b}
    out = [(spec, src) for (spec, src) in a if key_of(spec) not in b_keys]
    existing = {spec for spec, _ in out}
    for s in b:
        if s not in existing:
            out.append((s, source))
            existing.add(s)
    return out


_ALLOWLIST_HOST_RE = re.compile(
    r"^[A-Za-z0-9]([A-Za-z0-9\-]{0,61}[A-Za-z0-9])?"
    r"(\.[A-Za-z0-9]([A-Za-z0-9\-]{0,61}[A-Za-z0-9])?)*$"
)


def validate_allowlist_entry(entry: str) -> None:
    stripped = entry.strip()
    if not stripped:
        raise ConfigError("allowlist entries must not be empty")
    host, _, port = stripped.partition(":")
    if "*" in host or "?" in host:
        raise ConfigError(
            f"allowlist entry {entry!r}: wildcards are not supported. "
            "list each hostname explicitly (e.g. `api.example.com:443`, "
            "`foo.api.example.com:443`)."
        )
    if not _ALLOWLIST_HOST_RE.match(host):
        raise ConfigError(
            f"allowlist entry {entry!r}: invalid hostname {host!r}"
        )
    if port and not port.isdigit():
        raise ConfigError(
            f"allowlist entry {entry!r}: port must be numeric, got {port!r}"
        )


def validate_ssh_allowlist_entry(entry: str) -> None:
    """PRD 09: SSH allowlist entries are bare hostnames, optionally with :22.

    Any other port is a configuration error — the SSH sidecar's
    ``ConnectPort`` is 22, so nothing else can be tunneled, and silently
    accepting e.g. `host:2222` would mislead the user into thinking
    non-standard ports are supported when they aren't.
    """
    stripped = entry.strip()
    if not stripped:
        raise ConfigError("ssh allowlist entries must not be empty")
    host, _, port = stripped.partition(":")
    if "*" in host or "?" in host:
        raise ConfigError(
            f"ssh allowlist entry {entry!r}: wildcards are not supported"
        )
    if not _ALLOWLIST_HOST_RE.match(host):
        raise ConfigError(
            f"ssh allowlist entry {entry!r}: invalid hostname {host!r}"
        )
    if port and port != "22":
        raise ConfigError(
            f"ssh allowlist entry {entry!r}: Git over SSH only supports "
            "port 22. Use --allow for other ports."
        )


def _reject_dotssh_mounts(user_mounts: list[Mount]) -> None:
    """Hard-error if any user-supplied mount would expose raw SSH keys.

    When ``--allow-ssh`` is active, mounting ``~/.ssh`` (or any
    directory containing it) is refused outright — the supported
    credential modes are ssh-agent forwarding and ``--ssh-key``. The
    non-ssh sensitive-dir case stays a warning (see
    ``_collect_warnings``); this stricter policy fires only when SSH
    egress is enabled and the blast radius is real.
    """
    for m in user_mounts:
        if not m.host.is_dir():
            continue
        if m.host.name == ".ssh" or (m.host / ".ssh").exists():
            raise ConfigError(
                f"mount {m.host} conflicts with --allow-ssh: contained "
                "refuses to mount .ssh directories when SSH egress is "
                "enabled. Use ssh-agent forwarding (ssh-add on the host, "
                "then run with SSH_AUTH_SOCK set) or pass --ssh-key <path> "
                "to forward a single key read-only."
            )


def _resolve_tmux_config_path(raw: Path) -> Path:
    p = Path(os.path.expanduser(os.path.expandvars(str(raw))))
    try:
        resolved = p.resolve(strict=True)
    except FileNotFoundError as e:
        raise ConfigError(f"--tmux-config: no such directory: {p}") from e
    except OSError as e:
        raise ConfigError(f"--tmux-config: {p}: {e}") from e
    if not resolved.is_dir():
        raise ConfigError(f"--tmux-config: not a directory: {resolved}")
    if not (resolved / "tmux.conf").is_file():
        raise ConfigError(
            f"--tmux-config: {resolved} does not contain a tmux.conf"
        )
    return resolved


def _resolve_ssh_key_path(raw: Path) -> Path:
    p = Path(os.path.expanduser(os.path.expandvars(str(raw))))
    try:
        resolved = p.resolve(strict=True)
    except FileNotFoundError as e:
        raise ConfigError(f"--ssh-key: no such file: {p}") from e
    except OSError as e:
        raise ConfigError(f"--ssh-key: {p}: {e}") from e
    if not resolved.is_file():
        raise ConfigError(f"--ssh-key: not a regular file: {resolved}")
    return resolved


# Container-side paths for the SSH plumbing. These must line up with
# the `install -d` for /home/agent/.ssh in Dockerfile.base and with the
# bind mounts added by runtime.run() when ssh_allowlist is non-empty.
SSH_CONFIG_CONTAINER_PATH = "/home/agent/.ssh/config"
SSH_KNOWN_HOSTS_CONTAINER_PATH = "/home/agent/.ssh/known_hosts"
SSH_KEY_CONTAINER_PATH = "/home/agent/.ssh/id_contained"
SSH_AGENT_SOCK_CONTAINER_PATH = "/run/ssh-agent.sock"


def generate_ssh_config(
    ssh_allowlist: list[str],
    *,
    ssh_key_container_path: str | None = None,
) -> str:
    """Build the per-run ~/.ssh/config that routes to the SSH sidecar.

    `Host` is emitted as the exact list of allowlisted hostnames —
    never a wildcard — so typos cleanly fail host-matching and fall
    through to ssh's default behavior (which the egress policy then
    blocks). ``nc -X connect`` speaks HTTP CONNECT to the sidecar,
    which then enforces its own per-host filter before dialing :22
    on the real host.
    """
    hosts = sorted({entry.split(":", 1)[0].strip() for entry in ssh_allowlist if entry.strip()})
    if not hosts:
        return ""
    lines = [
        "# Generated by contained (PRD 09). Do not edit — regenerated on each run.",
        f"Host {' '.join(hosts)}",
        f"    ProxyCommand /usr/bin/nc -X connect -x "
        f"{proxy.PROXY_SSH_ALIAS}:{proxy.PROXY_SSH_PORT} %h %p",
        "    StrictHostKeyChecking yes",
        f"    UserKnownHostsFile {SSH_KNOWN_HOSTS_CONTAINER_PATH}",
    ]
    if ssh_key_container_path:
        lines.append(f"    IdentityFile {ssh_key_container_path}")
        lines.append("    IdentitiesOnly yes")
    return "\n".join(lines) + "\n"


def _validate_mount_safety(m: Mount, *, allow_home: bool) -> None:
    host = m.host
    if str(host) == "/":
        raise ConfigError("refusing to mount host root `/` as a mount source")
    try:
        home = Path.home()
    except (RuntimeError, OSError):
        home = None
    if home is not None and host == home and not allow_home:
        raise ConfigError(
            f"refusing to mount home directory {host}. "
            "pass --allow-home-mount if you really mean it."
        )


def _require_host_path_exists(m: Mount) -> None:
    if not m.host.exists():
        raise ConfigError(
            f"mount source does not exist: {m.host} "
            "(contained does not create missing host paths)"
        )


def _seed_warnings(plans: list[state.PlannedSeed]) -> list[str]:
    out: list[str] = []
    for p in plans:
        if p.source is not None:
            continue  # resolved from host source or cached from prior run
        if p.seed.fallback_content:
            # A meaningful fallback will be written (e.g. ``{}\n`` for
            # claude.json) — no auth prompt expected. Credentials use
            # an empty fallback purely as a bind-mount target, so the
            # agent will still prompt; that path falls through to the
            # warning below.
            continue
        tried = ", ".join(p.seed.sources)
        out.append(
            f"warning: no host source for {p.seed.state_rel} "
            f"(tried: {tried}). the agent will prompt for auth on first run."
        )
    return out


_SENSITIVE_DIR_HINTS = (".env", ".ssh", ".aws")


def _collect_warnings(user_mounts: list[Mount]) -> list[str]:
    out: list[str] = []
    for m in user_mounts:
        if m.read_only or not m.host.is_dir():
            continue
        for hint in _SENSITIVE_DIR_HINTS:
            if (m.host / hint).exists():
                out.append(
                    f"warning: rw mount {m.host} contains {hint} — "
                    "consider --mount-ro to avoid write access"
                )
    return out


def _parse_mount(spec: str, base_dir: Path, *, read_only: bool) -> Mount:
    if ":" not in spec:
        raise ConfigError(f"mount spec must be host:container, got {spec!r}")
    host_raw, container = spec.split(":", 1)
    host = Path(os.path.expanduser(os.path.expandvars(host_raw)))
    if not host.is_absolute():
        host = base_dir / host
    try:
        host = host.resolve(strict=True)
    except FileNotFoundError as e:
        raise ConfigError(
            f"mount source does not exist: {host} "
            "(contained does not create missing host paths)"
        ) from e
    except OSError as e:
        raise ConfigError(f"mount source cannot be resolved: {host}: {e}") from e
    return Mount(host=host, container=container, read_only=read_only)


def _parse_env(spec: str, source: str = "tool defaults") -> EnvVar:
    if "=" in spec:
        key, value = spec.split("=", 1)
        return EnvVar(key=key, value=value, from_host=False, source=source)
    return EnvVar(key=spec, value=None, from_host=True, source=source)


def check_required_host_env(
    run: "ResolvedRun", host_env: dict[str, str]
) -> str | None:
    """Return an error message if a required from-host env var is unset.

    An env var is "required" if it was named by a bare ``--env KEY`` CLI
    flag, or if it appears in the agent profile's ``required_env`` list.
    Best-effort forwards (tool defaults, optional profile envs like
    ``CLAUDE_MODEL``) are skipped.
    """
    required_by_profile = set(run.agent.required_env)
    for e in run.env:
        if not e.from_host:
            continue
        required = e.source == "--env flag" or e.key in required_by_profile
        if not required:
            continue
        if host_env.get(e.key) is None:
            return (
                f"--env {e.key}: required but not set in host environment "
                f"(source: {e.source})"
            )
    return None


def _load_env_files(paths: list[Path]) -> list[str]:
    out: list[str] = []
    for p in paths:
        if not p.is_file():
            raise ConfigError(f"--env-from: no such file: {p}")
        out.extend(_parse_env_file(p, missing_msg=f"--env-from: no such file: {p}"))
    return out


def _parse_env_file(path: Path, *, missing_msg: str | None = None) -> list[str]:
    if not path.is_file():
        if missing_msg is not None:
            raise ConfigError(missing_msg)
        return []
    out: list[str] = []
    for lineno, raw in enumerate(path.read_text().splitlines(), start=1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            raise ConfigError(f"malformed env line at {path}:{lineno}")
        out.append(line)
    return out


def _load_global_env_file() -> list[str]:
    """Load the tool-wide env file if present, otherwise return empty."""
    return _parse_env_file(state.global_env_file())


_SENSITIVE_HINTS = (
    "KEY", "TOKEN", "SECRET", "PASSWORD", "PASS", "CREDENTIAL",
    "PAT", "DSN", "COOKIE", "BEARER", "AUTH",
)
_USER_SUPPLIED_SOURCES = ("--env flag", "--env-from file")


def mask_env_display(env_var: "EnvVar", value: str | None) -> str:
    """Return the display-safe form of an env var value.

    - Host-forwarded vars render as ``<from host>`` (the value never
      passes through contained anyway).
    - User-supplied concrete values (``--env``, ``--env-from``) are
      always masked: treat anything the user typed as sensitive.
    - Config-file-supplied values are masked by key-name heuristic.
    """
    if env_var.from_host:
        return "<from host>"
    if env_var.source in _USER_SUPPLIED_SOURCES:
        return "***"
    if any(h in env_var.key.upper() for h in _SENSITIVE_HINTS):
        return "***"
    return value if value is not None else ""


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
    if run.no_state:
        lines.append("  (state persistence disabled via --no-state)")
    lines.append("")
    lines.append("env:")
    for e in run.env:
        value = e.resolve_value(host_env)
        lines.append(f"  - {e.key}={mask_env_display(e, value)}")
    lines.append("")
    lines.append(f"allowlist ({len(run.allowlist)}):")
    for entry in run.allowlist:
        lines.append(f"  - {entry}")
    lines.append("")
    if run.ssh_allowlist:
        lines.append(f"ssh_allowlist ({len(run.ssh_allowlist)}):")
        for entry in run.ssh_allowlist:
            lines.append(f"  - {entry}")
        cred: str
        if run.ssh_auth_sock_host_path:
            cred = f"ssh-agent socket {run.ssh_auth_sock_host_path}"
        elif run.ssh_key_host_path:
            cred = f"key file {run.ssh_key_host_path}"
        else:
            cred = "(none — agent will fail to authenticate)"
        lines.append(f"ssh_credentials: {cred}")
        lines.append("ssh_config (generated):")
        ssh_key_cp = SSH_KEY_CONTAINER_PATH if run.ssh_key_host_path else None
        for ssh_line in generate_ssh_config(
            run.ssh_allowlist, ssh_key_container_path=ssh_key_cp
        ).splitlines():
            lines.append(f"  {ssh_line}")
        lines.append("")
    if run.planned_seeds:
        lines.append("credentials:")
        for p in run.planned_seeds:
            if p.source == "(cached)":
                status = "cached in state"
            elif p.source is not None:
                status = f"seed from {p.source}"
            elif p.data is not None:
                status = "empty placeholder (no host source)"
            else:
                status = "missing (agent will prompt)"
            lines.append(f"  - {p.seed.container_path} — {status}")
        lines.append("")
    from . import runtime  # local import to avoid cycle
    if run.tmux and run.tmux_prefix:
        has_user_config = any(
            m.container == "/home/agent/.config/tmux" for m in run.mounts
        )
        lines.append("tmux_wrapper (generated):")
        for w_line in runtime._build_tmux_wrapper_text(
            run.tmux_prefix, source_user_config=has_user_config
        ).splitlines():
            lines.append(f"  {w_line}")
        lines.append("")
    lines.append("docker invocation (preview):")
    lines.append("  " + " ".join(runtime.build_argv(run, mask_secrets=True)))
    if run.warnings:
        lines.append("")
        lines.extend(run.warnings)
    return "\n".join(lines)
