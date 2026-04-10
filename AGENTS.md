# AGENTS.md

Guidance for coding agents working in this repository.

## What this project is

**contained-ai** (CLI: `contained`) is a Python tool that wraps Docker to
make running coding agents — initially Claude Code and `pi` — inside
containers ergonomic. The point is maximum isolation from the host
without the usual friction of manual `docker run` invocations.

The containerized agent gets:
- The project directory mounted as `/workspace` (read-write by default).
- Only the env vars it explicitly needs, forwarded from the host.
- Host credentials mounted read-only where possible.
- Egress restricted to an allowlist (LLM provider + common registries +
  GitHub by default).
- Session history persisted to a per-project directory on the host so
  state survives the ephemeral container.

## Core objectives

1. **Isolation by default.** The container is the trust boundary. Don't
   add conveniences that silently punch holes in it (wholesale env
   forwarding, mounting `~`, disabling the allowlist).
2. **Ergonomics for the 95% case.** `contained run claude` in any project
   should Just Work with zero flags and no config file. Power comes from
   `contained.yaml` and flags, not from making the default verbose.
3. **Thin over Docker, not a replacement.** Don't reinvent what Docker
   already does well. Users should be able to drop down to `docker` when
   they need to, and `--dry-run` should always show the underlying
   invocation.
4. **Explicit over implicit.** Env vars, mounts, and allowlist entries
   are all declared. Silent magic is a bug.
5. **Credentials are never logged.** Values are masked in every output
   path (`--dry-run`, `doctor`, errors).

## Locked-in design decisions

These are settled for the MVP — don't re-litigate without checking with
the user first:

- **Language:** Python.
- **Target agents (MVP):** Claude Code and `pi`
  (`github.com/badlogic/pi-mono/tree/main/packages/coding-agent`).
- **Config:** CLI flags + per-project `contained.yaml`. Precedence
  (high → low): flags → `agents.<name>` → `defaults` → profile defaults
  → tool defaults. List-valued fields union across layers.
- **Network:** egress allowlist by default, via a sidecar HTTP(S) proxy.
  Modes are `allowlist` (default), `host`, `none`.
- **Platforms:** macOS (Docker Desktop) and Linux. No Windows/WSL2 in
  MVP.
- **Images:** one opinionated base image per agent, plus an optional
  per-project `Dockerfile.contained` overlay that must `FROM
  contained-base`. Multi-arch amd64/arm64.
- **Credentials:** forwarded from the host via mount/env, read-only
  where the agent allows it, scoped per profile (no cross-contamination
  between agents).
- **Lifecycle:** ephemeral per invocation (`docker run --rm` + TTY
  attach). No persistent/named containers in MVP.
- **State:** persisted under
  `${XDG_DATA_HOME:-~/.local/share}/contained/projects/<project-id>/<agent>/`
  and bind-mounted into the agent's state path.

## Where things live

- `docs/prd/` — PRDs, organized kanban-style:
  - `todo/` — not yet implemented.
  - `in_progress/` — currently being worked on.
  - `done/` — shipped.
  - Move PRD files between these directories as work progresses.
- `docs/prd/todo/00-overview.md` — project overview, MVP roadmap, phase
  breakdown. **Read this first.**
- `docs/prd/todo/01-cli-and-config.md` — CLI surface and
  `contained.yaml` schema.
- `docs/prd/todo/02-container-runtime.md` — image strategy, lifecycle,
  platform support, security posture.
- `docs/prd/todo/03-mounts-and-state.md` — directory mounts, env
  forwarding, state persistence layout.
- `docs/prd/todo/04-networking.md` — allowlist model and proxy
  strategy.
- `docs/prd/todo/05-agents-and-auth.md` — agent profile concept and the
  `claude` / `pi` profile specs.

## Conventions for agents editing this repo

- **Prefer editing PRDs over writing new ones.** If a change fits an
  existing document, update it. New PRDs should only appear for
  genuinely new feature areas.
- **Don't expand scope.** The PRDs are deliberately MVP-focused. If
  you're tempted to add "while we're here," flag it to the user
  instead.
- **Default to no comments in code** once implementation starts.
  Identifiers should do the talking; comments explain non-obvious
  *why*, not *what*.
- **Keep `contained.yaml` examples in the PRDs in sync** when you
  change the schema.
- **Never commit credentials, `.env` files, or anything under
  `~/.claude`.** This project is specifically about not leaking those
  places.

## Out of scope (for now)

- Windows / WSL2 support.
- Podman or rootless Docker as the backend.
- Host keychain integration for credentials.
- Multi-tenant or remote execution.
- Agent orchestration / running multiple agents in one invocation.
- Named persistent containers (`contained start` / `attach`).
- Third-party agent profiles as a supported extension point (the
  abstraction allows it, but it's not a product surface yet).
