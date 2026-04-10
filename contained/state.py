"""Per-project state directory resolution (PRD 03).

Layout:
  ${XDG_DATA_HOME:-~/.local/share}/contained/projects/<project-id>/
"""

from __future__ import annotations

import hashlib
import os
import shutil
from pathlib import Path


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


def seed_credentials(state_dir: Path, seeds: dict[str, str]) -> list[Path]:
    """Copy host credential files into the per-project state dir.

    Only seeds files that don't already exist in state_dir. Missing host
    files are silently skipped — the agent will prompt for auth on first
    run if nothing got seeded. Returns the list of newly seeded paths.
    """
    seeded: list[Path] = []
    for host_spec, rel in seeds.items():
        src = Path(os.path.expanduser(os.path.expandvars(host_spec)))
        dst = state_dir / rel
        if dst.exists():
            continue
        if not src.is_file():
            continue
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dst)
        try:
            dst.chmod(0o600)
        except OSError:
            pass
        seeded.append(dst)
    return seeded
