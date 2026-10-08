"""Command-line entry point for syncplan."""

import argparse
from importlib.metadata import version


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="syncplan",
        description="Back up folders to a backup drive from a plan you review first.",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {version('syncplan')}")
    parser.parse_args(argv)
    parser.print_help()
    return 0
