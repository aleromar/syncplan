"""The backup drive: how much space the sources need (R2.5-R2.8), and its format."""

import ctypes
import logging
import os
import time
from pathlib import Path
from shutil import disk_usage

from syncplan.checks import Setup

logger = logging.getLogger(__name__)
_kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
_BUFFER = 300  # characters: a volume's root path, or its format's name
_DEFAULT_BLOCK = 4096
_FREE_SHARE = 10  # warn when less than 1/10 of the drive would be left (R2.7)
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


def filesystem(folder: Path) -> tuple[str, str]:
    """The root of the volume holding folder, and its format: ("E:\\", "NTFS") (plan-copies D9).

    The volume is looked up from the folder, so a drive mounted in a folder counts as itself.
    """
    root = ctypes.create_unicode_buffer(_BUFFER)
    kind = ctypes.create_unicode_buffer(_BUFFER)
    if not _kernel32.GetVolumePathNameW(str(folder), root, _BUFFER) or not (
        _kernel32.GetVolumeInformationW(root.value, None, 0, None, None, None, kind, _BUFFER)
    ):
        error = ctypes.WinError(ctypes.get_last_error())
        error.strerror, error.filename = error.strerror.rstrip("."), str(folder)  # as os does
        raise error
    logger.debug("drive %s: formatted as %s", root.value, kind.value)
    return root.value, kind.value


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
                    total += rounded(size, block)
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


def rounded(size: int, block: int) -> int:
    """The space a file of size bytes takes: whole blocks (design D5)."""
    return (size + block - 1) // block * block


def measure(setup: Setup) -> tuple[list[int], str, list[str]]:
    """R2.5-R2.8: each source's size, the space line, and warnings if the drive looks short."""
    block = block_size(setup.backup)
    sizes, skipped = [], 0
    for source in setup.sources:
        size, missed = folder_size(source.path, block)
        sizes.append(size)
        skipped += missed
    needed = sum(sizes)
    capacity, _, free = disk_usage(setup.backup)
    drive = setup.backup.anchor
    logger.debug("drive %s: capacity %s, free %s", drive, format_size(capacity), format_size(free))
    line = f"Space needed: about {format_size(needed)}. {drive} has {format_size(free)} free."
    if skipped == 1:
        line += " (1 folder couldn't be read and isn't counted)"
    elif skipped:
        line += f" ({skipped} folders couldn't be read and aren't counted)"
    warnings = []
    if needed > free:
        warnings.append(
            f"Warning: about {format_size(needed - free)} short."
            " The first apply will stop before copying unless you free space."
        )
        for folder in setup.adopted:
            try:
                copy = format_size(folder_size(folder, block)[0])
            except OSError:
                copy = "size unknown"
            warnings.append(
                f"An existing copy in {folder} ({copy}) may cover part of this;"
                " the first plan gives the exact figure."
            )
    elif (left := free - needed) * _FREE_SHARE < capacity:
        warnings.append(
            f"Note: after the first backup only {format_size(left)} ({left * 100 // capacity}%)"
            f" of {drive} would be free. Overwritten and deleted files are kept in quarantine"
            " on this drive, so it may fill up."
        )
    return sizes, line, warnings


def format_size(size: int) -> str:
    """Explorer's units: 1 KB = 1024 bytes, one decimal below 10 (design D7)."""
    if size < 1024:
        return "1 byte" if size == 1 else f"{size} bytes"
    value, units = size / 1024, list(_UNITS)
    while round(value, 1 if value < _WHOLE_FROM else 0) >= 1024 and len(units) > 1:
        value /= 1024
        units.pop(0)
    return f"{value:.1f} {units[0]}" if value < _WHOLE_FROM else f"{value:.0f} {units[0]}"
