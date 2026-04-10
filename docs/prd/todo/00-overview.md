# contained-ai — Project Overview & MVP Plan

## Problem

Running coding agents like Claude Code and `pi` directly on a developer's
machine gives them broad access to the host: the full filesystem, every
environment variable, all network destinations, and whatever credentials
happen to be loaded. That's convenient, but it makes it hard to trust an
agent with autonomous or long-running work, and a single prompt-injection or
runaway command can do real damage.

Docker already solves the isolation problem, but running an agent inside a
container is awkward:

- Mounting the right directories (read-only vs. read-write) requires long,
  error-prone `docker run` invocations.
- Forwarding just the environment variables the agent needs — and nothing
  more — is tedious.
- The agent's session history, logs, and caches live inside the container
  and vanish when it exits.
- Network access has to be either "all or nothing" unless you hand-roll
  iptables or a proxy.
- Credentials (API keys, OAuth tokens) have to be wired in by hand.

## Vision

**`contained-ai`** (CLI: `contained`) is a thin, opinionated wrapper around
Docker that makes running a coding agent in a container feel as ergonomic as
running it on the host — without giving up the isolation benefits.

The user types `contained run claude` in a project directory and gets a
fully interactive Claude Code session running inside a container, with the
project mounted, credentials forwarded, egress restricted to an allowlist,
and session history persisted to a predictable location on the host.

## Non-goals (for MVP)

- Not a sandbox for untrusted code execution in the cryptographic sense.
  Docker is the trust boundary; we inherit its guarantees and no more.
- Not a replacement for `docker` — power users can always drop down.
- Not a multi-tenant / remote execution platform. Local, single-user only.
- Not an agent orchestrator. One agent per invocation.
- No Windows support in the MVP (see `02-container-runtime.md`).

## Target users

Developers who already use Claude Code or `pi` on macOS or Linux and want to
run them with tighter isolation — either for peace of mind, to experiment
with `--dangerously-skip-permissions`-style modes safely, or to prevent
credential leakage across projects.

## Key design decisions (locked in)

| Decision | Choice | Rationale |
|---|---|---|
| Implementation language | Python | Fast iteration; `docker` SDK is mature; users likely already have Python. |
| Target agents (MVP) | Claude Code, `pi` | Two concrete profiles; generic fallback comes later. |
| Configuration | CLI flags + `contained.yaml` per project | Flags for one-offs, config file for repeat use. |
| Default network policy | Egress allowlist | Strong default; overridable. |
| Host platforms | macOS (Docker Desktop), Linux | Windows/WSL2 deferred. |
| Image strategy | Opinionated base image + optional per-project Dockerfile overlay | Good UX by default, extensible when needed. |
| Credential forwarding | Mount / env from host | Simplest; works with existing agent auth. |
| Lifecycle | Ephemeral per invocation | One container per `contained run`; history persisted via host mount. |

## MVP scope

The MVP is considered done when a user on macOS or Linux can:

1. `pip install contained-ai` (or equivalent).
2. `cd` into any project directory.
3. Run `contained run claude` and land in an interactive Claude Code session
   inside a container, with the current directory mounted read-write,
   host credentials forwarded, and egress restricted to the default
   allowlist.
4. Exit the agent, re-run `contained run claude`, and have the previous
   session history available.
5. Optionally drop a `contained.yaml` in the project to declare extra
   mounts, env vars, allowlist entries, or an agent override — and have
   subsequent `contained run` invocations pick it up automatically.
6. Do the same with `contained run pi`.

## Feature breakdown

Detailed requirements live in sibling documents:

- `01-cli-and-config.md` — CLI surface, flags, and `contained.yaml` schema.
- `02-container-runtime.md` — base image, Dockerfile overlay, lifecycle,
  platform support.
- `03-mounts-and-state.md` — directory mounts, env forwarding, session/state
  persistence.
- `04-networking.md` — egress allowlist model and defaults.
- `05-agents-and-auth.md` — agent profiles (Claude Code, `pi`) and
  credential forwarding.

## High-level roadmap to MVP

The phases below are a suggested ordering, not a rigid schedule.

### Phase 0 — Skeleton
- Python package scaffold (`pyproject.toml`, `contained/` module, entry
  point).
- `contained --version`, `contained --help`.
- CI: lint + type-check + unit tests on macOS and Linux runners.

### Phase 1 — Minimum runnable container
- Base image published (or built locally on first run) with `git`, a
  reasonable shell, and the Claude Code CLI installed.
- `contained run claude` spawns an ephemeral container, mounts `$PWD`
  read-write, attaches a TTY, removes the container on exit.
- No config file yet; no network restrictions yet; credentials forwarded
  naïvely via `ANTHROPIC_API_KEY` / `~/.claude` mount.

### Phase 2 — Config file + mounts/env
- `contained.yaml` parsing and schema validation.
- `--mount`, `--mount-ro`, `--env`, `--env-from` flags.
- Merge rules: flags override config; sensible defaults on top.
- Session/state persistence: per-project directory under
  `~/.local/share/contained/projects/<project-id>/` (XDG-aware), bind-mounted
  into the agent's state path.

### Phase 3 — Second agent profile
- `pi` agent profile (using the coding-agent from `pi-mono`).
- Refactor so adding a profile is a single file + registration.
- `contained run <agent>` dispatches to the right profile.

### Phase 4 — Network allowlist
- Default egress allowlist covering: the agent's LLM provider API,
  common package registries (npm, PyPI, crates.io, Go proxy), GitHub,
  and the user's configured git remotes.
- `allowlist:` key in `contained.yaml` for per-project additions.
- `--network=host|none|allowlist` flag for overrides.
- Implementation strategy documented in `04-networking.md`.

### Phase 5 — Dockerfile overlay
- If a `Dockerfile.contained` exists in the project, `contained` builds an
  image `FROM` the base, caches it, and uses it instead of the base.
- Rebuild triggered by file mtime / content hash.

### Phase 6 — Polish for MVP release
- `contained doctor` — diagnose Docker availability, image presence,
  credential forwarding, allowlist connectivity.
- Readable error messages for the common failure modes (Docker not running,
  image pull failure, mount path doesn't exist, credentials missing).
- Quickstart docs and a recorded demo.

## Open questions (for later, not MVP-blocking)

- Should we support rootless Docker / Podman as a runtime backend?
- How do we handle agents that want to run Docker-in-Docker themselves
  (e.g. to build project images)?
- Do we want a `contained exec` for attaching a second shell to a running
  session, even though the default lifecycle is ephemeral?
- Telemetry / anonymous usage stats — opt-in only, if at all.
