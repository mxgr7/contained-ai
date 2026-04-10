# contained-ai

A thin, opinionated wrapper around Docker for running AI coding agents
(Claude Code, `pi`) in isolated containers — with ergonomic mounts, env
forwarding, egress allowlisting, and per-project state persistence.

Status: **early development.** See `docs/prd/` for the MVP plan and
`AGENTS.md` for project orientation.

## Install (from source)

```sh
pip install -e '.[dev]'
```

## Usage (preview)

```sh
contained run claude
contained run pi
contained run claude --dry-run        # print resolved config, don't start
contained doctor
```

See `docs/prd/in_progress/01-cli-and-config.md` for the full CLI surface.
