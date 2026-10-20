"""Two places to keep priors: a SQL table (production) and a directory of JSON files (laptops, the Gym).

Both implement `autonomy.interfaces.PriorStore` and behave like `InMemoryPriorStore`: one prior per
key, a second `put` with the same key replaces the first, and `keys(prefix)` returns matching keys in
code-point order (the order Python sorts strings in).

A prior is a handful of pooled numbers, never customer rows, so the table stays small (one row per
pattern). That is why `keys` filters and sorts in Python rather than in SQL: it sidesteps `LIKE`
escaping and database collations, and costs nothing at this size.

Values must be finite. JSON has no NaN or infinity, and a NaN starting point would silently poison
every estimate blended with it, so both stores refuse one at `put` time rather than at read time.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import tempfile
from pathlib import Path
from typing import Any

from sqlalchemy import JSON, Column, Integer, MetaData, String, Table, select
from sqlalchemy.engine import Engine

from autonomy.contracts import Prior
from autonomy.store.schema import UtcDateTime
from autonomy.store.sql import upsert

__all__ = [
    "MAX_KEY_LENGTH",
    "JsonPriorStore",
    "SqlPriorStore",
    "create_all",
    "prior_metadata",
    "prior_table",
]

prior_metadata = MetaData()
"""The prior table's own metadata, kept apart from `autonomy.store.schema.metadata` so this module
owns its table. Pass `schema_translate_map` on the engine to place it in a schema of your own."""

MAX_KEY_LENGTH = 200
"""The longest key the SQL table holds. Postgres refuses a longer one and SQLite would silently keep it,
so `SqlPriorStore.put` refuses it up front and both databases behave the same."""

prior_table = Table(
    "prior",
    prior_metadata,
    Column("key", String(MAX_KEY_LENGTH), primary_key=True),
    Column("source", String(20), nullable=False),
    Column("n_clients", Integer, nullable=False),
    Column("n_observations", Integer, nullable=False),
    Column("updated_at", UtcDateTime(), nullable=False),
    Column("body", JSON, nullable=False, doc="The whole Prior as model_dump(mode='json')"),
)


def create_all(engine: Engine) -> None:
    """Create the prior table if it does not exist yet. Safe to call twice."""
    prior_metadata.create_all(engine)


def _check_finite(prior: Prior) -> None:
    bad = sorted(name for name, value in prior.values.items() if not math.isfinite(value))
    if bad:
        raise ValueError(f"prior {prior.key!r} has non-finite values for {bad}; refusing to store it")


class SqlPriorStore:
    """Priors in one SQL table (SQLAlchemy Core). Works on Postgres and SQLite alike.

    Call `create_all(engine)` once before use; the store does not create tables itself so that a
    read-only connection never tries to.
    """

    def __init__(self, engine: Engine) -> None:
        self._engine = engine

    def get(self, key: str) -> Prior | None:
        with self._engine.connect() as conn:
            body = conn.execute(select(prior_table.c.body).where(prior_table.c.key == key)).scalar()
        return None if body is None else Prior.model_validate(body)

    def put(self, prior: Prior) -> None:
        _check_finite(prior)
        if len(prior.key) > MAX_KEY_LENGTH:
            raise ValueError(
                f"prior key is {len(prior.key)} characters; the table holds at most {MAX_KEY_LENGTH}"
            )
        row: dict[str, Any] = {
            "key": prior.key,
            "source": prior.source.value,
            "n_clients": prior.n_clients,
            "n_observations": prior.n_observations,
            "updated_at": prior.updated_at,
            "body": prior.model_dump(mode="json"),
        }
        with self._engine.begin() as conn:
            upsert(conn, prior_table, [row], keep=())

    def keys(self, prefix: str = "") -> list[str]:
        with self._engine.connect() as conn:
            every = conn.execute(select(prior_table.c.key)).scalars().all()
        return sorted(k for k in every if k.startswith(prefix))


_UNSAFE = re.compile(r"[^A-Za-z0-9._-]+")


class JsonPriorStore:
    """Priors as one JSON file each in a directory, so they can be read, diffed and shipped by hand.

    File names are a readable slug of the key plus a hash of the exact key. The hash keeps two keys
    apart that differ only in case or punctuation (`a/b` and `a_b`, or `Fatigue` and `fatigue` on a
    case-insensitive disk). The key itself is stored inside the file and is the source of truth.
    Writes go to a temporary file first and are renamed into place, so a reader never sees half a file.
    """

    SUFFIX = ".prior.json"

    def __init__(self, directory: str | os.PathLike[str]) -> None:
        self._dir = Path(directory)
        self._dir.mkdir(parents=True, exist_ok=True)

    @property
    def directory(self) -> Path:
        return self._dir

    def path_for(self, key: str) -> Path:
        """Where the prior for `key` lives (whether or not it exists yet)."""
        slug = _UNSAFE.sub("_", key).strip("._")[:80] or "prior"
        digest = hashlib.sha256(key.encode("utf-8")).hexdigest()[:16]
        return self._dir / f"{slug}--{digest}{self.SUFFIX}"

    def get(self, key: str) -> Prior | None:
        path = self.path_for(key)
        if not path.exists():
            return None
        prior = Prior.model_validate_json(path.read_text(encoding="utf-8"))
        if prior.key != key:
            raise ValueError(f"{path} holds prior {prior.key!r}, not {key!r}; the directory is corrupt")
        return prior

    def put(self, prior: Prior) -> None:
        _check_finite(prior)
        target = self.path_for(prior.key)
        text = json.dumps(prior.model_dump(mode="json"), indent=2, sort_keys=True) + "\n"
        fd, tmp = tempfile.mkstemp(dir=self._dir, prefix=".tmp-", suffix=".json")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(text)
            Path(tmp).replace(target)
        except BaseException:
            Path(tmp).unlink(missing_ok=True)
            raise

    def keys(self, prefix: str = "") -> list[str]:
        found: list[str] = []
        for path in self._dir.glob(f"*{self.SUFFIX}"):
            key = Prior.model_validate_json(path.read_text(encoding="utf-8")).key
            if key.startswith(prefix):
                found.append(key)
        return sorted(found)
