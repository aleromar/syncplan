"""Shared test helpers: synthetic folder trees, a CLI runner, junctions and sparse files."""

import ctypes
import json
import msvcrt
import os
import re
import stat
import subprocess
import sys
from ctypes import wintypes
from datetime import UTC, datetime
from pathlib import Path

import pytest

from syncplan import cli

_kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
_kernel32.GetCompressedFileSizeW.argtypes = [wintypes.LPCWSTR, ctypes.POINTER(wintypes.DWORD)]
_kernel32.GetCompressedFileSizeW.restype = wintypes.DWORD  # unsigned, or big sizes go negative
_kernel32.CreateFileW.argtypes = [
    wintypes.LPCWSTR,
    wintypes.DWORD,
    wintypes.DWORD,
    wintypes.LPVOID,
    wintypes.DWORD,
    wintypes.DWORD,
    wintypes.HANDLE,
]
_kernel32.CreateFileW.restype = wintypes.HANDLE
_FSCTL_SET_SPARSE = 0x000900C4
_INVALID_FILE_SIZE = 0xFFFFFFFF
_INVALID_HANDLE = wintypes.HANDLE(-1).value
_READ_WRITE = 0x80000000 | 0x40000000
_OPEN_EXISTING = 3
_EPOCH = datetime(1970, 1, 1, tzinfo=UTC)

_HOLD_LOCK = """
import msvcrt, os, sys, time
fd = os.open(sys.argv[1], os.O_RDONLY | os.O_CREAT)
msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
print("held", flush=True)
time.sleep(60)
"""


def make_tree(root: Path, entries: dict[str, str | bytes | None]) -> Path:
    """Create files under root from {relative path: content}. A None content makes a folder."""
    for relative, content in entries.items():
        path = root / relative
        if content is None:
            path.mkdir(parents=True, exist_ok=True)
            continue
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content.encode() if isinstance(content, str) else content)
    return root


def setup_folders(tmp_path: Path) -> tuple[Path, Path, Path]:
    """An empty backup folder and two sources. Returns (backup, Photos, Docs)."""
    make_tree(tmp_path, {"src/Photos/a.jpg": "a", "src/Docs/b.txt": "b", "backup": None})
    return tmp_path / "backup", tmp_path / "src" / "Photos", tmp_path / "src" / "Docs"


def tree_snapshot(root: Path) -> dict[str, object]:
    """Every file, folder and link under root: a file's bytes, None for a folder, a link's target.

    Two equal snapshots prove a run created, deleted and changed nothing there. Links are
    recorded, not followed, so a junction can't make the walk loop or leave the tree.
    """
    snapshot: dict[str, object] = {}
    pending = [root]
    while pending:
        folder = pending.pop()
        for path in folder.iterdir():
            key = path.relative_to(root).as_posix()
            if path.is_symlink() or path.is_junction():
                snapshot[key] = ("link", str(path.readlink()))
            elif path.is_dir():
                snapshot[key] = None
                pending.append(path)
            else:
                snapshot[key] = path.read_bytes()
    return snapshot


def file_dates(root: Path) -> dict[str, int]:
    """The modification date, in ns, of every file, folder and link under root.

    With tree_snapshot, this shows that plan changed no date anywhere (plan-copies R1.2).
    Links are recorded, not followed.
    """
    dates: dict[str, int] = {}
    pending = [root]
    while pending:
        folder = pending.pop()
        for path in folder.iterdir():
            dates[path.relative_to(root).as_posix()] = path.lstat().st_mtime_ns
            if path.is_dir() and not (path.is_symlink() or path.is_junction()):
                pending.append(path)
    return dates


def read_plan(backup: Path, name: str) -> dict:
    """The plan file plan wrote for the source NAME, as JSON."""
    return json.loads((backup / ".syncplan" / "plans" / f"{name}.json").read_text("utf-8"))


def plan_date(text: str) -> int:
    """A plan file's date as ns since 1970 UTC, exactly (plan-copies D8).

    datetime.fromisoformat drops the 7th decimal (SP14), so the fraction is read apart.
    """
    whole, fraction = text.removesuffix("Z").split(".")
    since = datetime.fromisoformat(whole).replace(tzinfo=UTC) - _EPOCH
    return (since.days * 86_400 + since.seconds) * 1_000_000_000 + int(fraction) * 100


def extended(path: Path) -> str:
    """The \\\\?\\ form of path, which makes names Windows can't normally use (plan-copies SP9)."""
    return "\\\\?\\" + str(path)


def lists_source(report: str, name: str, path: Path) -> bool:
    """True if report has an aligned `NAME <- SOURCE` line for this source."""
    return bool(
        re.search(rf"^\s+{re.escape(name)}\s+<- {re.escape(str(path))}(\s|$)", report, re.M)
    )


def marker_names(folder: Path) -> list[str]:
    """What is in the folder's .syncplan: the marker, and any temporary file left behind."""
    return sorted(p.name for p in (folder / ".syncplan").iterdir())


def make_backup(run_cli, tmp_path: Path, *names: str) -> tuple[Path, list[Path]]:
    """An initialized backup, tmp_path/backup, of one synthetic source per name under src/.

    Returns (backup, sources). Each source holds one small file.
    """
    make_tree(tmp_path, {"backup": None, **{f"src/{name}/{name}.txt": name for name in names}})
    backup, sources = tmp_path / "backup", [tmp_path / "src" / name for name in names]
    code, _, err = run_cli("init", backup, *sources)
    assert code == 0, err
    return backup, sources


def size_on_disk(path: Path) -> int:
    """Bytes the file really occupies on disk, which for a sparse file is far below its size."""
    high = wintypes.DWORD()
    low = _kernel32.GetCompressedFileSizeW(str(path), ctypes.byref(high))
    if low == _INVALID_FILE_SIZE and ctypes.get_last_error():
        raise ctypes.WinError(ctypes.get_last_error())
    return (high.value << 32) + low


@pytest.fixture
def failing_scandir(monkeypatch):
    """Make listing a folder fail with the given error when the predicate matches its path."""

    def fail(matches, error: OSError) -> None:
        real_scandir = os.scandir

        def scandir(path):
            if matches(Path(path)):
                raise error
            return real_scandir(path)

        monkeypatch.setattr(os, "scandir", scandir)

    return fail


@pytest.fixture
def full_disk(monkeypatch):
    """Make the marker write fail as if the drive were full (R1.6)."""

    def fsync(fd):
        raise OSError(28, "There is not enough space on the disk")

    monkeypatch.setattr(os, "fsync", fsync)


@pytest.fixture(autouse=True)
def program_data(tmp_path_factory, monkeypatch):
    """Point syncplan's own folder at a temporary place: no test touches the real log or lock.

    It is a separate temporary folder, never inside a test's own tree, so it can't overlap
    the sources and backups tests create (R3.9, design D18).
    """
    folder = tmp_path_factory.mktemp("programdata")
    monkeypatch.setenv("PROGRAMDATA", str(folder))
    return folder


@pytest.fixture
def typed_case_kept(monkeypatch):
    """resolve() would restore the on-disk letter case and hide case-sensitive comparisons."""
    monkeypatch.setattr(Path, "resolve", Path.absolute)


@pytest.fixture
def held_lock(program_data):
    """Another syncplan run, simulated by a process holding the run lock."""
    lock = program_data / "syncplan" / "syncplan.lock"
    lock.parent.mkdir(parents=True, exist_ok=True)
    holder = subprocess.Popen(
        [sys.executable, "-c", _HOLD_LOCK, str(lock)], stdout=subprocess.PIPE, text=True
    )
    assert holder.stdout.readline().strip() == "held"
    yield lock
    holder.kill()
    holder.wait()
    lock.chmod(stat.S_IWRITE)


@pytest.fixture
def run_cli(capsys):
    """Run syncplan with the given arguments and return (exit code, stdout, stderr)."""

    def run(*args: object) -> tuple[int, str, str]:
        try:
            code = cli.main([str(arg) for arg in args])
        except SystemExit as exit_info:
            code = exit_info.code
        captured = capsys.readouterr()
        return code, captured.out, captured.err

    return run


@pytest.fixture
def locked():
    """Hold files open with no sharing at all, as some programs do (plan-copies SP1)."""
    handles = []

    def lock(path: Path) -> Path:
        handle = _kernel32.CreateFileW(str(path), _READ_WRITE, 0, None, _OPEN_EXISTING, 0, None)
        if handle == _INVALID_HANDLE:
            raise ctypes.WinError(ctypes.get_last_error())
        handles.append(handle)
        return path

    yield lock
    for handle in handles:
        _kernel32.CloseHandle(handle)


@pytest.fixture
def junction():
    """Make a directory junction, or skip the test when this machine can't (risk K2)."""

    def make(link: Path, target: Path) -> Path:
        result = subprocess.run(
            ["cmd", "/c", "mklink", "/J", str(link), str(target)], capture_output=True, text=True
        )
        if result.returncode != 0:
            pytest.skip(f"can't create a junction here: {result.stdout or result.stderr}")
        return link

    return make


@pytest.fixture
def sparse_file():
    """Make a file of any size that uses almost no disk (spike SP12, risk K5).

    Never use truncate() for this: on Windows it writes real zeros. The file is marked
    sparse and extended with SetEndOfFile. If it still takes real space, it is deleted
    and the test is skipped. Every file made is deleted at the end of the test.
    """
    made: list[Path] = []

    def make(path: Path, size: int) -> Path:
        path.parent.mkdir(parents=True, exist_ok=True)
        made.append(path)
        with path.open("wb") as file:
            handle = wintypes.HANDLE(msvcrt.get_osfhandle(file.fileno()))
            returned = wintypes.DWORD()
            ok = _kernel32.DeviceIoControl(
                handle, _FSCTL_SET_SPARSE, None, 0, None, 0, ctypes.byref(returned), None
            )
            ok = ok and _kernel32.SetFilePointerEx(handle, ctypes.c_longlong(size), None, 0)
            ok = ok and _kernel32.SetEndOfFile(handle)
        if not ok or size_on_disk(path) > 2**20:
            path.unlink()
            pytest.skip("this filesystem can't make sparse files")
        return path

    yield make
    for path in made:
        path.unlink(missing_ok=True)
