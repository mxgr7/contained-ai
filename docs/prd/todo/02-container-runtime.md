# PRD — Container runtime, images, and lifecycle

## Goal

Define how `contained` talks to Docker, what images it runs, how the
per-project Dockerfile overlay works, and what the container lifecycle
looks like.

## Runtime backend

- **MVP backend: Docker** (Docker Desktop on macOS, native on Linux).
- Accessed via the `docker` Python SDK where practical; fall back to
  shelling out to `docker` for features the SDK doesn't cover cleanly
  (e.g. interactive TTY attach).
- **Podman / rootless**: not in MVP. Design choices should not preclude it,
  but we won't test against it.

### Detection

On startup, `contained` checks that the Docker daemon is reachable. If
not, it emits a single clear error with a pointer to `contained doctor`
and exits non-zero. It does not try to start Docker Desktop for the user.

## Host platforms

| Platform | Status | Notes |
|---|---|---|
| macOS (Docker Desktop, arm64 + amd64) | Supported | Bind mounts are slower; acceptable for agent workflows. |
| Linux (native Docker, arm64 + amd64) | Supported | Primary dev target. |
| Windows / WSL2 | **Not supported in MVP.** | Path translation and credential forwarding need their own design. |

## Base image

A single opinionated base image per agent, published to a public registry
(e.g. `ghcr.io/<org>/contained-<agent>:<tag>`). The tag scheme and registry
are an implementation detail and can change before 1.0.

### Contents (target)

- A small, current Linux distribution (Debian slim or Ubuntu LTS).
- A non-root user (`agent`, uid 1000) — the agent runs as this user.
- `git`, `curl`, `ca-certificates`, `openssh-client`.
- Recent Node.js and Python runtimes (agents commonly shell out to these).
- The agent itself, pinned to a known version.
- A minimal entrypoint that:
  - `cd`s into the configured workspace mount.
  - Drops any extra capabilities.
  - Execs the agent with the passthrough args.

### Multi-arch

Images must be published for both `linux/amd64` and `linux/arm64` so Apple
Silicon users don't pay the emulation tax.

### Versioning

- `:latest` — most recent release, used by default.
- `:<semver>` — pinned release. `contained.yaml` should pin in real
  projects.
- `:edge` — built from `main`, for development of `contained` itself.

## Per-project Dockerfile overlay

If a project contains a `Dockerfile.contained` at the same level as
`contained.yaml` (or at the project root), `contained` treats it as an
overlay on the base image.

### Rules

- The overlay **must** start with `FROM contained-base` (a placeholder tag
  that `contained` resolves to the actual base image for the selected
  agent at build time).
- `contained` builds the overlay on the first `run` that needs it and
  caches the resulting image tag keyed by `(agent, content hash of
  Dockerfile.contained, base image digest)`.
- If any of those inputs change, the image is rebuilt on the next run.
- `contained run --rebuild` forces a rebuild regardless of cache.
- Build output streams to the user's terminal; failures abort the run
  with the builder's error message.

### Example

```dockerfile
# Dockerfile.contained
FROM contained-base

USER root
RUN apt-get update && apt-get install -y --no-install-recommends \
    postgresql-client \
 && rm -rf /var/lib/apt/lists/*

USER agent
RUN pip install --user poetry
```

## Lifecycle

The MVP lifecycle is **ephemeral per invocation**:

1. `contained run <agent>` resolves config → ensures image → creates
   container with `--rm`, the computed mounts, env, network, and an
   interactive TTY.
2. The container runs the agent in the foreground. Stdin/stdout/stderr
   are attached to the user's terminal.
3. When the agent exits (or the user Ctrl-Cs), the container exits and is
   removed by `--rm`.
4. State that needs to survive (session history, logs, caches) is written
   to host-mounted paths; see `03-mounts-and-state.md`.

### Signals

- Ctrl-C in the terminal sends SIGINT to the container's PID 1 (the agent
  entrypoint).
- A second Ctrl-C escalates to SIGTERM.
- A third Ctrl-C escalates to `docker kill`.
- `contained` itself should not swallow signals — it's a thin shim around
  the attached process.

### No persistent mode (yet)

Named persistent containers (`contained start`, `contained attach`) are
deliberately out of scope for MVP. Ephemeral + persisted-on-host state
covers the common case, and adding persistence later is additive.

## Security posture

These are defaults, not hard requirements; all can be overridden by
advanced users if and when we expose flags for them.

- Run as a non-root user inside the container.
- Drop all Linux capabilities (`--cap-drop ALL`); add none back by default.
- `--security-opt no-new-privileges`.
- Read-only root filesystem, with explicit writable mounts for the
  workspace and state dirs. (Stretch goal; if it breaks common agents we
  relax it in MVP and revisit.)
- No host PID / IPC / network namespace sharing.
- Network policy defaults to `allowlist` (see `04-networking.md`).

## Acceptance criteria (MVP)

- [ ] `contained run <agent>` with no config file starts a working agent
      session in `$PWD` (covers the case PRD 01 defers to here).
- [ ] `contained run` in a directory with `contained.yaml` picks it up
      and launches a container using the resolved config.
- [ ] `contained` refuses to run with a clear error if Docker is not
      reachable.
- [ ] The base image exists for `linux/amd64` and `linux/arm64` for each
      supported agent.
- [ ] An ephemeral `contained run claude` starts the agent as a non-root
      user in an interactive TTY and exits cleanly on agent exit.
- [ ] A project with `Dockerfile.contained` builds its overlay once,
      reuses it on the next run, and rebuilds when the file changes.
- [ ] `contained run --rebuild` forces a rebuild.
- [ ] Ctrl-C is forwarded to the agent, not swallowed by `contained`.
