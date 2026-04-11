"""contained — CLI entry point.

Defines the argparse surface from PRD 01 and dispatches to handlers.
Actual container execution is deferred to PRD 02; for now, `run` only
supports `--dry-run`.
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

from . import __version__, doctor, profiles, proxy, runtime
from .config import ConfigError, discover, load
from .run import (
    CliOverrides,
    check_required_host_env,
    render_dry_run,
    resolve,
    validate_allowlist_entry,
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="contained",
        description="Run AI coding agents inside isolated Docker containers.",
    )
    parser.add_argument("--version", action="version", version=f"contained {__version__}")

    sub = parser.add_subparsers(dest="command", metavar="<command>")

    run_p = sub.add_parser(
        "run",
        help="launch an agent inside a container",
        description="Launch an agent inside a container.",
    )
    run_p.add_argument(
        "agent",
        nargs="?",
        help=f"agent profile ({', '.join(profiles.names())}); "
        "optional if default_agent is set in contained.yaml",
    )
    run_p.add_argument(
        "--mount", action="append", default=[], metavar="HOST:CONTAINER",
        help="bind-mount a host directory read-write (repeatable)",
    )
    run_p.add_argument(
        "--mount-ro", action="append", default=[], metavar="HOST:CONTAINER",
        help="bind-mount read-only (repeatable)",
    )
    run_p.add_argument(
        "--env", action="append", default=[], metavar="KEY[=VALUE]",
        help="forward an env var (value from host if omitted, repeatable)",
    )
    run_p.add_argument(
        "--env-from", action="append", default=[], metavar="FILE", type=Path,
        help="load env vars from a dotenv-style file (repeatable)",
    )
    run_p.add_argument(
        "--network", choices=["host", "none", "allowlist"],
        help="network policy (default: allowlist)",
    )
    run_p.add_argument(
        "--allow", action="append", default=[], metavar="HOST[:PORT]",
        help="add to the egress allowlist for this run (repeatable)",
    )
    run_p.add_argument("--image", metavar="REF", help="override the base image")
    run_p.add_argument(
        "--rebuild", action="store_true",
        help="force rebuild of Dockerfile.contained overlay if present",
    )
    run_p.add_argument(
        "--no-state", action="store_true",
        help="do not mount per-project state dir (clean run, no persistence)",
    )
    run_p.add_argument(
        "--allow-home-mount", action="store_true",
        help="permit mounting the user's home directory as a mount source",
    )
    run_p.add_argument(
        "--dry-run", action="store_true",
        help="print resolved config without running anything",
    )
    run_p.add_argument(
        "--config", type=Path, metavar="PATH",
        help="use a specific contained.yaml instead of discovering one",
    )
    run_p.add_argument(
        "--no-config", action="store_true",
        help="ignore any discovered contained.yaml",
    )

    build_p = sub.add_parser(
        "build",
        help="build the shared base image locally",
        description="Build the bundled Dockerfile.base and tag it as the "
        "default agent base image (or --tag).",
    )
    build_p.add_argument(
        "--tag", metavar="REF",
        help=f"tag for the built image (default: {profiles.BASE_IMAGE})",
    )
    build_p.add_argument(
        "--rebuild", action="store_true",
        help="pass --no-cache to docker build",
    )

    allow_p = sub.add_parser(
        "allow",
        help="add hosts to the allowlist of a running session",
        description="Add hosts to the egress allowlist of a running "
        "`contained run` session without restarting it.",
    )
    allow_p.add_argument(
        "hosts", nargs="*", metavar="HOST[:PORT]",
        help="one or more hosts to add (same grammar as `run --allow`)",
    )
    allow_p.add_argument(
        "--run", dest="run_id", metavar="ID",
        help="target a specific run id (required if multiple sessions are active)",
    )
    allow_p.add_argument(
        "--list", action="store_true",
        help="print the current allowlist for the targeted run and exit",
    )

    sub.add_parser("doctor", help="diagnose environment readiness")
    sub.add_parser("version", help="print version")

    return parser


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)

    # Split off passthrough args after `--` so argparse doesn't choke on them.
    passthrough: list[str] = []
    if "--" in argv:
        idx = argv.index("--")
        passthrough = argv[idx + 1 :]
        argv = argv[:idx]

    parser = build_parser()
    args = parser.parse_args(argv)

    if args.command is None:
        parser.print_help()
        return 0
    if args.command == "version":
        print(f"contained {__version__}")
        return 0
    if args.command == "doctor":
        print(doctor.format_report(doctor.run_checks()))
        return 0
    if args.command == "run":
        return _cmd_run(args, passthrough)
    if args.command == "build":
        return _cmd_build(args)
    if args.command == "allow":
        return _cmd_allow(args)

    parser.error(f"unknown command: {args.command}")
    return 2  # unreachable


def _cmd_run(args: argparse.Namespace, passthrough: list[str]) -> int:
    cwd = Path.cwd()
    try:
        if args.no_config and args.config:
            raise ConfigError("--no-config and --config are mutually exclusive")
        if args.no_config:
            loaded = load(None, cwd=cwd)
        elif args.config:
            loaded = load(args.config, cwd=cwd)
        else:
            loaded = load(discover(cwd), cwd=cwd)

        overrides = CliOverrides(
            mounts=args.mount,
            mounts_ro=args.mount_ro,
            env=args.env,
            env_from=args.env_from,
            network=args.network,
            allow=args.allow,
            image=args.image,
            passthrough=passthrough,
            rebuild=args.rebuild,
            no_state=args.no_state,
            allow_home_mount=args.allow_home_mount,
        )
        resolved = resolve(args.agent, loaded, overrides, cwd=cwd)
    except ConfigError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2

    host_env = dict(os.environ)
    missing = check_required_host_env(resolved, host_env)
    if missing is not None:
        print(f"error: {missing}", file=sys.stderr)
        return 2

    if args.dry_run:
        print(render_dry_run(resolved, host_env))
        return 0

    for w in resolved.warnings:
        print(w, file=sys.stderr)

    if resolved.network == "allowlist":
        print(
            f"contained: egress via allowlist proxy "
            f"({len(resolved.allowlist)} hosts)",
            file=sys.stderr,
        )
        print(
            "contained: if the agent reports a blocked host, add it with "
            "`--allow <host>:<port>` or in contained.yaml",
            file=sys.stderr,
        )

    try:
        return runtime.run(resolved, cwd)
    except runtime.DockerError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1


def _cmd_build(args: argparse.Namespace) -> int:
    try:
        base_tag = runtime.build_base(args.tag, rebuild=args.rebuild)
        # The `--tag` override only targets the base image; the proxy
        # image always uses its canonical ref so `contained run` finds
        # it without extra plumbing.
        proxy_tag = runtime.build_proxy(rebuild=args.rebuild)
    except runtime.DockerError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    print(f"built {base_tag}")
    print(f"built {proxy_tag}")
    return 0


def _cmd_allow(args: argparse.Namespace) -> int:
    if args.list and args.hosts:
        print(
            "error: --list is mutually exclusive with host arguments",
            file=sys.stderr,
        )
        return 2
    if not args.list and not args.hosts:
        print(
            "error: specify one or more hosts, or pass --list",
            file=sys.stderr,
        )
        return 2

    # Validate host syntax up front so we fail before touching docker.
    for entry in args.hosts:
        try:
            validate_allowlist_entry(entry)
        except ConfigError as e:
            print(f"error: {e}", file=sys.stderr)
            return 2

    try:
        runs = proxy.discover_runs()
    except proxy.ProxyError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1

    if args.run_id:
        matches = [r for r in runs if r.run_id == args.run_id]
        if not matches:
            print(
                f"error: no running session with run_id={args.run_id}",
                file=sys.stderr,
            )
            return 1
        target = matches[0]
    elif not runs:
        print(
            "error: no running contained sessions found. "
            "start one with `contained run <agent>` first.",
            file=sys.stderr,
        )
        return 1
    elif len(runs) > 1:
        print(
            f"error: {len(runs)} running sessions; pass --run <id>:",
            file=sys.stderr,
        )
        for r in runs:
            print(
                f"  {r.run_id}  {r.project or '(unknown project)'}",
                file=sys.stderr,
            )
        return 1
    else:
        target = runs[0]

    if args.list:
        try:
            hosts = proxy.read_filter_hosts(target.filter_path)
        except OSError as e:
            print(f"error: cannot read filter file: {e}", file=sys.stderr)
            return 1
        print(f"run {target.run_id} — {len(hosts)} entries:")
        for h in hosts:
            print(f"  {h}")
        return 0

    try:
        all_hosts, added = proxy.append_to_filter(target.filter_path, args.hosts)
    except (proxy.ProxyError, OSError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 1

    if not added:
        print(
            f"contained: no new hosts to add "
            f"(all {len(all_hosts)} already allowed)"
        )
        return 0

    try:
        proxy.reload(target.container)
    except proxy.ProxyError as e:
        print(
            f"error: rewrote filter file but failed to reload proxy: {e}",
            file=sys.stderr,
        )
        return 1

    before = len(all_hosts) - len(added)
    print(
        f"contained: added {', '.join(added)} to run {target.run_id} "
        f"({before} → {len(all_hosts)} entries)"
    )

    # Advisory host-side reachability probe. The proxy runs inside docker
    # so a failed probe here doesn't prove the agent can't reach the
    # host — it just tells the user their own network path is suspect.
    import socket
    for entry in args.hosts:
        host, _, port_s = entry.partition(":")
        try:
            port = int(port_s) if port_s else 443
        except ValueError:
            continue
        try:
            with socket.create_connection((host, port), timeout=2):
                print(f"  {entry}: reachable")
        except OSError as e:
            print(f"  {entry}: warning: not reachable from host ({e})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
