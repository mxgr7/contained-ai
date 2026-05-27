#!/usr/bin/env python3
"""contained — container-side clipboard shim (file-based).

Installed in the agent base image as ``pbpaste``, ``xclip``, and
``wl-paste`` (symlinks to this script). For image requests it reads
from a bind-mounted file the host-side bridge maintains; for type-
listing probes (``xclip -t TARGETS -o``, ``wl-paste -l``) it reports
``image/png`` available iff that file exists.

Why file-based: Unix-socket bind-mounts from a macOS host into Docker
Desktop / OrbStack containers don't actually carry socket I/O — the
socket appears as a file but ``connect()`` returns ECONNREFUSED.
Regular file mounts work, so the bridge writes a PNG and the shim
reads it.
"""

from __future__ import annotations

import json
import os
import sys

# Bind-mount paths configured by contained/runtime.py. Kept overridable
# via env so test rigs can point at fixtures without rebuilding.
CLIPBOARD_FILE = os.environ.get(
    "CONTAINED_CLIPBOARD_FILE", "/var/contained/clipboard/image.png"
)
CLIPBOARD_META = os.environ.get(
    "CONTAINED_CLIPBOARD_META", "/var/contained/clipboard/meta.json"
)


def _has_image() -> bool:
    """Truthful answer to "is an image on the clipboard right now?".

    We consult the meta file first (it's tiny and always up to date) and
    fall back to checking the PNG itself in case meta is mid-rewrite.
    """
    try:
        with open(CLIPBOARD_META, "rb") as f:
            meta = json.loads(f.read())
        if isinstance(meta, dict) and meta.get("has_image"):
            return True
    except (OSError, ValueError):
        pass
    try:
        return os.path.getsize(CLIPBOARD_FILE) > 0
    except OSError:
        return False


def _is_list_query(name: str, args: list[str]) -> bool:
    """Detect "what types are on the clipboard?" probes."""
    if name == "xclip" and "TARGETS" in args:
        return True
    if name == "wl-paste" and any(a in ("-l", "--list-types") for a in args):
        return True
    return False


def _list_output(name: str, has_image: bool) -> bytes:
    """Synthetic types listing, gated on whether an image is actually present.

    Lying about availability would make Claude Code fetch ``image/png``
    every time and waste a roundtrip; reporting it only when the bridge
    has written a non-empty PNG keeps "no image found" responsive.
    """
    if name == "xclip":
        lines = ["TARGETS", "UTF8_STRING", "text/plain"]
        if has_image:
            lines.append("image/png")
        return ("\n".join(lines) + "\n").encode()
    lines: list[str] = []
    if has_image:
        lines.append("image/png")
    lines.append("text/plain")
    return ("\n".join(lines) + "\n").encode()


def _want_image(args: list[str]) -> bool:
    joined = " ".join(args)
    if "image/" in joined:
        return True
    return "public.png" in joined or "public.tiff" in joined


def _fetch_image() -> int:
    try:
        with open(CLIPBOARD_FILE, "rb") as f:
            data = f.read()
    except OSError:
        return 1
    if not data:
        return 1
    try:
        sys.stdout.buffer.write(data)
        sys.stdout.buffer.flush()
    except OSError:
        return 1
    return 0


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv if argv is None else argv)
    name = os.path.basename(argv[0]) if argv else ""
    args = argv[1:]

    if _is_list_query(name, args):
        try:
            sys.stdout.buffer.write(_list_output(name, _has_image()))
            sys.stdout.buffer.flush()
            return 0
        except OSError:
            return 1

    if _want_image(args):
        return _fetch_image()

    # Text or unrecognised — the file bridge only carries images.
    # Terminal-level bracketed paste already handles text Ctrl-V in the
    # outer shell, so falling through to "nothing" is correct here.
    return 1


if __name__ == "__main__":
    sys.exit(main())
