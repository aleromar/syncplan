"""The backup marker, `<backup>\\.syncplan\\marker.json`.

It says a folder is a syncplan backup, lists the source folders it holds, and lists the
folders of removed sources that wait for the quarantine.
"""

import json
import logging
import os
import tempfile
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
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
class Waiting:
    """A removed source's subfolder, waiting for the quarantine (manage-sources R3.1)."""

    name: str
    path: Path
    removed: datetime


@dataclass(frozen=True)
class Marker:
    created: datetime
    sources: tuple[Source, ...]
    waiting: tuple[Waiting, ...] = ()
    raw: bytes = field(default=b"", compare=False, repr=False)  # as read (manage-sources D2)


class MarkerError(Exception):
    """A marker problem. The message is meant for the user."""


class NotInitialized(MarkerError):
    pass


class UnreadableMarker(MarkerError):
    pass


class NewerMarker(MarkerError):
    pass


class MarkerChanged(MarkerError):
    pass


def marker_path(folder: Path) -> Path:
    """Where the folder's marker is, whether or not it exists."""
    return folder / MARKER_DIR / MARKER_NAME


def has_marker(folder: Path) -> bool:
    """True if the folder has a marker file, whatever its content.

    Any error other than "not there", such as access denied, is raised: a marker that
    can't be checked must not pass for no marker.
    """
    try:
        marker_path(folder).stat()
    except FileNotFoundError, NotADirectoryError:
        return False
    return True


def read_marker(folder: Path) -> Marker:
    """The marker check: the folder's marker, or a MarkerError saying why there is none."""
    path = marker_path(folder)
    unreadable = UnreadableMarker(f"{path} can't be read; inspect it.")
    try:
        raw = path.read_bytes()
        data = _parse(raw)
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
        sources = tuple(
            Source(_text(item["name"]), Path(_text(item["path"])))
            for item in _list(data["sources"])
        )
        waiting = tuple(
            Waiting(
                _text(item["name"]),
                Path(_text(item["path"])),
                datetime.fromisoformat(_text(item["removed"])),
            )
            for item in _list(data.get("to_quarantine", []))
        )
    except (KeyError, TypeError, ValueError) as error:
        raise unreadable from error
    return Marker(created, sources, waiting, raw)


def write_marker(folder: Path, sources: Sequence[Source]) -> Marker:
    """Write a new marker into folder, never overwriting one (design D4).

    Raises FileExistsError if a marker is already there, or OSError if writing fails.
    Either way, no marker and no temporary file is left behind.
    """
    marker = Marker(datetime.now().astimezone().replace(microsecond=0), tuple(sources))
    data = {
        "syncplan_marker": FORMAT,
        "created": marker.created.isoformat(),
        "sources": [_source_entry(s) for s in marker.sources],
    }
    own_dir = folder / MARKER_DIR
    created_dir = not own_dir.exists()
    own_dir.mkdir(exist_ok=True)
    target = marker_path(folder)
    write_through_temp(
        own_dir, _json_text(data), lambda temp: temp.rename(target), created_dir, "marker-"
    )
    logger.debug("marker written: %s", marker_path(folder))
    return marker


def replace_marker(
    folder: Path, old: Marker, sources: Sequence[Source], waiting: Sequence[Waiting]
) -> None:
    """Replace the folder's marker, read earlier as old, with new lists (manage-sources D2, D3).

    Every other field of the marker is kept, and so is the original entry of every source
    and waiting folder that stays. Raises MarkerChanged if the marker on disk is no longer
    the one old was read from, or OSError if writing fails. Either way the marker on disk
    is left as it was.
    """
    data = _parse(old.raw)
    data["sources"] = _keep_originals(data["sources"], [_source_entry(s) for s in sources])
    entries = [{**_source_entry(w), "removed": w.removed.isoformat()} for w in waiting]
    data["to_quarantine"] = _keep_originals(data.get("to_quarantine", []), entries)
    if not data["to_quarantine"]:
        del data["to_quarantine"]
    target = marker_path(folder)

    def check_then_replace(temp: Path) -> None:
        # Checked right before the swap, after the slow fsync (manage-sources K3).
        try:
            current = target.read_bytes()
        except FileNotFoundError:
            target.parent.stat()  # the drive gone? That is the error (manage-sources SP6)
            current = None
        if current != old.raw:
            raise MarkerChanged(
                f"The source list of {folder} changed while syncplan was running;"
                " nothing was changed. Check it and run the command again."
            )
        temp.replace(target)

    write_through_temp(target.parent, _json_text(data), check_then_replace, False, "marker-")
    logger.debug("marker replaced: %s", target)


def write_through_temp(
    own_dir: Path, text: str, swap: Callable[[Path], None], created_dir: bool, prefix: str
) -> None:
    """Write text to a new temporary file in own_dir, flushed and closed, then swap it in.

    The file is UTF-8; a broken character becomes a \\udc80 escape (plan-copies D8). If
    anything fails, the temporary file, and own_dir if this write created it, are removed
    when possible (R1.6, manage-sources R6.2).
    """
    temp, done = None, False
    try:
        handle, name = tempfile.mkstemp(prefix=prefix, suffix=".tmp", dir=own_dir)
        temp = Path(name)
        utf8 = {"encoding": "utf-8", "errors": "backslashreplace", "newline": "\n"}
        with os.fdopen(handle, "w", **utf8) as file:
            file.write(text)
            file.flush()
            os.fsync(file.fileno())
        swap(temp)  # only once closed: an open file can't be renamed (manage-sources SP4)
        done = True
    finally:
        if not done:
            _clean_up([temp] + ([own_dir] if created_dir else []), prefix.rstrip("-"))


def _clean_up(leftovers: list[Path | None], what: str) -> None:
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
        "%s write failed; cleanup removed: %s%s",
        what,
        ", ".join(removed) or "nothing",
        f"; left: {', '.join(left)}" if left else "",
    )


def _json_text(data: dict) -> str:
    """The marker's text: indented JSON, ASCII only (init-marker D12)."""
    return json.dumps(data, indent=2) + "\n"


def _parse(raw: bytes) -> dict:
    """The marker's JSON. A byte-order mark is fine: Notepad may add one (init-marker D12)."""
    return json.loads(raw.decode("utf-8-sig"))


def _source_entry(source: Source | Waiting) -> dict:
    return {"name": source.name, "path": str(source.path)}


def _keep_originals(before: list[dict], after: list[dict]) -> list[dict]:
    """The after entries, each kept as its original object if it was there (manage-sources D3)."""
    originals = {(item["name"], str(Path(item["path"]))): item for item in before}
    return [originals.get((entry["name"], entry["path"]), entry) for entry in after]


def _list(value: object) -> list:
    if type(value) is not list:
        raise TypeError(value)
    return value


def _text(value: object) -> str:
    if not isinstance(value, str):
        raise TypeError(value)
    return value
