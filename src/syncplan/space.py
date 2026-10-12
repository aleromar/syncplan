"""How much space the sources need on the backup drive (R2.5-R2.8)."""

import ctypes
import logging
import os
import time
from pathlib import Path

logger = logging.getLogger(__name__)
_kernel32 = ctypes.windll.kernel32
_DEFAULT_BLOCK = 4096
_UNITS = ("KB", "MB", "GB", "TB")
_WHOLE_FROM = 9.95  # sizes that would show as 10.0 or more drop the decimal (design D7)


def block_size(folder: Path) -> int:
    """The block (cluster) size of the drive holding folder, or 4096 if Windows won't say (D6).

    The folder itself is asked about, not its drive letter, so a drive mounted in a folder
    reports its own blocks. Windows wants the path with a trailing backslash (SP4b).
    """
    asked = str(folder)
    if not asked.endswith("\\"):
        asked += "\\"
    sectors, sector_bytes, free, total = (ctypes.c_ulong() for _ in range(4))
    ok = _kernel32.GetDiskFreeSpaceW(
        asked,
        ctypes.byref(sectors),
        ctypes.byref(sector_bytes),
        ctypes.byref(free),
        ctypes.byref(total),
    )
    size = sectors.value * sector_bytes.value
    if ok and size:
        logger.debug("block size of %s: %d bytes", folder, size)
        return size
    logger.debug("block size of %s: assumed %d bytes (Windows didn't say)", folder, _DEFAULT_BLOCK)
    return _DEFAULT_BLOCK


def folder_size(folder: Path, block: int) -> tuple[int, int]:
    """The size of the files under folder, each rounded up to whole blocks (design D5).

    Returns (bytes, subfolders that couldn't be listed). Links aren't followed, and a
    file that vanishes during the walk is ignored. An error on folder itself is raised.
    """
    total = skipped = files = 0
    started = time.perf_counter()
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
                    files += 1
        except OSError as error:
            if current == os.fspath(folder):
                raise
            folder.stat()  # still there? If the drive was unplugged, stop (§2 error table)
            skipped += 1
            logger.debug("skipped %s: %s", current, error.strerror or error)
    elapsed = time.perf_counter() - started
    counted = f"{files:,} file" if files == 1 else f"{files:,} files"
    logger.debug(
        "measured %s: %s in %s, %d skipped, %.2f s",
        folder,
        format_size(total),
        counted,
        skipped,
        elapsed,
    )
    return total, skipped


def format_size(size: int) -> str:
    """Explorer's units: 1 KB = 1024 bytes, one decimal below 10 (design D7)."""
    if size < 1024:
        return "1 byte" if size == 1 else f"{size} bytes"
    value, units = size / 1024, list(_UNITS)
    while round(value, 1 if value < _WHOLE_FROM else 0) >= 1024 and len(units) > 1:
        value /= 1024
        units.pop(0)
    return f"{value:.1f} {units[0]}" if value < _WHOLE_FROM else f"{value:.0f} {units[0]}"
