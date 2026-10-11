"""`syncplan init`: turn an existing folder into a backup of the given source folders.

Everything is read and checked first, and the marker is the only thing written (design D3).
"""

from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

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
    named = []
    for name, path in (read_argument(text) for text in sources):
        path = path.resolve()
        named.append(Source(path.name if name is None else name, path))
    adopted = tuple(root / s.name for s in named if (root / s.name).is_dir())
    return Setup(root, tuple(named), adopted)


def finish_init(setup: Setup) -> list[str]:
    """Steps 5-7: write the marker and return the report lines."""
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
    return [f"Initialized syncplan backup at {setup.backup}", *source_lines(setup.sources)]


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
