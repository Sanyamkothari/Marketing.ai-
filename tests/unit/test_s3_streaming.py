"""Bounded memory, proved without a large file anywhere on disk.

The claim `S3Storage` makes is that a 40 GiB parquet file costs the same memory as a 40 KiB one:
reads come down in ranges of `READ_CHUNK_BYTES` and writes go up in parts of `PART_BYTES`, and
neither the object nor the mirror is ever whole in memory (DEC-314). A fixture large enough to test
that against a real bucket would be a fixture nobody runs, so the object here is *generated* - a
fake client that invents the bytes of whatever range it is asked for and records the shape of the
request rather than the body - and the claim is checked with `tracemalloc` (DEC-322).

Two things are asserted, and the second is the one that matters:

* the **shape** of the traffic - how many ranged GETs, how many parts, what size each part was,
  that a body at the threshold is one `PutObject` and that a failure aborts the upload;
* the **peak allocation**, which must stay under a fraction of the object's size. A bound that is a
  multiple of this module's own constants is not a measurement of any machine - it is arithmetic on
  the buffers the code declares, which is the only thing a test on a laptop can honestly check
  (plan section 13.3).
"""

from __future__ import annotations

import hashlib
import io
import tracemalloc
from pathlib import Path
from typing import Any

import pytest

from engine.aws.s3_storage import (
    MULTIPART_THRESHOLD_BYTES,
    PART_BYTES,
    READ_BUFFER_BYTES,
    READ_CHUNK_BYTES,
    WRITE_BUFFER_BYTES,
    S3Storage,
)
from engine.storage import StorageError

BUCKET = "marketing-ai-streaming"
KEY = "runs/r1/big.parquet"

BLOCK: bytes = bytes(range(256)) * 256
"""A 64 KiB tile the generated body repeats, so any range is a memcpy and not a Python loop."""

WRITE_CHUNK_BYTES: int = 256 * 1024
"""How much the test hands `write()` at a time. Chosen to be far smaller than a part, so the part
boundary is crossed in the middle of a `write` rather than tidily between two."""

READ_PEAK_CEILING: int = 4 * (READ_CHUNK_BYTES + READ_BUFFER_BYTES)
"""Arithmetic on the reader's own buffers with generous headroom - not a measured figure."""

WRITE_PEAK_CEILING: int = 4 * (PART_BYTES + WRITE_BUFFER_BYTES)
"""Arithmetic on the writer's own buffers with generous headroom - not a measured figure."""


def pattern(start: int, length: int) -> bytes:
    """`length` bytes of the generated object, starting at `start`."""
    offset = start % len(BLOCK)
    tile = BLOCK[offset:] + BLOCK[:offset]
    whole, rest = divmod(length, len(tile))
    return tile * whole + tile[:rest]


class FakeS3:
    """An S3 client that generates the bytes it is asked for and keeps none of them.

    It records what was requested, which is the whole point: the assertions are about the shape of
    the traffic, and a fake that stored the bodies would put the object in memory itself and make
    the `tracemalloc` numbers meaningless.
    """

    def __init__(self, size: int = 0) -> None:
        self.size = size
        self.ranges: list[tuple[int, int]] = []
        self.puts: list[int] = []
        self.parts: list[int] = []
        self.part_numbers: list[int] = []
        self.created = 0
        self.completed = 0
        self.aborted = 0
        self.completed_parts: list[dict[str, Any]] = []

    def head_object(self, **_kwargs: Any) -> dict[str, Any]:
        return {"ContentLength": self.size, "Metadata": {}}

    def get_object(self, **kwargs: Any) -> dict[str, Any]:
        header = kwargs.get("Range")
        if header is None:
            first, last = 0, self.size - 1
        else:
            first_text, last_text = header.removeprefix("bytes=").split("-")
            first, last = int(first_text), int(last_text)
        self.ranges.append((first, last))
        return {"Body": io.BytesIO(pattern(first, last - first + 1)), "Metadata": {}}

    def put_object(self, **kwargs: Any) -> dict[str, Any]:
        self.puts.append(len(kwargs["Body"]))
        return {"ETag": '"single"'}

    def create_multipart_upload(self, **_kwargs: Any) -> dict[str, Any]:
        self.created += 1
        return {"UploadId": "upload-1"}

    def upload_part(self, **kwargs: Any) -> dict[str, Any]:
        self.parts.append(len(kwargs["Body"]))
        self.part_numbers.append(int(kwargs["PartNumber"]))
        return {"ETag": f'"part-{kwargs["PartNumber"]}"'}

    def complete_multipart_upload(self, **kwargs: Any) -> dict[str, Any]:
        self.completed += 1
        self.completed_parts = list(kwargs["MultipartUpload"]["Parts"])
        return {"ETag": '"complete"'}

    def abort_multipart_upload(self, **_kwargs: Any) -> dict[str, Any]:
        self.aborted += 1
        return {}


def store(client: FakeS3, tmp_path: Path) -> S3Storage:
    return S3Storage(BUCKET, prefix="artefacts", client=client, workspace=tmp_path / "mirror")


def write_bytes_to(handle: Any, total: int) -> None:
    """Hand `total` bytes to `handle` in `WRITE_CHUNK_BYTES` pieces, allocating one of them."""
    chunk = bytes(WRITE_CHUNK_BYTES)
    whole, rest = divmod(total, WRITE_CHUNK_BYTES)
    for _ in range(whole):
        handle.write(chunk)
    if rest:
        handle.write(chunk[:rest])


# ---------------------------------------------------------------------------
# Reading
# ---------------------------------------------------------------------------
def test_a_read_comes_down_in_ranges_and_never_holds_the_object(tmp_path: Path) -> None:
    size = 6 * READ_CHUNK_BYTES
    client = FakeS3(size)
    subject = store(client, tmp_path)

    digest = hashlib.sha256()
    read = 0
    tracemalloc.start()
    try:
        with subject.open_read(KEY) as handle:
            while True:
                block = handle.read(READ_BUFFER_BYTES)
                if not block:
                    break
                digest.update(block)
                read += len(block)
        _current, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()

    expected = hashlib.sha256()
    for start in range(0, size, READ_CHUNK_BYTES):
        expected.update(pattern(start, READ_CHUNK_BYTES))

    assert read == size
    assert digest.hexdigest() == expected.hexdigest(), "the ranges must reassemble into the object"
    assert len(client.ranges) == 6, "one ranged GET per chunk, and no re-reads"
    assert all(last - first + 1 <= READ_CHUNK_BYTES for first, last in client.ranges)
    assert peak < size // 2, "a reader that held the object would be near the object's size"
    assert peak < READ_PEAK_CEILING


def test_a_read_is_seekable_and_a_seek_outside_the_window_re_ranges(tmp_path: Path) -> None:
    """`pyarrow.parquet.read_table` reads the footer, then seeks back - on a stream that cannot
    seek it raises `io.UnsupportedOperation: seek`, measured against the installed pyarrow."""
    size = 3 * READ_CHUNK_BYTES
    client = FakeS3(size)

    with store(client, tmp_path).open_read(KEY) as handle:
        assert handle.seekable() is True
        assert handle.seek(0, io.SEEK_END) == size
        assert handle.tell() == size
        assert handle.read(16) == b""

        far = 2 * READ_CHUNK_BYTES + 7
        handle.seek(far)
        assert handle.read(32) == pattern(far, 32)
        handle.seek(11)
        assert handle.read(32) == pattern(11, 32)

    assert len(client.ranges) == 2, "only the two windows a seek actually needed"


def test_a_zero_byte_object_asks_for_no_range_at_all(tmp_path: Path) -> None:
    """A range request for an empty object is not a valid request, so there must not be one."""
    client = FakeS3(0)

    with store(client, tmp_path).open_read(KEY) as handle:
        assert handle.read() == b""

    assert client.ranges == []


# ---------------------------------------------------------------------------
# Writing
# ---------------------------------------------------------------------------
def test_a_body_at_the_threshold_is_a_single_put(tmp_path: Path) -> None:
    client = FakeS3()
    with store(client, tmp_path).open_write(KEY) as handle:
        write_bytes_to(handle, MULTIPART_THRESHOLD_BYTES)

    assert client.puts == [MULTIPART_THRESHOLD_BYTES]
    assert client.created == 0, "exactly at the threshold must not become a one-part upload"
    assert client.parts == []


def test_a_body_above_the_threshold_goes_up_in_parts(tmp_path: Path) -> None:
    client = FakeS3()
    total = 6 * PART_BYTES
    with store(client, tmp_path).open_write(KEY) as handle:
        write_bytes_to(handle, total)

    assert client.puts == []
    assert client.created == 1
    assert client.parts == [PART_BYTES] * 6
    assert sum(client.parts) == total
    assert client.part_numbers == [1, 2, 3, 4, 5, 6], "part numbers are one-based and in order"
    assert client.completed == 1
    assert [part["PartNumber"] for part in client.completed_parts] == [1, 2, 3, 4, 5, 6]
    assert client.aborted == 0


def test_a_remainder_becomes_the_last_part(tmp_path: Path) -> None:
    """Every part but the last is exactly `PART_BYTES`, which is what keeps it above the 5 MiB
    minimum S3 documents; only the final part may be short."""
    client = FakeS3()
    total = 2 * PART_BYTES + WRITE_CHUNK_BYTES
    with store(client, tmp_path).open_write(KEY) as handle:
        write_bytes_to(handle, total)

    assert client.parts == [PART_BYTES, PART_BYTES, WRITE_CHUNK_BYTES]
    assert sum(client.parts) == total


def test_write_memory_is_bounded_by_the_part_and_not_by_the_object(tmp_path: Path) -> None:
    client = FakeS3()
    total = 6 * PART_BYTES
    subject = store(client, tmp_path)

    tracemalloc.start()
    try:
        with subject.open_write(KEY) as handle:
            write_bytes_to(handle, total)
        _current, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()

    assert sum(client.parts) == total
    assert peak < total // 2, "a writer that buffered the body would be near the body's size"
    assert peak < WRITE_PEAK_CEILING


def test_a_failure_mid_multipart_aborts_and_completes_nothing(tmp_path: Path) -> None:
    client = FakeS3()
    total = 2 * PART_BYTES + WRITE_CHUNK_BYTES

    with pytest.raises(RuntimeError), store(client, tmp_path).open_write(KEY) as handle:
        write_bytes_to(handle, total)
        raise RuntimeError("the stage crashed")

    assert client.created == 1
    # Some parts were already on the wire - that is what a multipart upload is - and the tail was
    # still in the buffer, so what S3 holds is a fragment and not the object.
    assert client.parts and sum(client.parts) < total
    assert all(part == PART_BYTES for part in client.parts)
    assert client.completed == 0, "nothing was committed"
    assert client.aborted == 1, "and the upload was not left for a lifecycle rule to find"


def test_a_failure_below_the_threshold_sends_nothing_at_all(tmp_path: Path) -> None:
    client = FakeS3()

    with pytest.raises(RuntimeError), store(client, tmp_path).open_write(KEY) as handle:
        write_bytes_to(handle, WRITE_CHUNK_BYTES)
        raise RuntimeError("the stage crashed")

    assert client.puts == []
    assert client.created == 0
    assert client.aborted == 0, "there was no upload to abort"


def test_a_failed_part_upload_aborts_the_upload(tmp_path: Path) -> None:
    """The commit point is `CompleteMultipartUpload`; anything before it must clean up after itself."""

    class Failing(FakeS3):
        def upload_part(self, **kwargs: Any) -> dict[str, Any]:
            super().upload_part(**kwargs)
            raise _client_error("AccessDenied", 403, "UploadPart")

    client = Failing()
    with pytest.raises(StorageError) as excinfo, store(client, tmp_path).open_write(KEY) as handle:
        write_bytes_to(handle, 2 * PART_BYTES)

    assert excinfo.value.code == "WRITE_FAILED"
    assert client.completed == 0
    assert client.aborted == 1


@pytest.mark.slow
def test_a_much_larger_object_costs_the_same_memory(tmp_path: Path) -> None:
    """The same bound, one order of magnitude up: the point is that the ceiling does not move."""
    client = FakeS3()
    total = 48 * PART_BYTES
    subject = store(client, tmp_path)

    tracemalloc.start()
    try:
        with subject.open_write(KEY) as handle:
            write_bytes_to(handle, total)
        _current, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()

    assert client.parts == [PART_BYTES] * 48
    assert peak < WRITE_PEAK_CEILING, "the ceiling is the part size, whatever the object's size is"


def _client_error(code: str, status: int, operation: str) -> Exception:
    from botocore.exceptions import ClientError

    return ClientError(
        {"Error": {"Code": code, "Message": "not for a log"}, "ResponseMetadata": {"HTTPStatusCode": status}},
        operation,
    )
