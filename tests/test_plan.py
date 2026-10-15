"""plan-copies: `syncplan plan` compares a source with its copy and writes a plan of copies."""

import contextlib
import ctypes
import io
import json
import os
import re
import shutil
import subprocess
import tempfile
from pathlib import Path
from types import SimpleNamespace

import pytest
from conftest import (
    extended,
    file_dates,
    hand_written,
    make_backup,
    make_tree,
    plan_date,
    read_plan,
    tree_snapshot,
)
from hypothesis import given, settings
from hypothesis import strategies as st

from syncplan import cli, compare, marker, space

MARKER = ".syncplan/marker.json"
T0 = 1_760_000_000_123_456_700  # a date with a 100-ns digit (SP3)
T1 = T0 + 3_600_000_000_000  # an hour later
T2 = T1 + 1_000_000_000  # a second after that


def put(path: Path, content: str | bytes, date: int = T0) -> Path:
    """A file with this content and modification date, in ns."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content.encode() if isinstance(content, str) else content)
    os.utime(path, ns=(date, date))
    return path


def planned(run_cli, backup: Path, source: Path, *extra: object) -> tuple[str, str, dict]:
    """Run plan, assert it succeeded, and return (stdout, stderr, the plan file)."""
    code, out, err = run_cli("plan", backup, source, *extra)
    assert code == 0, err
    return out, err, read_plan(backup, source.name)


def actions(plan: dict) -> list[tuple[str, str]]:
    return [(action["do"], action["path"]) for action in plan["actions"]]


@pytest.fixture
def opened(monkeypatch) -> list[Path]:
    """Every file plan opens to read, in order (S3: "not read" must be observed)."""
    seen: list[Path] = []
    real = compare._open_shared

    def record(path):
        seen.append(Path(path))
        return real(path)

    monkeypatch.setattr(compare, "_open_shared", record)
    return seen


def refuses(
    run_cli, root: Path, *args: object, expected: tuple[str, ...] = (), listed: bool = False
) -> str:
    """Run plan and assert a refusal that changed nothing under root (R1.2). Returns stderr.

    listed: the refusal comes after listing and reading, which print progress lines.
    """
    before = tree_snapshot(root), file_dates(root)
    code, out, err = run_cli("plan", *args)
    assert code == 1, err
    assert listed or out == ""
    for text in expected:
        assert text in err
    assert (tree_snapshot(root), file_dates(root)) == before
    return err


@pytest.fixture
def photos(tmp_path, run_cli) -> tuple[Path, Path]:
    """An initialized backup tmp_path/backup of one source, tmp_path/src/Photos."""
    backup, (source,) = make_backup(run_cli, tmp_path, "Photos")
    return backup, source


# R7.1: the backup folder


def test_r7_1_missing_backup_is_refused(tmp_path, run_cli, photos):
    _, source = photos
    refuses(run_cli, tmp_path, tmp_path / "nope", source, expected=("does not exist.",))


def test_r7_1_backup_that_is_a_file_is_refused(tmp_path, run_cli, photos):
    _, source = photos
    make_tree(tmp_path, {"file": "x"})
    refuses(run_cli, tmp_path, tmp_path / "file", source, expected=("is not a folder.",))


def test_r7_1_backup_path_starting_with_backslashes_is_refused(tmp_path, run_cli, photos):
    _, source = photos
    refuses(run_cli, tmp_path, "\\\\server\\share\\Backups", source, expected=("starts with \\\\",))


def test_r7_1_backup_with_a_broken_character_says_run_plan_again(tmp_path, run_cli, photos):
    _, source = photos
    err = refuses(run_cli, tmp_path, tmp_path / "bad\udc80name", source)
    assert "rename that folder and run plan again" in err


def test_r7_1_syncplan_folder_that_is_a_file_is_refused(tmp_path, run_cli, photos):
    backup, source = photos
    (backup / ".syncplan" / "marker.json").unlink()
    (backup / ".syncplan").rmdir()
    make_tree(backup, {".syncplan": "not a folder"})
    refuses(run_cli, tmp_path, backup, source, expected=("is not a real folder",))


def test_r7_1_plans_folder_that_is_a_file_is_refused(tmp_path, run_cli, photos):
    backup, source = photos
    make_tree(backup, {".syncplan/plans": "not a folder"})
    refuses(
        run_cli,
        tmp_path,
        backup,
        source,
        expected=(
            f"{backup / '.syncplan' / 'plans'} is not a real folder.",
            "Remove it, then run plan again.",
        ),
    )


def test_r7_1_plans_folder_that_is_a_link_is_refused(tmp_path, run_cli, photos, junction):
    backup, source = photos
    make_tree(tmp_path, {"elsewhere": None})
    junction(backup / ".syncplan" / "plans", tmp_path / "elsewhere")
    refuses(run_cli, tmp_path, backup, source, expected=("is not a real folder",))


def test_r7_1_a_plain_folder_inside_a_backup_is_refused_as_inside(tmp_path, run_cli):
    # Checked before the marker, so the message doesn't send the user to init (I1).
    make_tree(tmp_path, {"src/Photos/a.txt": "a", "outer/inner": None})
    hand_written(tmp_path / "outer", [("Other", tmp_path / "src" / "Other")])
    refuses(
        run_cli,
        tmp_path,
        tmp_path / "outer" / "inner",
        tmp_path / "src" / "Photos",
        expected=("is inside the syncplan backup",),
    )


def test_r7_1_backup_inside_another_backup_is_refused(tmp_path, run_cli):
    make_tree(tmp_path, {"src/Photos/a.txt": "a"})
    outer, inner = tmp_path / "outer", tmp_path / "outer" / "inner"
    hand_written(outer, [("Other", tmp_path / "src" / "Other")])
    hand_written(inner, [("Photos", tmp_path / "src" / "Photos")])
    refuses(
        run_cli,
        tmp_path,
        inner,
        tmp_path / "src" / "Photos",
        expected=(f"{inner} is inside the syncplan backup {outer}.",),
    )


# R7.2: the marker


def test_r7_2_folder_that_is_not_a_backup_is_refused(tmp_path, run_cli, photos):
    _, source = photos
    make_tree(tmp_path, {"plain": None})
    refuses(
        run_cli,
        tmp_path,
        tmp_path / "plain",
        source,
        expected=("is not a syncplan backup", "syncplan init"),
    )


@pytest.mark.parametrize(
    ("content", "expected"),
    [
        ("{not json", "can't be read; inspect it."),
        ('{"syncplan_marker": 99}', "newer syncplan"),
    ],
)
def test_r7_2_unreadable_or_newer_marker_is_refused(tmp_path, run_cli, photos, content, expected):
    backup, source = photos
    (backup / MARKER).write_text(content)
    refuses(run_cli, tmp_path, backup, source, expected=(expected,))


@pytest.mark.parametrize("name", ["..", "CON", "Photos "])
def test_r7_2_marker_with_an_unusable_name_is_refused(tmp_path, run_cli, name):
    make_tree(tmp_path, {"src/Photos/a.txt": "a"})
    backup = tmp_path / "backup"
    hand_written(backup, [(name, tmp_path / "src" / "Photos")])
    refuses(run_cli, tmp_path, backup, tmp_path / "src" / "Photos", expected=("inspect it.",))


# R7.3: the source must be listed


def test_r7_3_source_not_listed_is_refused_with_the_list(tmp_path, run_cli, photos):
    backup, source = photos
    make_tree(tmp_path, {"src/Music/m.mp3": "m"})
    err = refuses(
        run_cli,
        tmp_path,
        backup,
        tmp_path / "src" / "Music",
        expected=(f"{backup} is not a backup of {tmp_path / 'src' / 'Music'}. Its sources:",),
    )
    assert f"Photos <- {source}" in err


def test_r7_3_a_folder_inside_a_listed_source_is_not_listed(tmp_path, run_cli, photos):
    backup, source = photos
    make_tree(source, {"2024/a.jpg": "a"})
    refuses(run_cli, tmp_path, backup, source / "2024", expected=("is not a backup of",))


def test_nfr1_source_typed_in_another_case_is_listed(
    tmp_path, run_cli, photos, monkeypatch, typed_case_kept
):
    # Past the listed check, the next refusal (R7.7, faked) shows the source was found.
    backup, source = photos
    monkeypatch.setattr(space, "filesystem", lambda folder: ("Z:\\", "exFAT"))
    typed = Path(str(source).upper())
    refuses(run_cli, tmp_path, backup, typed, expected=("is formatted as exFAT",))


# R7.4: the source must be there


def test_r7_4_missing_source_is_refused_pointing_to_remove(tmp_path, run_cli, photos):
    backup, source = photos
    (source / "Photos.txt").unlink()
    source.rmdir()
    refuses(
        run_cli,
        tmp_path,
        backup,
        source,
        expected=(
            f"{source} doesn't exist: it was deleted, or its drive is unplugged.",
            f"syncplan remove {backup} Photos",
        ),
    )


def test_r7_4_source_on_a_drive_that_is_not_there_is_refused(tmp_path, run_cli):
    taken = {drive[0].upper() for drive in os.listdrives()}
    free = [letter for letter in "QRSTUVWXYZ" if letter not in taken]
    if not free:
        pytest.skip("every drive letter from Q to Z is in use")
    camera = Path(f"{free[0]}:\\Cam")
    backup = tmp_path / "backup"
    hand_written(backup, [("Cam", camera)])
    refuses(run_cli, tmp_path, backup, camera, expected=("doesn't exist",))


# R7.5: init's source checks, again


def test_r7_5_source_that_became_a_backup_is_refused(tmp_path, run_cli, photos):
    backup, source = photos
    hand_written(source, [("Other", tmp_path / "src" / "Other")])
    err = refuses(run_cli, tmp_path, backup, source, expected=(f"{source} is a syncplan backup.",))
    assert "A backup can't be backed up." in err
    assert "syncplan plan" not in err  # no suggestion plan itself would refuse (S1)


def test_r7_5_source_inside_the_backup_is_refused(tmp_path, run_cli):
    backup = tmp_path / "backup"
    make_tree(backup, {"Inside/a.txt": "a"})
    hand_written(backup, [("Inside", backup / "Inside")])
    refuses(
        run_cli,
        tmp_path,
        backup,
        backup / "Inside",
        expected=(f"The source {backup / 'Inside'} is inside the backup {backup}.",),
    )


def test_r7_5_source_holding_syncplan_s_own_folder_is_refused(tmp_path, run_cli, program_data):
    backup = tmp_path / "backup"
    hand_written(backup, [("Data", program_data)])
    refuses(run_cli, tmp_path, backup, program_data, expected=("holds syncplan's own folder",))


# R7.6: the copy's own folder


def test_r7_6_copy_that_is_a_file_is_refused(tmp_path, run_cli, photos):
    backup, source = photos
    make_tree(backup, {"Photos": "a file"})
    refuses(
        run_cli,
        tmp_path,
        backup,
        source,
        expected=(
            f"{backup / 'Photos'} is not a real folder, so syncplan can't plan copies into it.",
            f"Move it out of {backup}, then run plan again.",
        ),
    )


def test_r7_6_copy_that_is_a_link_is_refused(tmp_path, run_cli, photos, junction):
    backup, source = photos
    make_tree(tmp_path, {"elsewhere": None})
    junction(backup / "Photos", tmp_path / "elsewhere")
    refuses(run_cli, tmp_path, backup, source, expected=("is not a real folder",))


def test_r7_6_copy_that_is_a_backup_itself_is_refused(tmp_path, run_cli, photos):
    backup, source = photos
    hand_written(backup / "Photos", [("Other", tmp_path / "src" / "Other")])
    refuses(
        run_cli,
        tmp_path,
        backup,
        source,
        expected=(f"{backup / 'Photos'} is a syncplan backup itself",),
    )


def long_name(backup: Path, units: int) -> str:
    """A NAME that makes <backup>\\<name> exactly `units` UTF-16 units long."""
    name = "N" * (units - len(str(backup)) - 1)
    assert len(str(backup / name)) == units
    return name


@pytest.mark.parametrize(("units", "expected"), [(248, "248 characters"), (239, "260 characters")])
def test_r7_6_copy_or_plan_file_path_too_long_is_refused(tmp_path, run_cli, units, expected):
    # 248: Windows can't make the copy's folder (SP4). 239: the plan file's path,
    # 21 units longer, reaches 260 (S1, S4).
    make_tree(tmp_path, {"src/Long/a.txt": "a"})
    backup = tmp_path / "backup"
    name = long_name(backup, units)
    hand_written(backup, [(name, tmp_path / "src" / "Long")])
    err = refuses(run_cli, tmp_path, backup, tmp_path / "src" / "Long", expected=(expected,))
    assert "shorter NAME=SOURCE" in err


def test_r7_6_a_temporary_plan_file_path_too_long_is_refused(tmp_path, run_cli):
    # NAME "P": the plan file's path is 249 units, its temporary file's 260 (I1, I6).
    make_tree(tmp_path, {"src/P/a.txt": "a"})
    backup = tmp_path / ("B" * (226 - len(str(tmp_path)) - 1))
    hand_written(backup, [("P", tmp_path / "src" / "P")])
    assert len(str(backup)) == 226
    refuses(run_cli, tmp_path, backup, tmp_path / "src" / "P", expected=("260 characters",))


# R7.7: NTFS only


def test_r7_7_backup_that_is_not_ntfs_is_refused(tmp_path, run_cli, photos, monkeypatch):
    backup, source = photos
    monkeypatch.setattr(space, "filesystem", lambda folder: ("F:\\", "exFAT"))
    refuses(
        run_cli,
        tmp_path,
        backup,
        source,
        expected=(
            "F:\\ is formatted as exFAT. syncplan backs up only to NTFS drives,"
            " which keep file dates exactly. Use an NTFS drive.",
        ),
    )


def test_r7_7_a_drive_that_cannot_say_its_format_stops_the_run(
    tmp_path, run_cli, photos, monkeypatch
):
    backup, source = photos

    def not_ready(*args):
        ctypes.set_last_error(21)
        return 0

    monkeypatch.setattr(space._kernel32, "GetVolumePathNameW", not_ready)
    code, _, err = run_cli("plan", backup, source)
    assert code == 1
    assert f"{backup}: The device is not ready." in err
    assert ".." not in err


def test_r7_7_the_drive_format_comes_from_the_backup_folder(tmp_path):
    root, kind = space.filesystem(tmp_path)
    assert root == tmp_path.anchor
    assert kind == "NTFS"


# R3.3: --overwrite takes files inside the source


@pytest.mark.parametrize("where", ["outside", "missing", "folder"])
def test_r3_3_overwrite_that_is_not_a_file_in_the_source_is_refused(
    tmp_path, run_cli, photos, where
):
    backup, source = photos
    make_tree(tmp_path, {"other/x.txt": "x", "src/Photos/sub": None})
    chosen = {
        "outside": tmp_path / "other" / "x.txt",
        "missing": source / "nope.txt",
        "folder": source / "sub",
    }[where]
    refuses(
        run_cli,
        tmp_path,
        backup,
        source,
        "--overwrite",
        chosen,
        expected=(f"{chosen} isn't a file in {source}.",),
    )


# Usage, the log and the lock


def test_usage_plan_without_a_source_exits_2(tmp_path, run_cli, photos):
    backup, _ = photos
    code, _, err = run_cli("plan", backup)
    assert code == 2
    assert "usage: syncplan plan" in err


def test_nfr2_a_refusal_is_logged_with_the_checks(tmp_path, run_cli, photos, program_data):
    backup, _ = photos
    make_tree(tmp_path, {"src/Music/m.mp3": "m"})
    run_cli("plan", backup, tmp_path / "src" / "Music")
    log = (program_data / "syncplan" / "logs" / "syncplan.log").read_text("utf-8")
    assert "command: plan" in log
    assert f"backup {backup}: ok" in log
    assert "is not a backup of" in log
    assert "exit 1" in log


def test_nfr2_a_second_run_is_refused_while_one_holds_the_lock(
    tmp_path, run_cli, photos, held_lock
):
    backup, source = photos
    err = refuses(run_cli, tmp_path, backup, source)
    assert "Another syncplan is already running" in err


# R1 and R2: plan one source, comparing it with its copy


@pytest.fixture
def first(tmp_path, run_cli) -> tuple[Path, Path]:
    """A backup of src/Photos, which holds two files and an empty folder; no copy yet."""
    backup, (source,) = make_backup(run_cli, tmp_path, "Photos")
    (source / "Photos.txt").unlink()
    put(source / "a" / "1.jpg", "one")
    put(source / "b.txt", "bee", T1)
    (source / "empty").mkdir()
    return backup, source


def test_r1_1_first_plan_writes_the_plan_and_the_summary(tmp_path, run_cli, first):
    backup, source = first
    out, err, plan = planned(run_cli, backup, source)
    assert err == ""
    assert f"Plan for Photos ({source} -> {backup / 'Photos'}):" in out
    assert f"Plan written to {backup / '.syncplan' / 'plans' / 'Photos.json'}" in out
    assert out.rstrip().endswith("Nothing has been changed.")
    assert plan["name"] == "Photos"


def test_r1_2_plan_changes_nothing_but_its_plan_file(tmp_path, run_cli, first):
    backup, source = first
    put(backup / "Photos" / "old.txt", "old")  # a copy-only file must stay too
    before = tree_snapshot(tmp_path), file_dates(tmp_path)

    planned(run_cli, backup, source)

    plans = "backup/.syncplan/plans"
    snapshot = {k: v for k, v in tree_snapshot(tmp_path).items() if not k.startswith(plans)}
    assert snapshot == before[0]
    own = {"backup/.syncplan"}  # its date changes when plans\ is made inside it
    dates = {k: v for k, v in file_dates(tmp_path).items() if k not in own}
    assert {k: v for k, v in dates.items() if not k.startswith(plans)} == {
        k: v for k, v in before[1].items() if k not in own
    }


def test_r2_1_and_r2_2_first_plan_makes_folders_then_copies(tmp_path, run_cli, first, opened):
    backup, source = first
    _, _, plan = planned(run_cli, backup, source)
    assert actions(plan) == [
        ("make folder", "."),
        ("make folder", "a"),
        ("copy", "a\\1.jpg"),
        ("copy", "b.txt"),
        ("make folder", "empty"),
    ]
    copy = plan["actions"][2]
    assert copy["source"]["size"] == 3
    assert plan_date(copy["source"]["date"]) == T0
    assert opened == []  # new files are never read (R2.4)


def test_r2_2_an_empty_folder_missing_on_the_backup_is_made(tmp_path, run_cli, first):
    backup, source = first
    shutil.copytree(source, backup / "Photos")
    (source / "new" / "deeper").mkdir(parents=True)
    _, _, plan = planned(run_cli, backup, source)
    assert actions(plan) == [("make folder", "new"), ("make folder", "new\\deeper")]


@pytest.fixture
def copied(tmp_path, run_cli, first) -> tuple[Path, Path, Path]:
    """first, with an exact copy on the backup. Returns (backup, source, copy)."""
    backup, source = first
    shutil.copytree(source, backup / "Photos")
    return backup, source, backup / "Photos"


def test_r2_3_an_edit_of_another_size_is_overwritten_without_reading(
    tmp_path, run_cli, copied, opened
):
    backup, source, copy = copied
    put(source / "b.txt", "bee, edited", T2)
    _, _, plan = planned(run_cli, backup, source)
    assert actions(plan) == [("overwrite", "b.txt")]
    action = plan["actions"][0]
    assert action["source"]["size"] == 11
    assert action["backup"]["size"] == 3
    assert plan_date(action["backup"]["date"]) == T1
    assert copy / "b.txt" not in opened
    assert source / "b.txt" not in opened


def test_r2_3_and_r2_4_an_edit_of_the_same_size_is_read_and_overwritten(
    tmp_path, run_cli, copied, opened
):
    backup, source, copy = copied
    put(source / "b.txt", "BEE", T2)
    _, _, plan = planned(run_cli, backup, source)
    assert actions(plan) == [("overwrite", "b.txt")]
    expected = [source / "a" / "1.jpg", copy / "a" / "1.jpg", source / "b.txt", copy / "b.txt"]
    assert sorted(opened) == sorted(expected)


def test_r2_4_only_same_size_pairs_are_read_once_each(tmp_path, run_cli, copied, opened):
    backup, source, copy = copied
    put(source / "new.txt", "new")  # one side only
    put(copy / "gone.txt", "gone")  # one side only
    put(source / "b.txt", "longer now", T2)  # another size
    planned(run_cli, backup, source)
    assert opened == [source / "a" / "1.jpg", copy / "a" / "1.jpg"]


@pytest.mark.parametrize("backup_date", [T0 - 1_000, T0 + 1_000])
def test_r2_5_same_content_with_another_date_sets_the_date(tmp_path, run_cli, copied, backup_date):
    backup, source, copy = copied
    os.utime(copy / "a" / "1.jpg", ns=(backup_date, backup_date))
    _, _, plan = planned(run_cli, backup, source)
    assert actions(plan) == [("set date", "a\\1.jpg")]
    action = plan["actions"][0]
    assert plan_date(action["source"]["date"]) == T0
    assert plan_date(action["backup"]["date"]) == backup_date


def test_r2_5_same_content_and_date_is_unchanged(tmp_path, run_cli, copied):
    backup, source, _ = copied
    out, _, plan = planned(run_cli, backup, source)
    assert plan["actions"] == []
    assert ["unchanged", "2", "files"] in [line.split() for line in out.splitlines()]


def test_r2_6_a_quick_plan_reads_nothing(tmp_path, run_cli, copied, opened):
    backup, source, copy = copied
    put(source / "b.txt", "BEE", T2)  # same size, later: taken as an edit
    put(copy / "a" / "1.jpg", "ONE", T0)  # same size and date: taken as unchanged
    out, _, plan = planned(run_cli, backup, source, "--quick")
    assert actions(plan) == [("overwrite", "b.txt")]
    assert plan["quick"] is True
    assert "Quick plan: sizes and dates only; damaged files can't be spotted." in out
    assert opened == []


def test_r2_7_what_is_only_on_the_backup_is_counted_and_left(tmp_path, run_cli, copied):
    backup, source, copy = copied
    put(copy / "gone.txt", "gone")
    put(copy / "old" / "x.txt", "x")
    put(copy / "old" / "y.txt", "y")
    out, _, plan = planned(run_cli, backup, source)
    assert plan["actions"] == []
    assert (
        f"On the backup but no longer in {source}: 3 files, 1 folder (left as they are for now)."
        in out
    )


# R4: the plan file


def plans_folder(backup: Path) -> list[str]:
    return sorted(p.name for p in (backup / ".syncplan" / "plans").iterdir())


def test_r4_1_each_plan_replaces_the_previous_one(tmp_path, run_cli, first):
    backup, source = first
    planned(run_cli, backup, source)
    shutil.copytree(source, backup / "Photos")
    _, _, plan = planned(run_cli, backup, source)
    assert plan["actions"] == []
    assert plans_folder(backup) == ["Photos.json"]


def test_r4_1_a_plan_file_path_just_under_the_limit_is_written(tmp_path, run_cli):
    make_tree(tmp_path, {"src/Long/a.txt": "a"})
    backup = tmp_path / "backup"
    name = "N" * (238 - len(str(backup)) - 1)  # the copy at 238 units, the plan file at 259
    hand_written(backup, [(name, tmp_path / "src" / "Long")])
    code, _, err = run_cli("plan", backup, tmp_path / "src" / "Long")
    assert code == 0, err
    assert len(str(backup / ".syncplan" / "plans" / f"{name}.json")) == 259


def test_r4_2_the_plan_file_holds_the_source_the_backup_and_exact_dates(tmp_path, run_cli, first):
    backup, source = first
    old = -86_400 * 1_000_000_000 * 365 + 1_234_567_800  # in 1969, with a 100-ns digit
    put(source / "old.txt", "o", old)
    _, _, plan = planned(run_cli, backup, source)
    assert plan["syncplan_plan"] == 1
    assert plan["backup"] == str(backup)
    assert plan["source"] == str(source)
    assert plan["quick"] is False
    assert plan["created"].startswith("20")
    assert plan["created"][-6] in "+-"
    assert plan["decide"] == []
    assert plan["skipped"] == []
    assert plan["chosen"] == []
    dated = [action for action in plan["actions"] if "source" in action]
    assert len(dated) == 3
    for action in dated:
        on_disk = (source / action["path"]).stat().st_mtime_ns
        assert plan_date(action["source"]["date"]) == on_disk


def test_r4_2_each_entry_sits_on_a_line_of_its_own(tmp_path, run_cli, first):
    backup, source = first
    planned(run_cli, backup, source)
    text = (backup / ".syncplan" / "plans" / "Photos.json").read_text("utf-8")
    entries = [line for line in text.splitlines() if line.startswith("    {")]
    assert len(entries) == 5
    assert all(json.loads(line.rstrip(",")) for line in entries)


def test_r4_2_names_stay_readable_in_the_plan_file(tmp_path, run_cli, first):
    backup, source = first
    put(source / "Música" / "canción.mp3", "la")
    planned(run_cli, backup, source)
    text = (backup / ".syncplan" / "plans" / "Photos.json").read_text("utf-8")
    assert "Música\\\\canción.mp3" in text


def test_r4_3_nothing_to_do_still_writes_an_empty_plan(tmp_path, run_cli, copied):
    backup, source, copy = copied
    out, _, plan = planned(run_cli, backup, source)
    assert plan["actions"] == []
    assert f"Nothing to do: {copy} is up to date." in out


def test_r4_4_ctrl_c_while_reading_keeps_the_previous_plan(tmp_path, run_cli, copied, monkeypatch):
    backup, source, _ = copied
    put(source / "b.txt", "BEE", T2)
    planned(run_cli, backup, source)
    before = (backup / ".syncplan" / "plans" / "Photos.json").read_bytes()

    def interrupted(path):
        raise KeyboardInterrupt

    monkeypatch.setattr(compare, "_open_shared", interrupted)
    with pytest.raises(KeyboardInterrupt):
        run_cli("plan", backup, source)
    assert (backup / ".syncplan" / "plans" / "Photos.json").read_bytes() == before
    assert plans_folder(backup) == ["Photos.json"]


@pytest.mark.parametrize("step", ["create", "fsync", "replace"])
def test_r4_4_and_r4_5_a_failed_write_keeps_the_previous_plan(
    tmp_path, run_cli, first, monkeypatch, step
):
    backup, source = first
    planned(run_cli, backup, source)
    before = (backup / ".syncplan" / "plans" / "Photos.json").read_bytes()
    put(source / "new.txt", "new")

    def fail(*args, **kwargs):
        raise OSError(28, "There is not enough space on the disk")

    if step == "create":
        monkeypatch.setattr(marker.tempfile, "mkstemp", fail)
    elif step == "fsync":
        monkeypatch.setattr(os, "fsync", fail)
    else:
        monkeypatch.setattr(Path, "replace", fail)
    code, _, err = run_cli("plan", backup, source)
    assert code == 1
    assert (
        f"Couldn't write the plan in {backup / '.syncplan' / 'plans'}:"
        " There is not enough space on the disk." in err
    )
    assert "Details:" in err
    assert (backup / ".syncplan" / "plans" / "Photos.json").read_bytes() == before
    assert plans_folder(backup) == ["Photos.json"]


def test_r4_5_a_failed_first_write_leaves_no_plans_folder(tmp_path, run_cli, first, full_disk):
    backup, source = first
    code, _, err = run_cli("plan", backup, source)
    assert code == 1
    assert "Couldn't write the plan in" in err
    assert sorted(p.name for p in (backup / ".syncplan").iterdir()) == ["marker.json"]


# R5: what plan prints


def test_r5_1_the_summary_counts_every_kind_of_action(tmp_path, run_cli, copied):
    backup, source, copy = copied
    put(source / "new.txt", "new!")  # copy
    put(source / "b.txt", "bee, edited", T2)  # overwrite
    os.utime(copy / "a" / "1.jpg", ns=(T0 - 1_000, T0 - 1_000))  # set date
    (source / "more").mkdir()  # make folder
    out, err, _ = planned(run_cli, backup, source)
    lines = [line.split() for line in out.splitlines()]
    assert ["copy", "(new)", "1", "file", "4", "bytes"] in lines
    assert ["overwrite", "1", "file", "11", "bytes"] in lines
    assert ["set", "date", "1", "file"] in lines
    assert ["make", "folder", "1", "folder"] in lines
    assert ["unchanged", "0", "files"] in lines
    assert "Nothing to do" not in out
    assert err == ""


def test_r5_2_space_to_copy_is_rounded_to_blocks_against_free_space(
    tmp_path, run_cli, first, monkeypatch
):
    backup, source = first
    put(source / "big.bin", b"x" * 5000)
    monkeypatch.setattr(space, "block_size", lambda folder: 4096)
    monkeypatch.setattr(space, "disk_usage", lambda folder: (2**30, 0, 900 * 2**20))
    out, err, _ = planned(run_cli, backup, source)
    # 1.jpg and b.txt take a block each, big.bin two: 16 KB.
    assert f"To copy: 16 KB. {backup.anchor} has 900 MB free." in out
    assert err == ""


def test_r5_2_overwrites_count_in_the_space_to_copy(tmp_path, run_cli, copied, monkeypatch):
    backup, source, _ = copied
    put(source / "b.txt", b"y" * 5000, T2)
    monkeypatch.setattr(space, "block_size", lambda folder: 4096)
    monkeypatch.setattr(space, "disk_usage", lambda folder: (2**30, 0, 900 * 2**20))
    out, _, plan = planned(run_cli, backup, source)
    assert actions(plan) == [("overwrite", "b.txt")]
    assert f"To copy: 8.0 KB. {backup.anchor} has 900 MB free." in out


def test_r5_2_free_space_is_asked_before_the_plan_is_replaced(
    tmp_path, run_cli, copied, monkeypatch
):
    # A drive that drops at that moment must not leave an unreviewed plan in place (I3).
    backup, source, _ = copied
    planned(run_cli, backup, source)
    before = (backup / ".syncplan" / "plans" / "Photos.json").read_bytes()
    put(source / "new.txt", "new")

    def gone(folder):
        raise OSError(21, "The device is not ready", str(folder))

    monkeypatch.setattr(space, "disk_usage", gone)
    code, _, _ = run_cli("plan", backup, source)
    assert code == 1
    assert (backup / ".syncplan" / "plans" / "Photos.json").read_bytes() == before


def test_r5_3_a_drive_too_small_is_warned_about_and_the_plan_still_written(
    tmp_path, run_cli, first, monkeypatch
):
    backup, source = first
    monkeypatch.setattr(space, "block_size", lambda folder: 4096)
    monkeypatch.setattr(space, "disk_usage", lambda folder: (2**30, 0, 4096))
    code, out, err = run_cli("plan", backup, source)
    assert code == 0
    assert "Warning: about 4.0 KB short." in err
    assert "Plan written to" in out


# NFR1, NFR2


def test_nfr1_a_name_in_another_case_on_the_backup_is_the_same_file(tmp_path, run_cli, copied):
    backup, source, copy = copied
    (copy / "b.txt").rename(copy / "B.TXT")
    _, _, plan = planned(run_cli, backup, source)
    assert plan["actions"] == []


def test_nfr2_listing_and_reading_times_are_logged(tmp_path, run_cli, copied, program_data):
    backup, source, _ = copied
    planned(run_cli, backup, source)
    log = (program_data / "syncplan" / "logs" / "syncplan.log").read_text("utf-8")
    assert f"listed {source} and {backup / 'Photos'}:" in log
    assert "read 2 pairs" in log
    assert f"plan written: {backup / '.syncplan' / 'plans' / 'Photos.json'}" in log


# R3: files that need your decision


def decided(plan: dict) -> list[tuple[str, str]]:
    return [(entry["path"], entry["case"]) for entry in plan["decide"]]


@pytest.mark.parametrize(
    ("content", "date", "case"),
    [
        ("bee, other", T1, "same date, other size"),
        ("BEE", T1, "same date and size, different content"),
        ("bee, other", T2, "backup copy newer"),
        ("BEE", T2, "backup copy newer"),
        ("bee", T2, None),  # same content, newer: only the date (R2.5)
    ],
)
def test_r3_1_a_difference_that_is_no_edit_needs_a_decision(
    tmp_path, run_cli, copied, content, date, case
):
    backup, source, copy = copied
    put(copy / "b.txt", content, date)
    out, err, plan = planned(run_cli, backup, source)
    if case is None:
        assert actions(plan) == [("set date", "b.txt")]
        assert plan["decide"] == []
        return
    assert plan["actions"] == []
    assert decided(plan) == [("b.txt", case)]
    entry = plan["decide"][0]
    assert entry["source"]["size"] == 3
    assert entry["backup"]["size"] == len(content)
    assert plan_date(entry["backup"]["date"]) == date
    assert "Needs your decision (nothing planned for these):" in err
    assert f"  b.txt  {case}" in err
    assert err.count(" | backup ") == 1
    assert "Nothing to do" not in out  # R4.3: a decision is not "up to date"


def test_r3_1_a_quick_plan_finds_a_newer_backup_copy_but_not_different_bytes(
    tmp_path, run_cli, copied
):
    backup, source, copy = copied
    put(copy / "b.txt", "BEE", T1)  # same size and date: unchanged to a quick plan
    put(copy / "a" / "1.jpg", "one", T1)  # the backup's copy is newer
    _, _, plan = planned(run_cli, backup, source, "--quick")
    assert decided(plan) == [("a\\1.jpg", "backup copy newer")]


def test_r1_1_and_r4_2_a_plan_with_only_a_decision_exits_0(tmp_path, run_cli, copied):
    backup, source, copy = copied
    put(copy / "b.txt", "BEE", T1)
    code, out, _ = run_cli("plan", backup, source)
    assert code == 0
    assert "Nothing to do" not in out
    assert read_plan(backup, "Photos")["decide"][0]["case"] == (
        "same date and size, different content"
    )


def test_r3_2_overwrite_turns_a_decision_into_an_overwrite_shown_as_chosen(
    tmp_path, run_cli, copied
):
    backup, source, copy = copied
    put(copy / "b.txt", "BEE", T1)
    put(copy / "a" / "1.jpg", "one!", T2)
    out, err, plan = planned(run_cli, backup, source, "--overwrite", source / "b.txt")
    assert actions(plan) == [("overwrite", "b.txt")]
    assert [entry["path"] for entry in plan["chosen"]] == ["b.txt"]
    assert decided(plan) == [("a\\1.jpg", "backup copy newer")]
    assert "Overwriting, as you chose (the old copies go to the quarantine):" in out
    lines = out.splitlines()
    first = lines.index("  b.txt  same date and size, different content")
    assert re.fullmatch(r"    source .+  3 bytes \| backup .+  3 bytes", lines[first + 1])
    chosen = plan["chosen"][0]
    assert plan_date(chosen["source"]["date"]) == T1
    assert plan_date(chosen["backup"]["date"]) == T1
    assert chosen["source"]["size"] == chosen["backup"]["size"] == 3
    assert "b.txt" not in err


def test_nfr1_overwrite_typed_in_another_case_is_accepted(
    tmp_path, run_cli, copied, typed_case_kept
):
    backup, source, copy = copied
    put(copy / "b.txt", "BEE", T1)
    _, _, plan = planned(run_cli, backup, source, "--overwrite", str(source / "b.txt").upper())
    assert actions(plan) == [("overwrite", "b.txt")]


def test_r3_3_overwrite_of_a_file_needing_no_decision_is_refused_before_the_long_read(
    tmp_path, run_cli, copied, opened
):
    backup, source, copy = copied
    put(source / "c.txt", "sea")
    shutil.copy2(source / "c.txt", copy / "c.txt")
    err = refuses(
        run_cli,
        tmp_path,
        backup,
        source,
        "--overwrite",
        source / "c.txt",
        expected=(
            f"{source / 'c.txt'} doesn't need a decision; --overwrite only takes files"
            ' listed under "Needs your decision".',
        ),
        listed=True,
    )
    assert opened == [source / "c.txt", copy / "c.txt"]  # its pair alone, read first
    assert "Details" not in err


def test_r3_4_a_backup_file_that_cannot_be_read_needs_a_decision(
    tmp_path, run_cli, copied, monkeypatch
):
    backup, source, copy = copied
    real = compare._open_shared

    def damaged(path):
        if Path(path) == copy / "b.txt":
            raise OSError(22, "Data error (cyclic redundancy check)", str(path), 23)
        return real(path)

    monkeypatch.setattr(compare, "_open_shared", damaged)
    _, err, plan = planned(run_cli, backup, source)
    assert decided(plan) == [("b.txt", "backup copy can't be read")]
    assert plan["decide"][0]["reason"] == "Data error (cyclic redundancy check)."
    assert "b.txt  backup copy can't be read: Data error (cyclic redundancy check)" in err

    _, _, plan = planned(run_cli, backup, source, "--overwrite", source / "b.txt")
    assert actions(plan) == [("overwrite", "b.txt")]


def test_r5_4_long_lists_stop_at_50_lines_and_the_plan_file_has_all(tmp_path, run_cli, copied):
    backup, source, copy = copied
    for number in range(55):
        put(source / "many" / f"{number:02}.txt", "x", T0)
        put(copy / "many" / f"{number:02}.txt", "y", T1)
    _, err, plan = planned(run_cli, backup, source)
    assert len(plan["decide"]) == 55
    assert plan["decide"][50]["path"] == "many\\50.txt"
    assert err.count("backup copy newer") == 50
    assert "  ... and 5 more (all in the plan file)" in err


# The plan, applied, leaves nothing to do (D12, R2, R3.1)

NAMES = ["a", "b", "c", "d\\e", "d\\f"]
DATES = [T0, T0 + 100, T1, -2_592_000_000_000_000 + 1_234_567_800]  # one in 1969
CONTENTS = [b"", b"x", b"xy", b"xz", b"xyz"]
FILES = st.dictionaries(
    st.sampled_from(NAMES),
    st.tuples(st.sampled_from(CONTENTS), st.sampled_from(DATES)),
    max_size=5,
)


def apply_plan(plan: dict, source: Path, copy: Path) -> None:
    """A reference apply, for the test only: paths and dates come from the plan file."""
    for action in plan["actions"]:
        target = copy / action["path"]
        if action["do"] == "make folder":
            target.mkdir()
            continue
        if action["do"] in ("copy", "overwrite"):
            shutil.copyfile(source / action["path"], target)
        date = plan_date(action["source"]["date"])
        os.utime(target, ns=(date, date))


def plan_counting(backup: Path, source: Path) -> tuple[dict, int]:
    """Run plan; return the plan file and the summary's unchanged count."""
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        assert cli.main(["plan", str(backup), str(source)]) == 0
    found = re.search(r"^\s+unchanged\s+([\d,]+) files?$", out.getvalue(), re.M)
    return read_plan(backup, "Photos"), int(found.group(1).replace(",", ""))


@settings(max_examples=25, deadline=None)
@given(mine=FILES, theirs=FILES)
def test_property_a_plan_applied_leaves_nothing_to_do(mine, theirs):
    with tempfile.TemporaryDirectory() as temp:
        root = Path(temp)
        source, backup = root / "src" / "Photos", root / "backup"
        for files, folder in ((mine, source), (theirs, backup / "Photos")):
            folder.mkdir(parents=True)
            for name, (content, date) in files.items():
                put(folder / name, content, date)
        assert cli.main(["init", str(backup), str(source)]) == 0
        first, unchanged = plan_counting(backup, source)

        # Each source file is counted once (D12), and sorted as R2 and R3.1 say.
        files = {a["path"]: a for a in first["actions"] if a["do"] != "make folder"}
        decide = {entry["path"] for entry in first["decide"]}
        assert not files.keys() & decide
        assert unchanged + len(files) + len(decide) + len(first["skipped"]) == len(mine)
        for name, (content, date) in mine.items():
            other = theirs.get(name)
            if name in decide:
                assert other is not None and other[0] != content and date <= other[1]
            elif name not in files:
                assert other == (content, date)  # unchanged
            elif files[name]["do"] == "copy":
                assert other is None
            elif files[name]["do"] == "overwrite":
                assert other[0] != content and date > other[1]
            else:
                assert files[name]["do"] == "set date"
                assert other[0] == content and date != other[1]

        apply_plan(first, source, backup / "Photos")
        for name in mine.keys() - decide:  # an exact copy now: bytes and date
            copied = backup / "Photos" / name
            assert copied.read_bytes() == (source / name).read_bytes()
            assert copied.stat().st_mtime_ns == (source / name).stat().st_mtime_ns

        second, unchanged = plan_counting(backup, source)
        assert second["actions"] == []
        assert decided(second) == decided(first)
        assert unchanged + len(second["decide"]) + len(second["skipped"]) == len(mine)


# R6, R2.8, R2.9: what plan skips, and carries on


def skipped(plan: dict) -> list[tuple[str, str]]:
    return [(entry["path"], entry["reason"]) for entry in plan["skipped"]]


class Twisted:
    """A folder entry with some answers changed, for cases a test can't make for real."""

    def __init__(
        self,
        entry,
        name: str | None = None,
        link: bool = False,
        attributes: int = 0,
        reparse_tag: int | None = None,
    ):
        self._entry, self.name = entry, name or entry.name
        self.path = str(Path(entry.path).with_name(self.name))
        self._link, self._attributes, self._tag = link, attributes, reparse_tag

    def is_symlink(self) -> bool:
        return self._link or self._entry.is_symlink()

    def is_junction(self) -> bool:
        return self._entry.is_junction()

    def is_dir(self, follow_symlinks: bool = True) -> bool:
        return self._entry.is_dir(follow_symlinks=follow_symlinks)

    def stat(self, follow_symlinks: bool = True):
        info = self._entry.stat(follow_symlinks=follow_symlinks)
        return SimpleNamespace(
            st_mode=info.st_mode,
            st_size=info.st_size,
            st_mtime_ns=info.st_mtime_ns,
            st_file_attributes=info.st_file_attributes | self._attributes,
            st_reparse_tag=info.st_reparse_tag if self._tag is None else self._tag,
        )


@pytest.fixture
def twisted(monkeypatch):
    """Change the entries listed for one folder: twist(folder, name, change)."""

    def twist(folder: Path, name: str, change) -> None:
        real = os.scandir

        class Listing:
            def __init__(self, path):
                self._real = real(path)

            def __enter__(self):
                entries = []
                for entry in self._real:
                    entries += change(entry) if entry.name == name else [entry]
                return iter(entries)

            def __exit__(self, *exc):
                self._real.close()

        def scandir(path):
            return Listing(path) if Path(path) == folder else real(path)

        monkeypatch.setattr(os, "scandir", scandir)

    return twist


def test_r6_1_a_folder_that_cannot_be_listed_is_skipped_with_all_below(
    tmp_path, run_cli, copied, failing_scandir
):
    backup, source, _ = copied
    put(source / "a" / "deep" / "new.txt", "new")
    failing_scandir(lambda path: path == source / "a", PermissionError(13, "Access is denied"))
    _, err, plan = planned(run_cli, backup, source)
    assert skipped(plan) == [("a", "can't be read: Access is denied.")]
    assert plan["actions"] == []
    assert "Skipped (nothing planned for these):" in err
    assert "  a  can't be read: Access is denied." in err


def test_r6_1_a_folder_of_the_copy_that_cannot_be_listed_is_skipped(
    tmp_path, run_cli, copied, failing_scandir
):
    backup, source, copy = copied
    failing_scandir(lambda path: path == copy / "a", PermissionError(13, "Access is denied"))
    _, _, plan = planned(run_cli, backup, source)
    assert skipped(plan) == [("a", "can't be read: Access is denied.")]


@pytest.mark.parametrize("side", ["source", "copy"])
def test_r1_4_a_top_folder_that_cannot_be_listed_stops_the_run(
    tmp_path, run_cli, copied, failing_scandir, side
):
    backup, source, copy = copied
    top = source if side == "source" else copy
    failing_scandir(lambda path: path == top, PermissionError(13, "Access is denied", str(top)))
    code, _, err = run_cli("plan", backup, source)
    assert code == 1
    assert f"{top}: Access is denied." in err
    assert "Details:" in err
    assert not (backup / ".syncplan" / "plans").exists()


def gone_after(monkeypatch, root: Path) -> dict:
    """Once state["gone"] is set, root's stat fails as if its drive were unplugged (R1.3)."""
    state = {"gone": False}
    real_stat = Path.stat

    def stat(self, *args, **kwargs):
        if state["gone"] and self == root:
            raise FileNotFoundError(3, "The system cannot find the path specified", str(self))
        return real_stat(self, *args, **kwargs)

    monkeypatch.setattr(Path, "stat", stat)
    return state


@pytest.mark.parametrize("side", ["source", "copy"])
def test_r1_3_a_drive_gone_while_listing_stops_the_run(
    tmp_path, run_cli, copied, monkeypatch, failing_scandir, side
):
    backup, source, copy = copied
    top = source if side == "source" else copy
    state = gone_after(monkeypatch, source if side == "source" else backup)

    def unplugged(path: Path) -> bool:
        if path == top / "a":
            state["gone"] = True
            return True
        return False

    failing_scandir(unplugged, OSError(21, "The device is not ready", str(top / "a")))
    code, _, err = run_cli("plan", backup, source)
    assert code == 1
    assert "The system cannot find the path specified" in err
    assert "Details:" in err
    assert not (backup / ".syncplan" / "plans").exists()


@pytest.mark.parametrize("side", ["source", "copy"])
def test_r1_3_a_drive_gone_while_reading_stops_the_run(
    tmp_path, run_cli, copied, monkeypatch, side
):
    backup, source, copy = copied
    planned(run_cli, backup, source)
    before = (backup / ".syncplan" / "plans" / "Photos.json").read_bytes()
    state = gone_after(monkeypatch, source if side == "source" else backup)
    real = compare._open_shared

    def unplugged(path):
        if not Path(path).is_relative_to(source if side == "source" else copy):
            return real(path)
        state["gone"] = True
        raise OSError(21, "The device is not ready", str(path))

    monkeypatch.setattr(compare, "_open_shared", unplugged)
    code, _, err = run_cli("plan", backup, source)
    assert code == 1
    assert "Details:" in err
    assert (backup / ".syncplan" / "plans" / "Photos.json").read_bytes() == before


def test_r2_8_a_locked_source_file_is_skipped(tmp_path, run_cli, copied, locked):
    backup, source, _ = copied
    locked(source / "b.txt")
    _, err, plan = planned(run_cli, backup, source)
    assert skipped(plan) == [
        (
            "b.txt",
            "the source's file can't be read: The process cannot access the file because"
            " it is being used by another process.",
        )
    ]
    assert "  b.txt  the source's file can't be read" in err


def test_r3_3_overwrite_of_a_skipped_file_is_refused_with_the_reason(
    tmp_path, run_cli, copied, locked
):
    backup, source, copy = copied
    put(copy / "b.txt", "BEE", T1)  # the last plan listed it for a decision
    locked(source / "b.txt")  # now it is open in another program
    code, _, err = run_cli("plan", backup, source, "--overwrite", source / "b.txt")
    assert code == 1
    assert f"{source / 'b.txt'} was skipped: the source's file can't be read:" in err
    assert not (backup / ".syncplan" / "plans").exists()


@pytest.mark.parametrize("side", ["source", "copy"])
@pytest.mark.parametrize("change", ["grows", "redated"])
def test_r2_9_a_file_that_changes_while_read_is_skipped(
    tmp_path, run_cli, copied, monkeypatch, side, change
):
    backup, source, copy = copied
    changing = (source if side == "source" else copy) / "b.txt"
    real = compare._open_shared

    def changed(path):
        opened = real(path)
        if Path(path) == changing:  # another program changes it while plan reads
            if change == "grows":
                with changing.open("ab") as writer:
                    writer.write(b"more")
            else:
                os.utime(changing, ns=(T2, T2))
        return opened

    monkeypatch.setattr(compare, "_open_shared", changed)
    _, _, plan = planned(run_cli, backup, source)
    assert skipped(plan) == [("b.txt", "changed while planning; plan again")]
    assert plan["actions"] == []


def test_r5_5_progress_lines_come_every_10_seconds_also_inside_a_file(
    tmp_path, run_cli, copied, monkeypatch
):
    backup, source, copy = copied
    for folder in (source, copy):
        put(folder / "big.bin", b"x" * 100)
    clock = iter(range(100_000))  # one second per look at the clock
    monkeypatch.setattr(compare, "monotonic", lambda: next(clock))
    monkeypatch.setattr(compare, "CHUNK", 4)
    out, _, _ = planned(run_cli, backup, source)
    assert "Reading 3 files to compare (106 bytes)..." in out
    progress = [line for line in out.splitlines() if " read (" in line]
    assert 1 <= len(progress) <= 4  # every 10 s or so, not after every one of 27 chunks
    figures = [
        re.fullmatch(r"  (\d+) bytes of 106 bytes read \((\d) of 3 files\)", line)
        for line in progress
    ]
    assert all(figures)
    inside = [f for f in figures if f.group(2) == "2" and 6 < int(f.group(1)) < 106]
    assert inside  # a line in the middle of big.bin, the third file read


def test_r6_2_a_link_in_the_source_is_skipped_with_the_copy_below_it(
    tmp_path, run_cli, copied, junction
):
    backup, source, copy = copied
    put(tmp_path / "elsewhere" / "song.mp3", "la")
    junction(source / "My Music", tmp_path / "elsewhere")
    put(copy / "My Music" / "old.mp3", "old")  # a real folder there on the backup
    out, _, plan = planned(run_cli, backup, source)
    assert skipped(plan) == [("My Music", "a link; links aren't followed")]
    assert plan["actions"] == []
    assert "no longer in" not in out  # the copy's My Music is not counted as extra


def test_r6_2_a_link_in_the_copy_is_never_written_through(tmp_path, run_cli, copied, junction):
    backup, source, copy = copied
    put(source / "2025" / "a.jpg", "a")
    (tmp_path / "elsewhere").mkdir()
    junction(copy / "2025", tmp_path / "elsewhere")
    _, _, plan = planned(run_cli, backup, source)
    assert skipped(plan) == [("2025", "a link; links aren't followed")]
    assert plan["actions"] == []
    assert list((tmp_path / "elsewhere").iterdir()) == []


def test_r6_2_a_file_link_is_skipped_too(tmp_path, run_cli, copied, twisted, opened):
    # File symlinks need admin rights here (init-marker SP13), so the listing is twisted.
    backup, source, _ = copied
    twisted(source, "b.txt", lambda entry: [Twisted(entry, link=True)])
    _, _, plan = planned(run_cli, backup, source)
    assert skipped(plan) == [("b.txt", "a link; links aren't followed")]
    assert source / "b.txt" not in opened


@pytest.mark.parametrize("name", ["a.", "b ", "CON", "x\udc80.txt"])
def test_r6_3_a_name_windows_cannot_normally_use_is_skipped(tmp_path, run_cli, copied, name):
    backup, source, _ = copied
    odd = extended(source / name)
    Path(odd).write_bytes(b"odd")
    try:
        _, _, plan = planned(run_cli, backup, source)
    finally:
        Path(odd).unlink()
    assert skipped(plan) == [(name, "a name Windows can't normally use")]


def test_r6_3_a_device_name_with_an_extension_is_an_ordinary_file(tmp_path, run_cli, copied):
    # Checked with --quick, so it is never opened on an older Windows (DD16).
    backup, source, _ = copied
    Path(extended(source / "aux.h")).write_bytes(b"#define X")
    try:
        _, _, plan = planned(run_cli, backup, source, "--quick")
    finally:
        Path(extended(source / "aux.h")).unlink()
    assert actions(plan) == [("copy", "aux.h")]


def test_r6_3_names_differing_only_in_case_are_all_skipped(
    tmp_path, run_cli, copied, twisted, opened
):
    backup, source, copy = copied
    twisted(source, "b.txt", lambda entry: [entry, Twisted(entry, name="B.TXT")])
    _, _, plan = planned(run_cli, backup, source)
    case = "names that differ only in letter case"
    assert skipped(plan) == [("b.txt", case), ("B.TXT", case)]
    assert plan["actions"] == []
    assert copy / "b.txt" not in opened


def test_r6_3_names_python_merges_but_windows_keeps_apart_are_skipped(tmp_path, run_cli, copied):
    # A final sigma: lower() makes these one key, NTFS keeps two folders (I3).
    backup, source, copy = copied
    put(source / "ΦΩΤΟΓΡΑΦΙΕΣ" / "a.txt", "a")
    put(copy / "φωτογραφιες" / "a.txt", "a")
    assert not (copy / "ΦΩΤΟΓΡΑΦΙΕΣ").exists()
    _, _, plan = planned(run_cli, backup, source)
    case = "names that differ only in letter case"
    assert sorted(skipped(plan)) == sorted([("ΦΩΤΟΓΡΑΦΙΕΣ", case), ("φωτογραφιες", case)])
    assert plan["actions"] == []


def long_backup(tmp_path: Path, run_cli, units: int) -> tuple[Path, Path]:
    """A backup whose copy, <backup>\\Photos, is exactly `units` long; the source is short."""
    make_tree(tmp_path, {"src/Photos/a.txt": "a"})
    backup = tmp_path / ("B" * (units - len(str(tmp_path)) - len("\\\\Photos")))
    backup.mkdir()
    assert len(str(backup / "Photos")) == units
    assert run_cli("init", backup, tmp_path / "src" / "Photos")[0] == 0
    return backup, tmp_path / "src" / "Photos"


def test_r6_3_a_file_too_long_for_the_copy_is_skipped(tmp_path, run_cli):
    backup, source = long_backup(tmp_path, run_cli, 230)
    put(source / ("f" * 28), "at 259")  # the copy's path: 230 + 1 + 28 = 259
    put(source / ("g" * 29), "at 260")
    _, _, plan = planned(run_cli, backup, source)
    assert ("copy", "f" * 28) in actions(plan)
    assert skipped(plan) == [("g" * 29, "path of 260 characters or more")]


def test_r6_3_a_folder_too_long_for_the_copy_is_skipped(tmp_path, run_cli):
    backup, source = long_backup(tmp_path, run_cli, 230)
    (source / ("d" * 16)).mkdir()  # the copy's path: 230 + 1 + 16 = 247
    (source / ("e" * 17)).mkdir()
    _, _, plan = planned(run_cli, backup, source)
    assert ("make folder", "d" * 16) in actions(plan)
    assert skipped(plan) == [("e" * 17, "a folder path of 248 characters or more")]


def test_r6_3_a_source_file_too_long_to_open_is_skipped(tmp_path, run_cli, copied):
    backup, source, _ = copied
    name = "L" * (260 - len(str(source)) - 1)
    Path(extended(source / name)).write_bytes(b"long")
    try:
        _, _, plan = planned(run_cli, backup, source)
    finally:
        Path(extended(source / name)).unlink()
    assert skipped(plan) == [(name, "path of 260 characters or more")]


def test_r6_4_a_file_here_and_a_folder_there_are_both_skipped(tmp_path, run_cli, copied):
    backup, source, copy = copied
    put(source / "2024", "a file")
    put(copy / "2024" / "x.jpg", "x")
    (source / "2025").mkdir()
    put(source / "2025" / "y.jpg", "y")
    put(copy / "2025", "a file")
    out, _, plan = planned(run_cli, backup, source)
    assert skipped(plan) == [
        ("2024", "a file here, a folder on the backup"),
        ("2025", "a folder here, a file on the backup"),
    ]
    assert plan["actions"] == []
    assert "no longer in" not in out


@pytest.mark.parametrize("side", ["source", "copy"])
def test_r6_5_a_syncplan_backup_inside_is_skipped(tmp_path, run_cli, copied, side):
    backup, source, copy = copied
    put(source / "Old" / "x.jpg", "x")
    hand_written((source if side == "source" else copy) / "Old", [("O", tmp_path / "o")])
    _, _, plan = planned(run_cli, backup, source)
    assert skipped(plan) == [("Old", "a syncplan backup; backups aren't backed up")]
    assert plan["actions"] == []


def test_r6_5_a_backup_only_in_the_copy_is_skipped_not_counted(tmp_path, run_cli, copied):
    backup, source, copy = copied
    hand_written(copy / "OldOnly", [("O", tmp_path / "o")])
    out, _, plan = planned(run_cli, backup, source)
    assert skipped(plan) == [("OldOnly", "a syncplan backup; backups aren't backed up")]
    assert "no longer in" not in out


@pytest.mark.parametrize("quick", [False, True])
def test_r6_6_an_online_only_file_is_skipped_and_never_opened(
    tmp_path, run_cli, copied, opened, quick
):
    backup, source, copy = copied
    put(source / "new.txt", "new")
    subprocess.run(["attrib", "+O", str(source / "b.txt")], check=True)
    subprocess.run(["attrib", "+O", str(source / "new.txt")], check=True)
    _, _, plan = planned(run_cli, backup, source, *(["--quick"] if quick else []))
    reason = 'online-only (OneDrive); make it "Always keep on this device"'
    assert skipped(plan) == [("b.txt", reason), ("new.txt", reason)]
    assert opened == ([] if quick else [source / "a" / "1.jpg", copy / "a" / "1.jpg"])


def test_r6_6_the_recall_on_access_mark_counts_as_online_only(
    tmp_path, run_cli, copied, twisted, opened
):
    backup, source, _ = copied
    twisted(source, "b.txt", lambda entry: [Twisted(entry, attributes=0x400000)])
    _, _, plan = planned(run_cli, backup, source)
    assert [path for path, _ in skipped(plan)] == ["b.txt"]
    assert source / "b.txt" not in opened


FAR = 600_000_000_000 * 1_000_000_000  # in about the year 20983 (SP16)


@pytest.mark.parametrize("side", ["source", "copy"])
def test_r6_7_a_date_after_9999_is_skipped(tmp_path, run_cli, copied, side):
    backup, source, copy = copied
    folder = source if side == "source" else copy
    os.utime(folder / "b.txt", ns=(FAR, FAR))
    _, _, plan = planned(run_cli, backup, source)
    assert skipped(plan) == [("b.txt", "a date syncplan can't record")]


def test_r4_2_and_r5_4_every_skipped_item_is_in_the_plan_file_and_the_log(
    tmp_path, run_cli, copied, program_data
):
    backup, source, _ = copied
    for number in range(55):
        put(source / "far" / f"{number:02}.txt", "x", FAR)
    _, err, plan = planned(run_cli, backup, source)
    assert len(plan["skipped"]) == 55
    assert plan["skipped"][50]["path"] == "far\\50.txt"
    assert err.count("a date syncplan can't record") == 50
    assert "  ... and 5 more (all in the plan file)" in err
    log = (program_data / "syncplan" / "logs" / "syncplan.log").read_text("utf-8")
    assert "far\\54.txt  a date syncplan can't record" in log


# Review fixes (implementation panel)


def test_r2_4_files_larger_than_a_chunk_are_compared_to_the_end(
    tmp_path, run_cli, copied, monkeypatch
):
    backup, source, copy = copied
    monkeypatch.setattr(compare, "CHUNK", 4)
    put(source / "later.bin", b"abcdefghijkl", T2)
    put(copy / "later.bin", b"abcdefghijkX", T1)
    put(source / "same.bin", b"abcdefghijkl", T1)
    put(copy / "same.bin", b"abcdefghijkX", T1)
    _, _, plan = planned(run_cli, backup, source)
    assert actions(plan) == [("overwrite", "later.bin")]
    assert decided(plan) == [("same.bin", "same date and size, different content")]


def test_r1_2_a_plan_that_reads_still_changes_nothing(tmp_path, run_cli, copied, monkeypatch):
    backup, source, copy = copied
    os.utime(copy / "a" / "1.jpg", ns=(T0 - 1_000, T0 - 1_000))  # set date
    put(source / "b.txt", "BEE", T2)  # overwrite, read
    put(source / "c.txt", "sea")
    put(copy / "c.txt", "SEA")  # a decision
    put(source / "d.txt", "dee")
    put(copy / "d.txt", "dee")  # the backup's copy can't be read
    real = compare._open_shared

    def damaged(path):
        if Path(path) == copy / "d.txt":
            raise OSError(22, "Data error (cyclic redundancy check)", str(path), 23)
        return real(path)

    monkeypatch.setattr(compare, "_open_shared", damaged)
    before = tree_snapshot(tmp_path), file_dates(tmp_path)
    _, _, plan = planned(run_cli, backup, source)
    assert {action["do"] for action in plan["actions"]} == {"set date", "overwrite"}
    assert len(plan["decide"]) == 2
    plans = "backup/.syncplan/plans"
    own = {"backup/.syncplan"}
    snapshot = {k: v for k, v in tree_snapshot(tmp_path).items() if not k.startswith(plans)}
    assert snapshot == before[0]
    dates = {k: v for k, v in file_dates(tmp_path).items() if k not in own}
    assert {k: v for k, v in dates.items() if not k.startswith(plans)} == {
        k: v for k, v in before[1].items() if k not in own
    }


def test_r6_2_a_link_windows_marks_as_another_name_is_skipped(
    tmp_path, run_cli, copied, twisted, opened
):
    # A link made from WSL: neither is_symlink() nor is_junction() (I6).
    backup, source, _ = copied
    twisted(source, "b.txt", lambda entry: [Twisted(entry, reparse_tag=0xA000001D)])
    _, _, plan = planned(run_cli, backup, source)
    assert skipped(plan) == [("b.txt", "a link; links aren't followed")]
    assert source / "b.txt" not in opened


def test_r3_3_overwrite_named_through_a_link_is_refused_with_the_reason(
    tmp_path, run_cli, copied, junction
):
    backup, source, copy = copied
    put(source / "2024" / "beach.jpg", "a")
    put(copy / "2024" / "beach.jpg", "A")  # needs a decision
    junction(source / "Album", source / "2024")
    code, _, err = run_cli("plan", backup, source, "--overwrite", source / "Album" / "beach.jpg")
    assert code == 1
    assert "was skipped: a link; links aren't followed" in err


def test_r4_2_the_plan_file_records_the_source_as_the_marker_spells_it(tmp_path, run_cli):
    make_tree(tmp_path, {"src/Photos/a.txt": "a", "backup": None})
    backup, spelled = tmp_path / "backup", tmp_path / "src" / "photos"
    hand_written(backup, [("Photos", spelled)])
    out, _, plan = planned(run_cli, backup, tmp_path / "src" / "Photos")
    assert plan["source"] == str(spelled)
    assert f"Plan for Photos ({spelled} -> " in out
