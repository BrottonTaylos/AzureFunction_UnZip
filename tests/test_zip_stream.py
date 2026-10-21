import io
import zipfile

import pytest

from shared.zip_stream import (
    Limits,
    ZipValidationError,
    open_blob_zip,
    plan_entries,
    sharepoint_relative_path,
)
from tests.fakes import FakeBlobClient


def make_zip(entries, compression=zipfile.ZIP_DEFLATED):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", compression) as zf:
        for name, data in entries:
            zf.writestr(name, data)
    return buf.getvalue()


def test_entries_stream_from_blob_via_range_reads():
    payload = bytes(range(256)) * 40_000  # ~10 MB, incompressible-ish
    blob = FakeBlobClient(make_zip([("a/b.bin", payload), ("c.txt", b"hello")]))

    zf = open_blob_zip(blob, buffer_size=1024 * 1024)
    with zf.open("a/b.bin") as src:
        assert src.read() == payload
    assert zf.read("c.txt") == b"hello"
    assert all(length <= 1024 * 1024 for _, length in blob.range_requests)


@pytest.mark.parametrize("name, expected", [
    ("folder/file.csv", "folder/file.csv"),
    ("folder\\sub\\file.csv", "folder/sub/file.csv"),
    ("./a//b.txt", "a/b.txt"),
    ('bad:na*me?.txt', "bad_na_me_.txt"),
    ("CON.txt", "_CON.txt"),
    ("x_vti_y.txt", "x_vti-y.txt"),
    ("trailing. ", "trailing"),
])
def test_sharepoint_relative_path(name, expected):
    assert sharepoint_relative_path(name) == expected


@pytest.mark.parametrize("name", ["../etc/passwd", "a/../../b", "/abs/file", "C:\\x.txt", "", "./"])
def test_unsafe_paths_rejected(name):
    with pytest.raises(ZipValidationError):
        sharepoint_relative_path(name)


def test_plan_skips_directories_and_records_indexes():
    data = make_zip([("dir/", b""), ("dir/one.txt", b"1"), ("two.txt", b"22")])
    zf = zipfile.ZipFile(io.BytesIO(data))
    plan = plan_entries(zf, Limits())
    assert plan == [
        {"index": 1, "target": "dir/one.txt", "size": 1},
        {"index": 2, "target": "two.txt", "size": 2},
    ]


def test_plan_rejects_traversal_and_collisions_together():
    data = make_zip([("../evil.txt", b"x"), ("Report.csv", b"1"), ("report.csv", b"2")])
    with pytest.raises(ZipValidationError) as exc:
        plan_entries(zipfile.ZipFile(io.BytesIO(data)), Limits())
    message = str(exc.value)
    assert "2 problem(s)" in message
    assert "path traversal" in message
    assert "collides" in message


def test_plan_rejects_zip_bomb_ratio():
    data = make_zip([("zeros.bin", b"\0" * (20 * 1024 * 1024))])
    with pytest.raises(ZipValidationError, match="compression ratio"):
        plan_entries(zipfile.ZipFile(io.BytesIO(data)), Limits(max_ratio=100))


def test_plan_enforces_entry_and_size_limits():
    data = make_zip([("a", b"1234"), ("b", b"5678")])
    with pytest.raises(ZipValidationError, match="exceeds limit"):
        plan_entries(zipfile.ZipFile(io.BytesIO(data)), Limits(max_entries=1))
    with pytest.raises(ZipValidationError, match="total uncompressed size"):
        plan_entries(zipfile.ZipFile(io.BytesIO(data)), Limits(max_total_uncompressed=5))


def test_plan_rejects_unsupported_compression():
    data = bytearray(make_zip([("a.txt", b"hello")], compression=zipfile.ZIP_STORED))
    # Patch the method field in both headers to Deflate64 (9), as Windows uses for big files.
    data[8:10] = (9).to_bytes(2, "little")
    cd = data.rfind(b"PK\x01\x02")
    data[cd + 10:cd + 12] = (9).to_bytes(2, "little")
    with pytest.raises(ZipValidationError, match="Deflate64"):
        plan_entries(zipfile.ZipFile(io.BytesIO(bytes(data))), Limits())


def test_plan_rejects_overlong_paths():
    data = make_zip([("a" * 200 + "/" + "b" * 150, b"x")])
    zf = zipfile.ZipFile(io.BytesIO(data))
    assert plan_entries(zf, Limits())
    with pytest.raises(ZipValidationError, match="longer than"):
        plan_entries(zf, Limits(), target_folder="c" * 100)
