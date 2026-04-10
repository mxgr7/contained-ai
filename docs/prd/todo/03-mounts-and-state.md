# PRD — Directory mounts, env forwarding, and state persistence

## Goal

Make the two biggest pain points of running an agent in a container —
"how do I get my files in?" and "how do I keep my session history
across runs?" — feel ergonomic and safe.

## Directory mounts

### The default mount

In every `contained run`, the project root (the directory containing the
discovered `contained.yaml`, or `$PWD` if none was found) is bind-mounted
to `/workspace` inside the container, read-write, and the agent starts
with `/workspace` as its working directory.

This is the single most common thing users want. Making it automatic is
the reason this tool exists.

### Additional mounts

Users can add more mounts via CLI flags or config:

- `--mount <host>:<container>` — read-write bind.
- `--mount-ro <host>:<container>` — read-only bind.
- `mounts:` / `mounts_ro:` in `contained.yaml`.

### Path resolution

- `~` is expanded to the invoking user's home.
- Relative host paths are resolved relative to the **config file's
  directory** if the mount came from `contained.yaml`, and relative to
  `$PWD` if it came from a CLI flag.
- Nonexistent host paths are a hard error (`contained` does **not**
  `mkdir -p` silently; that's a common source of surprise empty
  directories).
- Container paths must be absolute.

### Safety rails

- Refuse to mount `/` as a source.
- Refuse to mount the user's home directory as a source unless an explicit
  `--allow-home-mount` flag is set. (Too easy to leak SSH keys, browser
  profiles, etc.)
- Warn — but don't refuse — when a mount source contains a `.env`, a
  `.ssh` directory, or a `.aws` directory, unless it's `mount-ro`.

### Permissions (Linux specifically)

Bind mounts on Linux preserve host uid/gid, which can clash with the
container's `agent` user (uid 1000). The MVP strategy:

- If the host uid matches the container user's uid, do nothing.
- Otherwise, document the issue in `contained doctor` and recommend
  `--user $(id -u):$(id -g)` as an override. A fuller solution
  (user-namespace remapping or dynamic entrypoint chown) is deferred.

On macOS the Docker Desktop VM handles this transparently, so no special
handling is needed.

## Environment variable forwarding

### Explicit by default

`contained` does **not** forward the host environment wholesale. Every
variable the container sees is either:

1. Set explicitly via `--env` / `env:` in config.
2. Provided by the agent profile's defaults (e.g. `ANTHROPIC_API_KEY` is
   in the `claude` profile's env allowlist).
3. A small set of universally safe vars set by `contained` itself:
   `TERM`, `LANG`, `LC_ALL`, `TZ`.

This is deliberately conservative. Agents that need a var will say so,
and users will get a clear error; that's better than silently leaking
`AWS_SECRET_ACCESS_KEY` into a container.

### Flag syntax

- `--env KEY` — forward `KEY` from the host; error if unset.
- `--env KEY=VALUE` — set explicitly.
- `--env-from <file>` — dotenv-style file; values from the file override
  anything earlier in the precedence chain.

### Masking in logs

Whenever `contained` prints the resolved config (e.g. `--dry-run`,
`doctor`, error messages), env values are masked: only the variable name
and a fixed-width placeholder are shown. Values are never logged in full.

## Session / state persistence

### Problem

Agents write state that the user wants to keep across runs:

- Claude Code: session history, MCP configs, auth tokens (currently under
  `~/.claude`).
- `pi`: whatever the coding-agent persists under its config dir.
- Shared things both agents care about: a shell history, a cache
  directory, logs.

If we don't mount these out, they vanish when the container exits (the
lifecycle is ephemeral + `--rm`).

### Strategy

`contained` manages a **per-project state directory on the host**,
bind-mounted into the container at the paths the agent expects.

### Host layout

```
${XDG_DATA_HOME:-~/.local/share}/contained/
  projects/
    <project-id>/
      claude/        # mounted as the claude agent's state dir
      pi/            # mounted as the pi agent's state dir
      shared/
        bash_history
        logs/
  base/              # shared across projects (opt-in; empty in MVP)
```

- `<project-id>` is derived from the absolute path of the project root,
  hashed to a short stable identifier, with the basename kept as a prefix
  for readability (e.g. `contained-ai-3f2a9c`).
- The directory is created lazily on first run, with `0700` permissions.
- Users can inspect or delete it with regular filesystem tools;
  `contained` never touches state outside this tree.

### Container-side mounts

Each agent profile declares the container paths its state directory
should appear at. For Claude Code that's roughly `/home/agent/.claude`;
for `pi` it's whatever the coding-agent uses (to be confirmed during
implementation). See `05-agents-and-auth.md`.

These mounts are added automatically; the user doesn't need to configure
them. They can be disabled with `--no-state` for one-off runs where the
user explicitly wants a clean container (e.g. for reproducing a bug).

### What does **not** go in the state dir

- The project's source code. That's the workspace mount.
- Host credentials being forwarded read-only (e.g. a mounted
  `~/.gitconfig`). Those are inputs, not state.
- Anything under `/tmp` inside the container. Tmp is ephemeral on
  purpose.

## Acceptance criteria (MVP)

- [ ] `contained run claude` in an arbitrary directory bind-mounts that
      directory to `/workspace` read-write.
- [ ] Additional `--mount` and `--mount-ro` flags work and compose with
      config-file mounts.
- [ ] Mounting `/` or `~` without the explicit opt-in flag fails with a
      clear error.
- [ ] No host env var reaches the container unless it's in the profile
      defaults, the config, or a flag.
- [ ] Env values are masked in `--dry-run` and error output.
- [ ] Exiting and re-running `contained run claude` in the same project
      preserves the agent's prior session history.
- [ ] `--no-state` runs a clean container with no persisted state mounted.
- [ ] Two different projects have independent state directories.
