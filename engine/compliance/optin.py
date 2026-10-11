"""The opt-in record of a channel that needs one: a grant on that channel itself (Plan K M134, DEC-1405 (e)).

WhatsApp's business messaging asks for the customer's opt-in to WhatsApp messages. The product's evidence of
one is the consent ledger: **a record whose `channel` is that channel**, for the run's client and purpose, whose
latest state as of the moment the treat list is built is a grant that has not expired. An all-channel record
(`channel` null) is consent to be contacted, not an opt-in to that channel, so it never counts here - though a
later all-channel withdrawal still closes the channel, through the run's per-channel contactability (M99).

Read only: nothing here writes to the ledger. Ids are hashed with the ledger's own salt (`ConsentLedger.hash`),
in chunks of 500 per lookup, as `ConsentLedger.classify` reads it.
"""

from __future__ import annotations

from collections.abc import Iterable
from datetime import datetime
from typing import TYPE_CHECKING, Final

if TYPE_CHECKING:
    from engine.privacy.consent import ConsentLedger

__all__ = ["channel_grants"]

_CHUNK: Final[int] = 500


def channel_grants(
    ledger: ConsentLedger,
    *,
    client_id: str,
    purpose: str,
    principals: Iterable[str],
    channel: str,
    at: datetime,
) -> dict[str, int]:
    """Per principal (as `principal_key` writes it) holding a valid grant on `channel` itself at `at`: the
    `seq` of that grant's record. A principal not in the answer has no opt-in record on the channel."""
    from sqlalchemy import select
    from sqlmodel import col

    from engine.privacy.consent import principal_key
    from engine.privacy.contracts import ConsentStatus
    from engine.privacy.tables import ConsentRecordRow
    from engine.registry import aware_utc, to_utc

    moment = to_utc(at)
    wanted = channel.strip().lower()
    by_hash: dict[str, list[str]] = {}
    for principal in {principal_key(value) for value in principals}:
        if principal:
            by_hash.setdefault(ledger.hash(principal), []).append(principal)
    hashes = list(by_hash)
    # Plain columns, not ORM rows: at a million customers, building a row object per record is most of the time.
    latest: dict[str, tuple[datetime, int, str, datetime | None]] = {}
    with ledger.engine.connect() as connection:
        for start in range(0, len(hashes), _CHUNK):
            statement = (
                select(
                    col(ConsentRecordRow.principal_hash),
                    col(ConsentRecordRow.channel),
                    col(ConsentRecordRow.recorded_at),
                    col(ConsentRecordRow.seq),
                    col(ConsentRecordRow.status),
                    col(ConsentRecordRow.expires_at),
                )
                .where(col(ConsentRecordRow.client_id) == client_id)
                .where(col(ConsentRecordRow.purpose) == purpose)
                .where(col(ConsentRecordRow.principal_hash).in_(hashes[start : start + _CHUNK]))
                .where(col(ConsentRecordRow.channel).is_not(None))
            )
            for hashed, row_channel, recorded_at, seq, status, expires_at in connection.execute(
                statement
            ).all():
                if row_channel is None or row_channel.strip().lower() != wanted:
                    continue
                recorded = aware_utc(recorded_at)
                if recorded > moment:
                    continue
                current = latest.get(hashed)
                if current is None or (recorded, seq or 0) > (current[0], current[1]):
                    latest[hashed] = (recorded, int(seq or 0), status, expires_at)
    granted: dict[str, int] = {}
    for hashed, (_recorded, seq, status, expires_at) in latest.items():
        if status != ConsentStatus.GRANTED.value:
            continue
        if expires_at is not None and aware_utc(expires_at) <= moment:
            continue
        for principal in by_hash[hashed]:
            granted[principal] = seq
    return granted
