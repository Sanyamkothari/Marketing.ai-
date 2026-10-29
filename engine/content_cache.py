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
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from typing import TYPE_CHECKING, Any, Final, Generic, TypeVar

if TYPE_CHECKING:
    import pandas as pd

__all__ = [
    "ContentCache",
    "clear_content_caches",
    "column_cache",
    "content_digest",
    "digest_memo",
    "frame_digest",
    "map_distinct",
    "series_bytes",
]

_VALUE = TypeVar("_VALUE")
_HASH_KEY: Final[str] = "0123456789123456"


def _values_hash(values: Any) -> bytes:
    """A 64-bit hash per cell, as bytes. An object column must hold text (and blanks) only: a number among strings
    would hash like its text, so `1` and `"1"` could share a hash, and such a column has no digest."""
    import numpy as np
    import pandas as pd

    array = values if isinstance(values, np.ndarray) else np.asarray(values)
    if array.dtype == object and pd.api.types.infer_dtype(array, skipna=True) not in {"string", "empty"}:
        raise TypeError("an object column of mixed kinds")
    return bytes(pd.util.hash_array(array, categorize=False).tobytes())


def _index_bytes(index: pd.Index[Any]) -> bytes:
    import pandas as pd

    if isinstance(index, pd.RangeIndex):
        return f"range:{index.start}:{index.stop}:{index.step}".encode()
    return b"index:" + _values_hash(index.to_numpy())


_MEMO_LOCK = threading.Lock()
_MEMO_DEPTH = 0
_MEMO: dict[tuple[Any, ...], tuple[tuple[Any, ...], bytes, str | None]] = {}
"""Digests found during the step now running, by where the column's cells sit in memory. Each entry holds the
column's arrays, so an address cannot be reused by another column while the entry lives, and a few sampled cells
that must still match (a column changed in place is hashed again). The memo is empty outside `digest_memo()`."""
_SAMPLE: Final[int] = 257


@contextmanager
def digest_memo() -> Iterator[None]:
    """Within the block a column is hashed once, however many caches ask for its digest (one step of a visit looks
    the same big columns up in a dozen caches). The memo is dropped when the outermost block ends."""
    global _MEMO_DEPTH
    with _MEMO_LOCK:
        _MEMO_DEPTH += 1
    try:
        yield
    finally:
        with _MEMO_LOCK:
            _MEMO_DEPTH -= 1
            if _MEMO_DEPTH == 0:
                _MEMO.clear()


def _location(series: pd.Series[Any]) -> tuple[tuple[Any, ...], bytes, tuple[Any, ...]] | None:
    """Where the column's cells and labels are held, a sample of the cells, and the objects to keep alive; None for
    a column whose storage cannot be named."""
    import pandas as pd

    array = series.array
    if isinstance(array, pd.arrays.NumpyExtensionArray):
        values = array.to_numpy()
        if values.ndim != 1 or not values.flags.c_contiguous:
            return None
        where: tuple[Any, ...] = (
            "np",
            values.__array_interface__["data"][0],
            values.strides,
            values.dtype.str,
        )
        held: tuple[Any, ...] = (values, series.index)
        taken = values[:: max(1, len(values) // _SAMPLE)]
        sample = taken.tobytes() if values.dtype != object else repr(taken.tolist()).encode()
    else:
        where, held = ("ext", id(array)), (array, series.index)
        sample = repr(array[:: max(1, len(array) // _SAMPLE)].tolist()).encode()
    key = (*where, id(series.index), len(series), repr(series.name), str(series.dtype))
    return key, sample, held


def content_digest(series: pd.Series[Any]) -> str | None:
    """A digest of the column: its name, dtype, labels, blanks and every cell, in order. None when the cells cannot
    be hashed (mixed kinds in one object column, unhashable cells)."""
    if _MEMO_DEPTH == 0:
        return _content_digest(series)
    try:
        located = _location(series)
    except Exception:  # storage that cannot be sampled: hash it every time
        located = None
    if located is None:
        return _content_digest(series)
    key, sample, held = located
    found = _MEMO.get(key)
    if found is not None and found[1] == sample:
        return found[2]
    result = _content_digest(series)
    with _MEMO_LOCK:
        if _MEMO_DEPTH:
            _MEMO[key] = (held, sample, result)
    return result


def _content_digest(series: pd.Series[Any]) -> str | None:
    import numpy as np

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


def series_bytes(series: pd.Series[Any]) -> int:
    """What a column weighs, text included (a Python string per cell is counted)."""
    return int(series.memory_usage(index=True, deep=True))


def column_cache(size: int, *, megabytes: int) -> ContentCache[pd.Series[Any]]:
    """A cache of columns read from other columns (the filled cells as trimmed text), by budget in megabytes. Use it
    with `cached_column`, which hands out a copy so a caller may change what it gets."""
    return ContentCache(size, budget=megabytes * 2**20, cost=series_bytes)


def cached_column(
    cache: ContentCache[pd.Series[Any]],
    series: pd.Series[Any],
    tag: str,
    compute: Callable[[], pd.Series[Any]],
) -> pd.Series[Any]:
    """`compute()` once per content of `series` and `tag`; the caller gets a column of its own."""
    digest = content_digest(series)
    return cache.get_or_compute(None if digest is None else f"{tag}|{digest}", compute).copy()


def map_distinct(series: pd.Series[Any], convert: Callable[[pd.Index[Any]], Any]) -> pd.Series[Any]:
    """`series` with `convert` applied to its distinct values, then spread over the rows (a column of a few thousand
    spellings over half a million rows is read a few thousand times, not half a million). `convert` gets the distinct
    values and returns one answer per value, in order; blank cells stay blank."""
    import numpy as np
    import pandas as pd

    codes, distinct = pd.factorize(series, use_na_sentinel=True)
    converted = np.asarray(convert(pd.Index(distinct)), dtype=object)
    spread = np.empty(len(series), dtype=object)
    found = codes >= 0
    spread[found] = converted[codes[found]]
    spread[~found] = None
    return pd.Series(spread, index=series.index, name=series.name)
