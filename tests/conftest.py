"""Shared pytest fixtures for the test suite."""

from __future__ import annotations

from pathlib import Path

import pytest


@pytest.fixture
def mock_proxy(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Stub `proxy.start` / `proxy.stop` so `runtime.run` doesn't need docker.

    The default claude profile sets network=allowlist, which makes
    `runtime.run` shell out to `docker network create` via `proxy.start`.
    Tests that exercise `runtime.run` for reasons unrelated to proxy
    behaviour (state dirs, overlays, claude.json patching, exec argv)
    pull in this fixture so they're hermetic on hosts without docker.

    Tests that *do* care about proxy semantics keep mocking inline so
    they can assert on the calls.
    """
    from contained import proxy

    def fake_start(run_id, allowlist, image, *, state_dir=None, project=None):
        filter_path = tmp_path / f"filter-{run_id}.txt"
        filter_path.write_text("")
        return proxy.ProxySession(
            run_id,
            f"contained-net-{run_id}",
            f"contained-proxy-{run_id}",
            filter_path,
        )

    monkeypatch.setattr(proxy, "start", fake_start)
    monkeypatch.setattr(proxy, "stop", lambda session: None)
