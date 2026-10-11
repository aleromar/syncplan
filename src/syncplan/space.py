"""How much space the sources need on the backup drive (R2.5-R2.8)."""

import ctypes
import os
from pathlib import Path

_kernel32 = ctypes.windll.kernel32
_DEFAULT_BLOCK = 4096
_UNITS = ("KB", "MB", "GB", "TB")


def block_size(folder: Path) -> int:
    """The drive's block (cluster) size, or 4096 if Windows won't say (design D6)."""
    sectors, sector_bytes, free, total = (ctypes.c_ulong() for _ in range(4))
    ok = _kernel32.GetDiskFreeSpaceW(
        folder.anchor,
        ctypes.byref(sectors),
        ctypes.byref(sector_bytes),
        ctypes.byref(free),
        ctypes.byref(total),
    )
    size = sectors.value * sector_bytes.value
    return size if ok and size else _DEFAULT_BLOCK


def folder_size(folder: Path, block: int) -> tuple[int, int]:
    """The size of the files under folder, each rounded up to whole blocks (design D5).

    Returns (bytes, subfolders that couldn't be listed). Links aren't followed, and a
    file that vanishes during the walk is ignored. An error on folder itself is raised.
    """
    total = skipped = 0
    pending = [os.fspath(folder)]
    while pending:
        current = pending.pop()
        try:
            with os.scandir(current) as entries:
                for entry in entries:
                    if entry.is_symlink() or entry.is_junction():
                        continue
                    if entry.is_dir(follow_symlinks=False):
                        pending.append(entry.path)
                        continue
                    try:
                        size = entry.stat(follow_symlinks=False).st_size
                    except OSError:
                        continue
                    total += (size + block - 1) // block * block
        except OSError:
            if current == os.fspath(folder):
                raise
            skipped += 1
    return total, skipped


def format_size(size: int) -> str:
    """Explorer's units: 1 KB = 1024 bytes, one decimal below 10 (design D7)."""
    if size < 1024:
        return "1 byte" if size == 1 else f"{size} bytes"
    value, units = size / 1024, list(_UNITS)
    while round(value, 1 if value < 9.95 else 0) >= 1024 and len(units) > 1:
        value /= 1024
        units.pop(0)
    return f"{value:.1f} {units[0]}" if value < 9.95 else f"{value:.0f} {units[0]}"
