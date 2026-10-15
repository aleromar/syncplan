"""`syncplan plan`: compare one source with its copy on the backup, and write a plan.

Everything is checked before anything is read. Nothing is written but the plan file, which
replaces the previous plan for that source in one step (plan-copies D8).
"""

import json
import logging
import stat
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

from syncplan import space
from syncplan.checks import (
    FOLDER_LIMIT,
    PATH_LIMIT,
    Refusal,
    backup_folder,
    check_not_inside_backup,
    check_source,
    checked_marker,
    plain_folder,
    quoted,
    reason,
    resolve,
    source_lines,
    too_long,
)
from syncplan.compare import Action, Comparison, Decision, Seen, compare, key
from syncplan.marker import MARKER_DIR, Source, has_marker, write_through_temp
from syncplan.space import format_size, rounded

logger = logging.getLogger(__name__)
FORMAT = 1
SHOWN = 50  # items per list on screen; the plan file has them all (R5.4)
_TEMPORARY = "plan-XXXXXXXX.tmp"  # as long as the name write_through_temp gives its file
_KINDS = ("copy", "overwrite", "set date", "make folder")
_EPOCH = datetime(1970, 1, 1, tzinfo=UTC)
_NS = 1_000_000_000


@dataclass(frozen=True)
class Target:
    backup: Path
    source: Source  # as the marker lists it: its spelling goes into the plan (R4.2)
    drive: str  # the root of the backup's volume, for the space line (D9)
    overwrite: dict[str, Path]  # the --overwrite files, by key (R3.3)

    @property
    def copy(self) -> Path:
        return self.backup / self.source.name

    @property
    def plan_file(self) -> Path:
        return _plan_file(self.backup, self.source.name)


def check_plan(backup: str, source: str, overwrite: Sequence[str]) -> Target:
    """Steps 1-6: refuse anything unsafe before anything is read (R7, R3.3)."""
    backup_dir = backup_folder(backup, "plan")
    check_not_inside_backup(backup_dir)  # R7.1, before the marker is read
    current = checked_marker(backup_dir)
    path = resolve(source, "plan")
    listed = next((s for s in current.sources if s.path == path), None)
    if listed is None:
        heading = f"{backup_dir} is not a backup of {path}. Its sources:"
        raise Refusal("\n".join([heading, *source_lines(current.sources)]))
    _check_there(backup_dir, listed)
    check_source(path, backup_dir, "plan")
    logger.debug("source %s: listed as %s, there, apart from the backup", path, listed.name)
    _check_copy(backup_dir, listed)
    _check_plans(_plan_file(backup_dir, listed.name).parent)
    drive, kind = space.filesystem(backup_dir)
    if kind.upper() != "NTFS":
        raise Refusal(
            f"{drive} is formatted as {kind}. syncplan backs up only to NTFS drives,"
            " which keep file dates exactly. Use an NTFS drive."
        )
    chosen = {}
    for text in overwrite:
        chosen_path = _chosen(text, listed.path)
        chosen[key(str(chosen_path.relative_to(listed.path)))] = chosen_path
    return Target(backup_dir, listed, drive, chosen)


def finish_plan(target: Target, quick: bool) -> None:
    """Steps 7-11: compare, write the plan file, then report (R1.1, R4, R5)."""
    result = compare(target.source.path, target.copy, quick, target.overwrite)
    needed, free = _space(target, result)  # asked before the swap: nothing fails after it
    _write(target, _plan_text(target, quick, result))
    _report(target, quick, result, needed, free)


def _space(target: Target, result: Comparison) -> tuple[int, int]:
    """R5.2: the space to copy, in whole blocks, and the backup drive's free space."""
    block = space.block_size(target.backup)
    copied = [a.source.size for a in result.actions if a.do in ("copy", "overwrite")]
    return sum(rounded(size, block) for size in copied), space.disk_usage(target.backup)[2]


def _write(target: Target, text: str) -> None:
    """R4.1, R4.4, R4.5: replace the plan file in one step, or keep the previous one."""
    plans = target.plan_file.parent
    try:
        try:
            plans.mkdir()
            made = True
        except FileExistsError:
            made = False
        write_through_temp(plans, text, lambda temp: temp.replace(target.plan_file), made, "plan-")
    except OSError as error:
        raise Refusal(f"Couldn't write the plan in {plans}: {reason(error)}") from error
    logger.debug("plan written: %s", target.plan_file)


def _plan_text(target: Target, quick: bool, result: Comparison) -> str:
    """The plan file: JSON with every entry of its lists on a line of its own (D8)."""
    fields = {
        "syncplan_plan": FORMAT,
        "created": datetime.now().astimezone().replace(microsecond=0).isoformat(),
        "backup": str(target.backup),
        "name": target.source.name,
        "source": str(target.source.path),
        "quick": quick,
    }
    lists = {
        "actions": [_action_entry(action) for action in result.actions],
        "chosen": [_decision_entry(decision) for decision in result.chosen],
        "decide": [_decision_entry(decision) for decision in result.decide],
        "skipped": [{"path": item.path, "reason": item.reason} for item in result.skipped],
    }
    parts = [f"  {_json(name)}: {_json(value)}" for name, value in fields.items()]
    for name, entries in lists.items():
        inner = ",\n".join(f"    {_json(entry)}" for entry in entries)
        parts.append(f"  {_json(name)}: [\n{inner}\n  ]" if entries else f"  {_json(name)}: []")
    return "{\n" + ",\n".join(parts) + "\n}\n"


def _action_entry(action: Action) -> dict:
    entry: dict = {"do": action.do, "path": action.path}
    for side, seen in (("source", action.source), ("backup", action.backup)):
        if seen is not None:
            entry[side] = _seen_entry(seen)
    return entry


def _decision_entry(decision: Decision) -> dict:
    entry = {
        "path": decision.path,
        "case": decision.case,
        "source": _seen_entry(decision.source),
        "backup": _seen_entry(decision.backup),
    }
    if decision.reason:
        entry["reason"] = decision.reason
    return entry


def _seen_entry(seen: Seen) -> dict:
    return {"size": seen.size, "date": _plan_date(seen.date)}


def _plan_date(date: int) -> str:
    """A date in UTC with 7 decimals: exact to NTFS's 100 ns, before 1970 too (D8, SP10)."""
    seconds, rest = divmod(date, _NS)
    return f"{_EPOCH + timedelta(seconds=seconds):%Y-%m-%dT%H:%M:%S}.{rest // 100:07d}Z"


def _json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False)


def _report(target: Target, quick: bool, result: Comparison, needed: int, free: int) -> None:
    """R5.1-R5.4: the counts, the space to copy, the lists, and where the plan is."""
    logger.info("Plan for %s (%s -> %s):", target.source.name, target.source.path, target.copy)
    if quick:
        logger.info("Quick plan: sizes and dates only; damaged files can't be spotted.")
    done = {kind: [a for a in result.actions if a.do == kind] for kind in _KINDS}
    rows = [
        ("copy (new)", len(done["copy"]), "file", _size(done["copy"])),
        ("overwrite", len(done["overwrite"]), "file", _size(done["overwrite"])),
        ("set date", len(done["set date"]), "file", ""),
        ("make folder", len(done["make folder"]), "folder", ""),
        ("unchanged", result.unchanged, "file", ""),
    ]
    width = max(len(f"{count:,}") for _, count, _, _ in rows)
    for label, count, unit, size in rows:
        units = unit if count == 1 else f"{unit}s"
        logger.info("%s", f"  {label:<12}{count:>{width},} {units:<7}  {size}".rstrip())
    logger.info(
        "To copy: %s. %s has %s free.", format_size(needed), target.drive, format_size(free)
    )
    if needed > free:
        logger.warning(
            "Warning: about %s short. Apply will stop before copying unless you free space.",
            format_size(needed - free),
        )
    _listed(
        logging.INFO,
        "Overwriting, as you chose (the old copies go to the quarantine):",
        [_decision_lines(decision) for decision in result.chosen],
    )
    _listed(
        logging.WARNING,
        "Needs your decision (nothing planned for these):",
        [_decision_lines(decision) for decision in result.decide],
    )
    _listed(
        logging.WARNING,
        "Skipped (nothing planned for these):",
        [[f"  {item.path}  {item.reason}"] for item in result.skipped],
    )
    if result.extra_files or result.extra_folders:
        logger.info(
            "On the backup but no longer in %s: %s, %s (left as they are for now).",
            target.source.path,
            _count(result.extra_files, "file"),
            _count(result.extra_folders, "folder"),
        )
    if not (result.actions or result.decide or result.skipped):
        logger.info("Nothing to do: %s is up to date.", target.copy)
    logger.info("Plan written to %s", target.plan_file)
    logger.info("Nothing has been changed.")


def _listed(level: int, heading: str, items: list[list[str]]) -> None:
    """R5.4: a heading and up to SHOWN items; the rest go to the log only (NFR2)."""
    if not items:
        return
    logger.log(level, "%s", heading)
    for number, lines in enumerate(items):
        for line in lines:
            logger.log(level if number < SHOWN else logging.DEBUG, "%s", line)
    if len(items) > SHOWN:
        more = f"{len(items) - SHOWN:,}"
        logger.log(level, "  ... and %s more (all in the plan file)", more)


def _decision_lines(decision: Decision) -> list[str]:
    case = f"{decision.case}: {decision.reason}" if decision.reason else decision.case
    sides = " | ".join(
        f"{side} {_shown(seen.date)}  {format_size(seen.size)}"
        for side, seen in (("source", decision.source), ("backup", decision.backup))
    )
    return [f"  {decision.path}  {case}", f"    {sides}"]


def _shown(date: int) -> str:
    """A date on screen: local time to the second, or UTC where Windows can't (SP10)."""
    try:
        return f"{datetime.fromtimestamp(date // _NS):%d %b %Y %H:%M:%S}"
    except OSError, OverflowError, ValueError:
        return f"{_EPOCH + timedelta(seconds=date // _NS):%d %b %Y %H:%M:%S} UTC"


def _size(actions: list[Action]) -> str:
    return format_size(sum(action.source.size for action in actions))


def _count(count: int, unit: str) -> str:
    return f"{count:,} {unit}" if count == 1 else f"{count:,} {unit}s"


def _plan_file(backup: Path, name: str) -> Path:
    return backup / MARKER_DIR / "plans" / f"{name}.json"


def _check_there(backup: Path, listed: Source) -> None:
    """R7.4: a listed source whose folder is gone is refused until deletions arrive (F4)."""
    try:
        listed.path.stat()
    except FileNotFoundError, NotADirectoryError:
        raise Refusal(
            f"{listed.path} doesn't exist: it was deleted, or its drive is unplugged.\n"
            "Plug the drive in, or stop backing it up with"
            f" syncplan remove {quoted(str(backup))} {quoted(listed.name)}."
        ) from None


def _check_copy(backup: Path, listed: Source) -> None:
    """R7.6: the copy's folder is a plain folder or missing, not a backup, and short enough."""
    copy = backup / listed.name
    shorter = (
        f"Stop backing it up with syncplan remove {quoted(str(backup))}"
        f" {quoted(listed.name)}, then add it again with a shorter NAME=SOURCE."
    )
    if too_long(copy, FOLDER_LIMIT):
        raise Refusal(
            f"{copy} would be a path of {FOLDER_LIMIT} characters or more,"
            f" too long for Windows. {shorter}"
        )
    plan_file = _plan_file(backup, listed.name)
    longest = max(plan_file, plan_file.with_name(_TEMPORARY), key=lambda path: len(str(path)))
    if too_long(longest):
        raise Refusal(
            f"The plan for {listed.name} would need a path of {PATH_LIMIT} characters or more"
            f" ({longest}), too long for Windows. {shorter}"
        )
    why = "so syncplan can't plan copies into it"
    if plain_folder(copy, backup, "plan", why) and has_marker(copy):
        raise Refusal(
            f"{copy} is a syncplan backup itself, {why}."
            f" Move it out of {backup}, then run plan again."
        )
    logger.debug("copy %s: ok", copy)


def _check_plans(plans: Path) -> None:
    """R7.1: the plans folder is a real folder, or not there yet."""
    try:
        info = plans.lstat()
    except FileNotFoundError:
        return
    if (
        not stat.S_ISDIR(info.st_mode)
        or info.st_file_attributes & stat.FILE_ATTRIBUTE_REPARSE_POINT
    ):
        raise Refusal(f"{plans} is not a real folder. Remove it, then run plan again.")


def _chosen(text: str, source: Path) -> Path:
    """R3.3, before anything is read: an --overwrite FILE must be a plain file in the source.

    The path is kept as typed, not resolved: a FILE named through a link must meet that
    link's skip (R6.2) rather than quietly become the link's target.
    """
    path = None if text.startswith(("\\\\", "//")) else Path(text).absolute()
    try:
        info = None if path is None else path.lstat()
    except OSError:
        info = None
    plain = (
        info is not None
        and stat.S_ISREG(info.st_mode)
        and not info.st_file_attributes & stat.FILE_ATTRIBUTE_REPARSE_POINT
    )
    if not plain or not path.is_relative_to(source):
        raise Refusal(f"{path or text} isn't a file in {source}.")
    return path
