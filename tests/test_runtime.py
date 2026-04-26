from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from contained import profiles, runtime
from contained.config import LoadedConfig, ConfigSection
from contained.run import CliOverrides, resolve


def _resolved(tmp_path: Path, **cli_kwargs):
    loaded = LoadedConfig(
        path=None,
        base_dir=tmp_path,
        default_agent=None,
        defaults=ConfigSection(),
        agents={},
    )
    return resolve("claude", loaded, CliOverrides(**cli_kwargs), cwd=tmp_path)


def test_build_argv_has_security_flags(tmp_path: Path):
    r = _resolved(tmp_path)
    argv = runtime.build_argv(r)
    assert argv[:4] == ["docker", "run", "--rm", "-it"]
    assert "--cap-drop" in argv
    assert argv[argv.index("--cap-drop") + 1] == "ALL"
    assert "--security-opt" in argv
    assert "no-new-privileges" in argv
    assert "--user" in argv
    assert "--init" in argv


def test_build_argv_workdir_and_workspace_mount(tmp_path: Path):
    r = _resolved(tmp_path)
    argv = runtime.build_argv(r)
    assert "--workdir" in argv
    assert argv[argv.index("--workdir") + 1] == "/workspace"
    joined = " ".join(argv)
    assert f"src={tmp_path}" in joined
    assert "dst=/workspace" in joined


def test_build_argv_network_modes(tmp_path: Path):
    host = runtime.build_argv(_resolved(tmp_path, network="host"))
    none = runtime.build_argv(_resolved(tmp_path, network="none"))
    allow = runtime.build_argv(_resolved(tmp_path, network="allowlist"))
    assert "--network" in host and host[host.index("--network") + 1] == "host"
    assert "--network" in none and none[none.index("--network") + 1] == "none"
    assert "--network" in allow
    assert allow[allow.index("--network") + 1] == "contained-allowlist"
    joined = " ".join(allow)
    assert "HTTPS_PROXY=http://proxy:8888" in joined
    assert "HTTP_PROXY=http://proxy:8888" in joined
    assert "NO_PROXY=" in joined


def test_build_argv_allowlist_uses_real_network_name(tmp_path: Path):
    argv = runtime.build_argv(
        _resolved(tmp_path, network="allowlist"),
        proxy_network="contained-net-abc123",
    )
    assert argv[argv.index("--network") + 1] == "contained-net-abc123"


def test_build_argv_entrypoint_and_passthrough(tmp_path: Path):
    r = _resolved(tmp_path, passthrough=["--foo", "bar"])
    argv = runtime.build_argv(r)
    image_idx = argv.index(r.image)
    tail = argv[image_idx + 1 :]
    assert tail[0] == "claude"  # profile entrypoint
    assert tail[-2:] == ["--foo", "bar"]


def test_build_argv_tmux_wraps_entrypoint(tmp_path: Path):
    r = _resolved(tmp_path, tmux=True, passthrough=["--help"])
    argv = runtime.build_argv(r)
    image_idx = argv.index(r.image)
    tail = argv[image_idx + 1 :]
    assert tail[:5] == ["tmux", "new-session", "-A", "-s", "contained"]
    # Original entrypoint + passthrough come after the tmux wrapper.
    assert tail[5] == "claude"
    assert tail[-1] == "--help"


def test_build_argv_no_tmux_by_default(tmp_path: Path):
    r = _resolved(tmp_path)
    argv = runtime.build_argv(r)
    assert "tmux" not in argv


def test_build_argv_tmux_with_wrapper_uses_dash_f(tmp_path: Path):
    r = _resolved(tmp_path, tmux=True, tmux_prefix="C-b")
    wrapper = tmp_path / "wrapper.conf"
    wrapper.write_text("set -g prefix C-b\n")
    argv = runtime.build_argv(r, tmux_wrapper_host_path=wrapper)
    image_idx = argv.index(r.image)
    tail = argv[image_idx + 1 :]
    assert tail[:3] == ["tmux", "-f", runtime.TMUX_WRAPPER_CONTAINER_PATH]
    assert tail[3:7] == ["new-session", "-A", "-s", "contained"]
    # And the file is bind-mounted in.
    joined = " ".join(argv[:image_idx])
    assert f"src={wrapper}" in joined
    assert f"dst={runtime.TMUX_WRAPPER_CONTAINER_PATH}" in joined


def test_prepare_tmux_wrapper_writes_override(tmp_path: Path):
    cfg_dir = tmp_path / "tmux"
    cfg_dir.mkdir()
    (cfg_dir / "tmux.conf").write_text("set -g prefix C-a\n")
    from contained.run import Mount
    r = _resolved(tmp_path, tmux=True, tmux_prefix="C-b")
    # Simulate the resolve()-added user config mount.
    r.mounts.append(Mount(host=cfg_dir, container="/home/agent/.config/tmux", read_only=True))
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    wrapper = runtime._prepare_tmux_wrapper(r, state_dir)
    assert wrapper == state_dir / "tmux-wrapper.conf"
    text = wrapper.read_text()
    assert "source-file /home/agent/.config/tmux/tmux.conf" in text
    assert "set -g prefix C-b" in text
    assert "bind C-b send-prefix" in text


def test_prepare_tmux_wrapper_skips_source_when_no_user_config(tmp_path: Path):
    r = _resolved(tmp_path, tmux=True, tmux_prefix="C-b")
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    wrapper = runtime._prepare_tmux_wrapper(r, state_dir)
    text = wrapper.read_text()
    assert "source-file" not in text
    assert "set -g prefix C-b" in text


def test_build_argv_env_masking(tmp_path: Path):
    r = _resolved(tmp_path, env=["MY_SECRET_KEY=hunter2"])
    masked = " ".join(runtime.build_argv(r, mask_secrets=True))
    unmasked = " ".join(runtime.build_argv(r, mask_secrets=False))
    assert "hunter2" in unmasked
    assert "hunter2" not in masked
    assert "***" in masked


def test_ensure_daemon_missing_binary(monkeypatch):
    monkeypatch.setattr(runtime.shutil, "which", lambda _: None)
    with pytest.raises(runtime.DockerError, match="docker binary not found"):
        runtime.ensure_daemon()


def test_ensure_daemon_daemon_down(monkeypatch):
    monkeypatch.setattr(runtime.shutil, "which", lambda _: "/usr/bin/docker")
    result = MagicMock(returncode=1, stderr="Cannot connect to the Docker daemon")
    monkeypatch.setattr(runtime.subprocess, "run", lambda *a, **k: result)
    with pytest.raises(runtime.DockerError, match="not reachable"):
        runtime.ensure_daemon()


def test_ensure_daemon_ok(monkeypatch):
    monkeypatch.setattr(runtime.shutil, "which", lambda _: "/usr/bin/docker")
    result = MagicMock(returncode=0, stdout="24.0.7\n", stderr="")
    monkeypatch.setattr(runtime.subprocess, "run", lambda *a, **k: result)
    runtime.ensure_daemon()  # no raise


def test_find_overlay_prefers_config_dir(tmp_path: Path):
    cfg_dir = tmp_path / "sub"
    cfg_dir.mkdir()
    (cfg_dir / "Dockerfile.contained").write_text("FROM contained-base\n")
    (cfg_dir / "contained.yaml").write_text("")
    loaded = LoadedConfig(
        path=cfg_dir / "contained.yaml",
        base_dir=cfg_dir,
        default_agent=None,
        defaults=ConfigSection(),
        agents={},
    )
    r = resolve("claude", loaded, CliOverrides(), cwd=tmp_path)
    assert runtime.find_overlay(r, tmp_path) == cfg_dir / "Dockerfile.contained"


def test_find_overlay_falls_back_to_cwd(tmp_path: Path):
    (tmp_path / "Dockerfile.contained").write_text("FROM contained-base\n")
    r = _resolved(tmp_path)
    assert runtime.find_overlay(r, tmp_path) == tmp_path / "Dockerfile.contained"


def test_find_overlay_absent(tmp_path: Path):
    r = _resolved(tmp_path)
    assert runtime.find_overlay(r, tmp_path) is None


def test_overlay_tag_changes_when_file_changes(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(runtime, "_base_image_id", lambda img: img)
    df = tmp_path / "Dockerfile.contained"
    df.write_text("FROM contained-base\nRUN echo a\n")
    t1 = runtime.overlay_tag("claude", df, "base:1")
    df.write_text("FROM contained-base\nRUN echo b\n")
    t2 = runtime.overlay_tag("claude", df, "base:1")
    assert t1 != t2


def test_overlay_tag_changes_when_base_changes(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(runtime, "_base_image_id", lambda img: img)
    df = tmp_path / "Dockerfile.contained"
    df.write_text("FROM contained-base\n")
    t1 = runtime.overlay_tag("claude", df, "base:1")
    t2 = runtime.overlay_tag("claude", df, "base:2")
    assert t1 != t2


def test_build_overlay_uses_cache_when_image_exists(tmp_path: Path, monkeypatch):
    df = tmp_path / "Dockerfile.contained"
    df.write_text("FROM contained-base\n")
    monkeypatch.setattr(runtime, "_base_image_id", lambda img: img)
    monkeypatch.setattr(runtime, "_image_exists", lambda tag: True)
    called = []
    monkeypatch.setattr(
        runtime.subprocess, "run",
        lambda *a, **k: called.append(a) or MagicMock(returncode=0),
    )
    runtime.build_overlay("claude", df, "base:1", rebuild=False)
    assert called == []  # no build invoked


def test_build_overlay_rebuilds_when_forced(tmp_path: Path, monkeypatch):
    df = tmp_path / "Dockerfile.contained"
    df.write_text("FROM contained-base\n")
    monkeypatch.setattr(runtime, "_base_image_id", lambda img: img)
    monkeypatch.setattr(runtime, "_image_exists", lambda tag: True)
    calls: list[tuple[list[str], str | None]] = []

    def fake_run(cmd, *, input_text=None):
        calls.append((cmd, input_text))
        return runtime._BuildResult(returncode=0, tail="")

    monkeypatch.setattr(runtime, "_run_capturing_tail", fake_run)
    runtime.build_overlay("claude", df, "base:1", rebuild=True)
    assert calls and calls[0][0][:2] == ["docker", "build"]


def test_build_overlay_pipes_rewritten_dockerfile(tmp_path: Path, monkeypatch):
    df = tmp_path / "Dockerfile.contained"
    df.write_text(
        "# comment\n"
        "\n"
        "FROM contained-base AS build\n"
        "RUN echo hi\n"
        "FROM contained-base AS runtime\n"
        "COPY --from=build /x /x\n"
    )
    monkeypatch.setattr(runtime, "_base_image_id", lambda img: img)
    monkeypatch.setattr(runtime, "_image_exists", lambda tag: False)
    captured: dict = {}

    def fake_run(cmd, *, input_text=None):
        captured["cmd"] = cmd
        captured["input"] = input_text
        return runtime._BuildResult(returncode=0, tail="")

    monkeypatch.setattr(runtime, "_run_capturing_tail", fake_run)
    runtime.build_overlay("claude", df, "base:1", rebuild=True)
    text = captured["input"]
    # Both stages were rewritten to point at the real base image.
    assert "FROM contained-base" not in text
    assert text.count("FROM base:1 AS build") == 1
    assert text.count("FROM base:1 AS runtime") == 1
    # Comment and content preserved in order.
    assert text.startswith("# comment\n")
    assert "RUN echo hi" in text
    # docker build reads from stdin
    assert "-f" in captured["cmd"]
    assert captured["cmd"][captured["cmd"].index("-f") + 1] == "-"


def test_build_overlay_rejects_non_from_first_instruction(tmp_path: Path, monkeypatch):
    df = tmp_path / "Dockerfile.contained"
    df.write_text("ARG FOO=1\nFROM contained-base\n")
    monkeypatch.setattr(runtime, "_base_image_id", lambda img: img)
    monkeypatch.setattr(runtime, "_image_exists", lambda tag: False)
    with pytest.raises(runtime.DockerError, match="first instruction must be"):
        runtime.build_overlay("claude", df, "base:1", rebuild=True)


def test_build_overlay_rejects_wrong_base_with_quoted_line(tmp_path: Path, monkeypatch):
    df = tmp_path / "Dockerfile.contained"
    df.write_text("FROM debian:bookworm\nRUN true\n")
    monkeypatch.setattr(runtime, "_base_image_id", lambda img: img)
    monkeypatch.setattr(runtime, "_image_exists", lambda tag: False)
    with pytest.raises(runtime.DockerError, match="FROM debian:bookworm"):
        runtime.build_overlay("claude", df, "base:1", rebuild=True)


def test_build_overlay_rejects_bad_from(tmp_path: Path, monkeypatch):
    df = tmp_path / "Dockerfile.contained"
    df.write_text("FROM debian:bookworm\n")
    monkeypatch.setattr(runtime, "_base_image_id", lambda img: img)
    monkeypatch.setattr(runtime, "_image_exists", lambda tag: False)
    with pytest.raises(runtime.DockerError, match="FROM contained-base"):
        runtime.build_overlay("claude", df, "base:1", rebuild=False)


def test_execute_emits_extended_keys_sequence_when_tty(monkeypatch, capsys):
    class FakeProc:
        def wait(self):
            return 0

    monkeypatch.setattr(subprocess, "Popen", lambda *a, **k: FakeProc())
    monkeypatch.setattr(sys.stdout, "isatty", lambda: True, raising=False)
    monkeypatch.delenv("TMUX", raising=False)

    rc = runtime._execute(["docker", "run", "hi"])
    assert rc == 0
    out = capsys.readouterr().out
    assert runtime._ENABLE_EXTENDED_KEYS in out
    assert runtime._RESET_EXTENDED_KEYS in out
    assert out.index(runtime._ENABLE_EXTENDED_KEYS) < out.index(runtime._RESET_EXTENDED_KEYS)


def test_execute_skips_tty_sequence_when_not_a_tty(monkeypatch, capsys):
    class FakeProc:
        def wait(self):
            return 0

    monkeypatch.setattr(subprocess, "Popen", lambda *a, **k: FakeProc())
    monkeypatch.setattr(sys.stdout, "isatty", lambda: False, raising=False)
    monkeypatch.delenv("TMUX", raising=False)

    runtime._execute(["docker", "run", "hi"])
    out = capsys.readouterr().out
    assert runtime._ENABLE_EXTENDED_KEYS not in out
    assert runtime._RESET_EXTENDED_KEYS not in out


def test_execute_configures_tmux_when_in_tmux(monkeypatch, capsys):
    class FakeProc:
        def wait(self):
            return 0

    monkeypatch.setattr(subprocess, "Popen", lambda *a, **k: FakeProc())
    monkeypatch.setattr(sys.stdout, "isatty", lambda: True, raising=False)
    monkeypatch.setattr(runtime.shutil, "which", lambda name: "/usr/bin/tmux" if name == "tmux" else None)
    monkeypatch.setenv("TMUX", "/tmp/tmux-1000/default,123,0")

    calls: list[list[str]] = []
    def fake_run(cmd, *args, **kwargs):
        calls.append(cmd)
        return subprocess.CompletedProcess(cmd, 0, b"", b"")
    monkeypatch.setattr(runtime.subprocess, "run", fake_run)

    runtime._execute(["docker", "run", "hi"])

    assert ["tmux", "set", "-g", "extended-keys", "on"] in calls
    assert ["tmux", "set", "-ga", "terminal-features", "xterm*:extkeys"] in calls


def test_execute_skips_tmux_config_outside_tmux(monkeypatch):
    class FakeProc:
        def wait(self):
            return 0

    monkeypatch.setattr(subprocess, "Popen", lambda *a, **k: FakeProc())
    monkeypatch.setattr(sys.stdout, "isatty", lambda: True, raising=False)
    monkeypatch.delenv("TMUX", raising=False)

    calls: list[list[str]] = []
    def fake_run(cmd, *args, **kwargs):
        calls.append(cmd)
        return subprocess.CompletedProcess(cmd, 0, b"", b"")
    monkeypatch.setattr(runtime.subprocess, "run", fake_run)

    runtime._execute(["docker", "run", "hi"])

    assert not any(cmd and cmd[0] == "tmux" for cmd in calls)


def test_execute_resets_tty_on_keyboard_interrupt(monkeypatch, capsys):
    class FakeProc:
        def __init__(self):
            self.calls = 0

        def wait(self):
            self.calls += 1
            if self.calls == 1:
                raise KeyboardInterrupt
            return 130

    monkeypatch.setattr(subprocess, "Popen", lambda *a, **k: FakeProc())
    monkeypatch.setattr(sys.stdout, "isatty", lambda: True, raising=False)
    monkeypatch.delenv("TMUX", raising=False)

    rc = runtime._execute(["docker", "run", "hi"])
    assert rc == 130
    out = capsys.readouterr().out
    assert runtime._RESET_EXTENDED_KEYS in out


def test_run_end_to_end(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(runtime, "ensure_daemon", lambda: None)
    captured: dict[str, list[str]] = {}
    def fake_execute(argv):
        captured["argv"] = argv
        return 0
    monkeypatch.setattr(runtime, "_execute", fake_execute)
    r = _resolved(tmp_path)
    rc = runtime.run(r, tmp_path)
    assert rc == 0
    assert captured["argv"][0] == "docker"


def test_run_patches_claude_json_shift_enter_flag(tmp_path: Path, monkeypatch):
    import json as _json
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "xdg"))
    monkeypatch.setattr(runtime, "ensure_daemon", lambda: None)
    monkeypatch.setattr(runtime, "_execute", lambda argv: 0)
    proj = tmp_path / "proj"
    proj.mkdir()
    r = _resolved(proj)
    rc = runtime.run(r, proj)
    assert rc == 0
    from contained import state
    claude_json = state.project_state_dir(proj) / "claude.json"
    assert claude_json.exists()
    data = _json.loads(claude_json.read_text())
    assert data.get("shiftEnterKeyBindingInstalled") is True


def _mock_build(monkeypatch):
    calls: list[list[str]] = []

    def fake_run(cmd, *, input_text=None):
        calls.append(cmd)
        return runtime._BuildResult(returncode=0, tail="")

    monkeypatch.setattr(runtime, "_run_capturing_tail", fake_run)
    return calls


def test_build_base_default_tag(monkeypatch):
    monkeypatch.setattr(runtime.shutil, "which", lambda _: "/usr/bin/docker")
    calls = _mock_build(monkeypatch)
    tag = runtime.build_base()
    assert tag == profiles.BASE_IMAGE
    assert calls[0][:2] == ["docker", "build"]
    assert "-t" in calls[0]
    assert profiles.BASE_IMAGE in calls[0]
    assert "--no-cache" not in calls[0]


def test_build_base_custom_tag_and_rebuild(monkeypatch):
    monkeypatch.setattr(runtime.shutil, "which", lambda _: "/usr/bin/docker")
    calls = _mock_build(monkeypatch)
    tag = runtime.build_base("my/tag:dev", rebuild=True)
    assert tag == "my/tag:dev"
    assert "my/tag:dev" in calls[0]
    assert "--no-cache" in calls[0]


def test_build_base_missing_docker(monkeypatch):
    monkeypatch.setattr(runtime.shutil, "which", lambda _: None)
    with pytest.raises(runtime.DockerError, match="docker binary not found"):
        runtime.build_base()


def test_build_base_failed(monkeypatch):
    monkeypatch.setattr(runtime.shutil, "which", lambda _: "/usr/bin/docker")
    monkeypatch.setattr(
        runtime,
        "_run_capturing_tail",
        lambda cmd, *, input_text=None: runtime._BuildResult(
            returncode=2, tail="step 3/5: /bin/sh -c apt-get install cruft\nE: Unable to locate package cruft"
        ),
    )
    with pytest.raises(runtime.DockerError) as ei:
        runtime.build_base()
    msg = str(ei.value)
    assert "build failed" in msg
    assert "Unable to locate package cruft" in msg


def test_base_dockerfile_asset_present():
    path = runtime.base_dockerfile_path()
    assert path.is_file()
    assert "FROM debian" in path.read_text().splitlines()[10] or "FROM" in path.read_text()


def test_run_creates_state_dir_before_launch(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "xdg"))
    monkeypatch.setattr(runtime, "ensure_daemon", lambda: None)
    monkeypatch.setattr(runtime, "_execute", lambda argv: 0)
    r = _resolved(tmp_path)
    runtime.run(r, tmp_path)
    state_mount = next(m for m in r.mounts if m.container == "/home/agent/.claude")
    assert state_mount.host.is_dir()


def test_run_skips_state_dir_creation_with_no_state(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "xdg"))
    monkeypatch.setattr(runtime, "ensure_daemon", lambda: None)
    monkeypatch.setattr(runtime, "_execute", lambda argv: 0)
    r = _resolved(tmp_path, no_state=True)
    runtime.run(r, tmp_path)
    assert not (tmp_path / "xdg" / "contained").exists()


def test_run_starts_and_stops_proxy_in_allowlist(tmp_path: Path, monkeypatch):
    from contained import proxy
    monkeypatch.setattr(runtime, "ensure_daemon", lambda: None)
    started: list[tuple] = []
    stopped: list[proxy.ProxySession] = []

    def fake_start(run_id, allowlist, image, *, state_dir=None, project=None):
        s = proxy.ProxySession(
            run_id, f"contained-net-{run_id}", f"contained-proxy-{run_id}",
            tmp_path / "filter.txt",
        )
        (tmp_path / "filter.txt").write_text("")
        started.append((run_id, allowlist, image))
        return s

    monkeypatch.setattr(proxy, "start", fake_start)
    monkeypatch.setattr(proxy, "stop", lambda s: stopped.append(s))
    captured: dict[str, list[str]] = {}
    monkeypatch.setattr(
        runtime, "_execute",
        lambda argv: captured.__setitem__("argv", argv) or 0,
    )
    r = _resolved(tmp_path)  # default network is allowlist
    assert r.network == "allowlist"
    rc = runtime.run(r, tmp_path)
    assert rc == 0
    assert len(started) == 1
    assert len(stopped) == 1
    assert "api.anthropic.com:443" in started[0][1]
    # Agent container got the real network name, not the placeholder.
    argv = captured["argv"]
    net_idx = argv.index("--network")
    assert argv[net_idx + 1].startswith("contained-net-")


def test_run_stops_proxy_even_if_execute_raises(tmp_path: Path, monkeypatch):
    from contained import proxy
    monkeypatch.setattr(runtime, "ensure_daemon", lambda: None)
    fp = tmp_path / "filter.txt"
    fp.write_text("")
    session = proxy.ProxySession("xx", "net-xx", "proxy-xx", fp)
    monkeypatch.setattr(proxy, "start", lambda *a, **k: session)
    stopped: list[proxy.ProxySession] = []
    monkeypatch.setattr(proxy, "stop", lambda s: stopped.append(s))

    def boom(argv):
        raise KeyboardInterrupt()

    monkeypatch.setattr(runtime, "_execute", boom)
    with pytest.raises(KeyboardInterrupt):
        runtime.run(_resolved(tmp_path), tmp_path)
    assert stopped == [session]


def test_run_skips_proxy_for_host_network(tmp_path: Path, monkeypatch):
    from contained import proxy
    monkeypatch.setattr(runtime, "ensure_daemon", lambda: None)
    called = {"start": 0}
    monkeypatch.setattr(
        proxy, "start",
        lambda *a, **k: called.__setitem__("start", called["start"] + 1),
    )
    monkeypatch.setattr(runtime, "_execute", lambda argv: 0)
    r = _resolved(tmp_path, network="host")
    runtime.run(r, tmp_path)
    assert called["start"] == 0


def test_run_proxy_start_failure_surfaces_error(tmp_path: Path, monkeypatch):
    from contained import proxy
    monkeypatch.setattr(runtime, "ensure_daemon", lambda: None)

    def fail(*a, **k):
        raise proxy.ProxyError("docker network create: boom")

    monkeypatch.setattr(proxy, "start", fail)
    with pytest.raises(runtime.DockerError, match="egress proxy"):
        runtime.run(_resolved(tmp_path), tmp_path)


def test_build_proxy_uses_proxy_dockerfile(monkeypatch):
    monkeypatch.setattr(runtime.shutil, "which", lambda _: "/usr/bin/docker")
    calls = _mock_build(monkeypatch)
    tag = runtime.build_proxy()
    assert tag == profiles.PROXY_IMAGE
    assert "Dockerfile.proxy" in calls[0][calls[0].index("-f") + 1]


def test_run_builds_overlay_when_present(tmp_path: Path, monkeypatch):
    (tmp_path / "Dockerfile.contained").write_text("FROM contained-base\n")
    monkeypatch.setattr(runtime, "ensure_daemon", lambda: None)
    monkeypatch.setattr(runtime, "build_overlay", lambda *a, **k: "overlay:abc")
    captured: dict[str, list[str]] = {}
    def fake_execute(argv):
        captured["argv"] = argv
        return 0
    monkeypatch.setattr(runtime, "_execute", fake_execute)
    r = _resolved(tmp_path)
    runtime.run(r, tmp_path)
    assert "overlay:abc" in captured["argv"]
