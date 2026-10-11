"""`syncplan init`: turn an existing folder into a backup of the given source folders.

Everything is read and checked first, and the marker is the only thing written (design D3).
"""

import os
from collections.abc import Sequence
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from shutil import disk_usage

from syncplan import space
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

_FORBIDDEN = frozenset('\\/:*?"<>|')


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
    root = _bare_drive(Path(backup)).resolve()
    _check_backup(root)
    given = [(name, path.resolve()) for name, path in map(read_argument, sources)]
    for _, path in given:
        _check_source(path, root)
    for index, (_, first) in enumerate(given):
        for _, second in given[index + 1 :]:
            _check_apart(first, second)
    named = [Source(subfolder_name(name, path), path) for name, path in given]
    seen: dict[str, Source] = {}
    for source in named:
        check_name(source.name)
        if (clash := seen.setdefault(source.name.casefold(), source)) is not source:
            raise Refusal(
                f'Two sources would use the same subfolder name "{source.name}":\n'
                f"  {clash.path}\n  {source.path}\n"
                f"Name one of them, for example {source.name}2={source.path}"
            )
    for source in named:
        _check_subfolder(root / source.name, source)
    adopted = tuple(root / s.name for s in named if (root / s.name).is_dir())
    return Setup(root, tuple(named), adopted)


def subfolder_name(name: str | None, path: Path) -> str:
    """The NAME given, or the resolved folder's own name (design D14). R3.4 for drive roots."""
    if name is not None:
        return name
    if not path.name:
        raise Refusal(
            f"{path} is a whole drive, so it has no folder name. Give it one with NAME=SOURCE,"
            f" for example Drive{path.drive.rstrip(':')}={path}"
        )
    return path.name


def check_name(name: str) -> None:
    """Refuse a subfolder name Windows can't use, or syncplan's own (R3.5, design D15)."""
    if name.casefold() == MARKER_DIR:
        raise Refusal(f'"{name}" is reserved for syncplan\'s own files.')
    too_long = len(name.encode("utf-16-le")) // 2 > 255  # Windows counts UTF-16 units
    if (
        name in ("", ".", "..")
        or too_long
        or not _FORBIDDEN.isdisjoint(name)
        or (
            os.path.isreserved(name)  # device names, a trailing dot or space, control characters
        )
    ):
        raise Refusal(f'"{name}" can\'t be a folder name on Windows.')


def finish_init(setup: Setup) -> list[str]:
    """Steps 5-7: measure, write the marker, and return the report lines."""
    sizes, space_lines = _measure(setup) if setup.sources else ([], [])
    try:
        write_marker(setup.backup, setup.sources)
    except FileExistsError:
        if not has_marker(setup.backup):
            raise
        raise Refusal(
            f"{setup.backup} became a syncplan backup while init was running; nothing changed."
        ) from None
    except OSError as error:
        reason = error.strerror or str(error)
        raise Refusal(
            f"Couldn't write the marker in {setup.backup / MARKER_DIR}: {reason}."
        ) from error
    return [
        f"Initialized syncplan backup at {setup.backup}",
        *source_lines(setup.sources, [format_size(size) for size in sizes]),
        *space_lines,
    ]


def source_lines(sources: Sequence[Source], sizes: Sequence[str] = ()) -> list[str]:
    """One aligned `NAME <- SOURCE` line per source, with an optional size column."""
    if not sources:
        return ["  (no sources)"]
    name_width = max(len(s.name) for s in sources)
    path_width = max(len(str(s.path)) for s in sources)
    size_width = max((len(size) for size in sizes), default=0)
    lines = []
    for index, source in enumerate(sources):
        line = f"  {source.name:<{name_width}} <- {source.path}"
        if sizes:
            line = f"{line:<{name_width + path_width + 6}}  {sizes[index]:>{size_width}}"
        lines.append(line)
    return lines


def _measure(setup: Setup) -> tuple[list[int], list[str]]:
    """R2.5-R2.8: the space each source needs, and a warning if the drive looks short."""
    block = space.block_size(setup.backup)
    sizes, skipped = [], 0
    for source in setup.sources:
        size, missed = space.folder_size(source.path, block)
        sizes.append(size)
        skipped += missed
    needed = sum(sizes)
    capacity, _, free = disk_usage(setup.backup)
    drive = setup.backup.anchor
    line = f"Space needed: about {format_size(needed)}. {drive} has {format_size(free)} free."
    if skipped == 1:
        line += " (1 folder couldn't be read and isn't counted)"
    elif skipped:
        line += f" ({skipped} folders couldn't be read and aren't counted)"
    lines = [line]
    if needed > free:
        lines.append(
            f"Warning: about {format_size(needed - free)} short."
            " The first apply will stop before copying unless you free space."
        )
        for folder in setup.adopted:
            with suppress(OSError):
                copy = format_size(space.folder_size(folder, block)[0])
                lines.append(
                    f"An existing copy in {folder} ({copy}) may cover part of this;"
                    " the first plan gives the exact figure."
                )
    elif (left := free - needed) * 10 < capacity:
        lines.append(
            f"Note: after the first backup only {format_size(left)} ({left * 100 // capacity}%)"
            f" of {drive} would be free. Overwritten and deleted files are kept in quarantine"
            " on this drive, so it may fill up."
        )
    return sizes, lines


def _check_backup(root: Path) -> None:
    """R2.1-R2.4. The folder's own marker comes before the folders above it (design D3)."""
    if not root.exists():
        raise Refusal(f"{root} does not exist.")
    if not root.is_dir():
        raise Refusal(f"{root} is not a folder.")
    try:
        existing = read_marker(root)
    except NotInitialized:
        pass
    except MarkerError as error:
        raise Refusal(str(error)) from None
    else:
        raise Refusal(_already_a_backup(root, existing))
    for parent in root.parents:
        if has_marker(parent):
            raise Refusal(f"{root} is inside the syncplan backup {parent}.")


def _check_source(path: Path, root: Path) -> None:
    """R3.1, R3.6 and R3.2 for one source. Paths are already resolved, so links can't hide."""
    if not path.exists():
        raise Refusal(f"{path} does not exist.")
    if not path.is_dir():
        raise Refusal(f"{path} is not a folder.")
    for folder in (path, *path.parents):
        if has_marker(folder):
            raise Refusal(_backup_as_source(path, folder, root))
    if path == root:
        raise Refusal(f"{path} is both the backup and a source.")
    if root.is_relative_to(path):
        raise Refusal(f"The backup {root} is inside the source {path}.")
    if path.is_relative_to(root):
        raise Refusal(f"The source {path} is inside the backup {root}.")


def _check_apart(first: Path, second: Path) -> None:
    """R3.3: two sources must not be the same folder or nested."""
    if first == second:
        raise Refusal(f"{second} is given twice.")
    if second.is_relative_to(first):
        raise Refusal(f"{second} is inside another source, {first}.")
    if first.is_relative_to(second):
        raise Refusal(f"{first} is inside another source, {second}.")


def _check_subfolder(target: Path, source: Source) -> None:
    """R3.7: an existing subfolder is adopted only if it is a real folder."""
    if target.is_symlink() or target.is_junction():
        problem = "is a link, not a folder"
    elif target.exists() and not target.is_dir():
        problem = "is not a folder"
    else:
        return
    raise Refusal(
        f"{target} {problem}. Pick another subfolder name, for example {source.name}2={source.path}"
    )


def _backup_as_source(path: Path, backup: Path, root: Path) -> str:
    """R3.6: name the backup and suggest its own sources instead."""
    if path == backup:
        intro = f"{backup} is a syncplan backup."
    else:
        intro = f"{path} is inside the syncplan backup {backup}."
    try:
        sources = read_marker(backup).sources
    except MarkerError as error:
        return f"{intro} {error}"
    arguments = " ".join(_quoted(_argument(s)) for s in sources)
    return f"{intro} Back up its sources instead:\n  syncplan init {_quoted(str(root))} {arguments}"


def _argument(source: Source) -> str:
    """The command-line form of a source: NAME=SOURCE only when the name isn't the default."""
    return str(source.path) if source.name == source.path.name else f"{source.name}={source.path}"


def _quoted(text: str) -> str:
    return f'"{text}"' if " " in text else text


def _already_a_backup(root: Path, existing: Marker) -> str:
    return "\n".join(
        [
            f"{root} is already a syncplan backup (created {existing.created.date()}).",
            "Its sources:",
            *source_lines(existing.sources),
            "To change them, use syncplan add / syncplan remove.",
        ]
    )


def _bare_drive(path: Path) -> Path:
    """`E:` alone means the top folder `E:\\`, not Windows' current folder on E: (D8)."""
    if path.drive and not path.root and len(path.parts) == 1:
        return Path(path.drive) / "\\"
    return path
