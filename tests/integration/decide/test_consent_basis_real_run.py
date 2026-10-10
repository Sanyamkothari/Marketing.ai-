"""TRAI's consent-basis rules on real scoring runs (Plan K M131, DEC-1323).

Every run is the product's own: the uplift model `test_channel_consent_real_run` trains, scored through
`Pipeline.run_score` over a file with per-channel consent columns, for clients whose consent ledger (in
the store's `platform.db`) holds an all-channel grant for everyone, recorded through the CSV import with
the new `basis` and `operator_registered` columns. Nothing below writes a run artefact by hand: the treat
list is built from what the runs wrote. Each run reads `privacy.yaml`, the catalogue and the region file
from the config root `MARKETING_AI_CONFIG_DIR` names, as a deployment does.

With the catalogue's region (`configs/regions/in.yaml`) carrying a `consent_basis` block:

* a customer whose deciding grant is an inquiry consent older than 7 days, or a legacy consent not recorded
  as registered (false, or not recorded), is not contactable on sms, but is on email;
* they are not suppressed (`scores.csv`), the treat list sends them email, and
  `channel_contactability.json` counts them for sms only, with the rules' sha256.

With no catalogue region, or a region file without the block, the same ledger gives byte-for-byte what a
ledger without the new columns gives: the files carry no new key.
"""

# ruff: noqa: F811, F401 - the imported pytest fixtures are used by name
from __future__ import annotations

import io
import json
import shutil
from collections.abc import Iterator
from dataclasses import dataclass, replace
from datetime import timedelta
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
import pytest
import yaml

from engine.holdout.assign import selection_masks
from engine.platform_db import PLATFORM_DB_FILENAME, sqlite_engine
from engine.privacy.config import load_privacy_config
from engine.privacy.consent import ConsentLedger
from engine.storage import run_key
from engine.utils.time import utc_now
from tests.integration.decide.test_channel_consent_real_run import (
    CHANNELS,
    PRIMARY_KEY,
    PRIVACY_SALT,
    PURPOSE,
    Scored,
    World,
    _score,
    _scores,
    _treat_list,
    world,
)

pytestmark = pytest.mark.integration

GAMMA = "gamma"
"""The client whose ledger records a basis for some customers."""
DELTA = "delta"
"""The client whose ledger holds the same grants without the new columns: what a ledger was before M131."""
RUN_INDIA = "r_20261008_0e310001"
RUN_SAME = "r_20261008_0e310002"
CONTACTABILITY_PARQUET = "channel_contactability.parquet"
CONTACTABILITY_JSON = "channel_contactability.json"
CATALOGUE = """\
# A test catalogue: India's rules{region_note}; no action is named by the use case, so sms and email are both planned.
{region}actions:
  - action_id: winback_sms
    label: Win-back offer by SMS
    channels: [sms]
    offer_cost: 20.0
    contact_cost: 0.15
    dlt_template_id: "1107000000000000001"
    message_category: promotional
  - action_id: winback_email
    label: Win-back offer by email
    channels: [email]
    offer_cost: 20.0
    contact_cost: 0.02
"""
NEW_KEYS = ("inquiry_lapsed_rows", "legacy_unregistered_rows", "consent_basis_rules_sha256")

# The ledger's customers, by their position in the file (every other customer holds an explicit grant).
LAPSED, FRESH, LEGACY_FALSE, LEGACY_NULL, LEGACY_REGISTERED = 0, 1, 2, 3, 4
KINDS = 12


def _variant_root(source: Path, target: Path, *, region: bool, block: bool) -> Path:
    shutil.copytree(source, target)
    assert not (target / "decide" / "catalogue.yaml").exists(), "the repository ships no catalogue"
    text = CATALOGUE.format(
        region="region: IN\n" if region else "",
        region_note=" are applied" if region else " are not applied (no region)",
    )
    (target / "decide" / "catalogue.yaml").write_text(text, encoding="utf-8")
    if not block:
        path = target / "regions" / "in.yaml"
        document = yaml.safe_load(path.read_text(encoding="utf-8"))
        assert "consent_basis" in document, "the shipped India file carries the block"
        del document["consent_basis"]
        path.write_text(yaml.safe_dump(document, sort_keys=False), encoding="utf-8")
    path = target / "use_cases" / "win_back_campaign.yaml"
    document = yaml.safe_load(path.read_text(encoding="utf-8"))
    document.setdefault("actions", {}).setdefault("suppression", {})["channels"] = CHANNELS
    path.write_text(yaml.safe_dump(document, sort_keys=False), encoding="utf-8")
    return target


def _kind(position: int) -> int | None:
    return position % KINDS if position % KINDS <= LEGACY_REGISTERED else None


def _plant(data_dir: Path, frame: pd.DataFrame) -> dict[str, frozenset[str]]:
    """GAMMA: bases and registrations recorded through the CSV import. DELTA: the same grants, no new columns."""
    customers = [str(key) for key in frame[PRIMARY_KEY]]
    old = (utc_now() - timedelta(days=10)).date().isoformat()
    recent = (utc_now() - timedelta(days=3)).date().isoformat()
    long_ago = (utc_now() - timedelta(days=60)).date().isoformat()
    with_basis = ["principal_id,purpose,status,recorded_at,basis,operator_registered"]
    without = ["principal_id,purpose,status,recorded_at"]
    groups: dict[int, set[str]] = {kind: set() for kind in range(LEGACY_REGISTERED + 1)}
    for position, key in enumerate(customers):
        kind = _kind(position)
        recorded, basis, registered = {
            LAPSED: (old, "inquiry", ""),
            FRESH: (recent, "inquiry", ""),
            LEGACY_FALSE: (long_ago, "legacy", "false"),
            LEGACY_NULL: (long_ago, "legacy", ""),
            LEGACY_REGISTERED: (long_ago, "legacy", "yes"),
            None: (long_ago, "", ""),
        }[kind]
        if kind is not None:
            groups[kind].add(key)
        with_basis.append(f"{key},{PURPOSE},granted,{recorded},{basis},{registered}")
        without.append(f"{key},{PURPOSE},granted,{recorded}")
    ledger = ConsentLedger(sqlite_engine(data_dir / PLATFORM_DB_FILENAME), salt=PRIVACY_SALT)
    for client, lines in ((GAMMA, with_basis), (DELTA, without)):
        report = ledger.import_csv("\n".join(lines) + "\n", client_id=client, privacy=load_privacy_config())
        assert report.imported and not report.errors, report.errors
    return {
        "lapsed": frozenset(groups[LAPSED]),
        "fresh": frozenset(groups[FRESH]),
        "unregistered": frozenset(groups[LEGACY_FALSE] | groups[LEGACY_NULL]),
        "registered": frozenset(groups[LEGACY_REGISTERED]),
    }


@dataclass(frozen=True)
class Basis:
    world: World
    groups: dict[str, frozenset[str]]
    snapshot: Path
    """A copy of the data directory made before any run below: the same models, files and ledgers."""


@dataclass(frozen=True)
class Ran:
    """A run and the world (data directory) it ran in."""

    world: World
    run: Scored


@pytest.fixture(scope="module")
def basis(world: World, config_root: Path, tmp_path_factory: pytest.TempPathFactory) -> Basis:
    base = tmp_path_factory.mktemp("consent-basis")
    roots = {
        "india": _variant_root(config_root, base / "india", region=True, block=True),
        "no_region": _variant_root(config_root, base / "no_region", region=False, block=True),
        "no_block": _variant_root(config_root, base / "no_block", region=True, block=False),
    }
    groups = _plant(world.data_dir, world.frame)
    snapshot = base / "snapshot"
    shutil.copytree(world.data_dir, snapshot)
    return Basis(replace(world, roots={**world.roots, **roots}), groups, snapshot)


def _in_copy(basis: Basis, name: str, variant: str, client: str) -> Ran:
    """The run `RUN_SAME` in its own copy of the snapshot: a run's control group is seeded by its id, so two
    runs only compare byte for byte under one id."""
    copy = basis.snapshot.parent / name
    shutil.copytree(basis.snapshot, copy)
    world = replace(basis.world, data_dir=copy)
    return Ran(world, _score(world, RUN_SAME, variant, client=client))


@pytest.fixture(scope="module")
def india(basis: Basis) -> Scored:
    return _score(basis.world, RUN_INDIA, "india", client=GAMMA)


@pytest.fixture(scope="module")
def no_region(basis: Basis) -> Ran:
    return _in_copy(basis, "copy_no_region", "no_region", GAMMA)


@pytest.fixture(scope="module")
def no_block(basis: Basis) -> Ran:
    return _in_copy(basis, "copy_no_block", "no_block", GAMMA)


@pytest.fixture(scope="module")
def baseline(basis: Basis) -> Ran:
    """Today's behaviour: a ledger with no basis and no registration, under a catalogue with no region."""
    return _in_copy(basis, "copy_baseline", "no_region", DELTA)


@pytest.fixture(scope="module")
def baseline_india(basis: Basis) -> Ran:
    """The India root, under the same run id, over a ledger that records no basis: nothing for the rules to act on."""
    return _in_copy(basis, "copy_baseline_india", "india", DELTA)


def _contactability(world: World, run_id: str) -> pd.DataFrame:
    data = world.storage.read_bytes(run_key(run_id, CONTACTABILITY_PARQUET))
    return pd.read_parquet(io.BytesIO(data)).astype({PRIMARY_KEY: str}).set_index(PRIMARY_KEY)


def _summary(world: World, run_id: str) -> dict[str, object]:
    loaded: dict[str, object] = json.loads(world.storage.read_text(run_key(run_id, CONTACTABILITY_JSON)))
    return loaded


def _by_channel(summary: dict[str, object]) -> list[dict[str, Any]]:
    entries = summary["by_channel"]
    assert isinstance(entries, list)
    return [dict(entry) for entry in entries]


def _file_masks(world: World, keys: pd.Series) -> tuple[np.ndarray, np.ndarray]:
    """sms_ok and email_ok from the uploaded file's own columns."""
    uploaded = world.frame.astype({PRIMARY_KEY: str}).set_index(PRIMARY_KEY).loc[keys]
    sms = (uploaded["sms_opt_in"] == "true").to_numpy(dtype=bool)
    email = ((uploaded["email_opt_in"] == "yes") & (uploaded["email_valid"] == 1)).to_numpy(dtype=bool)
    return sms, email


# ---------------------------------------------------------------------------
# India: the rules take effect on sms only
# ---------------------------------------------------------------------------
def test_the_catalogue_names_the_region_whose_file_carries_the_rules(basis: Basis, india: Scored) -> None:
    from engine.decide.catalogue import region_consent_basis  # new in M131: imported here, not above

    rules = region_consent_basis(india.config_root)
    assert rules is not None
    assert (rules.channels, rules.inquiry_valid_days) == (("sms", "call", "ivr"), 7)
    assert rules.legacy_requires_operator_registration is True
    assert region_consent_basis(basis.world.roots["no_region"]) is None
    assert region_consent_basis(basis.world.roots["no_block"]) is None


def test_lapsed_and_unregistered_customers_are_not_contactable_on_sms_but_are_on_email(
    basis: Basis, india: Scored
) -> None:
    table = _contactability(basis.world, india.run_id)
    scores = _scores(basis.world, india.run_id)
    sms_file, email_file = _file_masks(basis.world, scores[PRIMARY_KEY])
    keys = scores[PRIMARY_KEY]
    taken = keys.isin(basis.groups["lapsed"] | basis.groups["unregistered"]).to_numpy(dtype=bool)
    assert taken.sum() > 200
    assert list(table.columns) == ["contactable_sms", "contactable_email"]
    got = table.loc[keys]
    assert (got["contactable_sms"].to_numpy() == (sms_file & ~taken)).all()
    assert (
        got["contactable_email"].to_numpy() == email_file
    ).all(), "email is a channel the rules do not list"
    kept = keys.isin(basis.groups["fresh"] | basis.groups["registered"]).to_numpy(dtype=bool)
    assert (
        got["contactable_sms"].to_numpy()[kept] == sms_file[kept]
    ).all(), "recent and registered still count"
    assert got["contactable_sms"].to_numpy()[kept & sms_file].all()


def test_they_are_not_suppressed(basis: Basis, india: Scored) -> None:
    text = basis.world.storage.read_bytes(run_key(india.run_id, "scores.csv")).decode("utf-8")
    scores = pd.read_csv(io.StringIO(text), dtype=str, keep_default_na=False).set_index(PRIMARY_KEY)
    taken = sorted(basis.groups["lapsed"] | basis.groups["unregistered"])
    assert (scores.loc[taken, "suppressed_reason"] == "").all(), "per-channel only, like any opt-out"


def test_the_treat_list_sends_them_email(basis: Basis, india: Scored) -> None:
    out, summary = _treat_list(basis.world, india)
    out = out.set_index(PRIMARY_KEY)
    scores = _scores(basis.world, india.run_id).set_index(PRIMARY_KEY)
    selected, sleeping = selection_masks(scores.reset_index(), india.resolved.config)
    eligible = scores["suppressed_reason"].isna() & ~scores["control_group"].astype(bool)
    base = pd.Series(selected & ~sleeping, index=scores.index) & eligible
    sms_file, email_file = _file_masks(basis.world, pd.Series(scores.index))
    email_ok = pd.Series(email_file, index=scores.index)
    sms_ok = pd.Series(sms_file, index=scores.index)
    taken = pd.Series(
        scores.index.isin(sorted(basis.groups["lapsed"] | basis.groups["unregistered"])), index=scores.index
    )
    treat = out.loc[scores.index, "treat"] == "1"
    channel = out.loc[scores.index, "channel"]
    assert (treat == (base & ((sms_ok & ~taken) | email_ok))).all()
    hit = base & taken & email_ok
    assert hit.sum() > 20
    assert (channel[hit] == "email").all() and treat[hit].all()
    assert (channel[treat & sms_ok & ~taken] == "sms").all(), "everyone else keeps SMS first"
    only_sms = base & taken & ~email_ok
    assert only_sms.any() and not treat[only_sms].any(), "nothing else to send them on"
    contactable = out.loc[scores.index, "contactable_channels"]
    assert not contactable[taken].str.contains("sms").any()
    assert summary.channel_counts is not None
    assert summary.channel_counts["sms"] == int((eligible & ~(sms_ok & ~taken)).sum())


def test_the_summary_counts_the_two_rules_for_sms_only_and_records_the_rules(
    basis: Basis, india: Scored
) -> None:
    scores = _scores(basis.world, india.run_id).set_index(PRIMARY_KEY)
    eligible = scores["suppressed_reason"].isna() & ~scores["control_group"].astype(bool)
    lapsed = int(eligible[scores.index.isin(sorted(basis.groups["lapsed"]))].sum())
    unregistered = int(eligible[scores.index.isin(sorted(basis.groups["unregistered"]))].sum())
    assert lapsed > 20 and unregistered > 40
    summary = _summary(basis.world, india.run_id)
    sms, email = _by_channel(summary)
    assert (sms["channel"], email["channel"]) == ("sms", "email")
    assert (sms["inquiry_lapsed_rows"], sms["legacy_unregistered_rows"]) == (lapsed, unregistered)
    assert "inquiry_lapsed_rows" not in email and "legacy_unregistered_rows" not in email
    from engine.decide.catalogue import region_consent_basis

    rules = region_consent_basis(india.config_root)
    assert rules is not None
    assert summary["consent_basis_rules_sha256"] == rules.sha256()
    assert sms["not_contactable_eligible_rows"] >= lapsed + unregistered


def test_a_ledger_that_records_no_basis_gives_the_same_flags_under_india_s_rules(
    baseline: Ran, baseline_india: Ran
) -> None:
    storage = baseline.world.storage
    name = run_key(RUN_SAME, CONTACTABILITY_PARQUET)
    assert baseline_india.world.storage.read_bytes(name) == storage.read_bytes(
        name
    ), "nothing recorded, nothing taken"
    summary = _summary(baseline_india.world, RUN_SAME)
    sms, email = _by_channel(summary)
    assert (sms["inquiry_lapsed_rows"], sms["legacy_unregistered_rows"]) == (0, 0)
    assert "inquiry_lapsed_rows" not in email


def test_the_rules_take_customers_away_from_sms(basis: Basis, india: Scored, baseline_india: Ran) -> None:
    taken = _contactability(basis.world, india.run_id)["contactable_sms"].sum()
    assert taken < _contactability(baseline_india.world, RUN_SAME)["contactable_sms"].sum()


# ---------------------------------------------------------------------------
# No region, or no block: today's outputs, byte for byte
# ---------------------------------------------------------------------------
def _without_run_identity(text: str) -> dict[str, object]:
    document: dict[str, object] = json.loads(text)
    for key in ("run_id", "created_at", "scored_at"):
        document.pop(key, None)
    return document


@pytest.mark.parametrize("variant", ["no_region", "no_block"])
def test_with_no_region_or_no_block_the_run_is_what_it_was(
    baseline: Ran, no_region: Ran, no_block: Ran, variant: str
) -> None:
    """The same run id over the same files, once with the bases recorded and once without: identical files."""
    mine = {"no_region": no_region, "no_block": no_block}[variant]
    for name in (CONTACTABILITY_PARQUET, "scores.csv", "scores.parquet"):
        key = run_key(RUN_SAME, name)
        assert mine.world.storage.read_bytes(key) == baseline.world.storage.read_bytes(key), name
    for name in (CONTACTABILITY_JSON, "scoring_summary.json"):  # these carry the time they were written
        text = mine.world.storage.read_text(run_key(RUN_SAME, name))
        assert not any(key in text for key in NEW_KEYS)
        assert _without_run_identity(text) == _without_run_identity(
            baseline.world.storage.read_text(run_key(RUN_SAME, name))
        ), name
    out, summary = _treat_list(mine.world, mine.run)
    base_out, base_summary = _treat_list(baseline.world, baseline.run)
    assert out.equals(base_out)
    assert summary.channel_counts == base_summary.channel_counts
    assert mine.world.storage.read_bytes(
        run_key(RUN_SAME, "treat_list.csv")
    ) == baseline.world.storage.read_bytes(run_key(RUN_SAME, "treat_list.csv"))


def test_the_lapsed_inquiry_customers_are_contactable_by_sms_without_the_rules(
    basis: Basis, no_region: Ran, no_block: Ran
) -> None:
    for ran in (no_region, no_block):
        table = _contactability(ran.world, RUN_SAME)
        scores = _scores(ran.world, RUN_SAME)
        sms_file, _ = _file_masks(basis.world, scores[PRIMARY_KEY])
        assert (table.loc[scores[PRIMARY_KEY], "contactable_sms"].to_numpy() == sms_file).all()
        lapsed = table.loc[sorted(basis.groups["lapsed"]), "contactable_sms"]
        assert lapsed.any(), "an inquiry consent ten days old still counts where no rule applies"
