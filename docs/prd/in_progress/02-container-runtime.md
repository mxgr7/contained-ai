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

A **single shared base image** used by every agent profile, published to
a public registry at `ghcr.io/contained-ai/contained-base:<tag>`. Agents
differ only in their entrypoint and the credentials/mounts they require
— the image itself contains all supported agent CLIs side by side. The
tag scheme and registry are implementation details and can change
before 1.0.

Keeping one image avoids duplicated base layers, simplifies overlay
caching (the `FROM contained-base` placeholder always resolves to the
same ref), and lets the user install every supported agent with a
single `contained build`.

### Contents (target)

- A small, current Linux distribution (Debian slim).
- A non-root user (`agent`, uid 1000) — the agent runs as this user.
- `git`, `curl`, `ca-certificates`, `openssh-client`, `tini`.
- Recent Node.js and Python runtimes (agents commonly shell out to these).
- All MVP agent CLIs installed globally, pinned to known versions.
- A minimal entrypoint that:
  - `cd`s into the configured workspace mount.
  - Drops any extra capabilities.
  - Execs the selected agent with the passthrough args.

### Local builds

The repository ships `contained/assets/Dockerfile.base` and a
`contained build` subcommand that builds it locally and tags the result
as the default base image ref (or `--tag <ref>`). Use cases:

- **Before a published image exists** — today, users `contained build`
  once and then `contained run` works against the local image because
  the tag matches the default ref.
- **Customizing the base** — users can fork the Dockerfile, build with
  `--tag` to a private ref, and point `contained.yaml` or `--image` at
  it.
- **Forcing a clean rebuild** — `contained build --rebuild` passes
  `--no-cache` to `docker build`.

Per-project customization that only needs extra packages should still
use `Dockerfile.contained` overlays (see below) rather than forking the
base image.

### Multi-arch

The published image must exist for both `linux/amd64` and `linux/arm64`
so Apple Silicon users don't pay the emulation tax. Local `contained
build` produces whatever arch the host runs.

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

- The overlay **must** start with `FROM contained-base` (a placeholder
  tag that `contained` resolves to the shared base image ref at build
  time).
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

Code-side criteria (this PRD's ownership):

- [x] `contained run` composes a `docker run` invocation with the
      resolved config, workspace mount, non-root user, `--cap-drop ALL`,
      `--security-opt no-new-privileges`, `--init`, and `--rm`.
- [x] `contained run` refuses to run with a clear error if Docker is
      not reachable (missing binary or daemon down).
- [x] A project with `Dockerfile.contained` builds its overlay once,
      reuses the cached image when the file and base image are
      unchanged, and rebuilds when either changes.
- [x] `contained run --rebuild` forces an overlay rebuild.
- [x] Ctrl-C is not swallowed by `contained` — the Python parent shares
      a process group with `docker` and waits on the child after
      receiving SIGINT.
- [x] `--dry-run` output and the real invocation share a single
      `build_argv` code path so the preview can't drift from reality.

Local-build path (satisfies end-to-end in the absence of a published
image):

- [x] `contained build` builds `contained/assets/Dockerfile.base` and
      tags it as the default base image ref (or `--tag <ref>`).
- [x] `contained build --rebuild` passes `--no-cache` to docker build.

Blocked on base-image publishing (tracked separately):

- [ ] A published `ghcr.io/contained-ai/contained-base` image exists
      for `linux/amd64` and `linux/arm64`.
- [ ] `contained run claude` works end-to-end in an empty directory
      against the published image (without requiring `contained build`
      first).

Deferred to PRD 04 (networking):

- [ ] `network: allowlist` actually restricts egress via the sidecar
      proxy. Today the runtime falls back to the default bridge for
      `allowlist` and only honours `host`/`none` strictly.
