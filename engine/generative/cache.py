"""An on-disk cache for completions, keyed by everything that could change the answer.

A generative run asks the same question more than once more often than it looks: a reference set is
graded twice while a threshold is tuned, a copy batch is regenerated for one blocked variant and
re-judges the rest, an index is rebuilt and most documents are unchanged. Each of those is a call
that costs money and returns the same bytes, so the second one is read from here instead.

The key is a digest of **the prompt's content hash, the rendered system and user strings, the model
id and the temperature**, because every one of those changes what a model would say. A reworded
prompt has a different content hash and so misses; a different temperature misses; the same call
made twice hits. Nothing else is in the key, and in particular the *purpose* is not: two purposes
that render byte-identical prompts to the same model deserve the same answer.

A hit is metered as a hit rather than as a call, so `llm_usage.json` says what was spent and what
was saved rather than quietly under-reporting both.

Two properties keep it honest:

**It never caches a failure.** Only a completion that came back is stored. A refusal, a timeout or
a malformed answer is not: caching one would make a transient failure permanent.

**It is bounded and it says so.** `prune` drops the least recently read entries once the directory
passes `max_bytes`. A cache that grew without limit would be a disk-full incident in a batch job,
and one that silently dropped entries would look like a cache that does not work.
"""

from __future__ import annotations

import hashlib
import json
import os
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Final

from engine.llm import Completion
from engine.utils.logging import get_logger, log_failure

__all__ = ["CACHE_DIRNAME", "DEFAULT_MAX_BYTES", "CompletionCache", "NullCache", "cache_key"]

_LOGGER = get_logger(__name__)

CACHE_DIRNAME: Final[str] = "llm_cache"
"""The directory under the data root. Gitignored with the rest of `data/`."""

DEFAULT_MAX_BYTES: Final[int] = 256 * 1024 * 1024
"""A quarter of a gigabyte of completions, which is tens of thousands of them."""

_SUFFIX: Final[str] = ".json"
_FANOUT: Final[int] = 2
"""Characters of the key used as a sub-directory, so one directory never holds every entry."""


def cache_key(*, content_hash: str, system: str, user: str, model_id: str, temperature: float) -> str:
    """The digest of everything that could change what a model says.

    Temperature is formatted rather than hashed as a float so that `0.2` and `0.20` are one key:
    they are one setting, and a cache that treated them as two would miss for no reason.
    """
    digest = hashlib.blake2b(digest_size=20)
    for part in (content_hash, system, user, model_id, f"{temperature:.4f}"):
        digest.update(part.encode("utf-8"))
        digest.update(b"\x00")
    return digest.hexdigest()


@dataclass(frozen=True)
class NullCache:
    """The cache a run gets when `generative.budget.cache` is off: it stores nothing and says so."""

    def get(self, key: str) -> Completion | None:
        """Always a miss."""
        del key
        return None

    def put(self, key: str, completion: Completion) -> None:
        """Stores nothing."""
        del key, completion

    def prune(self, max_bytes: int = DEFAULT_MAX_BYTES) -> int:
        """Nothing to prune; returns 0."""
        del max_bytes
        return 0


class CompletionCache:
    """Completions on disk, one JSON file per key, under `root/<first two chars>/<key>.json`.

    A read touches the file's access time, which is what `prune` orders by, so the entries that go
    are the ones nothing has asked for. Every read and write is wrapped: a cache that raised would
    turn a working run into a failed one, and a cache is by definition the part you can do without.
    """

    def __init__(self, root: Path) -> None:
        self._root = Path(root)

    @property
    def root(self) -> Path:
        """The directory entries are written under."""
        return self._root

    def _path(self, key: str) -> Path:
        return self._root / key[:_FANOUT] / f"{key}{_SUFFIX}"

    def get(self, key: str) -> Completion | None:
        """The stored completion for `key`, or `None` on a miss or on any failure to read one."""
        path = self._path(key)
        try:
            if not path.is_file():
                return None
            payload = json.loads(path.read_text(encoding="utf-8"))
            os.utime(path, None)  # a read is what keeps an entry alive, so record that it happened
        except (OSError, ValueError) as exc:
            log_failure(_LOGGER, "llm_cache.read", exc)
            return None
        return Completion(
            text=str(payload["text"]),
            model_id=str(payload["model_id"]),
            input_tokens=int(payload["input_tokens"]),
            output_tokens=int(payload["output_tokens"]),
            latency_ms=int(payload["latency_ms"]),
            stop_reason=str(payload["stop_reason"]),
            estimated_tokens=bool(payload["estimated_tokens"]),
        )

    def put(self, key: str, completion: Completion) -> None:
        """Store `completion` under `key`, atomically, and give up quietly if the disk will not have it."""
        path = self._path(key)
        payload = {
            "text": completion.text,
            "model_id": completion.model_id,
            "input_tokens": completion.input_tokens,
            "output_tokens": completion.output_tokens,
            "latency_ms": completion.latency_ms,
            "stop_reason": completion.stop_reason,
            "estimated_tokens": completion.estimated_tokens,
        }
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            temporary = path.with_suffix(f"{_SUFFIX}.tmp")
            temporary.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
            temporary.replace(path)
        except OSError as exc:
            log_failure(_LOGGER, "llm_cache.write", exc)

    def entries(self) -> Iterator[Path]:
        """Every stored entry, unordered."""
        if not self._root.is_dir():
            return iter(())
        return self._root.glob(f"*/*{_SUFFIX}")

    def size_bytes(self) -> int:
        """How much disk the cache is using."""
        return sum(path.stat().st_size for path in self.entries() if path.is_file())

    def prune(self, max_bytes: int = DEFAULT_MAX_BYTES) -> int:
        """Drop least-recently-read entries until the cache fits; returns how many were dropped."""
        try:
            files = [(path.stat().st_atime, path.stat().st_size, path) for path in self.entries()]
        except OSError as exc:
            log_failure(_LOGGER, "llm_cache.prune", exc)
            return 0
        total = sum(size for _atime, size, _path in files)
        if total <= max_bytes:
            return 0
        dropped = 0
        for _atime, size, path in sorted(files):
            if total <= max_bytes:
                break
            try:
                path.unlink(missing_ok=True)
            except OSError as exc:  # one unremovable entry must not stop the prune
                log_failure(_LOGGER, "llm_cache.prune", exc)
                continue
            total -= size
            dropped += 1
        _LOGGER.info("llm_cache.pruned dropped=%d bytes=%d", dropped, total)
        return dropped
