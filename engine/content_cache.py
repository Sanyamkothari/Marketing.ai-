"""Small bounded caches keyed by what a table or column holds, never by where it lives in memory.

Data Doctor reads the same big columns again at every step of a visit (to find the dates, the number formats, the
types). A step that changed one column should not pay again for the others. A cache here is keyed by
`content_digest`: a hash of the cells (and their order, labels, dtype and which are blank), so the same cells found
again - the same upload read twice, a column a plan step did not touch - give the same key, and a changed cell gives
another. `id()` is never a key: an object freed and its address reused would hand back another column's answer.

Every cache is small and least-recently-used (a parsed date column is megabytes), thread-safe (the visit runs on worker
threads), and holds only derived values (a parsed column, a type, an issue): never a copy of the cells themselves.
A column the hash cannot read (cells of mixed kinds in one column) has no digest and is computed every time.
"""

from __future__ import annotations

import hashlib
import threading
import weakref
from collections import OrderedDict
from collections.abc import Callable
from typing import Any, Final, Generic, TypeVar

import numpy as np
import pandas as pd

__all__ = ["ContentCache", "clear_content_caches", "content_digest", "frame_digest"]

_VALUE = TypeVar("_VALUE")
_HASH_KEY: Final[str] = "0123456789123456"


def _values_hash(values: Any) -> bytes:
    """A 64-bit hash per cell, as bytes. An object column must hold text (and blanks) only: a number among strings
    would hash like its text, so `1` and `"1"` could share a hash, and such a column has no digest."""
    array = values if isinstance(values, np.ndarray) else np.asarray(values)
    if array.dtype == object and pd.api.types.infer_dtype(array, skipna=True) not in {"string", "empty"}:
        raise TypeError("an object column of mixed kinds")
    return bytes(pd.util.hash_array(array, categorize=False).tobytes())


def _index_bytes(index: pd.Index[Any]) -> bytes:
    if isinstance(index, pd.RangeIndex):
        return f"range:{index.start}:{index.stop}:{index.step}".encode()
    return b"index:" + _values_hash(index.to_numpy())


def content_digest(series: pd.Series[Any]) -> str | None:
    """A digest of the column: its name, dtype, labels, blanks and every cell, in order. None when the cells cannot
    be hashed (mixed kinds in one object column, unhashable cells)."""
    try:
        digest = hashlib.sha256()
        digest.update(f"{series.name!r}|{series.dtype}|{len(series)}|".encode())
        digest.update(_index_bytes(series.index))
        digest.update(np.packbits(series.isna().to_numpy()).tobytes())  # a blank hashes as the text "nan"
        values = series.to_numpy() if isinstance(series.dtype, np.dtype) else series.to_numpy(dtype=object)
        digest.update(_values_hash(values))
        return digest.hexdigest()
    except Exception:  # unhashable cells: no digest, so no cache
        return None


def frame_digest(frame: pd.DataFrame) -> str | None:
    """A digest of the whole table: its column names and order, and each column's `content_digest`."""
    digest = hashlib.sha256(f"{len(frame)}|{len(frame.columns)}|".encode())
    for position in range(frame.shape[1]):
        column = content_digest(frame.iloc[:, position])
        if column is None:
            return None
        digest.update(column.encode())
    return digest.hexdigest()


_ALL: weakref.WeakSet[ContentCache[Any]] = weakref.WeakSet()


def clear_content_caches() -> None:
    """Empty every cache (a test that wants a cold start, or a process that wants its memory back)."""
    for cache in list(_ALL):
        cache.clear()


class ContentCache(Generic[_VALUE]):
    """A least-recently-used cache of at most `size` values (and, when `cost` is given, of at most `budget` of total
    cost), safe to share between threads.

    `get_or_compute(key, compute)` runs `compute` when the key is new and keeps its value; a key of None (no digest)
    always computes. Two threads may compute one key at the same time; the answers are equal, the last is kept.
    `cost(value)` is what a value weighs (bytes, for a parsed column): the oldest values go until the rest fit the
    budget, and a value heavier than the whole budget is not kept. `hits` and `misses` count the lookups, for tests
    and for the benchmark."""

    def __init__(self, size: int, *, budget: int = 0, cost: Callable[[_VALUE], int] | None = None) -> None:
        self._size = size
        self._budget = budget
        self._cost = cost
        self._items: OrderedDict[str, tuple[_VALUE, int]] = OrderedDict()
        self._total = 0
        self._lock = threading.Lock()
        self.hits = 0
        self.misses = 0
        _ALL.add(self)

    def get_or_compute(self, key: str | None, compute: Callable[[], _VALUE]) -> _VALUE:
        if key is None:
            return compute()
        with self._lock:
            if key in self._items:
                self._items.move_to_end(key)
                self.hits += 1
                return self._items[key][0]
            self.misses += 1
        value = compute()
        weight = self._cost(value) if self._cost is not None else 0
        if self._cost is not None and weight > self._budget:
            return value
        with self._lock:
            if key in self._items:
                self._total -= self._items.pop(key)[1]
            self._items[key] = (value, weight)
            self._total += weight
            while len(self._items) > self._size or (self._cost is not None and self._total > self._budget):
                self._total -= self._items.popitem(last=False)[1][1]
        return value

    def clear(self) -> None:
        with self._lock:
            self._items.clear()
            self._total = 0
            self.hits = self.misses = 0

    def __len__(self) -> int:
        return len(self._items)
