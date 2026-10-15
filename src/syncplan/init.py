"""`syncplan init`: turn an existing folder into a backup of the given source folders.

Everything is read and checked first, and the marker is the only thing written (design D3).
Only ordinary folders on drives with a letter are supported; anything exotic is refused (PD10).
"""

import logging
from collections.abc import Sequence
from pathlib import Path

from syncplan.checks import (
    Refusal,
    Setup,
    backup_folder,
    check_not_inside_backup,
    check_sources,
    source_lines,
)
from syncplan.marker import (
    MARKER_DIR,
    Marker,
    MarkerError,
    NotInitialized,
    has_marker,
    read_marker,
    write_marker,
)
from syncplan.space import format_size, measure

logger = logging.getLogger(__name__)


def check_init(backup: str, sources: Sequence[str]) -> Setup:
    """Steps 1-4: resolve every path, then refuse anything unsafe. Nothing is written."""
    backup_dir = backup_folder(backup, "init")
    _check_backup(backup_dir)
    logger.debug("backup %s: ok (exists, no marker, no backup above)", backup_dir)
    named, adopted = check_sources(backup_dir, sources, "init")
    return Setup(backup_dir, named, adopted)


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
    check_not_inside_backup(backup_dir)


def _already_a_backup(backup_dir: Path, existing: Marker) -> str:
    return "\n".join(
        [
            f"{backup_dir} is already a syncplan backup (created {existing.created.date()}).",
            "Its sources:",
            *source_lines(existing.sources),
            "To change them, use syncplan add / syncplan remove.",
        ]
    )
