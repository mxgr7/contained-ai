"""Egress allowlist proxy sidecar (PRD 04).

For `network: allowlist` runs, the runtime starts a small tinyproxy
sidecar on a private Docker network and points the agent container at
it via HTTP(S)_PROXY. Tinyproxy's Filter directive gives us hostname-
level allowlisting for both HTTP and HTTPS CONNECT without having to
terminate TLS.

Network topology per run:

    [agent container]  -- private internal net --  [proxy container] -- default bridge -- internet

The agent net is created with `--internal` so the agent has no route
to the outside world; only the proxy straddles both networks.
"""

from __future__ import annotations

import re
import secrets
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path


PROXY_ALIAS = "proxy"
PROXY_PORT = 8888


class ProxyError(Exception):
    pass


@dataclass
class ProxySession:
    run_id: str
    network: str
    container: str
    filter_path: Path

    @property
    def proxy_url(self) -> str:
        return f"http://{PROXY_ALIAS}:{PROXY_PORT}"


def new_run_id() -> str:
    return secrets.token_hex(4)


def write_filter_file(allowlist: list[str]) -> Path:
    """Write a tinyproxy Filter file containing one anchored regex per host.

    Each allowlist entry is of the form `host` or `host:port`; we only
    filter on the hostname since tinyproxy's Filter matches CONNECT by
    hostname, not port.
    """
    lines: list[str] = []
    seen: set[str] = set()
    for entry in allowlist:
        host = entry.split(":", 1)[0].strip()
        if not host or host in seen:
            continue
        seen.add(host)
        lines.append(f"^{re.escape(host)}$")
    fd, path = tempfile.mkstemp(prefix="contained-filter-", suffix=".txt")
    Path(path).write_text("\n".join(lines) + "\n")
    import os
    os.close(fd)
    return Path(path)


def start(run_id: str, allowlist: list[str], proxy_image: str) -> ProxySession:
    """Create the private network and start the proxy container.

    Caller must invoke `stop()` on the returned session, even on error
    paths, to avoid leaking docker resources.
    """
    network = f"contained-net-{run_id}"
    container = f"contained-proxy-{run_id}"
    filter_path: Path | None = None
    try:
        filter_path = write_filter_file(allowlist)
        _run(["docker", "network", "create", "--internal", network])
        _run([
            "docker", "run", "-d", "--rm",
            "--name", container,
            "--mount",
            f"type=bind,src={filter_path},dst=/etc/tinyproxy/filter,ro",
            proxy_image,
        ])
        _run([
            "docker", "network", "connect",
            "--alias", PROXY_ALIAS,
            network, container,
        ])
    except Exception:
        stop(ProxySession(run_id, network, container, filter_path or Path("/dev/null")))
        raise
    return ProxySession(run_id, network, container, filter_path)


def stop(session: ProxySession) -> None:
    """Best-effort teardown. Never raises — used from finally blocks."""
    subprocess.run(
        ["docker", "rm", "-f", session.container],
        capture_output=True,
    )
    subprocess.run(
        ["docker", "network", "rm", session.network],
        capture_output=True,
    )
    try:
        session.filter_path.unlink(missing_ok=True)
    except OSError:
        pass


def _run(cmd: list[str]) -> None:
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        raise ProxyError(
            f"{' '.join(cmd)}: {result.stderr.strip() or 'failed'}"
        )
