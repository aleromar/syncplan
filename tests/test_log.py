"""R5: the debug log, the screen through logging, one run at a time; R3.9: syncplan's own folder."""

import logging
import os
import re
import stat
import subprocess
import sys
from pathlib import Path

import pytest
from conftest import make_tree

from syncplan import init, log, space

PREFIX = re.compile(
    r"^\d{4}-\d\d-\d\d \d\d:\d\d:\d\d\.\d{3}[+-]\d\d:\d\d [A-Z]+ +\[run ([0-9a-f]{4})\] "
)
MIB = 1_048_576
HOLD_LOCK = """
import msvcrt, os, sys, time
fd = os.open(sys.argv[1], os.O_RDONLY | os.O_CREAT)
msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
print("held", flush=True)
time.sleep(60)
"""


def log_file(program_data: Path) -> Path:
    return program_data / "syncplan" / "logs" / "syncplan.log"


def runs(program_data: Path) -> dict[str, list[str]]:
    """The log's lines grouped by run ID, prefix removed. Every line must carry the prefix."""
    grouped: dict[str, list[str]] = {}
    for line in log_file(program_data).read_text(encoding="utf-8").splitlines():
        match = PREFIX.match(line)
        assert match, f"log line without the time, level and run ID: {line!r}"
        grouped.setdefault(match[1], []).append(line[match.end() :])
    return grouped


def only_run(program_data: Path) -> list[str]:
    grouped = runs(program_data)
    assert len(grouped) == 1, grouped
    return next(iter(grouped.values()))


def setup_folders(tmp_path: Path) -> tuple[Path, Path, Path]:
    make_tree(tmp_path, {"src/Photos/a.jpg": "a", "src/Docs/b.txt": "b", "backup": None})
    return tmp_path / "backup", tmp_path / "src" / "Photos", tmp_path / "src" / "Docs"


def no_handlers_left() -> bool:
    handlers = logging.getLogger("syncplan").handlers
    return all(isinstance(handler, logging.NullHandler) for handler in handlers)


def fill(path: Path, content: bytes) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    return path


@pytest.fixture
def held_lock(program_data):
    """Another syncplan run, simulated by a process holding the run lock."""
    lock = program_data / "syncplan" / "syncplan.lock"
    lock.parent.mkdir(parents=True, exist_ok=True)
    holder = subprocess.Popen(
        [sys.executable, "-c", HOLD_LOCK, str(lock)], stdout=subprocess.PIPE, text=True
    )
    assert holder.stdout.readline().strip() == "held"
    yield lock
    holder.kill()
    holder.wait()
    lock.chmod(stat.S_IWRITE)


# R5.1 Every run is recorded


def test_r5_1_a_successful_run_is_logged_step_by_step(tmp_path, run_cli, program_data):
    backup, photos, docs = setup_folders(tmp_path)

    code, _, err = run_cli("init", backup, photos, docs)

    assert (code, err) == (0, "")
    lines = only_run(program_data)
    text = "\n".join(lines)
    assert lines[0].startswith("syncplan ")
    assert ", Python " in lines[0]
    assert ", Windows " in lines[0]
    assert lines[1] == f"command: init {backup} {photos} {docs}"
    assert f"backup {backup.resolve()}: ok" in text
    assert f"source {photos.resolve()}: ok (subfolder Photos)" in text
    assert re.search(
        rf"measured {re.escape(str(photos.resolve()))}: \S+ \S+ in 1 file, 0 skipped, \d+\.\d\d s",
        text,
    )
    assert f"marker written: {backup.resolve() / '.syncplan' / 'marker.json'}" in text
    assert "Initialized syncplan backup at" in text
    assert re.fullmatch(r"exit 0 after \d+\.\d\d s", lines[-1])
    assert no_handlers_left()


def test_r5_1_a_refused_run_logs_the_reason_and_exit_1(tmp_path, run_cli, program_data):
    backup, _, _ = setup_folders(tmp_path)
    camera = tmp_path.resolve() / "Camera"

    code, _, err = run_cli("init", backup, camera)

    assert code == 1
    assert err == f"{camera} does not exist.\n"  # a plain refusal: no Details line (R5.2, R5.5)
    lines = only_run(program_data)
    assert f"{camera} does not exist." in lines
    assert lines[-1].startswith("exit 1 after")


def test_r5_1_a_usage_error_logs_exit_2_and_leaves_no_handlers(run_cli, program_data):
    code, _, err = run_cli("init")

    assert code == 2
    assert "usage: syncplan init" in err
    lines = only_run(program_data)
    assert lines[1] == "command: init"
    assert lines[-1].startswith("exit 2 after")
    assert no_handlers_left()


def test_r5_1_control_characters_in_the_command_cannot_fake_a_line(tmp_path, run_cli, program_data):
    backup, photos, _ = setup_folders(tmp_path)
    fake = "2026-10-11 00:00:00.000+00:00 INFO  [run 0000] marker written: C:\\fake"

    run_cli("init", backup, f"Pics\n{fake}={photos}")

    lines = only_run(program_data)  # one run, every line with a real prefix
    assert "\\x0a" in lines[1]


def test_r5_1_ctrl_c_is_logged_and_everything_is_released(
    tmp_path, run_cli, program_data, monkeypatch
):
    backup, photos, _ = setup_folders(tmp_path)
    real_folder_size = space.folder_size

    def interrupted(folder, block):
        raise KeyboardInterrupt

    monkeypatch.setattr(space, "folder_size", interrupted)

    with pytest.raises(KeyboardInterrupt):
        run_cli("init", backup, photos)

    assert only_run(program_data)[-1].startswith("interrupted after")
    assert no_handlers_left()
    assert not (backup / ".syncplan").exists()
    monkeypatch.setattr(space, "folder_size", real_folder_size)
    assert run_cli("init", backup, photos)[0] == 0  # the run lock was released


# R5.2 Errors end with Details, and the traceback is in the log


@pytest.mark.parametrize(
    "problem", ["full disk", "drive not ready", "marker above can't be checked", "bug"]
)
def test_r5_2_errors_end_with_details_and_log_the_traceback(
    tmp_path, run_cli, program_data, monkeypatch, request, problem
):
    backup, photos, _ = setup_folders(tmp_path)
    if problem == "full disk":
        request.getfixturevalue("full_disk")
    elif problem == "drive not ready":
        root = photos.resolve()
        not_ready = OSError(21, "The device is not ready", str(root))
        request.getfixturevalue("failing_scandir")(lambda path: path == root, not_ready)
    elif problem == "marker above can't be checked":
        real_stat = Path.stat

        def denied(self, *args, **kwargs):
            if self.name == "marker.json" and self.parent.parent == tmp_path.resolve():
                raise PermissionError(13, "Access is denied", str(self))
            return real_stat(self, *args, **kwargs)

        monkeypatch.setattr(Path, "stat", denied)
    else:

        def bug(*args):
            raise ValueError("boom")

        monkeypatch.setattr(init, "check_init", bug)

    code, _, err = run_cli("init", backup, photos)

    assert code == 1
    assert err.rstrip("\n").endswith(f"Details: {log_file(program_data)}")
    assert "Traceback" not in err  # on screen only while the log can't be written (R5.4)
    assert "Traceback (most recent call last):" in "\n".join(only_run(program_data))


# R5.3 The log is kept to the current file plus 5 old ones


def old(program_data: Path, n: int) -> Path:
    return log_file(program_data).with_name(f"syncplan.log.{n}")


@pytest.mark.parametrize(("size", "rotated"), [(MIB - 1, False), (MIB, True)])
def test_r5_3_the_log_rotates_at_1_mib(tmp_path, run_cli, program_data, size, rotated):
    fill(log_file(program_data), b"c" * size)
    for n in range(1, 6):
        fill(old(program_data, n), f"old{n}".encode())

    run_cli("init", setup_folders(tmp_path)[0])

    contents = [old(program_data, n).read_bytes() for n in range(1, 6)]
    if rotated:
        assert contents == [b"c" * size, b"old1", b"old2", b"old3", b"old4"]  # old5 dropped
        assert log_file(program_data).stat().st_size < 10_000  # a fresh log for this run
    else:
        assert contents == [b"old1", b"old2", b"old3", b"old4", b"old5"]
    assert not old(program_data, 6).exists()


def test_r5_3_a_log_that_cannot_be_moved_leaves_the_old_logs_alone(tmp_path, run_cli, program_data):
    current = fill(log_file(program_data), b"c" * MIB)
    for n in range(1, 6):
        fill(old(program_data, n), f"old{n}".encode())

    with current.open("rb"):  # held open, as by an editor: Windows won't rename it
        code, _, _ = run_cli("init", setup_folders(tmp_path)[0])

    assert code == 0
    assert [old(program_data, n).read_bytes() for n in range(1, 6)] == [
        f"old{n}".encode() for n in range(1, 6)
    ]
    assert not current.with_name("syncplan.log.rotating").exists()


def test_r5_3_an_interrupted_rotation_is_finished_first(tmp_path, run_cli, program_data):
    fill(log_file(program_data).with_name("syncplan.log.rotating"), b"interrupted")
    for n in range(1, 6):
        fill(old(program_data, n), f"old{n}".encode())

    run_cli("init", setup_folders(tmp_path)[0])

    contents = [old(program_data, n).read_bytes() for n in range(1, 6)]
    assert contents == [b"interrupted", b"old1", b"old2", b"old3", b"old4"]
    assert not log_file(program_data).with_name("syncplan.log.rotating").exists()


def test_r5_3_a_held_old_log_stops_the_rotation_without_losing_files(
    tmp_path, run_cli, program_data
):
    fill(log_file(program_data), b"c" * MIB)
    for n in range(1, 6):
        fill(old(program_data, n), f"old{n}".encode())
    rotating = log_file(program_data).with_name("syncplan.log.rotating")

    with old(program_data, 1).open("rb"):  # .1 can't move, so .rotating can't become .1
        assert run_cli("init", setup_folders(tmp_path)[0])[0] == 0
    kept = {p.read_bytes() for p in [rotating, *(old(program_data, n) for n in (1, 3, 4, 5))]}
    assert kept == {b"c" * MIB, b"old1", b"old2", b"old3", b"old4"}  # only old5, due anyway

    assert run_cli("init", tmp_path / "src" / "Docs")[0] == 0  # the next run finishes it
    contents = [old(program_data, n).read_bytes() for n in range(1, 6)]
    assert contents == [b"c" * MIB, b"old1", b"old2", b"old3", b"old4"]
    assert not rotating.exists()


# R5.4 When the log can't be written, the command still runs


def test_r5_4_an_unwritable_log_gives_one_note_and_init_still_works(tmp_path, run_cli, monkeypatch):
    blocker = fill(tmp_path / "not-a-folder", b"x")
    monkeypatch.setenv("PROGRAMDATA", str(blocker))
    backup, photos, _ = setup_folders(tmp_path)

    code, out, err = run_cli("init", backup, photos)

    assert code == 0
    assert "Initialized syncplan backup" in out
    assert len(err.splitlines()) == 1
    assert err.startswith("Note: couldn't write")


def test_r5_4_a_log_write_failing_mid_run_gives_one_note(tmp_path, run_cli, monkeypatch):
    backup, photos, _ = setup_folders(tmp_path)

    def disk_full(self):
        raise OSError(28, "There is not enough space on the disk")

    monkeypatch.setattr(log._FileHandler, "flush", disk_full)

    code, out, err = run_cli("init", backup, photos)

    assert code == 0
    assert "Initialized syncplan backup" in out
    assert len(err.splitlines()) == 1
    assert "not enough space" in err


def test_r5_4_an_error_record_that_cannot_be_written_shows_its_traceback(
    tmp_path, run_cli, monkeypatch
):
    backup, photos, _ = setup_folders(tmp_path)
    real_format = log._FileFormatter.format

    def full_on_errors(self, record):
        if record.levelno >= logging.ERROR:
            raise OSError(28, "There is not enough space on the disk")
        return real_format(self, record)

    def bug(*args):
        raise ValueError("boom")

    monkeypatch.setattr(log._FileFormatter, "format", full_on_errors)
    monkeypatch.setattr(init, "check_init", bug)

    code, _, err = run_cli("init", backup, photos)

    assert code == 1
    assert err.startswith("Note: couldn't write the log")
    assert "syncplan hit an unexpected problem (ValueError: boom)." in err
    assert err.count("Traceback (most recent call last):") == 1
    assert "Details:" not in err


def test_r5_4_an_error_while_the_log_is_off_shows_its_traceback_once(tmp_path):
    # A real process: inside pytest, Python's last-resort output is hidden by pytest's own logging.
    blocker = fill(tmp_path / "not-a-folder", b"x")
    code = (
        "import sys\nfrom syncplan import cli, init\n"
        "def bug(*args):\n    raise ValueError('boom')\n"
        "init.check_init = bug\nsys.exit(cli.main(sys.argv[1:]))"
    )
    env = {**os.environ, "PROGRAMDATA": str(blocker)}

    result = subprocess.run(
        [sys.executable, "-c", code, "init", str(tmp_path)], capture_output=True, env=env
    )

    err = result.stderr.decode("utf-8")
    assert result.returncode == 1
    assert err.startswith("Note: couldn't write")
    assert "syncplan hit an unexpected problem (ValueError: boom)." in err
    assert err.count("Traceback (most recent call last):") == 1
    assert "Details:" not in err


# R5.5 The screen shows the same report, through logging


def test_r5_5_the_report_reaches_the_screen_unchanged(tmp_path, run_cli):
    backup, photos, _ = setup_folders(tmp_path)

    code, out, err = run_cli("init", backup, photos)

    assert (code, err) == (0, "")
    lines = out.splitlines()
    assert lines[:2] == [
        "Measuring sources...",
        f"Initialized syncplan backup at {backup.resolve()}",
    ]
    assert not any(PREFIX.match(line) for line in lines)  # no log prefixes on screen


# R5.6 One run at a time


@pytest.mark.parametrize("read_only", [False, True])
def test_r5_6_a_second_run_is_refused_while_one_is_running(
    tmp_path, run_cli, program_data, held_lock, read_only
):
    if read_only:  # as for another Windows account, which can't write the first one's file
        held_lock.chmod(stat.S_IREAD)
    backup, photos, _ = setup_folders(tmp_path)

    code, _, err = run_cli("init", backup, photos)

    assert code == 1
    assert err == (
        "Another syncplan is already running on this PC; wait for it to finish, then try again.\n"
    )
    assert not log_file(program_data).exists()
    assert not (backup / ".syncplan").exists()
    assert no_handlers_left()


def test_pd10_a_link_at_syncplans_own_folder_is_refused(tmp_path, run_cli, junction, monkeypatch):
    # A link there could put the log inside a source before any check runs: refused (PD10).
    source = make_tree(tmp_path, {"Data/real-syncplan": None, "backup": None}) / "Data"
    program_data = make_tree(tmp_path, {"pd": None}) / "pd"
    junction(program_data / "syncplan", source / "real-syncplan")
    monkeypatch.setenv("PROGRAMDATA", str(program_data))

    code, _, err = run_cli("init", tmp_path / "backup", source)

    assert code == 1
    assert "is a link, not a real folder" in err
    assert list((source / "real-syncplan").iterdir()) == []


# R3.9 syncplan's own folder is never a source


@pytest.mark.parametrize("which", ["its own folder", "a folder holding it", "a folder inside it"])
def test_r3_9_syncplans_own_folder_is_never_a_source(tmp_path, run_cli, program_data, which):
    own = program_data / "syncplan"
    (own / "logs").mkdir(parents=True)
    source = {
        "its own folder": own,
        "a folder holding it": program_data,
        "a folder inside it": own / "logs",
    }[which]
    backup = make_tree(tmp_path, {"backup": None}) / "backup"

    code, _, err = run_cli("init", backup, f"X={source}")

    assert code == 1
    assert "syncplan's own folder" in err
    assert not (backup / ".syncplan").exists()


# D18 Where syncplan's folder is


@pytest.mark.parametrize("value", [None, "", "data", "\\ProgramData", "C:ProgramData"])
def test_d18_an_unusable_programdata_falls_back_to_the_standard_place(monkeypatch, value):
    if value is None:
        monkeypatch.delenv("PROGRAMDATA")
    else:
        monkeypatch.setenv("PROGRAMDATA", value)

    assert log.home() == Path("C:\\ProgramData\\syncplan")


def test_d18_programdata_is_used_when_it_is_an_absolute_path(program_data):
    assert log.home() == program_data / "syncplan"
