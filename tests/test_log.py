"""R5: the debug log, the screen through logging, one run at a time; R3.9: syncplan's own folder."""

import logging
import os
import re
import stat
import subprocess
import sys
from pathlib import Path

import pytest
from conftest import make_tree, setup_folders

from syncplan import init, log, marker, space
from syncplan.marker import Source

PREFIX = re.compile(
    r"^\d{4}-\d\d-\d\d \d\d:\d\d:\d\d\.\d{3}[+-]\d\d:\d\d [A-Z]+ +\[run ([0-9a-f]{4})\] "
)
HOLD_LOCK = """
import msvcrt, os, sys, time
fd = os.open(sys.argv[1], os.O_RDONLY | os.O_CREAT)
msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
print("held", flush=True)
time.sleep(60)
"""
BUG_WHILE_LOG_IS_OFF = """
import sys
from syncplan import cli, init

def bug(*args):
    raise ValueError("boom")

init.check_init = bug
sys.exit(cli.main(sys.argv[1:]))
"""


def log_file(program_data: Path) -> Path:
    return program_data / "syncplan" / "logs" / "syncplan.log"


def old(program_data: Path, n: int) -> Path:
    return log_file(program_data).with_name(f"syncplan.log.{n}")


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


def screen_is_in_the_log(out: str, err: str, program_data: Path) -> bool:
    """R5.5: everything shown on screen went through logging, so the log has it too."""
    logged = {line for lines in runs(program_data).values() for line in lines}
    return all(line in logged for line in (out + err).splitlines())


def no_handlers_left() -> bool:
    handlers = logging.getLogger("syncplan").handlers
    return all(isinstance(handler, logging.NullHandler) for handler in handlers)


def seed_logs(program_data: Path, current: bytes, olds: int = log.OLD_LOGS) -> None:
    """A current log plus old logs .1 to .olds holding b"old1", b"old2", ..."""
    logs = {f"syncplan.log.{n}": f"old{n}".encode() for n in range(1, olds + 1)}
    make_tree(program_data / "syncplan" / "logs", {"syncplan.log": current} | logs)


def old_logs(program_data: Path) -> list[bytes | None]:
    paths = (old(program_data, n) for n in range(1, log.OLD_LOGS + 1))
    return [path.read_bytes() if path.exists() else None for path in paths]


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


@pytest.fixture
def short_drive(monkeypatch):
    """A backup drive with almost no free space, so R2.6 warns."""
    monkeypatch.setattr(init, "disk_usage", lambda path: (100 * 2**20, 100 * 2**20 - 10, 10))


# R5.1 Every run is recorded


def test_r5_1_a_successful_run_is_logged_step_by_step(tmp_path, run_cli, program_data):
    backup, photos, docs = setup_folders(tmp_path)

    code, out, err = run_cli("init", backup, photos, docs)

    assert (code, err) == (0, "")
    lines = only_run(program_data)
    text = "\n".join(lines)
    assert lines[0].startswith("syncplan ")
    assert ", Python " in lines[0]
    assert ", Windows " in lines[0]
    assert lines[1] == f"command: init {backup} {photos} {docs}"
    assert f"backup {backup.resolve()}: ok" in text
    assert f"source {photos.resolve()}: ok (subfolder Photos)" in text
    assert re.search(rf"block size of {re.escape(str(backup.resolve()))}: \d+ bytes", text)
    assert re.search(r"drive .*: capacity \S+ \S+, free \S+ \S+", text)
    assert re.search(
        rf"measured {re.escape(str(photos.resolve()))}: \S+ \S+ in 1 file, 0 skipped, \d+\.\d\d s",
        text,
    )
    assert f"marker written: {backup.resolve() / '.syncplan' / 'marker.json'}" in text
    assert re.fullmatch(r"exit 0 after \d+\.\d\d s", lines[-1])
    assert screen_is_in_the_log(out, err, program_data)
    assert no_handlers_left()


def test_r5_1_a_refused_run_logs_the_reason_and_exit_1(tmp_path, run_cli, program_data):
    backup, _, _ = setup_folders(tmp_path)
    camera = tmp_path.resolve() / "Camera"

    code, out, err = run_cli("init", backup, camera)

    assert code == 1
    assert (out, err) == ("", f"{camera} does not exist.\n")  # stderr only, no Details
    lines = only_run(program_data)
    assert f"{camera} does not exist." in lines
    assert lines[-1].startswith("exit 1 after")


def test_r5_1_a_usage_error_logs_exit_2_and_leaves_no_handlers(run_cli, program_data):
    code, out, err = run_cli("init")

    assert code == 2
    assert "usage: syncplan init" in err
    lines = only_run(program_data)
    assert lines[1] == "command: init"
    assert lines[-1].startswith("exit 2 after")
    assert screen_is_in_the_log(out, err, program_data)  # argparse goes through logging too
    assert no_handlers_left()


def test_r5_1_the_command_keeps_its_argument_boundaries(tmp_path, run_cli, program_data):
    _, photos, _ = setup_folders(tmp_path)
    spaced = make_tree(tmp_path, {"my backups": None}) / "my backups"

    run_cli("init", spaced, photos)

    assert only_run(program_data)[1] == f'command: init "{spaced}" {photos}'


def test_r5_1_control_characters_in_the_command_cannot_fake_a_line(tmp_path, run_cli, program_data):
    backup, photos, _ = setup_folders(tmp_path)
    fake = "2026-10-11 00:00:00.000+00:00 INFO  [run 0000] marker written: C:\\fake"

    run_cli("init", backup, f"Pics\n{fake}={photos}")

    lines = only_run(program_data)  # one run, every line with a real prefix
    assert "\\x0a" in lines[1]


def test_r5_1_warnings_are_logged_and_stay_off_stdout(tmp_path, run_cli, program_data, short_drive):
    backup, photos, _ = setup_folders(tmp_path)

    code, out, err = run_cli("init", backup, photos)

    assert code == 0
    assert "Warning: about" in err
    assert "Warning" not in out
    assert screen_is_in_the_log(out, err, program_data)


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

    code, out, err = run_cli("init", backup, photos)

    assert code == 1
    assert err.rstrip("\n").endswith(f"Details: {log_file(program_data)}")
    assert "Traceback" not in err  # on screen only while the log can't be written (R5.4)
    text = "\n".join(only_run(program_data))
    assert "Traceback (most recent call last):" in text
    assert screen_is_in_the_log(out, err, program_data)
    if problem == "full disk":  # the R1.6 row: what cleanup did is in the log
        assert re.search(r"cleanup removed: marker-\S+\.tmp, \.syncplan", text)


def test_r5_2_a_backup_given_as_a_source_whose_marker_windows_cannot_read_gets_details(
    tmp_path, run_cli, program_data
):
    backup, _, _ = setup_folders(tmp_path)
    other = make_tree(tmp_path, {"other/.syncplan/marker.json/inside": None}) / "other"

    code, _, err = run_cli("init", backup, other)

    assert code == 1
    assert "is a syncplan backup" in err
    assert err.rstrip("\n").endswith(f"Details: {log_file(program_data)}")


@pytest.mark.parametrize("refusal", ["missing source", "damaged marker", "race"])
def test_r5_2_plain_refusals_have_no_details(tmp_path, run_cli, monkeypatch, refusal):
    backup, photos, docs = setup_folders(tmp_path)
    target = backup.resolve()
    if refusal == "missing source":
        photos = tmp_path.resolve() / "Camera"
        expected = f"{photos} does not exist.\n"
    elif refusal == "damaged marker":
        make_tree(backup, {".syncplan/marker.json": "damaged"})
        expected = f"{target / '.syncplan' / 'marker.json'} can't be read; inspect it.\n"
    else:
        real_fsync, raced = marker.os.fsync, []

        def other_init_wins(fd):
            real_fsync(fd)
            if not raced:
                raced.append(True)
                marker.write_marker(target, [Source("Docs", docs.resolve())])

        monkeypatch.setattr(marker.os, "fsync", other_init_wins)
        expected = f"{target} became a syncplan backup while init was running; nothing changed.\n"

    code, _, err = run_cli("init", backup, photos)

    assert (code, err) == (1, expected)


# R5.3 The log is kept to the current file plus 5 old ones


@pytest.mark.parametrize(("size", "rotated"), [(log.LOG_LIMIT - 1, False), (log.LOG_LIMIT, True)])
def test_r5_3_the_log_rotates_at_1_mib(tmp_path, run_cli, program_data, size, rotated):
    seed_logs(program_data, b"c" * size)

    run_cli("init", setup_folders(tmp_path)[0])

    if rotated:
        assert old_logs(program_data) == [b"c" * size, b"old1", b"old2", b"old3", b"old4"]
        assert log_file(program_data).stat().st_size < 10_000  # a fresh log for this run
    else:
        assert old_logs(program_data) == [b"old1", b"old2", b"old3", b"old4", b"old5"]
    assert not old(program_data, 6).exists()


@pytest.mark.parametrize("olds", [0, 2])
def test_r5_3_early_rotations_keep_every_old_log(tmp_path, run_cli, program_data, olds):
    seed_logs(program_data, b"c" * log.LOG_LIMIT, olds)

    run_cli("init", setup_folders(tmp_path)[0])

    kept = [b"c" * log.LOG_LIMIT] + [f"old{n}".encode() for n in range(1, olds + 1)]
    assert old_logs(program_data) == kept + [None] * (log.OLD_LOGS - len(kept))
    assert not log_file(program_data).with_name("syncplan.log.rotating").exists()


def test_r5_3_a_log_that_cannot_be_moved_leaves_the_old_logs_alone(tmp_path, run_cli, program_data):
    seed_logs(program_data, b"c" * log.LOG_LIMIT)

    with log_file(program_data).open("rb"):  # held open, as by an editor: Windows won't rename it
        code, _, _ = run_cli("init", setup_folders(tmp_path)[0])

    assert code == 0
    assert old_logs(program_data) == [b"old1", b"old2", b"old3", b"old4", b"old5"]
    assert not log_file(program_data).with_name("syncplan.log.rotating").exists()


def test_r5_3_an_interrupted_rotation_is_finished_first(tmp_path, run_cli, program_data):
    seed_logs(program_data, b"")
    log_file(program_data).with_name("syncplan.log.rotating").write_bytes(b"interrupted")

    run_cli("init", setup_folders(tmp_path)[0])

    assert old_logs(program_data) == [b"interrupted", b"old1", b"old2", b"old3", b"old4"]
    assert not log_file(program_data).with_name("syncplan.log.rotating").exists()


def test_r5_3_a_held_old_log_stops_the_rotation_without_losing_files(
    tmp_path, run_cli, program_data
):
    seed_logs(program_data, b"c" * log.LOG_LIMIT)
    rotating = log_file(program_data).with_name("syncplan.log.rotating")

    with old(program_data, 1).open("rb"):  # .1 can't move, so .rotating can't become .1
        assert run_cli("init", setup_folders(tmp_path)[0])[0] == 0
    kept = {p.read_bytes() for p in [rotating, *(old(program_data, n) for n in (1, 3, 4, 5))]}
    assert kept == {b"c" * log.LOG_LIMIT, b"old1", b"old2", b"old3", b"old4"}  # old5 was due

    assert run_cli("init", tmp_path / "src" / "Docs")[0] == 0  # the next run finishes it
    assert old_logs(program_data) == [b"c" * log.LOG_LIMIT, b"old1", b"old2", b"old3", b"old4"]
    assert not rotating.exists()


# R5.4 When the log can't be written, the command still runs


@pytest.mark.parametrize(
    "problem", ["folder is a file", "lock can't be opened", "log is read-only", "lock and log"]
)
def test_r5_4_an_unwritable_log_gives_one_note_and_init_still_works(
    tmp_path, run_cli, program_data, monkeypatch, problem
):
    own = program_data / "syncplan"
    read_only = []
    if problem == "folder is a file":
        blocker = make_tree(tmp_path, {"not-a-folder": "x"}) / "not-a-folder"
        monkeypatch.setenv("PROGRAMDATA", str(blocker))
    if problem in ("lock can't be opened", "lock and log"):
        (own / "syncplan.lock").mkdir(parents=True)  # a folder where the lock file should be
    if problem in ("log is read-only", "lock and log"):
        logged = make_tree(own, {"logs/syncplan.log": "earlier run"}) / "logs" / "syncplan.log"
        logged.chmod(stat.S_IREAD)
        read_only.append(logged)
    backup, photos, _ = setup_folders(tmp_path)

    try:
        code, out, err = run_cli("init", backup, photos)
    finally:
        for path in read_only:
            path.chmod(stat.S_IWRITE)

    assert code == 0
    assert "Initialized syncplan backup" in out
    assert "Note" not in out
    assert len(err.splitlines()) == 1
    assert err.startswith("Note: couldn't")


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
    blocker = make_tree(tmp_path, {"not-a-folder": "x"}) / "not-a-folder"
    env = {**os.environ, "PROGRAMDATA": str(blocker)}

    result = subprocess.run(
        [sys.executable, "-c", BUG_WHILE_LOG_IS_OFF, "init", str(tmp_path)],
        capture_output=True,
        env=env,
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
    seed_logs(program_data, b"c" * log.LOG_LIMIT)  # a full log: a wrong order would rotate it
    backup, photos, _ = setup_folders(tmp_path)

    code, out, err = run_cli("init", backup, photos)

    assert code == 1
    assert (out, err) == (
        "",
        "Another syncplan is already running on this PC; wait for it to finish, then try again.\n",
    )
    assert log_file(program_data).read_bytes() == b"c" * log.LOG_LIMIT
    assert old_logs(program_data) == [b"old1", b"old2", b"old3", b"old4", b"old5"]
    assert not log_file(program_data).with_name("syncplan.log.rotating").exists()
    assert not (backup / ".syncplan").exists()
    assert no_handlers_left()


# R5.7 A link on the way to syncplan's folder refuses the run


@pytest.mark.parametrize(
    "link", ["its own folder", "its logs folder", "a folder above it", "the log file", "the lock"]
)
def test_r5_7_a_link_that_could_put_the_log_in_a_source_refuses_the_run(
    tmp_path, run_cli, junction, monkeypatch, link
):
    tree = {"Data/notes.txt": "mine", "Data/pd": None, "backup": None}
    source = make_tree(tmp_path, tree) / "Data"
    program_data = tmp_path / "programdata"
    own = program_data / "syncplan"
    if link == "a folder above it":
        program_data = junction(tmp_path / "pdlink", source / "pd")
    elif link == "its own folder":
        program_data.mkdir()
        junction(own, source / "pd")
    elif link == "its logs folder":
        own.mkdir(parents=True)
        junction(own / "logs", source / "pd")
    else:
        (own / "logs").mkdir(parents=True)
        named = own / "logs" / "syncplan.log" if link == "the log file" else own / "syncplan.lock"
        os.link(source / "notes.txt", named)  # a second name for a file inside the source
    monkeypatch.setenv("PROGRAMDATA", str(program_data))
    before = sorted(p.name for p in source.rglob("*"))

    code, out, err = run_cli("init", tmp_path / "backup", source)

    assert code == 1
    assert out == ""
    assert " is a link" in err or " through a link" in err
    assert sorted(p.name for p in source.rglob("*")) == before
    assert (source / "notes.txt").read_text() == "mine"


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
    expected = {
        "its own folder": f"{own} is syncplan's own folder; it can't be backed up.",
        "a folder holding it": f"{program_data} holds syncplan's own folder ({own});"
        " back up its folders instead, for example C:\\Users\\you.",
        "a folder inside it": f"{own / 'logs'} is inside syncplan's own folder ({own});"
        " it can't be backed up.",
    }[which]
    backup = make_tree(tmp_path, {"backup": None}) / "backup"

    code, _, err = run_cli("init", backup, f"X={source}")

    assert (code, err) == (1, expected + "\n")
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
