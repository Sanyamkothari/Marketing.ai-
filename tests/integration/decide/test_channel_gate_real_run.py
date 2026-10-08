"""Plan J M100 part B: a customer whose consent is recorded per channel only, on real scoring runs.

The M99 gap DEC-1309 left open: the scoring gate asks the consent ledger an all-channel question, and a
customer whose records are all channel-specific has no all-channel record, so every such customer was
suppressed as `consent_false`, even with a valid grant on a channel the use case sends by. With
`actions.suppression.channels` configured, the gate now passes a customer with a valid grant on at least
one configured channel and no newer all-channel withdrawal; per-channel contactability then decides the
channel. Without channels, the run is what it was.

The runs are the product's own (`Pipeline.run_score` on the uplift model `test_channel_consent_real_run`
trains), for a second client, `beta`, whose ledger holds only channel-specific records. The tests fail
on 91cc0b4, where the channels run suppresses every one of beta's customers.
"""

# ruff: noqa: F811, F401 - the imported pytest fixtures are used by name
from __future__ import annotations

import io
from datetime import timedelta

import pandas as pd
import pytest

from engine.platform_db import PLATFORM_DB_FILENAME, sqlite_engine
from engine.privacy.config import load_privacy_config
from engine.privacy.consent import ConsentLedger
from engine.storage import run_key
from engine.utils.time import utc_now
from tests.integration.decide.test_channel_consent_real_run import (
    PRIMARY_KEY,
    PRIVACY_SALT,
    PURPOSE,
    Scored,
    World,
    _score,
    _treat_list,
    world,
)

pytestmark = pytest.mark.integration

BETA = "beta"
RUN_CHANNELS = "r_20261008_0e300001"
RUN_PLAIN = "r_20261008_0e300002"


@pytest.fixture(scope="module")
def beta(world: World) -> dict[str, str]:
    """Beta's ledger: an SMS grant for every third customer, an email grant for the next, nothing for the
    rest; and for some SMS-granted customers an all-channel withdrawal newer than the grant."""
    customers = [str(key) for key in world.frame[PRIMARY_KEY]]
    granted_at = (utc_now() - timedelta(days=30)).date().isoformat()
    withdrawn_at = (utc_now() - timedelta(days=3)).date().isoformat()
    kind: dict[str, str] = {}
    lines = ["principal_id,purpose,status,recorded_at,channel"]
    for index, key in enumerate(customers):
        if index % 3 == 0:
            lines.append(f"{key},{PURPOSE},granted,{granted_at},sms")
            kind[key] = "sms"
            if index % 9 == 0:
                lines.append(f"{key},{PURPOSE},withdrawn,{withdrawn_at},")
                kind[key] = "withdrawn"
        elif index % 3 == 1:
            lines.append(f"{key},{PURPOSE},granted,{granted_at},email")
            kind[key] = "email"
        else:
            kind[key] = "none"
    ledger = ConsentLedger(sqlite_engine(world.data_dir / PLATFORM_DB_FILENAME), salt=PRIVACY_SALT)
    report = ledger.import_csv("\n".join(lines) + "\n", client_id=BETA, privacy=load_privacy_config())
    assert report.imported and not report.errors, report.errors
    return kind


@pytest.fixture(scope="module")
def channels_run(world: World, beta: dict[str, str]) -> Scored:
    return _score(world, RUN_CHANNELS, "channels", client=BETA)


@pytest.fixture(scope="module")
def plain_run(world: World, beta: dict[str, str]) -> Scored:
    return _score(world, RUN_PLAIN, "plain", client=BETA)


def _suppressed(world: World, run_id: str) -> pd.Series:
    text = world.storage.read_bytes(run_key(run_id, "scores.csv")).decode("utf-8")
    scores = pd.read_csv(io.StringIO(text), dtype=str, keep_default_na=False)
    return scores.set_index(PRIMARY_KEY)["suppressed_reason"]


def test_a_channel_only_grant_passes_the_gate_when_channels_are_configured(
    world: World, beta: dict[str, str], channels_run: Scored
) -> None:
    reason = _suppressed(world, channels_run.run_id)
    kind = pd.Series(beta).reindex(reason.index)
    assert ((reason == "consent_false") == kind.isin(["none", "withdrawn"])).all()
    assert (kind == "sms").sum() > 100 and (kind == "email").sum() > 100


def test_per_channel_contactability_then_decides_the_channel(
    world: World, beta: dict[str, str], channels_run: Scored
) -> None:
    out, _ = _treat_list(world, channels_run)
    out = out.set_index(PRIMARY_KEY)
    kind = pd.Series(beta).reindex(out.index)
    open_ = out["contactable_channels"].str.split(",")
    assert not open_[kind == "sms"].map(lambda names: "email" in names).any(), "no email grant"
    assert not open_[kind == "email"].map(lambda names: "sms" in names).any(), "no SMS grant"
    assert open_[kind == "sms"].map(lambda names: "sms" in names).any()
    assert open_[kind == "email"].map(lambda names: "email" in names).any()
    treated = out["treat"] == "1"
    assert treated.any()
    assert (out.loc[treated & (kind == "sms"), "channel"] == "sms").all()
    assert (out.loc[treated & (kind == "email"), "channel"] == "email").all()
    assert not treated[kind.isin(["none", "withdrawn"])].any()


def test_without_channels_the_all_channel_question_is_unchanged(
    world: World, beta: dict[str, str], plain_run: Scored
) -> None:
    reason = _suppressed(world, plain_run.run_id)
    assert (reason == "consent_false").all(), "no all-channel record: suppressed, exactly as before"
