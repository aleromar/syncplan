"""Command-line entry point for syncplan."""

import argparse
import sys
from importlib.metadata import version

from syncplan import init


def main(argv: list[str] | None = None) -> int:
    # A redirected report keeps names like 写真 (design D11). A broken character, which init
    # refuses (R3.8), is shown as \udc80 instead of crashing the message that names it.
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="backslashreplace")
    parser = argparse.ArgumentParser(
        prog="syncplan",
        description="Back up folders to a backup drive from a plan you review first.",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {version('syncplan')}")
    commands = parser.add_subparsers(dest="command", metavar="COMMAND")
    init_parser = commands.add_parser(
        "init",
        help="make a folder a backup of the given source folders",
        description="Make an existing folder (or drive, like E:) a syncplan backup of the given"
        " source folders. Only a small marker file is written; nothing is copied.",
    )
    init_parser.add_argument("backup", metavar="BACKUP", help="existing folder to hold the backup")
    init_parser.add_argument(
        "sources",
        metavar="SOURCE",
        nargs="*",
        help="folder to back up; write NAME=SOURCE to choose its subfolder name",
    )
    args = parser.parse_args(argv)
    if args.command is None:
        parser.print_help()
        return 0
    try:
        setup = init.check_init(args.backup, args.sources)
        if setup.sources:
            print("Measuring sources...", flush=True)  # a slow USB drive shouldn't look stuck
        print("\n".join(init.finish_init(setup)))
    except init.Refusal as refusal:
        print(refusal, file=sys.stderr)
        return 1
    except OSError as error:
        reason = f"{error.strerror or error}."
        print(f"{error.filename}: {reason}" if error.filename else reason, file=sys.stderr)
        return 1
    return 0
