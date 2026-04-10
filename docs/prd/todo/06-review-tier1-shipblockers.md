# PRD — Review tier 1: ship-blockers

## Goal

Close the correctness and safety-claim gaps that three independent
code reviews (security, senior Python, UX) all flagged as must-fix
before `contained` can honestly stand behind its posture claims. All
items here are small, mostly one-file patches, but each one invalidates
a concrete README promise today.

## Context

After PRDs 01–05 shipped, three reviews surfaced overlapping findings.
The items pulled into this PRD are the ones where **at least one
reviewer called them ship-blocking** and the fix is well-defined. The
broader "should-fix" items live in PRD 07; the longer-tail items in
PRD 08.

## Items

### 1. Workspace-default mount bypasses the `/` and `$HOME` safety checks

**File:** `contained/run.py:147-162`

`_validate_mount_safety` and `_require_host_path_exists` currently run
only over `user_mounts`. The default `$PWD → /workspace` mount is
appended directly to `mounts` without validation. Consequence: running
`contained run claude` from `/` or `$HOME` mounts them rw into the
container — the exact failure mode the README says is refused.

**Also:** the `has_workspace` detection is buggy —
`":/workspace" in m or m.endswith(":/workspace")` matches
`foo:/workspace/sub` and `foo:/workspace_backup`, so a user mount like
`./fixtures:/workspace/fixtures` silently suppresses the default
workspace mount and the agent launches with no workspace at all.

**Fix:** parse each mount spec into a Mount *before* the "has_workspace"
check, compare `container == "/workspace"` exactly, and run every
mount (default included) through the safety checks in a single pass.
Drop the `user_mounts` / `mounts` bifurcation.

### 2. `ConnectPort 22` in tinyproxy allows SSH tunneling for any allowlisted host

**File:** `contained/assets/tinyproxy.conf:23`

For any host in the allowlist (e.g. `github.com`), the sandboxed agent
can `CONNECT github.com:22` and tunnel arbitrary SSH. Combined with a
credential-bearing mount (`~/.config/gh` ro is in the README example),
this is a plausible exfil path. The filter is hostname-only, so the
allowlist does not constrain port.

**Fix:** remove `ConnectPort 22` from the default config. Document
SSH egress as an opt-in follow-up; no user has asked for it yet and
the README already names git-over-HTTPS as the supported path.

### 3. `--env KEY` unset check doesn't cover profile envs and disagrees with `--dry-run`

**Files:** `contained/cli.py:179-185`, `contained/run.py:298-332`

The README promises: "Unset required env vars fail fast." Reality:

- The check in `_cmd_run` only looks at bare CLI `--env KEY` forms, so
  `contained run claude` with `ANTHROPIC_API_KEY` unset on the host
  (which is profile-supplied) does not fail — the container launches
  and claude gets an empty env var.
- The check runs only in the live-run path. `--dry-run` happily shows
  `KEY=<from host>` for an unset var, so a green dry-run can launch
  straight into a failure.

**Fix:** move the check into `resolve()` or the cli layer such that it
runs against `resolved.env` where `from_host=True`, covering profile
envs, config envs, and CLI envs uniformly. Run it for both `--dry-run`
and live paths. Surface which layer the required var came from in the
error message.

### 4. `proxy.start` leaks the filter tempfile on `network create` failure

**File:** `contained/proxy.py:74-95`

`filter_path = write_filter_file(...)` is called before the `try`
block. If `docker network create` raises `ProxyError`, the `except`
cleanup in `start()` never runs, the `stop()` call never happens, and
the `/tmp/contained-filter-*.txt` file leaks.

**Fix:** create the filter file inside the `try`, or allocate it
after network creation. Add a test that asserts no tempfile leaks
when network create fails.

### 5. `runtime.RuntimeError` shadows the builtin

**File:** `contained/runtime.py:24-26` and every call site.

Defining `class RuntimeError(Exception)` at module scope means every
`raise RuntimeError(...)` / `except RuntimeError` inside `runtime.py`
uses the custom one, while code in sibling modules (`run.py`, anywhere
else) using the bare name gets the builtin. One stray
`from contained.runtime import RuntimeError` plus a later `raise
RuntimeError(...)` in that module and a real Python error would be
silently caught.

**Fix:** rename to `DockerError` (or `RuntimeExecError`). Update
`cli.py:201` and any tests that reference it. Grep for
`runtime.RuntimeError` before and after the rename.

## Acceptance criteria

- [ ] `contained run claude` from `$HOME` or `/` is refused with the
      same message as an explicit `--mount $HOME:/workspace`.
- [ ] A user mount at a container path like `/workspace/sub` no longer
      suppresses the default `$PWD → /workspace` mount.
- [ ] `ConnectPort 22` removed from `contained/assets/tinyproxy.conf`;
      README deferred-features list updated.
- [ ] `contained run claude` with `ANTHROPIC_API_KEY` unset on the
      host fails with a clear error naming the missing var and its
      source layer, in both live and `--dry-run` paths.
- [ ] `proxy.start` has a regression test for network-create failure
      that asserts the tempfile is unlinked.
- [ ] `runtime.RuntimeError` renamed; `grep -r "runtime.RuntimeError"`
      returns no hits; all existing tests still pass.
- [ ] A new test in `tests/test_run.py` asserts `resolve()` refuses
      `cwd == "/"` and `cwd == Path.home()` via the default workspace
      mount.
