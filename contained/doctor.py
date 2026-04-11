"""`contained doctor` — diagnose environment readiness.

Must run to completion even when Docker is missing. Each check is
independent and prints its own status line.
"""

from __future__ import annotations

import shutil
import socket
import subprocess
from dataclasses import dataclass
from pathlib import Path

from . import profiles
from .config import ConfigError, discover, load
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
        *_check_allowlist_reachability(),
        *_check_ssh_allowlist_reachability(),
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


def _check_allowlist_reachability() -> list[Check]:
    """TCP-connect probe each tool-wide allowlist entry.

    Runs from the host, so this is an optimistic check: it tells you
    the network path exists, not that the in-container proxy will let
    it through. Per-profile and per-project allowlist entries are
    skipped to keep doctor fast.
    """
    out: list[Check] = []
    for entry in profiles.TOOL_DEFAULT_ALLOWLIST:
        host, _, port_s = entry.partition(":")
        try:
            port = int(port_s) if port_s else 443
        except ValueError:
            out.append(Check(f"allowlist {entry}", False, "malformed entry"))
            continue
        try:
            with socket.create_connection((host, port), timeout=2):
                out.append(Check(f"allowlist {entry}", True, "reachable"))
        except OSError as e:
            out.append(Check(f"allowlist {entry}", False, f"{e}"))
    return out


def _check_ssh_allowlist_reachability() -> list[Check]:
    """TCP-connect probe each SSH allowlist entry (PRD 09).

    Unlike the HTTPS default list, the SSH allowlist is always
    project-specific — profiles contribute nothing. Doctor discovers
    the nearest ``contained.yaml`` and probes whatever ``ssh.allowlist``
    entries it finds. Silent no-op if no config is discoverable.
    """
    try:
        cfg_path = discover(Path.cwd())
    except OSError:
        return []
    if cfg_path is None:
        return []
    try:
        loaded = load(cfg_path, cwd=Path.cwd())
    except ConfigError:
        return []
    out: list[Check] = []
    for entry in loaded.ssh_allowlist:
        host = entry.split(":", 1)[0].strip()
        if not host:
            continue
        try:
            with socket.create_connection((host, 22), timeout=2):
                out.append(Check(f"ssh allowlist {host}:22", True, "reachable"))
        except OSError as e:
            out.append(Check(f"ssh allowlist {host}:22", False, f"{e}"))
    return out


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
