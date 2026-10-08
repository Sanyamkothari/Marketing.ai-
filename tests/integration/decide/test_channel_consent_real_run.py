"""Channel-aware consent on real scoring runs (Plan J M99, DEC-1309; Claude Code's review fix).

Every run here is the product's own: an uplift model trained through `Pipeline.run_train` (LightGBM,
50 bootstrap resamples, as `tests/integration/uplift/test_uplift_consent.py` trains it), then scored
through `Pipeline.run_score` over a file that carries per-channel consent columns, with a consent
ledger in the store's `platform.db` holding an all-channel grant for everyone (a record from before
M99's `channel` column) and channel-specific SMS withdrawals for some. Nothing below writes
`scores.parquet` or any other run artefact by hand: the treat list is built from what the runs wrote.

The M99 review found that the treat list looked for the consent columns in `scores.parquet`, which a
real run never writes (`scores_csv_columns`, DEC-083), so every customer counted as contactable on
every channel, and that the ledger's channel was never consulted. These tests fail on 01e923e.

* opted out of SMS (in the file, or by a channel-specific ledger record) but in for email, both
  planned: treated only by email;
* only SMS planned and opted out of it: not treated, counted in `channel_counts`, not suppressed in
  `scores.csv`;
* a rule that did not run gets no `SuppressionCount` entry;
* a run without `actions.suppression.channels` writes nothing new.
"""

from __future__ import annotations

import io
import json
import shutil
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
import pytest
import yaml
from pydantic import SecretStr

from engine.config import ResolvedConfig, RunMode, resolve_config
from engine.contracts import RunRecord, RunState, ScoringSummary
from engine.decide.treat_list import TREAT_LIST_CSV, build_treat_list
from engine.holdout.assign import selection_masks
from engine.jobs import CancelToken, NullJobRunner
from engine.pipeline import Pipeline, StageContext
from engine.platform_db import PLATFORM_DB_FILENAME, sqlite_engine
from engine.privacy.config import load_privacy_config, privacy_salt
from engine.privacy.consent import ConsentLedger
from engine.registry import LocalModelRegistry
from engine.settings import Settings
from engine.storage import LocalStorage, run_key, upload_key
from engine.utils.time import utc_now
from tests.fixtures.make_uplift_data import make_uplift_data, make_winback_campaign

pytestmark = pytest.mark.integration

USE_CASE = "win-back-campaign"
PURPOSE = "marketing_communication"
PRIMARY_KEY = "customer_id"
TARGET = "reactivated_90d"
TREATMENT = "treatment"
CLIENT = "acme"
PRIVACY_SALT = "channel-consent-salt-01"
TRAIN_ROWS = 6_000
SCORE_ROWS = 1_200
TRAIN_RUN = "r_20261008_0c000001"
RUN_BOTH = "r_20261008_0c000002"
RUN_SMS_ONLY = "r_20261008_0c000003"
RUN_NO_LEDGER = "r_20261008_0c000004"
RUN_DEFAULT = "r_20261008_0c000005"
CONTACTABILITY_PARQUET = "channel_contactability.parquet"
CONTACTABILITY_JSON = "channel_contactability.json"

UPLIFT: dict[str, Any] = {
    "problem_type": "uplift",
    "uplift": {
        "treatment_column": TREATMENT,
        "base_model": "lightgbm",
        "bootstrap_samples": 50,
        "min_arm_rows": 200,
        "min_arm_positives": 20,
    },
    "governance": {"approval_required": False},
}
CHANNELS: dict[str, Any] = {
    "sms": {"consent_column": "sms_opt_in"},
    "email": {"consent_column": "email_opt_in", "contactable_column": "email_valid"},
}
CATALOGUE = """\
# A test catalogue: an SMS-only action, nothing else.
actions:
  - action_id: winback_sms
    label: Win-back offer by SMS
    channel: sms
    offer_cost: 20.0
    contact_cost: 0.15
"""


VARIANTS: dict[str, tuple[bool, bool]] = {
    "plain": (False, False),
    "channels": (True, False),
    "sms_only": (True, True),
}
"""Config roots by name: `(actions.suppression.channels set, uplift.policy.treat_action_id set)`.

Both are use-case settings, not per-run overrides, so each variant is its own copy of `configs/`."""


def _variant_root(source: Path, target: Path, *, channels: bool, sms_only: bool) -> Path:
    shutil.copytree(source, target)
    (target / "decide" / "catalogue.yaml").write_text(CATALOGUE, encoding="utf-8")
    path = target / "use_cases" / "win_back_campaign.yaml"
    document = yaml.safe_load(path.read_text(encoding="utf-8"))
    if channels:
        document.setdefault("actions", {}).setdefault("suppression", {})["channels"] = CHANNELS
    if sms_only:
        document.setdefault("uplift", {}).setdefault("policy", {})["treat_action_id"] = "winback_sms"
    path.write_text(yaml.safe_dump(document, sort_keys=False), encoding="utf-8")
    return target


@dataclass(frozen=True)
class World:
    data_dir: Path
    roots: dict[str, Path]
    model_version_id: str
    frame: pd.DataFrame
    sms_file_out: frozenset[str]
    sms_ledger_out: frozenset[str]

    @property
    def storage(self) -> LocalStorage:
        return LocalStorage(self.data_dir)

    @property
    def registry(self) -> LocalModelRegistry:
        return LocalModelRegistry(self.data_dir / "registry.db")


@dataclass(frozen=True)
class Scored:
    run_id: str
    record: RunRecord
    resolved: ResolvedConfig
    config_root: Path


def _context(
    world_storage: LocalStorage,
    registry: LocalModelRegistry,
    resolved: ResolvedConfig,
    run_id: str,
    *,
    mode: RunMode,
    upload: str,
    target: str | None,
    model_version_id: str | None,
) -> StageContext:
    return StageContext(
        run_id=run_id,
        mode=mode,
        config=resolved.config,
        resolved=resolved,
        storage=world_storage,
        registry=registry,
        cancel=CancelToken(),
        primary_key=[PRIMARY_KEY],
        target=target,
        upload_key=upload,
        model_version_id=model_version_id,
    )


def _write_csv(storage: LocalStorage, key: str, frame: pd.DataFrame) -> None:
    storage.write_bytes(key, frame.to_csv(index=False, lineterminator="\n").encode("utf-8"))


def _scoring_frame() -> pd.DataFrame:
    """The campaign file, with per-channel consent columns a client would send."""
    campaign = make_winback_campaign(SCORE_ROWS, seed=11).frame
    frame = campaign.drop(columns=[TARGET, TREATMENT, "treatment_date"], errors="ignore").reset_index(
        drop=True
    )
    position = np.arange(len(frame.index))
    frame["sms_opt_in"] = np.where(position % 4 == 1, "false", "true")
    frame["email_opt_in"] = np.where(position % 5 == 2, "no", "yes")
    frame["email_valid"] = np.where(position % 11 == 7, 0, 1)
    return frame


def _plant_ledger(data_dir: Path, frame: pd.DataFrame) -> frozenset[str]:
    """An all-channel grant for everyone, then an SMS-only withdrawal for some still opted in by file.

    The grant has no `channel` (as every record from before M99 has none), so it applies to every
    channel; the withdrawal names `sms`, so it must take SMS away and leave email. Returns the
    customers whose SMS opt-out comes from the ledger alone.
    """
    customers = [str(key) for key in frame[PRIMARY_KEY]]
    opted_in_by_file = set(frame.loc[frame["sms_opt_in"] == "true", PRIMARY_KEY].astype(str))
    ledger_out = {key for index, key in enumerate(customers) if index % 6 == 3} & opted_in_by_file
    granted_at = (utc_now() - timedelta(days=30)).date().isoformat()
    withdrawn_at = (utc_now() - timedelta(days=3)).date().isoformat()
    grants = ["principal_id,purpose,status,recorded_at"]
    grants += [f"{key},{PURPOSE},granted,{granted_at}" for key in customers]
    withdrawals = ["principal_id,purpose,status,recorded_at,channel"]
    withdrawals += [f"{key},{PURPOSE},withdrawn,{withdrawn_at},SMS" for key in sorted(ledger_out)]
    engine = sqlite_engine(data_dir / PLATFORM_DB_FILENAME)
    ledger = ConsentLedger(engine, salt=PRIVACY_SALT)
    assert privacy_salt(Settings(privacy_salt=SecretStr(PRIVACY_SALT)), engine=engine) == PRIVACY_SALT
    for lines in (grants, withdrawals):
        report = ledger.import_csv("\n".join(lines) + "\n", client_id=CLIENT, privacy=load_privacy_config())
        assert report.imported and not report.errors, report.errors
    return frozenset(ledger_out)


@pytest.fixture(scope="module")
def world(config_root: Path, tmp_path_factory: pytest.TempPathFactory) -> Iterator[World]:
    base = tmp_path_factory.mktemp("channel-consent")
    roots = {
        name: _variant_root(config_root, base / name, channels=channels, sms_only=sms_only)
        for name, (channels, sms_only) in VARIANTS.items()
    }
    data_dir = base / "data"
    data_dir.mkdir()
    with pytest.MonkeyPatch.context() as patch:
        patch.setenv("MARKETING_AI_CLIENT_ID", CLIENT)
        patch.setenv("MARKETING_AI_PRIVACY_SALT", PRIVACY_SALT)
        resolved = resolve_config(USE_CASE, UPLIFT, root=roots["plain"])
        storage = LocalStorage(data_dir)
        registry = LocalModelRegistry(data_dir / "registry.db")
        train_upload = upload_key("u_0c0000000001", "source.csv")
        _write_csv(storage, train_upload, make_uplift_data(TRAIN_ROWS, seed=7).frame)
        context = _context(
            storage,
            registry,
            resolved,
            TRAIN_RUN,
            mode=RunMode.TRAIN,
            upload=train_upload,
            target=TARGET,
            model_version_id=None,
        )
        record = Pipeline(storage, registry, NullJobRunner()).run_train(context)
        assert record.state is RunState.DONE, record.error
        assert record.model_version_id is not None
        frame = _scoring_frame()
        ledger_out = _plant_ledger(data_dir, frame)
        file_out = frozenset(frame.loc[frame["sms_opt_in"] == "false", PRIMARY_KEY].astype(str))
        yield World(data_dir, roots, record.model_version_id, frame, file_out, ledger_out)


def _score(world: World, run_id: str, variant: str, *, client: str | None) -> Scored:
    with pytest.MonkeyPatch.context() as patch:
        if client is None:
            patch.delenv("MARKETING_AI_CLIENT_ID", raising=False)
        else:
            patch.setenv("MARKETING_AI_CLIENT_ID", client)
        patch.setenv("MARKETING_AI_PRIVACY_SALT", PRIVACY_SALT)
        resolved = resolve_config(USE_CASE, UPLIFT, root=world.roots[variant])
        storage = world.storage
        storage.write_model(run_key(run_id, "run_config.json"), resolved)  # as `POST /runs` does
        source = upload_key(f"u_{run_id[-8:]}", "source.csv")
        _write_csv(storage, source, world.frame)
        context = _context(
            storage,
            world.registry,
            resolved,
            run_id,
            mode=RunMode.SCORE,
            upload=source,
            target=None,
            model_version_id=world.model_version_id,
        )
        record = Pipeline(storage, world.registry, NullJobRunner()).run_score(context)
        assert record.state is RunState.DONE, record.error
        return Scored(run_id, record, resolved, world.roots[variant])


@pytest.fixture(scope="module")
def both(world: World) -> Scored:
    """SMS and email configured; the treat action names no catalogue action, so both are planned."""
    return _score(world, RUN_BOTH, "channels", client=CLIENT)


@pytest.fixture(scope="module")
def sms_only(world: World) -> Scored:
    """The same channels; the treat action is the catalogue's SMS-only action."""
    return _score(world, RUN_SMS_ONLY, "sms_only", client=CLIENT)


@pytest.fixture(scope="module")
def no_ledger(world: World) -> Scored:
    """Channels configured, but no client is named, so no ledger gates the run and no rule runs."""
    return _score(world, RUN_NO_LEDGER, "channels", client=None)


@pytest.fixture(scope="module")
def default(world: World) -> Scored:
    """The default configuration: no `actions.suppression.channels`."""
    return _score(world, RUN_DEFAULT, "plain", client=CLIENT)


def _scores(world: World, run_id: str) -> pd.DataFrame:
    data = world.storage.read_bytes(run_key(run_id, "scores.parquet"))
    return pd.read_parquet(io.BytesIO(data)).astype({PRIMARY_KEY: str})


def _treat_list(world: World, run: Scored) -> tuple[pd.DataFrame, Any]:
    summary = build_treat_list(world.storage, run.run_id, config_root=run.config_root)
    text = world.storage.read_bytes(run_key(run.run_id, TREAT_LIST_CSV)).decode("utf-8")
    return pd.read_csv(io.StringIO(text), dtype=str, keep_default_na=False), summary


@dataclass(frozen=True)
class Masks:
    base: np.ndarray
    """Treated before M99: selected by the policy, eligible, not control, not a sleeping dog."""
    eligible: np.ndarray
    """Neither suppressed nor control: the rows channel_counts counts."""
    sms_ok: np.ndarray
    email_ok: np.ndarray
    file_out: np.ndarray
    ledger_out: np.ndarray


def _masks(world: World, run: Scored, out: pd.DataFrame) -> Masks:
    scores = _scores(world, run.run_id)
    assert list(scores[PRIMARY_KEY]) == list(out[PRIMARY_KEY])
    uploaded = world.frame.astype({PRIMARY_KEY: str}).set_index(PRIMARY_KEY).loc[scores[PRIMARY_KEY]]
    selected, sleeping = selection_masks(scores, run.resolved.config)
    suppressed = scores["suppressed_reason"].notna().to_numpy(dtype=bool)
    control = scores["control_group"].astype(bool).to_numpy(dtype=bool)
    keys = scores[PRIMARY_KEY]
    file_out = keys.isin(world.sms_file_out).to_numpy(dtype=bool)
    ledger_out = keys.isin(world.sms_ledger_out).to_numpy(dtype=bool)
    email_ok = ((uploaded["email_opt_in"] == "yes") & (uploaded["email_valid"] == 1)).to_numpy(dtype=bool)
    eligible = ~suppressed & ~control
    return Masks(
        base=selected & eligible & ~sleeping,
        eligible=eligible,
        sms_ok=~file_out & ~ledger_out,
        email_ok=email_ok,
        file_out=file_out,
        ledger_out=ledger_out,
    )


# ---------------------------------------------------------------------------
# SMS and email both planned
# ---------------------------------------------------------------------------
def test_opted_out_of_sms_but_in_for_email_is_treated_only_by_email(world: World, both: Scored) -> None:
    out, _ = _treat_list(world, both)
    m = _masks(world, both, out)
    treat = (out["treat"] == "1").to_numpy()
    assert (treat == (m.base & (m.sms_ok | m.email_ok))).all()
    channel = out["channel"].to_numpy(dtype=object)
    assert (channel[treat & m.sms_ok] == "sms").all()
    assert (channel[treat & ~m.sms_ok] == "email").all()
    assert (channel[~treat] == "").all(), "an untreated customer has no channel"
    # Both sources of an SMS opt-out are honoured, each on customers the policy would treat.
    by_file = m.base & m.file_out & m.email_ok
    by_ledger = m.base & m.ledger_out & m.email_ok
    assert by_file.any() and by_ledger.any()
    assert (channel[by_file] == "email").all() and (channel[by_ledger] == "email").all()


def test_contactable_channels_are_the_run_s_own(world: World, both: Scored) -> None:
    out, _ = _treat_list(world, both)
    m = _masks(world, both, out)
    expected = [
        ",".join(name for name, ok in (("sms", sms), ("email", email)) if ok)
        for sms, email in zip(m.sms_ok, m.email_ok, strict=True)
    ]
    assert list(out["contactable_channels"]) == expected


def test_a_channel_opt_out_suppresses_nobody_in_scores_csv(world: World, both: Scored) -> None:
    text = world.storage.read_bytes(run_key(both.run_id, "scores.csv")).decode("utf-8")
    scores = pd.read_csv(io.StringIO(text), dtype=str, keep_default_na=False)
    assert not {"sms_opt_in", "email_opt_in", "email_valid", "contactable_channels"} & set(scores.columns)
    # Everyone holds an all-channel grant, so the ledger's row-level rule suppresses nobody.
    assert (scores["suppressed_reason"] == "").all()


def test_the_contactability_artefact_is_row_level_and_keyed(world: World, both: Scored) -> None:
    table = pd.read_parquet(
        io.BytesIO(world.storage.read_bytes(run_key(both.run_id, CONTACTABILITY_PARQUET)))
    )
    assert list(table.columns) == [PRIMARY_KEY, "contactable_sms", "contactable_email"]
    assert len(table.index) == SCORE_ROWS and table[PRIMARY_KEY].is_unique
    assert both.record.artefacts[CONTACTABILITY_PARQUET] == run_key(both.run_id, CONTACTABILITY_PARQUET)
    assert "sms_opt_in" not in _scores(world, both.run_id).columns, "scores.parquet is unchanged"


# ---------------------------------------------------------------------------
# Only SMS planned
# ---------------------------------------------------------------------------
def test_only_sms_planned_and_opted_out_is_not_treated_and_is_counted(world: World, sms_only: Scored) -> None:
    out, summary = _treat_list(world, sms_only)
    m = _masks(world, sms_only, out)
    treat = (out["treat"] == "1").to_numpy()
    assert (treat == (m.base & m.sms_ok)).all()
    assert (out.loc[treat, "channel"] == "sms").all()
    lost = m.base & ~m.sms_ok
    assert lost.any()
    assert (out.loc[lost, "suppression_reason"] == "").all(), "not suppressed: only not treated"
    expected = int((m.eligible & ~m.sms_ok).sum())
    assert summary.channel_counts == {"sms": expected, "email": int((m.eligible & ~m.email_ok).sum())}
    assert summary.uncontactable_rows == int(lost.sum())
    # The run's own scoring summary carries the same counts on the rule that ran (the ledger's
    # consent_false), and invents no entry for a rule that did not.
    scoring = world.storage.read_model(run_key(sms_only.run_id, "scoring_summary.json"), ScoringSummary)
    assert [item.reason for item in scoring.suppressed] == ["consent_false"]
    assert scoring.suppressed[0].rows == 0
    assert scoring.suppressed[0].channel_counts == summary.channel_counts


def test_only_sms_planned_and_opted_out_by_the_ledger_is_not_treated(world: World, sms_only: Scored) -> None:
    out, _ = _treat_list(world, sms_only)
    m = _masks(world, sms_only, out)
    by_ledger = m.base & m.ledger_out
    assert by_ledger.any(), "the ledger withdrew SMS from customers the policy would treat"
    assert not m.file_out[by_ledger].any(), "the file still opts them in"
    assert (out.loc[by_ledger, "treat"] == "0").all()
    assert (out.loc[by_ledger, "suppression_reason"] == "").all()
    expected = np.where(m.email_ok[by_ledger], "email", "")
    assert (out.loc[by_ledger, "contactable_channels"].to_numpy() == expected).all(), "SMS taken, email kept"
    text = world.storage.read_bytes(run_key(sms_only.run_id, "scores.csv")).decode("utf-8")
    scores = pd.read_csv(io.StringIO(text), dtype=str, keep_default_na=False).set_index(PRIMARY_KEY)
    assert (scores.loc[out.loc[by_ledger, PRIMARY_KEY], "suppressed_reason"] == "").all()


# ---------------------------------------------------------------------------
# No invented entry, and nothing new by default
# ---------------------------------------------------------------------------
def test_no_suppression_entry_is_invented_for_a_rule_that_did_not_run(
    world: World, no_ledger: Scored
) -> None:
    scoring = world.storage.read_model(run_key(no_ledger.run_id, "scoring_summary.json"), ScoringSummary)
    # The file has none of the use case's opt-out or contact columns and no ledger gates the run.
    assert scoring.suppressed == ()
    text = world.storage.read_text(run_key(no_ledger.run_id, CONTACTABILITY_JSON))
    assert '"channel_counts"' in text


def test_a_default_run_writes_nothing_new(world: World, default: Scored) -> None:
    storage = world.storage
    assert not storage.exists(run_key(default.run_id, CONTACTABILITY_PARQUET))
    assert not storage.exists(run_key(default.run_id, CONTACTABILITY_JSON))
    assert CONTACTABILITY_PARQUET not in default.record.artefacts
    summary_text = storage.read_text(run_key(default.run_id, "scoring_summary.json"))
    assert "channel_counts" not in summary_text
    saved = json.loads(storage.read_text(run_key(default.run_id, "run_config.json")))["config"]
    assert "channels" not in saved["actions"]["suppression"]
    assert all("action_id" not in band for band in saved["actions"]["bands"])
    assert "treat_action_id" not in saved["uplift"]["policy"]
    out, summary = _treat_list(world, default)
    assert (out["contactable_channels"] == "").all() and (out["channel"] == "").all()
    assert summary.channel_counts is None and summary.uncontactable_rows is None
