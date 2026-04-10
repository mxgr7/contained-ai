from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from contained import proxy


def test_write_filter_file_escapes_and_dedupes(tmp_path: Path):
    path = proxy.write_filter_file(
        ["api.anthropic.com:443", "api.anthropic.com:443", "github.com:443"]
    )
    try:
        lines = [ln for ln in path.read_text().splitlines() if ln]
        assert lines == [r"^api\.anthropic\.com$", r"^github\.com$"]
    finally:
        path.unlink(missing_ok=True)


def test_write_filter_file_skips_blank(tmp_path: Path):
    path = proxy.write_filter_file(["", "  ", "example.com:443"])
    try:
        lines = [ln for ln in path.read_text().splitlines() if ln]
        assert lines == [r"^example\.com$"]
    finally:
        path.unlink(missing_ok=True)


def test_new_run_id_is_unique():
    assert proxy.new_run_id() != proxy.new_run_id()


def test_start_creates_network_and_container(monkeypatch):
    calls: list[list[str]] = []

    def fake_run(cmd, **kw):
        calls.append(cmd)
        return MagicMock(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(proxy.subprocess, "run", fake_run)
    session = proxy.start("abc123", ["api.anthropic.com:443"], "proxy:tag")
    try:
        assert session.network == "contained-net-abc123"
        assert session.container == "contained-proxy-abc123"
        assert session.proxy_url == "http://proxy:8888"
        # docker network create --internal <name>
        assert calls[0][:4] == ["docker", "network", "create", "--internal"]
        assert calls[0][4] == "contained-net-abc123"
        # docker run -d --rm ... proxy:tag
        assert calls[1][:2] == ["docker", "run"]
        assert "proxy:tag" in calls[1]
        assert "contained-proxy-abc123" in calls[1]
        # docker network connect --alias proxy <net> <container>
        assert calls[2][:3] == ["docker", "network", "connect"]
        assert "--alias" in calls[2]
        assert calls[2][calls[2].index("--alias") + 1] == "proxy"
    finally:
        session.filter_path.unlink(missing_ok=True)


def test_start_cleans_up_on_proxy_run_failure(monkeypatch):
    calls: list[list[str]] = []

    def fake_run(cmd, **kw):
        calls.append(cmd)
        if cmd[:2] == ["docker", "run"]:
            return MagicMock(returncode=1, stdout="", stderr="boom")
        return MagicMock(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(proxy.subprocess, "run", fake_run)
    with pytest.raises(proxy.ProxyError, match="boom"):
        proxy.start("abc", ["example.com:443"], "proxy:tag")
    # Teardown should attempt network rm after failure.
    assert any(c[:3] == ["docker", "network", "rm"] for c in calls)


def test_start_cleans_up_filter_on_network_create_failure(monkeypatch, tmp_path):
    calls: list[list[str]] = []
    tempfiles: list[Path] = []

    real_write = proxy.write_filter_file

    def tracking_write(allowlist):
        p = real_write(allowlist)
        tempfiles.append(p)
        return p

    def fake_run(cmd, **kw):
        calls.append(cmd)
        if cmd[:3] == ["docker", "network", "create"]:
            return MagicMock(returncode=1, stdout="", stderr="net boom")
        return MagicMock(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(proxy, "write_filter_file", tracking_write)
    monkeypatch.setattr(proxy.subprocess, "run", fake_run)
    with pytest.raises(proxy.ProxyError, match="net boom"):
        proxy.start("abc", ["example.com:443"], "proxy:tag")
    # Filter tempfile must not leak.
    assert tempfiles, "write_filter_file was not invoked"
    for p in tempfiles:
        assert not p.exists(), f"tempfile leaked: {p}"


def test_stop_is_best_effort(monkeypatch, tmp_path):
    calls: list[list[str]] = []
    monkeypatch.setattr(
        proxy.subprocess, "run",
        lambda cmd, **kw: calls.append(cmd) or MagicMock(returncode=1),
    )
    fp = tmp_path / "filter.txt"
    fp.write_text("x")
    session = proxy.ProxySession("abc", "net", "container", fp)
    proxy.stop(session)  # must not raise
    assert any(c[:3] == ["docker", "rm", "-f"] for c in calls)
    assert not fp.exists()
