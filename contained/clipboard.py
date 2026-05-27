"""Host-side clipboard bridge lifecycle (file-based).

Spawns a small Python daemon (``assets/clipboard-bridge.py``) that
polls the host clipboard and writes the current image to a regular
file inside a bind-mounted directory. The container-side shim reads
that file directly.

Why this isn't a Unix socket: bind-mounting an AF_UNIX socket from a
macOS host into a Docker Desktop / OrbStack container surfaces the
socket as a file with the correct inode type, but the filesystem-
sharing layer does NOT carry socket I/O — ``connect()`` returns
ECONNREFUSED. Regular file mounts propagate updates fine, so the
bridge writes a PNG and the shim reads it.

Mirrors :mod:`contained.proxy` in shape: ``start`` returns a session
handle, ``stop`` is best-effort and safe in ``finally`` blocks.
"""

from __future__ import annotations

import contextlib
import shutil
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass
from importlib import resources
from pathlib import Path


# Container-side paths the shim reads from. The runtime bind-mounts
# the host dir at the parent of these paths; the shim resolves the
# exact filenames via env vars (CONTAINED_CLIPBOARD_FILE / _META)
# baked into the docker run by runtime.build_argv.
CONTAINER_DIR = "/var/contained/clipboard"
CONTAINER_IMAGE_PATH = f"{CONTAINER_DIR}/image.png"
CONTAINER_META_PATH = f"{CONTAINER_DIR}/meta.json"

_STARTUP_TIMEOUT_SEC = 3.0
_STARTUP_POLL_SEC = 0.05
_STOP_TIMEOUT_SEC = 2.0


class ClipboardError(Exception):
    pass


@dataclass
class ClipboardBridge:
    host_dir: Path
    image_path: Path
    meta_path: Path
    process: subprocess.Popen
    log_path: Path
    owns_dir: bool = False


def host_supports_clipboard() -> bool:
    """Best-effort host capability check.

    Returns False when no plausible clipboard tool is reachable — e.g.
    headless Linux server with no X / Wayland session. The bridge is a
    no-op in that case and there's no point spawning it.
    """
    if sys.platform == "darwin":
        return shutil.which("osascript") is not None
    if sys.platform.startswith("linux"):
        return any(shutil.which(t) for t in ("wl-paste", "xclip", "xsel"))
    return False


def bridge_script_path() -> Path:
    """Filesystem path to the bundled ``clipboard-bridge.py`` asset."""
    ref = resources.files("contained").joinpath("assets/clipboard-bridge.py")
    with resources.as_file(ref) as p:
        return Path(p)


def start(*, state_dir: Path | None = None) -> ClipboardBridge:
    """Spawn the bridge daemon and return a session handle.

    The bridge writes inside ``<state_dir>/clipboard/`` when state is
    enabled, else a fresh tempdir (--no-state). Bridge stderr is
    redirected to a log file in the same dir so its writes don't land
    inside Claude Code's alt-screen TUI where they'd be invisibly
    overdrawn.
    """
    if state_dir is not None:
        parent_dir = state_dir
        owns_dir = False
    else:
        parent_dir = Path(tempfile.mkdtemp(prefix="contained-clipboard-"))
        owns_dir = True
    host_dir = parent_dir / "clipboard"
    host_dir.mkdir(parents=True, exist_ok=True)
    image_path = host_dir / "image.png"
    meta_path = host_dir / "meta.json"
    log_path = host_dir / "clipboard-bridge.log"

    try:
        log_fh = open(log_path, "wb")  # truncate per-run
    except OSError as e:
        if owns_dir:
            shutil.rmtree(parent_dir, ignore_errors=True)
        raise ClipboardError(
            f"cannot open clipboard bridge log {log_path}: {e}"
        ) from e

    script = bridge_script_path()
    try:
        proc = subprocess.Popen(
            [
                sys.executable, "-u", str(script),
                "--output", str(image_path),
                "--output-meta", str(meta_path),
            ],
            stdin=subprocess.DEVNULL,
            stdout=log_fh,
            stderr=log_fh,
        )
    except OSError as e:
        log_fh.close()
        if owns_dir:
            shutil.rmtree(parent_dir, ignore_errors=True)
        raise ClipboardError(f"failed to spawn clipboard bridge: {e}") from e
    finally:
        log_fh.close()

    # Wait for the meta file as a liveness signal — the bridge writes it
    # right after it starts, before its first poll. If the process exits
    # before then, surface the log tail in the error.
    deadline = time.monotonic() + _STARTUP_TIMEOUT_SEC
    while time.monotonic() < deadline:
        if meta_path.exists():
            return ClipboardBridge(
                host_dir=host_dir,
                image_path=image_path,
                meta_path=meta_path,
                process=proc,
                log_path=log_path,
                owns_dir=owns_dir,
            )
        if proc.poll() is not None:
            break
        time.sleep(_STARTUP_POLL_SEC)

    if proc.poll() is None:
        with contextlib.suppress(OSError):
            proc.terminate()
        with contextlib.suppress(Exception):
            proc.wait(timeout=_STOP_TIMEOUT_SEC)
    log_tail = ""
    try:
        log_tail = log_path.read_text(errors="replace").strip()
    except OSError:
        pass
    if owns_dir:
        shutil.rmtree(parent_dir, ignore_errors=True)
    raise ClipboardError(
        f"clipboard bridge did not start within "
        f"{_STARTUP_TIMEOUT_SEC:.0f}s (process exit={proc.returncode})"
        + (f"\nlog tail:\n{log_tail}" if log_tail else "")
    )


def stop(bridge: ClipboardBridge) -> None:
    """Best-effort teardown. Never raises — safe in ``finally`` blocks."""
    proc = bridge.process
    if proc.poll() is None:
        with contextlib.suppress(OSError):
            proc.terminate()
        try:
            proc.wait(timeout=_STOP_TIMEOUT_SEC)
        except subprocess.TimeoutExpired:
            with contextlib.suppress(OSError):
                proc.kill()
            with contextlib.suppress(Exception):
                proc.wait(timeout=_STOP_TIMEOUT_SEC)
    for p in (bridge.image_path, bridge.meta_path):
        with contextlib.suppress(OSError):
            p.unlink()
    if bridge.owns_dir:
        shutil.rmtree(bridge.host_dir.parent, ignore_errors=True)
