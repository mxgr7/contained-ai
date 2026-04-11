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

import fcntl
import os
import re
import secrets
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path

PROXY_ALIAS = "proxy"
PROXY_PORT = 8888

LABEL_RUN_ID = "contained.run_id"
LABEL_PROJECT = "contained.project"
LABEL_FILTER_PATH = "contained.filter_path"

# Matches a filter line produced by `write_filter_file`:
#   ^<escaped-host>(:[0-9]+)?$
_FILTER_LINE_RE = re.compile(r"^\^(?P<host>.+?)\(:\[0-9\]\+\)\?\$$")


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


@dataclass
class RunInfo:
    run_id: str
    container: str
    project: str | None
    filter_path: Path


def new_run_id() -> str:
    return secrets.token_hex(4)


def write_filter_file(allowlist: list[str], *, state_dir: Path | None = None) -> Path:
    """Write a tinyproxy Filter file containing one anchored regex per host.

    Each allowlist entry is of the form `host` or `host:port`. Tinyproxy
    matches the Filter regex against the full CONNECT target, which
    includes the port (`github.com:443`), so we anchor with an optional
    `:<port>` suffix to match regardless of whether the client includes
    one.

    The file is created under ``state_dir`` (already 0700) when provided
    so the filter contents don't sit in world-readable ``/tmp``. The fd
    from ``mkstemp`` is used directly to avoid a re-open race.
    """
    lines: list[str] = []
    seen: set[str] = set()
    for entry in allowlist:
        host = entry.split(":", 1)[0].strip()
        if not host or host in seen:
            continue
        seen.add(host)
        lines.append(rf"^{re.escape(host)}(:[0-9]+)?$")
    mkstemp_dir = str(state_dir) if state_dir is not None else None
    fd, path = tempfile.mkstemp(
        prefix="contained-filter-", suffix=".txt", dir=mkstemp_dir
    )
    with os.fdopen(fd, "w") as f:
        f.write("\n".join(lines) + "\n")
    return Path(path)


def start(
    run_id: str,
    allowlist: list[str],
    proxy_image: str,
    *,
    state_dir: Path | None = None,
    project: Path | None = None,
) -> ProxySession:
    """Create the private network and start the proxy container.

    Caller must invoke `stop()` on the returned session, even on error
    paths, to avoid leaking docker resources.
    """
    network = f"contained-net-{run_id}"
    container = f"contained-proxy-{run_id}"
    filter_path: Path | None = None
    try:
        filter_path = write_filter_file(allowlist, state_dir=state_dir)
        _run(["docker", "network", "create", "--internal", network])
        run_cmd = [
            "docker", "run", "-d", "--rm",
            "--name", container,
            "--label", f"{LABEL_RUN_ID}={run_id}",
            "--label", f"{LABEL_FILTER_PATH}={filter_path}",
        ]
        if project is not None:
            run_cmd += ["--label", f"{LABEL_PROJECT}={project}"]
        run_cmd += [
            "--mount",
            f"type=bind,src={filter_path},dst=/etc/tinyproxy/filter,ro",
            proxy_image,
        ]
        _run(run_cmd)
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


def _host_from_filter_line(line: str) -> str | None:
    """Return the hostname encoded by a `write_filter_file` line, or None."""
    stripped = line.strip()
    if not stripped:
        return None
    m = _FILTER_LINE_RE.match(stripped)
    if not m:
        return None
    # Invert re.escape — every metacharacter becomes `\<c>`.
    return re.sub(r"\\(.)", r"\1", m.group("host"))


def read_filter_hosts(filter_path: Path) -> list[str]:
    """Parse an existing filter file back into an ordered list of hostnames.

    Lines that don't match the `write_filter_file` format are skipped;
    they're preserved on rewrite (see `append_to_filter`) but do not
    contribute to the dedupe set.
    """
    hosts: list[str] = []
    seen: set[str] = set()
    for line in filter_path.read_text().splitlines():
        host = _host_from_filter_line(line)
        if host is None or host in seen:
            continue
        seen.add(host)
        hosts.append(host)
    return hosts


def append_to_filter(
    filter_path: Path, new_entries: list[str]
) -> tuple[list[str], list[str]]:
    """Union `new_entries` into an existing filter file, in place.

    Returns ``(all_hosts, added)`` where ``all_hosts`` is the full
    ordered host list after the update and ``added`` is just the new
    ones (empty if every entry was already present). Idempotent:
    duplicates are a no-op.

    **In-place rewrite is load-bearing.** The proxy container
    bind-mounts this file as a single file, which docker binds by
    *inode*. A temp-file-plus-rename would give the new content a new
    inode and the container would keep seeing the old file forever, so
    `contained allow` would appear to succeed while tinyproxy reloaded
    a stale view. We truncate-and-write in place to keep the inode
    stable. Concurrency is covered by an fcntl lock on a sibling
    ``.lock`` file so two `contained allow` invocations serialize; the
    write-vs-tinyproxy-reader window is zero in practice because
    tinyproxy only re-reads its filter on SIGHUP, which we send
    *after* the write completes.

    Unrecognized lines in the existing file (e.g. hand-edits) are
    preserved verbatim above the regenerated host block.
    """
    if not filter_path.exists():
        raise ProxyError(f"filter file not found: {filter_path}")

    lock_path = filter_path.with_name(filter_path.name + ".lock")
    lock_fd = os.open(str(lock_path), os.O_CREAT | os.O_RDWR, 0o600)
    try:
        fcntl.flock(lock_fd, fcntl.LOCK_EX)

        existing_hosts: list[str] = []
        seen: set[str] = set()
        unknown_lines: list[str] = []
        for line in filter_path.read_text().splitlines():
            host = _host_from_filter_line(line)
            if host is None:
                if line.strip():
                    unknown_lines.append(line)
                continue
            if host in seen:
                continue
            seen.add(host)
            existing_hosts.append(host)

        added: list[str] = []
        for entry in new_entries:
            host = entry.split(":", 1)[0].strip()
            if not host or host in seen:
                continue
            seen.add(host)
            existing_hosts.append(host)
            added.append(host)

        regex_lines = [rf"^{re.escape(h)}(:[0-9]+)?$" for h in existing_hosts]
        body = "\n".join(unknown_lines + regex_lines) + "\n"

        # Truncate-and-write keeps the inode stable so the proxy
        # container's single-file bind mount sees the new content on
        # its next SIGHUP reload. Do NOT switch to temp-file-rename.
        fd = os.open(str(filter_path), os.O_WRONLY | os.O_TRUNC)
        try:
            os.write(fd, body.encode())
        finally:
            os.close(fd)
    finally:
        try:
            fcntl.flock(lock_fd, fcntl.LOCK_UN)
        finally:
            os.close(lock_fd)

    return (existing_hosts, added)


def reload(container: str) -> None:
    """Restart the proxy container so tinyproxy re-reads its Filter file.

    Note: SIGHUP does *not* work here. In the tinyproxy version we
    bundle, SIGHUP reloads the main config file but the Filter
    directive's host list is cached after the initial load, so live
    edits to the filter file are silently ignored until the process
    restarts. `docker restart` stops and starts the container, which
    re-runs the bind mount (picking up any on-disk edit) and forces a
    fresh Filter load. This survives the container's ``--rm`` — that
    only fires on the final exit, not on a restart — and keeps the
    existing network attachment so the agent doesn't have to
    re-join. The agent sees a ~1s egress blip mid-restart, which is
    acceptable for an explicit `contained allow` action.
    """
    _run(["docker", "restart", container])


def discover_runs() -> list[RunInfo]:
    """List active contained-proxy containers via docker labels.

    Source of truth is docker — no on-disk registry. If docker isn't
    reachable this raises ProxyError.
    """
    fmt = (
        '{{.Names}}\t'
        f'{{{{.Label "{LABEL_RUN_ID}"}}}}\t'
        f'{{{{.Label "{LABEL_PROJECT}"}}}}\t'
        f'{{{{.Label "{LABEL_FILTER_PATH}"}}}}'
    )
    try:
        result = subprocess.run(
            [
                "docker", "ps",
                "--filter", f"label={LABEL_RUN_ID}",
                "--format", fmt,
            ],
            capture_output=True, text=True, timeout=10,
        )
    except (subprocess.TimeoutExpired, OSError) as e:
        raise ProxyError(f"docker ps failed: {e}") from e
    if result.returncode != 0:
        raise ProxyError(
            f"docker ps failed: {result.stderr.strip() or 'unknown error'}"
        )
    runs: list[RunInfo] = []
    for line in result.stdout.splitlines():
        parts = line.split("\t")
        if len(parts) < 4:
            continue
        container, run_id, project, filter_path = parts[:4]
        if not run_id or not filter_path:
            continue
        runs.append(
            RunInfo(
                run_id=run_id,
                container=container,
                project=project or None,
                filter_path=Path(filter_path),
            )
        )
    return runs
