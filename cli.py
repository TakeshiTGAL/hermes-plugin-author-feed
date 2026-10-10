"""Local CLI: hermes author-feed schedule|unschedule|status."""
from __future__ import annotations

import argparse

if __package__:
    from .service import Deps, schedule, status, unschedule
else:
    from service import Deps, schedule, status, unschedule


def setup_parser(parser: argparse.ArgumentParser) -> None:
    sub = parser.add_subparsers(dest="command", required=True)
    sch = sub.add_parser("schedule", help="Save one account and one cron job")
    sch.add_argument("--actor", required=True, help="Handle or DID to watch")
    sch.add_argument("--deliver", default="local", help="Cron deliver target. The default is local, and local does not send to chat.")
    sch.add_argument("--every", default=None, help="Repeat interval. Omit this when rescheduling to keep the current interval.")
    sub.add_parser("unschedule", help="Remove the cron job and keep the saved file")
    sub.add_parser("status", help="Show the saved file and whether the job still exists. Does not fetch.")


def handle(args: argparse.Namespace) -> int:
    deps = Deps()
    command = getattr(args, "command", None)
    if command == "schedule":
        code, text = schedule(deps, args.actor, args.deliver, args.every)
    elif command == "unschedule":
        code, text = unschedule(deps)
    elif command == "status":
        code, text = status(deps)
    else:
        print("Use schedule, unschedule, or status.")
        return 1
    print(text)
    return code
