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
from dataclasses import dataclass, field, replace
from pathlib import Path
from time import monotonic

from syncplan.checks import PATH_LIMIT, Refusal, broken, too_long
from syncplan.marker import MARKER_DIR, has_marker
from syncplan.space import format_size

logger = logging.getLogger(__name__)
CHUNK = 1 << 20  # bytes read at a time from each file (D4)
PROGRESS_EVERY = 10  # seconds between progress lines (R5.5)
FOLDER_LIMIT = 248  # Windows can't make a folder whose path is this long (SP4)
SAME_DATE = "same date, other size"  # the cases of R3.1 and R3.4
DIFFERENT = "same date and size, different content"
NEWER = "backup copy newer"
UNREADABLE = "backup copy can't be read"
_ONLINE_ONLY = 0x400000 | 0x1000  # "recall on data access" or "offline" (D6, SP6)
_LATEST = 253_402_300_800 * 1_000_000_000  # 10000-01-01 in ns since 1970: past it, R6.7

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


@dataclass(frozen=True, slots=True)
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


@dataclass(frozen=True)
class Skipped:
    path: str
    reason: str


@dataclass
class Comparison:
    actions: list[Action] = field(default_factory=list)
    decide: list[Decision] = field(default_factory=list)
    chosen: list[Decision] = field(default_factory=list)  # turned into overwrites (R3.2)
    skipped: list[Skipped] = field(default_factory=list)
    unchanged: int = 0
    extra_files: int = 0  # only in the copy (R2.7)
    extra_folders: int = 0


@dataclass(frozen=True)
class _Entry:
    """One name in a folder listing, and why plan can't handle it, if it can't (R6)."""

    name: str
    folder: bool
    seen: Seen
    problem: str = ""


@dataclass(frozen=True, slots=True)
class _Pair:
    """Two files of the same size, to read and compare (R2.4).

    Only the path inside the source is kept: 100,000 pairs must fit in memory (NFR3).
    """

    path: str
    seen_source: Seen
    seen_backup: Seen


class _Unreadable(Exception):
    """Reading one side of a pair failed; side is "source" or "backup"."""

    def __init__(self, side: str, error: OSError) -> None:
        super().__init__(side)
        self.side, self.error = side, error


class _Progress:
    """R5.5: a line every PROGRESS_EVERY seconds, checked after every chunk (D4)."""

    def __init__(self, files: int, size: int) -> None:
        self.files, self.size = files, size
        self.files_done = self.done = self.reading = 0
        self.due = monotonic() + PROGRESS_EVERY

    def read(self, count: int) -> None:
        self.reading += count
        if monotonic() >= self.due:
            logger.info(
                "  %s of %s read (%s of %s files)",
                format_size(self.done + self.reading),
                format_size(self.size),
                f"{self.files_done:,}",
                f"{self.files:,}",
            )
            self.due = monotonic() + PROGRESS_EVERY

    def finished(self, pair: _Pair) -> None:
        self.files_done += 1
        self.done += pair.seen_source.size
        self.reading = 0


def key(path: str) -> str:
    """An entry's key: its path inside the source, lower-cased as Windows compares (NFR1)."""
    return path.lower()


def compare(source: Path, copy: Path, quick: bool, overwrite: Mapping[str, Path]) -> Comparison:
    """Walk the source and its copy, then read the same-size pairs (steps 7-9)."""
    walk = _Walk(source, copy, quick, overwrite)
    logger.info("Listing %s and %s...", source, copy)
    started = monotonic()
    walk.top()
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

    def top(self) -> None:
        """The two top folders. Neither may fail to list: that ends the run (R1.3, R1.4)."""
        mine, _ = _listing(self.source, source_side=True)
        if _exists(self.copy):
            theirs, _ = _listing(self.copy, source_side=False)
            self._entries("", self.source, self.copy, mine, theirs)
        else:
            self.result.actions.append(Action("make folder", "."))
            self._entries("", self.source, None, mine, {})

    def _entries(
        self,
        rel: str,
        source: Path,
        backup: Path | None,
        mine: dict[str, _Entry],
        theirs: dict[str, _Entry],
    ) -> None:
        """Sort every entry of one folder of the source and its counterpart in the copy."""
        for entry_key in sorted(mine.keys() | theirs.keys()):
            here, there = mine.get(entry_key), theirs.get(entry_key)
            path = _join(rel, (here or there).name)
            problem = (here and here.problem) or (there and there.problem)
            if problem:
                self._skip(path, problem)
            elif here is None:
                self._extra(path, backup / there.name, there)
            elif there is not None and here.folder != there.folder:
                where = "a folder here, a file" if here.folder else "a file here, a folder"
                self._skip(path, f"{where} on the backup")  # R6.4
            elif too_long(self.copy / path, FOLDER_LIMIT if here.folder else PATH_LIMIT):
                self._skip(path, _too_long_reason(here))  # R6.3, the copy's side
            elif here.folder:
                self._folder(path, source / here.name, there and backup / there.name)
            elif there is None:
                self.result.actions.append(Action("copy", path, here.seen))
            elif here.seen.size != there.seen.size:
                self._sort(path, here.seen, there.seen, same=False)
            elif self.quick:
                self._sort(path, here.seen, there.seen, same=here.seen.date == there.seen.date)
            else:
                self.pairs.append(_Pair(path, here.seen, there.seen))

    def _folder(self, rel: str, source: Path, backup: Path | None) -> None:
        """A folder of the source: list both sides, then make it if needed and go down."""
        try:
            mine, mine_backup = _listing(source, source_side=True)
            theirs, theirs_backup = (
                _listing(backup, source_side=False) if backup is not None else ({}, False)
            )
        except OSError as error:
            self._reachable()  # a drive gone ends the run (R1.3)
            self._skip(rel, f"can't be read: {_reason(error)}")  # R6.1
            return
        if mine_backup or theirs_backup:
            self._skip(rel, "a syncplan backup; backups aren't backed up")  # R6.5
            return
        if backup is None:
            self.result.actions.append(Action("make folder", rel))
        self._entries(rel, source, backup, mine, theirs)

    def _extra(self, rel: str, path: Path, entry: _Entry) -> None:
        """R2.7: count what is only in the copy, folders with everything in them."""
        if not entry.folder:
            self.result.extra_files += 1
            return
        try:
            inside, is_backup = _listing(path, source_side=False)
        except OSError as error:
            self._reachable()
            self._skip(rel, f"can't be read: {_reason(error)}")
            return
        if is_backup:
            self._skip(rel, "a syncplan backup; backups aren't backed up")
            return
        self.result.extra_folders += 1
        for inner in inside.values():
            inner_rel = _join(rel, inner.name)
            if inner.problem:
                self._skip(inner_rel, inner.problem)
            else:
                self._extra(inner_rel, path / inner.name, inner)

    def read_pairs(self) -> None:
        """Steps 8-9: read each pair once, byte for byte (R2.4, D4); --overwrite's first (D3)."""
        total = sum(pair.seen_source.size for pair in self.pairs)
        if self.pairs:
            count = f"{len(self.pairs):,}"
            logger.info("Reading %s files to compare (%s)...", count, format_size(total))
        started = monotonic()
        progress = _Progress(len(self.pairs), total)
        chosen = [pair for pair in self.pairs if key(pair.path) in self.overwrite]
        for pair in chosen:
            self._read(pair, progress)
        self._check_chosen()
        for pair in self.pairs:
            if key(pair.path) not in self.overwrite:
                self._read(pair, progress)
        logger.debug(
            "read %d pairs (%s) in %.2f s",
            len(self.pairs),
            format_size(total),
            monotonic() - started,
        )

    def _read(self, pair: _Pair, progress: _Progress) -> None:
        try:
            same = _same_bytes(self.source / pair.path, self.copy / pair.path, progress)
        except _Unreadable as unreadable:
            self._reachable()
            reason = _reason(unreadable.error)
            if unreadable.side == "source":
                self._skip(pair.path, f"the source's file can't be read: {reason}")  # R2.8
            else:
                decision = Decision(
                    pair.path, UNREADABLE, pair.seen_source, pair.seen_backup, reason
                )
                self._decide(decision)  # R3.4
            return
        finally:
            progress.finished(pair)
        if self._changed(pair):
            self._skip(pair.path, "changed while planning; plan again")  # R2.9
            return
        self._sort(pair.path, pair.seen_source, pair.seen_backup, same)

    def _changed(self, pair: _Pair) -> bool:
        """R2.9, D5: the size or date of either file differs from the listing now."""
        sides = ((self.source, pair.seen_source), (self.copy, pair.seen_backup))
        for top, seen in sides:
            path = top / pair.path
            try:
                info = path.stat()
            except OSError:
                self._reachable()
                return True
            if (info.st_size, info.st_mtime_ns) != (seen.size, seen.date):
                return True
        return False

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
            if entry_key in chosen:
                continue
            for item in self.result.skipped:
                skipped_key = key(item.path)
                if entry_key == skipped_key or entry_key.startswith(f"{skipped_key}\\"):
                    raise Refusal(f"{path} was skipped: {item.reason}")
            raise Refusal(
                f"{path} doesn't need a decision; --overwrite only takes files listed"
                ' under "Needs your decision".'
            )

    def _skip(self, path: str, reason: str) -> None:
        self.result.skipped.append(Skipped(path, reason))

    def _reachable(self) -> None:
        """R1.3: after a failure, make sure both drives are still there; if not, stop."""
        self.source.stat()
        self.copy.parent.stat()


def _listing(folder: Path, source_side: bool) -> tuple[dict[str, _Entry], bool]:
    """One folder's entries by key, as listed (SP11), and whether it is a syncplan backup."""
    entries: dict[str, _Entry] = {}
    with os.scandir(folder) as found:
        for item in found:
            info = item.stat(follow_symlinks=False)
            entry = _Entry(
                item.name,
                item.is_dir(follow_symlinks=False),
                Seen(info.st_size, info.st_mtime_ns),
            )
            entry = replace(entry, problem=_problem(item, info, entry, source_side))
            entry_key = key(item.name)
            if entry_key in entries:  # R6.3: two names differing only in letter case
                entry = replace(entries[entry_key], problem="names that differ only in letter case")
            entries[entry_key] = entry
    own = entries.get(MARKER_DIR)
    is_backup = own is not None and own.folder and not own.problem and has_marker(folder)
    return entries, is_backup


def _problem(item: os.DirEntry, info: os.stat_result, entry: _Entry, source_side: bool) -> str:
    """Why plan can't handle this entry, or "" (R6.2, R6.3, R6.6, R6.7)."""
    if item.is_symlink() or item.is_junction():
        return "a link; links aren't followed"
    if _odd(item.name):
        return "a name Windows can't normally use"
    if too_long(Path(item.path), FOLDER_LIMIT if entry.folder else PATH_LIMIT):
        return _too_long_reason(entry)
    if entry.folder:
        return ""
    if source_side and info.st_file_attributes & _ONLINE_ONLY:
        return 'online-only (OneDrive); make it "Always keep on this device"'
    if info.st_mtime_ns >= _LATEST:
        return "a date syncplan can't record"
    return ""


def _odd(name: str) -> bool:
    """R6.3: a name Windows can't normally use. A device name with an extension is fine (SP13)."""
    return (
        broken(name)
        or name != name.rstrip(". ")
        or any(ord(char) < 32 for char in name)
        or ("." not in name and os.path.isreserved(name))
    )


def _too_long_reason(entry: _Entry) -> str:
    if entry.folder:
        return f"a folder path of {FOLDER_LIMIT} characters or more"
    return f"path of {PATH_LIMIT} characters or more"


def _same_bytes(source: Path, backup: Path, progress: _Progress) -> bool:
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
                    progress.read(len(chunk))
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


def _reason(error: OSError) -> str:
    """Windows' words for an error, ending with a full stop."""
    reason = error.strerror or str(error)
    return reason if reason.endswith(".") else f"{reason}."


def _join(rel: str, name: str) -> str:
    return f"{rel}\\{name}" if rel else name
