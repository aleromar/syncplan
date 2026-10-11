"""syncplan's own folder: the debug log and the one-run lock (R5, design D16-D21).

Nothing in syncplan prints. Every message goes to the `syncplan` logger. While a command
runs, the screen shows INFO and up (warnings and errors on stderr) and the log file shows
everything, each line prefixed with the time, the level and a short run ID.
"""

import logging
import msvcrt
import os
import platform
import secrets
import sys
import time
from collections.abc import Sequence
from contextlib import suppress
from datetime import datetime
from importlib.metadata import version
from pathlib import Path

LOG_LIMIT = 1_048_576  # a log this big is rotated when the next run starts (R5.3)
OLD_LOGS = 5
_FALLBACK = Path("C:\\ProgramData")

logger = logging.getLogger("syncplan")
logger.setLevel(logging.DEBUG)
logger.propagate = False  # never Python's root logger or its last-resort output
logger.addHandler(logging.NullHandler())


class CannotStart(Exception):
    """The run can't start safely (R5.6, PD10). The message says why and is for the user."""


class _FileHandler(logging.FileHandler):
    """Appends to the log. The first failed write turns it off with one note (R5.4)."""

    broken = False

    def handleError(self, record: logging.LogRecord) -> None:
        if self.broken:
            return
        self.broken = True
        _note(f"couldn't write the log at {self.baseFilename}", sys.exc_info()[1])

    def emit(self, record: logging.LogRecord) -> None:
        if not self.broken:
            super().emit(record)


class _FileFormatter(logging.Formatter):
    """Puts the time, the level and the run ID on every line, tracebacks included (R5.1)."""

    def __init__(self, run_id: str) -> None:
        super().__init__()
        self.run_id = run_id

    def format(self, record: logging.LogRecord) -> str:
        stamp = datetime.fromtimestamp(record.created).astimezone()
        prefix = (
            f"{stamp.isoformat(' ', 'milliseconds')} {record.levelname:<5} [run {self.run_id}] "
        )
        return "\n".join(prefix + line for line in super().format(record).splitlines() or [""])


class _ScreenFormatter(logging.Formatter):
    """The message alone; the traceback only when no log holds it (R5.2, R5.4)."""

    def format(self, record: logging.LogRecord) -> str:
        message = record.getMessage()
        if record.exc_info and not logging_to_file():
            message += "\n" + self.formatException(record.exc_info).rstrip("\n")
        return message


_run: dict = {}  # the current run: its file handler, lock and start time


def home() -> Path:
    """syncplan's own folder, normally C:\\ProgramData\\syncplan (design D18)."""
    value = os.environ.get("PROGRAMDATA", "")
    base = Path(value) if value and Path(value).is_absolute() else _FALLBACK
    return base / "syncplan"


def start(argv: Sequence[str]) -> Path | None:
    """Set up the screen, take the run lock, rotate and open the log. Returns the log path.

    Returns None when the log can't be written (R5.4, after one note). Raises CannotStart
    when another run holds the lock (R5.6) or syncplan's folder is a link (PD10).
    """
    _run.clear()
    _run["started"] = time.perf_counter()
    report = logging.StreamHandler(sys.stdout)  # INFO: the report (D21)
    report.setLevel(logging.INFO)
    report.addFilter(lambda record: record.levelno < logging.WARNING)
    problems = logging.StreamHandler(sys.stderr)  # WARNING and up: warnings, refusals, errors
    problems.setLevel(logging.WARNING)
    for handler in (report, problems):
        handler.setFormatter(_ScreenFormatter())
        logger.addHandler(handler)
    folder = home()
    for path in (folder, folder / "logs"):
        if path.is_symlink() or path.is_junction():
            raise CannotStart(
                f"{path} is a link, not a real folder. syncplan keeps its log there and needs a"
                " real folder; remove the link and try again."
            )
    try:
        (folder / "logs").mkdir(parents=True, exist_ok=True)
    except OSError as error:
        _note(f"couldn't write syncplan's folder at {folder}", error)
        return None
    _lock(folder / "syncplan.lock")
    log_path = folder / "logs" / "syncplan.log"
    _rotate(log_path)
    try:
        handler = _FileHandler(log_path, encoding="utf-8", errors="backslashreplace")
    except OSError as error:
        _note(f"couldn't write the log at {log_path}", error)
        return None
    handler.setFormatter(_FileFormatter(secrets.token_hex(2)))
    logger.handlers.insert(0, handler)  # the file first, so errors reach it before the screen
    _run["file"] = handler
    # Diagnostics are DEBUG: the screen shows INFO and up, and these belong in the file only.
    logger.debug(
        "syncplan %s, Python %s, Windows %s",
        version("syncplan"),
        platform.python_version(),
        platform.version(),
    )
    logger.debug("command: %s", " ".join(_escaped(arg) for arg in argv))
    return log_path


def end(outcome: int | str) -> None:
    """Log how the run ended, then remove the handlers and release the run lock."""
    if "started" in _run:
        elapsed = time.perf_counter() - _run["started"]
        ended = f"exit {outcome}" if isinstance(outcome, int) else outcome
        logger.debug("%s after %.2f s", ended, elapsed)
    for handler in [h for h in logger.handlers if not isinstance(h, logging.NullHandler)]:
        logger.removeHandler(handler)
        with suppress(OSError):
            handler.close()
    if (lock := _run.get("lock")) is not None:
        os.close(lock)  # Windows releases the lock with the handle (SP18)
    _run.clear()


def logging_to_file() -> bool:
    """True while this run's log file is being written."""
    handler = _run.get("file")
    return handler is not None and not handler.broken


def _lock(path: Path) -> None:
    """Take the one-run lock (R5.6, D20). Read-only, so any account can take it (SP19)."""
    try:
        handle = os.open(path, os.O_RDONLY | os.O_CREAT)
    except OSError as error:
        _note(f"couldn't use the run lock at {path}", error)
        return
    try:
        msvcrt.locking(handle, msvcrt.LK_NBLCK, 1)
    except OSError:
        os.close(handle)
        raise CannotStart(
            "Another syncplan is already running on this PC; wait for it to finish, then try again."
        ) from None
    _run["lock"] = handle


def _rotate(log_path: Path) -> None:
    """R5.3, D17: at the start, move a full log aside first; never overwrite an old log."""
    rotating = log_path.with_name(f"{log_path.name}.rotating")
    olds = [log_path.with_name(f"{log_path.name}.{n}") for n in range(1, OLD_LOGS + 1)]
    with suppress(OSError):  # a failed step stops here; .rotating is finished next time
        if not rotating.exists():
            if log_path.stat().st_size < LOG_LIMIT:
                return
            log_path.rename(rotating)
        gap = next((index for index, old in enumerate(olds) if not old.exists()), None)
        if gap is None:
            olds[-1].unlink()
            gap = OLD_LOGS - 1
        for index in range(gap, 0, -1):
            olds[index - 1].rename(olds[index])
        rotating.rename(olds[0])


def _note(what: str, error: BaseException | None) -> None:
    reason = getattr(error, "strerror", None) or error
    logger.warning("Note: %s: %s.", what, reason)


def _escaped(text: str) -> str:
    """Control characters as \\x0a escapes, so a crafted argument can't fake a log line."""
    return "".join(char if char.isprintable() else f"\\x{ord(char):02x}" for char in text)
