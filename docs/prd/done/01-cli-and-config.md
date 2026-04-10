# PRD — CLI surface & configuration

## Goal

Define the command-line interface and the per-project configuration file
(`contained.yaml`) for the MVP. The CLI is the primary UX; everything else
in this project exists to make the CLI feel good.

## Design principles

- **`contained run <agent>` should Just Work** in any project, with zero
  flags and no config file. Defaults are opinionated.
- **Flags are for one-offs; config files are for repeat use.** Anything you
  can set via a flag, you can set in `contained.yaml`, and vice versa.
- **Flags always win** over config-file values. Config-file values always
  win over built-in defaults.
- **Keep the top-level verb list small.** Subcommands earn their place.

## Commands (MVP)

### `contained run <agent> [-- <agent args>]`

Starts an ephemeral container, attaches a TTY, and runs the agent inside.
Removes the container on exit.

**Positional:**
- `<agent>` — the agent profile to launch. MVP: `claude`, `pi`. See
  `05-agents-and-auth.md`.
- Everything after `--` is passed through to the agent's entrypoint
  unchanged.

**Flags:**
- `--mount <host>:<container>` — bind-mount a host directory read-write.
  Repeatable.
- `--mount-ro <host>:<container>` — bind-mount read-only. Repeatable.
- `--env KEY[=VALUE]` — forward an env var (value from host if omitted).
  Repeatable.
- `--env-from <file>` — load env vars from a dotenv-style file.
- `--network host|none|allowlist` — override network policy. Default:
  `allowlist`.
- `--allow <host>[:port]` — add to the egress allowlist for this run.
  Repeatable.
- `--image <ref>` — override the base image.
- `--rebuild` — force a rebuild of the project's Dockerfile overlay if one
  exists.
- `--dry-run` — print the resolved container config and the `docker`
  invocation without running it. Useful for debugging.
- `--config <path>` — use a specific config file instead of discovering
  `contained.yaml`.
- `--no-config` — ignore any discovered config file.

**Exit code:** the agent's exit code, passed through verbatim.

### `contained doctor`

Prints a diagnostic report: Docker reachable, base image present, session
store writable, host credentials discoverable, allowlist connectivity. Must
be runnable even when most things are broken.

### `contained version`

Prints version and build info. Also available as `contained --version`.

### Not in MVP

- `contained pull` — pull/update the base image. Phase 6 if needed.
- `contained login` — only if we move off host credential forwarding.
- `contained exec` — ephemeral lifecycle makes this fuzzy; defer.
- `contained ps` / `contained logs` — same reasoning.

## Config file: `contained.yaml`

Discovered by walking up from `$PWD` until a `contained.yaml` is found, or
the filesystem root is reached. If none is found, built-in defaults apply.

### Schema (informal)

```yaml
# Agent to use when `contained run` is called without an explicit agent.
# Optional; if absent, the user must pass one.
default_agent: claude

# Per-agent overrides. Any key here merges on top of the profile defaults.
agents:
  claude:
    # Override the image for just this agent in this project.
    image: ghcr.io/example/contained-claude:edge
    env:
      - ANTHROPIC_API_KEY         # forward from host
      - CLAUDE_MODEL=claude-opus-4-6
    mounts:
      - ./:/workspace             # rw bind
    mounts_ro:
      - ~/.gitconfig:/root/.gitconfig
    allowlist:
      - api.anthropic.com
      - registry.npmjs.org
  pi:
    env:
      - OPENAI_API_KEY

# Settings that apply regardless of agent.
defaults:
  mounts:
    - ./:/workspace
  env:
    - TERM
    - LANG
  network: allowlist
  allowlist:
    - github.com:443
    - objects.githubusercontent.com:443
```

### Merge rules

Precedence, highest to lowest:

1. CLI flags for this invocation.
2. `agents.<name>.*` in the discovered `contained.yaml`.
3. `defaults.*` in the discovered `contained.yaml`.
4. The agent profile's built-in defaults (see `05-agents-and-auth.md`).
5. Tool-wide built-in defaults.

List-valued fields (`mounts`, `env`, `allowlist`) **union** across layers
rather than replacing, so a project can add to the defaults without
restating them. A flag like `--no-default-allowlist` (phase 4) can be added
later if users need to start from an empty list.

### Validation

- Unknown keys are a hard error (typos should fail loudly).
- Host paths are resolved relative to the config file's directory.
- `~` is expanded.
- Environment-variable references in values (e.g. `${HOME}`) are expanded
  at load time.

## `--dry-run` output

`--dry-run` must print:

1. The resolved config (after all merging).
2. The list of mounts with resolved absolute host paths.
3. The list of env vars with values masked (`ANTHROPIC_API_KEY=***`).
4. The effective network policy and allowlist.
5. The `docker run` command that would be executed.

This is the primary debugging tool; treat its output as part of the
contract and don't break it casually.

## Acceptance criteria

Scoped to what this PRD owns — the CLI surface and config loader.
Criteria that require a running container (e.g. "starts a Claude Code
session") live in `02-container-runtime.md`.

- [x] `contained --version` and `contained --help` work.
- [x] `contained run <agent>` accepts every flag listed above.
- [x] `contained.yaml` is discovered by walking up from `$PWD`.
- [x] `--config <path>` and `--no-config` override discovery.
- [x] Merge precedence holds: flag > `agents.<name>` > `defaults` >
      profile defaults > tool defaults.
- [x] List-valued fields (`mounts`, `mounts_ro`, `allowlist`) union
      across layers; `env` merges by key with the higher layer winning.
- [x] Unknown keys in `contained.yaml` cause a clear error, not silent
      ignore.
- [x] `~` and `${VAR}` are expanded in config string values.
- [x] `contained run --dry-run <agent>` prints the resolved config, the
      env list with secrets masked, the allowlist, and a preview of the
      `docker` invocation — without touching Docker.
- [x] `contained doctor` runs to completion even when Docker is not
      reachable and reports each check's status.
