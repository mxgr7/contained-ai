from __future__ import annotations

import shutil
import subprocess
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from contained import proxy, profiles, runtime


def test_write_filter_file_escapes_and_dedupes(tmp_path: Path):
    path = proxy.write_filter_file(
        ["api.anthropic.com:443", "api.anthropic.com:443", "github.com:443"]
    )
    try:
        lines = [ln for ln in path.read_text().splitlines() if ln]
        assert lines == [
            r"^api\.anthropic\.com(:[0-9]+)?$",
            r"^github\.com(:[0-9]+)?$",
        ]
    finally:
        path.unlink(missing_ok=True)


def test_write_filter_file_skips_blank(tmp_path: Path):
    path = proxy.write_filter_file(["", "  ", "example.com:443"])
    try:
        lines = [ln for ln in path.read_text().splitlines() if ln]
        assert lines == [r"^example\.com(:[0-9]+)?$"]
    finally:
        path.unlink(missing_ok=True)


def test_write_filter_file_honors_state_dir(tmp_path: Path):
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    path = proxy.write_filter_file(["example.com:443"], state_dir=state_dir)
    try:
        assert path.parent == state_dir
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

    def tracking_write(allowlist, *, state_dir=None):
        p = real_write(allowlist, state_dir=state_dir)
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


def _docker_available() -> bool:
    if shutil.which("docker") is None:
        return False
    try:
        r = subprocess.run(
            ["docker", "info", "--format", "{{.ServerVersion}}"],
            capture_output=True,
            timeout=5,
        )
    except (subprocess.TimeoutExpired, OSError):
        return False
    return r.returncode == 0


@pytest.mark.skipif(not _docker_available(), reason="docker not available")
def test_proxy_allowlist_end_to_end():
    """Start a real proxy with a two-host allowlist and curl through it."""
    runtime.build_proxy()
    session = proxy.start(
        proxy.new_run_id(),
        ["example.com:443"],
        profiles.PROXY_IMAGE,
    )
    try:
        def curl(host: str) -> int:
            return subprocess.run(
                [
                    "docker", "run", "--rm", "--network", session.network,
                    "-e", f"HTTPS_PROXY=http://proxy:8888",
                    "-e", f"HTTP_PROXY=http://proxy:8888",
                    "curlimages/curl:latest",
                    "-sS", "-o", "/dev/null", "-m", "15",
                    f"https://{host}/",
                ],
                capture_output=True,
                timeout=60,
            ).returncode

        assert curl("example.com") == 0, "allowed host failed"
        assert curl("www.iana.org") != 0, "denied host succeeded"
    finally:
        proxy.stop(session)


def test_start_sets_run_id_and_filter_path_labels(monkeypatch, tmp_path):
    calls: list[list[str]] = []

    def fake_run(cmd, **kw):
        calls.append(cmd)
        return MagicMock(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(proxy.subprocess, "run", fake_run)
    session = proxy.start(
        "xyz999", ["example.com:443"], "proxy:tag",
        state_dir=tmp_path, project=Path("/fake/project"),
    )
    try:
        run_cmd = calls[1]
        assert run_cmd[:2] == ["docker", "run"]
        labels = [
            run_cmd[i + 1] for i, tok in enumerate(run_cmd) if tok == "--label"
        ]
        assert f"{proxy.LABEL_RUN_ID}=xyz999" in labels
        assert f"{proxy.LABEL_PROJECT}=/fake/project" in labels
        assert any(
            lbl.startswith(f"{proxy.LABEL_FILTER_PATH}=") for lbl in labels
        )
    finally:
        session.filter_path.unlink(missing_ok=True)


def test_read_filter_hosts_roundtrip(tmp_path: Path):
    fp = proxy.write_filter_file(
        ["api.anthropic.com:443", "github.com:443", "a-b.example.com"],
        state_dir=tmp_path,
    )
    try:
        hosts = proxy.read_filter_hosts(fp)
        assert hosts == ["api.anthropic.com", "github.com", "a-b.example.com"]
    finally:
        fp.unlink(missing_ok=True)


def test_append_to_filter_adds_new_hosts(tmp_path: Path):
    fp = proxy.write_filter_file(
        ["api.anthropic.com:443", "github.com:443"], state_dir=tmp_path,
    )
    try:
        all_hosts, added = proxy.append_to_filter(
            fp, ["example.com:443", "pypi.org:443"]
        )
        assert added == ["example.com", "pypi.org"]
        assert all_hosts == [
            "api.anthropic.com", "github.com", "example.com", "pypi.org",
        ]
        # And the file actually contains the new entries.
        assert proxy.read_filter_hosts(fp) == all_hosts
    finally:
        fp.unlink(missing_ok=True)


def test_append_to_filter_idempotent(tmp_path: Path):
    fp = proxy.write_filter_file(["api.anthropic.com:443"], state_dir=tmp_path)
    try:
        all_hosts, added = proxy.append_to_filter(
            fp, ["api.anthropic.com", "api.anthropic.com:8443"]
        )
        assert added == []
        assert all_hosts == ["api.anthropic.com"]
    finally:
        fp.unlink(missing_ok=True)


def test_append_to_filter_preserves_unknown_lines(tmp_path: Path):
    fp = tmp_path / "filter.txt"
    fp.write_text(
        "# hand-edited comment\n"
        "^api\\.anthropic\\.com(:[0-9]+)?$\n"
    )
    all_hosts, added = proxy.append_to_filter(fp, ["example.com:443"])
    assert added == ["example.com"]
    contents = fp.read_text()
    assert "# hand-edited comment" in contents
    assert r"^example\.com(:[0-9]+)?$" in contents


def test_append_to_filter_preserves_inode(tmp_path):
    """The proxy bind-mounts the filter file by inode — the rewrite
    must NOT replace the file or the container will keep reading the
    old content."""
    fp = proxy.write_filter_file(["api.anthropic.com:443"], state_dir=tmp_path)
    try:
        inode_before = fp.stat().st_ino
        proxy.append_to_filter(fp, ["example.com:443", "pypi.org:443"])
        assert fp.stat().st_ino == inode_before
        # And the new content is actually on disk.
        assert "example.com" in proxy.read_filter_hosts(fp)
    finally:
        fp.unlink(missing_ok=True)


def test_reload_restarts_container(monkeypatch):
    """SIGHUP is insufficient — tinyproxy caches the Filter host list
    at startup, so the proxy container must be fully restarted."""
    calls: list[list[str]] = []
    monkeypatch.setattr(
        proxy.subprocess, "run",
        lambda cmd, **kw: calls.append(cmd) or MagicMock(returncode=0, stdout="", stderr=""),
    )
    proxy.reload("contained-proxy-abc")
    assert calls == [["docker", "restart", "contained-proxy-abc"]]


def test_reload_raises_on_failure(monkeypatch):
    monkeypatch.setattr(
        proxy.subprocess, "run",
        lambda cmd, **kw: MagicMock(returncode=1, stdout="", stderr="no such container"),
    )
    with pytest.raises(proxy.ProxyError, match="no such container"):
        proxy.reload("nope")


def test_discover_runs_parses_labels(monkeypatch):
    stdout = (
        "contained-proxy-aaa\taaa\t/home/me/proj1\t/var/f/aaa.txt\n"
        "contained-proxy-bbb\tbbb\t\t/var/f/bbb.txt\n"
    )
    monkeypatch.setattr(
        proxy.subprocess, "run",
        lambda cmd, **kw: MagicMock(returncode=0, stdout=stdout, stderr=""),
    )
    runs = proxy.discover_runs()
    assert len(runs) == 2
    assert runs[0].run_id == "aaa"
    assert runs[0].project == "/home/me/proj1"
    assert runs[0].filter_path == Path("/var/f/aaa.txt")
    assert runs[1].project is None


def test_discover_runs_raises_on_docker_error(monkeypatch):
    monkeypatch.setattr(
        proxy.subprocess, "run",
        lambda cmd, **kw: MagicMock(returncode=1, stdout="", stderr="daemon down"),
    )
    with pytest.raises(proxy.ProxyError, match="daemon down"):
        proxy.discover_runs()


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
