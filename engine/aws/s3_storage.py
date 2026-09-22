"""`S3Storage`: the same thirteen-method `Storage` contract, answered by an S3 bucket.

Phase 1 wrote `Storage` as a protocol so that moving the artefacts off a laptop would be a second
implementation rather than a rewrite (plan section 12). This module is that second implementation,
and the whole of it: nothing above it knows which store it holds, and the protocol did not grow a
member to let this one in (DEC-311).

Three things are genuinely different about a bucket, and each is handled here rather than pushed up
to the caller:

**A key is not a path.** `local_path()` is the one escape hatch the protocol has, because AutoGluon
and pyarrow insist on a real directory (DEC-016). Over S3 that path is a *mirror*: reading it
hydrates the tree out of the bucket on first use, and the write direction cannot be inferred,
because a `Path` has no close event - AutoGluon writes the predictor tree through the filesystem and
this object is never called again. So the write direction is explicit, `publish_local_path(key)`,
declared through `SupportsLocalMirror` and dispatched by the free function in `engine/storage.py`
that no-ops on `LocalStorage` (DEC-312). Publishing is content-diffed, so calling it twice uploads
nothing the second time (DEC-313).

**A body does not fit in memory just because a disk would hold it.** `open_read` issues ranged GETs
and is seekable, because `pyarrow.parquet.read_table` seeks and refuses a stream that cannot;
`open_write` buffers to a part and switches from a single `PutObject` to a multipart upload above
the threshold. Memory is bounded by the part size and not by the object (DEC-314).

**A write is not atomic until it is committed.** `LocalStorage` gets atomicity from `os.replace`;
here it comes from S3 itself, because an object appears only when `PutObject` or
`CompleteMultipartUpload` returns. A run that dies mid-write leaves no object, and a run that dies
mid-*rewrite* leaves the previous document readable in full - which is what makes the API's
`status.json` polling honest on either backend (DEC-315).

**`exists()` may raise.** Without `s3:ListBucket` a missing object answers 403, not 404. Reporting
"it does not exist" for "you may not look" is a lie the caller cannot detect and a permissions bug
nobody would ever find, so a 403 raises and only a 404 is `False` (DEC-316).

No boto3 import at module scope: the client is built on first use, so a checkout without the `aws`
extra imports this module's siblings for free (DEC-306).
"""

from __future__ import annotations

import hashlib
import io
import os
import re
import shutil
import tempfile
from collections.abc import Iterator, Mapping
from contextlib import contextmanager, suppress
from pathlib import Path
from typing import TYPE_CHECKING, Any, BinaryIO, Final, TypeVar
from urllib.parse import urlencode

from pydantic import BaseModel

from engine.aws.secrets import quiet_aws_wire_logs
from engine.contracts import dump_artefact
from engine.storage import (
    Storage,
    StorageError,
    SupportsLocalMirror,
    SupportsPresignedDownload,
    validate_key,
    validate_prefix,
)
from engine.utils.logging import get_logger, log_failure

if TYPE_CHECKING:
    from _typeshed import ReadableBuffer, WriteableBuffer
    from types_boto3_s3.client import S3Client
    from types_boto3_s3.type_defs import CompletedPartTypeDef

__all__ = [
    "DIGEST_METADATA_KEY",
    "MAX_PARTS",
    "MAX_S3_KEY_BYTES",
    "MULTIPART_THRESHOLD_BYTES",
    "PART_BYTES",
    "READ_CHUNK_BYTES",
    "S3Storage",
]

M = TypeVar("M", bound=BaseModel)

_LOGGER = get_logger(__name__)

MAX_S3_KEY_BYTES: Final[int] = 1024
"""S3's documented maximum object key length, in UTF-8 **bytes**.

Not chosen: this is the service limit. It is checked separately from `validate_key`'s 512
*characters* because the two count different things - one non-ASCII character is up to four bytes,
so a key this product considers valid can still be a key S3 will not accept, and the caller
deserves a `KEY_INVALID` here rather than a signature error from the wire (DEC-317).
"""

PART_BYTES: Final[int] = 8 * 1024 * 1024
"""Bytes per multipart part. **Chosen, not measured.**

It sits above S3's documented 5 MiB minimum for every part but the last, with room to spare so a
change of mind does not silently cross it, and `PART_BYTES * MAX_PARTS` is a ceiling of 78 GiB per
object - far above any artefact this product writes. Nobody here has measured the throughput of
this or any other part size against a real bucket, and no number in this repository claims it has
(plan section 13.3).
"""

MULTIPART_THRESHOLD_BYTES: Final[int] = PART_BYTES
"""Bodies of at most this many bytes go up as a single `PutObject`. **Chosen, not measured.**

Equal to the part size because that is the smallest threshold that cannot produce a multipart upload
of one part: below it, one request; above it, parts of exactly `PART_BYTES` and a remainder.
"""

MAX_PARTS: Final[int] = 10_000
"""S3's documented maximum number of parts in one multipart upload. Not chosen: the service limit."""

READ_CHUNK_BYTES: Final[int] = 4 * 1024 * 1024
"""Bytes per ranged GET while streaming a read. **Chosen, not measured.**

Large enough that a sequential read of a parquet file is not one request per row group, small
enough that the reader's memory is a constant a reviewer can see rather than a function of the
object's size. No benchmark backs this figure.
"""

READ_BUFFER_BYTES: Final[int] = 1024 * 1024
"""`io.BufferedReader` buffer over the ranged reader. **Chosen, not measured.**"""

WRITE_BUFFER_BYTES: Final[int] = 1024 * 1024
"""`io.BufferedWriter` buffer over the part writer. **Chosen, not measured.**"""

COPY_CHUNK_BYTES: Final[int] = 1024 * 1024
"""Bytes per `copyfileobj` hop when a mirror file is hashed or moved. **Chosen, not measured.**"""

CONNECT_TIMEOUT_SECONDS: Final[int] = 10
"""Socket connect timeout for the S3 client. **Chosen, not measured.**

A connect that takes longer than this is a network or endpoint problem, not a slow bucket, and the
run is better off failing than hanging on a request thread.
"""

READ_TIMEOUT_SECONDS: Final[int] = 60
"""Socket read timeout for one S3 request. **Chosen, not measured.**

It bounds a single ranged GET or one part upload, never a whole object, because every transfer in
this module is chunked. No measurement of real transfer times informs it.
"""

MAX_ATTEMPTS: Final[int] = 5
"""botocore attempts per request, its adaptive mode included. **Chosen, not measured.**"""

DEFAULT_PAGE_SIZE: Final[int] = 1000
"""`ListObjectsV2` keys per page, and S3's own default. Not chosen: it is the service default."""

DIGEST_METADATA_KEY: Final[str] = "content-sha256"
"""User metadata holding the hex SHA-256 of the body, which is how `publish_local_path` diffs.

Not an ETag: an ETag is the MD5 of the body only for an unencrypted single-part upload, so it is
neither stable across the multipart boundary nor available at all under SSE-KMS. A digest this
module writes itself is true in every one of those cases (DEC-313).
"""

_TAG_VALUE: Final[re.Pattern[str]] = re.compile(r"^[\w\s+\-=.:/@]{1,256}$")
"""S3's documented tag-value character set. A value outside it is dropped rather than sent."""

_RUN_KEY: Final[re.Pattern[str]] = re.compile(r"^runs/([^/]+)/")
"""`runs/<run_id>/…`, the one key layout that carries a run id (`engine.storage.run_key`)."""

_NO_BUCKET_CODES: Final[frozenset[str]] = frozenset({"NoSuchBucket"})
"""Checked before the 404 codes: a missing bucket answers 404 but is a misconfiguration, not a
missing artefact, and telling a caller its key is absent would send it looking in the wrong place."""

_NOT_FOUND_CODES: Final[frozenset[str]] = frozenset({"NoSuchKey", "NotFound", "404"})
"""`GetObject` answers `NoSuchKey`, `HeadObject` answers a bare `404` with no body to name it."""

_DENIED_CODES: Final[frozenset[str]] = frozenset(
    {
        "AccessDenied",
        "AccessDeniedException",
        "AllAccessDisabled",
        "InvalidAccessKeyId",
        "SignatureDoesNotMatch",
        "ExpiredToken",
        "InvalidToken",
        "KMS.AccessDeniedException",
        "KMSAccessDeniedException",
        "403",
    }
)
"""Everything that means "this identity may not do that", including the KMS grant failures that
SSE-KMS adds to a bucket which would otherwise have answered."""


class S3Storage:
    """`Storage` over one S3 bucket, optionally under `prefix`.

    `prefix` is `""` for a bucket-per-environment deployment, which is what `api/deps.py` passes;
    it is not dead code, because a shared bucket and every test in this repository use one, and
    because the key arithmetic has to be right in both cases or `list_keys` silently returns a
    neighbouring prefix's keys.

    `client` is for tests and for a caller that has already built a configured client; left `None`,
    one is built on first use, which is also when boto3 is imported (DEC-306).
    """

    def __init__(
        self,
        bucket: str,
        *,
        prefix: str = "",
        region_name: str | None = None,
        kms_key_id: str | None = None,
        client_id: str | None = None,
        workspace: Path | None = None,
        client: S3Client | None = None,
        page_size: int = DEFAULT_PAGE_SIZE,
    ) -> None:
        self._bucket = bucket
        self._prefix = prefix.strip("/")
        self._root = f"{self._prefix}/" if self._prefix else ""
        self._region_name = region_name
        self._kms_key_id = kms_key_id
        self._client_id = client_id
        self._workspace = None if workspace is None else Path(workspace)
        self._s3 = client
        self._page_size = max(1, page_size)
        self._mirror: Path | None = None
        self._hydrated: set[str] = set()
        self._known: dict[str, tuple[int, str]] = {}

    # -- identity ----------------------------------------------------------------
    @property
    def bucket(self) -> str:
        """The bucket every key is resolved in."""
        return self._bucket

    @property
    def prefix(self) -> str:
        """The key prefix this store lives under, without slashes, `""` for the bucket root."""
        return self._prefix

    def __repr__(self) -> str:
        """`S3Storage(bucket, prefix)`; never the credentials, which it does not hold anyway."""
        return f"{type(self).__name__}(bucket={self._bucket!r}, prefix={self._prefix!r})"

    # -- the client and the key arithmetic ---------------------------------------
    def _client(self) -> S3Client:
        """The boto3 client, built and cached on first use.

        `quiet_aws_wire_logs()` runs first: botocore at DEBUG prints signed URLs, which are bearer
        credentials, and turning the engine up to DEBUG must not turn that on by accident (DEC-309).
        """
        if self._s3 is None:
            import boto3  # a deliberate local import: boto3 is optional (DEC-306)
            from botocore.config import Config  # same reason: botocore is optional too

            quiet_aws_wire_logs()
            self._s3 = boto3.client(
                "s3",
                region_name=self._region_name,
                config=Config(
                    connect_timeout=CONNECT_TIMEOUT_SECONDS,
                    read_timeout=READ_TIMEOUT_SECONDS,
                    retries={"max_attempts": MAX_ATTEMPTS, "mode": "adaptive"},
                ),
            )
        return self._s3

    def _object_key(self, key: str) -> str:
        """`<prefix>/<key>`, with both key gates applied: 512 characters, then 1024 bytes."""
        object_key = self._root + validate_key(key)
        encoded = len(object_key.encode("utf-8"))
        if encoded > MAX_S3_KEY_BYTES:
            raise StorageError(
                "KEY_INVALID",
                f"An S3 object key must be at most {MAX_S3_KEY_BYTES} bytes, "
                f"this one is {encoded} once the bucket prefix is added: {key!r}.",
                key=key,
            )
        return object_key

    def _relative(self, object_key: str) -> str | None:
        """`object_key` as a store key, or `None` when it belongs to a neighbouring prefix."""
        if self._root and not object_key.startswith(self._root):
            return None
        return object_key[len(self._root) :]

    # -- write extras: tagging and encryption ------------------------------------
    def _extra_args(self, key: str) -> dict[str, Any]:
        """Tagging and encryption for one write.

        The run id is read out of the key rather than passed in, because `Storage` has no parameter
        for it and inventing one would change the protocol for every implementation. `runs/<id>/…`
        is a layout `engine.storage.run_key` owns, so reading it back is not guesswork (DEC-318).
        SSE-KMS is requested only when a key id was configured; with none, the bucket's own default
        encryption applies and this module does not second-guess it.
        """
        extra: dict[str, Any] = {}
        tags = self._tags_for(key)
        if tags:
            extra["Tagging"] = urlencode(tags)
        if self._kms_key_id:
            extra["ServerSideEncryption"] = "aws:kms"
            extra["SSEKMSKeyId"] = self._kms_key_id
        return extra

    def _tags_for(self, key: str) -> dict[str, str]:
        """`run_id` when the key names a run, `client_id` when this deployment serves one."""
        tags: dict[str, str] = {}
        match = _RUN_KEY.match(key)
        if match is not None:
            tags["run_id"] = match.group(1)
        if self._client_id:
            tags["client_id"] = self._client_id
        return {name: value for name, value in tags.items() if _TAG_VALUE.match(value)}

    # -- Storage: whole values ---------------------------------------------------
    def write_bytes(self, key: str, data: bytes) -> None:
        """Write `data` as the whole body of `key`."""
        with self.open_write(key) as handle:
            handle.write(data)

    def read_bytes(self, key: str) -> bytes:
        """The whole body of `key`.

        Whole, because the protocol says so; a caller that cannot afford the body in memory uses
        `open_read`, which is why that method exists.
        """
        object_key = self._object_key(key)
        try:
            response = self._client().get_object(Bucket=self._bucket, Key=object_key)
            body: bytes = response["Body"].read()
        except Exception as exc:  # mapped below; the botocore exception types are an optional import
            raise _s3_error(exc, key, writing=False) from exc
        return body

    def write_text(self, key: str, text: str) -> None:
        """Write `text` as UTF-8."""
        self.write_bytes(key, text.encode("utf-8"))

    def read_text(self, key: str) -> str:
        """The body of `key`, decoded as UTF-8."""
        return self.read_bytes(key).decode("utf-8")

    def write_model(self, key: str, model: BaseModel) -> None:
        """Write `model` in the one artefact JSON shape (`engine.contracts.dump_artefact`)."""
        self.write_text(key, dump_artefact(model))

    def read_model(self, key: str, model_type: type[M]) -> M:
        """Parse the body of `key` as `model_type`."""
        return model_type.model_validate_json(self.read_bytes(key))

    # -- Storage: metadata -------------------------------------------------------
    def exists(self, key: str) -> bool:
        """Whether `key` holds an object. **Raises on 403** (DEC-316).

        Without `s3:ListBucket` on the bucket, S3 answers a `HeadObject` for a key that is not there
        with 403 rather than 404, so "absent" and "forbidden" arrive as different codes for the same
        question. Returning `False` for the second would turn a permissions bug into a missing
        artefact - a run that quietly re-does work, or an API that reports a run has no results -
        and nothing downstream could tell the difference. So only 404 is `False`.
        """
        object_key = self._object_key(key)
        try:
            self._client().head_object(Bucket=self._bucket, Key=object_key)
        except Exception as exc:  # mapped below; the botocore exception types are an optional import
            if _is_absent(exc):
                return False
            raise _s3_error(exc, key, writing=False) from exc
        return True

    def size_bytes(self, key: str) -> int:
        """The body length of `key`, or `KEY_NOT_FOUND`."""
        head = self._head(key)
        if head is None:
            raise StorageError("KEY_NOT_FOUND", f"No stored object for key {key!r}.", key=key)
        return head[0]

    def list_keys(self, prefix: str = "") -> tuple[str, ...]:
        """Every key under `prefix`, relative to this store's root and sorted - as `LocalStorage`.

        Sorted here rather than trusted from the service: `ListObjectsV2` returns keys in UTF-8
        binary order, which is not Python's order for every string, and two implementations of one
        protocol that disagree about ordering would be a bug nobody finds until a diff is compared
        (DEC-323).
        Keys ending in "/" are the directory markers a console or a sync tool creates; they are not
        objects this product wrote and `LocalStorage` has no equivalent, so they are excluded.
        """
        validate_prefix(prefix)
        client = self._client()
        request: dict[str, Any] = {
            "Bucket": self._bucket,
            "Prefix": self._root + prefix,
            "MaxKeys": self._page_size,
        }
        found: list[str] = []
        while True:
            try:
                response = client.list_objects_v2(**request)
            except Exception as exc:  # mapped below; botocore's exception types are an optional import
                raise _s3_error(exc, prefix, writing=False) from exc
            for item in response.get("Contents", ()):
                object_key = str(item.get("Key", ""))
                if not object_key or object_key.endswith("/"):
                    continue
                relative = self._relative(object_key)
                if relative is not None and relative.startswith(prefix):
                    found.append(relative)
            token = response.get("NextContinuationToken")
            if not response.get("IsTruncated") or not token:
                break
            request["ContinuationToken"] = token
        return tuple(sorted(found))

    def delete(self, key: str) -> None:
        """Remove `key`; deleting what is not there is not an error, as on the local store."""
        object_key = self._object_key(key)
        try:
            self._client().delete_object(Bucket=self._bucket, Key=object_key)
        except Exception as exc:  # mapped below; the botocore exception types are an optional import
            raise _s3_error(exc, key, writing=True) from exc
        self._known.pop(key, None)

    def _head(self, key: str) -> tuple[int, str] | None:
        """`(length, stored digest)` for `key`, or `None` when it is absent. Raises on 403."""
        object_key = self._object_key(key)
        try:
            response = self._client().head_object(Bucket=self._bucket, Key=object_key)
        except Exception as exc:  # mapped below; the botocore exception types are an optional import
            if _is_absent(exc):
                return None
            raise _s3_error(exc, key, writing=False) from exc
        metadata = response.get("Metadata") or {}
        return int(response.get("ContentLength", 0)), str(metadata.get(DIGEST_METADATA_KEY, ""))

    # -- Storage: streams --------------------------------------------------------
    @contextmanager
    def open_read(self, key: str) -> Iterator[BinaryIO]:
        """A **seekable** stream over `key`, fetched in ranges of `READ_CHUNK_BYTES`.

        Seekable is not a nicety: `pyarrow.parquet.read_table` seeks, and on a stream that cannot it
        raises `io.UnsupportedOperation: seek` - measured in this repository's environment against
        the installed pyarrow. A `StreamingBody` straight off `GetObject` is not seekable, so the
        raw stream here re-issues a ranged GET on a seek instead (DEC-314).
        """
        head = self._head(key)
        if head is None:
            raise StorageError("KEY_NOT_FOUND", f"No stored object for key {key!r}.", key=key)
        raw = _RangedReader(self._client(), self._bucket, self._object_key(key), key, head[0])
        handle = io.BufferedReader(raw, buffer_size=READ_BUFFER_BYTES)
        try:
            yield handle
        finally:
            handle.close()

    @contextmanager
    def open_write(self, key: str) -> Iterator[BinaryIO]:
        """A stream that becomes `key` when the block exits normally, and nothing when it does not.

        One `PutObject` at or below `MULTIPART_THRESHOLD_BYTES`; above it, a multipart upload whose
        commit point is `CompleteMultipartUpload` and whose failure path is
        `AbortMultipartUpload`, so a crash leaves neither a half-written object nor a part nobody
        will ever pay attention to again (DEC-315).
        """
        with self._open_write(key, metadata=None) as handle:
            yield handle

    @contextmanager
    def _open_write(self, key: str, *, metadata: Mapping[str, str] | None) -> Iterator[BinaryIO]:
        """`open_write`, plus the user metadata a mirror publish attaches (`DIGEST_METADATA_KEY`)."""
        writer = _PartWriter(
            client=self._client(),
            bucket=self._bucket,
            object_key=self._object_key(key),
            key=key,
            extra=self._extra_args(key),
            metadata=dict(metadata or {}),
        )
        handle = io.BufferedWriter(writer, buffer_size=WRITE_BUFFER_BYTES)
        try:
            yield handle
            handle.flush()
            writer.commit()
        except BaseException:
            writer.abort()
            raise
        finally:
            # The writer has finished either way, so its `write` is a no-op and closing the buffer
            # cannot resurrect a body that was aborted.
            handle.close()

    # -- Storage: the local escape hatch -----------------------------------------
    def local_path(self, key: str) -> Path:
        """A real path for `key`, hydrated out of the bucket on first use.

        The path is inside this store's mirror, not the bucket, and - exactly as `LocalStorage`
        does - it need not exist: a key nothing was ever written under yields a path that is simply
        not there, which is what lets a caller test `local_path(k).exists()` on either backend.
        Reading is transparent; writing is not, and `publish_local_path` is why (DEC-312).
        """
        validate_key(key)
        self._object_key(key)
        self._hydrate(key)
        return self._mirror_root() / key

    def publish_local_path(self, key: str) -> tuple[str, ...]:
        """Upload whatever is under `local_path(key)`; the keys written, which may be `()`.

        Content-diffed against the stored SHA-256, so the second call in a row writes nothing and
        costs one `HeadObject` per file. That matters because the train stage calls this on a
        predictor directory which the score stage may already have mirrored (DEC-313).
        """
        validate_key(key)
        return self._publish(self._mirror_root() / key, key)

    def flush_local(self, prefix: str = "") -> tuple[str, ...]:
        """Publish everything under `prefix` the mirror holds and the store does not."""
        validate_prefix(prefix)
        root = self._mirror_root()
        written: list[str] = []
        for path, stored in _mirror_files(root, prefix):
            if self._publish_file(path, stored):
                written.append(stored)
        return tuple(written)

    def release_local(self, prefix: str = "") -> None:
        """Flush `prefix`, then reclaim the disk it used. Safe to call twice.

        A predictor directory is the largest thing a run puts on local disk, and on a task with a
        fixed volume the next run needs that space back. Flushing first means releasing can never
        be the thing that loses an artefact; forgetting the hydration marks means the next
        `local_path` fetches the tree again rather than handing back an empty directory.
        """
        self.flush_local(prefix)
        root = self._mirror_root()
        for path, _stored in _mirror_files(root, prefix):
            path.unlink(missing_ok=True)
        _prune_empty_dirs(root)
        self._hydrated = {
            mark for mark in self._hydrated if not (mark.startswith(prefix) or prefix.startswith(mark))
        }

    def _mirror_root(self) -> Path:
        """The directory `local_path` resolves against, created on first use."""
        if self._mirror is None:
            self._mirror = (
                self._workspace
                if self._workspace is not None
                else Path(tempfile.mkdtemp(prefix="marketing-ai-s3-"))
            )
        self._mirror.mkdir(parents=True, exist_ok=True)
        return self._mirror

    def _hydrate(self, key: str) -> None:
        """Materialise `key` - an object or a whole tree - into the mirror, once per instance."""
        if any(key == mark or key.startswith(f"{mark}/") for mark in self._hydrated):
            return
        root = self._mirror_root()
        object_key = self._object_key(key)
        for stored in self._stored_under(key):
            destination = root / stored
            destination.parent.mkdir(parents=True, exist_ok=True)
            self._download(self._root + stored, stored, destination)
        _LOGGER.info("s3 hydrate bucket=%s key=%s", self._bucket, object_key)
        self._hydrated.add(key)

    def _stored_under(self, key: str) -> tuple[str, ...]:
        """`key` itself when it is an object, plus every key under `key/`.

        The `key/` test is what keeps `runs/r1` from dragging down `runs/r10`: a raw prefix match
        would treat one run's directory as part of another's.
        """
        return tuple(
            candidate
            for candidate in self.list_keys(key)
            if candidate == key or candidate.startswith(f"{key}/")
        )

    def _download(self, object_key: str, key: str, destination: Path) -> None:
        """Stream one object onto local disk in `COPY_CHUNK_BYTES` hops."""
        try:
            response = self._client().get_object(Bucket=self._bucket, Key=object_key)
            with destination.open("wb") as sink:
                shutil.copyfileobj(response["Body"], sink, COPY_CHUNK_BYTES)
        except Exception as exc:  # mapped below; the botocore exception types are an optional import
            raise _s3_error(exc, key, writing=False) from exc
        metadata = response.get("Metadata") or {}
        digest = str(metadata.get(DIGEST_METADATA_KEY, ""))
        if digest:
            self._known[key] = (destination.stat().st_size, digest)

    def _publish(self, path: Path, key: str) -> tuple[str, ...]:
        """Publish one mirror file, or every file under one mirror directory."""
        if path.is_file():
            return (key,) if self._publish_file(path, key) else ()
        if not path.is_dir():
            return ()
        written: list[str] = []
        for child in sorted(path.rglob("*")):
            if not child.is_file():
                continue
            stored = f"{key}/{child.relative_to(path).as_posix()}"
            if self._publish_file(child, stored):
                written.append(stored)
        return tuple(written)

    def _publish_file(self, path: Path, key: str) -> bool:
        """Upload `path` as `key` unless the store already holds exactly those bytes."""
        validate_key(key)
        size = path.stat().st_size
        known = self._known.get(key)
        digest: str | None = None
        if known is not None and known[0] == size:
            digest = _digest(path)
            if digest == known[1]:
                return False
        else:
            head = self._head(key)
            if head is not None and head[0] == size and head[1]:
                digest = _digest(path)
                if digest == head[1]:
                    self._known[key] = (size, head[1])
                    return False
        digest = digest if digest is not None else _digest(path)
        with self._open_write(key, metadata={DIGEST_METADATA_KEY: digest}) as sink, path.open("rb") as source:
            shutil.copyfileobj(source, sink, COPY_CHUNK_BYTES)
        self._known[key] = (size, digest)
        _LOGGER.info("s3 publish bucket=%s key=%s bytes=%d", self._bucket, self._root + key, size)
        return True

    # -- SupportsPresignedDownload -----------------------------------------------
    def presigned_download_url(self, key: str, *, expires_in: int, filename: str | None = None) -> str:
        """A time-limited URL a browser can fetch `key` with directly.

        The lifetime is the caller's: `engine/settings.py` owns `download_url_ttl_seconds` and there
        is deliberately no second default here, because two defaults for one policy is how they come
        to disagree (DEC-319). The URL is a bearer credential for as long as it lives, which is why
        `quiet_aws_wire_logs()` exists and why nothing in this module logs one.
        """
        object_key = self._object_key(key)
        if expires_in <= 0:
            raise StorageError(
                "READ_FAILED", f"A pre-signed URL needs a positive lifetime for key {key!r}.", key=key
            )
        params: dict[str, Any] = {"Bucket": self._bucket, "Key": object_key}
        if filename:
            params["ResponseContentDisposition"] = f'attachment; filename="{_safe_filename(filename)}"'
        try:
            url: str = self._client().generate_presigned_url(
                "get_object", Params=params, ExpiresIn=expires_in
            )
        except Exception as exc:  # mapped below; the botocore exception types are an optional import
            raise _s3_error(exc, key, writing=False) from exc
        return url


class _RangedReader(io.RawIOBase):
    """A seekable read stream over one object, filled by ranged GETs.

    Holds at most one `READ_CHUNK_BYTES` window, so a 40 GiB parquet file costs the same memory as
    a 40 KiB one. A seek inside the current window costs nothing; a seek outside it costs the next
    GET, which is exactly what a parquet footer read then a row-group read does.
    """

    def __init__(self, client: S3Client, bucket: str, object_key: str, key: str, size: int) -> None:
        super().__init__()
        self._s3 = client
        self._bucket = bucket
        self._object_key = object_key
        self._key = key
        self._size = size
        self._position = 0
        self._window = b""
        self._window_start = 0

    def readable(self) -> bool:
        """Yes; this is a read stream."""
        return True

    def seekable(self) -> bool:
        """Yes - and the reason this class exists rather than a `StreamingBody`."""
        return True

    def tell(self) -> int:
        """The current offset in the object."""
        return self._position

    def seek(self, offset: int, whence: int = os.SEEK_SET) -> int:
        """Move to `offset`; the object's length is already known, so `SEEK_END` needs no request."""
        if whence == os.SEEK_SET:
            target = offset
        elif whence == os.SEEK_CUR:
            target = self._position + offset
        elif whence == os.SEEK_END:
            target = self._size + offset
        else:
            raise ValueError(f"Unsupported whence value: {whence!r}.")
        if target < 0:
            raise ValueError("A negative offset is not a position in an object.")
        self._position = target
        return target

    def readinto(self, buffer: WriteableBuffer) -> int:
        """Fill `buffer` from the current window, fetching the next range when it runs out."""
        view = memoryview(buffer).cast("B")
        if len(view) == 0 or self._position >= self._size:
            return 0
        if not self._window_holds(self._position):
            self._fetch(self._position)
        start = self._position - self._window_start
        taken = min(len(view), len(self._window) - start)
        view[:taken] = self._window[start : start + taken]
        self._position += taken
        return taken

    def _window_holds(self, position: int) -> bool:
        return self._window_start <= position < self._window_start + len(self._window)

    def _fetch(self, position: int) -> None:
        """One ranged GET starting at `position`, at most `READ_CHUNK_BYTES` long."""
        last = min(position + READ_CHUNK_BYTES, self._size) - 1
        try:
            response = self._s3.get_object(
                Bucket=self._bucket, Key=self._object_key, Range=f"bytes={position}-{last}"
            )
            self._window = response["Body"].read()
        except Exception as exc:  # mapped below; the botocore exception types are an optional import
            raise _s3_error(exc, self._key, writing=False) from exc
        self._window_start = position


class _PartWriter(io.RawIOBase):
    """A write stream that becomes an object only when `commit()` is called.

    Below the threshold that is one `PutObject` of the buffered body. Above it, the buffer is drained
    in parts of exactly `PART_BYTES` as it fills, so the peak is the part plus whatever the caller
    handed over last - never the object. `abort()` is the failure path, and it is what keeps a
    crashed run from leaving parts in the bucket for a lifecycle rule to find weeks later.
    """

    def __init__(
        self,
        *,
        client: S3Client,
        bucket: str,
        object_key: str,
        key: str,
        extra: Mapping[str, Any],
        metadata: Mapping[str, str],
    ) -> None:
        super().__init__()
        self._s3 = client
        self._bucket = bucket
        self._object_key = object_key
        self._key = key
        self._extra = dict(extra)
        self._metadata = dict(metadata)
        self._buffer = bytearray()
        self._upload_id: str | None = None
        self._parts: list[CompletedPartTypeDef] = []
        self._finished = False

    def writable(self) -> bool:
        """Yes; this is a write stream."""
        return True

    def write(self, b: ReadableBuffer) -> int:
        """Buffer `b`, draining a whole part whenever more than one part is held.

        Strictly more than, so a body of exactly `PART_BYTES` stays a single `PutObject` rather than
        becoming a one-part multipart upload.
        """
        view = memoryview(b).cast("B")
        if self._finished:
            return len(view)
        self._buffer.extend(view)
        while len(self._buffer) > PART_BYTES:
            self._upload_part(PART_BYTES)
        return len(view)

    def commit(self) -> None:
        """Make the buffered body the object. The one place an object comes into existence."""
        if self._finished:
            return
        try:
            if self._upload_id is None:
                self._put()
            else:
                if self._buffer:
                    self._upload_part(len(self._buffer))
                self._complete()
        except BaseException:
            self.abort()
            raise
        self._finished = True
        self._buffer.clear()

    def abort(self) -> None:
        """Throw the body away, and the multipart upload with it if one was started.

        A failed abort is logged by class name and swallowed: the caller is already unwinding a
        failure it cares about more, and replacing that exception with this one would hide it.
        """
        if self._finished:
            return
        self._finished = True
        self._buffer.clear()
        if self._upload_id is not None:
            with suppress(Exception):
                self._s3.abort_multipart_upload(
                    Bucket=self._bucket, Key=self._object_key, UploadId=self._upload_id
                )
                self._upload_id = None
            if self._upload_id is not None:
                _LOGGER.warning("s3 abort bucket=%s parts=%d incomplete", self._bucket, len(self._parts))

    def _put(self) -> None:
        """One `PutObject` for a body at or below the multipart threshold."""
        body = bytes(self._buffer)
        metadata = dict(self._metadata)
        metadata.setdefault(DIGEST_METADATA_KEY, hashlib.sha256(body).hexdigest())
        try:
            self._s3.put_object(
                Bucket=self._bucket, Key=self._object_key, Body=body, Metadata=metadata, **self._extra
            )
        except Exception as exc:  # mapped below; the botocore exception types are an optional import
            raise _s3_error(exc, self._key, writing=True) from exc

    def _start(self) -> str:
        """Begin a multipart upload; metadata and tagging can only be set here, never at completion."""
        if self._upload_id is None:
            request: dict[str, Any] = dict(self._extra)
            if self._metadata:
                request["Metadata"] = dict(self._metadata)
            try:
                response = self._s3.create_multipart_upload(
                    Bucket=self._bucket, Key=self._object_key, **request
                )
            except Exception as exc:  # mapped below; botocore's exception types are an optional import
                raise _s3_error(exc, self._key, writing=True) from exc
            self._upload_id = str(response["UploadId"])
        return self._upload_id

    def _upload_part(self, length: int) -> None:
        """Send the first `length` buffered bytes as the next part."""
        upload_id = self._start()
        if len(self._parts) >= MAX_PARTS:
            raise StorageError(
                "WRITE_FAILED",
                f"{self._key!r} would need more than {MAX_PARTS} parts of {PART_BYTES} bytes; "
                "that is larger than this product writes.",
                key=self._key,
            )
        # One copy, not two: slicing the bytearray first would build a second buffer of the same
        # size before `bytes()` copied it again, which would double the peak this class exists to
        # keep flat. The view is released explicitly because a bytearray cannot be resized while
        # a memoryview onto it is exported.
        view = memoryview(self._buffer)
        payload = bytes(view[:length])
        view.release()
        del self._buffer[:length]
        number = len(self._parts) + 1
        try:
            response = self._s3.upload_part(
                Bucket=self._bucket,
                Key=self._object_key,
                UploadId=upload_id,
                PartNumber=number,
                Body=payload,
            )
        except Exception as exc:  # mapped below; the botocore exception types are an optional import
            raise _s3_error(exc, self._key, writing=True) from exc
        self._parts.append({"ETag": str(response["ETag"]), "PartNumber": number})

    def _complete(self) -> None:
        """`CompleteMultipartUpload`: the commit point for a body that went up in parts."""
        try:
            self._s3.complete_multipart_upload(
                Bucket=self._bucket,
                Key=self._object_key,
                UploadId=str(self._upload_id),
                MultipartUpload={"Parts": self._parts},
            )
        except Exception as exc:  # mapped below; the botocore exception types are an optional import
            raise _s3_error(exc, self._key, writing=True) from exc


def _mirror_files(root: Path, prefix: str) -> tuple[tuple[Path, str], ...]:
    """Every file in the mirror whose store key starts with `prefix`, sorted by key.

    The whole mirror is walked and then filtered by key rather than descending into
    `root / prefix`, because a `list_keys` prefix is a string and need not end at a directory
    boundary - `runs/r1` selects `runs/r10` too, on both backends or on neither.
    """
    if not root.is_dir():
        return ()
    out: list[tuple[Path, str]] = []
    for path in sorted(root.rglob("*")):
        if not path.is_file():
            continue
        key = path.relative_to(root).as_posix()
        if key.startswith(prefix):
            out.append((path, key))
    return tuple(out)


def _prune_empty_dirs(root: Path) -> None:
    """Remove the directories a release emptied, deepest first; `root` itself stays."""
    for path in sorted((p for p in root.rglob("*") if p.is_dir()), key=lambda p: len(p.parts), reverse=True):
        with suppress(OSError):
            path.rmdir()


def _digest(path: Path) -> str:
    """The hex SHA-256 of a file, read in `COPY_CHUNK_BYTES` hops so a predictor fits in memory."""
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(COPY_CHUNK_BYTES), b""):
            digest.update(block)
    return digest.hexdigest()


def _safe_filename(filename: str) -> str:
    """A filename fit for a `Content-Disposition` header: no quote, no backslash, no newline."""
    return re.sub(r'[\r\n"\\]', "_", filename).strip() or "download"


def _aws_error(exc: BaseException) -> tuple[str, int]:
    """`(error code, HTTP status)` out of a botocore `ClientError`, or `("", 0)` for anything else.

    Read structurally rather than by catching `ClientError`, because importing botocore at module
    scope is the thing this package does not do (DEC-306).
    """
    response = getattr(exc, "response", None)
    if not isinstance(response, Mapping):
        return "", 0
    error = response.get("Error")
    code = str(error.get("Code", "")) if isinstance(error, Mapping) else ""
    metadata = response.get("ResponseMetadata")
    status = int(metadata.get("HTTPStatusCode", 0) or 0) if isinstance(metadata, Mapping) else 0
    return code, status


def _is_absent(exc: BaseException) -> bool:
    """Whether `exc` means "no such object" - and specifically not "no such bucket"."""
    code, status = _aws_error(exc)
    if code in _NO_BUCKET_CODES:
        return False
    return code in _NOT_FOUND_CODES or status == 404


def _s3_error(exc: BaseException, key: str, *, writing: bool) -> StorageError:
    """One AWS failure as a `StorageError`, named by code and never by message.

    The mapping is exhaustive on purpose, because the interesting cases are the ones a wrong guess
    would make invisible (DEC-320):

    * `NoSuchBucket` - checked first, because it arrives as a 404 and is *not* a missing artefact.
      The bucket is configuration; saying `KEY_NOT_FOUND` would send a reader looking for a run.
    * `NoSuchKey` / `NotFound` / a bare 404 - `KEY_NOT_FOUND` on a read, which is the code
      `LocalStorage` raises for the same question. On a write the same status means the bucket or
      the multipart upload has gone, so it is `WRITE_FAILED`: nothing was written and the key is not
      the problem.
    * `AccessDenied` and the credential and KMS-grant failures beside it - `READ_FAILED` on a read
      and `WRITE_FAILED` on a write. Never `KEY_NOT_FOUND`: a permissions problem that presents as
      an absent artefact is a bug that hides for as long as nobody counts the artefacts.

    The exception's message is never quoted. A botocore message carries the key and can carry a
    request's query string, and a log line is not where either belongs (plan section 13.7).
    """
    code, status = _aws_error(exc)
    failure = "WRITE_FAILED" if writing else "READ_FAILED"
    named = f"{type(exc).__name__}{f' ({code})' if code else ''}"
    if code in _NO_BUCKET_CODES:
        return StorageError(
            failure,
            f"The artefact bucket does not exist, so {key!r} could not be reached. "
            "That is a deployment setting, not a missing artefact.",
            key=key,
        )
    if code in _DENIED_CODES or status == 403:
        return StorageError(
            failure,
            f"This role may not {'write' if writing else 'read'} {key!r} ({named}). "
            "Grant the permission and retry; nothing was changed.",
            key=key,
        )
    if code in _NOT_FOUND_CODES or status == 404:
        if writing:
            return StorageError(
                "WRITE_FAILED",
                f"Could not write {key!r}: the bucket or the upload it belonged to no longer exists ({named}).",
                key=key,
            )
        return StorageError("KEY_NOT_FOUND", f"No stored object for key {key!r}.", key=key)
    log_failure(_LOGGER, f"s3 {'write' if writing else 'read'}", exc)
    return StorageError(failure, f"Could not {'write' if writing else 'read'} {key!r}: {named}.", key=key)


_: type[Storage] = S3Storage
__: type[SupportsLocalMirror] = S3Storage
___: type[SupportsPresignedDownload] = S3Storage
"""The three protocols this class claims, checked by mypy so no test has to assert them by hand."""
