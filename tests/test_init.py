import os
import re
import subprocess
import sys
from pathlib import Path

from conftest import make_tree

from syncplan import marker
from syncplan.init import read_argument
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
