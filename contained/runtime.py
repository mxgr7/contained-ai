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
from importlib import resources
from pathlib import Path

from . import profiles
from .run import ResolvedRun
from .state import state_root


class RuntimeError(Exception):
    pass


OVERLAY_FROM_PLACEHOLDER = "contained-base"


def ensure_daemon() -> None:
    if shutil.which("docker") is None:
        raise RuntimeError(
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
        raise RuntimeError(f"could not reach docker daemon: {e}") from e
    if result.returncode != 0:
        detail = (
            result.stderr.strip().splitlines()[-1]
            if result.stderr.strip()
            else "daemon unreachable"
        )
        raise RuntimeError(
            f"docker daemon is not reachable: {detail}. "
            "on macOS, start Docker Desktop. see `contained doctor`."
        )


def build_argv(run: ResolvedRun, *, mask_secrets: bool = False) -> list[str]:
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
    # allowlist: proxy sidecar is PRD 04; fall back to default bridge.
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
        raise RuntimeError(
            f"{dockerfile}: first FROM must be `FROM {OVERLAY_FROM_PLACEHOLDER}`"
        )
    rewritten = source.replace(placeholder, f"FROM {base_image}", 1)

    cmd = ["docker", "build", "-t", tag, "-f", "-", str(dockerfile.parent)]
    result = subprocess.run(cmd, input=rewritten, text=True)
    if result.returncode != 0:
        raise RuntimeError(f"overlay build failed for {dockerfile}")
    return tag


def run(resolved: ResolvedRun, cwd: Path) -> int:
    ensure_daemon()

    overlay = find_overlay(resolved, cwd)
    if overlay is not None:
        image = build_overlay(
            resolved.agent.name,
            overlay,
            resolved.image,
            rebuild=resolved.rebuild,
        )
        resolved = dataclasses.replace(resolved, image=image)

    argv = build_argv(resolved)
    return _execute(argv)


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


def build_base(tag: str | None = None, *, rebuild: bool = False) -> str:
    """Build the shared base image locally.

    `tag` defaults to the profile base image ref so `contained run` picks
    it up without `--image`. `rebuild` forces `--no-cache`.
    """
    if shutil.which("docker") is None:
        raise RuntimeError(
            "docker binary not found in PATH. install Docker Desktop (macOS) "
            "or docker-ce (Linux), then re-run."
        )
    resolved_tag = tag or profiles.BASE_IMAGE
    dockerfile = base_dockerfile_path()
    cmd = ["docker", "build", "-t", resolved_tag, "-f", str(dockerfile)]
    if rebuild:
        cmd.append("--no-cache")
    cmd.append(str(dockerfile.parent))
    result = subprocess.run(cmd)
    if result.returncode != 0:
        raise RuntimeError(f"base image build failed (tag={resolved_tag})")
    return resolved_tag
