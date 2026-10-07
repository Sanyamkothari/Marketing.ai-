"""The consent ledger gates an uplift scoring run exactly as it gates a propensity run (M91 fix a).

`engine/pipeline.py` gates Phase 1's score flow by rebinding `_ScoreFlow._actions` to
`_consent_gated_actions` (DEC-732). `UpliftScoreFlow` overrides `_actions`, so before M91 an uplift
run never consulted the ledger: a customer who had withdrawn consent stayed contactable and no
`consent_report.json` was written. These tests pin the fix:

* an uplift scoring run of `win-back-campaign` (mapped to `marketing_communication` in
  `configs/privacy.yaml`) with a `withdrawn` ledger record suppresses that customer as
  `consent_false` and writes `consent_report.json`, through `Pipeline.run_score` end to end;
* on the same scored rows and the same ledger, the propensity actions stage and the uplift actions
  stage suppress exactly the same rows for exactly the same reasons, and report the same counts.

The uplift model is trained once per module through `Pipeline.run_train` with LightGBM and 50
bootstrap resamples, as in `test_uplift_api.py`: seconds, not minutes. The propensity side needs no
trained model: the comparison is of the two actions stages on one scored frame, which is where the
consent gate lives.
"""

from __future__ import annotations

import io
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path
from typing import Any

import pandas as pd
import pytest
from pydantic import SecretStr

from engine.config import ResolvedConfig, RunMode, resolve_config
from engine.contracts import ModelVersion, RunRecord, RunState
from engine.jobs import CancelToken, NullJobRunner
from engine.pipeline import Pipeline, StageContext, _ScoreFlow
from engine.platform_db import PLATFORM_DB_FILENAME, sqlite_engine
from engine.privacy.config import load_privacy_config, privacy_salt
from engine.privacy.consent import LEDGER_CONSENT_COLUMN, ConsentLedger
from engine.privacy.contracts import CONSENT_REPORT_FILENAME, ConsentReport
from engine.registry import LocalModelRegistry
from engine.settings import Settings
from engine.storage import LocalStorage, run_key, upload_key
from engine.uplift.contracts import UpliftModelCard
from engine.uplift.flow import UpliftScoreFlow, model_card_key
from engine.utils.time import utc_now
from tests.fixtures.make_uplift_data import make_uplift_data, make_winback_campaign

pytestmark = pytest.mark.integration

USE_CASE = "win-back-campaign"
PURPOSE = "marketing_communication"
PRIMARY_KEY = "customer_id"
TARGET = "reactivated_90d"
TREATMENT = "treatment"
CLIENT = "acme"
PRIVACY_SALT = "uplift-consent-salt-01"
TRAIN_ROWS = 6_000
SCORE_ROWS = 1_200
TRAIN_RUN = "r_20261007_0d000001"
SCORE_RUN = "r_20261007_0d000002"
PROPENSITY_ACTIONS_RUN = "r_20261007_0d000003"
UPLIFT_ACTIONS_RUN = "r_20261007_0d000004"
PROPENSITY_SCORE_FIELD = "winback_prob"
UNUSED_UPLOAD = "uploads/u_0d0000000003/source.csv"
"""The actions-stage comparison never ingests; its flows are handed the scored rows directly."""

OVERRIDES: dict[str, Any] = {
    "problem_type": "uplift",
    "uplift.treatment_column": TREATMENT,
    "uplift.base_model": "lightgbm",
    "uplift.bootstrap_samples": 50,
    "uplift.min_arm_rows": 200,
    "uplift.min_arm_positives": 20,
    "governance.approval_required": False,
}
"""What `POST /uplift/runs` would apply for a fast LightGBM run (see `test_uplift_api.py`)."""


@dataclass(frozen=True)
class World:
    data_dir: Path
    resolved: ResolvedConfig
    version: ModelVersion
    scoring_frame: pd.DataFrame
    withdrawn: frozenset[str]
    unrecorded: frozenset[str]

    @property
    def storage(self) -> LocalStorage:
        return LocalStorage(self.data_dir)

    @property
    def registry(self) -> LocalModelRegistry:
        return LocalModelRegistry(self.data_dir / "registry.db")

    @property
    def pipeline(self) -> Pipeline:
        return Pipeline(self.storage, self.registry, NullJobRunner())

    def score_context(self, run_id: str, *, upload: str) -> StageContext:
        return _context(
            self.storage,
            self.registry,
            self.resolved,
            run_id,
            mode=RunMode.SCORE,
            upload=upload,
            target=None,
            model_version_id=self.version.model_id,
        )


def _context(
    storage: LocalStorage,
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
        storage=storage,
        registry=registry,
        cancel=CancelToken(),
        primary_key=[PRIMARY_KEY],
        target=target,
        upload_key=upload,
        model_version_id=model_version_id,
    )


def _write_csv(storage: LocalStorage, key: str, frame: pd.DataFrame) -> None:
    storage.write_bytes(key, frame.to_csv(index=False, lineterminator="\n").encode("utf-8"))


def _plant_ledger(data_dir: Path, customers: list[str]) -> tuple[frozenset[str], frozenset[str]]:
    """Grant everyone, then withdraw every 9th customer; every 13th (not withdrawn) has no record.

    Returns `(withdrawn, unrecorded)`. The ledger lives in the store's own `platform.db`, which is
    where the scoring seam looks for it on a local store (DEC-706).
    """
    withdrawn = {key for index, key in enumerate(customers) if index % 9 == 4}
    unrecorded = {key for index, key in enumerate(customers) if index % 13 == 6} - withdrawn
    granted_at = (utc_now() - timedelta(days=30)).date().isoformat()
    withdrawn_at = (utc_now() - timedelta(days=3)).date().isoformat()
    lines = ["principal_id,purpose,status,recorded_at"]
    lines += [f"{key},{PURPOSE},granted,{granted_at}" for key in customers if key not in unrecorded]
    lines += [f"{key},{PURPOSE},withdrawn,{withdrawn_at}" for key in sorted(withdrawn)]
    engine = sqlite_engine(data_dir / PLATFORM_DB_FILENAME)
    ledger = ConsentLedger(engine, salt=PRIVACY_SALT)
    # Recorded the way the API records it: the salt's fingerprint is kept beside the ledger (DEC-883).
    assert privacy_salt(Settings(privacy_salt=SecretStr(PRIVACY_SALT)), engine=engine) == PRIVACY_SALT
    report = ledger.import_csv("\n".join(lines) + "\n", client_id=CLIENT, privacy=load_privacy_config())
    assert report.imported and not report.errors, report.errors
    return frozenset(withdrawn), frozenset(unrecorded)


@pytest.fixture(scope="module")
def world(config_root: Path, tmp_path_factory: pytest.TempPathFactory) -> Iterator[World]:
    """One uplift model trained through `Pipeline.run_train`, and a ledger for the scoring file."""
    with pytest.MonkeyPatch.context() as patch:
        # The run names no client (an upload run), so the deployment's client id is the ledger's.
        patch.setenv("MARKETING_AI_CLIENT_ID", CLIENT)
        patch.setenv("MARKETING_AI_PRIVACY_SALT", PRIVACY_SALT)
        data_dir = tmp_path_factory.mktemp("uplift-consent") / "data"
        data_dir.mkdir()
        resolved = resolve_config(USE_CASE, OVERRIDES, root=config_root)
        storage = LocalStorage(data_dir)
        train_upload = upload_key("u_0d0000000001", "source.csv")
        _write_csv(storage, train_upload, make_uplift_data(TRAIN_ROWS, seed=7).frame)
        registry = LocalModelRegistry(data_dir / "registry.db")
        train = _context(
            storage,
            registry,
            resolved,
            TRAIN_RUN,
            mode=RunMode.TRAIN,
            upload=train_upload,
            target=TARGET,
            model_version_id=None,
        )
        record = Pipeline(storage, registry, NullJobRunner()).run_train(train)
        assert record.state is RunState.DONE, record.error
        assert record.model_version_id is not None
        version = registry.get(record.model_version_id)
        campaign = make_winback_campaign(SCORE_ROWS, seed=11).frame
        frame = campaign.drop(columns=[TARGET, TREATMENT, "treatment_date"], errors="ignore")
        withdrawn, unrecorded = _plant_ledger(data_dir, [str(key) for key in frame[PRIMARY_KEY]])
        yield World(
            data_dir=data_dir,
            resolved=resolved,
            version=version,
            scoring_frame=frame,
            withdrawn=withdrawn,
            unrecorded=unrecorded,
        )


@pytest.fixture(scope="module")
def scored(world: World) -> Iterator[RunRecord]:
    """The uplift scoring run, through `Pipeline.run_score` end to end, with the ledger in place."""
    with pytest.MonkeyPatch.context() as patch:
        patch.setenv("MARKETING_AI_CLIENT_ID", CLIENT)
        patch.setenv("MARKETING_AI_PRIVACY_SALT", PRIVACY_SALT)
        source = upload_key("u_0d0000000002", "source.csv")
        _write_csv(world.storage, source, world.scoring_frame)
        record = world.pipeline.run_score(world.score_context(SCORE_RUN, upload=source))
        assert record.state is RunState.DONE, record.error
        yield record


def _scores(storage: LocalStorage, run_id: str) -> pd.DataFrame:
    data = storage.read_bytes(run_key(run_id, "scores.csv"))
    return pd.read_csv(io.BytesIO(data), dtype={PRIMARY_KEY: str})


# ---------------------------------------------------------------------------
# An uplift scoring run is gated by the ledger
# ---------------------------------------------------------------------------
def test_an_uplift_run_suppresses_a_withdrawn_customer_as_consent_false(
    world: World, scored: RunRecord
) -> None:
    frame = _scores(world.storage, SCORE_RUN)
    assert len(frame.index) == SCORE_ROWS
    reasons = dict(zip(frame[PRIMARY_KEY], frame["suppressed_reason"], strict=True))
    assert world.withdrawn, "the ledger must withdraw somebody for this test to mean anything"
    for customer in world.withdrawn:
        assert reasons[customer] == "consent_false", customer
    excluded = world.withdrawn | world.unrecorded
    suppressed = set(frame.loc[frame["suppressed_reason"] == "consent_false", PRIMARY_KEY])
    assert suppressed == excluded
    contacted = frame.loc[frame["action"].astype(str).str.lower() == "treat", PRIMARY_KEY]
    assert not (set(contacted) & excluded), "a customer without valid consent was recommended for treatment"
    assert not frame.loc[frame[PRIMARY_KEY].isin(excluded), "control_group"].astype(bool).any()
    assert LEDGER_CONSENT_COLUMN not in frame.columns, "the synthetic consent column is never exported"


def test_an_uplift_run_writes_the_consent_report(world: World, scored: RunRecord) -> None:
    key = run_key(SCORE_RUN, CONSENT_REPORT_FILENAME)
    assert world.storage.exists(key), "an uplift run gated by the ledger must write consent_report.json"
    report = world.storage.read_model(key, ConsentReport)
    assert (report.use_case_id, report.client_id, report.purpose) == (USE_CASE, CLIENT, PURPOSE)
    assert report.consent_column == LEDGER_CONSENT_COLUMN
    assert report.principals_checked == SCORE_ROWS
    assert report.excluded_withdrawn == len(world.withdrawn)
    assert report.excluded_no_consent == len(world.unrecorded)
    assert report.excluded_total == len(world.withdrawn | world.unrecorded)
    assert scored.artefacts.get(CONSENT_REPORT_FILENAME) == key
    summary = world.storage.read_bytes(run_key(SCORE_RUN, "scoring_summary.json")).decode("utf-8")
    assert '"consent_false"' in summary


# ---------------------------------------------------------------------------
# Propensity and uplift suppress the same rows on the same input and ledger
# ---------------------------------------------------------------------------
def test_propensity_and_uplift_actions_suppress_the_same_rows(world: World, scored: RunRecord) -> None:
    """Both actions stages on one scored frame: the rows they suppress, and why, are identical."""
    import numpy as np

    uplift_scores = _scores(world.storage, SCORE_RUN)
    base = world.scoring_frame.copy()
    base[PRIMARY_KEY] = base[PRIMARY_KEY].astype(str)
    predictions = uplift_scores.set_index(PRIMARY_KEY).loc[base[PRIMARY_KEY]]
    for column in ("uplift", "p_treated", "p_control"):
        base[column] = predictions[column].to_numpy(dtype=np.float64)
    base[PROPENSITY_SCORE_FIELD] = base["p_treated"].clip(0.0, 1.0)

    with pytest.MonkeyPatch.context() as patch:
        patch.setenv("MARKETING_AI_CLIENT_ID", CLIENT)
        patch.setenv("MARKETING_AI_PRIVACY_SALT", PRIVACY_SALT)
        propensity_ctx = world.score_context(PROPENSITY_ACTIONS_RUN, upload=UNUSED_UPLOAD)
        propensity = _ScoreFlow(world.pipeline, propensity_ctx)
        propensity._scored = base.copy()
        propensity._actions()

        uplift_ctx = world.score_context(UPLIFT_ACTIONS_RUN, upload=UNUSED_UPLOAD)
        uplift = UpliftScoreFlow(world.pipeline, uplift_ctx)
        uplift._scored = base.copy()
        uplift._version = world.version
        uplift._card = world.storage.read_model(model_card_key(world.version.predictor_key), UpliftModelCard)
        uplift._actions()

    left = _reasons(_require_frame(propensity._scored))
    right = _reasons(_require_frame(uplift._scored))
    assert left.equals(right)
    assert set(left.index[left == "consent_false"]) == set(world.withdrawn | world.unrecorded)

    def report(run_id: str) -> dict[str, Any]:
        model = world.storage.read_model(run_key(run_id, CONSENT_REPORT_FILENAME), ConsentReport)
        return model.model_dump(exclude={"run_id", "ledger_as_of", "created_at"})

    assert report(PROPENSITY_ACTIONS_RUN) == report(UPLIFT_ACTIONS_RUN)


def _require_frame(frame: pd.DataFrame | None) -> pd.DataFrame:
    assert frame is not None
    return frame


def _reasons(frame: pd.DataFrame) -> pd.Series:
    reasons = frame.set_index(PRIMARY_KEY)["suppressed_reason"].astype("object")
    return reasons.where(reasons.notna(), None).sort_index()


# ---------------------------------------------------------------------------
# No ledger leaves the stage as it was; a client with no ledger is failed closed
# ---------------------------------------------------------------------------
NO_LEDGER_RUN = "r_20261007_0d000005"
OTHER_CLIENT_RUN = "r_20261007_0d000006"


def _uplift_actions(world: World, scored: RunRecord, run_id: str) -> pd.DataFrame:
    """`UpliftScoreFlow._actions` alone, on the uplift run's scored rows, under the caller's env."""
    frame = world.scoring_frame.copy()
    frame[PRIMARY_KEY] = frame[PRIMARY_KEY].astype(str)
    uplift_scores = _scores(world.storage, SCORE_RUN).set_index(PRIMARY_KEY).loc[frame[PRIMARY_KEY]]
    for column in ("uplift", "p_treated", "p_control"):
        frame[column] = uplift_scores[column].to_numpy()
    flow = UpliftScoreFlow(world.pipeline, world.score_context(run_id, upload=UNUSED_UPLOAD))
    flow._scored = frame
    flow._version = world.version
    flow._card = world.storage.read_model(model_card_key(world.version.predictor_key), UpliftModelCard)
    flow._actions()
    return _require_frame(flow._scored)


def test_with_no_client_the_uplift_stage_is_ungated_and_writes_no_consent_report(
    world: World, scored: RunRecord
) -> None:
    """Neither the run nor the deployment names a client: `consent_gate_for_run` is None, nothing new runs."""
    with pytest.MonkeyPatch.context() as patch:
        patch.delenv("MARKETING_AI_CLIENT_ID", raising=False)
        patch.setenv("MARKETING_AI_PRIVACY_SALT", PRIVACY_SALT)
        frame = _uplift_actions(world, scored, NO_LEDGER_RUN)
    assert not world.storage.exists(run_key(NO_LEDGER_RUN, CONSENT_REPORT_FILENAME))
    assert not (frame["suppressed_reason"] == "consent_false").any()
    assert LEDGER_CONSENT_COLUMN not in frame.columns


def test_a_client_with_no_ledger_is_failed_closed_on_an_uplift_run(world: World, scored: RunRecord) -> None:
    """Another client holds a ledger for the purpose and this one has none: nobody is contactable."""
    with pytest.MonkeyPatch.context() as patch:
        patch.setenv("MARKETING_AI_CLIENT_ID", "someone-else")
        patch.setenv("MARKETING_AI_PRIVACY_SALT", PRIVACY_SALT)
        frame = _uplift_actions(world, scored, OTHER_CLIENT_RUN)
    assert (frame["suppressed_reason"] == "consent_false").all()
    assert not frame["control_group"].astype(bool).any()
    report = world.storage.read_model(run_key(OTHER_CLIENT_RUN, CONSENT_REPORT_FILENAME), ConsentReport)
    assert report.client_id == "someone-else"
    assert report.excluded_total == SCORE_ROWS
