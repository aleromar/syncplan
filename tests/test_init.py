import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest
from conftest import make_tree
from hypothesis import given, settings
from hypothesis import strategies as st

from syncplan import marker
from syncplan.init import Refusal, check_name, read_argument, subfolder_name
from syncplan.marker import Source, read_marker


def sources_of(backup: Path) -> tuple[Source, ...]:
    return read_marker(backup).sources


def lists_source(report: str, name: str, path: Path) -> bool:
    return re.search(rf"^\s+{re.escape(name)}\s+<- {re.escape(str(path))}(\s|$)", report, re.M)


def setup_folders(tmp_path: Path) -> tuple[Path, Path, Path]:
    make_tree(tmp_path, {"src/Photos/a.jpg": "a", "src/Docs/b.txt": "b", "backup": None})
    return tmp_path / "backup", tmp_path / "src" / "Photos", tmp_path / "src" / "Docs"


# R1 Initialize a backup


def test_r1_1_init_writes_the_marker_and_exits_0(tmp_path, run_cli):
    backup, photos, docs = setup_folders(tmp_path)

    code, _, err = run_cli("init", backup, photos, docs)

    assert (code, err) == (0, "")
    assert sources_of(backup) == (
        Source("Photos", photos.resolve()),
        Source("Docs", docs.resolve()),
    )


def test_r1_1_init_without_sources_writes_an_empty_list(tmp_path, run_cli):
    backup, _, _ = setup_folders(tmp_path)

    code, out, _ = run_cli("init", backup)

    assert code == 0
    assert sources_of(backup) == ()
    assert "(no sources)" in out


def test_r1_2_subfolder_is_named_after_the_source_folder(tmp_path, run_cli):
    backup, photos, _ = setup_folders(tmp_path)

    run_cli("init", backup, photos)

    assert sources_of(backup)[0].name == "Photos"


def test_r1_2_name_equals_source_chooses_the_subfolder_name(tmp_path, run_cli):
    backup, _, docs = setup_folders(tmp_path)

    code, _, _ = run_cli("init", backup, f"WorkDocs={docs}")

    assert code == 0
    assert sources_of(backup) == (Source("WorkDocs", docs.resolve()),)


def test_r1_3_relative_paths_are_recorded_absolute(tmp_path, run_cli, monkeypatch):
    backup, photos, _ = setup_folders(tmp_path)
    monkeypatch.chdir(tmp_path / "src")

    code, _, _ = run_cli("init", "..\\backup", "Photos")

    assert code == 0
    recorded = sources_of(backup)[0].path
    assert recorded.is_absolute()
    assert recorded == photos.resolve()


def test_r1_3_junction_source_is_recorded_by_its_target(tmp_path, run_cli, junction):
    backup, photos, _ = setup_folders(tmp_path)
    link = junction(tmp_path / "Pics", photos)

    code, _, _ = run_cli("init", backup, link)

    assert code == 0
    assert sources_of(backup) == (Source("Photos", photos.resolve()),)


def test_r1_4_report_names_the_backup_and_each_source(tmp_path, run_cli):
    backup, photos, docs = setup_folders(tmp_path)

    _, out, _ = run_cli("init", backup, photos, f"Papers={docs}")

    assert f"Initialized syncplan backup at {backup.resolve()}" in out
    assert lists_source(out, "Photos", photos.resolve())
    assert lists_source(out, "Papers", docs.resolve())


def test_r1_4_report_survives_redirected_output_with_a_non_english_name(tmp_path):
    make_tree(tmp_path, {"\u5199\u771f/a.jpg": "a", "backup": None})
    env = {k: v for k, v in os.environ.items() if k not in ("PYTHONIOENCODING", "PYTHONUTF8")}
    code = "import sys; from syncplan.cli import main; sys.exit(main(sys.argv[1:]))"

    backup, source = tmp_path / "backup", tmp_path / "\u5199\u771f"

    result = subprocess.run(
        [sys.executable, "-c", code, "init", str(backup), str(source)], capture_output=True, env=env
    )

    assert result.returncode == 0, result.stderr
    assert "\u5199\u771f" in result.stdout.decode("utf-8")


def test_r1_5_existing_content_of_the_backup_is_left_unchanged(tmp_path, run_cli):
    backup, photos, _ = setup_folders(tmp_path)
    make_tree(backup, {"notes.txt": "mine", "Photos/old.jpg": "old copy"})

    code, _, _ = run_cli("init", backup, photos)

    assert code == 0
    assert sorted(p.name for p in backup.iterdir()) == [".syncplan", "Photos", "notes.txt"]
    assert (backup / "notes.txt").read_text() == "mine"
    assert [p.name for p in (backup / "Photos").iterdir()] == ["old.jpg"]
    assert (backup / "Photos" / "old.jpg").read_text() == "old copy"


def test_r1_6_failed_write_exits_1_with_the_reason_and_leaves_no_marker(
    tmp_path, run_cli, monkeypatch
):
    backup, photos, _ = setup_folders(tmp_path)

    def full_disk(fd):
        raise OSError(28, "There is not enough space on the disk")

    monkeypatch.setattr(marker.os, "fsync", full_disk)

    code, _, err = run_cli("init", backup, photos)

    assert code == 1
    assert "Couldn't write the marker" in err
    assert "not enough space" in err
    assert list(backup.iterdir()) == []


def test_r1_6_marker_written_meanwhile_by_another_init_is_kept(tmp_path, run_cli, monkeypatch):
    backup, photos, docs = setup_folders(tmp_path)
    real_fsync = marker.os.fsync
    raced = []

    def other_init_wins(fd):
        real_fsync(fd)
        if not raced:
            raced.append(True)
            marker.write_marker(backup.resolve(), [Source("Docs", docs.resolve())])

    monkeypatch.setattr(marker.os, "fsync", other_init_wins)

    code, _, err = run_cli("init", backup, photos)

    assert code == 1
    assert "while init was running" in err
    assert sources_of(backup) == (Source("Docs", docs.resolve()),)
    assert sorted(p.name for p in (backup / ".syncplan").iterdir()) == ["marker.json"]


def test_r1_7_the_same_source_can_go_to_a_second_backup(tmp_path, run_cli):
    backup, photos, _ = setup_folders(tmp_path)
    second = tmp_path / "backup2"
    second.mkdir()

    first_code, _, _ = run_cli("init", backup, photos)
    second_code, _, _ = run_cli("init", second, photos)

    assert (first_code, second_code) == (0, 0)
    assert sources_of(backup) == sources_of(second) == (Source("Photos", photos.resolve()),)


# R2.1-R2.4 Check the backup folder


def test_r2_1_missing_backup_folder_is_refused_and_not_created(tmp_path, run_cli):
    _, photos, _ = setup_folders(tmp_path)
    missing = tmp_path / "Backpus"

    code, _, err = run_cli("init", missing, photos)

    assert code == 1
    assert f"{missing} does not exist" in err
    assert not missing.exists()


def test_r2_1_backup_that_is_a_file_is_refused(tmp_path, run_cli):
    _, photos, _ = setup_folders(tmp_path)
    file = make_tree(tmp_path, {"file.txt": "x"}) / "file.txt"

    code, _, err = run_cli("init", file, photos)

    assert code == 1
    assert "is not a folder" in err


def test_r2_2_existing_backup_is_refused_showing_its_date_and_sources(tmp_path, run_cli):
    backup, photos, docs = setup_folders(tmp_path)
    run_cli("init", backup, photos)
    before = (backup / ".syncplan" / "marker.json").read_bytes()
    created = read_marker(backup).created.date().isoformat()

    code, _, err = run_cli("init", backup, docs)

    assert code == 1
    assert f"is already a syncplan backup (created {created})" in err
    assert lists_source(err, "Photos", photos.resolve())
    assert "syncplan add / syncplan remove" in err
    assert (backup / ".syncplan" / "marker.json").read_bytes() == before


def test_r2_3_unreadable_marker_is_refused_and_left_untouched(tmp_path, run_cli):
    backup, photos, _ = setup_folders(tmp_path)
    make_tree(backup, {".syncplan/marker.json": "damaged"})

    code, _, err = run_cli("init", backup, photos)

    assert code == 1
    assert "can't be read; inspect it" in err
    assert (backup / ".syncplan" / "marker.json").read_text() == "damaged"


def test_r2_3_marker_from_a_newer_syncplan_is_refused(tmp_path, run_cli):
    backup, photos, _ = setup_folders(tmp_path)
    make_tree(backup, {".syncplan/marker.json": '{"syncplan_marker": 99}'})

    code, _, err = run_cli("init", backup, photos)

    assert code == 1
    assert "upgrade" in err


def test_r2_4_backup_inside_another_backup_is_refused(tmp_path, run_cli):
    backup, photos, _ = setup_folders(tmp_path)
    run_cli("init", backup)
    inner = make_tree(backup, {"Other": None}) / "Other"

    code, _, err = run_cli("init", inner, photos)

    assert code == 1
    assert f"{inner.resolve()} is inside the syncplan backup {backup.resolve()}" in err
    assert not (inner / ".syncplan").exists()


def test_r2_4_damaged_marker_above_still_counts_as_a_backup(tmp_path, run_cli):
    backup, photos, _ = setup_folders(tmp_path)
    make_tree(backup, {".syncplan/marker.json": "damaged", "Other": None})

    code, _, err = run_cli("init", backup / "Other", photos)

    assert code == 1
    assert "is inside the syncplan backup" in err


def test_r2_4_relative_path_into_a_backup_is_refused(tmp_path, run_cli, monkeypatch):
    backup, photos, _ = setup_folders(tmp_path)
    run_cli("init", backup)
    make_tree(backup, {"Sub/Other": None})
    monkeypatch.chdir(backup / "Sub")

    code, _, err = run_cli("init", "Other", photos)

    assert code == 1
    assert "is inside the syncplan backup" in err


def test_r2_4_junction_into_a_backup_is_refused(tmp_path, run_cli, junction):
    backup, photos, _ = setup_folders(tmp_path)
    run_cli("init", backup)
    inner = make_tree(backup, {"Other": None}) / "Other"
    link = junction(tmp_path / "LooksSafe", inner)

    code, _, err = run_cli("init", link, photos)

    assert code == 1
    assert "is inside the syncplan backup" in err


def test_r2_2_folder_own_marker_is_reported_before_one_above_it(tmp_path, run_cli):
    # Design D3: for a whole-drive backup like E:\ (no folders above) and for nested
    # leftovers alike, the folder's own marker decides the message.
    backup, photos, _ = setup_folders(tmp_path)
    make_tree(backup, {"Inner": None})
    run_cli("init", backup / "Inner")
    run_cli("init", backup)

    code, _, err = run_cli("init", backup / "Inner", photos)

    assert code == 1
    assert "is already a syncplan backup" in err


# R3 Check the sources, and NFR1


def refused_and_nothing_written(result, backup: Path, *expected: str) -> bool:
    code, _, err = result
    assert code == 1, err
    for text in expected:
        assert text in err
    return not (backup / ".syncplan").exists()


def test_r3_1_missing_source_is_refused(tmp_path, run_cli):
    backup, photos, _ = setup_folders(tmp_path)
    camera = tmp_path / "Camera"

    result = run_cli("init", backup, photos, camera)

    assert refused_and_nothing_written(result, backup, f"{camera} does not exist")


def test_r3_1_source_that_is_a_file_is_refused(tmp_path, run_cli):
    backup, photos, _ = setup_folders(tmp_path)

    result = run_cli("init", backup, photos / "a.jpg")

    assert refused_and_nothing_written(result, backup, "is not a folder")


def test_r3_2_backup_inside_a_source_is_refused(tmp_path, run_cli):
    home = make_tree(tmp_path, {"me/Backup": None, "me/Photos/a.jpg": "a"}) / "me"

    result = run_cli("init", home / "Backup", home)

    assert refused_and_nothing_written(
        result, home / "Backup", f"The backup {home / 'Backup'} is inside the source {home}"
    )


def test_r3_2_source_inside_the_backup_is_refused(tmp_path, run_cli):
    backup, _, _ = setup_folders(tmp_path)
    inner = make_tree(backup, {"Photos/a.jpg": "a"}) / "Photos"

    result = run_cli("init", backup, inner)

    assert refused_and_nothing_written(result, backup, "is inside the backup")


def test_r3_2_source_that_is_the_backup_is_refused(tmp_path, run_cli):
    backup, _, _ = setup_folders(tmp_path)

    result = run_cli("init", backup, backup)

    assert refused_and_nothing_written(result, backup, "is both the backup and a source")


def test_r3_2_relative_backup_inside_a_source_is_refused(tmp_path, run_cli, monkeypatch):
    home = make_tree(tmp_path, {"me/Backup": None}) / "me"
    monkeypatch.chdir(home)

    result = run_cli("init", "Backup", home)

    assert refused_and_nothing_written(result, home / "Backup", "is inside the source")


def test_r3_2_junction_cannot_hide_a_backup_inside_a_source(tmp_path, run_cli, junction):
    home = make_tree(tmp_path, {"me/Backup": None}) / "me"
    link = junction(tmp_path / "Bk", home / "Backup")

    result = run_cli("init", link, home)

    assert refused_and_nothing_written(result, home / "Backup", "is inside the source")


def test_r3_3_the_same_source_twice_is_refused(tmp_path, run_cli):
    backup, photos, _ = setup_folders(tmp_path)

    result = run_cli("init", backup, photos, f"Again={photos}")

    assert refused_and_nothing_written(result, backup, "is given twice")


def test_r3_3_source_inside_another_source_is_refused(tmp_path, run_cli):
    backup, photos, _ = setup_folders(tmp_path)
    src = photos.parent

    result = run_cli("init", backup, src, photos)

    assert refused_and_nothing_written(
        result, backup, f"{photos.resolve()} is inside another source, {src.resolve()}"
    )


def test_r3_4_two_sources_with_the_same_name_are_refused(tmp_path, run_cli):
    make_tree(tmp_path, {"a/Documents/x": "x", "b/Documents/y": "y", "backup": None})
    backup = tmp_path / "backup"

    result = run_cli("init", backup, tmp_path / "a" / "Documents", tmp_path / "b" / "Documents")

    assert refused_and_nothing_written(
        result, backup, 'same subfolder name "Documents"', "=", str(tmp_path / "b" / "Documents")
    )
    code, _, _ = run_cli(
        "init", backup, tmp_path / "a" / "Documents", f"WorkDocs={tmp_path / 'b' / 'Documents'}"
    )
    assert code == 0


def test_r3_4_drive_root_without_a_name_is_refused():
    with pytest.raises(Refusal, match=r"NAME=SOURCE"):
        subfolder_name(None, Path("F:\\"))
    assert subfolder_name("Stick", Path("F:\\")) == "Stick"
    assert subfolder_name(None, Path("C:\\Photos")) == "Photos"


@pytest.mark.parametrize(
    "name",
    ["", ".", "..", ".syncplan", ".SyncPlan", "CON", "con.txt", "NUL ", "a.", "a ", "a\tb",
     "a*b", "a?b", 'a"b', "a<b", "a>b", "a|b", "x" * 256, "\U0001f600" * 128],
)  # fmt: skip
def test_r3_5_invalid_subfolder_names_are_refused(tmp_path, run_cli, name):
    backup, photos, _ = setup_folders(tmp_path)

    with pytest.raises(Refusal):
        check_name(name)
    result = run_cli("init", backup, f"{name}={photos}")
    assert refused_and_nothing_written(result, backup)


@pytest.mark.parametrize("name", ["Photos", "WorkDocs", "a b", "\u5199\u771f", "x" * 255, "a.b"])
def test_r3_5_ordinary_names_are_accepted(name):
    check_name(name)


def test_r3_5_source_folder_named_syncplan_is_refused(tmp_path, run_cli):
    backup, _, _ = setup_folders(tmp_path)
    odd = make_tree(tmp_path, {"old/.syncplan/a.txt": "a"}) / "old" / ".syncplan"

    result = run_cli("init", backup, odd)

    assert refused_and_nothing_written(result, backup, "reserved for syncplan")


@settings(max_examples=150, deadline=None)
@given(st.text(max_size=64))
def test_r3_5_every_accepted_name_can_be_created_exactly(name):
    try:
        check_name(name)
    except Refusal:
        assert (
            name in ("", ".", "..")
            or not set(name).isdisjoint('\\/:*?"<>|')
            or (name.casefold() == ".syncplan" or os.path.isreserved(name))
        )
        return
    with tempfile.TemporaryDirectory() as folder:
        (Path(folder) / name).mkdir()
        assert [p.name for p in Path(folder).iterdir()] == [name]


def test_r3_6_a_backup_given_as_a_source_is_refused_with_its_real_sources(tmp_path, run_cli):
    backup, photos, docs = setup_folders(tmp_path)
    run_cli("init", backup, photos, docs)
    second = make_tree(tmp_path, {"backup2": None}) / "backup2"

    result = run_cli("init", second, backup)

    assert refused_and_nothing_written(
        result,
        second,
        f"{backup.resolve()} is a syncplan backup",
        f"syncplan init {second.resolve()} {photos.resolve()} {docs.resolve()}",
    )


def test_r3_6_a_folder_inside_a_backup_is_refused(tmp_path, run_cli):
    backup, photos, _ = setup_folders(tmp_path)
    run_cli("init", backup, photos)
    mirror = make_tree(backup, {"Photos/a.jpg": "a"}) / "Photos"
    second = make_tree(tmp_path, {"backup2": None}) / "backup2"

    result = run_cli("init", second, mirror)

    assert refused_and_nothing_written(result, second, "is inside the syncplan backup")


@pytest.mark.parametrize(
    ("content", "reason"), [("damaged", "can't be read"), ('{"syncplan_marker": 99}', "upgrade")]
)
def test_r3_6_a_backup_with_an_unreadable_or_newer_marker_is_still_refused(
    tmp_path, run_cli, content, reason
):
    backup, _, _ = setup_folders(tmp_path)
    other = make_tree(tmp_path, {"other/.syncplan/marker.json": content}) / "other"

    result = run_cli("init", backup, other)

    assert refused_and_nothing_written(result, backup, "is a syncplan backup", reason)


def test_r3_7_a_file_where_the_subfolder_would_be_is_refused(tmp_path, run_cli):
    backup, photos, _ = setup_folders(tmp_path)
    make_tree(backup, {"Photos": "not a folder"})

    result = run_cli("init", backup, photos)

    assert refused_and_nothing_written(result, backup, "not a folder", f"={photos.resolve()}")


def test_r3_7_a_junction_where_the_subfolder_would_be_is_refused(tmp_path, run_cli, junction):
    backup, photos, _ = setup_folders(tmp_path)
    junction(backup / "Photos", photos)

    result = run_cli("init", backup, photos)

    assert refused_and_nothing_written(result, backup, "is a link")


def test_nfr1_overlap_is_found_whatever_the_letter_case(tmp_path, run_cli):
    home = make_tree(tmp_path, {"me/Backup": None}) / "me"

    result = run_cli("init", home / "Backup", str(home).upper())

    assert refused_and_nothing_written(result, home / "Backup", "is inside the source")


def test_nfr1_the_same_source_in_another_case_is_a_duplicate(tmp_path, run_cli):
    backup, photos, _ = setup_folders(tmp_path)

    result = run_cli("init", backup, photos, f"Again={str(photos).lower()}")

    assert refused_and_nothing_written(result, backup, "is given twice")


def test_nfr1_names_differing_only_in_case_clash(tmp_path, run_cli):
    backup, photos, docs = setup_folders(tmp_path)

    result = run_cli("init", backup, photos, f"photos={docs}")

    assert refused_and_nothing_written(result, backup, "same subfolder name")


# Reading arguments (D2, D8)


def test_d2_name_equals_source_is_split():
    assert read_argument("WorkDocs=D:\\Documents") == ("WorkDocs", Path("D:\\Documents"))


def test_d2_equals_sign_inside_a_path_stays_part_of_the_path():
    assert read_argument("D:\\a=b") == (None, Path("D:\\a=b"))


def test_d2_relative_folder_with_an_equals_sign_is_written_with_a_dot():
    assert read_argument(".\\a=b") == (None, Path(".\\a=b"))


def test_d2_plain_path_has_no_name():
    assert read_argument("C:\\Photos") == (None, Path("C:\\Photos"))


def test_d8_bare_drive_means_its_top_folder():
    assert read_argument("E:") == (None, Path("E:\\"))
    assert read_argument("Stick=E:") == ("Stick", Path("E:\\"))
