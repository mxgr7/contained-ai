# PRD — Networking & egress allowlist

## Goal

Give the containerized agent enough network access to be useful —
reaching its LLM provider, package registries, git remotes — without
granting unrestricted egress to the host's network.

## Why an allowlist?

Unrestricted egress is the most common vector for "the agent did
something unexpected" surprises: exfiltrating files, fetching arbitrary
payloads, hitting paid APIs the user didn't intend. A default allowlist
covers 95% of real agent workflows and forces everything else to be
explicit.

We chose allowlisting over a blocklist because:

- Blocklists are impossible to maintain against a determined or buggy
  agent.
- The set of hosts a coding agent legitimately needs is small and
  well-known.
- Making egress explicit makes it reviewable — the project's
  `contained.yaml` becomes a document of "what this agent is allowed to
  touch."

## Network policy modes

`contained run` supports three modes via `--network`:

| Mode | Behavior |
|---|---|
| `allowlist` *(default)* | Only the hosts in the resolved allowlist are reachable. DNS is restricted to resolving those names. Everything else fails with a connection-refused-like error. |
| `host` | No restrictions. The container can reach anything the host can. For debugging and advanced users. |
| `none` | No network at all. Useful for offline work or for running an agent against a purely local codebase with no LLM calls (rare, but supported). |

Mode is set via `--network` or the `network:` key in `contained.yaml`.
Flag wins over config.

## The default allowlist

Every agent profile contributes its own allowlist entries. The tool-wide
defaults (applied regardless of agent) are:

- `github.com:443`
- `codeload.github.com:443`
- `objects.githubusercontent.com:443`
- `raw.githubusercontent.com:443`
- `registry.npmjs.org:443`
- `pypi.org:443`
- `files.pythonhosted.org:443`
- `proxy.golang.org:443`
- `sum.golang.org:443`
- `crates.io:443`
- `static.crates.io:443`

Plus DNS to a fixed resolver (e.g. `1.1.1.1:53`, `8.8.8.8:53`) — or a
configurable one.

Agent profile contributions (see `05-agents-and-auth.md`):

- `claude`: `api.anthropic.com:443`, plus whatever endpoints Claude Code
  uses for OAuth / updates.
- `pi`: the LLM endpoints the `pi` coding-agent calls (to be confirmed
  during profile implementation).

Project additions go in `contained.yaml`:

```yaml
defaults:
  allowlist:
    - my-internal-registry.corp:443
    - sentry.io:443
```

And one-off additions via CLI:

```
contained run claude --allow staging.internal.example.com:443
```

## Implementation strategy

Docker's built-in network controls aren't expressive enough for a hostname
allowlist (bridge networks give you "all or nothing," and iptables rules
on host are platform-specific and don't work on Docker Desktop's VM). We
need a small, portable egress proxy.

**MVP approach: sidecar HTTP(S)-aware proxy.**

- `contained` starts a small proxy container (e.g. a tiny Go or Python
  program, or a thin wrapper around `tinyproxy` / `mitmproxy` /
  `squid` — implementation detail) on a private Docker network shared
  with the agent container.
- The proxy is configured with the resolved allowlist.
- The agent container is given the proxy as `HTTP_PROXY`,
  `HTTPS_PROXY`, `http_proxy`, `https_proxy`, and `NO_PROXY` is set to
  cover internal container traffic.
- The agent container has no direct route to the outside world (custom
  bridge network with no default route, or with an egress netfilter rule
  — Linux-side; on Docker Desktop the VM handles isolation).
- DNS inside the container is pointed at the proxy or a stub that only
  resolves allowlisted names.

### Known limitations of this approach (MVP-acceptable)

- **HTTPS SNI-based allowlisting only** — we allow based on the
  hostname in the TLS ClientHello / HTTP CONNECT, without terminating
  TLS. This means we can't distinguish paths, but that's fine: the
  decision point is "which host," not "which URL."
- **Non-HTTP protocols** (raw sockets, custom TCP, SSH over 22) are
  **not proxyable** by an HTTP proxy. For SSH specifically (git over SSH),
  we either:
  - add TCP-level allowlisting for port 22 to known hosts, or
  - document that git-over-HTTPS is the supported path in MVP and
    defer git-over-SSH to a follow-up.
- Agents that ignore the `HTTPS_PROXY` env var will be unable to reach
  the network at all. That's a feature (it forces them to go through
  the proxy), but it will break some tools until they learn about the
  proxy. This is known and acceptable.

A Phase-4 spike will confirm which specific proxy implementation we use
and whether the SSH limitation is a blocker in practice.

## User-facing behavior

- Network errors from the agent should surface the allowlist context:
  "`curl https://example.com` failed because `example.com` is not in the
  egress allowlist. Add it with `--allow example.com:443` or in
  `contained.yaml`."
- `contained doctor` performs a reachability check against each entry in
  the resolved allowlist and reports the result.
- `contained run --dry-run` prints the full resolved allowlist before
  starting.

## Acceptance criteria (MVP)

- [ ] `contained run claude` with the default allowlist can reach
      `api.anthropic.com` and clone a public GitHub repo, but cannot
      reach an arbitrary host like `example.com`.
- [ ] `--allow example.com:443` adds an entry for the single run.
- [ ] `allowlist:` entries in `contained.yaml` are honored.
- [ ] `--network host` disables all restrictions.
- [ ] `--network none` denies all traffic.
- [ ] `contained doctor` reports reachability for each allowlist entry.
- [ ] Denied-host errors include a copy-pasteable hint on how to allow
      the host.
