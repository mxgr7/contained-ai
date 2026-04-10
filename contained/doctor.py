"""`contained doctor` — diagnose environment readiness.

Must run to completion even when Docker is missing. Each check is
independent and prints its own status line.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

from . import profiles
from .state import state_root


@dataclass
class Check:
    name: str
    ok: bool
    detail: str


def run_checks() -> list[Check]:
    return [
        _check_docker_binary(),
        _check_docker_daemon(),
        _check_state_dir(),
        _check_known_profiles(),
    ]


def _check_docker_binary() -> Check:
    path = shutil.which("docker")
    if path:
        return Check("docker binary", True, path)
    return Check("docker binary", False, "not found in PATH")


def _check_docker_daemon() -> Check:
    if shutil.which("docker") is None:
        return Check("docker daemon", False, "skipped (no docker binary)")
    try:
        result = subprocess.run(
            ["docker", "info", "--format", "{{.ServerVersion}}"],
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (subprocess.TimeoutExpired, OSError) as e:
        return Check("docker daemon", False, f"error: {e}")
    if result.returncode != 0:
        msg = result.stderr.strip().splitlines()[-1] if result.stderr.strip() else "unreachable"
        return Check("docker daemon", False, msg)
    return Check("docker daemon", True, f"server {result.stdout.strip()}")


def _check_state_dir() -> Check:
    root = state_root()
    try:
        root.mkdir(parents=True, exist_ok=True)
        probe = root / ".write-probe"
        probe.write_text("ok")
        probe.unlink()
    except OSError as e:
        return Check("state dir", False, f"{root}: {e}")
    return Check("state dir", True, str(root))


def _check_known_profiles() -> Check:
    names = profiles.names()
    return Check("agent profiles", True, ", ".join(names))


def format_report(checks: list[Check]) -> str:
    lines = ["contained doctor", ""]
    for c in checks:
        mark = "ok  " if c.ok else "FAIL"
        lines.append(f"  [{mark}] {c.name}: {c.detail}")
    lines.append("")
    failing = [c for c in checks if not c.ok]
    if failing:
        lines.append(f"{len(failing)} check(s) failed.")
    else:
        lines.append("all checks passed.")
    return "\n".join(lines)
