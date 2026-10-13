"""manage-sources: `syncplan add` and `syncplan remove` change an existing backup's sources."""

import json
import re
from pathlib import Path

import pytest
from conftest import make_backup, make_tree, tree_snapshot

from syncplan import init, marker, space
from syncplan.marker import Source, read_marker

MARKER = ".syncplan/marker.json"


def marker_file(backup: Path) -> Path:
    return backup / ".syncplan" / "marker.json"


def lists_source(report: str, name: str, path: Path) -> bool:
    return bool(
        re.search(rf"^\s+{re.escape(name)}\s+<- {re.escape(str(path))}(\s|$)", report, re.M)
    )


def refused(result, *expected: str) -> str:
    """Assert a refusal: exit 1, nothing on stdout, each expected text on stderr."""
    code, out, err = result
    assert code == 1, err
    assert out == ""
    for text in expected:
        assert text in err
    return err


def same_but_marker(before: dict, after: dict, backup: str = "backup") -> bool:
    """R6.4: every file and folder is as before, apart from the backup's marker."""
    key = f"{backup}/{MARKER}"
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
    assert lists_source(added, "Music", music)
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

    def full_disk(fd):
        raise OSError(28, "There is not enough space on the disk")

    monkeypatch.setattr(marker.os, "fsync", full_disk)

    code, _, err = run_cli("add", backup, music)

    assert code == 1
    assert f"Couldn't save the source list in {backup / '.syncplan'}:" in err
    assert "not enough space" in err
    assert "Details:" in err
    assert tree_snapshot(tmp_path) == before


def test_r6_3_a_marker_changed_while_measuring_is_kept(tmp_path, run_cli, music, monkeypatch):
    backup, _ = make_backup(run_cli, tmp_path, "Photos")
    edited = json.dumps(
        {"syncplan_marker": 1, "created": "2026-10-08T16:40:00+02:00", "sources": []}
    )
    real = space.folder_size

    def edited_meanwhile(folder, block):
        marker_file(backup).write_text(edited)
        return real(folder, block)

    monkeypatch.setattr(space, "folder_size", edited_meanwhile)

    code, _, err = run_cli("add", backup, music)

    assert code == 1
    assert f"The source list of {backup} changed while syncplan was running" in err
    assert "Details:" not in err
    assert marker_file(backup).read_text() == edited
    assert sorted(p.name for p in marker_file(backup).parent.iterdir()) == ["marker.json"]


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
