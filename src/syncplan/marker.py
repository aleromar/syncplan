"""The backup marker, `<backup>\\.syncplan\\marker.json`.

It says a folder is a syncplan backup and lists the source folders it holds.
"""

import json
import logging
import os
import tempfile
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

logger = logging.getLogger(__name__)
FORMAT = 1
MARKER_DIR = ".syncplan"
MARKER_NAME = "marker.json"


@dataclass(frozen=True)
class Source:
    name: str
    path: Path


@dataclass(frozen=True)
class Marker:
    created: datetime
    sources: tuple[Source, ...]


class MarkerError(Exception):
    """A marker problem. The message is meant for the user."""


class NotInitialized(MarkerError):
    pass


class UnreadableMarker(MarkerError):
    pass


class NewerMarker(MarkerError):
    pass


def _marker_path(folder: Path) -> Path:
    return folder / MARKER_DIR / MARKER_NAME


def has_marker(folder: Path) -> bool:
    """True if the folder has a marker file, whatever its content.

    Any error other than "not there", such as access denied, is raised: a marker that
    can't be checked must not pass for no marker.
    """
    try:
        _marker_path(folder).stat()
    except FileNotFoundError, NotADirectoryError:
        return False
    return True


def read_marker(folder: Path) -> Marker:
    """The marker check: the folder's marker, or a MarkerError saying why there is none."""
    path = _marker_path(folder)
    unreadable = UnreadableMarker(f"{path} can't be read; inspect it.")
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
    except FileNotFoundError:
        raise NotInitialized(f"{folder} is not a syncplan backup.") from None
    except (OSError, ValueError) as error:
        raise unreadable from error
    try:
        version = data["syncplan_marker"]
        if type(version) is not int or version < 1:
            raise ValueError(version)
    except (KeyError, TypeError, ValueError) as error:
        raise unreadable from error
    if version > FORMAT:
        raise NewerMarker(f"{path} was written by a newer syncplan; upgrade syncplan to use it.")
    try:
        created = datetime.fromisoformat(data["created"])
        if type(data["sources"]) is not list:
            raise TypeError(data["sources"])
        sources = tuple(
            Source(_text(item["name"]), Path(_text(item["path"]))) for item in data["sources"]
        )
    except (KeyError, TypeError, ValueError) as error:
        raise unreadable from error
    return Marker(created, sources)


def write_marker(folder: Path, sources: Sequence[Source]) -> Marker:
    """Write a new marker into folder, never overwriting one (design D4).

    Raises FileExistsError if a marker is already there, or OSError if writing fails.
    Either way, no marker and no temporary file is left behind.
    """
    marker = Marker(datetime.now().astimezone().replace(microsecond=0), tuple(sources))
    text = json.dumps(
        {
            "syncplan_marker": FORMAT,
            "created": marker.created.isoformat(),
            "sources": [{"name": s.name, "path": str(s.path)} for s in marker.sources],
        },
        indent=2,
    )
    own_dir = folder / MARKER_DIR
    created_dir = not own_dir.exists()
    own_dir.mkdir(exist_ok=True)
    temp, done = None, False
    try:
        handle, name = tempfile.mkstemp(prefix="marker-", suffix=".tmp", dir=own_dir)
        temp = Path(name)
        with os.fdopen(handle, "w", encoding="ascii", newline="\n") as file:
            file.write(text + "\n")
            file.flush()
            os.fsync(file.fileno())
        temp.rename(_marker_path(folder))
        done = True
        logger.debug("marker written: %s", _marker_path(folder))
    finally:
        if not done:
            _clean_up([temp] + ([own_dir] if created_dir else []))
    return marker


def _clean_up(leftovers: list[Path | None]) -> None:
    """After a failed write: remove what this write created, and log what stayed (R1.6)."""
    removed, left = [], []
    for path in leftovers:
        if path is None:
            continue
        try:
            path.rmdir() if path.is_dir() else path.unlink()
            removed.append(path.name)
        except OSError as error:
            left.append(f"{path} ({error.strerror or error})")
    logger.debug(
        "marker write failed; cleanup removed: %s%s",
        ", ".join(removed) or "nothing",
        f"; left: {', '.join(left)}" if left else "",
    )


def _text(value: object) -> str:
    if not isinstance(value, str):
        raise TypeError(value)
    return value
