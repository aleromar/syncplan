import json
import os
import stat
import tempfile
from datetime import datetime
from pathlib import Path

import pytest
from conftest import make_tree
from hypothesis import example, given
from hypothesis import strategies as st

from syncplan import marker
from syncplan.marker import (
    FORMAT,
    Marker,
    MarkerChanged,
    NewerMarker,
    NotInitialized,
    Source,
    UnreadableMarker,
    Waiting,
    has_marker,
    read_marker,
    replace_marker,
    write_marker,
)

VALID = {
    "syncplan_marker": 1,
    "created": "2026-10-08T16:40:00+02:00",
    "sources": [{"name": "Photos", "path": "C:\\Photos"}],
}


def put_marker(folder: Path, content: str | bytes) -> Path:
    return make_tree(folder, {".syncplan/marker.json": content}) / ".syncplan" / "marker.json"


def test_r4_1_valid_marker_gives_creation_time_and_sources(tmp_path):
    put_marker(tmp_path, json.dumps(VALID))

    found = read_marker(tmp_path)

    assert found == Marker(
        created=datetime.fromisoformat("2026-10-08T16:40:00+02:00"),
        sources=(Source("Photos", Path("C:\\Photos")),),
    )


def test_r4_1_marker_saved_with_byte_order_mark_still_reads(tmp_path):
    put_marker(tmp_path, b"\xef\xbb\xbf" + json.dumps(VALID).encode())

    assert read_marker(tmp_path).sources == (Source("Photos", Path("C:\\Photos")),)


def test_r4_1_extra_fields_are_ignored(tmp_path):
    put_marker(tmp_path, json.dumps({**VALID, "added_later": [1, 2]}))

    assert len(read_marker(tmp_path).sources) == 1


def test_r4_2_folder_without_marker_is_not_initialized(tmp_path):
    with pytest.raises(NotInitialized):
        read_marker(tmp_path)


def test_r4_2_syncplan_folder_without_marker_is_not_initialized(tmp_path):
    (tmp_path / ".syncplan" / "trash").mkdir(parents=True)

    with pytest.raises(NotInitialized):
        read_marker(tmp_path)


@pytest.mark.parametrize(
    "content",
    [
        "not json at all",
        "[]",
        '"text"',
        "{}",
        json.dumps({**VALID, "syncplan_marker": "1"}),
        json.dumps({**VALID, "syncplan_marker": True}),
        json.dumps({**VALID, "syncplan_marker": 0}),
        json.dumps({k: v for k, v in VALID.items() if k != "created"}),
        json.dumps({**VALID, "created": "yesterday"}),
        json.dumps({**VALID, "created": 20261008}),
        json.dumps({**VALID, "sources": {"name": "Photos"}}),
        json.dumps({**VALID, "sources": "C:\\Photos"}),
        json.dumps({**VALID, "sources": [{"name": "Photos"}]}),
        json.dumps({**VALID, "sources": [{"name": 7, "path": "C:\\Photos"}]}),
        json.dumps({**VALID, "sources": [["Photos", "C:\\Photos"]]}),
        json.dumps({**VALID, "sources": {}}),
        json.dumps({**VALID, "sources": ""}),
    ],
)
def test_r4_3_damaged_marker_is_unreadable(tmp_path, content):
    path = put_marker(tmp_path, content)

    with pytest.raises(UnreadableMarker) as error:
        read_marker(tmp_path)

    assert str(path) in str(error.value)
    assert "inspect" in str(error.value)


def test_r4_3_marker_that_is_a_folder_is_unreadable(tmp_path):
    (tmp_path / ".syncplan" / "marker.json").mkdir(parents=True)

    with pytest.raises(UnreadableMarker):
        read_marker(tmp_path)


def test_r4_3_marker_that_is_not_utf8_is_unreadable(tmp_path):
    put_marker(tmp_path, b'{"syncplan_marker": 1, "created": "\xff\xfe"}')

    with pytest.raises(UnreadableMarker):
        read_marker(tmp_path)


def test_r4_4_newer_format_says_to_upgrade(tmp_path):
    put_marker(tmp_path, json.dumps({**VALID, "syncplan_marker": FORMAT + 1}))

    with pytest.raises(NewerMarker) as error:
        read_marker(tmp_path)

    assert "upgrade" in str(error.value)


def test_r4_4_newer_format_is_reported_even_if_its_shape_changed(tmp_path):
    put_marker(tmp_path, json.dumps({"syncplan_marker": FORMAT + 1, "volumes": []}))

    with pytest.raises(NewerMarker):
        read_marker(tmp_path)


def test_r1_1_marker_records_format_creation_time_and_sources(tmp_path):
    sources = [Source("Photos", Path("C:\\Photos")), Source("Docs", Path("D:\\Work\\Documents"))]

    written = write_marker(tmp_path, sources)

    data = json.loads((tmp_path / ".syncplan" / "marker.json").read_text(encoding="ascii"))
    assert data["syncplan_marker"] == FORMAT
    created = datetime.fromisoformat(data["created"])
    assert created.utcoffset() is not None
    assert created == written.created
    assert data["sources"] == [
        {"name": "Photos", "path": "C:\\Photos"},
        {"name": "Docs", "path": "D:\\Work\\Documents"},
    ]


@given(
    st.lists(
        st.tuples(st.text(min_size=1, max_size=20), st.text(min_size=1, max_size=40)),
        max_size=5,
    )
)
@example([("\udc80", "C:\\\u5199\u771f"), ("Fotos", "D:\\\u0424\u043e\u0442\u043e")])
def test_r1_1_written_marker_reads_back_the_same_sources(pairs):
    sources = [Source(name, Path(path)) for name, path in pairs]
    with tempfile.TemporaryDirectory() as folder:
        write_marker(Path(folder), sources)

        assert read_marker(Path(folder)).sources == tuple(sources)
        assert (Path(folder) / ".syncplan" / "marker.json").read_bytes().isascii()


def test_r2_4_has_marker_is_false_without_marker_file(tmp_path):
    (tmp_path / ".syncplan").mkdir()

    assert not has_marker(tmp_path)


def test_r2_4_has_marker_raises_when_it_cannot_look(tmp_path, monkeypatch):
    # A marker hidden by a permission error must not pass for "no marker" (R2.4, R3.6).
    real_stat = Path.stat

    def denied(self, *args, **kwargs):
        if self.name == "marker.json":
            raise PermissionError(13, "Access is denied", str(self))
        return real_stat(self, *args, **kwargs)

    monkeypatch.setattr(Path, "stat", denied)

    with pytest.raises(PermissionError):
        has_marker(tmp_path)


def test_r2_4_has_marker_is_true_whatever_the_marker_says(tmp_path):
    put_marker(tmp_path, "damaged")

    assert has_marker(tmp_path)


def test_r1_6_failed_write_removes_the_syncplan_folder_it_created(tmp_path, full_disk):
    with pytest.raises(OSError, match="not enough space"):
        write_marker(tmp_path, [Source("Photos", Path("C:\\Photos"))])

    assert list(tmp_path.iterdir()) == []


def test_r1_6_failed_write_keeps_an_existing_syncplan_folder(tmp_path, full_disk):
    kept = make_tree(tmp_path, {".syncplan/trash/kept.txt": "kept"}) / ".syncplan" / "trash"

    with pytest.raises(OSError):
        write_marker(tmp_path, [])

    assert sorted(p.name for p in (tmp_path / ".syncplan").iterdir()) == ["trash"]
    assert (kept / "kept.txt").read_text() == "kept"


def test_r1_6_failed_cleanup_still_reports_the_original_error(tmp_path, monkeypatch):
    def drive_gone(fd):
        raise OSError(21, "The device is not ready")

    def cleanup_fails(self, missing_ok=False):
        raise OSError(21, "cleanup failed too")

    monkeypatch.setattr(marker.os, "fsync", drive_gone)
    monkeypatch.setattr(Path, "unlink", cleanup_fails)

    with pytest.raises(OSError, match="device is not ready"):
        write_marker(tmp_path, [])


def test_r1_6_data_is_flushed_to_disk_before_the_rename(tmp_path, monkeypatch):
    calls = []
    real_fsync, real_rename = marker.os.fsync, Path.rename

    def fsync(fd):
        calls.append("fsync")
        real_fsync(fd)

    def rename(self, target):
        calls.append("rename")
        return real_rename(self, target)

    monkeypatch.setattr(marker.os, "fsync", fsync)
    monkeypatch.setattr(Path, "rename", rename)

    write_marker(tmp_path, [])

    assert calls == ["fsync", "rename"]


def test_r1_6_existing_marker_is_never_overwritten(tmp_path):
    path = put_marker(tmp_path, json.dumps(VALID))
    before = path.read_bytes()

    with pytest.raises(FileExistsError):
        write_marker(tmp_path, [Source("Other", Path("D:\\Other"))])

    assert path.read_bytes() == before
    assert sorted(p.name for p in path.parent.iterdir()) == ["marker.json"]


def test_r1_6_stale_temporary_file_does_not_block_a_retry(tmp_path):
    stale = tmp_path / ".syncplan" / "marker-old.tmp"
    stale.parent.mkdir()
    stale.write_text("left over from a crash")

    write_marker(tmp_path, [Source("Photos", Path("C:\\Photos"))])

    assert read_marker(tmp_path).sources == (Source("Photos", Path("C:\\Photos")),)


def test_r1_6_backup_folder_that_vanished_is_not_recreated(tmp_path):
    gone = tmp_path / "Backups"

    with pytest.raises(FileNotFoundError):
        write_marker(gone, [])

    assert not gone.exists()


# manage-sources: waiting folders (R3.1, R4.3) and replacing the marker (R6, D3)

REMOVED = "2026-10-12T10:15:00+02:00"
WAITING = {"name": "Old", "path": "C:\\Old", "removed": REMOVED}


def marker_names(folder: Path) -> list[str]:
    return sorted(p.name for p in (folder / ".syncplan").iterdir())


def test_r3_1_waiting_folders_read_back_as_written(tmp_path):
    put_marker(tmp_path, json.dumps({**VALID, "to_quarantine": [WAITING]}))

    found = read_marker(tmp_path)

    assert found.waiting == (Waiting("Old", Path("C:\\Old"), datetime.fromisoformat(REMOVED)),)
    assert found.sources == (Source("Photos", Path("C:\\Photos")),)


def test_r3_1_marker_without_a_waiting_list_has_no_waiting_folders(tmp_path):
    put_marker(tmp_path, json.dumps(VALID))

    assert read_marker(tmp_path).waiting == ()


@pytest.mark.parametrize(
    "waiting",
    [
        {},
        "C:\\Old",
        [["Old", "C:\\Old"]],
        [{"name": "Old", "path": "C:\\Old"}],
        [{**WAITING, "removed": "yesterday"}],
        [{**WAITING, "name": 7}],
    ],
)
def test_r4_3_damaged_waiting_list_is_unreadable(tmp_path, waiting):
    put_marker(tmp_path, json.dumps({**VALID, "to_quarantine": waiting}))

    with pytest.raises(UnreadableMarker):
        read_marker(tmp_path)


def test_r6_1_replace_gives_the_new_lists_and_keeps_the_creation_time(tmp_path):
    put_marker(tmp_path, json.dumps(VALID))
    old = read_marker(tmp_path)
    sources = [Source("Music", Path("D:\\Music"))]
    waiting = [Waiting("Photos", Path("C:\\Photos"), datetime.fromisoformat(REMOVED))]

    replace_marker(tmp_path, old, sources, waiting)

    found = read_marker(tmp_path)
    assert (found.created, found.sources, found.waiting) == (
        old.created,
        tuple(sources),
        tuple(waiting),
    )
    assert marker_names(tmp_path) == ["marker.json"]


def test_r6_1_an_empty_waiting_list_is_left_out_of_the_file(tmp_path):
    put_marker(tmp_path, json.dumps({**VALID, "to_quarantine": [WAITING]}))
    old = read_marker(tmp_path)

    replace_marker(tmp_path, old, old.sources, [])

    data = json.loads((tmp_path / ".syncplan" / "marker.json").read_text(encoding="ascii"))
    assert "to_quarantine" not in data


_PAIRS = st.lists(
    st.tuples(st.text(min_size=1, max_size=20), st.text(min_size=1, max_size=40)), max_size=4
)


@given(_PAIRS, _PAIRS)
def test_r6_1_replaced_marker_reads_back_the_same_lists(source_pairs, waiting_pairs):
    sources = [Source(name, Path(path)) for name, path in source_pairs]
    removed = datetime.fromisoformat(REMOVED)
    waiting = [Waiting(name, Path(path), removed) for name, path in waiting_pairs]
    with tempfile.TemporaryDirectory() as folder:
        write_marker(Path(folder), [Source("Photos", Path("C:\\Photos"))])
        old = read_marker(Path(folder))

        replace_marker(Path(folder), old, sources, waiting)

        found = read_marker(Path(folder))
        assert (found.created, found.sources, found.waiting) == (
            old.created,
            tuple(sources),
            tuple(waiting),
        )


def test_d3_unknown_fields_survive_a_replace(tmp_path):
    photos = {"name": "Photos", "path": "C:\\Photos", "exclude": ["*.tmp"]}
    content = {**VALID, "added_later": [1, 2], "sources": [photos], "to_quarantine": [WAITING]}
    put_marker(tmp_path, b"\xef\xbb\xbf" + json.dumps(content).encode())
    old = read_marker(tmp_path)

    replace_marker(tmp_path, old, [*old.sources, Source("Music", Path("D:\\Music"))], old.waiting)

    data = json.loads((tmp_path / ".syncplan" / "marker.json").read_text(encoding="ascii"))
    assert data["added_later"] == [1, 2]
    assert data["created"] == VALID["created"]
    assert data["sources"] == [photos, {"name": "Music", "path": "D:\\Music"}]
    assert data["to_quarantine"] == [WAITING]


class _FullDiskFile:
    """Stands in for the temporary file: writing to it fails as on a full drive."""

    def __init__(self, handle, *args, **kwargs):
        self.handle = handle

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        os.close(self.handle)

    def write(self, text):
        raise OSError(28, "There is not enough space on the disk")


def _fail_at(monkeypatch, step: str) -> None:
    """Make one step of replace_marker fail as Windows would."""

    def fail(*args, **kwargs):
        raise OSError(21, "The device is not ready")

    real_read = Path.read_bytes
    if step == "temporary file":
        monkeypatch.setattr(marker.tempfile, "mkstemp", fail)
    elif step == "write":
        monkeypatch.setattr(marker.os, "fdopen", _FullDiskFile)
    elif step == "fsync":
        monkeypatch.setattr(marker.os, "fsync", fail)
    elif step == "check":
        monkeypatch.setattr(
            Path,
            "read_bytes",
            lambda self: fail() if self.name == "marker.json" else real_read(self),
        )
    else:
        monkeypatch.setattr(Path, "replace", fail)


@pytest.mark.parametrize("step", ["temporary file", "write", "fsync", "check", "swap"])
def test_r6_1_a_failure_at_any_step_leaves_the_old_marker(tmp_path, monkeypatch, step):
    path = put_marker(tmp_path, json.dumps({**VALID, "to_quarantine": [WAITING]}))
    before = path.read_bytes()
    old = read_marker(tmp_path)
    _fail_at(monkeypatch, step)

    with pytest.raises(OSError):
        replace_marker(tmp_path, old, [Source("Music", Path("D:\\Music"))], [])

    monkeypatch.undo()
    assert path.read_bytes() == before
    assert marker_names(tmp_path) == ["marker.json"]


def test_r6_1_flushed_then_checked_then_swapped(tmp_path, monkeypatch):
    put_marker(tmp_path, json.dumps(VALID))
    old = read_marker(tmp_path)
    calls = []
    real_fsync, real_read, real_replace = marker.os.fsync, Path.read_bytes, Path.replace

    def fsync(fd):
        calls.append("fsync")
        real_fsync(fd)

    def read_bytes(self):
        if self.name == "marker.json":
            calls.append("check")
        return real_read(self)

    def replace(self, target):
        calls.append("swap")
        return real_replace(self, target)

    monkeypatch.setattr(marker.os, "fsync", fsync)
    monkeypatch.setattr(Path, "read_bytes", read_bytes)
    monkeypatch.setattr(Path, "replace", replace)

    replace_marker(tmp_path, old, [], [])

    assert calls == ["fsync", "check", "swap"]


def test_r6_2_marker_held_open_by_another_program_is_not_replaced(tmp_path):
    # Real Windows behaviour, no fakes: any open handle makes the swap fail (SP2).
    path = put_marker(tmp_path, json.dumps(VALID))
    before = path.read_bytes()
    old = read_marker(tmp_path)

    with path.open("rb"), pytest.raises(PermissionError):
        replace_marker(tmp_path, old, [], [])

    assert path.read_bytes() == before
    assert marker_names(tmp_path) == ["marker.json"]


def test_r6_2_read_only_marker_is_not_replaced(tmp_path):
    path = put_marker(tmp_path, json.dumps(VALID))
    before = path.read_bytes()
    old = read_marker(tmp_path)
    path.chmod(stat.S_IREAD)
    try:
        with pytest.raises(PermissionError):
            replace_marker(tmp_path, old, [], [])
    finally:
        path.chmod(stat.S_IREAD | stat.S_IWRITE)

    assert path.read_bytes() == before
    assert marker_names(tmp_path) == ["marker.json"]


def test_r6_2_a_drive_gone_at_the_check_is_an_error_not_a_change(tmp_path, monkeypatch):
    # A removed drive reports "not found" for every path on it (SP6).
    put_marker(tmp_path, json.dumps(VALID))
    old = read_marker(tmp_path)
    real_read, real_stat = Path.read_bytes, Path.stat

    def gone(path):
        raise FileNotFoundError(2, "The system cannot find the path specified", str(path))

    monkeypatch.setattr(
        Path,
        "read_bytes",
        lambda self: gone(self) if self.name == "marker.json" else real_read(self),
    )
    monkeypatch.setattr(
        Path,
        "stat",
        lambda self, **kw: gone(self) if self.name == ".syncplan" else real_stat(self, **kw),
    )

    with pytest.raises(FileNotFoundError):
        replace_marker(tmp_path, old, [], [])


def test_r6_3_marker_changed_since_it_was_read_is_kept(tmp_path):
    path = put_marker(tmp_path, json.dumps(VALID))
    old = read_marker(tmp_path)
    path.write_text(json.dumps({**VALID, "sources": []}))
    changed = path.read_bytes()

    with pytest.raises(MarkerChanged) as error:
        replace_marker(tmp_path, old, [Source("Music", Path("D:\\Music"))], [])

    assert str(tmp_path) in str(error.value)
    assert path.read_bytes() == changed
    assert marker_names(tmp_path) == ["marker.json"]


def test_r6_3_marker_removed_since_it_was_read_is_a_change(tmp_path):
    path = put_marker(tmp_path, json.dumps(VALID))
    old = read_marker(tmp_path)
    path.unlink()

    with pytest.raises(MarkerChanged):
        replace_marker(tmp_path, old, [], [])

    assert marker_names(tmp_path) == []
