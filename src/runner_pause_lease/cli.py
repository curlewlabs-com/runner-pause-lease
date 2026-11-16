"""The `runner-pause-lease` command line."""

from __future__ import annotations

import argparse
import os
import sys
from collections.abc import Callable
from datetime import UTC, datetime

from .labels import Fleet
from .pause import (
    QUIET_STATUS,
    command_pause,
    command_reconcile,
    command_resume,
    command_status,
)

Handler = Callable[[Fleet, argparse.Namespace, datetime], int]


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="runner-pause-lease",
        description=(
            "Pause a self-hosted runner host for a bounded window; "
            "a scheduled reconcile puts it back."
        ),
    )
    parser.add_argument(
        "--repo",
        default=os.environ.get("GITHUB_REPOSITORY"),
        help="OWNER/NAME whose runners and issues to use (default: $GITHUB_REPOSITORY)",
    )
    parser.add_argument(
        "--host-label-prefix",
        required=True,
        help="a host is every runner carrying the label <prefix><host>; "
        "these labels are never stripped",
    )
    parser.add_argument(
        "--keep-label-prefix",
        action="append",
        default=[],
        help="also keep labels starting with this on a pause (repeatable)",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    hosts_help = "hosts to act on; accepts several, and the flag may repeat"
    pause = subparsers.add_parser("pause", help="stop routing jobs to hosts")
    pause.add_argument(
        "--host", required=True, nargs="+", action="extend", help=hosts_help
    )
    pause.set_defaults(handler=_pause)

    resume = subparsers.add_parser("resume", help="restore routing to hosts now")
    resume.add_argument(
        "--host", required=True, nargs="+", action="extend", help=hosts_help
    )
    resume.set_defaults(handler=_resume)

    status = subparsers.add_parser(
        "status", help=f"exit {QUIET_STATUS} if a host is inside a live window"
    )
    status.add_argument("--host", required=True)
    status.set_defaults(handler=_status)

    reconcile = subparsers.add_parser(
        "reconcile", help="enforce active leases and restore ended ones"
    )
    reconcile.set_defaults(handler=_reconcile)
    return parser


def _pause(fleet: Fleet, arguments: argparse.Namespace, now: datetime) -> int:
    return command_pause(fleet, arguments.host, now)


def _resume(fleet: Fleet, arguments: argparse.Namespace, now: datetime) -> int:
    return command_resume(fleet, arguments.host, now)


def _status(fleet: Fleet, arguments: argparse.Namespace, now: datetime) -> int:
    return command_status(fleet, arguments.host, now)


def _reconcile(fleet: Fleet, arguments: argparse.Namespace, now: datetime) -> int:
    return command_reconcile(fleet, now)


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    arguments = parser.parse_args(argv)
    if not arguments.repo:
        parser.error("--repo is required outside GitHub Actions")
    if not arguments.host_label_prefix:
        parser.error("--host-label-prefix must not be empty")
    fleet = Fleet(
        repo=arguments.repo,
        host_label_prefix=arguments.host_label_prefix,
        keep_label_prefixes=tuple(arguments.keep_label_prefix),
        # The runner endpoints need repo Administration, which a workflow
        # token cannot be granted; the issue calls stay on the ambient login.
        admin_token=os.environ.get("RUNNER_ADMIN_TOKEN") or None,
    )
    handler: Handler = arguments.handler
    return handler(fleet, arguments, datetime.now(UTC))


if __name__ == "__main__":
    sys.exit(main())
