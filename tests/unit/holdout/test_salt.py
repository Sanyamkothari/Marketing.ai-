"""The holdout salt and the epoch ledger (Plan J M92, DEC-1302 (b)).

Missing salt with a persistent scope -> `HOLDOUT_SALT_MISSING`; a changed fingerprint ->
`HOLDOUT_SALT_CHANGED`; raising the fraction keeps every member in the same epoch; lowering it is
refused until an Admin starts a new epoch; rotating the salt starts a new epoch of every holdout.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest
from sqlalchemy import text
from sqlalchemy.engine import Engine

from engine.holdout.assign import member_flags
from engine.holdout.salt import (
    FINGERPRINT_KEY,
    HoldoutLedger,
    resolve_holdout,
    salt_fingerprint,
    start_epoch,
)
from engine.holdout.spec import (
    HOLDOUT_FRACTION_LOWERED,
    HOLDOUT_SALT_CHANGED,
    HOLDOUT_SALT_MISSING,
    HoldoutError,
)
from engine.platform_db import PLATFORM_DB_FILENAME, sqlite_engine
from engine.settings import Settings
from tests.unit.holdout.support import OTHER_SALT, SALT, keys, use_case

AT = datetime(2026, 10, 7, 9, 0, tzinfo=UTC)


@pytest.fixture
def engine(tmp_path: Path) -> Engine:
    return sqlite_engine(tmp_path / PLATFORM_DB_FILENAME)


def settings(salt: str | None = SALT) -> Settings:
    return Settings.from_env({} if salt is None else {"MARKETING_AI_HOLDOUT_SALT": salt})


def stored_values(engine: Engine) -> list[str]:
    with engine.connect() as connection:
        return [row[0] for row in connection.execute(text("SELECT value FROM platform_setting"))]


def test_scope_run_reads_no_salt_and_no_database() -> None:
    resolved = resolve_holdout(use_case(), None, None, at=AT, record=True)
    assert resolved.active is None
    assert resolved.spec.scope == "run" and resolved.spec.epoch is None and resolved.spec.salt_id is None
    assert resolved.spec.fraction == 0.10


def test_a_persistent_scope_without_the_salt_is_refused(engine: Engine) -> None:
    with pytest.raises(HoldoutError) as caught:
        resolve_holdout(use_case(scope="universal", fraction=0.1), settings(None), engine, at=AT, record=True)
    assert caught.value.code == HOLDOUT_SALT_MISSING
    assert "MARKETING_AI_HOLDOUT_SALT" in caught.value.message


def test_the_first_use_records_a_fingerprint_and_epoch_one_never_the_salt(engine: Engine) -> None:
    config = use_case(scope="universal", fraction=0.1)
    resolved = resolve_holdout(config, settings(), engine, at=AT, record=True)
    assert resolved.spec.epoch == 1
    assert resolved.spec.scope_key == "universal"
    assert resolved.active is not None and resolved.active.fraction == 0.1
    ledger = HoldoutLedger(engine)
    assert ledger.fingerprint() == salt_fingerprint(SALT)
    entry = ledger.entry("universal", "universal")
    assert entry is not None and (entry.epoch, entry.fraction) == (1, 0.1)
    assert resolved.spec.salt_id == entry.salt_id == salt_fingerprint(SALT)[:16]
    assert all(SALT not in value for value in stored_values(engine)), "the salt itself is never stored"


def test_a_check_before_the_run_writes_nothing(engine: Engine) -> None:
    resolve_holdout(use_case(scope="universal", fraction=0.1), settings(), engine, at=AT, record=False)
    ledger = HoldoutLedger(engine)
    assert ledger.fingerprint() is None and ledger.entries() == ()


def test_a_changed_salt_refuses_scoring(engine: Engine) -> None:
    config = use_case(scope="universal", fraction=0.1)
    resolve_holdout(config, settings(), engine, at=AT, record=True)
    with pytest.raises(HoldoutError) as caught:
        resolve_holdout(config, settings(OTHER_SALT), engine, at=AT, record=True)
    assert caught.value.code == HOLDOUT_SALT_CHANGED
    assert HoldoutLedger(engine).fingerprint() == salt_fingerprint(SALT), "the recorded salt stays"


def test_raising_the_fraction_keeps_every_member_in_the_same_epoch(engine: Engine) -> None:
    resolve_holdout(use_case(scope="universal", fraction=0.05), settings(), engine, at=AT, record=True)
    raised = resolve_holdout(
        use_case(scope="universal", fraction=0.10), settings(), engine, at=AT, record=True
    )
    assert raised.spec.epoch == 1
    entry = HoldoutLedger(engine).entry("universal", "universal")
    assert entry is not None and entry.fraction == 0.10
    ids = keys(20_000)
    before = member_flags(ids, salt=SALT, scope_key="universal", fraction=0.05)
    after = member_flags(ids, salt=SALT, scope_key="universal", fraction=0.10)
    assert not bool((before & ~after).any()), "nobody held out at 5% is contacted at 10%"


def test_lowering_the_fraction_needs_an_admins_new_epoch(engine: Engine) -> None:
    resolve_holdout(use_case(scope="use_case", fraction=0.10), settings(), engine, at=AT, record=True)
    lower = use_case(scope="use_case", fraction=0.05)
    with pytest.raises(HoldoutError) as caught:
        resolve_holdout(lower, settings(), engine, at=AT, record=True)
    assert caught.value.code == HOLDOUT_FRACTION_LOWERED
    entry = start_epoch(
        HoldoutLedger(engine),
        scope="use_case",
        key=lower.id,
        fraction=0.05,
        settings=settings(),
        rotate_salt=False,
        at=AT,
    )
    assert (entry.epoch, entry.fraction) == (2, 0.05)
    assert resolve_holdout(lower, settings(), engine, at=AT, record=True).spec.epoch == 2


def test_an_epoch_cannot_be_started_with_an_unrecorded_salt_unless_it_is_a_rotation(engine: Engine) -> None:
    config = use_case(scope="universal", fraction=0.1)
    resolve_holdout(config, settings(), engine, at=AT, record=True)
    ledger = HoldoutLedger(engine)
    with pytest.raises(HoldoutError) as caught:
        start_epoch(
            ledger,
            scope="universal",
            key="universal",
            fraction=0.1,
            settings=settings(OTHER_SALT),
            rotate_salt=False,
            at=AT,
        )
    assert caught.value.code == HOLDOUT_SALT_CHANGED


def test_rotating_the_salt_starts_a_new_epoch_of_every_holdout(engine: Engine) -> None:
    universal = use_case(scope="universal", fraction=0.1)
    per_use_case = use_case("telco-churn", scope="use_case", fraction=0.2)
    resolve_holdout(universal, settings(), engine, at=AT, record=True)
    resolve_holdout(per_use_case, settings(), engine, at=AT, record=True)
    ledger = HoldoutLedger(engine)
    start_epoch(
        ledger,
        scope="universal",
        key="universal",
        fraction=0.1,
        settings=settings(OTHER_SALT),
        rotate_salt=True,
        at=AT,
    )
    assert ledger.fingerprint() == salt_fingerprint(OTHER_SALT)
    epochs = {(entry.scope_key, entry.epoch, entry.fraction) for entry in ledger.entries()}
    assert epochs == {("universal", 2, 0.1), ("telco-churn", 2, 0.2)}
    assert resolve_holdout(per_use_case, settings(OTHER_SALT), engine, at=AT, record=True).spec.epoch == 2
    with pytest.raises(HoldoutError) as caught:
        resolve_holdout(universal, settings(), engine, at=AT, record=True)
    assert caught.value.code == HOLDOUT_SALT_CHANGED, "the old salt is now the wrong one"


def test_the_fingerprint_lives_under_its_own_key(engine: Engine) -> None:
    resolve_holdout(use_case(scope="universal", fraction=0.1), settings(), engine, at=AT, record=True)
    with engine.connect() as connection:
        found = {row[0] for row in connection.execute(text("SELECT key FROM platform_setting"))}
    assert found == {FINGERPRINT_KEY, "holdout_epoch:universal:universal"}
    assert FINGERPRINT_KEY != "privacy_salt_fingerprint"
