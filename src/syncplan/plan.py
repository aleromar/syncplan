"""`syncplan plan`: compare one source with its copy on the backup, and write a plan.

Everything is checked before anything is read. Nothing is written but the plan file, which
replaces the previous plan for that source in one step (plan-copies D8).
"""

import dataclasses
import logging
import stat
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from syncplan import space
from syncplan.checks import (
    PATH_LIMIT,
    Refusal,
    backup_folder,
    check_source,
    checked_marker,
    plain_folder,
    quoted,
    resolve,
    source_lines,
    too_long,
)
from syncplan.compare import key
from syncplan.marker import MARKER_DIR, Source, has_marker

logger = logging.getLogger(__name__)
FOLDER_LIMIT = 248  # Windows can't make a folder whose path is this long (SP4)


@dataclass(frozen=True)
class Target:
    backup: Path
    source: Source  # the name it is listed under, and its folder, resolved
    overwrite: dict[str, Path]  # the --overwrite files, by key (R3.3)

    @property
    def copy(self) -> Path:
        return self.backup / self.source.name

    @property
    def plan_file(self) -> Path:
        return self.backup / MARKER_DIR / "plans" / f"{self.source.name}.json"


def check_plan(backup: str, source: str, overwrite: Sequence[str]) -> Target:
    """Steps 1-6: refuse anything unsafe before anything is read (R7, R3.3)."""
    backup_dir = backup_folder(backup, "plan")
    current = checked_marker(backup_dir)
    for parent in backup_dir.parents:  # R7.1, as init-marker R2.4
        if has_marker(parent):
            raise Refusal(f"{backup_dir} is inside the syncplan backup {parent}.")
    path = resolve(source, "plan")
    listed = next((s for s in current.sources if s.path == path), None)
    if listed is None:
        heading = f"{backup_dir} is not a backup of {path}. Its sources:"
        raise Refusal("\n".join([heading, *source_lines(current.sources)]))
    target = Target(backup_dir, Source(listed.name, path), {})
    _check_there(target)
    check_source(path, backup_dir, "plan")
    logger.debug("source %s: listed as %s, there, apart from the backup", path, listed.name)
    _check_copy(target)
    plans = target.plan_file.parent
    plain_folder(plans, backup_dir, "plan", "so syncplan can't write its plan there")
    root, kind = space.filesystem(backup_dir)
    if kind.upper() != "NTFS":
        raise Refusal(
            f"{root} is formatted as {kind}. syncplan backs up only to NTFS drives,"
            " which keep file dates exactly. Use an NTFS drive."
        )
    chosen = {}
    for text in overwrite:
        chosen_path = _chosen(text, path)
        chosen[key(str(chosen_path.relative_to(path)))] = chosen_path
    return dataclasses.replace(target, overwrite=chosen)


def _check_there(target: Target) -> None:
    """R7.4: a listed source whose folder is gone is refused until deletions arrive (F4)."""
    try:
        target.source.path.stat()
    except FileNotFoundError, NotADirectoryError:
        raise Refusal(
            f"{target.source.path} doesn't exist: it was deleted, or its drive is unplugged.\n"
            "Plug the drive in, or stop backing it up with"
            f" syncplan remove {quoted(str(target.backup))} {quoted(target.source.name)}."
        ) from None


def _check_copy(target: Target) -> None:
    """R7.6: the copy's folder is a plain folder or missing, not a backup, and short enough."""
    copy = target.copy
    shorter = (
        f"Stop backing it up with syncplan remove {quoted(str(target.backup))}"
        f" {quoted(target.source.name)}, then add it again with a shorter NAME=SOURCE."
    )
    if too_long(copy, FOLDER_LIMIT):
        raise Refusal(
            f"{copy} would be a path of {FOLDER_LIMIT} characters or more,"
            f" too long for Windows. {shorter}"
        )
    if too_long(target.plan_file):
        raise Refusal(
            f"The plan for {target.source.name} would be {target.plan_file}, a path of"
            f" {PATH_LIMIT} characters or more, too long for Windows. {shorter}"
        )
    why = "so syncplan can't plan copies into it"
    if plain_folder(copy, target.backup, "plan", why) and has_marker(copy):
        raise Refusal(
            f"{copy} is a syncplan backup itself, {why}."
            f" Move it out of {target.backup}, then run plan again."
        )
    logger.debug("copy %s: ok", copy)


def _chosen(text: str, source: Path) -> Path:
    """R3.3, before anything is read: an --overwrite FILE must be a plain file in the source."""
    path = resolve(text, "plan")
    try:
        info = path.lstat()
    except OSError:
        info = None
    plain = (
        info is not None
        and stat.S_ISREG(info.st_mode)
        and not info.st_file_attributes & stat.FILE_ATTRIBUTE_REPARSE_POINT
    )
    if not plain or not path.is_relative_to(source):
        raise Refusal(f"{path} isn't a file in {source}.")
    return path
