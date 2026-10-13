"""Command-line entry point for syncplan.

Nothing here prints: every message goes to the `syncplan` loggers (design D21).
"""

import argparse
import logging
import sys
from importlib.metadata import version
from typing import IO

from syncplan import init, log, sources

logger = logging.getLogger(__name__)


class _Parser(argparse.ArgumentParser):
    """argparse with its help, usage and errors sent through the logger instead of printed."""

    def _print_message(self, message: str, file: IO[str] | None = None) -> None:
        if message:
            level = logging.ERROR if file is sys.stderr else logging.INFO
            logger.log(level, "%s", message.rstrip("\n"))


def main(argv: list[str] | None = None) -> int:
    # A redirected report keeps names like 写真 (design D11). A broken character, which init
    # refuses (R3.8), is shown as \udc80 instead of crashing the message that names it.
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="backslashreplace")
    argv = sys.argv[1:] if argv is None else argv
    outcome: int | str = 1
    try:
        log.start(argv)
        outcome = _run(argv)
        return outcome
    except log.CannotStart as blocked:
        logger.error("%s", blocked)
        return 1
    except SystemExit as exiting:  # a usage error or --version, from argparse (D19)
        outcome = exiting.code if isinstance(exiting.code, int) else 1
        raise
    except KeyboardInterrupt:
        outcome = "interrupted"
        raise
    finally:
        log.end(outcome)


def _run(argv: list[str]) -> int:
    parser = _parser()
    args = parser.parse_args(argv)
    if args.command is None:
        parser.print_help()
        return 0
    try:
        if args.command == "init":
            setup = init.check_init(args.backup, args.sources)
            if setup.sources:
                logger.info("Measuring sources...")  # a slow USB drive shouldn't look stuck
            init.finish_init(setup)
        elif args.command == "add":
            addition = sources.check_add(args.backup, args.sources)
            logger.info("Measuring sources...")
            sources.finish_add(addition)
        else:
            sources.remove(args.backup, args.names)
        return 0
    except init.Refusal as refusal:
        if not _windows_problem(refusal):
            logger.error("%s", refusal)
            return 1
        logger.error("%s", refusal, exc_info=True)
    except OSError as error:
        reason = f"{error.strerror or error}."
        message = f"{error.filename}: {reason}" if error.filename else reason
        logger.error("%s", message, exc_info=True)
    except Exception as error:
        problem = f"{type(error).__name__}: {error}"
        logger.error("syncplan hit an unexpected problem (%s).", problem, exc_info=True)
    if (path := log.log_file()) is not None:  # R5.2; otherwise the screen had it (R5.4)
        logger.error("Details: %s", path)
    return 1


def _parser() -> argparse.ArgumentParser:
    parser = _Parser(
        prog="syncplan",
        description="Back up folders to a backup drive from a plan you review first.",
        color=False,  # no terminal colour codes in the log
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
    add_parser = commands.add_parser(
        "add",
        help="add source folders to an existing backup",
        description="Add source folders to an existing syncplan backup. Only its marker file"
        " changes; nothing is copied.",
    )
    add_parser.add_argument("backup", metavar="BACKUP", help="an existing syncplan backup")
    add_parser.add_argument(
        "sources",
        metavar="SOURCE",
        nargs="+",
        help="folder to back up; write NAME=SOURCE to choose its subfolder name",
    )
    remove_parser = commands.add_parser(
        "remove",
        help="stop backing up sources, named by their subfolder",
        description="Stop backing up sources of a syncplan backup, named by their subfolder."
        " Nothing is deleted: a source's folder on the backup stays as it is, waiting for"
        " the quarantine.",
    )
    remove_parser.add_argument("backup", metavar="BACKUP", help="an existing syncplan backup")
    remove_parser.add_argument(
        "names", metavar="NAME", nargs="+", help="subfolder name of a source, as listed"
    )
    return parser


def _windows_problem(error: BaseException | None) -> bool:
    """True if a refusal was caused by an error Windows reported (R5.2, design D19)."""
    while error is not None:
        if isinstance(error, OSError):
            return True
        error = error.__cause__
    return False
