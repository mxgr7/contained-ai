# PRD — Review tier 2: posture polish

## Goal

Tighten the security posture and the user-facing messaging so the
claims in the README are actually borne out by the code. These items
aren't blockers the way PRD 06 is, but every one of them was flagged
by at least one reviewer as "you said X in the README and the code
doesn't quite do X."

## Context

Follow-up to PRD 06. PRD 06 fixes correctness bugs that invalidate
concrete claims; this PRD fixes posture and messaging gaps that erode
trust even when nothing is technically broken.

## Items

### 1. Allowlist regex: port-aware, wildcard-rejecting, integration-tested

**Files:** `contained/proxy.py:44-65`, `contained/assets/tinyproxy.conf`

Current behavior writes `^host$` regexes and trusts tinyproxy to match
them against the CONNECT target. That target includes the port
(`github.com:443`), so anchored `^github\.com$` is suspect under
`FilterExtended Yes` + `FilterCaseSensitive No`. It may work today by
accident of tinyproxy internals; it should work by construction.

Additionally, a user entry like `*.github.com:443` is accepted by the
CLI today and re-escaped by `re.escape` into the literal
`\*\.github\.com` — which matches nothing and the user thinks
wildcards work.

**Fix:**

- Emit `^host(:[0-9]+)?$` per entry.
- Explicitly reject wildcard entries at config-resolve time with a
  clear error message pointing the user at individual host entries,
  or implement proper subdomain expansion (one or the other, decide
  when implementing).
- Add one real end-to-end test: build the proxy image, start it with a
  two-host allowlist, and verify one `curl` succeeds and another fails.
  Skip the test if docker isn't available.

### 2. Symlink resolution gap in mount safety checks

**File:** `contained/run.py:255-262`, `_validate_mount_safety`

`_parse_mount` calls `.resolve()` only on **relative** host paths; an
absolute `~/link` with a symlink to `/` is expanded to
`/Users/max/link` and is **not** resolved. Then `_validate_mount_safety`
compares to `/` and `Path.home()` by equality and the check passes —
after which the kernel follows the symlink at mount time and Docker
mounts the symlink target. Combined with the workspace-default fix
from PRD 06, this closes a real bypass of the mount-safety posture.

**Fix:** always `.resolve(strict=True)` host paths in `_parse_mount`,
regardless of whether they're absolute or relative. Add a test that
creates a symlink to `/` and asserts the mount is refused.

### 3. Centralize and strengthen secret masking

**Files:** `contained/run.py:287-295`, `contained/runtime.py:103-111`

Two copies of `_mask` with subtly different None handling (`""` vs
`"<from host>"`). Both use a substring allow-list of hints
(`KEY|TOKEN|SECRET|PASSWORD|PASS|CREDENTIAL`) which misses real
secrets: `GITHUB_PAT`, `DATABASE_URL`, `SENTRY_DSN`, `COOKIE`,
`BEARER`, `NPM_AUTH`, `HF_HUB_ACCESS`.

Separately, `_load_env_files` echoes raw lines into `ConfigError`
messages on parse errors, so a malformed dotenv entry leaks its value
to stderr.

**Fix:**

- One `_mask` function, shared between `run.py` and `runtime.py`.
- By default, mask any env var supplied via `--env KEY=VALUE` or
  `--env-from FILE` (i.e. values that originate from the user, not the
  host process environment). Host-forwarded values remain `<from host>`
  as they do today — the actual value never passes through `contained`.
- Scrub `raw` line content out of `_load_env_files` error messages;
  say "malformed env line at {path}:{line_no}" without echoing the
  line.
- Apply masking to `render_dry_run`'s `docker invocation (preview)`
  line, which currently goes through `build_argv(..., mask_secrets=True)`
  but relies on the same substring heuristic.

### 4. Credential seeding visibility

**Files:** `contained/state.py:50-72`, `contained/cli.py:_cmd_run`,
`contained/run.py:render_dry_run`

Today `seed_credentials` silently copies `~/.claude/.credentials.json`
into `~/.local/share/contained/projects/<id>/claude/.credentials.json`
on first run with mode 0600. There is **no output**. A security-
sensitive user has to read source to learn their credentials were
copied. The README sells this as a safety feature but the behavior is
invisible.

**Fix:**

- When `seed_credentials` actually copies a file (newly-seeded list is
  non-empty), `runtime.run` prints a one-line notice to stderr for each
  file: `contained: seeded ~/.claude/.credentials.json → <per-project
  path> (mode 600)`.
- `render_dry_run` gains a `credentials:` section that lists which
  files *would* be seeded and which already exist in the state dir.
- Also validate that the source is a regular file via `os.lstat`, not
  a symlink, before copying (closes security review M2).

### 5. Diagnostic detail in build / proxy error messages

**Files:** `contained/runtime.py:_build_image`, `contained/proxy.py:_run`

- `_build_image` runs `subprocess.run(cmd)` with inherited stdio (fine
  for streaming docker output) but on failure only says
  `f"{what} build failed (tag={tag})"`. The user has no idea *why*.
  For the overlay path, same story: `"overlay build failed for
  {dockerfile}"`.
- `proxy._run` dumps raw docker stderr into `ProxyError`, which is
  only pretty-wrapped by `runtime.run` for the top-level ProxyError,
  not for OSError paths. The common "you didn't run `contained build`"
  failure hits an OSError from docker and the wrapping hint is lost.

**Fix:**

- `_build_image` and `build_overlay` capture stderr via `tee` (or by
  streaming and retaining the last N lines) and include the tail in
  the error message.
- `runtime.run`'s proxy error wrapper catches `OSError` as well as
  `ProxyError` and emits the same "run `contained build` first, or
  pass `--network host`" hint.
- Overlay `"first FROM must be FROM contained-base"` error names what
  the user actually wrote and explains **why** the constraint exists
  (the line is rewritten at build time to pin the real base ref).

### 6. Overlay Dockerfile `FROM` rewrite is a substring replace

**File:** `contained/runtime.py:174-180`

`source.replace("FROM contained-base", f"FROM {base_image}", 1)` is
brittle:

- A multi-stage build with two `FROM contained-base` lines only gets
  the first rewritten; the second stage fails.
- A `FROM contained-base-extra` or an inline `RUN echo FROM
  contained-base` anywhere in the file can confuse the match.
- The "first FROM must be the placeholder" rule is documented but not
  enforced — any occurrence counts.

**Fix:** parse the Dockerfile line-by-line, locate the first `FROM`
instruction (the first non-comment, non-whitespace line whose first
token is `FROM`), and rewrite only that instruction. Reject any file
whose first `FROM` isn't the placeholder with a concrete error that
quotes the offending line. Rewrite all subsequent `FROM
contained-base` lines too (multi-stage) — they should resolve to the
same base.

Also: add a test that asserts the rewritten Dockerfile actually
reaches `docker build`'s stdin (currently `test_build_overlay_rebuilds_
when_forced` only checks that *some* `docker build` command fired, not
the content piped to it).

### 7. Proxy tempfile handling

**File:** `contained/proxy.py:44-70`

`mkstemp` gives a secure fd, which the code then closes before calling
`Path(path).write_text(path)` — so the filesystem path is written via
a fresh open, negating the mkstemp guarantee and leaving a race
window. The late `import os` inside the function is also odd.

**Fix:** write via the fd directly (`os.fdopen(fd, "w")`), or use
`tempfile.NamedTemporaryFile(delete=False, dir=<per-project state
dir>)` so the file lives under the already-0700 state tree rather than
world-writable `/tmp`. Move `import os` to module scope.

## Acceptance criteria

- [ ] Allowlist regexes are `^host(:[0-9]+)?$`; wildcards either work
      or are rejected with a clear error.
- [ ] One end-to-end test that actually curls through the proxy and
      verifies allow/deny (docker-gated, skipped when unavailable).
- [ ] `contained run claude --mount /some/symlink-to-home:/x` is
      refused; new test covers it.
- [ ] Single `_mask` function in the codebase.
- [ ] `--env KEY=VALUE` and `--env-from` values never appear
      unredacted in dry-run output or error messages.
- [ ] `_load_env_files` errors do not echo the offending line.
- [ ] First `contained run claude` with an unseeded state dir prints
      a visible `seeded ... -> ...` line.
- [ ] `render_dry_run` shows a `credentials:` section.
- [ ] Credential source is verified as a regular file (not a symlink)
      before copy.
- [ ] Base and overlay build failures include the tail of docker's
      stderr in the error message.
- [ ] Overlay `FROM` rewrite parses instructions properly and handles
      multi-stage builds.
- [ ] A test asserts the rewritten Dockerfile content reaches the
      build subprocess, not just that `docker build` was invoked.
- [ ] Proxy filter file is written via the mkstemp fd or lives under
      the per-project state dir; `import os` at module scope.

## Out of scope

- IPv6 / DNS-tunneling exfil paths (separate PRD or documented as
  known limitation).
- Stale proxy container / network sweep on startup.
- `contained prune` for orphaned per-project state dirs.
- `contained profiles` / `contained run --explain`.
- Richer doctor diagnostics and `contained build` progress framing —
  those are UX items for a separate PRD.
