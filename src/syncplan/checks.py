"""The checks that init, add, remove and plan share: the backup folder, its marker, sources.

Everything here only reads. A check that fails raises Refusal, whose message is for the user.
Only ordinary folders on drives with a letter are supported; anything exotic is refused (PD10).
"""

import logging
import os
import stat
from collections.abc import Sequence
from dataclasses import dataclass
from itertools import combinations
from pathlib import Path

from syncplan import log
from syncplan.marker import (
    MARKER_DIR,
    Marker,
    MarkerError,
    NotInitialized,
    Source,
    has_marker,
    marker_path,
    read_marker,
)

logger = logging.getLogger(__name__)
_FORBIDDEN = frozenset('\\/:*?"<>|')
_MAX_NAME = 255  # UTF-16 units, as Windows counts them
PATH_LIMIT = 260  # Windows' path limit while long paths are off (manage-sources SP6)


class Refusal(Exception):
    """The command won't go ahead. The message says why and is meant for the user."""


@dataclass(frozen=True)
class Setup:
    backup: Path
    sources: tuple[Source, ...]
    adopted: tuple[Path, ...]


def read_argument(text: str) -> tuple[str | None, Path]:
    """Split `NAME=SOURCE` (init-marker D2) and read a bare drive `E:` as `E:\\` (D8)."""
    name = None
    before, equals, after = text.partition("=")
    if equals and not any(char in before for char in "\\/:"):
        name, text = before, after
    return name, _bare_drive(Path(text))


def resolve(text: str, command: str) -> Path:
    """A path as typed: a bare drive is its top folder (init-marker D8), then resolved."""
    return _resolve(_bare_drive(Path(text)), command)


def backup_folder(text: str, command: str) -> Path:
    """init-marker R2.1, R2.9, R2.10: the backup folder, resolved. Refuses a missing or exotic one.

    command is the syncplan command running; refusals name it (manage-sources D7).
    """
    backup_dir = resolve(text, command)
    _check_folder(backup_dir)
    own = backup_dir / MARKER_DIR
    if own.is_symlink() or own.is_junction() or (own.exists() and not own.is_dir()):
        raise Refusal(f"{own} is not a real folder. Remove it, or choose another backup folder.")
    return backup_dir


def checked_marker(backup_dir: Path) -> Marker:
    """The backup's marker (manage-sources R4.2, R4.3), with every name in it usable (D12)."""
    try:
        current = read_marker(backup_dir)
    except NotInitialized:
        raise Refusal(
            f"{backup_dir} is not a syncplan backup."
            f" Set it up first with syncplan init {quoted(str(backup_dir))}."
        ) from None
    except MarkerError as error:
        # An OS error behind it gets Details (init-marker R5.2).
        raise Refusal(str(error)) from error
    path = marker_path(backup_dir)
    seen: set[str] = set()
    for item in (*current.sources, *current.waiting):
        try:
            check_name(item.name)
        except Refusal:
            raise Refusal(
                f'{path} lists the name "{item.name}", which syncplan can\'t use; inspect it.'
            ) from None
        if item.name.lower() in seen:
            raise Refusal(f'{path} lists the name "{item.name}" twice; inspect it.')
        seen.add(item.name.lower())
    logger.debug(
        "backup %s: ok (%d sources, %d waiting)",
        backup_dir,
        len(current.sources),
        len(current.waiting),
    )
    return current


def check_sources(
    backup_dir: Path, arguments: Sequence[str], command: str
) -> tuple[tuple[Source, ...], tuple[Path, ...]]:
    """init-marker steps 3-4 for each SOURCE or NAME=SOURCE (R3.1-R3.9). Nothing is written.

    Returns the sources, named, and the folders already on the backup they adopt (R1.5).
    """
    given = [(name, _resolve(path, command)) for name, path in map(read_argument, arguments)]
    for _, path in given:
        check_source(path, backup_dir, command)
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


def check_source(path: Path, backup_dir: Path, command: str) -> None:
    """init-marker R3.1, R3.9, R3.6, R3.2 for one source. Paths are resolved: links can't hide.

    The backup's own marker is skipped: init has none yet, and for add a source inside the
    backup gets R3.2's message rather than R3.6's (manage-sources D7).
    """
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
        if folder != backup_dir and has_marker(folder):
            message, cause = _backup_as_source(path, folder, backup_dir, command)
            raise Refusal(message) from cause  # a Windows error behind it gets Details (R5.2)
    if path == backup_dir:
        raise Refusal(f"{path} is both the backup and a source.")
    if backup_dir.is_relative_to(path):
        raise Refusal(f"The backup {backup_dir} is inside the source {path}.")
    if path.is_relative_to(backup_dir):
        raise Refusal(f"The source {path} is inside the backup {backup_dir}.")


def subfolder_name(name: str | None, path: Path) -> str:
    """The NAME given, or the resolved folder's own name (init-marker D14). R3.4 for drive roots."""
    if name is not None:
        return name
    if not path.name:
        raise Refusal(
            f"{path} is a whole drive, so it has no folder name. Give it one with NAME=SOURCE,"
            f" for example Drive{path.drive.rstrip(':')}={path.drive}"
        )
    return path.name


def check_name(name: str) -> None:
    """Refuse a subfolder name Windows can't use, or syncplan's own (init-marker R3.5, D15)."""
    if broken(name):
        raise Refusal(f'"{name}" has a character that isn\'t valid text.')
    if name.lower() == MARKER_DIR:
        raise Refusal(f'"{name}" is reserved for syncplan\'s own files.')
    too_long = len(name.encode("utf-16-le")) // 2 > _MAX_NAME
    invalid = name in ("", ".", "..") or too_long or not _FORBIDDEN.isdisjoint(name)
    if invalid or os.path.isreserved(name):  # device names, trailing dot/space, control chars
        raise Refusal(f'"{name}" can\'t be a folder name on Windows.')


def plain_folder(folder: Path, backup_dir: Path, command: str, why: str) -> bool:
    """True for a plain folder, False when nothing is there; refuses anything else.

    One lstat, never exists() or is_dir(): those read any error, even a failing drive, as
    "missing" (manage-sources SP6, D11). why says what a non-folder would get in the way of.
    """
    try:
        info = folder.lstat()
    except FileNotFoundError, NotADirectoryError:
        backup_dir.stat()  # the backup drive itself gone? Then that error ends the run (SP6)
        return False
    reparse = info.st_file_attributes & stat.FILE_ATTRIBUTE_REPARSE_POINT
    if stat.S_ISDIR(info.st_mode) and not reparse:
        return True
    raise Refusal(
        f"{folder} is not a real folder, {why}."
        f" Move it out of {backup_dir}, then run {command} again."
    )


def too_long(path: Path, limit: int = PATH_LIMIT) -> bool:
    """limit or more UTF-16 units, as Windows counts a path (an emoji is two)."""
    return len(str(path).encode("utf-16-le")) // 2 >= limit


def broken(text: str) -> bool:
    """True if text holds a character that isn't valid text (an unpaired surrogate)."""
    try:
        text.encode("utf-16-le")
    except UnicodeEncodeError:
        return True
    return False


def quoted(text: str) -> str:
    return f'"{text}"' if " " in text else text


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


def _resolve(path: Path, command: str) -> Path:
    """Basic cases only (PD10): a drive-letter path in valid text, made absolute (R1.3)."""
    resolved = None if str(path).startswith(("\\\\", "//")) else path.resolve()
    if resolved is None or str(resolved).startswith(("\\\\?\\", "\\\\.\\")):
        raise Refusal(
            f"{path} starts with \\\\, which syncplan doesn't support."
            " Give it with a drive letter instead, for example E:\\Backups."
        )
    if broken(str(resolved)):
        raise Refusal(
            f"{resolved} has a character that isn't valid text;"
            f" rename that folder and run {command} again."
        )
    return resolved


def _check_folder(path: Path) -> None:
    """init-marker R2.1 and R3.1: the path must be an existing folder."""
    if not path.exists():
        raise Refusal(f"{path} does not exist.")
    if not path.is_dir():
        raise Refusal(f"{path} is not a folder.")


def _check_apart(first: Path, second: Path) -> None:
    """init-marker R3.3: two sources must not be the same folder or nested."""
    if first == second:
        raise Refusal(f"{second} is given twice.")
    if second.is_relative_to(first):
        raise Refusal(f"{second} is inside another source, {first}.")
    if first.is_relative_to(second):
        raise Refusal(f"{first} is inside another source, {second}.")


def _check_names(named: Sequence[Source]) -> None:
    """init-marker R3.4, R3.5: valid names, no two alike ignoring letter case (NFR1)."""
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
    """init-marker R3.7: an existing subfolder is adopted only if it is a real folder."""
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


def _backup_as_source(
    path: Path, outer: Path, backup_dir: Path, command: str
) -> tuple[str, Exception | None]:
    """init-marker R3.6: name the backup that holds the source, and suggest its own sources instead.

    Returns the message, and the marker check's error when the marker couldn't be read.
    plan takes one listed source, so it gets no suggestion (plan-copies R7.5).
    """
    if path == outer:
        intro = f"{outer} is a syncplan backup."
    else:
        intro = f"{path} is inside the syncplan backup {outer}."
    if command == "plan":
        return f"{intro} A backup can't be backed up.", None
    try:
        sources = read_marker(outer).sources
    except MarkerError as error:
        return f"{intro} {error}", error
    arguments = " ".join(quoted(_argument(s)) for s in sources)
    instead = f"syncplan {command} {quoted(str(backup_dir))} {arguments}"
    return f"{intro} Back up its sources instead:\n  {instead}", None


def _argument(source: Source) -> str:
    """How to type a source: NAME= only when needed, and a whole drive as F: (init-marker K4)."""
    typed = str(source.path) if source.path.name else source.path.drive
    return typed if source.name == source.path.name else f"{source.name}={typed}"


def _bare_drive(path: Path) -> Path:
    """`E:` alone means the top folder `E:\\`, not Windows' current folder on E: (D8)."""
    if path.drive and not path.root and len(path.parts) == 1:
        return Path(path.drive) / "\\"
    return path
