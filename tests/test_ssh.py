"""Tests for PRD 09 — Git over SSH plumbing."""

from __future__ import annotations

from importlib import resources
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from contained import proxy, runtime
from contained.config import ConfigError, load
from contained.run import (
    SSH_AGENT_SOCK_CONTAINER_PATH,
    SSH_CONFIG_CONTAINER_PATH,
    SSH_KEY_CONTAINER_PATH,
    SSH_KNOWN_HOSTS_CONTAINER_PATH,
    CliOverrides,
    generate_ssh_config,
    render_dry_run,
    resolve,
    validate_ssh_allowlist_entry,
)


# ---------------------------------------------------------------------------
# Config schema
# ---------------------------------------------------------------------------


def test_config_parses_ssh_allowlist(tmp_path: Path):
    (tmp_path / "contained.yaml").write_text(
        "ssh:\n  allowlist:\n    - github.com\n    - git.internal.corp\n"
    )
    loaded = load(tmp_path / "contained.yaml", cwd=tmp_path)
    assert loaded.ssh_allowlist == ["github.com", "git.internal.corp"]


def test_config_rejects_unknown_ssh_key(tmp_path: Path):
    (tmp_path / "contained.yaml").write_text("ssh:\n  wat: yes\n")
    with pytest.raises(ConfigError, match="unknown keys in ssh"):
        load(tmp_path / "contained.yaml", cwd=tmp_path)


def test_config_ssh_must_be_mapping(tmp_path: Path):
    (tmp_path / "contained.yaml").write_text("ssh: [github.com]\n")
    with pytest.raises(ConfigError, match="ssh must be a mapping"):
        load(tmp_path / "contained.yaml", cwd=tmp_path)


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------


def test_validate_ssh_entry_accepts_bare_and_22():
    validate_ssh_allowlist_entry("github.com")
    validate_ssh_allowlist_entry("github.com:22")


def test_validate_ssh_entry_rejects_other_ports():
    with pytest.raises(ConfigError, match="port 22"):
        validate_ssh_allowlist_entry("github.com:443")
    with pytest.raises(ConfigError, match="port 22"):
        validate_ssh_allowlist_entry("gitlab.example.com:2222")


def test_validate_ssh_entry_rejects_wildcards():
    with pytest.raises(ConfigError, match="wildcards"):
        validate_ssh_allowlist_entry("*.github.com")


# ---------------------------------------------------------------------------
# Resolve
# ---------------------------------------------------------------------------


def _loaded(tmp_path: Path):
    return load(None, cwd=tmp_path)


def test_resolve_unions_ssh_allowlist_from_config_and_flag(tmp_path: Path):
    (tmp_path / "contained.yaml").write_text(
        "ssh:\n  allowlist: [github.com]\n"
    )
    loaded = load(tmp_path / "contained.yaml", cwd=tmp_path)
    r = resolve(
        "claude",
        loaded,
        CliOverrides(allow_ssh=["git.internal.corp"]),
        cwd=tmp_path,
        host_env={},
    )
    assert r.ssh_allowlist == ["github.com", "git.internal.corp"]


def test_resolve_picks_up_ssh_auth_sock_from_host_env(tmp_path: Path):
    r = resolve(
        "claude",
        _loaded(tmp_path),
        CliOverrides(allow_ssh=["github.com"]),
        cwd=tmp_path,
        host_env={"SSH_AUTH_SOCK": "/tmp/ssh-agent.sock"},
    )
    assert r.ssh_auth_sock_host_path == "/tmp/ssh-agent.sock"


def test_resolve_ssh_key_flag_resolves_path(tmp_path: Path):
    key = tmp_path / "id_test"
    key.write_text("fake key")
    r = resolve(
        "claude",
        _loaded(tmp_path),
        CliOverrides(allow_ssh=["github.com"], ssh_key=key),
        cwd=tmp_path,
        host_env={},
    )
    assert r.ssh_key_host_path == key.resolve()


def test_resolve_ssh_key_missing_errors(tmp_path: Path):
    with pytest.raises(ConfigError, match="no such file"):
        resolve(
            "claude",
            _loaded(tmp_path),
            CliOverrides(
                allow_ssh=["github.com"], ssh_key=tmp_path / "missing"
            ),
            cwd=tmp_path,
            host_env={},
        )


def test_resolve_warns_when_no_credentials(tmp_path: Path):
    r = resolve(
        "claude",
        _loaded(tmp_path),
        CliOverrides(allow_ssh=["github.com"]),
        cwd=tmp_path,
        host_env={},
    )
    assert any("neither SSH_AUTH_SOCK nor --ssh-key" in w for w in r.warnings)


def test_resolve_rejects_dotssh_mount_when_ssh_enabled(tmp_path: Path):
    src = tmp_path / "src"
    src.mkdir()
    (src / ".ssh").mkdir()
    with pytest.raises(ConfigError, match="conflicts with --allow-ssh"):
        resolve(
            "claude",
            _loaded(tmp_path),
            CliOverrides(
                mounts=[f"{src}:/mnt/src"], allow_ssh=["github.com"]
            ),
            cwd=tmp_path,
            host_env={},
        )


def test_resolve_allows_dotssh_mount_when_ssh_disabled(tmp_path: Path):
    """Without --allow-ssh, .ssh stays a warning (existing behavior)."""
    src = tmp_path / "src"
    src.mkdir()
    (src / ".ssh").mkdir()
    r = resolve(
        "claude",
        _loaded(tmp_path),
        CliOverrides(mounts=[f"{src}:/mnt/src"]),
        cwd=tmp_path,
        host_env={},
    )
    assert any(".ssh" in w for w in r.warnings)  # warning, not error


def test_resolve_no_ssh_allowlist_leaves_defaults_untouched(tmp_path: Path):
    r = resolve("claude", _loaded(tmp_path), CliOverrides(), cwd=tmp_path)
    assert r.ssh_allowlist == []
    assert r.ssh_auth_sock_host_path is None
    assert r.ssh_key_host_path is None


# ---------------------------------------------------------------------------
# generate_ssh_config
# ---------------------------------------------------------------------------


def test_generate_ssh_config_lists_exact_hosts():
    out = generate_ssh_config(["github.com", "git.internal.corp"])
    assert "Host git.internal.corp github.com" in out
    assert "StrictHostKeyChecking yes" in out
    assert "ProxyCommand /usr/bin/nc -X connect -x proxy-ssh:8889 %h %p" in out
    assert f"UserKnownHostsFile {SSH_KNOWN_HOSTS_CONTAINER_PATH}" in out
    # No wildcards ever.
    assert "Host *" not in out


def test_generate_ssh_config_identity_file_only_when_key_present():
    without = generate_ssh_config(["github.com"])
    assert "IdentityFile" not in without
    with_key = generate_ssh_config(
        ["github.com"], ssh_key_container_path=SSH_KEY_CONTAINER_PATH
    )
    assert f"IdentityFile {SSH_KEY_CONTAINER_PATH}" in with_key
    assert "IdentitiesOnly yes" in with_key


def test_generate_ssh_config_empty_for_empty_list():
    assert generate_ssh_config([]) == ""


# ---------------------------------------------------------------------------
# build_argv
# ---------------------------------------------------------------------------


def _resolved(tmp_path: Path, **cli):
    return resolve(
        "claude",
        load(None, cwd=tmp_path),
        CliOverrides(**cli),
        cwd=tmp_path,
        host_env=cli.pop("_host_env", {}),
    )


def test_build_argv_mounts_ssh_config_and_known_hosts(tmp_path: Path):
    r = resolve(
        "claude",
        load(None, cwd=tmp_path),
        CliOverrides(allow_ssh=["github.com"]),
        cwd=tmp_path,
        host_env={"SSH_AUTH_SOCK": "/tmp/agent.sock"},
    )
    argv = runtime.build_argv(
        r,
        ssh_config_host_path=Path("/state/ssh_config"),
        ssh_known_hosts_host_path=Path("/state/known_hosts"),
    )
    joined = " ".join(argv)
    assert f"dst={SSH_CONFIG_CONTAINER_PATH}" in joined
    assert f"dst={SSH_KNOWN_HOSTS_CONTAINER_PATH}" in joined
    assert f"dst={SSH_AGENT_SOCK_CONTAINER_PATH}" in joined
    assert f"SSH_AUTH_SOCK={SSH_AGENT_SOCK_CONTAINER_PATH}" in joined


def test_build_argv_mounts_ssh_key_when_given(tmp_path: Path):
    key = tmp_path / "id"
    key.write_text("k")
    r = resolve(
        "claude",
        load(None, cwd=tmp_path),
        CliOverrides(allow_ssh=["github.com"], ssh_key=key),
        cwd=tmp_path,
        host_env={},
    )
    argv = runtime.build_argv(
        r,
        ssh_config_host_path=Path("/state/ssh_config"),
        ssh_known_hosts_host_path=Path("/state/known_hosts"),
    )
    joined = " ".join(argv)
    assert f"src={key.resolve()}" in joined
    assert f"dst={SSH_KEY_CONTAINER_PATH}" in joined


def test_build_argv_no_ssh_additions_when_disabled(tmp_path: Path):
    r = _resolved(tmp_path)
    argv = runtime.build_argv(r)
    joined = " ".join(argv)
    assert SSH_CONFIG_CONTAINER_PATH not in joined
    assert SSH_AGENT_SOCK_CONTAINER_PATH not in joined


# ---------------------------------------------------------------------------
# proxy.start_ssh
# ---------------------------------------------------------------------------


def test_start_ssh_runs_tinyproxy_with_ssh_config(monkeypatch, tmp_path):
    calls: list[list[str]] = []

    def fake_run(cmd, **kw):
        calls.append(cmd)
        return MagicMock(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(proxy.subprocess, "run", fake_run)
    sess = proxy.start_ssh(
        "abc123",
        ["github.com"],
        "proxy:tag",
        network="contained-net-abc123",
        state_dir=tmp_path,
        project=Path("/proj"),
    )
    try:
        run_cmd = calls[0]
        assert run_cmd[:2] == ["docker", "run"]
        assert "contained-proxy-ssh-abc123" in run_cmd
        # Command override targets the SSH config file.
        assert run_cmd[-4:] == [
            "tinyproxy", "-d", "-c", "/etc/tinyproxy/tinyproxy-ssh.conf",
        ]
        # Labels
        labels = [run_cmd[i + 1] for i, t in enumerate(run_cmd) if t == "--label"]
        assert f"{proxy.LABEL_ROLE}={proxy.ROLE_SSH}" in labels
        assert f"{proxy.LABEL_RUN_ID}=abc123" in labels
        # Filter path bind mount
        mounts = [run_cmd[i + 1] for i, t in enumerate(run_cmd) if t == "--mount"]
        assert any("dst=/etc/tinyproxy/filter-ssh" in m for m in mounts)
        # Network connect uses ssh alias
        connect_cmd = calls[1]
        assert connect_cmd[:3] == ["docker", "network", "connect"]
        assert "--alias" in connect_cmd
        assert connect_cmd[connect_cmd.index("--alias") + 1] == proxy.PROXY_SSH_ALIAS
        assert "contained-net-abc123" in connect_cmd
    finally:
        sess.filter_path.unlink(missing_ok=True)


def test_start_ssh_cleans_up_on_failure(monkeypatch, tmp_path):
    tempfiles: list[Path] = []
    real_write = proxy.write_filter_file

    def tracking_write(allowlist, *, state_dir=None):
        p = real_write(allowlist, state_dir=state_dir)
        tempfiles.append(p)
        return p

    def fake_run(cmd, **kw):
        if cmd[:2] == ["docker", "run"]:
            return MagicMock(returncode=1, stdout="", stderr="boom")
        return MagicMock(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(proxy, "write_filter_file", tracking_write)
    monkeypatch.setattr(proxy.subprocess, "run", fake_run)
    with pytest.raises(proxy.ProxyError, match="boom"):
        proxy.start_ssh(
            "x", ["github.com"], "proxy:tag", network="n", state_dir=tmp_path
        )
    for p in tempfiles:
        assert not p.exists()


def test_stop_ssh_is_best_effort(monkeypatch, tmp_path):
    calls: list[list[str]] = []
    monkeypatch.setattr(
        proxy.subprocess, "run",
        lambda cmd, **kw: calls.append(cmd) or MagicMock(returncode=1),
    )
    fp = tmp_path / "filter-ssh.txt"
    fp.write_text("x")
    session = proxy.SshProxySession("abc", "container", fp)
    proxy.stop_ssh(session)
    assert any(c[:3] == ["docker", "rm", "-f"] for c in calls)
    assert not fp.exists()


def test_discover_runs_filters_out_ssh_sidecars(monkeypatch):
    stdout = (
        "contained-proxy-aaa\taaa\t/proj1\t/var/f/aaa.txt\thttp\n"
        "contained-proxy-ssh-aaa\taaa\t/proj1\t/var/f/aaa-ssh.txt\tssh\n"
        "contained-proxy-bbb\tbbb\t\t/var/f/bbb.txt\t\n"  # legacy, no role
    )
    monkeypatch.setattr(
        proxy.subprocess, "run",
        lambda cmd, **kw: MagicMock(returncode=0, stdout=stdout, stderr=""),
    )
    runs = proxy.discover_runs()
    assert [r.container for r in runs] == [
        "contained-proxy-aaa",
        "contained-proxy-bbb",
    ]


# ---------------------------------------------------------------------------
# Regression: tinyproxy.conf must not re-open ConnectPort 22
# ---------------------------------------------------------------------------


def test_https_tinyproxy_conf_only_allows_connect_443():
    """PRD 06 closed the ConnectPort 22 hole; PRD 09 moves :22 to a
    separate sidecar. Guard the HTTPS proxy config to make sure neither
    a merge accident nor a cargo-culted paste re-adds :22."""
    ref = resources.files("contained").joinpath("assets/tinyproxy.conf")
    with resources.as_file(ref) as p:
        text = Path(p).read_text()
    connect_lines = [
        line.strip() for line in text.splitlines()
        if line.strip().lower().startswith("connectport")
    ]
    assert connect_lines == ["ConnectPort 443"], connect_lines


def test_ssh_tinyproxy_conf_uses_port_22_and_8889():
    ref = resources.files("contained").joinpath("assets/tinyproxy-ssh.conf")
    with resources.as_file(ref) as p:
        text = Path(p).read_text()
    connect_lines = [
        line.strip() for line in text.splitlines()
        if line.strip().lower().startswith("connectport")
    ]
    assert connect_lines == ["ConnectPort 22"], connect_lines
    assert "Port 8889" in text
    assert 'Filter "/etc/tinyproxy/filter-ssh"' in text


# ---------------------------------------------------------------------------
# Dry-run rendering
# ---------------------------------------------------------------------------


def test_render_dry_run_shows_ssh_section(tmp_path: Path):
    r = resolve(
        "claude",
        load(None, cwd=tmp_path),
        CliOverrides(allow_ssh=["github.com"]),
        cwd=tmp_path,
        host_env={"SSH_AUTH_SOCK": "/tmp/agent.sock"},
    )
    out = render_dry_run(r, host_env={"SSH_AUTH_SOCK": "/tmp/agent.sock"})
    assert "ssh_allowlist" in out
    assert "github.com" in out
    assert "ssh_credentials: ssh-agent socket /tmp/agent.sock" in out
    assert "ProxyCommand" in out


def test_render_dry_run_without_ssh_omits_section(tmp_path: Path):
    r = resolve(
        "claude", load(None, cwd=tmp_path), CliOverrides(), cwd=tmp_path
    )
    out = render_dry_run(r, host_env={})
    assert "ssh_allowlist" not in out


# ---------------------------------------------------------------------------
# runtime.run lifecycle
# ---------------------------------------------------------------------------


def test_run_rejects_ssh_with_host_network(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(runtime, "ensure_daemon", lambda: None)
    monkeypatch.setattr(runtime, "_execute", lambda argv: 0)
    r = resolve(
        "claude",
        load(None, cwd=tmp_path),
        CliOverrides(network="host", allow_ssh=["github.com"]),
        cwd=tmp_path,
        host_env={},
    )
    with pytest.raises(runtime.DockerError, match="allowlist"):
        runtime.run(r, tmp_path)


def test_run_starts_ssh_sidecar_and_tears_it_down(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "xdg"))
    monkeypatch.setattr(runtime, "ensure_daemon", lambda: None)
    monkeypatch.setattr(runtime, "_run_ssh_keyscan", lambda hosts: "")

    started_http: list = []
    started_ssh: list = []
    stopped_http: list = []
    stopped_ssh: list = []

    def fake_start(run_id, allowlist, image, *, state_dir=None, project=None):
        s = proxy.ProxySession(
            run_id, f"contained-net-{run_id}", f"contained-proxy-{run_id}",
            (state_dir or tmp_path) / "filter.txt",
        )
        s.filter_path.write_text("")
        started_http.append(s)
        return s

    def fake_start_ssh(run_id, allowlist, image, *, network, state_dir=None, project=None):
        s = proxy.SshProxySession(
            run_id, f"contained-proxy-ssh-{run_id}",
            (state_dir or tmp_path) / "filter-ssh.txt",
        )
        s.filter_path.write_text("")
        started_ssh.append(s)
        return s

    monkeypatch.setattr(proxy, "start", fake_start)
    monkeypatch.setattr(proxy, "start_ssh", fake_start_ssh)
    monkeypatch.setattr(proxy, "stop", lambda s: stopped_http.append(s))
    monkeypatch.setattr(proxy, "stop_ssh", lambda s: stopped_ssh.append(s))

    captured: dict = {}
    monkeypatch.setattr(
        runtime, "_execute",
        lambda argv: captured.__setitem__("argv", argv) or 0,
    )

    r = resolve(
        "claude",
        load(None, cwd=tmp_path),
        CliOverrides(allow_ssh=["github.com"]),
        cwd=tmp_path,
        host_env={"SSH_AUTH_SOCK": "/tmp/a.sock"},
    )
    runtime.run(r, tmp_path)

    assert len(started_http) == 1
    assert len(started_ssh) == 1
    assert len(stopped_http) == 1
    assert len(stopped_ssh) == 1
    # SSH sidecar joined the HTTPS sidecar's network.
    # Argv mounts the generated ssh_config.
    assert any(SSH_CONFIG_CONTAINER_PATH in a for a in captured["argv"])
