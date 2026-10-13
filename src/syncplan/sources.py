"""`syncplan add` and `syncplan remove`: change the source list of an existing backup.

Everything is read and checked first, and the marker is the only thing written, replaced
in one step (design D2). A removed source's folder stays where it is: the marker records
it as waiting for the quarantine, which a later plan carries out (R3).
"""

import logging
import stat
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from syncplan.init import (
    Refusal,
    Setup,
    backup_folder,
    check_name,
    check_sources,
    measure,
    quoted,
    read_argument,
    source_lines,
)
from syncplan.marker import (
    MARKER_DIR,
    MARKER_NAME,
    Marker,
    MarkerChanged,
    MarkerError,
    NotInitialized,
    Source,
    Waiting,
    read_marker,
    replace_marker,
)
from syncplan.space import format_size

logger = logging.getLogger(__name__)
_TOO_LONG = 260  # Windows' path limit while long paths are off (SP6); longer paths are F8's
_REPARSE_POINT = stat.FILE_ATTRIBUTE_REPARSE_POINT  # a symbolic link or a junction


@dataclass(frozen=True)
class Addition:
    backup: Path
    marker: Marker
    added: tuple[Source, ...]
    adopted: tuple[Path, ...]  # folders already on the backup taken as copies (R1.4, R3.3)
    reclaimed: tuple[Waiting, ...]  # waiting folders whose source comes back (R3.3)


def check_add(backup: str, arguments: Sequence[str]) -> Addition:
    """Steps 1-5 of add: refuse anything unsafe before measuring. Nothing is written."""
    backup_dir = backup_folder(backup, "add")
    current = _read(backup_dir)
    added, adopted = check_sources(backup_dir, arguments, "add")
    for source in added:
        _check_against_listed(source, current.sources, backup_dir)
    reclaimed = []
    for source in added:
        if (old := _named(current.waiting, source.name)) is None:
            continue
        if old.path != source.path:
            raise Refusal(
                f"{backup_dir / old.name} holds the old copy of {old.path} and is waiting for"
                " the quarantine. Pick another name, for example"
                f" {quoted(f'{source.name}2={source.path}')}, or apply a plan first."
            )
        reclaimed.append(old)
    logger.debug("listed sources and waiting folders: ok")
    return Addition(backup_dir, current, added, adopted, tuple(reclaimed))


def finish_add(addition: Addition) -> None:
    """Steps 6-8 of add: measure, replace the marker, then report (R1.2-R1.4, R3.3)."""
    backup = addition.backup
    sizes, space_line, warnings = measure(Setup(backup, addition.added, addition.adopted))
    sources = [*addition.marker.sources, *addition.added]
    waiting = [w for w in addition.marker.waiting if w not in addition.reclaimed]
    _save(backup, addition.marker, sources, waiting)
    logger.info("Added to syncplan backup %s:", backup)
    for line in source_lines(addition.added, [format_size(size) for size in sizes]):
        logger.info("%s", line)
    for source in addition.added:
        folder = backup / source.name
        if folder not in addition.adopted:
            continue
        if _named(addition.reclaimed, source.name):
            logger.info(
                "%s was waiting for the quarantine; it is kept and backs up %s again.",
                folder,
                source.path,
            )
        else:
            logger.info(
                "%s already exists and becomes the copy of %s;"
                " the first plan shows what changes in it.",
                folder,
                source.path,
            )
    logger.info("%s", space_line)
    for warning in warnings:
        logger.warning("%s", warning)
    _report_list(backup, sources, waiting)


def remove(backup: str, names: Sequence[str]) -> None:
    """Steps 1-6 of remove (R2, R3). Nothing is written until every check has passed."""
    backup_dir = backup_folder(backup, "remove")
    current = _read(backup_dir)
    removed: list[Source] = []
    for name in names:
        if _named(removed, name) is not None:
            raise Refusal(f'"{name}" is given twice.')
        if (source := _named(current.sources, name)) is None:
            raise Refusal(_not_listed(name, backup_dir, current.sources))
        removed.append(source)
    now = datetime.now().astimezone().replace(microsecond=0)
    copies = [s for s in removed if _has_copy(backup_dir / s.name, backup_dir)]
    sources = [s for s in current.sources if s not in removed]
    waiting = [*current.waiting, *(Waiting(s.name, s.path, now) for s in copies)]
    _save(backup_dir, current, sources, waiting)
    logger.info("Removed from syncplan backup %s:", backup_dir)
    for line in source_lines(removed):
        logger.info("%s", line)
    for source in removed:
        if source in copies:
            logger.info(
                "The next plan will move %s to the quarantine;"
                " nothing moves until you apply that plan.",
                backup_dir / source.name,
            )
        else:
            logger.info(
                "%s held no copy of %s, so nothing waits for the quarantine.",
                backup_dir,
                source.name,
            )
    _report_list(backup_dir, sources, waiting)


def _read(backup_dir: Path) -> Marker:
    """Step 2: the backup's marker (R4.2, R4.3), with every name in it usable (D12)."""
    try:
        current = read_marker(backup_dir)
    except NotInitialized:
        raise Refusal(
            f"{backup_dir} is not a syncplan backup."
            f" Set it up first with syncplan init {quoted(str(backup_dir))}."
        ) from None
    except MarkerError as error:
        raise Refusal(str(error)) from error  # an OS error behind it gets Details (R5.2)
    path = backup_dir / MARKER_DIR / MARKER_NAME
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


def _check_against_listed(source: Source, listed: Sequence[Source], backup_dir: Path) -> None:
    """R5.2-R5.4: a new source must not repeat, nest with, or take the name of a listed one."""
    for old in listed:
        where = f"already a source of {backup_dir} (as {old.name})"
        if source.path == old.path:
            raise Refusal(f"{source.path} is {where}.")
        if source.path.is_relative_to(old.path):
            raise Refusal(f"{source.path} is inside {old.path}, {where}.")
        if old.path.is_relative_to(source.path):
            raise Refusal(f"{source.path} holds {old.path}, {where}.")
    if (old := _named(listed, source.name)) is not None:
        raise Refusal(
            f'{backup_dir} already has a source named "{old.name}" ({old.path}).'
            f" Name the new one, for example {quoted(f'{source.name}2={source.path}')}"
        )


def _not_listed(name: str, backup_dir: Path, sources: Sequence[Source]) -> str:
    """R2.4's refusal. A listed folder given instead of its name gets that name (D5)."""
    if any(char in name for char in "\\/:"):
        path = read_argument(name)[1].resolve()  # read as add reads it: E: means E:\ (D8)
        for source in sources:
            if source.path == path:
                return (
                    f"remove takes subfolder names, not folders. {source.path} is listed as"
                    f" {source.name}:\n"
                    f"  syncplan remove {quoted(str(backup_dir))} {quoted(source.name)}"
                )
    heading = f'{backup_dir} has no source named "{name}". Its sources:'
    return "\n".join([heading, *source_lines(sources)])


def _has_copy(folder: Path, backup_dir: Path) -> bool:
    """D11: True for a plain folder, False when nothing is there; refuses anything else (R3.5).

    One lstat, never exists() or is_dir(): those read any error, even a failing drive, as
    "missing" (SP6), and the old copy would then drop off the record.
    """
    move = f"Move it out of {backup_dir}, then run remove again."
    if len(str(folder)) >= _TOO_LONG:
        raise Refusal(
            f"{folder} is a path of 260 characters or more, too long for syncplan to check. {move}"
        )
    try:
        info = folder.lstat()
    except FileNotFoundError, NotADirectoryError:
        backup_dir.stat()  # the backup drive itself gone? Then that error ends the run (SP6)
        logger.debug("subfolder %s: missing, so nothing waits", folder)
        return False
    if stat.S_ISDIR(info.st_mode) and not info.st_file_attributes & _REPARSE_POINT:
        logger.debug("subfolder %s: a plain folder, waiting for the quarantine", folder)
        return True
    raise Refusal(f"{folder} is not a real folder, so it can't wait for the quarantine. {move}")


def _save(
    backup_dir: Path, old: Marker, sources: Sequence[Source], waiting: Sequence[Waiting]
) -> None:
    """Replace the marker (R6). A change made meanwhile, or a failed write, is a refusal."""
    try:
        replace_marker(backup_dir, old, sources, waiting)
    except MarkerChanged as error:
        raise Refusal(str(error)) from None
    except OSError as error:
        reason = error.strerror or str(error)
        raise Refusal(
            f"Couldn't save the source list in {backup_dir / MARKER_DIR}: {reason}."
        ) from error


def _report_list(backup_dir: Path, sources: Sequence[Source], waiting: Sequence[Waiting]) -> None:
    """The end of every report: the full list after the change (R1.2, R2.2)."""
    logger.info("Sources of %s now:", backup_dir)
    for line in source_lines(sources):
        logger.info("%s", line)
    if waiting:
        logger.info("Waiting for the quarantine:")
        for item in waiting:
            logger.info("  %s (old copy of %s)", item.name, item.path)


def _named[T: (Source, Waiting)](items: Sequence[T], name: str) -> T | None:
    """The item with this subfolder name, ignoring letter case as Windows does (NFR1)."""
    return next((item for item in items if item.name.lower() == name.lower()), None)
