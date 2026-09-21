"""`engine.storage`: key validation, atomic writes and the local implementation of the protocol."""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from engine.contracts import RunError, StageKey
from engine.storage import (
    LocalStorage,
    Storage,
    StorageError,
    model_key,
    run_key,
    upload_key,
    validate_key,
)

INVALID_KEYS: list[str] = ["", "../x", "/x", "a//b", "a\\b", "a/../b", "./a", "x" * 513]


@pytest.fixture
def storage(tmp_path: Path) -> LocalStorage:
    return LocalStorage(tmp_path / "data")


def test_local_storage_satisfies_the_protocol(tmp_path: Path) -> None:
    assert isinstance(LocalStorage(tmp_path), Storage)


def test_root_is_created_and_exposed(tmp_path: Path) -> None:
    store = LocalStorage(tmp_path / "nested" / "data")
    assert store.root.is_dir()
    assert store.root == tmp_path / "nested" / "data"


@pytest.mark.parametrize("key", INVALID_KEYS)
def test_invalid_keys_are_rejected(key: str) -> None:
    with pytest.raises(StorageError) as excinfo:
        validate_key(key)
    assert excinfo.value.code == "KEY_INVALID"


@pytest.mark.parametrize("key", ["runs/r_1/run.json", "uploads/u1/file.csv", "a", "a/b/c/d.parquet"])
def test_valid_keys_pass_through(key: str) -> None:
    assert validate_key(key) == key


@pytest.mark.parametrize("key", INVALID_KEYS[1:])
def test_every_method_validates_the_key(storage: LocalStorage, key: str) -> None:
    for call in (
        lambda: storage.write_bytes(key, b"x"),
        lambda: storage.read_bytes(key),
        lambda: storage.exists(key),
        lambda: storage.delete(key),
        lambda: storage.size_bytes(key),
        lambda: storage.local_path(key),
    ):
        with pytest.raises(StorageError) as excinfo:
            call()
        assert excinfo.value.code == "KEY_INVALID"


def test_bytes_round_trip(storage: LocalStorage) -> None:
    storage.write_bytes("runs/r1/blob.bin", b"\x00\x01\x02")
    assert storage.read_bytes("runs/r1/blob.bin") == b"\x00\x01\x02"
    assert storage.size_bytes("runs/r1/blob.bin") == 3


def test_text_round_trip_is_utf8(storage: LocalStorage) -> None:
    storage.write_text("runs/r1/note.txt", "clip 1st-99th pct - ok")
    assert storage.read_text("runs/r1/note.txt") == "clip 1st-99th pct - ok"


def test_model_round_trip(storage: LocalStorage) -> None:
    error = RunError(
        code="ROWS_TOO_FEW",
        message="The file has fewer rows than the minimum.",
        stage=StageKey.VALIDATE,
    )
    storage.write_model("runs/r1/error.json", error)
    assert storage.read_text("runs/r1/error.json").endswith("}\n")
    assert storage.read_model("runs/r1/error.json", RunError) == error


def test_read_model_of_wrong_shape_raises_validation_error(storage: LocalStorage) -> None:
    storage.write_text("runs/r1/error.json", '{"nonsense": 1}')
    with pytest.raises(ValidationError):
        storage.read_model("runs/r1/error.json", RunError)


def test_exists_size_and_delete_are_idempotent(storage: LocalStorage) -> None:
    assert storage.exists("runs/r1/a.json") is False
    with pytest.raises(StorageError) as excinfo:
        storage.read_bytes("runs/r1/a.json")
    assert excinfo.value.code == "KEY_NOT_FOUND"
    with pytest.raises(StorageError) as excinfo:
        storage.size_bytes("runs/r1/a.json")
    assert excinfo.value.code == "KEY_NOT_FOUND"
    storage.delete("runs/r1/a.json")
    storage.write_text("runs/r1/a.json", "{}")
    assert storage.exists("runs/r1/a.json") is True
    storage.delete("runs/r1/a.json")
    storage.delete("runs/r1/a.json")
    assert storage.exists("runs/r1/a.json") is False


def test_list_keys_is_sorted_recursive_and_prefix_filtered(storage: LocalStorage) -> None:
    for key in ("runs/r2/b.json", "runs/r1/a.json", "uploads/u1/f.csv", "runs/r1/deep/c.json"):
        storage.write_text(key, "{}")
    assert storage.list_keys() == (
        "runs/r1/a.json",
        "runs/r1/deep/c.json",
        "runs/r2/b.json",
        "uploads/u1/f.csv",
    )
    assert storage.list_keys("runs/r1/") == ("runs/r1/a.json", "runs/r1/deep/c.json")
    assert storage.list_keys("nothing/") == ()


def test_open_read_yields_the_stored_bytes(storage: LocalStorage) -> None:
    storage.write_bytes("runs/r1/blob.bin", b"hello")
    with storage.open_read("runs/r1/blob.bin") as handle:
        assert handle.read() == b"hello"
    with pytest.raises(StorageError) as excinfo, storage.open_read("runs/r1/missing.bin"):
        pass
    assert excinfo.value.code == "KEY_NOT_FOUND"


def test_a_failure_mid_write_leaves_no_temp_file_and_no_target(storage: LocalStorage) -> None:
    storage.write_text("runs/r1/keep.json", "{}")
    with pytest.raises(RuntimeError), storage.open_write("runs/r1/half.json") as handle:
        handle.write(b"half a document")
        raise RuntimeError("the stage crashed")
    assert storage.exists("runs/r1/half.json") is False
    assert sorted(p.name for p in (storage.root / "runs" / "r1").iterdir()) == ["keep.json"]


def test_a_rewrite_never_shows_a_partial_file(storage: LocalStorage) -> None:
    storage.write_text("runs/r1/status.json", '{"state": "pending"}')
    with pytest.raises(RuntimeError), storage.open_write("runs/r1/status.json") as handle:
        handle.write(b'{"state": "run')
        raise RuntimeError("the stage crashed")
    assert storage.read_text("runs/r1/status.json") == '{"state": "pending"}'


def test_local_path_does_not_copy(storage: LocalStorage) -> None:
    storage.write_text("models/m1/model/meta.json", "{}")
    path = storage.local_path("models/m1/model/meta.json")
    assert path == storage.root / "models/m1/model/meta.json"
    assert path.read_text() == "{}"
    assert storage.local_path("models/m1/model/absent.json").exists() is False


def test_key_helpers() -> None:
    assert run_key("r_20260921_abcdef01", "run.json") == "runs/r_20260921_abcdef01/run.json"
    assert upload_key("u_0123456789ab", "customers.csv") == "uploads/u_0123456789ab/customers.csv"
    assert model_key("m_uc_1", "model") == "models/m_uc_1/model"
    assert model_key("m_uc_1", "model", "predictor.pkl") == "models/m_uc_1/model/predictor.pkl"
    for key in (run_key("r1", "run.json"), upload_key("u1", "f.csv"), model_key("m1", "model")):
        assert validate_key(key) == key
