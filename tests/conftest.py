"""Shared test helpers: synthetic folder trees, a CLI runner, junctions and sparse files."""

import ctypes
import msvcrt
import subprocess
from ctypes import wintypes
from pathlib import Path

import pytest

from syncplan import cli

_kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
_FSCTL_SET_SPARSE = 0x000900C4


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


def size_on_disk(path: Path) -> int:
    """Bytes the file really occupies on disk, which for a sparse file is far below its size."""
    high = wintypes.DWORD()
    low = _kernel32.GetCompressedFileSizeW(str(path), ctypes.byref(high))
    return (high.value << 32) + low


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
