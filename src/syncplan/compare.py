"""Compare a source with its copy on the backup (plan-copies R2, R3, R6).

The two folders are walked together, folder by folder (D2), and every entry is sorted into
an action, a decision, a skip or "unchanged". The same-size pairs are read afterwards, so
the totals for the progress lines are known first (D3).
"""

import ctypes
import logging
import msvcrt
import os
from collections.abc import Mapping
from ctypes import wintypes
from dataclasses import dataclass, field
from pathlib import Path
from time import monotonic

from syncplan.checks import Refusal
from syncplan.space import format_size

logger = logging.getLogger(__name__)
CHUNK = 1 << 20  # bytes read at a time from each file (D4)
SAME_DATE = "same date, other size"  # the cases of R3.1 and R3.4
DIFFERENT = "same date and size, different content"
NEWER = "backup copy newer"
UNREADABLE = "backup copy can't be read"

_kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
_kernel32.CreateFileW.argtypes = [
    wintypes.LPCWSTR,
    wintypes.DWORD,
    wintypes.DWORD,
    wintypes.LPVOID,
    wintypes.DWORD,
    wintypes.DWORD,
    wintypes.HANDLE,
]
_kernel32.CreateFileW.restype = wintypes.HANDLE
_INVALID_HANDLE = wintypes.HANDLE(-1).value
_GENERIC_READ = 0x80000000
_SHARE_ALL = 0x1 | 0x2 | 0x4  # read, write and delete (D15)
_OPEN_EXISTING = 3
_SEQUENTIAL_SCAN = 0x08000000


@dataclass(frozen=True)
class Seen:
    """A file as the listing showed it: its size in bytes and its date in ns (SP3)."""

    size: int
    date: int


@dataclass(frozen=True)
class Action:
    do: str  # "make folder", "copy", "overwrite" or "set date"
    path: str  # inside the source; "." is the copy's own folder
    source: Seen | None = None
    backup: Seen | None = None


@dataclass(frozen=True)
class Decision:
    """A file that differs in a way no edit explains: the user decides (R3.1, R3.4)."""

    path: str
    case: str
    source: Seen
    backup: Seen
    reason: str = ""  # Windows' reason, for an unreadable backup copy


@dataclass
class Comparison:
    actions: list[Action] = field(default_factory=list)
    decide: list[Decision] = field(default_factory=list)
    chosen: list[Decision] = field(default_factory=list)  # turned into overwrites (R3.2)
    unchanged: int = 0
    extra_files: int = 0  # only in the copy (R2.7)
    extra_folders: int = 0


@dataclass(frozen=True)
class _Entry:
    """One name in a folder listing."""

    name: str
    folder: bool
    seen: Seen


@dataclass(frozen=True)
class _Pair:
    """Two files of the same size, to read and compare (R2.4)."""

    path: str
    source: Path
    backup: Path
    seen_source: Seen
    seen_backup: Seen


class _Unreadable(Exception):
    """Reading one side of a pair failed; side is "source" or "backup"."""

    def __init__(self, side: str, error: OSError) -> None:
        super().__init__(side)
        self.side, self.error = side, error


def key(path: str) -> str:
    """An entry's key: its path inside the source, lower-cased as Windows compares (NFR1)."""
    return path.lower()


def compare(source: Path, copy: Path, quick: bool, overwrite: Mapping[str, Path]) -> Comparison:
    """Walk the source and its copy, then read the same-size pairs (steps 7-9)."""
    walk = _Walk(source, copy, quick, overwrite)
    logger.info("Listing %s and %s...", source, copy)
    started = monotonic()
    if _exists(copy):
        walk.folder("", source, copy)
    else:
        walk.result.actions.append(Action("make folder", "."))
        walk.folder("", source, None)
    logger.debug(
        "listed %s and %s: %d pairs to read, in %.2f s",
        source,
        copy,
        len(walk.pairs),
        monotonic() - started,
    )
    walk.read_pairs()
    return walk.result


class _Walk:
    def __init__(self, source: Path, copy: Path, quick: bool, overwrite: Mapping[str, Path]):
        self.source, self.copy, self.quick, self.overwrite = source, copy, quick, overwrite
        self.result = Comparison()
        self.pairs: list[_Pair] = []

    def folder(self, rel: str, source: Path, backup: Path | None) -> None:
        """One folder of the source and, if it has one, its counterpart in the copy."""
        mine = _listing(source)
        theirs = _listing(backup) if backup is not None else {}
        for entry_key in sorted(mine.keys() | theirs.keys()):
            here, there = mine.get(entry_key), theirs.get(entry_key)
            path = _join(rel, (here or there).name)
            if here is None:
                self._extra(there, backup / there.name)
            elif here.folder:
                inner = None if there is None else backup / there.name
                if inner is None:
                    self.result.actions.append(Action("make folder", path))
                self.folder(path, source / here.name, inner)
            elif there is None:
                self.result.actions.append(Action("copy", path, here.seen))
            elif here.seen.size != there.seen.size:
                self._sort(path, here.seen, there.seen, same=False)
            elif self.quick:
                self._sort(path, here.seen, there.seen, same=here.seen.date == there.seen.date)
            else:
                pair = _Pair(path, source / here.name, backup / there.name, here.seen, there.seen)
                self.pairs.append(pair)

    def read_pairs(self) -> None:
        """Steps 8-9: read each pair once, byte for byte (R2.4, D4); --overwrite's first (D3)."""
        total = sum(pair.seen_source.size for pair in self.pairs)
        if self.pairs:
            count = f"{len(self.pairs):,}"
            logger.info("Reading %s files to compare (%s)...", count, format_size(total))
        started = monotonic()
        chosen = [pair for pair in self.pairs if key(pair.path) in self.overwrite]
        for pair in chosen:
            self._read(pair)
        self._check_chosen()
        for pair in self.pairs:
            if key(pair.path) not in self.overwrite:
                self._read(pair)
        logger.debug(
            "read %d pairs (%s) in %.2f s",
            len(self.pairs),
            format_size(total),
            monotonic() - started,
        )

    def _read(self, pair: _Pair) -> None:
        try:
            same = _same_bytes(pair.source, pair.backup)
        except _Unreadable as unreadable:
            if unreadable.side != "backup":
                raise unreadable.error from None
            reason = unreadable.error.strerror or str(unreadable.error)
            decision = Decision(pair.path, UNREADABLE, pair.seen_source, pair.seen_backup, reason)
            self._decide(decision)
            return
        self._sort(pair.path, pair.seen_source, pair.seen_backup, same)

    def _sort(self, path: str, source: Seen, backup: Seen, same: bool) -> None:
        """R2.3, R2.5, R3.1: what a file on both sides needs."""
        if same and source.date == backup.date:
            self.result.unchanged += 1
        elif same:
            self.result.actions.append(Action("set date", path, source, backup))
        elif source.date > backup.date:
            self.result.actions.append(Action("overwrite", path, source, backup))
        elif source.date == backup.date:
            case = DIFFERENT if source.size == backup.size else SAME_DATE
            self._decide(Decision(path, case, source, backup))
        else:
            self._decide(Decision(path, NEWER, source, backup))

    def _decide(self, decision: Decision) -> None:
        """R3.1, R3.2: list it for the user, or overwrite it as --overwrite chose."""
        if key(decision.path) not in self.overwrite:
            self.result.decide.append(decision)
            return
        self.result.chosen.append(decision)
        overwrite = Action("overwrite", decision.path, decision.source, decision.backup)
        self.result.actions.append(overwrite)

    def _check_chosen(self) -> None:
        """R3.3: every --overwrite file must be one this plan lists as needing a decision."""
        chosen = {key(decision.path) for decision in self.result.chosen}
        for entry_key, path in self.overwrite.items():
            if entry_key not in chosen:
                raise Refusal(
                    f"{path} doesn't need a decision; --overwrite only takes files listed"
                    ' under "Needs your decision".'
                )

    def _extra(self, entry: _Entry, path: Path) -> None:
        """R2.7: count what is only in the copy, folders with everything in them."""
        if not entry.folder:
            self.result.extra_files += 1
            return
        self.result.extra_folders += 1
        for inner in _listing(path).values():
            self._extra(inner, path / inner.name)


def _listing(folder: Path) -> dict[str, _Entry]:
    """One folder's entries by key, with each file's size and date as listed (SP11)."""
    entries: dict[str, _Entry] = {}
    with os.scandir(folder) as found:
        for item in found:
            info = item.stat(follow_symlinks=False)
            is_folder = item.is_dir(follow_symlinks=False)
            entries[key(item.name)] = _Entry(
                item.name, is_folder, Seen(info.st_size, info.st_mtime_ns)
            )
    return entries


def _same_bytes(source: Path, backup: Path) -> bool:
    """True if the two files hold the same bytes, read in chunks to the first difference.

    A failure is raised as _Unreadable, naming the side that failed (R2.8, R3.4).
    """
    side = "source"
    try:
        with _open_shared(source) as mine:
            side = "backup"
            with _open_shared(backup) as theirs:
                while True:
                    side = "source"
                    chunk = mine.read(CHUNK)
                    side = "backup"
                    if chunk != theirs.read(CHUNK):
                        return False
                    if not chunk:
                        return True
    except OSError as error:
        raise _Unreadable(side, error) from error


def _open_shared(path: Path):
    """Open a file for reading so that other programs can still rename or delete it (D15)."""
    handle = _kernel32.CreateFileW(
        str(path), _GENERIC_READ, _SHARE_ALL, None, _OPEN_EXISTING, _SEQUENTIAL_SCAN, None
    )
    if handle == _INVALID_HANDLE:
        error = ctypes.WinError(ctypes.get_last_error())
        error.filename = str(path)
        raise error
    return os.fdopen(msvcrt.open_osfhandle(handle, os.O_RDONLY), "rb", buffering=0)


def _exists(copy: Path) -> bool:
    """Whether the copy's folder is there; check_plan already made sure it is a plain folder."""
    try:
        copy.lstat()
    except FileNotFoundError:
        copy.parent.stat()  # the backup drive itself gone? Then that error ends the run (R1.3)
        return False
    return True


def _join(rel: str, name: str) -> str:
    return f"{rel}\\{name}" if rel else name
