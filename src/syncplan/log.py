"""syncplan's own folder: the debug log and the one-run lock (R5, design D16-D21).

Nothing in syncplan prints. Every message goes to the `syncplan` logger. While a command
runs, the screen shows INFO and up (warnings and errors on stderr) and the log file shows
everything, each line prefixed with the time, the level and a short run ID.
"""

import errno
import logging
import msvcrt
import os
import platform
import secrets
import subprocess
import sys
import time
from collections.abc import Sequence
from contextlib import suppress
from dataclasses import dataclass
from datetime import datetime
from importlib.metadata import version
from pathlib import Path

LOG_LIMIT = 1_048_576  # a log this big is rotated when the next run starts (R5.3)
OLD_LOGS = 5
_FALLBACK = Path("C:\\ProgramData")
_BUSY = (errno.EACCES, errno.EDEADLOCK)  # what a lock held by another run gives (SP18)

logger = logging.getLogger("syncplan")
logger.setLevel(logging.DEBUG)
logger.propagate = False
logger.addHandler(logging.NullHandler())  # outside a run, nothing reaches Python's last resort


class CannotStart(Exception):
    """The run can't start safely (R5.6, R5.7). The message says why and is for the user."""


class _FileHandler(logging.FileHandler):
    """Appends to the log. The first failed write turns it off with one note (R5.4)."""

    broken = False

    def emit(self, record: logging.LogRecord) -> None:
        if not self.broken:
            super().emit(record)

    def handleError(self, record: logging.LogRecord) -> None:
        self.broken = True
        _note(f"couldn't write the log at {self.baseFilename}: {_reason(sys.exc_info()[1])}")


class _FileFormatter(logging.Formatter):
    """Puts the time, the level and the run ID on every line, tracebacks included (R5.1)."""

    def __init__(self, run_id: str) -> None:
        super().__init__()
        self.run_id = run_id

    def format(self, record: logging.LogRecord) -> str:
        stamp = datetime.fromtimestamp(record.created).astimezone().isoformat(" ", "milliseconds")
        prefix = f"{stamp} {record.levelname:<5} [run {self.run_id}] "
        return "\n".join(prefix + line for line in super().format(record).splitlines() or [""])


class _ScreenFormatter(logging.Formatter):
    """The message alone; the traceback only when no log holds it (R5.2, R5.4)."""

    def format(self, record: logging.LogRecord) -> str:
        message = record.getMessage()
        if record.exc_info and log_file() is None:
            message += "\n" + self.formatException(record.exc_info).rstrip("\n")
        return message


@dataclass
class _Run:
    started: float
    file: _FileHandler | None = None
    lock: int | None = None


_current: _Run | None = None


def home() -> Path:
    """syncplan's own folder, normally C:\\ProgramData\\syncplan (design D18)."""
    value = os.environ.get("PROGRAMDATA", "")
    base = Path(value) if value and Path(value).is_absolute() else _FALLBACK
    return base / "syncplan"


def start(argv: Sequence[str]) -> None:
    """Set up the screen, refuse links (R5.7), take the run lock (R5.6), rotate and open the log.

    Raises CannotStart. Anything else that fails gives one note, and the run goes on (R5.4).
    """
    global _current
    _current = _Run(time.perf_counter())
    report = logging.StreamHandler(sys.stdout)  # INFO: the report (D21)
    report.setLevel(logging.INFO)
    report.addFilter(lambda record: record.levelno < logging.WARNING)
    problems = logging.StreamHandler(sys.stderr)  # WARNING and up: warnings, refusals, errors
    problems.setLevel(logging.WARNING)
    for handler in (report, problems):
        handler.setFormatter(_ScreenFormatter())
        logger.addHandler(handler)
    folder = home()
    log_path, lock_path = folder / "logs" / "syncplan.log", folder / "syncplan.lock"
    _refuse_links(log_path.parent, log_path, lock_path)
    try:
        log_path.parent.mkdir(parents=True, exist_ok=True)
    except OSError as error:
        _note(f"couldn't write syncplan's folder at {folder}: {_reason(error)}")
        return
    troubles = _lock(lock_path)
    stopped = _rotate(log_path)
    try:
        handler = _FileHandler(log_path, encoding="utf-8", errors="backslashreplace")
    except OSError as error:
        troubles.append(f"couldn't write the log at {log_path}: {_reason(error)}")
    else:
        handler.setFormatter(_FileFormatter(secrets.token_hex(2)))
        logger.handlers.insert(0, handler)  # the file first, so errors reach it before the screen
        _current.file = handler
    if troubles:
        _note("; ".join(troubles))
    # Diagnostics are DEBUG: the screen shows INFO and up, and these belong in the file only.
    logger.debug(
        "syncplan %s, Python %s, Windows %s",
        version("syncplan"),
        platform.python_version(),
        platform.version(),
    )
    logger.debug("command: %s", subprocess.list2cmdline([_escaped(arg) for arg in argv]))
    if stopped is not None:
        logger.debug("log rotation stopped, to be finished next time: %s", _reason(stopped))


def end(outcome: int | str) -> None:
    """Log how the run ended, then remove the handlers and release the run lock."""
    global _current
    run = _current
    if run is not None:
        ended = f"exit {outcome}" if isinstance(outcome, int) else outcome
        logger.debug("%s after %.2f s", ended, time.perf_counter() - run.started)
    for handler in [h for h in logger.handlers if not isinstance(h, logging.NullHandler)]:
        logger.removeHandler(handler)
        with suppress(OSError):
            handler.close()
    if run is not None and run.lock is not None:
        os.close(run.lock)  # Windows releases the lock with the handle (SP18)
    _current = None


def log_file() -> Path | None:
    """The log this run is writing, or None when there is none (R5.4)."""
    handler = None if _current is None else _current.file
    return None if handler is None or handler.broken else Path(handler.baseFilename)


def _refuse_links(logs: Path, log_path: Path, lock_path: Path) -> None:
    """R5.7: a link anywhere on the way could put the log inside a backed-up folder."""
    if logs.resolve() != logs:
        raise CannotStart(
            f"{logs} is reached through a link, not a real folder. syncplan only writes its"
            " log to real folders; remove the link and try again."
        )
    for path in (log_path, lock_path):
        if path.is_symlink() or (path.exists() and path.stat().st_nlink > 1):
            raise CannotStart(
                f"{path} is a link, not a real file. syncplan only writes its log to real"
                " files; remove the link and try again."
            )


def _lock(path: Path) -> list[str]:
    """Take the one-run lock (R5.6, D20), read-only so any account can (SP19).

    Returns what couldn't be done, for R5.4's note.
    """
    try:
        handle = os.open(path, os.O_RDONLY | os.O_CREAT)
    except OSError as error:
        return [f"couldn't use the run lock at {path}: {_reason(error)}"]
    try:
        msvcrt.locking(handle, msvcrt.LK_NBLCK, 1)
    except OSError as error:
        os.close(handle)
        if error.errno in _BUSY:
            raise CannotStart(
                "Another syncplan is already running on this PC; wait for it to finish,"
                " then try again."
            ) from None
        return [f"couldn't use the run lock at {path}: {_reason(error)}"]
    _current.lock = handle
    return []


def _rotate(log_path: Path) -> OSError | None:
    """R5.3, D17: at the start, move a full log aside first; never overwrite an old log.

    Returns the error that stopped a rotation; `.rotating` is then finished next time.
    """
    rotating = log_path.with_name(f"{log_path.name}.rotating")
    olds = [log_path.with_name(f"{log_path.name}.{n}") for n in range(1, OLD_LOGS + 1)]
    try:
        if not rotating.exists():
            if not log_path.exists() or log_path.stat().st_size < LOG_LIMIT:
                return None
            log_path.rename(rotating)
        gap = next((index for index, old in enumerate(olds) if not old.exists()), None)
        if gap is None:
            olds[-1].unlink()
            gap = OLD_LOGS - 1
        for index in range(gap, 0, -1):
            olds[index - 1].rename(olds[index])
        rotating.rename(olds[0])
    except OSError as error:
        return error
    return None


def _note(text: str) -> None:
    logger.warning("Note: %s.", text)


def _reason(error: BaseException | None) -> str:
    return str(getattr(error, "strerror", None) or error)


def _escaped(text: str) -> str:
    """Control characters as \\x0a-style escapes, so an argument can't fake a log line."""
    return "".join(char if char.isprintable() else f"\\x{ord(char):02x}" for char in text)
