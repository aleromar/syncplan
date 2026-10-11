import json
import tempfile
from datetime import datetime
from pathlib import Path

import pytest
from hypothesis import example, given
from hypothesis import strategies as st

from syncplan import marker
from syncplan.marker import (
    FORMAT,
    Marker,
    NewerMarker,
    NotInitialized,
    Source,
    UnreadableMarker,
    has_marker,
    read_marker,
    write_marker,
)

VALID = {
    "syncplan_marker": 1,
    "created": "2026-10-08T16:40:00+02:00",
    "sources": [{"name": "Photos", "path": "C:\\Photos"}],
}


def put_marker(folder: Path, content: str | bytes) -> Path:
    path = folder / ".syncplan" / "marker.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content.encode() if isinstance(content, str) else content)
    return path


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


def test_has_marker_is_false_without_marker_file(tmp_path):
    (tmp_path / ".syncplan").mkdir()

    assert not has_marker(tmp_path)


def test_has_marker_raises_when_it_cannot_look(tmp_path, monkeypatch):
    # A marker hidden by a permission error must not pass for "no marker" (R2.4, R3.6).
    real_stat = Path.stat

    def denied(self, *args, **kwargs):
        if self.name == "marker.json":
            raise PermissionError(13, "Access is denied", str(self))
        return real_stat(self, *args, **kwargs)

    monkeypatch.setattr(Path, "stat", denied)

    with pytest.raises(PermissionError):
        has_marker(tmp_path)


def test_has_marker_is_true_whatever_the_marker_says(tmp_path):
    put_marker(tmp_path, "damaged")

    assert has_marker(tmp_path)


def test_r1_6_failed_write_removes_the_syncplan_folder_it_created(tmp_path, monkeypatch):
    def full_disk(fd):
        raise OSError(28, "There is not enough space on the disk")

    monkeypatch.setattr(marker.os, "fsync", full_disk)

    with pytest.raises(OSError, match="not enough space"):
        write_marker(tmp_path, [Source("Photos", Path("C:\\Photos"))])

    assert list(tmp_path.iterdir()) == []


def test_r1_6_failed_write_keeps_an_existing_syncplan_folder(tmp_path, monkeypatch):
    make = tmp_path / ".syncplan" / "trash" / "kept.txt"
    make.parent.mkdir(parents=True)
    make.write_text("kept")

    def full_disk(fd):
        raise OSError(28, "There is not enough space on the disk")

    monkeypatch.setattr(marker.os, "fsync", full_disk)

    with pytest.raises(OSError):
        write_marker(tmp_path, [])

    assert sorted(p.name for p in (tmp_path / ".syncplan").iterdir()) == ["trash"]
    assert make.read_text() == "kept"


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
