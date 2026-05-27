"""Docker runtime execution (PRD 02).

Turns a ResolvedRun into a `docker run` invocation, handles the optional
`Dockerfile.contained` overlay build with content-hash caching, and
shells out to docker with the user's TTY attached. Signals reach the
container because the Python parent shares a process group with docker
and swallows KeyboardInterrupt.
"""

from __future__ import annotations

import contextlib
import dataclasses
import hashlib
import os
import shutil
import subprocess
import sys
from collections import deque
from dataclasses import dataclass
from importlib import resources
from pathlib import Path

from . import clipboard, profiles, proxy, state
from .run import (
    SSH_AGENT_SOCK_CONTAINER_PATH,
    SSH_CONFIG_CONTAINER_PATH,
    SSH_KEY_CONTAINER_PATH,
    SSH_KNOWN_HOSTS_CONTAINER_PATH,
    ResolvedRun,
    generate_ssh_config,
    mask_env_display,
)
from .state import state_root


class DockerError(Exception):
    pass


@dataclass
class _BuildResult:
    returncode: int
    tail: str


_BUILD_TAIL_LINES = 30


def _run_capturing_tail(
    cmd: list[str], *, input_text: str | None = None
) -> _BuildResult:
    """Run a build command with stderr streamed AND the last N lines buffered.

    stdout is inherited (docker build writes progress there on BuildKit),
    stderr is captured line-by-line, echoed to our stderr, and the last
    ``_BUILD_TAIL_LINES`` lines are retained for error messages.
    """
    stdin = subprocess.PIPE if input_text is not None else None
    proc = subprocess.Popen(
        cmd,
        stdin=stdin,
        stderr=subprocess.PIPE,
        text=True,
        bufsize=1,
    )
    tail: deque[str] = deque(maxlen=_BUILD_TAIL_LINES)

    if input_text is not None and proc.stdin is not None:
        try:
            proc.stdin.write(input_text)
        finally:
            proc.stdin.close()

    assert proc.stderr is not None
    for line in proc.stderr:
        sys.stderr.write(line)
        tail.append(line.rstrip("\n"))
    proc.wait()
    return _BuildResult(returncode=proc.returncode, tail="\n".join(tail))


def _indent_tail(tail: str) -> str:
    if not tail.strip():
        return "  (no stderr output)"
    return "\n".join("  " + line for line in tail.splitlines())


OVERLAY_FROM_PLACEHOLDER = "contained-base"


def ensure_daemon() -> None:
    if shutil.which("docker") is None:
        raise DockerError(
            "docker binary not found in PATH. install Docker Desktop (macOS) "
            "or docker-ce (Linux), then re-run. see `contained doctor`."
        )
    try:
        result = subprocess.run(
            ["docker", "info", "--format", "{{.ServerVersion}}"],
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (subprocess.TimeoutExpired, OSError) as e:
        raise DockerError(f"could not reach docker daemon: {e}") from e
    if result.returncode != 0:
        detail = (
            result.stderr.strip().splitlines()[-1]
            if result.stderr.strip()
            else "daemon unreachable"
        )
        raise DockerError(
            f"docker daemon is not reachable: {detail}. "
            "on macOS, start Docker Desktop. see `contained doctor`."
        )


TMUX_WRAPPER_CONTAINER_PATH = "/home/agent/.config/contained/tmux-wrapper.conf"


def build_argv(
    run: ResolvedRun,
    *,
    mask_secrets: bool = False,
    proxy_network: str | None = None,
    ssh_config_host_path: Path | None = None,
    ssh_known_hosts_host_path: Path | None = None,
    tmux_wrapper_host_path: Path | None = None,
    clipboard_host_dir: Path | None = None,
) -> list[str]:
    argv = [
        "docker", "run", "--rm", "-it", "--init",
        "--user", "1000:1000",
        "--cap-drop", "ALL",
        "--security-opt", "no-new-privileges",
        "--workdir", run.workdir,
    ]
    for m in run.mounts:
        spec = f"type=bind,src={m.host},dst={m.container}"
        if m.read_only:
            spec += ",ro"
        argv += ["--mount", spec]
    if run.ssh_allowlist:
        if ssh_config_host_path is not None:
            argv += [
                "--mount",
                f"type=bind,src={ssh_config_host_path},"
                f"dst={SSH_CONFIG_CONTAINER_PATH},ro",
            ]
        if ssh_known_hosts_host_path is not None:
            argv += [
                "--mount",
                f"type=bind,src={ssh_known_hosts_host_path},"
                f"dst={SSH_KNOWN_HOSTS_CONTAINER_PATH},ro",
            ]
        if run.ssh_key_host_path is not None:
            argv += [
                "--mount",
                f"type=bind,src={run.ssh_key_host_path},"
                f"dst={SSH_KEY_CONTAINER_PATH},ro",
            ]
        if run.ssh_auth_sock_host_path is not None:
            argv += [
                "--mount",
                f"type=bind,src={run.ssh_auth_sock_host_path},"
                f"dst={SSH_AGENT_SOCK_CONTAINER_PATH}",
            ]
            argv += [
                "--env",
                f"SSH_AUTH_SOCK={SSH_AGENT_SOCK_CONTAINER_PATH}",
            ]
    for e in run.env:
        if e.from_host:
            argv += ["--env", e.key]
        else:
            value = mask_env_display(e, e.value) if mask_secrets else (e.value or "")
            argv += ["--env", f"{e.key}={value}"]
    if run.network == "host":
        argv += ["--network", "host"]
    elif run.network == "none":
        argv += ["--network", "none"]
    elif run.network == "allowlist":
        # Routed through the tinyproxy sidecar on a private internal
        # network. For --dry-run we don't know a real network name
        # yet; use a placeholder so the preview is still readable.
        network = proxy_network or "contained-allowlist"
        argv += ["--network", network]
        proxy_url = f"http://{proxy.PROXY_ALIAS}:{proxy.PROXY_PORT}"
        for key in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy"):
            argv += ["--env", f"{key}={proxy_url}"]
        argv += ["--env", "NO_PROXY=localhost,127.0.0.1"]
    if run.tmux and tmux_wrapper_host_path is not None:
        argv += [
            "--mount",
            f"type=bind,src={tmux_wrapper_host_path},"
            f"dst={TMUX_WRAPPER_CONTAINER_PATH},ro",
        ]
    if run.clipboard_bridge and clipboard_host_dir is not None:
        # Mount the *directory* containing the bridge's image/meta
        # files, not the individual files: the bridge rewrites
        # ``image.png`` atomically via ``rename``, which changes the
        # inode. A file-level bind mount binds by inode and the
        # container would forever see the original (empty) file.
        argv += [
            "--mount",
            f"type=bind,src={clipboard_host_dir},"
            f"dst={clipboard.CONTAINER_DIR},ro",
        ]
        argv += [
            "--env",
            f"CONTAINED_CLIPBOARD_FILE={clipboard.CONTAINER_IMAGE_PATH}",
            "--env",
            f"CONTAINED_CLIPBOARD_META={clipboard.CONTAINER_META_PATH}",
        ]
    argv.append(run.image)
    cmd: list[str] = []
    if run.agent.entrypoint:
        cmd += run.agent.entrypoint
    cmd += run.passthrough_args
    if run.tmux:
        # `-A` attaches if a session named `contained` already exists,
        # which lets the user `docker exec` into the container and
        # rejoin a detached agent. `-s` names the session so the
        # attach target is predictable. The trailing args are the
        # command tmux runs as the session's first window.
        tmux_argv = ["tmux"]
        if tmux_wrapper_host_path is not None:
            tmux_argv += ["-f", TMUX_WRAPPER_CONTAINER_PATH]
        tmux_argv += ["new-session", "-A", "-s", "contained"]
        argv += tmux_argv + cmd
    else:
        argv += cmd
    return argv


def find_overlay(resolved: ResolvedRun, cwd: Path) -> Path | None:
    candidates: list[Path] = []
    if resolved.config_path is not None:
        candidates.append(resolved.config_path.parent / "Dockerfile.contained")
    candidates.append(cwd / "Dockerfile.contained")
    seen: set[Path] = set()
    for c in candidates:
        if c in seen:
            continue
        seen.add(c)
        if c.is_file():
            return c
    return None


def _base_image_id(image: str) -> str:
    try:
        result = subprocess.run(
            ["docker", "image", "inspect", "--format", "{{.Id}}", image],
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (subprocess.TimeoutExpired, OSError):
        return image
    if result.returncode == 0 and result.stdout.strip():
        return result.stdout.strip()
    return image


def overlay_tag(agent: str, dockerfile: Path, base_image: str) -> str:
    h = hashlib.sha256()
    h.update(agent.encode())
    h.update(b"\0")
    h.update(dockerfile.read_bytes())
    h.update(b"\0")
    h.update(_base_image_id(base_image).encode())
    return f"contained-overlay-{agent}:{h.hexdigest()[:16]}"


def _image_exists(tag: str) -> bool:
    try:
        result = subprocess.run(
            ["docker", "image", "inspect", tag],
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (subprocess.TimeoutExpired, OSError):
        return False
    return result.returncode == 0


def build_overlay(
    agent: str, dockerfile: Path, base_image: str, *, rebuild: bool
) -> str:
    tag = overlay_tag(agent, dockerfile, base_image)
    if not rebuild and _image_exists(tag):
        return tag

    rewritten = _rewrite_overlay_from(
        dockerfile.read_text(), dockerfile, base_image
    )

    cmd = ["docker", "build", "-t", tag, "-f", "-", str(dockerfile.parent)]
    result = _run_capturing_tail(cmd, input_text=rewritten)
    if result.returncode != 0:
        raise DockerError(
            f"overlay build failed for {dockerfile}\n"
            f"{_indent_tail(result.tail)}"
        )
    return tag


def _rewrite_overlay_from(
    source: str, dockerfile: Path, base_image: str
) -> str:
    """Replace every ``FROM contained-base`` instruction with the real base.

    The very first instruction encountered must be a ``FROM`` whose
    image is exactly the placeholder — otherwise the overlay wouldn't
    actually be layered on our sandbox base, which is the whole point
    of the placeholder.
    """
    out_lines: list[str] = []
    first_instruction_seen = False
    for line in source.splitlines():
        stripped = line.lstrip()
        # Skip blanks and comments when hunting for the first instruction.
        if not first_instruction_seen and (not stripped or stripped.startswith("#")):
            out_lines.append(line)
            continue
        tokens = stripped.split()
        if tokens and tokens[0].upper() == "FROM":
            image = tokens[1] if len(tokens) > 1 else ""
            if not first_instruction_seen:
                first_instruction_seen = True
                if image != OVERLAY_FROM_PLACEHOLDER:
                    raise DockerError(
                        f"{dockerfile}: first FROM must be "
                        f"`FROM {OVERLAY_FROM_PLACEHOLDER}` (got: {line.strip()!r}). "
                        "contained rewrites that line at build time to pin the "
                        "real sandbox base image, so your overlay layers on top "
                        "of the hardened base."
                    )
                out_lines.append(line.replace(
                    OVERLAY_FROM_PLACEHOLDER, base_image, 1
                ))
                continue
            # Subsequent stage: rewrite only if it references the placeholder.
            if image == OVERLAY_FROM_PLACEHOLDER:
                out_lines.append(line.replace(
                    OVERLAY_FROM_PLACEHOLDER, base_image, 1
                ))
                continue
            out_lines.append(line)
            continue
        if not first_instruction_seen:
            # Non-FROM first instruction is illegal in Dockerfiles anyway.
            raise DockerError(
                f"{dockerfile}: first instruction must be "
                f"`FROM {OVERLAY_FROM_PLACEHOLDER}` (got: {line.strip()!r})"
            )
        out_lines.append(line)
    if not first_instruction_seen:
        raise DockerError(
            f"{dockerfile}: no FROM instruction found"
        )
    return "\n".join(out_lines) + ("\n" if source.endswith("\n") else "")


def run(resolved: ResolvedRun, cwd: Path) -> int:
    ensure_daemon()

    if not resolved.no_state and resolved.agent.state_mount is not None:
        # Matches the cross-mount in run.resolve(): every profile's
        # state dir is bound into every container, so every dir has
        # to exist on the host before docker starts.
        for p in profiles.all_profiles():
            if p.state_mount is None:
                continue
            state.ensure_agent_state_dir(cwd, p.name)
        state.ensure_agent_browser_profile_dir()

    if resolved.planned_seeds:
        if any(p.seed.is_global for p in resolved.planned_seeds):
            state.ensure_global_state_dir()
        written = state.apply_seeds(resolved.planned_seeds)
        for p in written:
            origin = p.source if p.source is not None else "empty placeholder"
            print(
                f"contained: seeded {origin} -> {p.host_path} (mode 600)",
                file=sys.stderr,
            )

    # After seeds are on disk, stamp the Shift-Enter flag into
    # claude.json so the container-side Claude Code will actually
    # interpret the extended-key sequence the outer tty is now
    # emitting (see _execute). Only meaningful for the claude agent;
    # the file lives at <project_state>/claude.json which is bind-
    # mounted to /home/agent/.claude.json.
    if (
        not resolved.no_state
        and resolved.agent.name == "claude"
        and any(
            p.seed.state_rel == "claude.json" for p in resolved.planned_seeds
        )
    ):
        claude_json = state.project_state_dir(cwd) / "claude.json"
        if state.patch_claude_json_shift_enter(claude_json):
            print(
                f"contained: set shiftEnterKeyBindingInstalled=true in {claude_json}",
                file=sys.stderr,
            )

    overlay = find_overlay(resolved, cwd)
    if overlay is not None:
        image = build_overlay(
            resolved.agent.name,
            overlay,
            resolved.image,
            rebuild=resolved.rebuild,
        )
        resolved = dataclasses.replace(resolved, image=image)

    session: proxy.ProxySession | None = None
    ssh_session: proxy.SshProxySession | None = None
    clipboard_session: clipboard.ClipboardBridge | None = None
    ssh_config_path: Path | None = None
    ssh_known_hosts_path: Path | None = None
    tmux_wrapper_path: Path | None = None
    state_dir: Path | None = None
    needs_state_dir = (
        resolved.network == "allowlist"
        or bool(resolved.ssh_allowlist)
        or resolved.tmux_prefix is not None
        or resolved.clipboard_bridge
    )
    if needs_state_dir and not resolved.no_state:
        state_dir = state.project_state_dir(cwd)
        state_dir.mkdir(parents=True, exist_ok=True)
        with contextlib.suppress(OSError):
            state_dir.chmod(0o700)

    if resolved.network == "allowlist":
        try:
            session = proxy.start(
                proxy.new_run_id(),
                resolved.allowlist,
                profiles.PROXY_IMAGE,
                state_dir=state_dir,
                project=cwd,
            )
        except (proxy.ProxyError, OSError) as e:
            # OSError shows up when docker itself isn't on PATH, which
            # typically means the user skipped `contained build`. We want
            # the same hint either way.
            raise DockerError(
                f"failed to start egress proxy: {e}. "
                "run `contained build` to build the proxy image, "
                "or pass `--network host` to bypass the allowlist."
            ) from e

    try:
        if resolved.ssh_allowlist:
            if session is None:
                # Catches --network host / none combined with --allow-ssh:
                # the SSH sidecar needs the private internal network the
                # HTTPS sidecar creates, so we can't layer SSH allowlisting
                # on top of --network host or --network none.
                raise DockerError(
                    "--allow-ssh requires --network=allowlist. it cannot "
                    "be combined with --network host or --network none."
                )
                # unreachable — DockerError raised above
            ssh_config_path, ssh_known_hosts_path = _prepare_ssh_assets(
                resolved, state_dir
            )
            try:
                ssh_session = proxy.start_ssh(
                    session.run_id,
                    resolved.ssh_allowlist,
                    profiles.PROXY_IMAGE,
                    network=session.network,
                    state_dir=state_dir,
                    project=cwd,
                )
            except (proxy.ProxyError, OSError) as e:
                raise DockerError(
                    f"failed to start SSH egress proxy: {e}. "
                    "run `contained build` to build the proxy image."
                ) from e

        if resolved.tmux and resolved.tmux_prefix is not None:
            tmux_wrapper_path = _prepare_tmux_wrapper(resolved, state_dir)

        if resolved.clipboard_bridge:
            clipboard_session = _start_clipboard_bridge(resolved, state_dir)

        clipboard_dir = (
            clipboard_session.host_dir if clipboard_session is not None else None
        )
        argv = build_argv(
            resolved,
            proxy_network=session.network if session else None,
            ssh_config_host_path=ssh_config_path,
            ssh_known_hosts_host_path=ssh_known_hosts_path,
            tmux_wrapper_host_path=tmux_wrapper_path,
            clipboard_host_dir=clipboard_dir,
        )
        return _execute(argv)
    finally:
        # Tear down SSH sidecar first so the HTTPS teardown can safely
        # remove the shared network.
        if ssh_session is not None:
            proxy.stop_ssh(ssh_session)
        if session is not None:
            proxy.stop(session)
        if clipboard_session is not None:
            clipboard.stop(clipboard_session)
        for artifact in (ssh_config_path, ssh_known_hosts_path, tmux_wrapper_path):
            if artifact is not None:
                with contextlib.suppress(OSError):
                    artifact.unlink(missing_ok=True)


def _start_clipboard_bridge(
    resolved: ResolvedRun, state_dir: Path | None
) -> clipboard.ClipboardBridge | None:
    """Spawn the host clipboard bridge if the host can support it.

    Returns None (with a stderr warning) when the host has no
    discoverable clipboard tool — headless Linux, mostly. The container
    shims fail silently in that case, which mirrors the behaviour of
    running real clipboard tools on an empty / unavailable clipboard.
    """
    if not clipboard.host_supports_clipboard():
        print(
            "contained: no host clipboard tool found "
            "(install pngpaste / xclip / wl-paste); Ctrl-V image paste "
            "into the agent will be unavailable. pass "
            "--no-clipboard-bridge to silence this.",
            file=sys.stderr,
        )
        return None
    try:
        bridge = clipboard.start(
            state_dir=state_dir if not resolved.no_state else None
        )
    except clipboard.ClipboardError as e:
        print(
            f"contained: clipboard bridge failed to start: {e}. "
            "continuing without it.",
            file=sys.stderr,
        )
        return None
    print(
        f"contained: clipboard bridge log -> {bridge.log_path} "
        "(tail -f for Ctrl-V debugging)",
        file=sys.stderr,
    )
    return bridge


def _build_tmux_wrapper_text(prefix: str, *, source_user_config: bool) -> str:
    """Render the tmux wrapper conf that overrides the prefix.

    `source-file` is emitted before the override so the user's bindings
    (including their original prefix) load first, then `set -g prefix`
    rewrites it. `bind <prefix> send-prefix` makes the new prefix
    nest-friendly: hitting it twice sends one through to an inner tmux.
    """
    lines: list[str] = []
    if source_user_config:
        lines.append("source-file /home/agent/.config/tmux/tmux.conf")
    lines.append(f"set -g prefix {prefix}")
    lines.append(f"bind {prefix} send-prefix")
    return "\n".join(lines) + "\n"


def _prepare_tmux_wrapper(resolved: ResolvedRun, state_dir: Path | None) -> Path:
    """Write the tmux wrapper conf and return its host path."""
    if state_dir is None:
        import tempfile
        state_dir = Path(tempfile.mkdtemp(prefix="contained-tmux-"))
    has_user_config = any(
        m.container == "/home/agent/.config/tmux" for m in resolved.mounts
    )
    text = _build_tmux_wrapper_text(
        resolved.tmux_prefix or "", source_user_config=has_user_config
    )
    wrapper = state_dir / "tmux-wrapper.conf"
    wrapper.write_text(text)
    with contextlib.suppress(OSError):
        wrapper.chmod(0o644)
    return wrapper


def _prepare_ssh_assets(
    resolved: ResolvedRun, state_dir: Path | None
) -> tuple[Path, Path]:
    """Write the per-run ssh_config and known_hosts files to disk.

    `known_hosts` is populated via host-side ``ssh-keyscan`` so the
    moment of first-use TOFU happens on the user's network, not inside
    a restricted container whose DNS is routed through our own sidecar.
    Per-host scan failures are warnings, not errors — an empty
    known_hosts against ``StrictHostKeyChecking yes`` will cause the
    agent's first connect to visibly fail, which is the right outcome.
    """
    if state_dir is None:
        # --no-state: write into a process-local tempdir that's cleaned
        # up in the outer finally.
        import tempfile
        state_dir = Path(tempfile.mkdtemp(prefix="contained-ssh-"))

    ssh_key_cp = SSH_KEY_CONTAINER_PATH if resolved.ssh_key_host_path else None
    config_text = generate_ssh_config(
        resolved.ssh_allowlist, ssh_key_container_path=ssh_key_cp
    )
    ssh_config_path = state_dir / "ssh_config"
    ssh_config_path.write_text(config_text)
    with contextlib.suppress(OSError):
        ssh_config_path.chmod(0o600)

    known_hosts_text = _run_ssh_keyscan(resolved.ssh_allowlist)
    ssh_known_hosts_path = state_dir / "ssh_known_hosts"
    ssh_known_hosts_path.write_text(known_hosts_text)
    with contextlib.suppress(OSError):
        ssh_known_hosts_path.chmod(0o644)
    return ssh_config_path, ssh_known_hosts_path


def _run_ssh_keyscan(ssh_allowlist: list[str]) -> str:
    hosts = sorted({e.split(":", 1)[0].strip() for e in ssh_allowlist if e.strip()})
    if not hosts or shutil.which("ssh-keyscan") is None:
        if hosts:
            print(
                "contained: ssh-keyscan not found on PATH — known_hosts "
                "will be empty and agent connects will fail verification.",
                file=sys.stderr,
            )
        return ""
    try:
        result = subprocess.run(
            ["ssh-keyscan", "-T", "5", *hosts],
            capture_output=True,
            text=True,
            timeout=30,
        )
    except (subprocess.TimeoutExpired, OSError) as e:
        print(
            f"contained: ssh-keyscan failed ({e}); known_hosts will be empty.",
            file=sys.stderr,
        )
        return ""
    if result.stderr.strip():
        for line in result.stderr.splitlines():
            if line.strip() and not line.strip().startswith("#"):
                print(f"contained: ssh-keyscan: {line}", file=sys.stderr)
    return result.stdout


# xterm "modifyOtherKeys" mode 2. Without this, Shift-Enter (and
# Alt-Enter, Ctrl-Enter, etc.) collapse to plain Enter before they
# ever reach the container — the host terminal never distinguishes
# them. Mode 2 makes terminals emit CSI 27 ; <mod> ; <key> ~ for
# modified keys that would otherwise be indistinguishable, which is
# what Claude Code reads to drive shift-enter-inserts-newline.
# Supported by iTerm2, Terminal.app, VSCode, xterm, kitty, ghostty,
# wezterm, foot, konsole. Reset to mode 0 on exit.
_ENABLE_EXTENDED_KEYS = "\x1b[>4;2m"
_RESET_EXTENDED_KEYS = "\x1b[>4m"


def _write_tty_sequence(seq: str) -> None:
    if not sys.stdout.isatty():
        return
    try:
        sys.stdout.write(seq)
        sys.stdout.flush()
    except OSError:
        pass


def _in_tmux() -> bool:
    return bool(os.environ.get("TMUX"))


def _configure_tmux_extended_keys() -> None:
    """Make the running tmux server deliver modified keys to panes.

    tmux filters Shift-Enter/Alt-Enter/etc. down to plain Enter unless
    ``extended-keys`` is on AND the outer terminal is tagged as
    supporting ``extkeys`` in ``terminal-features``. Emitting the
    modifyOtherKeys escape sequence from inside a pane is not enough
    on its own — tmux has to be told, at the server level, to pass
    those keys through. Both options persist only for the lifetime of
    the tmux server, and re-applying them is idempotent, so it is
    safe to run on every ``contained run`` without a restore step.
    Any failure (old tmux, tmux not on PATH) is ignored — the worst
    case is that Shift-Enter keeps behaving the way it already does.
    """
    if shutil.which("tmux") is None:
        return
    for args in (
        ["tmux", "set", "-g", "extended-keys", "on"],
        ["tmux", "set", "-ga", "terminal-features", "xterm*:extkeys"],
    ):
        try:
            subprocess.run(args, capture_output=True, timeout=5, check=False)
        except (subprocess.TimeoutExpired, OSError):
            pass


def _execute(argv: list[str]) -> int:
    """Run docker with stdio inherited; let signals flow to the child."""
    if _in_tmux():
        _configure_tmux_extended_keys()
    _write_tty_sequence(_ENABLE_EXTENDED_KEYS)
    try:
        proc = subprocess.Popen(argv)
        try:
            return proc.wait()
        except KeyboardInterrupt:
            # Docker already received SIGINT via the shared process group;
            # wait for it to clean up rather than racing it to exit.
            return proc.wait()
    finally:
        _write_tty_sequence(_RESET_EXTENDED_KEYS)


def overlay_cache_dir() -> Path:
    d = state_root() / "overlays"
    d.mkdir(parents=True, exist_ok=True)
    return d


def base_dockerfile_path() -> Path:
    """Filesystem path to the bundled Dockerfile.base asset."""
    ref = resources.files("contained").joinpath("assets/Dockerfile.base")
    with resources.as_file(ref) as p:
        return Path(p)


def proxy_dockerfile_path() -> Path:
    """Filesystem path to the bundled Dockerfile.proxy asset."""
    ref = resources.files("contained").joinpath("assets/Dockerfile.proxy")
    with resources.as_file(ref) as p:
        return Path(p)


def build_base(tag: str | None = None, *, rebuild: bool = False) -> str:
    """Build the shared base image locally.

    `tag` defaults to the profile base image ref so `contained run` picks
    it up without `--image`. `rebuild` forces `--no-cache`.
    """
    return _build_image(
        base_dockerfile_path(),
        tag or profiles.BASE_IMAGE,
        rebuild=rebuild,
        what="base image",
    )


def build_proxy(tag: str | None = None, *, rebuild: bool = False) -> str:
    """Build the egress proxy sidecar image locally."""
    return _build_image(
        proxy_dockerfile_path(),
        tag or profiles.PROXY_IMAGE,
        rebuild=rebuild,
        what="proxy image",
    )


def _build_image(
    dockerfile: Path, tag: str, *, rebuild: bool, what: str
) -> str:
    if shutil.which("docker") is None:
        raise DockerError(
            "docker binary not found in PATH. install Docker Desktop (macOS) "
            "or docker-ce (Linux), then re-run."
        )
    cmd = ["docker", "build", "-t", tag, "-f", str(dockerfile)]
    if rebuild:
        cmd.append("--no-cache")
    cmd.append(str(dockerfile.parent))
    result = _run_capturing_tail(cmd)
    if result.returncode != 0:
        raise DockerError(
            f"{what} build failed (tag={tag})\n{_indent_tail(result.tail)}"
        )
    return tag
