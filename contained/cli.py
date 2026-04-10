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

from . import __version__, doctor, profiles, runtime
from .config import ConfigError, discover, load
from .run import CliOverrides, render_dry_run, resolve


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

    if args.dry_run:
        print(render_dry_run(resolved, dict(os.environ)))
        return 0

    for w in resolved.warnings:
        print(w, file=sys.stderr)

    for spec in args.env:
        if "=" not in spec and os.environ.get(spec) is None:
            print(
                f"error: --env {spec}: required but not set in host environment",
                file=sys.stderr,
            )
            return 2

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
    except runtime.RuntimeError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1


def _cmd_build(args: argparse.Namespace) -> int:
    try:
        base_tag = runtime.build_base(args.tag, rebuild=args.rebuild)
        # The `--tag` override only targets the base image; the proxy
        # image always uses its canonical ref so `contained run` finds
        # it without extra plumbing.
        proxy_tag = runtime.build_proxy(rebuild=args.rebuild)
    except runtime.RuntimeError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    print(f"built {base_tag}")
    print(f"built {proxy_tag}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
