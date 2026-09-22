"""The conformance suite every `Storage` implementation must pass, written once and run twice.

`engine/storage.py` froze the `Storage` protocol so that a second backend would be a second
implementation and not a change to the contract (DEC-311). A frozen contract is only worth the
tests that check it, and a contract checked once per implementation is really two contracts that
happen to share a name: the moment one suite tests `list_keys` ordering and the other does not, the
backends are free to disagree about it and nothing fails.

So every assertion here that is *about the protocol rather than about a backend* is written once
and parametrised over `["local", "s3"]`, where `s3` is `S3Storage` against moto (DEC-321). What is
genuinely specific to one backend lives elsewhere: `tests/unit/test_storage.py` keeps the local
store's atomic-rename details, and `tests/unit/test_s3_storage.py` keeps key mapping, tagging,
encryption, pre-signing and the AWS error table.

The two hardest guarantees in here are the crash ones. A run that dies mid-write must leave no
object, and a run that dies mid-*rewrite* must leave the previous document readable **in full** -
because `status.json` is polled by the browser while the run is still writing it, and a reader that
can see half a document sees a JSON parse error it can neither explain nor retry (DEC-315).
"""

from __future__ import annotations

import itertools
from collections.abc import Callable, Iterator
from pathlib import Path

import pytest
from pydantic import ValidationError

from engine.contracts import RunError, StageKey
from engine.storage import (
    LocalStorage,
    Storage,
    StorageError,
    flush_local,
    publish_local_path,
    release_local,
)

MakeStorage = Callable[[], Storage]

BUCKET = "marketing-ai-conformance"
REGION = "us-east-1"
STORE_PREFIX = "artefacts"

INVALID_KEYS: list[str] = ["", "../x", "/x", "a//b", "a\\b", "a/../b", "./a", "x" * 513]

# A space and a non-ASCII character: both legal in a storage key, both a place where a backend that
# builds a URL by hand rather than letting its client sign one quietly loses the object.
AWKWARD_KEYS: tuple[str, ...] = ("runs/r1/quarterly report.csv", "runs/r1/café.csv")


@pytest.fixture(params=["local", "s3"])
def make_storage(
    request: pytest.FixtureRequest, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Iterator[MakeStorage]:
    """A factory for independent stores over one shared backing root.

    A factory rather than a store, because "materialise-on-read" can only be tested by building a
    *second* store over the same bucket and asking it for the same tree: a mirror that is already
    warm proves nothing about hydration.
    """
    if request.param == "local":
        root = tmp_path / "backing"
        yield lambda: LocalStorage(root)
        return

    moto = pytest.importorskip("moto", reason="the s3 half of the suite needs moto")
    for name, value in (
        ("AWS_ACCESS_KEY_ID", "testing"),
        ("AWS_SECRET_ACCESS_KEY", "testing"),
        ("AWS_SECURITY_TOKEN", "testing"),
        ("AWS_SESSION_TOKEN", "testing"),
        ("AWS_DEFAULT_REGION", REGION),
    ):
        monkeypatch.setenv(name, value)

    import boto3

    from engine.aws.s3_storage import S3Storage

    with moto.mock_aws():
        boto3.client("s3", region_name=REGION).create_bucket(Bucket=BUCKET)
        mirrors = itertools.count()

        def build() -> Storage:
            return S3Storage(
                BUCKET,
                prefix=STORE_PREFIX,
                client=boto3.client("s3", region_name=REGION),
                workspace=tmp_path / f"mirror-{next(mirrors)}",
            )

        yield build


@pytest.fixture
def storage(make_storage: MakeStorage) -> Storage:
    """One store from the parametrised factory; the subject of most of the suite."""
    return make_storage()


# ---------------------------------------------------------------------------
# The protocol itself
# ---------------------------------------------------------------------------
def test_the_store_satisfies_the_protocol(storage: Storage) -> None:
    assert isinstance(storage, Storage)


@pytest.mark.parametrize("key", INVALID_KEYS)
def test_every_method_validates_the_key(storage: Storage, key: str) -> None:
    """One gate, applied by every entry point - a method that forgets it is a path-traversal hole."""

    def open_read() -> None:
        with storage.open_read(key):
            pass  # pragma: no cover - the context manager must refuse before it yields

    def open_write() -> None:
        with storage.open_write(key):
            pass  # pragma: no cover - the context manager must refuse before it yields

    calls: tuple[Callable[[], object], ...] = (
        lambda: storage.write_bytes(key, b"x"),
        lambda: storage.read_bytes(key),
        lambda: storage.write_text(key, "x"),
        lambda: storage.read_text(key),
        lambda: storage.write_model(key, _error()),
        lambda: storage.read_model(key, RunError),
        lambda: storage.exists(key),
        lambda: storage.delete(key),
        lambda: storage.size_bytes(key),
        lambda: storage.local_path(key),
        open_read,
        open_write,
    )
    for call in calls:
        with pytest.raises(StorageError) as excinfo:
            call()
        assert excinfo.value.code == "KEY_INVALID"


# ---------------------------------------------------------------------------
# Round trips
# ---------------------------------------------------------------------------
def test_bytes_round_trip(storage: Storage) -> None:
    storage.write_bytes("runs/r1/blob.bin", b"\x00\x01\x02")
    assert storage.read_bytes("runs/r1/blob.bin") == b"\x00\x01\x02"
    assert storage.size_bytes("runs/r1/blob.bin") == 3


def test_text_round_trip_is_utf8(storage: Storage) -> None:
    storage.write_text("runs/r1/note.txt", "clip 1st-99th pct - café")
    assert storage.read_text("runs/r1/note.txt") == "clip 1st-99th pct - café"
    assert storage.size_bytes("runs/r1/note.txt") == len("clip 1st-99th pct - café".encode())


def test_model_round_trip(storage: Storage) -> None:
    storage.write_model("runs/r1/error.json", _error())
    assert storage.read_text("runs/r1/error.json").endswith("}\n")
    assert storage.read_model("runs/r1/error.json", RunError) == _error()


def test_read_model_of_the_wrong_shape_raises_validation_error(storage: Storage) -> None:
    storage.write_text("runs/r1/error.json", '{"nonsense": 1}')
    with pytest.raises(ValidationError):
        storage.read_model("runs/r1/error.json", RunError)


def test_a_zero_byte_value_is_a_value(storage: Storage) -> None:
    """Empty is not absent: a zero-row export is a result, and `exists` must say so."""
    storage.write_bytes("runs/r1/empty.csv", b"")
    assert storage.exists("runs/r1/empty.csv") is True
    assert storage.read_bytes("runs/r1/empty.csv") == b""
    assert storage.size_bytes("runs/r1/empty.csv") == 0
    with storage.open_read("runs/r1/empty.csv") as handle:
        assert handle.read() == b""
    assert storage.list_keys("runs/r1/") == ("runs/r1/empty.csv",)


def test_overwrite_replaces_content_and_size(storage: Storage) -> None:
    storage.write_text("runs/r1/status.json", '{"state": "running", "progress": 0}')
    storage.write_text("runs/r1/status.json", '{"state": "done"}')
    assert storage.read_text("runs/r1/status.json") == '{"state": "done"}'
    assert storage.size_bytes("runs/r1/status.json") == len('{"state": "done"}')
    assert storage.list_keys("runs/r1/") == ("runs/r1/status.json",)


@pytest.mark.parametrize("key", AWKWARD_KEYS)
def test_a_key_with_a_space_or_a_non_ascii_character_round_trips(storage: Storage, key: str) -> None:
    storage.write_bytes(key, b"payload")
    assert storage.exists(key) is True
    assert storage.read_bytes(key) == b"payload"
    assert key in storage.list_keys("runs/")
    storage.delete(key)
    assert storage.exists(key) is False


# ---------------------------------------------------------------------------
# Absence, deletion and listing
# ---------------------------------------------------------------------------
def test_a_missing_key_is_key_not_found_on_read_and_on_size(storage: Storage) -> None:
    assert storage.exists("runs/r1/a.json") is False
    for call in (
        lambda: storage.read_bytes("runs/r1/a.json"),
        lambda: storage.read_text("runs/r1/a.json"),
        lambda: storage.read_model("runs/r1/a.json", RunError),
        lambda: storage.size_bytes("runs/r1/a.json"),
    ):
        with pytest.raises(StorageError) as excinfo:
            call()
        assert excinfo.value.code == "KEY_NOT_FOUND"
    with pytest.raises(StorageError) as excinfo, storage.open_read("runs/r1/a.json"):
        pass  # pragma: no cover - open_read must refuse before it yields
    assert excinfo.value.code == "KEY_NOT_FOUND"


def test_delete_is_idempotent(storage: Storage) -> None:
    storage.delete("runs/r1/a.json")
    storage.write_text("runs/r1/a.json", "{}")
    storage.delete("runs/r1/a.json")
    storage.delete("runs/r1/a.json")
    assert storage.exists("runs/r1/a.json") is False


def test_list_keys_is_sorted_recursive_relative_and_prefix_filtered(storage: Storage) -> None:
    for key in ("runs/r2/b.json", "runs/r1/a.json", "uploads/u1/f.csv", "runs/r1/deep/c.json"):
        storage.write_text(key, "{}")
    assert storage.list_keys() == (
        "runs/r1/a.json",
        "runs/r1/deep/c.json",
        "runs/r2/b.json",
        "uploads/u1/f.csv",
    )
    assert storage.list_keys("runs/r1/") == ("runs/r1/a.json", "runs/r1/deep/c.json")
    assert storage.list_keys("runs/r1") == ("runs/r1/a.json", "runs/r1/deep/c.json")
    assert storage.list_keys("nothing/") == ()


def test_list_keys_does_not_leak_the_store_root(storage: Storage) -> None:
    """Keys come back relative, so the same key a caller wrote is the key it reads back."""
    storage.write_text("runs/r1/a.json", "{}")
    (key,) = storage.list_keys()
    assert key == "runs/r1/a.json"
    assert storage.read_text(key) == "{}"


# ---------------------------------------------------------------------------
# Streams, and what a crash leaves behind
# ---------------------------------------------------------------------------
def test_open_write_then_open_read_round_trips_in_pieces(storage: Storage) -> None:
    with storage.open_write("runs/r1/blob.bin") as handle:
        handle.write(b"hello ")
        handle.write(b"world")
    assert storage.size_bytes("runs/r1/blob.bin") == 11
    with storage.open_read("runs/r1/blob.bin") as handle:
        assert handle.read(5) == b"hello"
        assert handle.read() == b" world"


def test_a_crash_mid_write_leaves_no_object(storage: Storage) -> None:
    storage.write_text("runs/r1/keep.json", "{}")
    with pytest.raises(RuntimeError), storage.open_write("runs/r1/half.json") as handle:
        handle.write(b"half a document")
        raise RuntimeError("the stage crashed")
    assert storage.exists("runs/r1/half.json") is False
    assert storage.list_keys("runs/r1/") == ("runs/r1/keep.json",)


def test_a_crash_mid_rewrite_leaves_the_previous_document_readable_in_full(storage: Storage) -> None:
    """The `status.json` polling guarantee: a reader sees the old document or the new one."""
    storage.write_text("runs/r1/status.json", '{"state": "pending"}')
    with pytest.raises(RuntimeError), storage.open_write("runs/r1/status.json") as handle:
        handle.write(b'{"state": "run')
        raise RuntimeError("the stage crashed")
    assert storage.read_text("runs/r1/status.json") == '{"state": "pending"}'
    assert storage.size_bytes("runs/r1/status.json") == len('{"state": "pending"}')


# ---------------------------------------------------------------------------
# `local_path`, and the mirror behind it
# ---------------------------------------------------------------------------
def test_local_path_of_an_unwritten_key_does_not_exist(storage: Storage) -> None:
    """`LocalStorage` returns a path that is simply not there; a mirror must do the same."""
    assert storage.local_path("models/uc/1/model/absent.json").exists() is False


def test_a_tree_written_through_local_path_reaches_a_second_independent_store(
    make_storage: MakeStorage,
) -> None:
    """Write a nested tree through `local_path`, publish it, read it back somewhere else.

    The second store shares the backing root and nothing else - no warm mirror, no shared object -
    so the tree can only arrive by being materialised on read.
    """
    store = make_storage()
    root = store.local_path("models/uc/1/model")
    root.mkdir(parents=True, exist_ok=True)
    (root / "predictor.pkl").write_bytes(b"\x00binary")
    (root / "models" / "LightGBM").mkdir(parents=True)
    (root / "models" / "LightGBM" / "model.pkl").write_bytes(b"leaf")

    publish_local_path(store, "models/uc/1/model")
    assert publish_local_path(store, "models/uc/1/model") == (), "a second publish uploads nothing"
    assert store.list_keys("models/uc/1/model/") == (
        "models/uc/1/model/models/LightGBM/model.pkl",
        "models/uc/1/model/predictor.pkl",
    )

    other = make_storage()
    mirrored = other.local_path("models/uc/1/model")
    assert (mirrored / "predictor.pkl").read_bytes() == b"\x00binary"
    assert (mirrored / "models" / "LightGBM" / "model.pkl").read_bytes() == b"leaf"
    assert other.read_bytes("models/uc/1/model/predictor.pkl") == b"\x00binary"


def test_flush_local_publishes_what_the_mirror_holds_and_the_store_does_not(
    make_storage: MakeStorage,
) -> None:
    store = make_storage()
    path = store.local_path("runs/r1/predictor/leaf.bin")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"leaf")

    flush_local(store, "runs/r1/")
    assert flush_local(store, "runs/r1/") == (), "a second flush finds nothing new"
    assert make_storage().read_bytes("runs/r1/predictor/leaf.bin") == b"leaf"


def test_release_local_flushes_first_and_is_safe_twice(make_storage: MakeStorage) -> None:
    """Reclaiming the disk must never be the thing that loses an artefact."""
    store = make_storage()
    path = store.local_path("runs/r1/predictor/leaf.bin")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"leaf")

    release_local(store, "runs/r1/")
    release_local(store, "runs/r1/")
    assert store.read_bytes("runs/r1/predictor/leaf.bin") == b"leaf"
    assert store.local_path("runs/r1/predictor/leaf.bin").read_bytes() == b"leaf"


def _error() -> RunError:
    return RunError(
        code="ROWS_TOO_FEW",
        message="The file has fewer rows than the minimum.",
        stage=StageKey.VALIDATE,
    )
