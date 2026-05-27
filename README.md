# contained-ai

Run AI coding agents (Claude Code, `pi`) inside isolated Docker
containers, without fighting Docker every time you start a session.

`contained` is a thin Python wrapper around `docker run` that wires up
the fiddly bits you'd want for a sandboxed agent: a locked-down base
image, read-only workspace mounts, per-project state persistence, an
egress allowlist enforced by a tinyproxy sidecar, and credential
forwarding that doesn't expose your whole home directory. One command
to launch, same ergonomics across agents.

> **Status:** MVP code-complete. Every implementation PRD (01–05) is
> under `docs/prd/done/`. Live end-to-end smoke testing against real
> agents and hosts is the next step.

## Why

Running an autonomous coding agent directly on your laptop means
trusting it with your `~`, your SSH keys, your cloud credentials, and
an unrestricted internet connection. That's a lot of implicit trust
for a process that is, by design, doing things you didn't spell out.

The obvious fix is to put it in a container. In practice, doing that
well involves a long `docker run` line, a hand-rolled Dockerfile, a
state directory scheme, an allowlist, and a way to forward just the
credentials the agent actually needs. `contained` packages that up so
you can stop re-deriving it every time.

Design choices:

- **Allowlist by default, not blocklist.** The set of hosts a coding
  agent legitimately needs is small and well-known; everything else is
  explicit.
- **Per-project state, not shared.** Sessions, credentials copies, and
  command history live under
  `~/.local/share/contained/projects/<project-id>/<agent>/`, so two
  projects can't see each other's context.
- **Credentials from the host, not managed by `contained`.** We copy
  the minimum needed file into the per-project state dir on first run
  (scoped writable copy) so the container can't rewrite your host's
  canonical credential files.
- **Sensible default mounts.** `$PWD` → `/workspace` rw. That's it,
  unless you ask for more.
- **Refuse dangerous shortcuts.** Mounting `/` is refused. Mounting
  `$HOME` requires an explicit flag. Unset required env vars fail
  fast.

## Install

Requires Python 3.10+ and Docker. On macOS that means Docker Desktop;
on Linux, `docker-ce` (rootless works). See `contained doctor` for an
environment check.

The recommended path is [pipx](https://pipx.pypa.io/), which manages
an isolated virtualenv for you and puts `contained` on your `PATH`
globally:

```sh
brew install pipx          # or: python3 -m pip install --user pipx
pipx ensurepath            # reopen your shell after this
pipx install -e .
```

If you just want a plain virtualenv instead:

```sh
python3 -m venv .venv
source .venv/bin/activate
pip install -e .
```

With a venv, `contained` is only on `PATH` while the venv is active.

For running the test suite, install the dev extras into whichever
environment you're using — `pipx inject contained-ai pytest ruff
mypy`, or `pip install -e '.[dev]'` inside the venv.

> **Note:** Homebrew's Python is PEP 668–locked, so a bare
> `pip install -e .` will be refused with "externally-managed-
> environment." Use pipx or a venv.

Then build the two bundled images locally (shared base + egress
proxy):

```sh
contained build
```

This produces:

- `ghcr.io/contained-ai/contained-base:edge` — debian slim with
  Node 20, Python 3, git, `@anthropic-ai/claude-code`, and
  `@mariozechner/pi-coding-agent` pre-installed.
- `ghcr.io/contained-ai/contained-proxy:edge` — tinyproxy configured
  for `FilterDefaultDeny` allowlist enforcement.

`contained run` will pick these up automatically. (A published
multi-arch image on GHCR is a post-MVP follow-up.)

## Quick start

```sh
# From inside any project you want to hand to an agent:
contained run claude
```

What happens:

1. `contained` computes a stable project id from the current directory
   (`<basename>-<sha256(abspath)[:8]>`).
2. It creates the per-project state dir at
   `~/.local/share/contained/projects/<project-id>/claude/`.
3. On first run, it copies `~/.claude/.credentials.json` (or the
   equivalent macOS keychain entry) into a tool-wide global dir at
   `~/.local/share/contained/global/claude/.credentials.json` and
   bind-mounts it into every container, so all `contained` runs share
   a single credential (token refreshes propagate automatically).
4. It creates a `--internal` Docker network, starts a tinyproxy
   sidecar on it with the resolved allowlist, and attaches the agent
   container to that network only.
5. `claude` starts attached to your terminal, with `$PWD` mounted at
   `/workspace`.
6. On exit, the proxy container and network are torn down; the state
   dir is left in place for next time.

```sh
# Same idea, different agent:
contained run pi

# Preview without launching:
contained run claude --dry-run

# Add a one-off host to the allowlist:
contained run claude --allow staging.internal.example.com:443

# Forward extra env vars (errors fast if the var is unset on the host):
contained run claude --env MY_FEATURE_FLAG

# Pass args through to the agent CLI itself:
contained run claude -- --model claude-sonnet-4-5

# Clean run, no persisted state from previous sessions:
contained run claude --no-state
```

## Configuration: `contained.yaml`

`contained` looks for `contained.yaml` in the current directory (and
walks up). CLI flags always win over config; config wins over
built-in defaults.

```yaml
default_agent: claude

defaults:
  allowlist:
    - sentry.io:443
    - my-internal-registry.corp:443

agents:
  claude:
    env:
      - CLAUDE_MODEL=claude-sonnet-4-5
    mounts_ro:
      - ~/.config/gh:/home/agent/.config/gh
```

Precedence, high → low:

1. CLI flags
2. `agents.<name>.*` in `contained.yaml`
3. `defaults.*` in `contained.yaml`
4. Agent profile built-in defaults
5. Tool-wide built-in defaults

List-valued fields (`env`, `allowlist`, `mounts`) union across layers;
scalars let the higher layer override.

Run without config discovery:

```sh
contained run claude --no-config
contained run claude --config ./path/to/contained.yaml
```

## Per-project Dockerfile overlay

Drop a `Dockerfile.contained` next to your `contained.yaml` to layer
project-specific tooling on top of the shared base image:

```dockerfile
FROM contained-base

USER root
RUN apt-get update && apt-get install -y --no-install-recommends \
      postgresql-client \
 && rm -rf /var/lib/apt/lists/*
USER agent
```

The first `FROM` **must** be `FROM contained-base` — `contained`
rewrites it to the resolved base image tag at build time. The
resulting overlay image is tagged with a hash of
`(agent, dockerfile-bytes, base-image-id)` and cached; it's rebuilt
only when one of those changes or you pass `--rebuild`.

## Networking and the allowlist

Three modes, selected with `--network` or `network:` in
`contained.yaml`:

| Mode | Behavior |
|------|----------|
| `allowlist` *(default)* | Traffic routed through a tinyproxy sidecar on a `--internal` bridge. Only hosts in the resolved allowlist reach the outside world. |
| `host` | No restrictions. The container shares the host's network namespace. Debugging / advanced. |
| `none` | No network at all. Offline-only runs. |

The **tool-wide default allowlist** covers the common coding agent
surface: `github.com`, `codeload.github.com`, `registry.npmjs.org`,
`pypi.org`, `files.pythonhosted.org`, `proxy.golang.org`, `crates.io`,
and a few others. Each agent profile adds its own:

- `claude` → `api.anthropic.com:443`, `platform.claude.com:443`,
  `api.openai.com:443`, `openrouter.ai:443`
- `pi` → `chatgpt.com:443`, `auth.openai.com:443` (OpenAI Codex
  subscription only — every other provider is intentionally
  unreachable)

Add more per-run or per-project:

```sh
contained run claude --allow staging.example.com:443
```

```yaml
defaults:
  allowlist:
    - my-internal-registry.corp:443
```

### How the allowlist is enforced

`contained` runs a small tinyproxy container alongside the agent:

```
[agent container] ── private --internal bridge ── [tinyproxy] ── default bridge ── internet
```

The agent container has no direct route to the outside — it can only
reach the proxy. `HTTP_PROXY` / `HTTPS_PROXY` env vars point agents at
`http://proxy:8888`. tinyproxy uses `FilterDefaultDeny Yes` with one
anchored regex per allowlisted host, matching both plain HTTP requests
and HTTPS `CONNECT` targets. No TLS termination.

Known limitations of the MVP approach:

- **Hostname-only** — no per-path rules. The decision point is "which
  host," not "which URL."
- **Agents that ignore `HTTPS_PROXY`** won't reach the network at all.
  That's intentional but will break tools until they pick up the env
  vars.
- **Git over SSH** — not supported. tinyproxy's CONNECT allowlist is
  HTTPS-only (port 443); SSH egress is an opt-in post-MVP follow-up.
  Use git over HTTPS.

For escape hatches, use `--network host` for a single run.

## State and credential forwarding

Every agent writes its session history, command history, and whatever
else it wants to the per-project state dir:

```
~/.local/share/contained/projects/<basename>-<hash>/
├── claude/    ← mounted at /home/agent/.claude inside the container
└── pi/        ← mounted at /home/agent/.pi inside the container
```

Two projects have different hashes → completely separate state. No
cross-contamination.

Credential forwarding is a **scoped writable copy**. On first run,
`contained` copies the minimum credential file from the host
(Claude: `~/.claude/.credentials.json` or the macOS keychain entry)
into a tool-wide global dir at
`~/.local/share/contained/global/claude/.credentials.json` and
bind-mounts it into every container. All `contained` runs share this
single credential, so OAuth token refreshes done inside one container
propagate to every other one — no relogin churn across projects or
parallel sessions. Your canonical host credentials are never exposed
to the container.

`ANTHROPIC_API_KEY`, `OPENAI_API_KEY`, and `CLAUDE_MODEL` are
forwarded from the host shell to `claude`. **Pi forwards no provider
API keys at all** — it's restricted to its OpenAI Codex subscription
(auth via `pi /login`), so leaving Anthropic / OpenAI / OpenRouter
keys out of pi's env is what disables those providers (pi
auto-selects a provider whenever the matching `*_API_KEY` is set).
For persistent, user-wide defaults — so you don't have to re-export
keys in every shell — drop a dotenv file at
`~/.local/share/contained/global/env`:

```sh
# ~/.local/share/contained/global/env
OPENAI_API_KEY=sk-...
ANTHROPIC_API_KEY=sk-ant-...
```

One `KEY=VALUE` per line, `#` comments allowed. Applied to every
`contained run` (loaded between the profile's built-in forwarding and
any `contained.yaml`, so per-project config still wins). Recommend
`chmod 600` on the file. Values are masked in every place `contained`
prints them.

Clean run, no persistence:

```sh
contained run claude --no-state
```

## Mount safety

`contained` refuses a few things outright:

- Mounting `/` as a source — always refused.
- Mounting `$HOME` — refused unless you pass `--allow-home-mount`.
- Nonexistent host paths — hard error, no silent mkdir.

And warns on:

- Read-write mounts that contain `.ssh`, `.aws`, or `.env` files.
  (Use `--mount-ro` instead if the agent only needs to read.)

The `--mount` / `--mount-ro` flags take the usual `HOST:CONTAINER`
form and are repeatable:

```sh
contained run claude \
  --mount ./fixtures:/workspace/fixtures \
  --mount-ro ~/.config/gh:/home/agent/.config/gh
```

## Security posture

Every container launched by `contained run` gets:

- `--rm` — no leftover containers on exit
- `--user 1000:1000` — non-root `agent` user
- `--cap-drop ALL` — no Linux capabilities
- `--security-opt no-new-privileges`
- `--init` — proper signal handling, reaped zombies
- Private internal network (no default-route egress, except the proxy)
- Scoped env var forwarding per profile
- Per-project isolated state

This is a reasonable default posture for untrusted code. It's not a
claim of defense against a sophisticated exploit targeting the Docker
daemon or the kernel itself — those remain the host's attack surface.

## CLI reference

```
contained run <agent> [options] [-- passthrough args for the agent]
contained build [--tag REF] [--rebuild]
contained doctor
contained version
```

### `contained run`

| Flag | Purpose |
|------|---------|
| `--dry-run` | Print resolved config without launching. |
| `--mount HOST:CONTAINER` | Bind-mount rw. Repeatable. |
| `--mount-ro HOST:CONTAINER` | Bind-mount ro. Repeatable. |
| `--env KEY[=VALUE]` | Forward an env var (value from host if omitted). Repeatable. Unset required vars fail fast. |
| `--env-from FILE` | Load env vars from a dotenv-style file. |
| `--network {allowlist,host,none}` | Network mode (default: `allowlist`). |
| `--allow HOST:PORT` | Add one host to the allowlist for this run. Repeatable. |
| `--image REF` | Override the base image. |
| `--rebuild` | Force rebuild of `Dockerfile.contained` overlay. |
| `--no-state` | Skip state dir mount — clean run, no persistence. |
| `--allow-home-mount` | Permit mounting `$HOME` (off by default). |
| `--clipboard-bridge` / `--no-clipboard-bridge` | Run a host-side daemon so the agent's Ctrl-V pastes images from your clipboard (default: on). |
| `--config PATH` | Use a specific `contained.yaml` instead of discovering. |
| `--no-config` | Ignore any discovered `contained.yaml`. |

Everything after `--` is passed through verbatim to the agent's
entrypoint.

### `contained build`

Builds the shared base image and the egress proxy image locally.
`--tag` only targets the base image; the proxy always uses its
canonical ref. `--rebuild` passes `--no-cache` to both.

### `contained doctor`

Diagnostics that run even when Docker isn't installed. Checks:

- docker binary in `PATH`
- docker daemon reachable
- state dir is writable
- known profiles
- tool-wide allowlist hosts are reachable from the host (TCP probe)

## Agent profiles

Both profiles run in the same container image — both CLIs are
installed in either case, both `~/.claude` and `~/.pi` state dirs are
bound, and both OAuth tokens are shared tool-wide (see credential
forwarding above). The entrypoint, forwarded env, and egress
allowlist differ per profile so each agent only reaches the providers
it's meant to.

- **`claude`** — Claude Code (`@anthropic-ai/claude-code`). Forwards
  `ANTHROPIC_API_KEY`, `OPENAI_API_KEY`, `CLAUDE_MODEL`. Runs with
  `--permission-mode bypassPermissions` by default (the sandbox
  already enforces the real boundary).
- **`pi`** — pi coding-agent (`@mariozechner/pi-coding-agent`).
  Locked to the OpenAI Codex (ChatGPT Plus/Pro) subscription: no
  provider API keys are forwarded, and the egress allowlist only
  permits `chatgpt.com:443` and `auth.openai.com:443`. First-run auth
  is `pi /login` (writes `~/.pi/agent/auth.json`); non-credential
  state (settings, session history) persists per project. Every other
  provider pi knows about — Anthropic, OpenRouter, the OpenAI
  API-key path, etc. — is silently unavailable by design.

Adding a third profile is a single file edit: define an
`AgentProfile` constant in `contained/profiles.py` and add it to the
`_PROFILES` registry. The CLI and runtime don't need to know about
it. Third-party profiles are an explicit post-MVP goal.

## Development

```sh
python3 -m venv .venv
source .venv/bin/activate
pip install -e '.[dev]'
pytest                      # 87 tests, ~2.5s
```

Project layout:

```
contained/
├── assets/
│   ├── Dockerfile.base      # shared base image
│   ├── Dockerfile.proxy     # tinyproxy sidecar
│   └── tinyproxy.conf
├── cli.py                   # argparse surface, dispatches to runtime
├── config.py                # contained.yaml loader
├── doctor.py                # environment diagnostics
├── profiles.py              # built-in agent profiles + registry
├── proxy.py                 # allowlist proxy lifecycle
├── run.py                   # config → ResolvedRun (merge, validate)
├── runtime.py               # docker orchestration (build_argv, run)
└── state.py                 # per-project state dir + credential seeding
```

### PRDs

Every significant chunk of work is planned in a PRD under `docs/prd/`,
organized as a kanban:

```
docs/prd/
├── todo/         # not started
├── in_progress/  # being worked on
└── done/         # shipped
```

Current status: PRDs 01–05 done, PRD 00 is the overview. See
`AGENTS.md` for the convention.

## Roadmap

Code-complete MVP items:

- [x] CLI surface and config loader (PRD 01)
- [x] Docker runtime, security posture, overlay builds (PRD 02)
- [x] Mounts, env forwarding, per-project state (PRD 03)
- [x] Networking + egress allowlist proxy (PRD 04)
- [x] Agent profiles + credential forwarding (PRD 05)

Before the "it works" sticker:

- [ ] Live end-to-end smoke test: `contained build && contained run
      claude`, clone a public GitHub repo, verify `curl example.com` is
      blocked.
- [ ] Multi-arch GHCR publish of `contained-base` and
      `contained-proxy`.

Post-MVP ideas: third-party profiles, keychain integration, per-URL
rules, SSH-capable egress, richer doctor checks.

## License

MIT.
