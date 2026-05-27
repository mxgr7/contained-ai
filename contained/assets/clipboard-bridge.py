#!/usr/bin/env python3
"""contained — host-side clipboard bridge (file-based).

Polls the host clipboard and writes the current image to a bind-mounted
file on change. The container-side shim reads that file directly.

Why file-based and not a Unix socket: bind-mounting a Unix socket from a
macOS host into a Docker Desktop / OrbStack container surfaces the
socket as a file with the right inode type, but the filesystem-sharing
layer (VirtioFS / gRPC FUSE / OrbStack's FS) doesn't actually carry
socket I/O across the macOS↔Linux-VM boundary. ``connect()`` from inside
the container returns ECONNREFUSED. Regular file bind-mounts work fine,
so we use one of those instead.

Layout:
    --output <path>     PNG written here when an image is on the
                        clipboard; deleted when it isn't.
    --output-meta <path> JSON-ish single-line snapshot of what's on the
                         clipboard, refreshed every poll. Lets the shim
                         answer "are there available types?" queries
                         (xclip -t TARGETS -o, wl-paste -l) without
                         racing the PNG write.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import shutil
import signal
import subprocess
import sys
import tempfile
import time

_POLL_INTERVAL_SEC = 0.25
_CLIPBOARD_TIMEOUT_SEC = 5.0


def _log(msg: str) -> None:
    print(f"contained-clipboard: {msg}", file=sys.stderr, flush=True)


def read_clipboard_image() -> bytes:
    system = platform.system()
    if system == "Darwin":
        return _read_darwin()
    if system == "Linux":
        return _read_linux()
    return b""


def _read_darwin() -> bytes:
    png = _osascript_png()
    if png:
        return png
    return _osascript_tiff_to_png()


def _read_linux() -> bytes:
    if shutil.which("wl-paste"):
        out = _capture(["wl-paste", "--no-newline", "--type", "image/png"])
        if out:
            return out
    if shutil.which("xclip"):
        return _capture(
            ["xclip", "-selection", "clipboard", "-t", "image/png", "-o"]
        )
    return b""


def _capture(argv: list[str]) -> bytes:
    try:
        result = subprocess.run(
            argv, capture_output=True, timeout=_CLIPBOARD_TIMEOUT_SEC
        )
    except (subprocess.TimeoutExpired, OSError):
        return b""
    if result.returncode != 0:
        return b""
    return result.stdout


def _osascript_png() -> bytes:
    fd, path = tempfile.mkstemp(prefix="contained-clip-", suffix=".png")
    os.close(fd)
    try:
        argv = [
            "osascript",
            "-e", f'set f to open for access POSIX file "{path}" with write permission',
            "-e", "try",
            "-e", 'write (the clipboard as «class PNGf») to f',
            "-e", "end try",
            "-e", "close access f",
        ]
        try:
            result = subprocess.run(
                argv, capture_output=True, timeout=_CLIPBOARD_TIMEOUT_SEC
            )
        except (subprocess.TimeoutExpired, OSError) as e:
            _log(f"osascript PNG spawn failed: {e}")
            return b""
        if result.returncode != 0:
            _log(
                "osascript PNG exit "
                f"{result.returncode}: "
                f"{result.stderr.decode('utf-8', errors='replace').strip()}"
            )
            return b""
        try:
            with open(path, "rb") as fh:
                return fh.read()
        except OSError:
            return b""
    finally:
        try:
            os.unlink(path)
        except OSError:
            pass


def _osascript_tiff_to_png() -> bytes:
    fd, tiff_path = tempfile.mkstemp(prefix="contained-clip-", suffix=".tiff")
    os.close(fd)
    png_path = tiff_path + ".png"
    try:
        argv = [
            "osascript",
            "-e", f'set f to open for access POSIX file "{tiff_path}" with write permission',
            "-e", "try",
            "-e", 'write (the clipboard as «class TIFF») to f',
            "-e", "end try",
            "-e", "close access f",
        ]
        try:
            result = subprocess.run(
                argv, capture_output=True, timeout=_CLIPBOARD_TIMEOUT_SEC
            )
        except (subprocess.TimeoutExpired, OSError):
            return b""
        if result.returncode != 0:
            return b""
        try:
            if os.path.getsize(tiff_path) == 0:
                return b""
        except OSError:
            return b""
        try:
            sips = subprocess.run(
                ["sips", "-s", "format", "png", tiff_path, "--out", png_path],
                capture_output=True, timeout=_CLIPBOARD_TIMEOUT_SEC,
            )
        except (subprocess.TimeoutExpired, OSError):
            return b""
        if sips.returncode != 0:
            return b""
        try:
            with open(png_path, "rb") as fh:
                return fh.read()
        except OSError:
            return b""
    finally:
        for p in (tiff_path, png_path):
            try:
                os.unlink(p)
            except OSError:
                pass


def _atomic_write(path: str, data: bytes) -> None:
    """Write to ``path`` atomically via ``rename`` to keep readers consistent."""
    tmp = f"{path}.tmp.{os.getpid()}"
    with open(tmp, "wb") as f:
        f.write(data)
    os.replace(tmp, path)


def _atomic_unlink(path: str) -> None:
    try:
        os.unlink(path)
    except FileNotFoundError:
        pass
    except OSError as e:
        _log(f"unlink {path}: {e}")


def _write_meta(path: str, has_image: bool, size: int) -> None:
    meta = {"has_image": has_image, "size": size, "updated": time.time()}
    try:
        _atomic_write(path, (json.dumps(meta) + "\n").encode())
    except OSError as e:
        _log(f"write meta failed: {e}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="contained-clipboard-bridge")
    parser.add_argument("--output", required=True, help="PNG output path")
    parser.add_argument("--output-meta", required=True, help="JSON meta path")
    args = parser.parse_args(argv)

    out_path = args.output
    meta_path = args.output_meta
    os.makedirs(os.path.dirname(out_path), exist_ok=True)

    # Start clean — any stale file from a previous run is misleading.
    _atomic_unlink(out_path)
    _write_meta(meta_path, has_image=False, size=0)
    _log(f"watching clipboard, writing {out_path} (platform={platform.system()})")

    stop = {"flag": False}

    def _on_term(*_args: object) -> None:
        stop["flag"] = True

    signal.signal(signal.SIGTERM, _on_term)
    signal.signal(signal.SIGINT, _on_term)

    last_hash: str | None = None
    while not stop["flag"]:
        try:
            png = read_clipboard_image()
        except Exception as e:  # noqa: BLE001
            _log(f"read raised: {e}")
            png = b""
        h = hashlib.sha256(png).hexdigest() if png else None
        if h != last_hash:
            if png:
                try:
                    _atomic_write(out_path, png)
                    _write_meta(meta_path, has_image=True, size=len(png))
                    _log(f"updated {out_path} ({len(png)} bytes)")
                except OSError as e:
                    _log(f"write failed: {e}")
            else:
                _atomic_unlink(out_path)
                _write_meta(meta_path, has_image=False, size=0)
                _log(f"cleared {out_path}")
            last_hash = h
        # Short interruptible sleep so SIGTERM is responsive.
        for _ in range(int(_POLL_INTERVAL_SEC / 0.05)):
            if stop["flag"]:
                break
            time.sleep(0.05)

    _atomic_unlink(out_path)
    _atomic_unlink(meta_path)
    _log("bridge stopped")
    return 0


if __name__ == "__main__":
    sys.exit(main())
