import ctypes
import os
import tempfile
from pathlib import Path

import pytest
from conftest import make_tree
from hypothesis import given, settings
from hypothesis import strategies as st

from syncplan import space
from syncplan.space import block_size, folder_size, format_size

KB, MB, GB, TB = 2**10, 2**20, 2**30, 2**40


def rounded(size: int, block: int) -> int:
    """The independent oracle: size rounded up to whole blocks."""
    blocks, rest = divmod(size, block)
    return (blocks + (1 if rest else 0)) * block


# folder_size: the corner-case matrix (design §2)


def test_d5_empty_source_needs_nothing(tmp_path):
    assert folder_size(tmp_path, 4096) == (0, 0)


def test_d5_only_empty_folders_need_nothing(tmp_path):
    make_tree(tmp_path, {"a/b/c": None, "d": None})

    assert folder_size(tmp_path, 4096) == (0, 0)


def test_d5_zero_byte_files_need_nothing(tmp_path):
    make_tree(tmp_path, {f"empty{i}.txt": b"" for i in range(10)})

    assert folder_size(tmp_path, 4096) == (0, 0)


@pytest.mark.parametrize(("size", "needed"), [(1, 4096), (4095, 4096), (4096, 4096), (4097, 8192)])
def test_d5_each_file_is_rounded_up_to_whole_blocks(tmp_path, size, needed):
    make_tree(tmp_path, {"f.bin": b"x" * size})

    assert folder_size(tmp_path, 4096) == (needed, 0)


def test_d5_many_small_files_on_big_blocks_need_a_block_each(tmp_path):
    make_tree(tmp_path, {f"s{i:04}.txt": b"x" for i in range(2000)})

    total, _ = folder_size(tmp_path, 128 * KB)

    assert total == 2000 * 128 * KB
    assert format_size(total) == "250 MB"


def test_d5_deeply_nested_file_is_counted(tmp_path):
    deep = "/".join(["d"] * 30)
    make_tree(tmp_path, {f"{deep}/f.txt": b"x" * 10})

    assert folder_size(tmp_path, 4096) == (4096, 0)


def test_d5_wide_tree_is_counted_completely(tmp_path):
    make_tree(tmp_path, {f"dir{d:03}/f{f}.txt": b"x" * 10 for d in range(200) for f in range(5)})

    assert folder_size(tmp_path, 4096) == (1000 * 4096, 0)


def test_d5_large_files_are_counted_exactly(tmp_path, sparse_file):
    sparse_file(tmp_path / "video.mkv", 5 * GB)
    sparse_file(tmp_path / "archive" / "disk.img", TB)

    total, _ = folder_size(tmp_path, 4096)

    assert total == 5 * GB + TB
    assert format_size(5 * GB) == "5.0 GB"
    assert format_size(TB) == "1.0 TB"


def test_d5_mixed_tree_matches_an_independent_sum(tmp_path, sparse_file):
    sizes = {"a.txt": 0, "b/c.txt": 1, "b/d/e.bin": 4096, "b/d/f.bin": 4097, "g/h.bin": 70_000}
    make_tree(tmp_path, {path: b"x" * size for path, size in sizes.items()} | {"empty": None})
    sparse_file(tmp_path / "b" / "big.bin", 3 * GB + 1)

    for block in (512, 4096, 128 * KB):
        expected = sum(rounded(size, block) for size in [*sizes.values(), 3 * GB + 1])
        assert folder_size(tmp_path, block) == (expected, 0)


def test_d5_hidden_system_and_read_only_files_are_counted(tmp_path):
    files = [tmp_path / name for name in ("hidden.txt", "system.txt", "readonly.txt")]
    for file in files:
        file.write_bytes(b"x" * 100)
    set_attributes = ctypes.windll.kernel32.SetFileAttributesW
    for file, attribute in zip(files, (0x2, 0x4, 0x1), strict=True):
        assert set_attributes(str(file), attribute)
    try:
        assert folder_size(tmp_path, 4096) == (3 * 4096, 0)
    finally:
        for file in files:
            set_attributes(str(file), 0x80)


def test_d5_unusual_names_are_counted(tmp_path):
    # 150 characters keeps the whole path under 260; longer paths are F8's (NG9).
    make_tree(tmp_path, {"a b.txt": "x", "写真.jpg": "x", "n" * 150: "x"})

    assert folder_size(tmp_path, 4096) == (3 * 4096, 0)


def test_d5_junction_inside_a_source_is_not_followed(tmp_path, junction, sparse_file):
    sparse_file(tmp_path / "elsewhere" / "big.bin", 5 * GB)
    source = make_tree(tmp_path, {"source/a.txt": "x"}) / "source"
    junction(source / "link", tmp_path / "elsewhere")

    assert folder_size(source, 4096) == (4096, 0)


def test_d5_file_symlink_inside_a_source_is_not_followed(tmp_path, sparse_file):
    target = sparse_file(tmp_path / "big.bin", 5 * GB)
    source = make_tree(tmp_path, {"source/a.txt": "x"}) / "source"
    try:
        (source / "link.bin").symlink_to(target)
    except OSError as error:
        pytest.skip(f"file symlinks need admin rights here (SP13): WinError {error.winerror}")

    assert folder_size(source, 4096) == (4096, 0)


@pytest.mark.parametrize(
    "error",
    [
        PermissionError(13, "Access is denied"),
        FileNotFoundError(2, "The system cannot find the path specified"),
        OSError(0, "The filename or extension is too long", None, 206),
    ],
)
def test_r2_8_unreadable_subfolder_is_skipped_and_counted(tmp_path, failing_scandir, error):
    make_tree(tmp_path, {"ok/a.txt": "x", "locked/b.txt": "x"})
    failing_scandir(lambda path: path.name == "locked", error)

    assert folder_size(tmp_path, 4096) == (4096, 1)


def test_d5_file_that_vanishes_during_the_walk_is_ignored(tmp_path, monkeypatch):
    make_tree(tmp_path, {"stays.txt": "x", "goes.txt": "x"})
    real_scandir = os.scandir

    class Vanishing:
        def __init__(self, entry):
            self._entry = entry

        def __getattr__(self, name):
            return getattr(self._entry, name)

        def stat(self, follow_symlinks=True):
            if self._entry.name == "goes.txt":
                raise FileNotFoundError(2, "gone")
            return self._entry.stat(follow_symlinks=follow_symlinks)

    class Listing:
        def __init__(self, path):
            self._listing = real_scandir(path)

        def __enter__(self):
            return (Vanishing(entry) for entry in self._listing)

        def __exit__(self, *exc):
            self._listing.close()

    monkeypatch.setattr(space.os, "scandir", Listing)

    assert folder_size(tmp_path, 4096) == (4096, 0)


def test_r2_5_source_root_that_fails_is_an_error(tmp_path):
    with pytest.raises(FileNotFoundError):
        folder_size(tmp_path / "unplugged", 4096)


@settings(max_examples=40, deadline=None)
@given(
    files=st.dictionaries(
        st.lists(st.sampled_from("abc"), min_size=0, max_size=3).map(
            lambda parts: "/".join([*parts, "f.bin"])
        ),
        st.integers(min_value=0, max_value=10 * KB),
        max_size=30,
    ),
    block=st.sampled_from([512, 4096, 128 * KB]),
)
def test_d5_folder_size_always_matches_an_independent_sum(files, block):
    with tempfile.TemporaryDirectory() as folder:
        make_tree(Path(folder), {path: b"x" * size for path, size in files.items()})

        assert folder_size(Path(folder), block) == (
            sum(rounded(size, block) for size in files.values()),
            0,
        )


# block_size (design D6)


class FakeKernel32:
    def __init__(self, sectors_per_cluster: int, bytes_per_sector: int, ok: bool = True):
        self.values, self.ok, self.roots = (sectors_per_cluster, bytes_per_sector), ok, []

    def GetDiskFreeSpaceW(self, root, sectors, sector_bytes, free, total):
        self.roots.append(root)
        sectors._obj.value, sector_bytes._obj.value = self.values
        return self.ok


def test_d6_block_size_asks_windows_about_the_backup_folder_itself(tmp_path, monkeypatch):
    # The folder itself, not its drive letter: a drive mounted in a folder has its own blocks.
    fake = FakeKernel32(256, 512)
    monkeypatch.setattr(space, "_kernel32", fake)

    assert block_size(tmp_path / "Backups") == 128 * KB
    assert fake.roots == [f"{tmp_path / 'Backups'}\\"]


def test_d6_block_size_falls_back_to_4096_when_windows_refuses(tmp_path, monkeypatch):
    monkeypatch.setattr(space, "_kernel32", FakeKernel32(256, 512, ok=False))

    assert block_size(tmp_path) == 4096


def test_d6_block_size_of_this_drive_is_a_power_of_two(tmp_path):
    size = block_size(tmp_path)

    assert size >= 512
    assert size & (size - 1) == 0


# format_size (design D7)


@pytest.mark.parametrize(
    ("size", "text"),
    [
        (0, "0 bytes"),
        (1, "1 byte"),
        (1023, "1023 bytes"),
        (1024, "1.0 KB"),
        (10_239, "10 KB"),
        (1536 * KB, "1.5 MB"),
        (1023 * MB + 1000 * KB, "1.0 GB"),
        (312 * GB, "312 GB"),
        (TB, "1.0 TB"),
        (2048 * TB, "2048 TB"),
    ],
)
def test_d7_format_size_uses_explorer_units(size, text):
    assert format_size(size) == text


# The report (R2.5-R2.8) through the command


@pytest.fixture
def drive(monkeypatch):
    """Pretend the backup drive has the given capacity and free space, and 4096-byte blocks.

    Returns the folders that were asked about, so tests can check it was the backup.
    """
    asked: list[Path] = []

    def disk_usage(path):
        asked.append(Path(path))
        return capacity_free[0], capacity_free[0] - capacity_free[1], capacity_free[1]

    def fake_block_size(folder):
        asked.append(Path(folder))
        return 4096

    capacity_free = [0, 0]

    def set_drive(capacity: int, free: int) -> list[Path]:
        capacity_free[:] = [capacity, free]
        monkeypatch.setattr(space, "disk_usage", disk_usage)
        monkeypatch.setattr(space, "block_size", fake_block_size)
        return asked

    return set_drive


def two_sources(tmp_path):
    make_tree(tmp_path, {"Photos/a.jpg": b"x" * 5000, "Docs/b.txt": b"x" * 100, "backup": None})
    return tmp_path / "backup", tmp_path / "Photos", tmp_path / "Docs"


def test_r2_5_report_gives_each_size_the_total_and_free_space(tmp_path, run_cli, drive):
    backup, photos, docs = two_sources(tmp_path)
    asked = drive(capacity=1000 * MB, free=900 * MB)

    code, out, err = run_cli("init", backup, photos, docs)

    assert code == 0
    assert asked == [backup.resolve(), backup.resolve()]  # blocks and free space of the backup
    lines = out.splitlines()
    assert lines[0] == "Measuring sources..."
    assert any("Photos" in line and line.endswith("8.0 KB") for line in lines)
    assert any("Docs" in line and line.endswith("4.0 KB") for line in lines)
    assert f"Space needed: about 12 KB. {tmp_path.anchor} has 900 MB free." in lines
    assert err == ""  # no warning: plenty of room


def test_r2_5_nothing_is_measured_without_sources(tmp_path, run_cli):
    backup, _, _ = two_sources(tmp_path)

    _, out, _ = run_cli("init", backup)

    assert "Measuring" not in out
    assert "Space needed" not in out


def test_r2_6_too_small_drive_warns_but_still_initializes(tmp_path, run_cli, drive):
    backup, photos, docs = two_sources(tmp_path)
    drive(capacity=100 * MB, free=4 * KB)

    code, _, err = run_cli("init", backup, photos, docs)

    assert code == 0
    assert (backup / ".syncplan" / "marker.json").exists()
    assert "Warning: about 8.0 KB short." in err  # warnings go to the error output (R5.5)


def test_r2_6_warning_names_an_old_copy_that_may_cover_part(tmp_path, run_cli, drive):
    backup, photos, _ = two_sources(tmp_path)
    make_tree(backup, {"Photos/old.jpg": b"x" * 100})
    drive(capacity=100 * MB, free=4 * KB)

    _, _, err = run_cli("init", backup, photos)

    old_copy = backup.resolve() / "Photos"
    assert f"An existing copy in {old_copy} (4.0 KB) may cover part of this" in err


def test_r2_6_an_old_copy_that_cannot_be_measured_is_still_named(
    tmp_path, run_cli, drive, failing_scandir
):
    backup, photos, _ = two_sources(tmp_path)
    make_tree(backup, {"Photos/old.jpg": b"x" * 100})
    drive(capacity=100 * MB, free=4 * KB)
    failing_scandir(lambda path: path.parent.name == "backup", PermissionError(13, "denied"))

    _, _, err = run_cli("init", backup, photos)

    old_copy = backup.resolve() / "Photos"
    assert f"An existing copy in {old_copy} (size unknown) may cover part of this" in err


def test_r2_6_space_needed_exactly_equal_to_free_space_fits(tmp_path, run_cli, drive):
    backup, photos, docs = two_sources(tmp_path)
    drive(capacity=1000 * MB, free=12 * KB)

    _, out, err = run_cli("init", backup, photos, docs)

    assert "short" not in out + err


@pytest.mark.parametrize(
    ("free", "warned"), [(100 * MB + 12 * KB, False), (100 * MB + 12 * KB - 1, True)]
)
def test_r2_7_warns_only_below_10_percent_left(tmp_path, run_cli, drive, free, warned):
    backup, photos, docs = two_sources(tmp_path)
    drive(capacity=1000 * MB, free=free)

    code, _, err = run_cli("init", backup, photos, docs)

    assert code == 0
    assert ("kept in quarantine on this drive" in err) is warned
    if warned:
        assert "only 100 MB (9%)" in err


@pytest.mark.parametrize(
    ("locked", "note"),
    [
        (["locked"], "(1 folder couldn't be read and isn't counted)"),
        (["locked", "secret"], "(2 folders couldn't be read and aren't counted)"),
    ],
)
def test_r2_8_skipped_folders_are_mentioned_on_the_space_line(
    tmp_path, run_cli, failing_scandir, locked, note
):
    backup, photos, _ = two_sources(tmp_path)
    make_tree(photos, {f"{name}/c.jpg": "x" for name in locked})
    failing_scandir(lambda path: path.name in locked, PermissionError(13, "Access is denied"))

    code, out, _ = run_cli("init", backup, photos)

    assert code == 0
    assert note in out


def test_r2_5_source_root_failing_while_measuring_stops_before_writing(
    tmp_path, run_cli, failing_scandir
):
    backup, photos, _ = two_sources(tmp_path)
    root = photos.resolve()
    failing_scandir(lambda path: path == root, OSError(21, "The device is not ready", str(root)))

    code, _, err = run_cli("init", backup, photos)

    assert code == 1
    assert f"{root}: The device is not ready." in err
    assert not (backup / ".syncplan").exists()


def test_r2_5_source_drive_unplugged_during_the_walk_stops(tmp_path, run_cli, monkeypatch):
    # Once the drive is gone every subfolder fails too; that must not pass as a few skipped ones.
    backup, photos, _ = two_sources(tmp_path)
    make_tree(photos, {"sub/c.jpg": "x"})
    root = photos.resolve()
    real_scandir, real_stat = os.scandir, os.stat
    unplugged = []

    def scandir(path):
        if Path(path) != root:
            raise OSError(21, "The device is not ready", str(path))
        unplugged.append(True)  # the drive goes away right after its top folder is listed
        return real_scandir(path)

    def stat(path, *args, **kwargs):
        if unplugged and Path(path) == root:
            raise OSError(21, "The device is not ready", str(path))
        return real_stat(path, *args, **kwargs)

    monkeypatch.setattr(space.os, "scandir", scandir)
    monkeypatch.setattr(space.os, "stat", stat)

    code, _, err = run_cli("init", backup, photos)

    assert code == 1
    assert "The device is not ready" in err
    assert not (backup / ".syncplan").exists()
