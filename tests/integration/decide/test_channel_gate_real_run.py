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

The second review's blocker: once a channel grant alone passes the gate, a channel the use case does not
configure (one a catalogue action plans, such as push) must not be open to that customer, as it was for
everyone in M99. A run whose treat action is planned on push only treats none of beta's customers, and
still treats `acme`'s, whose consent is all-channel. Those tests fail on 53af5d5.
"""

# ruff: noqa: F811, F401 - the imported pytest fixtures are used by name
from __future__ import annotations

import io
import shutil
from dataclasses import replace
from datetime import timedelta
from pathlib import Path

import pandas as pd
import pytest
import yaml

from engine.platform_db import PLATFORM_DB_FILENAME, sqlite_engine
from engine.privacy.config import load_privacy_config
from engine.privacy.consent import ConsentLedger
from engine.storage import run_key
from engine.utils.time import utc_now
from tests.integration.decide.test_channel_consent_real_run import (
    CHANNELS,
    CLIENT,
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
RUN_PUSH_BETA = "r_20261008_0e300003"
RUN_PUSH_ACME = "r_20261008_0e300004"
PUSH_CATALOGUE = """\
# A test catalogue: the win-back offer sent only by push, a channel the use case does not configure.
actions:
  - action_id: winback_push
    label: Win-back offer by push
    channels: [push]
    offer_cost: 20.0
    contact_cost: 0.05
"""


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


# ---------------------------------------------------------------------------
# A channel the use case does not configure is open only to all-channel consent
# ---------------------------------------------------------------------------
@pytest.fixture(scope="module")
def push_world(world: World, config_root: Path, tmp_path_factory: pytest.TempPathFactory) -> World:
    """The channels root, with the uplift treat action planned on push only."""
    target = tmp_path_factory.mktemp("channel-gate-push") / "configs"
    shutil.copytree(config_root, target)
    (target / "decide" / "catalogue.yaml").write_text(PUSH_CATALOGUE, encoding="utf-8")
    path = target / "use_cases" / "win_back_campaign.yaml"
    document = yaml.safe_load(path.read_text(encoding="utf-8"))
    document.setdefault("actions", {}).setdefault("suppression", {})["channels"] = CHANNELS
    document.setdefault("uplift", {}).setdefault("policy", {})["treat_action_id"] = "winback_push"
    path.write_text(yaml.safe_dump(document, sort_keys=False), encoding="utf-8")
    return replace(world, roots={**world.roots, "push": target})


def _contactability(world: World, run_id: str) -> pd.DataFrame:
    data = world.storage.read_bytes(run_key(run_id, "channel_contactability.parquet"))
    return pd.read_parquet(io.BytesIO(data)).astype({PRIMARY_KEY: str}).set_index(PRIMARY_KEY)


def test_a_channel_grant_alone_never_opens_a_channel_the_use_case_does_not_configure(
    push_world: World, beta: dict[str, str]
) -> None:
    run = _score(push_world, RUN_PUSH_BETA, "push", client=BETA)
    out, summary = _treat_list(push_world, run)
    out = out.set_index(PRIMARY_KEY)
    kind = pd.Series(beta).reindex(out.index)
    passed = kind.isin(["sms", "email"])
    assert (out.loc[passed, "suppression_reason"] == "").all(), "they passed the gate"
    # The treat action is planned on push only, and none of them consented to push (or to every channel).
    assert not (out["treat"] == "1").any()
    assert not (out["channel"] == "push").any()
    assert summary.uncontactable_rows is not None and summary.uncontactable_rows > 0
    table = _contactability(push_world, RUN_PUSH_BETA)
    assert not table["all_channel_consent"].any()


def test_all_channel_consent_still_opens_a_channel_the_use_case_does_not_configure(
    push_world: World, beta: dict[str, str]
) -> None:
    run = _score(push_world, RUN_PUSH_ACME, "push", client=CLIENT)
    out, _ = _treat_list(push_world, run)
    treated = out["treat"] == "1"
    assert treated.sum() > 50
    assert (out.loc[treated, "channel"] == "push").all()
    # Everyone's consent is all-channel, so the run records nothing new: M99's file, unchanged.
    assert list(_contactability(push_world, RUN_PUSH_ACME).columns) == [
        "contactable_sms",
        "contactable_email",
    ]
