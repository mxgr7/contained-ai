"""Docker runtime execution (PRD 02).

Turns a ResolvedRun into a `docker run` invocation, handles the optional
`Dockerfile.contained` overlay build with content-hash caching, and
shells out to docker with the user's TTY attached. Signals reach the
container because the Python parent shares a process group with docker
and swallows KeyboardInterrupt.
"""

from __future__ import annotations

import dataclasses
import hashlib
import shutil
import subprocess
import sys
from importlib import resources
from pathlib import Path

from . import profiles, proxy, state
from .run import ResolvedRun
from .state import state_root


class DockerError(Exception):
    pass


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


def build_argv(
    run: ResolvedRun,
    *,
    mask_secrets: bool = False,
    proxy_network: str | None = None,
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
    for e in run.env:
        if e.from_host:
            argv += ["--env", e.key]
        else:
            value = _mask(e.key, e.value) if mask_secrets else (e.value or "")
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
    argv.append(run.image)
    if run.agent.entrypoint:
        argv += run.agent.entrypoint
    argv += run.passthrough_args
    return argv


_SENSITIVE_HINTS = ("KEY", "TOKEN", "SECRET", "PASSWORD", "PASS", "CREDENTIAL")


def _mask(key: str, value: str | None) -> str:
    if value is None:
        return ""
    if any(h in key.upper() for h in _SENSITIVE_HINTS):
        return "***"
    return value


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

    source = dockerfile.read_text()
    placeholder = f"FROM {OVERLAY_FROM_PLACEHOLDER}"
    if placeholder not in source:
        raise DockerError(
            f"{dockerfile}: first FROM must be `FROM {OVERLAY_FROM_PLACEHOLDER}`"
        )
    rewritten = source.replace(placeholder, f"FROM {base_image}", 1)

    cmd = ["docker", "build", "-t", tag, "-f", "-", str(dockerfile.parent)]
    result = subprocess.run(cmd, input=rewritten, text=True)
    if result.returncode != 0:
        raise DockerError(f"overlay build failed for {dockerfile}")
    return tag


def run(resolved: ResolvedRun, cwd: Path) -> int:
    ensure_daemon()

    if not resolved.no_state and resolved.agent.state_mount is not None:
        state.ensure_agent_state_dir(cwd, resolved.agent.name)

    if resolved.planned_seeds:
        written = state.apply_seeds(resolved.planned_seeds)
        for p in written:
            if p.source is not None:
                origin = p.source
            else:
                origin = "empty placeholder"
            print(
                f"contained: seeded {p.seed.container_path} ({origin})",
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
    if resolved.network == "allowlist":
        try:
            session = proxy.start(
                proxy.new_run_id(), resolved.allowlist, profiles.PROXY_IMAGE
            )
        except (proxy.ProxyError, OSError) as e:
            raise DockerError(
                f"failed to start egress proxy: {e}. "
                "run `contained build` to build the proxy image, "
                "or pass `--network host` to bypass the allowlist."
            ) from e
    try:
        argv = build_argv(
            resolved,
            proxy_network=session.network if session else None,
        )
        return _execute(argv)
    finally:
        if session is not None:
            proxy.stop(session)


def _execute(argv: list[str]) -> int:
    """Run docker with stdio inherited; let signals flow to the child."""
    proc = subprocess.Popen(argv)
    try:
        return proc.wait()
    except KeyboardInterrupt:
        # Docker already received SIGINT via the shared process group;
        # wait for it to clean up rather than racing it to exit.
        return proc.wait()


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
    result = subprocess.run(cmd)
    if result.returncode != 0:
        raise DockerError(f"{what} build failed (tag={tag})")
    return tag
