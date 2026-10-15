"""plan-copies: `syncplan plan` compares a source with its copy and writes a plan of copies."""

import json
from pathlib import Path

import pytest
from conftest import file_dates, make_backup, make_tree, tree_snapshot

from syncplan import space

MARKER = ".syncplan/marker.json"


def refuses(run_cli, root: Path, *args: object, expected: tuple[str, ...] = ()) -> str:
    """Run plan and assert a refusal that changed nothing under root (R1.2). Returns stderr."""
    before = tree_snapshot(root), file_dates(root)
    code, out, err = run_cli("plan", *args)
    assert code == 1, err
    assert out == ""
    for text in expected:
        assert text in err
    assert (tree_snapshot(root), file_dates(root)) == before
    return err


def hand_written(backup: Path, sources) -> None:
    """A marker written by hand: (name, path) pairs, paths taken as they are."""
    data = {
        "syncplan_marker": 1,
        "created": "2026-10-08T16:40:00+02:00",
        "sources": [{"name": name, "path": str(path)} for name, path in sources],
    }
    make_tree(backup, {MARKER: json.dumps(data)})


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
        expected=(f"{backup / '.syncplan' / 'plans'} is not a real folder", "run plan again"),
    )


def test_r7_1_plans_folder_that_is_a_link_is_refused(tmp_path, run_cli, photos, junction):
    backup, source = photos
    make_tree(tmp_path, {"elsewhere": None})
    junction(backup / ".syncplan" / "plans", tmp_path / "elsewhere")
    refuses(run_cli, tmp_path, backup, source, expected=("is not a real folder",))


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
    backup = tmp_path / "backup"
    hand_written(backup, [("Cam", Path("Q:\\Cam"))])
    refuses(run_cli, tmp_path, backup, "Q:\\Cam", expected=("doesn't exist",))


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
