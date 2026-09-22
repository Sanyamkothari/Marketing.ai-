"""Artefact storage: the `Storage` protocol and its local filesystem implementation.

Keys are opaque, posix-style relative strings validated by a single gate; every write is
atomic (temp file in the same directory, fsync, `os.replace`) so a crashed run never leaves
a half-written JSON behind (DEC-016). `local_path()` is the only escape hatch, for libraries
such as AutoGluon that insist on a real directory.
"""

from __future__ import annotations

import os
import tempfile
from collections.abc import Iterator
from contextlib import AbstractContextManager, contextmanager
from pathlib import Path
from typing import BinaryIO, Final, Protocol, TypeVar, runtime_checkable

from pydantic import BaseModel

from engine.contracts import dump_artefact
from engine.settings import DEFAULT_DATA_DIR as _DEFAULT_DATA_DIR
from engine.settings import ENV_VARS, settings

M = TypeVar("M", bound=BaseModel)

MAX_KEY_LENGTH: Final[int] = 512
DATA_DIR_ENV_VAR: Final[str] = ENV_VARS["data_dir"]
# Both constants are defined in `engine.settings` and re-exported here, where every caller of
# this module already looks for them.
DEFAULT_DATA_DIR: Final[str] = _DEFAULT_DATA_DIR


class StorageError(Exception):
    """A storage operation failed. `code` is one of KEY_NOT_FOUND | KEY_INVALID | WRITE_FAILED | READ_FAILED."""

    def __init__(self, code: str, message: str, *, key: str | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.key = key


def validate_key(key: str) -> str:
    """Return `key` unchanged when it is a valid storage key, else raise `StorageError('KEY_INVALID')`.

    Keys are posix-style relative paths: no leading "/", no "." or ".." segment, no backslash,
    no empty segment, at most 512 characters. Every `Storage` method calls this first (DEC-016).
    """
    if not key:
        raise StorageError("KEY_INVALID", "A storage key must not be empty.", key=key)
    if len(key) > MAX_KEY_LENGTH:
        raise StorageError(
            "KEY_INVALID",
            f"A storage key must be at most {MAX_KEY_LENGTH} characters, this one has {len(key)}.",
            key=key,
        )
    if "\\" in key:
        raise StorageError("KEY_INVALID", f"A storage key must not contain a backslash: {key!r}.", key=key)
    if "\x00" in key:
        raise StorageError("KEY_INVALID", f"A storage key must not contain a null byte: {key!r}.", key=key)
    segments = key.split("/")
    if any(segment == "" for segment in segments):
        raise StorageError(
            "KEY_INVALID",
            f"A storage key must be relative and must not contain an empty segment: {key!r}.",
            key=key,
        )
    if any(segment in {".", ".."} for segment in segments):
        raise StorageError(
            "KEY_INVALID",
            f"A storage key must not contain a '.' or '..' segment: {key!r}.",
            key=key,
        )
    return key


def _validate_prefix(prefix: str) -> str:
    """Validate a `list_keys` prefix, which may be empty or end with "/" (unlike a key)."""
    if prefix == "":
        return prefix
    if len(prefix) > MAX_KEY_LENGTH:
        raise StorageError(
            "KEY_INVALID",
            f"A storage prefix must be at most {MAX_KEY_LENGTH} characters, this one has {len(prefix)}.",
            key=prefix,
        )
    if prefix.startswith("/") or "\\" in prefix or "\x00" in prefix:
        raise StorageError("KEY_INVALID", f"Invalid storage prefix: {prefix!r}.", key=prefix)
    if any(segment in {".", ".."} for segment in prefix.split("/")):
        raise StorageError(
            "KEY_INVALID",
            f"A storage prefix must not contain a '.' or '..' segment: {prefix!r}.",
            key=prefix,
        )
    return prefix


@runtime_checkable
class Storage(Protocol):
    """Everything the engine needs from an artefact store; `LocalStorage` now, `S3Storage` in Phase 4."""

    def write_bytes(self, key: str, data: bytes) -> None: ...

    def read_bytes(self, key: str) -> bytes: ...

    def write_text(self, key: str, text: str) -> None: ...

    def read_text(self, key: str) -> str: ...

    def write_model(self, key: str, model: BaseModel) -> None: ...

    def read_model(self, key: str, model_type: type[M]) -> M: ...

    def exists(self, key: str) -> bool: ...

    def list_keys(self, prefix: str = "") -> tuple[str, ...]: ...

    def delete(self, key: str) -> None: ...

    def size_bytes(self, key: str) -> int: ...

    def open_read(self, key: str) -> AbstractContextManager[BinaryIO]: ...

    def open_write(self, key: str) -> AbstractContextManager[BinaryIO]: ...

    def local_path(self, key: str) -> Path: ...


class LocalStorage:
    """Filesystem `Storage` rooted at `root` (default `data/`, gitignored).

    Writes are atomic: a temp file in the same directory is written, flushed, fsynced and then
    moved over the target with `os.replace`, so readers see either the old file or the new one.
    """

    def __init__(self, root: Path) -> None:
        self._root = Path(root)
        self._root.mkdir(parents=True, exist_ok=True)

    @property
    def root(self) -> Path:
        """The directory every key is resolved against."""
        return self._root

    def _path(self, key: str) -> Path:
        return self._root / validate_key(key)

    def write_bytes(self, key: str, data: bytes) -> None:
        with self.open_write(key) as handle:
            handle.write(data)

    def read_bytes(self, key: str) -> bytes:
        path = self._path(key)
        if not path.is_file():
            raise StorageError("KEY_NOT_FOUND", f"No stored object for key {key!r}.", key=key)
        try:
            return path.read_bytes()
        except OSError as exc:
            raise StorageError("READ_FAILED", f"Could not read {key!r}: {exc}.", key=key) from exc

    def write_text(self, key: str, text: str) -> None:
        self.write_bytes(key, text.encode("utf-8"))

    def read_text(self, key: str) -> str:
        return self.read_bytes(key).decode("utf-8")

    def write_model(self, key: str, model: BaseModel) -> None:
        self.write_text(key, dump_artefact(model))

    def read_model(self, key: str, model_type: type[M]) -> M:
        return model_type.model_validate_json(self.read_bytes(key))

    def exists(self, key: str) -> bool:
        return self._path(key).is_file()

    def list_keys(self, prefix: str = "") -> tuple[str, ...]:
        _validate_prefix(prefix)
        if not self._root.is_dir():
            return ()
        keys = [path.relative_to(self._root).as_posix() for path in self._root.rglob("*") if path.is_file()]
        return tuple(sorted(key for key in keys if key.startswith(prefix)))

    def delete(self, key: str) -> None:
        path = self._path(key)
        try:
            path.unlink(missing_ok=True)
        except OSError as exc:
            raise StorageError("WRITE_FAILED", f"Could not delete {key!r}: {exc}.", key=key) from exc

    def size_bytes(self, key: str) -> int:
        path = self._path(key)
        if not path.is_file():
            raise StorageError("KEY_NOT_FOUND", f"No stored object for key {key!r}.", key=key)
        return path.stat().st_size

    @contextmanager
    def open_read(self, key: str) -> Iterator[BinaryIO]:
        path = self._path(key)
        if not path.is_file():
            raise StorageError("KEY_NOT_FOUND", f"No stored object for key {key!r}.", key=key)
        try:
            handle = path.open("rb")
        except OSError as exc:
            raise StorageError("READ_FAILED", f"Could not read {key!r}: {exc}.", key=key) from exc
        try:
            yield handle
        finally:
            handle.close()

    @contextmanager
    def open_write(self, key: str) -> Iterator[BinaryIO]:
        path = self._path(key)
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            descriptor, temp_name = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
        except OSError as exc:
            raise StorageError(
                "WRITE_FAILED", f"Could not open {key!r} for writing: {exc}.", key=key
            ) from exc
        temp_path = Path(temp_name)
        try:
            with os.fdopen(descriptor, "wb") as handle:
                yield handle
                handle.flush()
                os.fsync(handle.fileno())
            temp_path.replace(path)
        except OSError as exc:
            temp_path.unlink(missing_ok=True)
            raise StorageError("WRITE_FAILED", f"Could not write {key!r}: {exc}.", key=key) from exc
        except BaseException:
            temp_path.unlink(missing_ok=True)
            raise

    def local_path(self, key: str) -> Path:
        """The real filesystem path for `key`; nothing is copied and the file need not exist."""
        return self._path(key)


def run_key(run_id: str, filename: str) -> str:
    """Key of an artefact inside a run directory."""
    return f"runs/{run_id}/{filename}"


def upload_key(upload_id: str, filename: str) -> str:
    """Key of an uploaded file."""
    return f"uploads/{upload_id}/{filename}"


def model_key(model_id: str, *parts: str) -> str:
    """Key of a file (or directory) belonging to a model version."""
    return "/".join(("models", model_id, *parts))


def default_storage() -> LocalStorage:
    """The process-wide default store: `$MARKETING_AI_DATA_DIR` (or `data/`).

    Read through `engine.settings`, so the variable is documented in one place. `Settings` also
    carries `storage_backend`, which is `local` until Phase 4a implements `S3Storage`; this factory
    stays local-only until that implementation exists, rather than branching on a value with
    nothing behind it.
    """
    return LocalStorage(settings().data_dir)
