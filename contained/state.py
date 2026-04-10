"""Per-project state directory resolution and credential seeding (PRD 03/05).

Layout:
  ${XDG_DATA_HOME:-~/.local/share}/contained/projects/<project-id>/
"""

from __future__ import annotations

import hashlib
import os
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

from .profiles import AgentProfile, FileSeed


def state_root() -> Path:
    xdg = os.environ.get("XDG_DATA_HOME")
    base = Path(xdg) if xdg else Path.home() / ".local" / "share"
    return base / "contained"


def project_id(project_path: Path) -> str:
    abspath = str(project_path.resolve())
    digest = hashlib.sha256(abspath.encode()).hexdigest()[:8]
    return f"{project_path.name}-{digest}"


def project_state_dir(project_path: Path) -> Path:
    return state_root() / "projects" / project_id(project_path)


def agent_state_dir(project_path: Path, agent_name: str) -> Path:
    return project_state_dir(project_path) / agent_name


def ensure_agent_state_dir(project_path: Path, agent_name: str) -> Path:
    d = agent_state_dir(project_path, agent_name)
    d.mkdir(parents=True, exist_ok=True)
    try:
        d.chmod(0o700)
    except OSError:
        pass
    parent = d.parent
    try:
        parent.chmod(0o700)
    except OSError:
        pass
    return d


# ---------------------------------------------------------------------------
# File seeding
# ---------------------------------------------------------------------------


@dataclass
class PlannedSeed:
    """Decision for a single FileSeed, computed during resolve().

    ``data`` is the bytes to write on apply, or ``None`` if nothing
    needs writing (file already present in state). ``source`` is the
    spec that resolved, ``"(cached)"`` if we reused an existing state
    file, or ``None`` if no host source was found (in which case
    ``data`` may still be the fallback placeholder).

    ``needs_mount`` is True when ``container_path`` lies outside the
    profile's ``state_mount`` directory and therefore needs its own
    file-level bind.
    """

    seed: FileSeed
    host_path: Path
    source: str | None
    data: bytes | None
    needs_mount: bool

    @property
    def available(self) -> bool:
        """True iff the host file will exist by the time docker runs."""
        return self.data is not None or self.host_path.exists()


def plan_seeds(profile: AgentProfile, pstate_dir: Path) -> list[PlannedSeed]:
    """Decide what to seed for each FileSeed, without writing anything."""
    plans: list[PlannedSeed] = []
    state_mount = profile.state_mount
    for seed in profile.file_seeds:
        host_path = pstate_dir / seed.state_rel
        needs_mount = not _path_under(seed.container_path, state_mount)

        if host_path.exists():
            plans.append(
                PlannedSeed(
                    seed=seed,
                    host_path=host_path,
                    source="(cached)",
                    data=None,
                    needs_mount=needs_mount,
                )
            )
            continue

        source: str | None = None
        data: bytes | None = None
        for src in seed.sources:
            blob = _read_source(src)
            if blob is not None:
                source = src
                data = blob
                break

        if data is None and needs_mount:
            # Placeholder so the bind-mount target exists. The
            # container's writes will then persist via the mount.
            data = seed.fallback_content

        plans.append(
            PlannedSeed(
                seed=seed,
                host_path=host_path,
                source=source,
                data=data,
                needs_mount=needs_mount,
            )
        )
    return plans


def apply_seeds(plans: list[PlannedSeed]) -> list[PlannedSeed]:
    """Write any pending seed data to disk. Returns the newly-written ones."""
    written: list[PlannedSeed] = []
    for p in plans:
        if p.data is None:
            continue
        if p.host_path.exists():
            continue
        p.host_path.parent.mkdir(parents=True, exist_ok=True)
        p.host_path.write_bytes(p.data)
        try:
            p.host_path.chmod(0o600)
        except OSError:
            pass
        written.append(p)
    return written


def _path_under(container_path: str, state_mount: str | None) -> bool:
    if not state_mount:
        return False
    base = state_mount.rstrip("/")
    return container_path == base or container_path.startswith(base + "/")


_keychain_cache: dict[str, str | None] = {}


def _read_source(source: str) -> bytes | None:
    if source.startswith("keychain:"):
        if sys.platform != "darwin":
            return None
        service = source[len("keychain:") :]
        val = _keychain_read(service)
        return val.encode() if val is not None else None

    expanded = os.path.expanduser(os.path.expandvars(source))
    path = Path(expanded)
    try:
        if path.is_symlink():
            return None
        if not path.is_file():
            return None
        return path.read_bytes()
    except OSError:
        return None


def _keychain_read(service: str) -> str | None:
    if service in _keychain_cache:
        return _keychain_cache[service]
    try:
        result = subprocess.run(
            ["security", "find-generic-password", "-s", service, "-w"],
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (subprocess.TimeoutExpired, OSError):
        _keychain_cache[service] = None
        return None
    if result.returncode != 0:
        _keychain_cache[service] = None
        return None
    value = result.stdout.rstrip("\n")
    _keychain_cache[service] = value
    return value
