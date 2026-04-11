# PRD — Live allowlist updates for a running container

## Problem

Today the egress allowlist is frozen at `contained run` time. The
tinyproxy sidecar is started once with a bind-mounted filter file
(`contained/proxy.py:start`), and there is no way to add a host after
the agent is already running. If the agent needs to reach a domain the
user didn't anticipate, the only recourse is to Ctrl-C the session,
relaunch with `--allow <host>` (or edit `contained.yaml`), and lose the
in-progress REPL state.

This is painful in practice because allowlists are, by nature,
incomplete on the first run — users discover missing hosts when a
package install or API call fails mid-session.

## Goal

Let a user add one or more hosts to the allowlist of a **live**
`contained run` session without restarting the agent container.

Out of scope for this PRD:

- Removing hosts at runtime (restart is fine; removal is rare and the
  teardown path is already well-tested).
- Persisting the additions back to `contained.yaml` (users can do that
  themselves; auto-editing config files is its own discussion).
- Cross-run caching of "hosts I added last time" (see Deferred).

## Why this is feasible

Tinyproxy reloads its configuration, including the `Filter` file, on
`SIGHUP`. The filter file is already bind-mounted from a host path
tracked on `ProxySession.filter_path`. So the mechanism is:

1. Rewrite the host-side filter file with the expanded allowlist.
2. `docker kill -s HUP contained-proxy-<run_id>` to make tinyproxy
   re-read it.

No container restart, no network reshuffling, no loss of the agent's
TTY session.

## UX

New subcommand:

```
contained allow <host>[:port] [<host>[:port] ...] [--run <run_id>]
```

- With no `--run`, discover the active run by listing containers with
  the `contained-proxy-` name prefix. If there's exactly one, use it.
  If there's more than one, error out and list the candidates with
  their run IDs and target project paths (stored as a container label,
  see below).
- With `--run <id>`, target that specific proxy container.
- On success, print the resolved new allowlist delta and a one-line
  confirmation: `added example.com:443 to run a1b2c3d4 (5 → 6 entries)`.
- On failure (no matching run, docker error, SIGHUP sent but filter
  reload not observable), surface a clear error.

Companion read-only command, mostly for debugging and doctor-style
introspection:

```
contained allow --list [--run <run_id>]
```

Prints the current resolved allowlist for the run.

## Design

### Tracking runs

Today `ProxySession` lives only in the parent `contained run` process's
memory and is discarded when the process exits. `contained allow` is a
separate invocation, so it needs a way to find the proxy container and
its filter file path from scratch.

Two bits of state are needed:

1. **The filter file path on the host.** Add a docker label to the
   proxy container at start time, e.g.
   `contained.filter_path=/path/to/filter.txt`, and read it back with
   `docker inspect`.
2. **The run ID and project.** Already encoded in the container name
   (`contained-proxy-<run_id>`); also add labels
   `contained.run_id=<id>` and `contained.project=<abs path>` so the
   disambiguation UI can show something meaningful.

No new on-disk registry; the source of truth is docker itself. This
keeps the feature stateless and avoids the usual "stale lockfile"
class of bugs.

### Mutating the filter file

`contained/proxy.py` gains:

```python
def append_to_filter(session_or_path: Path, new_entries: list[str]) -> None:
    """Rewrite the filter file with the union of existing + new hosts.

    Idempotent: adding a host that's already present is a no-op.
    Preserves the existing anchored-regex format from write_filter_file.
    """
```

Rather than appending raw lines (which would drift from the format
`write_filter_file` produces), the implementation reads the current
file, extracts the host names from the anchored regexes, unions with
the new entries, and rewrites the file via the same code path
`write_filter_file` uses. That guarantees the on-disk format stays
consistent.

The filter file **must be rewritten in place** (truncate + write),
not via temp-file-plus-rename. Docker bind-mounts a single file by
*inode*, so replacing the host file with a new inode leaves the
proxy container pointing at the original (now orphaned) inode
forever. An fcntl lock on a sibling `.lock` file serializes
concurrent `contained allow` invocations; the write-vs-reader window
is zero in practice because the container reloads the filter only
after we explicitly trigger it post-write.

### Reloading tinyproxy

**SIGHUP is insufficient.** The original plan was `docker kill -s
HUP <container>`, based on the assumption that tinyproxy re-reads
its Filter file on SIGHUP. Empirically it does not — tinyproxy
reloads the main config file on SIGHUP but the Filter directive's
host list is cached after the initial parse, so live edits are
silently ignored until the process actually restarts.

The reliable mechanism is `docker restart <container>`. It:

- Stops the container and starts it again with the same config, so
  tinyproxy goes through a full startup and re-parses the Filter
  file from scratch.
- Re-runs the bind mount, picking up any on-disk edit regardless of
  whether the host file was rewritten in place or replaced.
- Survives `--rm` — that flag only fires when the container exits
  for the last time, not on a restart.
- Keeps the existing network attachment, so the agent container
  stays joined to `contained-net-<run_id>` and only sees a brief
  (~1s) egress blip mid-restart.

`contained allow` does a 2s TCP-connect probe per added host from
the host network (same pattern as
`doctor._check_allowlist_reachability`) as an advisory signal. A
failed probe is a warning, not an error, and doesn't prove the agent
container can't reach the host — it only tells the user their host's
own network path is suspect.

### CLI plumbing

`contained/cli.py` gains `_cmd_allow` alongside the existing
`_cmd_run`, `_cmd_build`, etc., wired up in `build_parser` as a new
`sub.add_parser("allow", ...)`. Flags:

- positional `hosts` (one or more, same grammar as `--allow`)
- `--run <id>` (optional)
- `--list` (mutually exclusive with positional hosts)

Host strings are validated with the same parser `--allow` uses today
so the UX is consistent.

## Acceptance criteria

- [x] With a `contained run claude` session active, running
      `contained allow example.com:443` from another terminal adds the
      host and the agent can immediately reach it without restart.
- [x] `contained allow` with no `--run` picks the unique active
      session automatically, and errors cleanly with a candidate list
      when more than one is running.
- [x] `contained allow --list` prints the current resolved allowlist
      for the targeted run.
- [x] Adding a host that's already allowed is a no-op (no duplicate
      filter lines, no error).
- [x] If the filter rewrite fails, the filter file is left in its
      original state (temp-file-plus-rename).
- [x] The post-reload reachability probe prints a one-line OK/warning.
- [x] Docker labels `contained.run_id`, `contained.project`, and
      `contained.filter_path` are set on the proxy container at start.

## Implementation notes

- **Label plumbing** — `proxy.start` takes an optional `project: Path`
  and passes `--label` flags to `docker run` for the proxy container.
  `contained allow` uses `docker ps --filter label=contained.run_id`
  and `docker inspect` to recover the filter path.
- **Filter parser** — a small helper in `proxy.py` that turns
  `^github\.com(:[0-9]+)?$` back into `github.com`. Anchored to the
  format `write_filter_file` produces; anything unrecognized is
  preserved verbatim so a user who hand-edited the file doesn't get
  their edits clobbered.
- **Concurrency** — two `contained allow` invocations racing on the
  same run could interleave writes. A lockfile next to the filter file
  (`<filter>.lock`, `fcntl.flock`) is enough; the critical section is
  measured in milliseconds.
- **Doctor integration** — `contained doctor` already probes the
  tool-wide allowlist; no change needed, but the PRD should note that
  live additions are not reflected in `doctor` output (doctor runs
  against config, not against a specific run).

## Deferred

- **Persisting additions.** A `--save` flag that appends the new host
  to `contained.yaml` would be convenient but opens questions around
  comment preservation and which config file wins when both project
  and user-level configs exist. Ship the live path first; see what
  users actually want.
- **Removing hosts at runtime.** `contained deny <host>` is the mirror
  operation and mechanically trivial (same rewrite + SIGHUP), but real
  demand is unclear — restarts cover the common case.
- **Per-profile scoping.** Today the allowlist is per-run, not
  per-agent-profile; live additions inherit that. If profiles ever
  grow their own runtime-mutable allowlists, revisit.
- **Non-HTTPS protocols.** Tinyproxy's filter only governs HTTP and
  HTTPS CONNECT. Adding an SSH host at runtime is out of scope until
  the base SSH story (PRD 04, "Git over SSH") lands.
