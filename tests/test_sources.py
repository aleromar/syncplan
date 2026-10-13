"""manage-sources: `syncplan add` and `syncplan remove` change an existing backup's sources."""

import json
import re
import shutil
import tempfile
from datetime import datetime
from pathlib import Path

import pytest
from conftest import lists_source, make_backup, make_tree, marker_names, tree_snapshot
from hypothesis import given, settings
from hypothesis import strategies as st

from syncplan import cli, init, marker, space
from syncplan.marker import Source, read_marker

MARKER = ".syncplan/marker.json"
EDITED = json.dumps(  # a marker edited by hand while a command runs (R6.3)
    {"syncplan_marker": 1, "created": "2026-10-08T16:40:00+02:00", "sources": []}
)


def marker_file(backup: Path) -> Path:
    return backup / ".syncplan" / "marker.json"


def refused(result, *expected: str) -> str:
    """Assert a refusal: exit 1, nothing on stdout, each expected text on stderr."""
    code, out, err = result
    assert code == 1, err
    assert out == ""
    for text in expected:
        assert text in err
    return err


def same_but_marker(before: dict, after: dict) -> bool:
    """R6.4: every file and folder is as before, apart from the backup's marker."""
    key = f"backup/{MARKER}"
    return {k: v for k, v in before.items() if k != key} == {
        k: v for k, v in after.items() if k != key
    }


def hand_written(backup: Path, sources, waiting=()) -> Path:
    """A marker written by hand, as before add and remove existed: (name, path) pairs."""
    data = {
        "syncplan_marker": 1,
        "created": "2026-10-08T16:40:00+02:00",
        "sources": [{"name": name, "path": str(path)} for name, path in sources],
    }
    if waiting:
        data["to_quarantine"] = [
            {"name": name, "path": str(path), "removed": "2026-10-12T10:15:00+02:00"}
            for name, path in waiting
        ]
    make_tree(backup, {MARKER: json.dumps(data)})
    return marker_file(backup)


def full_disk(fd):
    """Stands in for os.fsync: the backup drive is full, so saving fails (R6.2)."""
    raise OSError(28, "There is not enough space on the disk")


def _named_pair(pairs, name: str):
    """The (name, path) pair with this name, ignoring letter case, or None."""
    return next((pair for pair in pairs if pair[0].lower() == name.lower()), None)


@pytest.fixture
def music(tmp_path) -> Path:
    """A source folder not yet in any backup."""
    return make_tree(tmp_path, {"src/Music/song.mp3": "la"}) / "src" / "Music"


# R1 Add sources


def test_r1_1_add_appends_sources_in_order_and_keeps_the_rest(tmp_path, run_cli, music):
    backup, (photos,) = make_backup(run_cli, tmp_path, "Photos")
    docs = make_tree(tmp_path, {"src/Docs/a.txt": "a"}) / "src" / "Docs"
    created = read_marker(backup).created

    code, _, err = run_cli("add", backup, music, docs)

    assert (code, err) == (0, "")
    found = read_marker(backup)
    assert found.sources == (
        Source("Photos", photos.resolve()),
        Source("Music", music.resolve()),
        Source("Docs", docs.resolve()),
    )
    assert found.created == created


def test_r1_1_name_equals_source_chooses_the_subfolder_name(tmp_path, run_cli, music):
    backup, _ = make_backup(run_cli, tmp_path, "Photos")

    assert run_cli("add", backup, f"Tunes={music}")[0] == 0

    assert read_marker(backup).sources[-1] == Source("Tunes", music.resolve())


def test_r1_1_relative_source_is_recorded_absolute(tmp_path, run_cli, music, monkeypatch):
    backup, _ = make_backup(run_cli, tmp_path, "Photos")
    monkeypatch.chdir(music.parent)

    assert run_cli("add", backup, "Music")[0] == 0

    assert read_marker(backup).sources[-1] == Source("Music", music.resolve())


def test_r1_2_report_shows_the_added_sources_then_the_full_list(tmp_path, run_cli, music):
    backup, (photos,) = make_backup(run_cli, tmp_path, "Photos")

    code, out, _ = run_cli("add", backup, music)

    assert code == 0
    added, _, now = out.partition(f"Sources of {backup} now:\n")
    assert f"Added to syncplan backup {backup}:" in added
    assert re.search(rf"^  Music <- {re.escape(str(music))}  4\.0 KB$", added, re.M)
    assert not lists_source(added, "Photos", photos)
    assert lists_source(now, "Photos", photos)
    assert lists_source(now, "Music", music)
    assert "Waiting for the quarantine" not in out


def test_r1_3_only_the_added_sources_are_measured(tmp_path, run_cli, music, monkeypatch):
    backup, _ = make_backup(run_cli, tmp_path, "Photos")
    measured, real = [], space.folder_size

    def folder_size(folder, block):
        measured.append(folder)
        return real(folder, block)

    monkeypatch.setattr(space, "folder_size", folder_size)

    code, out, _ = run_cli("add", backup, music)

    assert code == 0
    assert measured == [music.resolve()]
    assert "Space needed: about 4.0 KB." in out


def test_r1_3_added_sources_that_do_not_fit_are_warned_about(
    tmp_path, run_cli, music, monkeypatch, sparse_file
):
    backup, _ = make_backup(run_cli, tmp_path, "Photos")
    sparse_file(music / "big.iso", 5 * 2**30)
    monkeypatch.setattr(init, "disk_usage", lambda path: (100 * 2**30, 99 * 2**30, 2**30))

    code, out, err = run_cli("add", backup, music)

    assert code == 0
    assert "Space needed: about 5.0 GB." in out
    assert "Warning: about 4.0 GB short." in err
    assert read_marker(backup).sources[-1].name == "Music"


def test_r1_4_a_folder_already_there_is_taken_over_and_named(tmp_path, run_cli, music):
    backup, _ = make_backup(run_cli, tmp_path, "Photos")
    make_tree(backup, {"Music/old.mp3": "old"})
    before = tree_snapshot(tmp_path)

    code, out, _ = run_cli("add", backup, music)

    assert code == 0
    assert (
        f"{backup / 'Music'} already exists and becomes the copy of {music.resolve()};"
        " the first plan shows what changes in it." in out
    )
    assert same_but_marker(before, tree_snapshot(tmp_path))


def test_r1_4_no_take_over_line_for_a_new_subfolder(tmp_path, run_cli, music):
    backup, _ = make_backup(run_cli, tmp_path, "Photos")

    code, out, _ = run_cli("add", backup, music)

    assert code == 0
    assert "already exists" not in out


def test_r1_5_a_source_failing_while_measured_saves_nothing(
    tmp_path, run_cli, music, failing_scandir, monkeypatch
):
    backup, _ = make_backup(run_cli, tmp_path, "Photos")
    before = tree_snapshot(tmp_path)
    failing_scandir(lambda path: path == music.resolve(), OSError(21, "The device is not ready"))

    code, _, err = run_cli("add", backup, music)

    monkeypatch.undo()  # the snapshot lists folders too
    assert code == 1
    assert "The device is not ready" in err
    assert "Details:" in err
    assert tree_snapshot(tmp_path) == before


# R4 Check the backup (through add)


def test_r4_1_missing_backup_is_refused(tmp_path, run_cli, music):
    before = tree_snapshot(tmp_path)

    refused(run_cli("add", tmp_path / "nope", music), f"{tmp_path / 'nope'} does not exist")

    assert tree_snapshot(tmp_path) == before


def test_r4_1_backup_that_is_a_file_is_refused(tmp_path, run_cli, music):
    make_tree(tmp_path, {"backup": "a file"})

    refused(run_cli("add", tmp_path / "backup", music), "is not a folder")


def test_r4_2_folder_that_is_not_a_backup_is_refused_pointing_to_init(tmp_path, run_cli, music):
    make_tree(tmp_path, {"backup": None})
    before = tree_snapshot(tmp_path)

    refused(
        run_cli("add", tmp_path / "backup", music),
        f"{tmp_path / 'backup'} is not a syncplan backup.",
        f"syncplan init {tmp_path / 'backup'}",
    )

    assert tree_snapshot(tmp_path) == before


@pytest.mark.parametrize(
    ("content", "expected"),
    [
        ("damaged", "can't be read; inspect it"),
        (json.dumps({"syncplan_marker": 99}), "newer syncplan"),
    ],
)
def test_r4_3_unreadable_or_newer_marker_is_refused_and_kept(
    tmp_path, run_cli, music, content, expected
):
    make_tree(tmp_path, {f"backup/{MARKER}": content})
    before = tree_snapshot(tmp_path)

    refused(run_cli("add", tmp_path / "backup", music), expected)

    assert tree_snapshot(tmp_path) == before


@pytest.mark.parametrize(
    ("sources", "waiting", "named"),
    [
        ([("..", "C:\\Up")], [], '"..", which syncplan can\'t use'),
        ([("Photos ", "C:\\Photos")], [], '"Photos ", which syncplan can\'t use'),
        ([("CON", "C:\\Con")], [], '"CON", which syncplan can\'t use'),
        ([("Photos", "C:\\A"), ("photos", "C:\\B")], [], '"photos" twice'),
        ([("Photos", "C:\\A")], [("PHOTOS", "C:\\B")], '"PHOTOS" twice'),
        ([], [("..", "C:\\Up")], '"..", which syncplan can\'t use'),
    ],
)
def test_r4_3_marker_with_an_unusable_or_repeated_name_is_refused(
    tmp_path, run_cli, music, sources, waiting, named
):
    # D12: acting on such a name could point the quarantine outside the backup.
    backup = tmp_path / "backup"
    path = hand_written(backup, sources, waiting)
    before = tree_snapshot(tmp_path)

    refused(run_cli("add", backup, music), f"{path} lists the name {named}", "inspect it")

    assert tree_snapshot(tmp_path) == before


@pytest.mark.parametrize("prefix", ["\\\\server\\share\\", "\\\\?\\"])
def test_r4_4_backup_path_starting_with_backslashes_is_refused(tmp_path, run_cli, music, prefix):
    refused(run_cli("add", f"{prefix}{tmp_path / 'backup'}", music), "starts with \\\\")


def test_r4_4_backup_with_a_broken_character_says_run_add_again(tmp_path, run_cli, music):
    odd = tmp_path / "B\udc80ckup"
    odd.mkdir()

    refused(run_cli("add", odd, music), "\\udc80", "rename that folder and run add again")


def test_r4_4_syncplan_folder_that_is_a_file_is_refused(tmp_path, run_cli, music):
    make_tree(tmp_path, {"backup/.syncplan": "a file"})

    refused(run_cli("add", tmp_path / "backup", music), "is not a real folder")


# R5 Check the added sources


def test_r5_1_missing_source_is_refused(tmp_path, run_cli):
    backup, _ = make_backup(run_cli, tmp_path, "Photos")
    before = tree_snapshot(tmp_path)

    refused(run_cli("add", backup, tmp_path / "Camera"), "does not exist")

    assert tree_snapshot(tmp_path) == before


def test_r5_1_two_new_sources_with_the_same_name_are_refused(tmp_path, run_cli, music):
    backup, _ = make_backup(run_cli, tmp_path, "Photos")
    other = make_tree(tmp_path, {"other/Music/x": "x"}) / "other" / "Music"

    refused(run_cli("add", backup, music, other), 'the same subfolder name "Music"')


def test_r5_1_source_inside_this_backup_gets_the_overlap_message(tmp_path, run_cli):
    # D7: the backup's own marker must not turn this into "back up its sources instead".
    backup, _ = make_backup(run_cli, tmp_path, "Photos")
    inner = make_tree(backup, {"Inner/x": "x"}) / "Inner"

    err = refused(run_cli("add", backup, inner), f"is inside the backup {backup}")

    assert "Back up its sources" not in err


def test_r5_1_another_backup_as_a_source_suggests_syncplan_add(tmp_path, run_cli):
    backup, (photos,) = make_backup(run_cli, tmp_path, "Photos")
    other = make_tree(tmp_path, {"other": None}) / "other"
    assert run_cli("init", other, photos)[0] == 0

    refused(
        run_cli("add", backup, other),
        f"{other} is a syncplan backup",
        f"syncplan add {backup} {photos}",
    )


def test_r5_1_source_with_a_broken_character_says_run_add_again(tmp_path, run_cli):
    backup, _ = make_backup(run_cli, tmp_path, "Photos")
    odd = tmp_path / "src" / "M\udc80sic"
    odd.mkdir()

    refused(run_cli("add", backup, odd), "\\udc80", "run add again")


def test_r5_2_source_already_listed_is_refused_and_nothing_added(tmp_path, run_cli, music):
    backup, (photos,) = make_backup(run_cli, tmp_path, "Photos")
    before = tree_snapshot(tmp_path)

    refused(
        run_cli("add", backup, music, photos),
        f"{photos.resolve()} is already a source of {backup} (as Photos).",
    )

    assert tree_snapshot(tmp_path) == before


def test_r5_2_the_same_folder_under_another_name_is_refused(tmp_path, run_cli):
    backup, (photos,) = make_backup(run_cli, tmp_path, "Photos")

    refused(run_cli("add", backup, f"Pics={photos}"), "already a source", "(as Photos)")


def test_r5_2_a_junction_to_a_listed_source_is_refused(tmp_path, run_cli, junction):
    backup, (photos,) = make_backup(run_cli, tmp_path, "Photos")
    link = junction(tmp_path / "Pics", photos)

    refused(run_cli("add", backup, link), "already a source", "(as Photos)")


def test_r5_3_source_inside_a_listed_source_is_refused(tmp_path, run_cli):
    backup, (photos,) = make_backup(run_cli, tmp_path, "Photos")
    year = make_tree(photos, {"2024/x.jpg": "x"}) / "2024"

    refused(
        run_cli("add", backup, year),
        f"{year.resolve()} is inside {photos.resolve()}, already a source of {backup} (as Photos).",
    )


def test_r5_3_source_holding_a_listed_source_is_refused(tmp_path, run_cli):
    backup, (photos,) = make_backup(run_cli, tmp_path, "Photos")
    src = photos.parent

    refused(
        run_cli("add", backup, src),
        f"{src.resolve()} holds {photos.resolve()}, already a source of {backup} (as Photos).",
    )


def test_r5_4_name_of_a_listed_source_is_refused(tmp_path, run_cli, music):
    backup, (photos,) = make_backup(run_cli, tmp_path, "Photos")

    refused(
        run_cli("add", backup, f"Photos={music}"),
        f'{backup} already has a source named "Photos" ({photos.resolve()}).',
        f"Photos2={music.resolve()}",
    )


def test_r5_5_add_works_while_a_listed_source_is_missing(tmp_path, run_cli, music):
    backup, (_, docs) = make_backup(run_cli, tmp_path, "Photos", "Docs")
    (docs / "Docs.txt").unlink()
    docs.rmdir()  # like a drive that isn't plugged in

    code, _, err = run_cli("add", backup, music)

    assert (code, err) == (0, "")
    assert [s.name for s in read_marker(backup).sources] == ["Photos", "Docs", "Music"]


# R6 Save the list safely (through add)


def test_r6_2_a_failed_save_gives_the_reason_and_keeps_the_marker(
    tmp_path, run_cli, music, monkeypatch
):
    backup, _ = make_backup(run_cli, tmp_path, "Photos")
    before = tree_snapshot(tmp_path)

    monkeypatch.setattr(marker.os, "fsync", full_disk)

    code, _, err = run_cli("add", backup, music)

    assert code == 1
    assert f"Couldn't save the source list in {backup / '.syncplan'}:" in err
    assert "not enough space" in err
    assert "Details:" in err
    assert tree_snapshot(tmp_path) == before


def test_r6_3_a_marker_changed_while_measuring_is_kept(tmp_path, run_cli, music, monkeypatch):
    backup, _ = make_backup(run_cli, tmp_path, "Photos")
    real = space.folder_size

    def edited_meanwhile(folder, block):
        marker_file(backup).write_text(EDITED)
        return real(folder, block)

    monkeypatch.setattr(space, "folder_size", edited_meanwhile)

    code, _, err = run_cli("add", backup, music)

    assert code == 1
    assert f"The source list of {backup} changed while syncplan was running" in err
    assert "Details:" not in err
    assert marker_file(backup).read_text() == EDITED
    assert marker_names(backup) == ["marker.json"]


def test_r6_4_add_changes_nothing_but_the_marker(tmp_path, run_cli, music):
    backup, _ = make_backup(run_cli, tmp_path, "Photos")
    before = tree_snapshot(tmp_path)

    assert run_cli("add", backup, music)[0] == 0

    after = tree_snapshot(tmp_path)
    assert same_but_marker(before, after)
    assert after[f"backup/{MARKER}"] != before[f"backup/{MARKER}"]


# NFR1 Letter case (add)


def test_nfr1_a_listed_source_typed_in_another_case_is_already_listed(
    tmp_path, run_cli, typed_case_kept
):
    backup, (photos,) = make_backup(run_cli, tmp_path, "Photos")

    refused(run_cli("add", backup, str(photos).swapcase()), "already a source", "(as Photos)")


def test_nfr1_a_folder_inside_a_listed_source_in_another_case_is_refused(
    tmp_path, run_cli, typed_case_kept
):
    backup, (photos,) = make_backup(run_cli, tmp_path, "Photos")
    make_tree(photos, {"2024/x.jpg": "x"})

    refused(run_cli("add", backup, f"{str(photos).upper()}\\2024"), "is inside", "(as Photos)")


def test_nfr1_a_listed_name_in_another_case_is_taken(tmp_path, run_cli, music, typed_case_kept):
    backup, _ = make_backup(run_cli, tmp_path, "Photos")

    refused(run_cli("add", backup, f"photos={music}"), 'already has a source named "Photos"')


# NFR2 Logging and one run at a time (add)


def test_nfr2_add_is_logged_step_by_step(tmp_path, run_cli, music, program_data):
    backup, _ = make_backup(run_cli, tmp_path, "Photos")

    assert run_cli("add", backup, music)[0] == 0

    log = (program_data / "syncplan" / "logs" / "syncplan.log").read_text(encoding="utf-8")
    add_run = log[log.index(f"command: add {backup}") :]
    for line in [
        f"backup {backup}: ok (1 sources, 0 waiting)",
        f"source {music.resolve()}: ok (subfolder Music)",
        "listed sources and waiting folders: ok",
        f"marker replaced: {marker_file(backup)}",
        "INFO  ",
        "exit 0 after",
    ]:
        assert line in add_run, line


def test_nfr2_add_is_refused_while_another_run_holds_the_lock(tmp_path, run_cli, music, held_lock):
    backup = tmp_path / "backup"
    hand_written(backup, [("Photos", tmp_path / "src" / "Photos")])  # init would be refused too
    before = tree_snapshot(tmp_path)

    refused(run_cli("add", backup, music), "Another syncplan is already running")

    assert tree_snapshot(tmp_path) == before


def test_dd9_add_and_remove_need_at_least_one_argument(tmp_path, run_cli):
    backup, _ = make_backup(run_cli, tmp_path, "Photos")

    for command, usage in [
        ("add", "BACKUP SOURCE [SOURCE ...]"),
        ("remove", "BACKUP NAME [NAME ...]"),
    ]:
        code, _, err = run_cli(command, backup)
        assert code == 2
        assert f"usage: syncplan {command} [-h] {usage}" in err


# R2 Remove sources


@pytest.fixture
def three(tmp_path, run_cli) -> tuple[Path, list[Path]]:
    """A backup of Photos, Docs and Music; Photos and Music already have a copy on it."""
    backup, sources = make_backup(run_cli, tmp_path, "Photos", "Docs", "Music")
    make_tree(backup, {"Photos/a.jpg": "a", "Music/b.mp3": "b"})
    return backup, sources


def test_r2_1_remove_takes_sources_off_and_keeps_the_rest(run_cli, three):
    backup, (photos, docs, music) = three
    created = read_marker(backup).created

    code, _, err = run_cli("remove", backup, "Photos", "Music")

    assert (code, err) == (0, "")
    found = read_marker(backup)
    assert found.sources == (Source("Docs", docs.resolve()),)
    assert [(w.name, w.path) for w in found.waiting] == [
        ("Photos", photos.resolve()),
        ("Music", music.resolve()),
    ]
    assert found.created == created


def test_r2_1_two_removes_in_a_row_keep_the_first_waiting_folder(run_cli, three):
    backup, _ = three

    assert run_cli("remove", backup, "Photos")[0] == 0
    assert run_cli("remove", backup, "Music")[0] == 0

    assert [w.name for w in read_marker(backup).waiting] == ["Photos", "Music"]


def test_r2_2_report_shows_the_removed_sources_their_fate_and_the_full_list(run_cli, three):
    backup, (photos, _, _) = three

    code, out, _ = run_cli("remove", backup, "Photos", "Docs")

    assert code == 0
    removed, _, now = out.partition(f"Sources of {backup} now:\n")
    assert f"Removed from syncplan backup {backup}:" in removed
    assert lists_source(removed, "Photos", photos.resolve())
    assert (
        f"The next plan will move {backup / 'Photos'} to the quarantine;"
        " nothing moves until you apply that plan." in removed
    )
    assert f"{backup} held no copy of Docs, so nothing waits for the quarantine." in removed
    assert lists_source(now, "Music", backup.parent / "src" / "Music")
    assert not lists_source(now, "Photos", photos.resolve())
    assert f"Waiting for the quarantine:\n  Photos (old copy of {photos.resolve()})\n" in now


def test_r2_3_removing_the_last_source_keeps_a_backup_with_none(tmp_path, run_cli):
    backup, _ = make_backup(run_cli, tmp_path, "Photos")
    before = tree_snapshot(tmp_path)

    code, out, _ = run_cli("remove", backup, "Photos")

    assert code == 0
    assert read_marker(backup).sources == ()
    assert f"Sources of {backup} now:\n  (no sources)\n" in out
    assert same_but_marker(before, tree_snapshot(tmp_path))


def test_r2_4_a_name_that_is_not_listed_refuses_everything(run_cli, three, tmp_path):
    backup, _ = three
    before = tree_snapshot(tmp_path)

    err = refused(
        run_cli("remove", backup, "Photos", "Musik"),
        f'{backup} has no source named "Musik". Its sources:',
    )

    assert lists_source(err, "Music", tmp_path / "src" / "Music")
    assert tree_snapshot(tmp_path) == before


def test_r2_4_a_listed_folder_given_instead_of_its_name_gets_the_name(run_cli, three):
    backup, (photos, _, _) = three

    refused(
        run_cli("remove", backup, photos),
        f"remove takes subfolder names, not folders. {photos.resolve()} is listed as Photos:",
        f"syncplan remove {backup} Photos",
    )


def test_r2_4_a_bare_drive_given_instead_of_its_name_gets_the_name(tmp_path, run_cli, monkeypatch):
    # D5: "X:" means X:\, not the current folder on X: (init-marker D8). The drive is the
    # temporary folder's own, so changing into that folder really moves X:'s current folder.
    drive = tmp_path.drive
    backup = tmp_path / "backup"
    hand_written(backup, [("Drive", f"{drive}\\")])
    monkeypatch.chdir(tmp_path)

    refused(run_cli("remove", backup, drive), f"{drive}\\ is listed as Drive")


def test_r2_4_a_network_path_given_as_a_name_is_never_looked_up(run_cli, three, monkeypatch):
    # D5: resolving \\nas\photos would contact that server; add refuses such paths unread.
    backup, _ = three
    real = Path.resolve

    def resolve(self, *args, **kwargs):
        assert not str(self).startswith("\\\\"), f"looked up {self}"
        return real(self, *args, **kwargs)

    monkeypatch.setattr(Path, "resolve", resolve)

    refused(run_cli("remove", backup, "\\\\nas\\photos"), 'has no source named "\\\\nas\\photos"')


def test_r2_4_a_folder_that_is_not_listed_gets_the_list(run_cli, three, tmp_path):
    backup, _ = three

    refused(run_cli("remove", backup, tmp_path / "Elsewhere"), "has no source named")


def test_r2_5_the_same_name_twice_is_refused(run_cli, three, tmp_path):
    backup, _ = three
    before = tree_snapshot(tmp_path)

    refused(run_cli("remove", backup, "Photos", "Photos"), '"Photos" is given twice.')

    assert tree_snapshot(tmp_path) == before


def test_r2_5_a_repeated_name_is_reported_before_an_unknown_one(run_cli, three):
    # D6: duplicates are checked first, whether or not the name is listed.
    backup, _ = three

    refused(run_cli("remove", backup, "Musik", "Musik"), '"Musik" is given twice.')
    refused(run_cli("remove", backup, "Photos", "Musik", "photos"), '"photos" is given twice.')


# R3 Folders waiting for the quarantine


def test_r3_1_a_removed_source_with_a_copy_waits_and_its_folder_is_untouched(
    run_cli, three, tmp_path
):
    backup, (photos, _, _) = three
    before = tree_snapshot(tmp_path)

    assert run_cli("remove", backup, "Photos")[0] == 0

    (waiting,) = read_marker(backup).waiting
    assert (waiting.name, waiting.path) == ("Photos", photos.resolve())
    assert waiting.removed.utcoffset() is not None
    assert abs((datetime.now().astimezone() - waiting.removed).total_seconds()) < 60
    assert same_but_marker(before, tree_snapshot(tmp_path))


def test_r3_2_a_removed_source_without_a_copy_leaves_nothing_waiting(run_cli, three, tmp_path):
    backup, _ = three
    before = tree_snapshot(tmp_path)

    code, out, _ = run_cli("remove", backup, "Docs")

    assert code == 0
    assert read_marker(backup).waiting == ()
    assert "held no copy of Docs" in out
    assert "Waiting for the quarantine" not in out
    assert same_but_marker(before, tree_snapshot(tmp_path))


def test_r3_3_adding_a_removed_source_back_keeps_its_copy(run_cli, three, tmp_path):
    backup, (photos, _, _) = three
    run_cli("remove", backup, "Photos")
    before = tree_snapshot(tmp_path)

    code, out, _ = run_cli("add", backup, photos)

    assert code == 0
    found = read_marker(backup)
    assert found.waiting == ()
    assert found.sources[-1] == Source("Photos", photos.resolve())
    assert (
        f"{backup / 'Photos'} was waiting for the quarantine; it is kept and backs up"
        f" {photos.resolve()} again." in out
    )
    assert "already exists" not in out
    assert same_but_marker(before, tree_snapshot(tmp_path))


def test_r3_3_adding_back_after_the_copy_was_deleted_by_hand(run_cli, three, tmp_path):
    backup, (photos, _, _) = three
    run_cli("remove", backup, "Photos")
    (backup / "Photos" / "a.jpg").unlink()
    (backup / "Photos").rmdir()
    before = tree_snapshot(tmp_path)

    code, out, _ = run_cli("add", backup, photos)

    assert code == 0
    found = read_marker(backup)
    assert found.waiting == ()
    assert found.sources[-1] == Source("Photos", photos.resolve())
    assert "was waiting" not in out
    assert "already exists" not in out
    assert same_but_marker(before, tree_snapshot(tmp_path))


def test_r3_3_taking_one_back_keeps_the_other_waiting_folders(run_cli, three):
    backup, (photos, _, _) = three
    run_cli("remove", backup, "Photos", "Music")
    music_waiting = read_marker(backup).waiting[1]

    assert run_cli("add", backup, photos)[0] == 0

    assert read_marker(backup).waiting == (music_waiting,)


def test_r3_4_another_folder_cannot_take_a_waiting_name(run_cli, three, tmp_path):
    backup, (photos, _, _) = three
    run_cli("remove", backup, "Photos")
    other = make_tree(tmp_path, {"other/Photos/x.jpg": "x"}) / "other" / "Photos"
    before = tree_snapshot(tmp_path)

    refused(
        run_cli("add", backup, other),
        f"{backup / 'Photos'} holds the old copy of {photos.resolve()} and is waiting for the"
        " quarantine. Pick another name, for example",
        f"Photos2={other.resolve()}",
    )

    assert tree_snapshot(tmp_path) == before


def test_dd4_the_same_folder_under_a_new_name_leaves_the_old_copy_waiting(run_cli, three):
    backup, (photos, _, _) = three
    run_cli("remove", backup, "Photos")

    assert run_cli("add", backup, f"Pics={photos}")[0] == 0

    found = read_marker(backup)
    assert [w.name for w in found.waiting] == ["Photos"]
    assert found.sources[-1] == Source("Pics", photos.resolve())


def test_r3_5_a_file_where_the_copy_would_be_is_refused(tmp_path, run_cli):
    backup, _ = make_backup(run_cli, tmp_path, "Photos")
    make_tree(backup, {"Photos": "a file"})
    before = tree_snapshot(tmp_path)

    refused(
        run_cli("remove", backup, "Photos"),
        f"{backup / 'Photos'} is not a real folder, so it can't wait for the quarantine.",
        f"Move it out of {backup}, then run remove again.",
    )

    assert tree_snapshot(tmp_path) == before


def test_r3_5_a_link_where_the_copy_would_be_is_refused(tmp_path, run_cli, junction):
    backup, (photos,) = make_backup(run_cli, tmp_path, "Photos")
    junction(backup / "Photos", photos)

    refused(run_cli("remove", backup, "Photos"), "is not a real folder")


def long_name(backup: Path, units: int, emoji: bool = False) -> str:
    """A NAME that makes <backup>\\<name> exactly `units` UTF-16 units long, as Windows counts.

    With emoji, the name starts with a camera emoji: one character, but two units.
    """
    rest = units - len(str(backup)) - 1
    name = "\U0001f4f7" + "N" * (rest - 2) if emoji else "N" * rest
    assert len(f"{backup}\\{name}".encode("utf-16-le")) // 2 == units
    return name


@pytest.mark.parametrize("emoji", [False, True])
def test_r3_5_a_copy_whose_path_is_too_long_to_check_is_refused(tmp_path, run_cli, emoji):
    # SP6: Windows reports such a folder as missing even when it exists. With the emoji
    # the path is 259 characters to Python but 260 to Windows.
    backup = tmp_path / "backup"
    name = long_name(backup, 260, emoji)
    hand_written(backup, [(name, tmp_path / "src" / "Long")])
    before = tree_snapshot(tmp_path)

    refused(run_cli("remove", backup, name), "260 characters or more", "then run remove again")

    assert tree_snapshot(tmp_path) == before


@pytest.mark.parametrize("emoji", [False, True])
def test_r5_6_a_name_that_makes_the_copy_path_too_long_is_refused(tmp_path, run_cli, music, emoji):
    backup, _ = make_backup(run_cli, tmp_path, "Photos")
    before = tree_snapshot(tmp_path)

    refused(
        run_cli("add", backup, f"{long_name(backup, 260, emoji)}={music}"),
        "would be a path of 260 characters or more, too long for syncplan.",
        "Pick a shorter name with NAME=SOURCE.",
    )

    assert tree_snapshot(tmp_path) == before


def test_r5_6_a_copy_path_just_under_the_limit_is_accepted(tmp_path, run_cli, music):
    backup, _ = make_backup(run_cli, tmp_path, "Photos")

    assert run_cli("add", backup, f"{long_name(backup, 259)}={music}")[0] == 0


def test_d11_a_backup_gone_while_its_copies_are_looked_at_stops_the_run(
    run_cli, three, tmp_path, monkeypatch
):
    # The drive is pulled between reading the marker and looking at the subfolder (SP6).
    backup, _ = three
    before = tree_snapshot(tmp_path)
    real_lstat, real_stat = Path.lstat, Path.stat

    def gone(path):
        raise FileNotFoundError(3, "The system cannot find the path specified", str(path))

    monkeypatch.setattr(
        Path, "lstat", lambda self: gone(self) if self.parent == backup else real_lstat(self)
    )
    monkeypatch.setattr(
        Path, "stat", lambda self, **kw: gone(self) if self == backup else real_stat(self, **kw)
    )

    code, _, err = run_cli("remove", backup, "Photos")

    monkeypatch.undo()
    assert code == 1
    assert f"{backup}: The system cannot find the path specified." in err
    assert "Details:" in err
    assert tree_snapshot(tmp_path) == before


def test_d11_a_copy_that_cannot_be_looked_at_stops_the_run(run_cli, three, tmp_path, monkeypatch):
    backup, _ = three
    before = tree_snapshot(tmp_path)
    real = Path.lstat

    def denied(self):
        if self.name == "Photos" and self.parent.name == "backup":
            raise PermissionError(13, "Access is denied", str(self))
        return real(self)

    monkeypatch.setattr(Path, "lstat", denied)

    code, _, err = run_cli("remove", backup, "Photos")

    monkeypatch.undo()
    assert code == 1
    assert "Access is denied" in err
    assert "Details:" in err
    assert tree_snapshot(tmp_path) == before


# R4 and R6 through remove


def test_r4_2_remove_on_a_folder_that_is_not_a_backup_is_refused(tmp_path, run_cli):
    make_tree(tmp_path, {"backup": None})

    refused(run_cli("remove", tmp_path / "backup", "Photos"), "is not a syncplan backup")


def test_r4_3_remove_with_an_unusable_name_in_the_marker_is_refused(tmp_path, run_cli):
    backup = tmp_path / "backup"
    path = hand_written(backup, [("..", "C:\\Up")])
    before = tree_snapshot(tmp_path)

    refused(
        run_cli("remove", backup, ".."), f'{path} lists the name "..", which syncplan can\'t use'
    )

    assert tree_snapshot(tmp_path) == before


def test_r4_4_remove_on_a_broken_backup_path_says_run_remove_again(tmp_path, run_cli):
    odd = tmp_path / "B\udc80ckup"
    odd.mkdir()

    refused(run_cli("remove", odd, "Photos"), "rename that folder and run remove again")


def test_r6_2_remove_that_cannot_save_keeps_the_marker(run_cli, three, tmp_path, monkeypatch):
    backup, _ = three
    before = tree_snapshot(tmp_path)

    monkeypatch.setattr(marker.os, "fsync", full_disk)

    code, _, err = run_cli("remove", backup, "Photos")

    assert code == 1
    assert "Couldn't save the source list in" in err
    assert "Details:" in err
    assert tree_snapshot(tmp_path) == before


def test_r6_3_remove_keeps_a_marker_changed_meanwhile(run_cli, three, monkeypatch):
    backup, _ = three
    real = Path.lstat

    def edited_meanwhile(self):
        if self.name == "Photos":
            marker_file(backup).write_text(EDITED)
        return real(self)

    monkeypatch.setattr(Path, "lstat", edited_meanwhile)

    code, _, err = run_cli("remove", backup, "Photos")

    assert code == 1
    assert "changed while syncplan was running" in err
    assert marker_file(backup).read_text() == EDITED


# NFR1 and NFR2 for remove


def test_nfr1_names_ignore_letter_case(run_cli, three):
    backup, _ = three

    assert run_cli("remove", backup, "photos")[0] == 0
    refused(run_cli("remove", backup, "Music", "MUSIC"), '"MUSIC" is given twice.')

    assert [s.name for s in read_marker(backup).sources] == ["Docs", "Music"]


def test_nfr1_a_waiting_name_in_another_case(run_cli, three, tmp_path):
    backup, (photos, _, _) = three
    run_cli("remove", backup, "Photos")
    other = make_tree(tmp_path, {"other/x": "x"}) / "other"

    refused(run_cli("add", backup, f"photos={other}"), "is waiting for the quarantine")
    assert run_cli("add", backup, f"photos={photos}")[0] == 0  # the same folder: taken back

    assert read_marker(backup).waiting == ()


def test_nfr2_remove_is_logged_step_by_step(run_cli, three, program_data):
    backup, _ = three

    assert run_cli("remove", backup, "Photos")[0] == 0

    log = (program_data / "syncplan" / "logs" / "syncplan.log").read_text(encoding="utf-8")
    remove_run = log[log.index(f"command: remove {backup} Photos") :]
    for line in [
        f"backup {backup}: ok (3 sources, 0 waiting)",
        f"subfolder {backup / 'Photos'}: a plain folder, waiting for the quarantine",
        f"marker replaced: {marker_file(backup)}",
        "exit 0 after",
    ]:
        assert line in remove_run, line


# A property: random sequences of add and remove (R3.1, R3.3, R3.4, R6)

_NAMES = ["Photos", "Docs", "Music"]
_STEPS = st.lists(
    st.tuples(
        st.sampled_from(["remove", "add back", "new name", "taken name", "delete copy"]),
        st.lists(st.sampled_from(_NAMES), min_size=1, max_size=3, unique=True),
        st.booleans(),
    ),
    min_size=1,
    max_size=6,
)


def _expected_add(listed, waiting, typed: str, path: Path) -> bool:
    """The model of add (R5.2, R5.4, R3.3, R3.4): True if it succeeds; updates the lists."""
    if any(p == path for _, p in listed) or _named_pair(listed, typed):
        return False
    if (old := _named_pair(waiting, typed)) is not None:
        if old[1] != path:
            return False
        waiting.remove(old)
    listed.append((typed, path))
    return True


@settings(max_examples=25, deadline=None)
@given(_STEPS, st.lists(st.booleans(), min_size=3, max_size=3))
def test_r3_property_add_and_remove_follow_the_model(steps, has_copy):
    """Every step's exit code and lists match a model of R2, R3.1-R3.4 and R5.2-R5.4."""
    with tempfile.TemporaryDirectory() as folder:
        root = Path(folder).resolve()
        tree = {f"src/{n}/{n}.txt": n for n in _NAMES} | {"backup": None, "other/x": "x"}
        tree |= {
            f"backup/{n}/old.txt": "old" for n, copy in zip(_NAMES, has_copy, strict=True) if copy
        }
        make_tree(root, tree)
        backup, src = root / "backup", {n: root / "src" / n for n in _NAMES}
        assert cli.main(["init", str(backup), *map(str, src.values())]) == 0
        created = read_marker(backup).created
        listed = [(n, src[n]) for n in _NAMES]  # the model: (name, path) pairs
        waiting: list[tuple[str, Path]] = []
        for step, names, upper in steps:
            typed = [n.upper() if upper else n for n in names]
            before = read_marker(backup)
            if step == "delete copy":
                if _named_pair(waiting, names[0]) and (backup / names[0]).exists():
                    shutil.rmtree(backup / names[0])
                continue
            if step == "remove":
                code = cli.main(["remove", str(backup), *typed])
                removed = [_named_pair(listed, n) for n in typed]
                ok = all(removed)
                if ok:
                    for pair in removed:
                        listed.remove(pair)
                        if (backup / pair[0]).is_dir():  # R3.1, else R3.2
                            waiting.append(pair)
            else:
                path = {"taken name": root / "other"}.get(step, src[names[0]])
                name = f"{names[0]}Again" if step == "new name" else typed[0]
                code = cli.main(["add", str(backup), f"{name}={path}"])
                ok = _expected_add(listed, waiting, name, path)
            after = read_marker(backup)
            assert code == (0 if ok else 1), (step, typed)
            if not ok:
                assert after.raw == before.raw
            assert [(s.name, s.path) for s in after.sources] == listed
            assert [(w.name, w.path) for w in after.waiting] == waiting
            assert after.created == created
