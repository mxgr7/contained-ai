"""Tests for the host-side clipboard bridge wiring (file-based)."""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from contained import clipboard, runtime
from contained.config import ConfigSection, LoadedConfig
from contained.run import CliOverrides, render_dry_run, resolve


def _resolved(tmp_path: Path, **cli_kwargs):
    loaded = LoadedConfig(
        path=None,
        base_dir=tmp_path,
        default_agent=None,
        defaults=ConfigSection(),
        agents={},
    )
    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    return resolve(
        "claude",
        loaded,
        CliOverrides(**cli_kwargs),
        cwd=tmp_path,
        host_env={"HOME": str(home)},
    )


# ---------------------------------------------------------------------------
# CLI / config wiring
# ---------------------------------------------------------------------------


def test_resolve_default_enables_clipboard_bridge(tmp_path: Path):
    r = _resolved(tmp_path)
    assert r.clipboard_bridge is True


def test_resolve_disabled_via_override(tmp_path: Path):
    r = _resolved(tmp_path, clipboard_bridge=False)
    assert r.clipboard_bridge is False


def test_cli_default_is_on(tmp_path: Path, capsys, monkeypatch):
    from contained import cli

    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    rc = cli.main(["run", "claude", "--dry-run"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "clipboard_bridge: enabled" in out


def test_cli_no_clipboard_bridge_flag_disables(tmp_path: Path, capsys, monkeypatch):
    from contained import cli

    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    rc = cli.main(["run", "claude", "--dry-run", "--no-clipboard-bridge"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "clipboard_bridge: disabled" in out


# ---------------------------------------------------------------------------
# build_argv: dir-mount + envs only when bridge enabled AND dir supplied
# ---------------------------------------------------------------------------


def test_build_argv_omits_clipboard_mount_without_dir(tmp_path: Path):
    r = _resolved(tmp_path)
    argv = runtime.build_argv(r)
    joined = " ".join(argv)
    assert clipboard.CONTAINER_DIR not in joined
    assert "CONTAINED_CLIPBOARD_FILE" not in joined


def test_build_argv_emits_clipboard_mount_when_dir_supplied(tmp_path: Path):
    r = _resolved(tmp_path)
    cdir = tmp_path / "clipdir"
    cdir.mkdir()
    argv = runtime.build_argv(r, clipboard_host_dir=cdir)
    joined = " ".join(argv)
    assert f"src={cdir}" in joined
    assert f"dst={clipboard.CONTAINER_DIR}" in joined
    assert f"CONTAINED_CLIPBOARD_FILE={clipboard.CONTAINER_IMAGE_PATH}" in joined
    assert f"CONTAINED_CLIPBOARD_META={clipboard.CONTAINER_META_PATH}" in joined


def test_build_argv_skips_clipboard_when_bridge_disabled(tmp_path: Path):
    r = _resolved(tmp_path, clipboard_bridge=False)
    cdir = tmp_path / "clipdir"
    cdir.mkdir()
    argv = runtime.build_argv(r, clipboard_host_dir=cdir)
    joined = " ".join(argv)
    assert clipboard.CONTAINER_DIR not in joined
    assert "CONTAINED_CLIPBOARD_FILE" not in joined


def test_dry_run_preview_includes_clipboard_mount(tmp_path: Path):
    r = _resolved(tmp_path)
    out = render_dry_run(r, host_env={})
    assert "clipboard_bridge: enabled" in out
    assert clipboard.CONTAINER_DIR in out
    assert clipboard.CONTAINER_IMAGE_PATH in out


# ---------------------------------------------------------------------------
# Lifecycle: start / stop / failure modes
# ---------------------------------------------------------------------------


def test_start_clipboard_bridge_creates_meta_and_runs(tmp_path: Path):
    bridge = clipboard.start(state_dir=tmp_path)
    try:
        assert bridge.host_dir.is_dir()
        assert bridge.meta_path.is_file()
        meta = json.loads(bridge.meta_path.read_text())
        assert meta["has_image"] is False
        assert meta["size"] == 0
        # Bridge process is alive and the image file is absent (empty clipboard).
        assert bridge.process.poll() is None
        assert not bridge.image_path.exists()
    finally:
        clipboard.stop(bridge)


def test_start_clipboard_bridge_with_no_state_uses_tempdir():
    bridge = clipboard.start(state_dir=None)
    try:
        assert bridge.owns_dir is True
        assert bridge.meta_path.is_file()
    finally:
        clipboard.stop(bridge)


def test_start_clipboard_bridge_failure_raises(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(
        clipboard, "bridge_script_path", lambda: tmp_path / "does-not-exist.py"
    )
    with pytest.raises(clipboard.ClipboardError, match="did not start"):
        clipboard.start(state_dir=tmp_path)


def test_stop_is_idempotent_after_process_already_exited(tmp_path: Path):
    bridge = clipboard.start(state_dir=tmp_path)
    bridge.process.kill()
    bridge.process.wait(timeout=2)
    clipboard.stop(bridge)  # no raise


# ---------------------------------------------------------------------------
# Host-capability detection
# ---------------------------------------------------------------------------


def test_host_supports_clipboard_darwin(monkeypatch):
    monkeypatch.setattr(clipboard.sys, "platform", "darwin")
    monkeypatch.setattr(clipboard.shutil, "which", lambda t: "/usr/bin/" + t)
    assert clipboard.host_supports_clipboard() is True


def test_host_supports_clipboard_linux_with_xclip(monkeypatch):
    monkeypatch.setattr(clipboard.sys, "platform", "linux")
    monkeypatch.setattr(
        clipboard.shutil, "which",
        lambda t: "/usr/bin/xclip" if t == "xclip" else None,
    )
    assert clipboard.host_supports_clipboard() is True


def test_host_supports_clipboard_linux_headless_returns_false(monkeypatch):
    monkeypatch.setattr(clipboard.sys, "platform", "linux")
    monkeypatch.setattr(clipboard.shutil, "which", lambda t: None)
    assert clipboard.host_supports_clipboard() is False


# ---------------------------------------------------------------------------
# runtime.run integrates the bridge
# ---------------------------------------------------------------------------


def _fake_bridge(tmp_path: Path) -> clipboard.ClipboardBridge:
    cdir = tmp_path / "clipboard"
    cdir.mkdir(exist_ok=True)
    return clipboard.ClipboardBridge(
        host_dir=cdir,
        image_path=cdir / "image.png",
        meta_path=cdir / "meta.json",
        process=MagicMock(poll=lambda: None),
        log_path=cdir / "clipboard-bridge.log",
        owns_dir=False,
    )


def test_run_starts_and_stops_clipboard_bridge(
    tmp_path: Path, monkeypatch, mock_proxy
):
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "xdg"))
    monkeypatch.setattr(runtime, "ensure_daemon", lambda: None)
    started: list = []
    stopped: list[clipboard.ClipboardBridge] = []

    def fake_start(*, state_dir):
        started.append(state_dir)
        return _fake_bridge(tmp_path)

    monkeypatch.setattr(clipboard, "host_supports_clipboard", lambda: True)
    monkeypatch.setattr(clipboard, "start", fake_start)
    monkeypatch.setattr(clipboard, "stop", lambda b: stopped.append(b))

    captured: dict[str, list[str]] = {}
    monkeypatch.setattr(
        runtime, "_execute",
        lambda argv: captured.__setitem__("argv", argv) or 0,
    )
    r = _resolved(tmp_path)
    rc = runtime.run(r, tmp_path)
    assert rc == 0
    assert len(started) == 1
    assert len(stopped) == 1
    joined = " ".join(captured["argv"])
    assert f"dst={clipboard.CONTAINER_DIR}" in joined
    assert "CONTAINED_CLIPBOARD_FILE=" in joined


def test_run_skips_bridge_when_host_unsupported(
    tmp_path: Path, monkeypatch, mock_proxy, capsys
):
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "xdg"))
    monkeypatch.setattr(runtime, "ensure_daemon", lambda: None)
    monkeypatch.setattr(clipboard, "host_supports_clipboard", lambda: False)
    started: list = []
    monkeypatch.setattr(clipboard, "start", lambda **kw: started.append(kw))

    captured: dict[str, list[str]] = {}
    monkeypatch.setattr(
        runtime, "_execute",
        lambda argv: captured.__setitem__("argv", argv) or 0,
    )
    r = _resolved(tmp_path)
    rc = runtime.run(r, tmp_path)
    assert rc == 0
    assert started == []
    err = capsys.readouterr().err
    assert "no host clipboard tool" in err
    joined = " ".join(captured["argv"])
    assert clipboard.CONTAINER_DIR not in joined


def test_run_skips_bridge_when_disabled_by_flag(
    tmp_path: Path, monkeypatch, mock_proxy
):
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "xdg"))
    monkeypatch.setattr(runtime, "ensure_daemon", lambda: None)
    started: list = []
    monkeypatch.setattr(clipboard, "host_supports_clipboard", lambda: True)
    monkeypatch.setattr(clipboard, "start", lambda **kw: started.append(kw))
    monkeypatch.setattr(runtime, "_execute", lambda argv: 0)
    r = _resolved(tmp_path, clipboard_bridge=False)
    runtime.run(r, tmp_path)
    assert started == []


def test_run_stops_bridge_even_when_execute_raises(
    tmp_path: Path, monkeypatch, mock_proxy
):
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "xdg"))
    monkeypatch.setattr(runtime, "ensure_daemon", lambda: None)
    monkeypatch.setattr(clipboard, "host_supports_clipboard", lambda: True)
    bridge = _fake_bridge(tmp_path)
    monkeypatch.setattr(clipboard, "start", lambda **kw: bridge)
    stopped: list[clipboard.ClipboardBridge] = []
    monkeypatch.setattr(clipboard, "stop", lambda b: stopped.append(b))

    def boom(_argv):
        raise KeyboardInterrupt()

    monkeypatch.setattr(runtime, "_execute", boom)
    with pytest.raises(KeyboardInterrupt):
        runtime.run(_resolved(tmp_path), tmp_path)
    assert stopped == [bridge]


def test_run_continues_when_bridge_start_fails(
    tmp_path: Path, monkeypatch, mock_proxy, capsys
):
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "xdg"))
    monkeypatch.setattr(runtime, "ensure_daemon", lambda: None)
    monkeypatch.setattr(clipboard, "host_supports_clipboard", lambda: True)

    def fail(**kw):
        raise clipboard.ClipboardError("nope")

    monkeypatch.setattr(clipboard, "start", fail)
    monkeypatch.setattr(runtime, "_execute", lambda argv: 0)
    rc = runtime.run(_resolved(tmp_path), tmp_path)
    assert rc == 0
    err = capsys.readouterr().err
    assert "clipboard bridge failed to start" in err


# ---------------------------------------------------------------------------
# Shim behaviour
# ---------------------------------------------------------------------------


SHIM_PATH = Path(__file__).parent.parent / "contained" / "assets" / "clipboard-shim.py"


def _run_shim(
    invoke_as: str,
    *args: str,
    image_file: str = "/nonexistent",
    meta_file: str = "/nonexistent",
) -> tuple[int, bytes]:
    """Invoke the shim with argv[0] basename == ``invoke_as``."""
    with tempfile.TemporaryDirectory() as td:
        link = Path(td) / invoke_as
        link.symlink_to(SHIM_PATH)
        r = subprocess.run(
            [str(link), *args],
            capture_output=True,
            timeout=5,
            env={
                "CONTAINED_CLIPBOARD_FILE": image_file,
                "CONTAINED_CLIPBOARD_META": meta_file,
                "PATH": "/usr/bin:/bin",
            },
        )
    return r.returncode, r.stdout


def _meta(tmp_path: Path, has_image: bool, size: int) -> Path:
    p = tmp_path / "meta.json"
    p.write_text(json.dumps({"has_image": has_image, "size": size}))
    return p


def test_shim_targets_omits_image_when_clipboard_has_none(tmp_path: Path):
    meta = _meta(tmp_path, has_image=False, size=0)
    rc, out = _run_shim(
        "xclip", "-selection", "clipboard", "-t", "TARGETS", "-o",
        meta_file=str(meta),
    )
    assert rc == 0
    assert b"TARGETS" in out
    assert b"text/plain" in out
    assert b"image/png" not in out


def test_shim_targets_includes_image_when_meta_says_so(tmp_path: Path):
    img = tmp_path / "image.png"
    img.write_bytes(b"\x89PNG\r\n\x1a\nfake")
    meta = _meta(tmp_path, has_image=True, size=img.stat().st_size)
    rc, out = _run_shim(
        "xclip", "-selection", "clipboard", "-t", "TARGETS", "-o",
        image_file=str(img), meta_file=str(meta),
    )
    assert rc == 0
    assert b"image/png" in out


def test_shim_wl_paste_list_types_includes_image(tmp_path: Path):
    img = tmp_path / "image.png"
    img.write_bytes(b"\x89PNGdata")
    meta = _meta(tmp_path, has_image=True, size=img.stat().st_size)
    rc, out = _run_shim(
        "wl-paste", "-l", image_file=str(img), meta_file=str(meta),
    )
    assert rc == 0
    assert b"image/png" in out


def test_shim_image_request_reads_file(tmp_path: Path):
    img = tmp_path / "image.png"
    payload = b"\x89PNG\r\n\x1a\n" + b"x" * 200
    img.write_bytes(payload)
    meta = _meta(tmp_path, has_image=True, size=len(payload))
    rc, out = _run_shim(
        "xclip", "-selection", "clipboard", "-t", "image/png", "-o",
        image_file=str(img), meta_file=str(meta),
    )
    assert rc == 0
    assert out == payload


def test_shim_image_request_returns_empty_when_file_missing(tmp_path: Path):
    rc, out = _run_shim(
        "xclip", "-selection", "clipboard", "-t", "image/png", "-o",
    )
    assert rc == 1
    assert out == b""


def test_shim_pbpaste_image_request_reads_file(tmp_path: Path):
    img = tmp_path / "image.png"
    img.write_bytes(b"\x89PNGbytes")
    meta = _meta(tmp_path, has_image=True, size=img.stat().st_size)
    rc, out = _run_shim(
        "pbpaste", "-Prefer", "public.png",
        image_file=str(img), meta_file=str(meta),
    )
    assert rc == 0
    assert out == b"\x89PNGbytes"


# ---------------------------------------------------------------------------
# Bridge daemon (script) — direct end-to-end on Linux
# ---------------------------------------------------------------------------


def test_bridge_writes_meta_on_startup(tmp_path: Path):
    """Smoke test: spawning the bridge produces the meta file quickly."""
    img = tmp_path / "image.png"
    meta = tmp_path / "meta.json"
    proc = subprocess.Popen(
        [
            sys.executable, "-u",
            str(Path(__file__).parent.parent / "contained" / "assets" / "clipboard-bridge.py"),
            "--output", str(img),
            "--output-meta", str(meta),
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline and not meta.exists():
            time.sleep(0.05)
        assert meta.exists(), "bridge did not write meta within 3s"
        data = json.loads(meta.read_text())
        assert "has_image" in data
    finally:
        proc.terminate()
        proc.wait(timeout=3)
