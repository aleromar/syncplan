"""`syncplan init`: turn an existing folder into a backup of the given source folders.

Everything is read and checked first, and the marker is the only thing written (design D3).
Only ordinary folders on drives with a letter are supported; anything exotic is refused (PD10).
"""

import logging
import os
from collections.abc import Sequence
from dataclasses import dataclass
from itertools import combinations
from pathlib import Path
from shutil import disk_usage

from syncplan import log, space
from syncplan.marker import (
    MARKER_DIR,
    Marker,
    MarkerError,
    NotInitialized,
    Source,
    has_marker,
    read_marker,
    write_marker,
)
from syncplan.space import format_size

logger = logging.getLogger(__name__)
_FORBIDDEN = frozenset('\\/:*?"<>|')
_MAX_NAME = 255  # UTF-16 units, as Windows counts them
_FREE_SHARE = 10  # warn when less than 1/10 of the drive would be left (R2.7)


class Refusal(Exception):
    """init won't go ahead. The message says why and is meant for the user."""


@dataclass(frozen=True)
class Setup:
    backup: Path
    sources: tuple[Source, ...]
    adopted: tuple[Path, ...]


def read_argument(text: str) -> tuple[str | None, Path]:
    """Split `NAME=SOURCE` (design D2) and read a bare drive `E:` as `E:\\` (D8)."""
    name = None
    before, equals, after = text.partition("=")
    if equals and not any(char in before for char in "\\/:"):
        name, text = before, after
    return name, _bare_drive(Path(text))


def check_init(backup: str, sources: Sequence[str]) -> Setup:
    """Steps 1-4: resolve every path, then refuse anything unsafe. Nothing is written."""
    backup_dir = backup_folder(backup)
    _check_backup(backup_dir)
    logger.debug("backup %s: ok (exists, no marker, no backup above)", backup_dir)
    named, adopted = check_sources(backup_dir, sources)
    return Setup(backup_dir, named, adopted)


def backup_folder(text: str) -> Path:
    """R2.1, R2.9 and R2.10: the backup folder, resolved. Refuses a missing or exotic one."""
    backup_dir = _resolve(_bare_drive(Path(text)))
    _check_folder(backup_dir)
    own = backup_dir / MARKER_DIR
    if own.is_symlink() or own.is_junction() or (own.exists() and not own.is_dir()):
        raise Refusal(f"{own} is not a real folder. Remove it, or choose another backup folder.")
    return backup_dir


def check_sources(
    backup_dir: Path, arguments: Sequence[str]
) -> tuple[tuple[Source, ...], tuple[Path, ...]]:
    """Steps 3-4 for each SOURCE or NAME=SOURCE (R3.1-R3.9). Nothing is written.

    Returns the sources, named, and the folders already on the backup they adopt (R1.5).
    """
    given = [(name, _resolve(path)) for name, path in map(read_argument, arguments)]
    for _, path in given:
        _check_source(path, backup_dir)
        logger.debug("source %s: exists, not syncplan's or a backup, apart from the backup", path)
    for (_, first), (_, second) in combinations(given, 2):
        _check_apart(first, second)
    named = [Source(subfolder_name(name, path), path) for name, path in given]
    _check_names(named)
    for source in named:
        _check_subfolder(backup_dir / source.name, source)
    adopted = tuple(backup_dir / s.name for s in named if (backup_dir / s.name).is_dir())
    for source in named:
        found = ", adopting the folder already there" if backup_dir / source.name in adopted else ""
        logger.debug("source %s: ok (subfolder %s%s)", source.path, source.name, found)
    return tuple(named), adopted


def subfolder_name(name: str | None, path: Path) -> str:
    """The NAME given, or the resolved folder's own name (design D14). R3.4 for drive roots."""
    if name is not None:
        return name
    if not path.name:
        raise Refusal(
            f"{path} is a whole drive, so it has no folder name. Give it one with NAME=SOURCE,"
            f" for example Drive{path.drive.rstrip(':')}={path.drive}"
        )
    return path.name


def check_name(name: str) -> None:
    """Refuse a subfolder name Windows can't use, or syncplan's own (R3.5, R3.8, design D15)."""
    if _broken(name):
        raise Refusal(f'"{name}" has a character that isn\'t valid text.')
    if name.lower() == MARKER_DIR:
        raise Refusal(f'"{name}" is reserved for syncplan\'s own files.')
    too_long = len(name.encode("utf-16-le")) // 2 > _MAX_NAME
    invalid = name in ("", ".", "..") or too_long or not _FORBIDDEN.isdisjoint(name)
    if invalid or os.path.isreserved(name):  # device names, trailing dot/space, control chars
        raise Refusal(f'"{name}" can\'t be a folder name on Windows.')


def finish_init(setup: Setup) -> None:
    """Steps 5-7: measure, write the marker, then report through the logger (R1.4, R5.5)."""
    sizes, space_line, warnings = measure(setup) if setup.sources else ([], None, [])
    try:
        write_marker(setup.backup, setup.sources)
    except OSError as error:
        if isinstance(error, FileExistsError) and has_marker(setup.backup):
            raise Refusal(
                f"{setup.backup} became a syncplan backup while init was running; nothing changed."
            ) from None
        reason = error.strerror or str(error)
        raise Refusal(
            f"Couldn't write the marker in {setup.backup / MARKER_DIR}: {reason}."
        ) from error
    logger.info("Initialized syncplan backup at %s", setup.backup)
    for line in source_lines(setup.sources, [format_size(size) for size in sizes]):
        logger.info("%s", line)
    if space_line is not None:
        logger.info("%s", space_line)
    for warning in warnings:
        logger.warning("%s", warning)


def _resolve(path: Path) -> Path:
    """Basic cases only (PD10): a drive-letter path in valid text, made absolute (R1.3)."""
    resolved = None if str(path).startswith(("\\\\", "//")) else path.resolve()
    if resolved is None or str(resolved).startswith(("\\\\?\\", "\\\\.\\")):
        raise Refusal(
            f"{path} starts with \\\\, which syncplan doesn't support."
            " Give it with a drive letter instead, for example E:\\Backups."
        )
    if _broken(str(resolved)):
        raise Refusal(
            f"{resolved} has a character that isn't valid text;"
            " rename that folder and run init again."
        )
    return resolved


def _broken(text: str) -> bool:
    """True if text holds a character that isn't valid text (an unpaired surrogate)."""
    try:
        text.encode("utf-16-le")
    except UnicodeEncodeError:
        return True
    return False


def measure(setup: Setup) -> tuple[list[int], str, list[str]]:
    """R2.5-R2.8: each source's size, the space line, and warnings if the drive looks short."""
    block = space.block_size(setup.backup)
    sizes, skipped = [], 0
    for source in setup.sources:
        size, missed = space.folder_size(source.path, block)
        sizes.append(size)
        skipped += missed
    needed = sum(sizes)
    capacity, _, free = disk_usage(setup.backup)
    drive = setup.backup.anchor
    logger.debug("drive %s: capacity %s, free %s", drive, format_size(capacity), format_size(free))
    line = f"Space needed: about {format_size(needed)}. {drive} has {format_size(free)} free."
    if skipped == 1:
        line += " (1 folder couldn't be read and isn't counted)"
    elif skipped:
        line += f" ({skipped} folders couldn't be read and aren't counted)"
    warnings = []
    if needed > free:
        warnings.append(
            f"Warning: about {format_size(needed - free)} short."
            " The first apply will stop before copying unless you free space."
        )
        for folder in setup.adopted:
            try:
                copy = format_size(space.folder_size(folder, block)[0])
            except OSError:
                copy = "size unknown"
            warnings.append(
                f"An existing copy in {folder} ({copy}) may cover part of this;"
                " the first plan gives the exact figure."
            )
    elif (left := free - needed) * _FREE_SHARE < capacity:
        warnings.append(
            f"Note: after the first backup only {format_size(left)} ({left * 100 // capacity}%)"
            f" of {drive} would be free. Overwritten and deleted files are kept in quarantine"
            " on this drive, so it may fill up."
        )
    return sizes, line, warnings


def _check_folder(path: Path) -> None:
    """R2.1 and R3.1: the path must be an existing folder."""
    if not path.exists():
        raise Refusal(f"{path} does not exist.")
    if not path.is_dir():
        raise Refusal(f"{path} is not a folder.")


def _check_backup(backup_dir: Path) -> None:
    """R2.2-R2.4. The folder's own marker comes before the folders above it (D3)."""
    try:
        existing = read_marker(backup_dir)
    except NotInitialized:
        pass
    except MarkerError as error:
        raise Refusal(str(error)) from error  # an OS error behind it gets Details (R5.2)
    else:
        raise Refusal(_already_a_backup(backup_dir, existing))
    for parent in backup_dir.parents:
        if has_marker(parent):
            raise Refusal(f"{backup_dir} is inside the syncplan backup {parent}.")


def _check_source(path: Path, backup_dir: Path) -> None:
    """R3.1, R3.9, R3.6 and R3.2 for one source. Paths are resolved, so links can't hide."""
    _check_folder(path)
    own = log.home().resolve()
    if path == own:
        raise Refusal(f"{path} is syncplan's own folder; it can't be backed up.")
    if path.is_relative_to(own):
        raise Refusal(f"{path} is inside syncplan's own folder ({own}); it can't be backed up.")
    if own.is_relative_to(path):
        raise Refusal(
            f"{path} holds syncplan's own folder ({own}); back up its folders instead,"
            " for example C:\\Users\\you."
        )
    for folder in (path, *path.parents):
        if has_marker(folder):
            message, cause = _backup_as_source(path, folder, backup_dir)
            raise Refusal(message) from cause  # a Windows error behind it gets Details (R5.2)
    if path == backup_dir:
        raise Refusal(f"{path} is both the backup and a source.")
    if backup_dir.is_relative_to(path):
        raise Refusal(f"The backup {backup_dir} is inside the source {path}.")
    if path.is_relative_to(backup_dir):
        raise Refusal(f"The source {path} is inside the backup {backup_dir}.")


def _check_apart(first: Path, second: Path) -> None:
    """R3.3: two sources must not be the same folder or nested."""
    if first == second:
        raise Refusal(f"{second} is given twice.")
    if second.is_relative_to(first):
        raise Refusal(f"{second} is inside another source, {first}.")
    if first.is_relative_to(second):
        raise Refusal(f"{first} is inside another source, {second}.")


def _check_names(named: Sequence[Source]) -> None:
    """R3.4 and R3.5: valid names, no two alike ignoring letter case as Windows does (NFR1)."""
    seen: dict[str, Source] = {}
    for source in named:
        check_name(source.name)
        key = source.name.lower()
        if key in seen:
            raise Refusal(
                f'Two sources would use the same subfolder name "{source.name}":\n'
                f"  {seen[key].path}\n  {source.path}\n"
                f"Name one of them, for example {quoted(f'{source.name}2={source.path}')}"
            )
        seen[key] = source


def _check_subfolder(target: Path, source: Source) -> None:
    """R3.7: an existing subfolder is adopted only if it is a real folder."""
    if target.is_symlink() or target.is_junction():
        problem = "is a link to somewhere else, not a real folder"
    elif target.exists() and not target.is_dir():
        problem = "is not a folder"
    else:
        return
    raise Refusal(
        f"{target} {problem}. Pick another subfolder name,"
        f" for example {quoted(f'{source.name}2={source.path}')}"
    )


def _backup_as_source(path: Path, outer: Path, backup_dir: Path) -> tuple[str, Exception | None]:
    """R3.6: name the backup that holds the source, and suggest its own sources instead.

    Returns the message, and the marker check's error when the marker couldn't be read.
    """
    if path == outer:
        intro = f"{outer} is a syncplan backup."
    else:
        intro = f"{path} is inside the syncplan backup {outer}."
    try:
        sources = read_marker(outer).sources
    except MarkerError as error:
        return f"{intro} {error}", error
    arguments = " ".join(quoted(_argument(s)) for s in sources)
    command = f"syncplan init {quoted(str(backup_dir))} {arguments}"
    return f"{intro} Back up its sources instead:\n  {command}", None


def _argument(source: Source) -> str:
    """How to type a source: NAME= only when needed, and a whole drive as F: (risk K4)."""
    typed = str(source.path) if source.path.name else source.path.drive
    return typed if source.name == source.path.name else f"{source.name}={typed}"


def quoted(text: str) -> str:
    return f'"{text}"' if " " in text else text


def _already_a_backup(backup_dir: Path, existing: Marker) -> str:
    return "\n".join(
        [
            f"{backup_dir} is already a syncplan backup (created {existing.created.date()}).",
            "Its sources:",
            *source_lines(existing.sources),
            "To change them, use syncplan add / syncplan remove.",
        ]
    )


def source_lines(sources: Sequence[Source], sizes: Sequence[str] = ()) -> list[str]:
    """One aligned `NAME <- SOURCE` line per source, with an optional size column."""
    if not sources:
        return ["  (no sources)"]
    name_width = max(len(s.name) for s in sources)
    path_width = max(len(str(s.path)) for s in sources)
    size_width = max((len(size) for size in sizes), default=0)
    lines = []
    for source, size in zip(sources, sizes or [""] * len(sources), strict=True):
        line = f"  {source.name:<{name_width}} <- {source.path!s:<{path_width}}"
        lines.append(f"{line}  {size:>{size_width}}" if sizes else line.rstrip())
    return lines


def _bare_drive(path: Path) -> Path:
    """`E:` alone means the top folder `E:\\`, not Windows' current folder on E: (D8)."""
    if path.drive and not path.root and len(path.parts) == 1:
        return Path(path.drive) / "\\"
    return path
