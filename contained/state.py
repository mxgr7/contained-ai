"""Per-project state directory resolution (PRD 03).

Layout:
  ${XDG_DATA_HOME:-~/.local/share}/contained/projects/<project-id>/
"""

from __future__ import annotations

import hashlib
import os
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
