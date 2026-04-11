# PRD — Git over SSH

## Problem

Today the contained agent can only reach git remotes over HTTPS. PRD
04 (Networking) documents git-over-HTTPS as the supported path, and
PRD 06 (Tier-1 shipblockers) closed a hole where `ConnectPort 22` in
`contained/assets/tinyproxy.conf` let any allowlisted host be used as
an arbitrary SSH tunnel. The fix was to drop port 22 from the CONNECT
allowlist entirely.

That leaves a real gap for users whose repositories are only reachable
over SSH — internal git hosts, `git@github.com:…` remotes baked into
existing checkouts, deploy keys, and the muscle memory of `git push`
to an `origin` URL that starts with `git@`. Rewriting remotes to HTTPS
is workable for casual use but breaks down as soon as a project has an
`.gitmodules` file, a pre-existing worktree, or CI hooks that expect
SSH URLs.

We want SSH egress back, without re-introducing the tunneling hole.

## Goal

Let a user in a `contained run` session clone, fetch, and push to a
small, user-declared set of git hosts over SSH, using the credentials
they already use on the host, without:

- mounting `~/.ssh` read-write into the container,
- re-enabling unrestricted `CONNECT :22` on the egress proxy, or
- exposing the agent to arbitrary outbound TCP on port 22.

Out of scope:

- Generic outbound SSH for non-git purposes (rsync over ssh, remote
  shells, `ssh user@prod`). The supported surface is "git talks to
  known git hosts." Anything else stays a follow-up.
- SCP / SFTP. Same reasoning.
- Writing new SSH keys from inside the container. Keys live on the
  host.

## Why the existing proxy can't just allow port 22

Tinyproxy's `Filter` directive matches on hostname only — it has no
way to tie an allowed host to a specific `ConnectPort`. Adding port
22 to the global `ConnectPort` list means *every* host in the
allowlist becomes reachable on :22. PRD 06 walked through the
consequence: `github.com` is in the default allowlist, so an agent
(or a prompt-injected payload) could `CONNECT github.com:22` and
run arbitrary SSH through the tunnel. Combined with the `~/.config/gh`
read-only mount in the README example, that's a plausible exfil path.

Port-aware allowlisting is the missing primitive. The cleanest way
to add it without replacing tinyproxy is to run a **second**
tinyproxy instance dedicated to `CONNECT :22`, with its own filter
file containing only the SSH-allowlisted hosts. Two small containers,
one responsibility each, no changes to the HTTPS path.

## UX

### Declaring SSH hosts

SSH hosts are opt-in and declared separately from the HTTPS allowlist,
because the threat model is different (port 22 is a fully authenticated
channel and its allowlist should stay short).

In `contained.yaml`:

```yaml
ssh:
  allowlist:
    - github.com
    - git.internal.corp
```

On the CLI:

```
contained run claude --allow-ssh github.com --allow-ssh git.internal.corp
```

`--allow-ssh` is additive, same merge semantics as `--allow`. A bare
hostname is assumed to mean port 22; the grammar accepts
`host[:port]` but anything other than `:22` is rejected with a clear
error ("Git over SSH only supports port 22; use --allow for other
ports"). No implicit defaults: if the list is empty, no SSH sidecar
is started and the behavior is identical to today.

`contained run --dry-run` prints the resolved SSH allowlist alongside
the HTTPS one.

### Credential forwarding

The agent needs to authenticate as the user without giving the
container a copy of the private key. Two supported modes, in
preference order:

1. **SSH-agent forwarding (default).** If `SSH_AUTH_SOCK` is set on
   the host, `contained` bind-mounts the socket into the container
   at a fixed path and sets `SSH_AUTH_SOCK` in the agent's
   environment. This is the same pattern `docker run -v
   $SSH_AUTH_SOCK:/ssh-agent` users already know, and it keeps the
   key material in the host's agent process. On macOS, where the
   default agent socket lives in a launchd-managed path, we
   document the `ssh-add --apple-use-keychain` dance in the README
   but don't try to paper over it.
2. **Explicit read-only key mount.** `--ssh-key <path>` mounts a
   single private key file (plus its `.pub` if present) read-only
   into the container and writes a minimal `~/.ssh/config` that
   references it. This exists for users who don't run an agent
   (CI-like setups, minimal Linux boxes). It is **not** the
   default.

Mounting `~/.ssh` wholesale is still refused the same way today's
sensitive-dir warning handles it (`contained/run.py:315`,
`_SENSITIVE_DIR_HINTS`). The warning becomes a hard error for
`.ssh` specifically when `--allow-ssh` is in play, with a hint
pointing at agent forwarding or `--ssh-key`.

### `known_hosts`

We don't want `StrictHostKeyChecking=no` — that would let a
compromised DNS or a misconfigured network silently MITM a push. We
also don't want to ship a vendored `known_hosts` that rots over
time.

The approach: at `contained run` startup, for each host in the
resolved SSH allowlist, run `ssh-keyscan -T 5 <host>` from the host
(not the container) and write the results to a per-run
`known_hosts` file that gets bind-mounted read-only into the
agent's `~/.ssh/known_hosts`. The scan happens in the same phase as
`doctor`'s reachability probe, before the agent container is
started, so a failure surfaces as a pre-launch error with a
copy-pasteable hint. Users who want to pin keys manually can drop a
`known_hosts` file in their `contained.yaml` state dir; if present,
that wins over the scan.

This is not cryptographically stronger than trust-on-first-use, but
it moves the TOFU moment to the host where the user's existing
network posture applies, rather than doing it inside a restricted
container whose DNS is routed through our own sidecar.

## Design

### Topology

Two proxy containers per run:

- `contained-proxy-<run_id>` — HTTPS, unchanged. `ConnectPort 443`,
  filter = full HTTPS allowlist. Agent uses it via `HTTPS_PROXY`.
- `contained-proxy-ssh-<run_id>` — SSH, new. `ConnectPort 22`,
  filter = SSH allowlist only. Agent uses it via an `ssh_config`
  `ProxyCommand` that speaks HTTP CONNECT.

Both join the same `contained-net-<run_id>` internal bridge, under
distinct DNS aliases (`proxy`, `proxy-ssh`). Only the SSH proxy is
started when the resolved SSH allowlist is non-empty; empty list =
no sidecar, no ssh_config changes, no behavior change.

### The SSH proxy container

Reuses the existing `Dockerfile.proxy` image — same tinyproxy
binary, different config file and filter file at runtime. The
config is a near-clone of `tinyproxy.conf` with two deltas:

- `Port 8889` (distinct from the HTTPS proxy's 8888, so the two
  can share a network without colliding).
- `ConnectPort 22` (replacing 443).

The filter file is generated by the same `proxy.write_filter_file`
code path, taking a different host list. That keeps the anchored-
regex format consistent and lets `contained allow --list` extend
naturally (see Deferred).

### Agent-side `ssh_config`

The runtime writes a per-run `ssh_config` to the state dir and
bind-mounts it at `/home/agent/.ssh/config` read-only. Contents:

```
Host github.com git.internal.corp
    ProxyCommand /usr/bin/nc -X connect -x proxy-ssh:8889 %h %p
    StrictHostKeyChecking yes
    UserKnownHostsFile /home/agent/.ssh/known_hosts
    IdentityAgent ${SSH_AUTH_SOCK}
```

`nc -X connect` (BSD netcat, already present on the debian-slim base
via the `ncat`/`netcat-openbsd` package — add to
`Dockerfile.base:19`) speaks HTTP CONNECT to the SSH proxy. An
alternative is `socat`, which is more flexible but pulls in a larger
dependency; `nc` is sufficient and the grammar is stable.

`Host` is emitted as the exact list of allowlisted hostnames
(glob-free), so a typo'd destination cleanly fails host-matching and
gets the default `ssh` behavior (which is blocked by the proxy,
producing a visible error).

### Why two tinyproxies instead of one

- One filter file per tinyproxy means port-awareness falls out for
  free: the filter list **is** the SSH allowlist, no cross-contamination
  with the HTTPS list.
- Blast radius of a mistake is confined: a bug in the SSH filter can't
  accidentally open up HTTPS, and vice versa.
- Teardown and live-update stay trivial — `proxy.stop` grows a second
  `docker rm -f` call, `contained allow` gains a parallel
  `--ssh` mode that targets the SSH filter file.
- Memory cost is ~5 MB per tinyproxy, which is in the noise next to
  the agent container.

An earlier sketch had a single tinyproxy with `ConnectPort 22 443`
plus a cleverer filter pipeline. It doesn't work: tinyproxy matches
hostname without knowing which port the CONNECT is targeting, so
the same filter entry covers both ports, which is exactly the PRD 06
hole.

### Teardown

`proxy.stop` already wraps best-effort cleanup of one container,
one network, and one filter file. It grows to handle an optional
second container and second filter file. Both cleanups run
unconditionally in the `try/finally`, neither raises, and the
network teardown waits on both proxies having been removed.

### `doctor` and `--dry-run`

- `contained doctor` gains a second reachability loop: for each SSH
  allowlist entry, do a 2s TCP-connect probe to `host:22` from the
  host network. Failure is a warning, same as the HTTPS probe.
- `contained run --dry-run` prints the resolved SSH allowlist and
  the effective `ssh_config` it would mount.

## Acceptance criteria

- [ ] `contained run claude --allow-ssh github.com` from a project
      with an existing `git@github.com:…` remote can `git fetch` and
      `git push` successfully, using the host's running ssh-agent.
- [ ] With the same invocation, `ssh git@gitlab.com` from inside the
      container fails with a clear "host not in SSH allowlist" error
      (the SSH proxy's CONNECT is rejected) and does **not** silently
      fall back to direct egress.
- [ ] With no `--allow-ssh` entries and no `ssh:` key in
      `contained.yaml`, behavior is identical to today: no SSH
      sidecar, no ssh_config mount, no warnings.
- [ ] `--ssh-key <path>` mounts a single key read-only and the agent
      can use it without `SSH_AUTH_SOCK` being set on the host.
- [ ] Passing `--mount ~/.ssh:/home/agent/.ssh` alongside
      `--allow-ssh` is refused with a hard error (not just a warning)
      pointing at agent forwarding and `--ssh-key`.
- [ ] `contained doctor` reports per-host SSH reachability alongside
      HTTPS.
- [ ] `contained run --dry-run` prints the resolved SSH allowlist and
      the effective `ssh_config` the agent would see.
- [ ] The SSH proxy container is removed, its network alias released,
      and its filter file unlinked on normal exit, Ctrl-C, and crash.
- [ ] `--allow-ssh host:443` (or any non-22 port) is rejected with a
      hint pointing at `--allow`.
- [ ] Re-enabling `ConnectPort 22` on the main HTTPS proxy is
      explicitly **not** part of this change — a regression test
      guards `contained/assets/tinyproxy.conf` to keep
      `ConnectPort 443` as the sole entry.

## Implementation notes

- **Config schema** — extend `contained.yaml` with `ssh.allowlist: [str]`,
  merging under the same precedence rules as `allowlist:`
  (`CliOverrides.allow_ssh` unions on top of config on top of
  profile-contributed defaults; profiles contribute nothing by
  default).
- **Proxy plumbing** — `contained/proxy.py` grows `start_ssh(...)`
  next to `start(...)`. Most of the body is the same; extract the
  shared pieces into a private helper taking
  `(port, connect_port, filter_path)`. The filter-file format is
  identical; `write_filter_file` takes a list and doesn't care what
  the list represents.
- **Labels** — mirror the PRD 08 convention: the SSH proxy
  container is labeled `contained.run_id=<id>`,
  `contained.project=<abs path>`, `contained.role=ssh`, and
  `contained.filter_path=<host path>`. `contained allow --list`
  gains a `--ssh` mode that targets the role=ssh container.
- **Base image** — add `netcat-openbsd` to the `apt-get install`
  line in `contained/assets/Dockerfile.base:19` so `nc -X connect`
  is present for the `ProxyCommand`. Pin the package the same way
  existing entries are pinned.
- **Agent ssh_config** — generated by `contained/run.py`, written
  to the per-run state dir, bind-mounted at
  `/home/agent/.ssh/config:ro`. The matching `known_hosts` file
  lives in the same dir and is bind-mounted read-only.
- **Sensitive-dir handling** — `_SENSITIVE_DIR_HINTS` in
  `contained/run.py:315` already flags `.ssh`. Extend the resolver
  so that when `allow_ssh` is non-empty, a user-supplied mount
  containing `.ssh` is refused (not warned) with the error text
  calling out the two supported credential modes.
- **Teardown ordering** — `runtime.run`'s `try/finally` calls
  `proxy.stop(session)` today; it grows to accept an optional
  `ssh_session` and tear down both before the network. Filter
  files are unlinked after the containers are gone.
- **Tests** — unit tests for `start_ssh` argv shape, filter-file
  round-tripping, `ssh_config` generation (exact text), sensitive-
  dir refusal when `--allow-ssh` is set, and `--dry-run` output.
  A manual smoke test clones a public GitHub repo over SSH; an
  automated integration test would need a reachable SSH server and
  is deferred.

## Open questions

- **macOS ssh-agent socket.** `SSH_AUTH_SOCK` on macOS points at a
  launchd-managed path (`/private/tmp/com.apple.launchd.*/Listeners`).
  Docker Desktop can bind-mount it, but the UX around
  `ssh-add --apple-use-keychain` and the keychain prompt for each
  key is rough. Worth confirming during implementation whether we
  need a README section or a one-time `contained doctor` check
  that prods the user to run `ssh-add` first.
- **IPv6-only SSH hosts.** `ssh-keyscan` and the TCP probe both
  need to resolve under the host's DNS; if an internal SSH host
  is IPv6-only and the host's Docker network is v4-only, the
  scan may work while the proxy-to-host leg fails. Cheap to
  detect in `doctor` but worth flagging.
- **`git:` protocol.** `git://host/repo` (TCP port 9418, no auth)
  is extinct in practice but the proxy would block it by default.
  No plan to support it; called out so the error message can be
  specific.

## Deferred

- **Generic outbound SSH.** `ssh user@prod` for non-git purposes
  stays blocked. If a real user asks for it, the mechanism here
  (SSH proxy + per-host allowlist) generalizes — it's a UX and
  threat-model question, not a plumbing one.
- **Live SSH-allowlist updates.** `contained allow --ssh host` is a
  natural extension of PRD 08's live-update story, using the
  `contained.role=ssh` label to pick the right container. Ship the
  base path first and add live updates when someone asks.
- **Per-profile SSH allowlists.** Today agent profiles contribute
  HTTPS allowlist entries (`05-agents-and-auth.md`); they do not
  contribute SSH entries. If a profile like `pi` grows an SSH
  dependency, revisit.
- **Host-key pinning in `contained.yaml`.** A `ssh.known_hosts:` key
  that lets users commit pinned fingerprints to the project repo
  would be stricter than live `ssh-keyscan`, at the cost of
  maintenance. Worth doing once someone hits a MITM concern in
  practice.
- **Persisting `ssh:` additions.** Same reasoning as PRD 08's
  deferred `--save` flag: auto-editing `contained.yaml` is its own
  discussion.
