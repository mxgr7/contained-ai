# PRD — Agent profiles & credential forwarding

## Goal

Define what an "agent profile" is, specify the two profiles shipped in
MVP (`claude` and `pi`), and describe how host credentials are forwarded
to the containerized agent.

## Agent profile concept

An **agent profile** is a small bundle of defaults that describes how to
run one specific agent inside `contained`. A profile is a single Python
module (or YAML manifest, TBD during implementation) that declares:

- `name` — the string the user types after `contained run`.
- `image` — the base image to use for this agent. All MVP profiles
  share `ghcr.io/contained-ai/contained-base:<tag>`; a profile only
  overrides this if it genuinely needs a different image.
- `entrypoint` — the command to run inside the container (e.g. the path
  to the `claude` or `pi` binary) and how to pass through user args.
- `env` — the set of env vars this agent needs forwarded from the host.
- `credential_mounts` — read-only host paths to mount for auth (e.g.
  `~/.claude` → `/home/agent/.claude`).
- `state_mounts` — paths inside the container where the agent expects
  to write persistent state; `contained` binds these to the per-project
  state dir (see `03-mounts-and-state.md`).
- `allowlist` — network hosts this agent specifically needs, on top of
  the tool-wide defaults (see `04-networking.md`).
- `workdir` — working directory inside the container (default
  `/workspace`).

Profiles are merged on top of tool-wide defaults and under
`contained.yaml` / CLI flags, per the precedence rules in
`01-cli-and-config.md`.

Adding a new profile should be a single file plus a line in a registry.
Third-party profiles are an explicit post-MVP goal but don't need to
work in MVP.

## MVP profile: `claude` (Claude Code)

### Identity

- CLI: `contained run claude`
- Upstream: [Anthropic Claude Code](https://claude.com/claude-code)

### Image

The shared `ghcr.io/contained-ai/contained-base:<tag>` — Claude Code is
installed in the base image alongside the other MVP agents. See
`02-container-runtime.md` for base image contents and the
`contained build` local-build path.

### Entrypoint

`claude` (the installed CLI), with all post-`--` args passed through.
Default working directory: `/workspace`.

### Env

- `ANTHROPIC_API_KEY` — forwarded from host if set.
- `CLAUDE_MODEL` — forwarded from host if set (optional).
- `TERM`, `LANG`, `LC_ALL`, `TZ` — from the tool-wide defaults.

### Credential mounts

- `~/.claude` → `/home/agent/.claude` **read-only**.
  - Rationale: Claude Code stores OAuth credentials and some config here.
    Mounting it read-only lets the container authenticate without the
    agent being able to rewrite the host's copy. A read-only mount may
    break features that expect to write into `~/.claude`; if so, we fall
    back to mounting it read-write under a scoped subdirectory of the
    per-project state dir and copying credentials in at startup. This
    will be validated during Phase 1 implementation.

### State mounts

- `/home/agent/.claude/projects/<project-id>` (or whatever subpath
  Claude Code uses for per-project session history) → per-project
  state dir under `.../projects/<project-id>/claude/`.
- A separate `/home/agent/.local/share/contained-shared` for things
  like shell history.

The exact container paths will be pinned down during Phase 1 by looking
at where Claude Code actually writes, not guessed.

### Allowlist contribution

- `api.anthropic.com:443`
- Any additional endpoints Claude Code uses for auth/updates (to be
  enumerated in Phase 4).

## MVP profile: `pi`

### Identity

- CLI: `contained run pi`
- Upstream: [`pi-mono/packages/coding-agent`](https://github.com/badlogic/pi-mono/tree/main/packages/coding-agent)

### Image

The shared `ghcr.io/contained-ai/contained-base:<tag>`. `pi` is
installed in the base image alongside Claude Code — there is no
per-agent image. Upstream install mechanism (npm, release binary, or
git clone of `pi-mono/packages/coding-agent`) is TBD during Phase 1
and lives in `contained/assets/Dockerfile.base`.

### Entrypoint

The `pi` coding-agent CLI, with pass-through args. Details deferred
until we read the upstream README during implementation; the profile
abstraction is intentionally flexible enough to accommodate whatever
`pi`'s actual invocation looks like.

### Env

- Whichever LLM provider key(s) `pi` uses (likely `OPENAI_API_KEY` or
  similar — to be confirmed). Forwarded from host if set.
- Tool-wide TERM/LANG/LC_ALL/TZ.

### Credential mounts

- `pi`'s config dir (path TBD during implementation), read-only, using
  the same fallback strategy as `claude` if read-only breaks things.

### State mounts

- `pi`'s state dir → per-project state under `.../projects/<id>/pi/`.

### Allowlist contribution

- The LLM provider API hosts `pi` talks to. Enumerated during profile
  implementation.

## Credential forwarding — principles

1. **Credentials come from the host**, not from a `contained`-managed
   store. The MVP's job is to wire them into the container, not to
   manage them.
2. **Read-only by default.** If a credential file can be mounted
   read-only without breaking the agent, it is. Agents that insist on
   writing to their credential dir get a scoped writable copy rather
   than access to the host's real file.
3. **Masked everywhere.** API keys and token values never appear in
   `contained`'s output — `dry-run`, logs, error messages all mask
   them.
4. **Per-agent scope.** A credential only reaches the agent that needs
   it. `ANTHROPIC_API_KEY` isn't forwarded to `pi`; `OPENAI_API_KEY`
   isn't forwarded to `claude`. This is enforced by the per-profile
   env declaration.
5. **No host keychain integration in MVP.** macOS Keychain / libsecret
   / etc. are out of scope for now; users either export an env var or
   keep their existing agent credential file in its standard location.

## What the user sees

```
$ contained run claude
contained: using image ghcr.io/contained-ai/contained-base:0.1.0
contained: mounting /Users/max/Projects/demo -> /workspace (rw)
contained: mounting /Users/max/.claude -> /home/agent/.claude (ro)
contained: state dir ~/.local/share/contained/projects/demo-3f2a9c/claude
contained: network policy = allowlist (17 hosts)
contained: forwarding env: ANTHROPIC_API_KEY=***
[claude starts here, attached to this terminal]
```

On exit, the session history is sitting in
`~/.local/share/contained/projects/demo-3f2a9c/claude/` and will be
there next time.

## Acceptance criteria (MVP)

- [ ] `claude` profile registered and runnable.
- [ ] `pi` profile registered and runnable.
- [ ] Each profile only forwards its own env vars, not the other's
      credentials.
- [ ] Host credential file is mounted (read-only where possible) and
      auth Just Works inside the container for both agents.
- [ ] Session history persists across runs for both agents in the
      per-project state dir.
- [ ] Env values are masked in every place `contained` prints them.
- [ ] Adding a third profile requires only a new profile file and a
      registry entry — no changes to the core CLI or runtime code.
