"""Plan J Phase 2 exit gate: the whole journey, on fixtures, through the product's own API.

The gate (docs/plans/MARKETING_AI_PLAN_J_PRODUCT.md, "Phase 2 exit gate"): approve with checks ->
value-ranked, offer-chosen, channel-aware treat list -> arbitration across three use cases -> campaigns
created and measured. Every step here is the product's own: nothing writes a run artefact, a flag, a
campaign or a measurement by hand. The app is `create_app` over a temporary store and a copy of
`configs/` (so the catalogue, the suppression channels, the persistent hold-out and the explore slice
are the use cases' own settings), and the work is done by the API's routes:

1. three use cases over one customer key - `win-back-campaign` (three offers), `retail-win-back` (one
   offer, a campaign-effect model) and `targeted-advertisement` (a risk model) - each trained from an
   uploaded file;
2. `GET /approvals` shows the advisory checks of M96 for the campaign-effect model, which is then approved;
   the risk model is approved too; the model of three offers is never champion
   (`MULTI_ARM_PROMOTION_REFUSED`) and is scored by naming its version;
3. each use case scored with a catalogue (actions per offer and channel, region rules), per-channel consent
   columns in the file and a persistent hold-out with an explore slice;
4. the treat lists, downloaded as an Analyst (a Viewer is refused);
5. `POST /decide/arbitrate` across the three use cases;
6. each use case's campaign given outcomes drawn from a planted truth and measured.

Why the data looks the way it does. The customers are the same in every use case (`K0000000`...), and
each file repeats the same consent columns, so "opted out of SMS" means one thing everywhere. A scoring
run finishes now, so a campaign's treatment starts now and its outcome window would not be over: the
outcomes file therefore carries a per-customer treatment date well in the past (supported input), and
the measurement is as of now.
"""

from __future__ import annotations

import hashlib
import io
import json
import shutil
import time
from collections.abc import Iterator
from contextlib import ExitStack
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Final

import numpy as np
import pandas as pd
import pytest
import yaml
from fastapi import FastAPI
from fastapi.testclient import TestClient
from numpy.typing import NDArray

from api.main import create_app
from engine import runs as engine_runs
from engine.access.roles import Role
from engine.access.users import SqlUserStore
from engine.audit.events import AuditQuery
from engine.contracts import ModelStatus, RunRecord, RunState
from engine.measurement.campaign import ASSIGNMENT_FILENAME, campaign_key, read_frame
from engine.pilot.plain import jargon_in
from engine.platform_db import PLATFORM_DB_FILENAME, sqlite_engine
from engine.registry import REGISTRY_FILENAME, LocalModelRegistry
from engine.settings import Settings
from engine.storage import LocalStorage
from engine.uplift.contracts import UpliftModelCard
from engine.uplift.flow import model_card_key
from engine.utils.time import utc_now
from tests.fixtures.decide.offer_population import LEVELS, OfferPopulation, offer_population
from tests.fixtures.make_data import GenerationSpec, generate
from tests.fixtures.make_uplift_data import UpliftDataset, make_uplift_data
from tests.integration.production.access_support import (
    FAST_ITERATIONS,
    audit_log_at,
    bearer,
    make_user,
)

pytestmark = [pytest.mark.slow, pytest.mark.integration]

MULTI: Final[str] = "win-back-campaign"
BINARY: Final[str] = "retail-win-back"
RISK: Final[str] = "targeted-advertisement"
USE_CASES: Final[tuple[str, str, str]] = (MULTI, BINARY, RISK)

KEY: Final[str] = "customer_id"
OUTCOME: Final[str] = "reactivated_90d"
RISK_OUTCOME: Final[str] = "converted_30d"
SALT: Final[str] = "phase2-journey-salt-0001"
HOLDOUT_FRACTION: Final[float] = 0.15
EXPLORE_FRACTION: Final[float] = 0.05
VALUE: Final[float] = 1000.0
BINARY_VALUE: Final[str] = "monthly_spend"
"""The column the campaign-effect use case ranks customers by: each customer's own value."""
BINARY_BUDGET: Final[int] = 600
RISK_VALUE: Final[str] = "monthly_value"
SCORE_ROWS: Final[int] = 4_000
RUN_TIMEOUT_S: Final[float] = 900.0

CATALOGUE: Final[str] = """\
# The journey's catalogue: an action per offer and channel, with the region's SMS rules (India).
region: IN
actions:
  - action_id: offer_a_sms
    label: Offer A by SMS
    channels: [sms]
    offer_cost: 10.0
    contact_cost: 0.5
    dlt_template_id: "1107000000000000001"
    message_category: promotional
  - action_id: offer_b_email
    label: Offer B by email
    channels: [email]
    offer_cost: 300.0
    contact_cost: 0.25
  - action_id: retail_offer_any
    label: Retail offer (email, else SMS)
    channels: [email, sms]
    offer_cost: 20.0
    contact_cost: 0.3
    dlt_template_id: "1107000000000000002"
    message_category: promotional
  - action_id: ad_any
    label: Advertisement (email, else SMS)
    channels: [email, sms]
    offer_cost: 0.0
    contact_cost: 0.1
    dlt_template_id: "1107000000000000003"
    message_category: promotional
"""
CHANNELS: Final[dict[str, dict[str, str]]] = {
    "sms": {"consent_column": "sms_opt_in"},
    "email": {"consent_column": "email_opt_in"},
}
"""The channels every use case of the journey configures, in order of preference, with their consent columns."""

EVIDENCE: Final[dict[str, Any]] = {
    "fold_auuc": True,
    "risk_comparison": True,
    "folds": 3,
}
"""M96's costly evidence, switched on for the campaign-effect model an Approver is shown."""

FAST_UPLIFT: Final[dict[str, Any]] = {
    "base_model": "lightgbm",
    "bootstrap_samples": 50,
    "min_arm_rows": 200,
    "min_arm_positives": 20,
    "segments": {"persuadable_min_uplift": 0.05},
}
FAST_RISK: Final[dict[str, Any]] = {
    "model_search": {
        "time_limit_minutes": 1,
        "strategy": "fast",
        "candidates": ["LogisticRegression"],
        "ensemble": False,
    }
}
"""The smallest real search there is; the journey tests the path, not the model."""


# ---------------------------------------------------------------------------
# The configuration root
# ---------------------------------------------------------------------------
def _edit(root: Path, name: str, change: Any) -> None:
    path = root / "use_cases" / name
    document = yaml.safe_load(path.read_text(encoding="utf-8"))
    change(document)
    path.write_text(yaml.safe_dump(document, sort_keys=False), encoding="utf-8")


def _shared(document: dict[str, Any]) -> None:
    actions = document.setdefault("actions", {})
    actions.setdefault("suppression", {})["channels"] = {k: dict(v) for k, v in CHANNELS.items()}
    actions["holdout"] = {"scope": "universal", "fraction": HOLDOUT_FRACTION}
    actions["explore_fraction"] = EXPLORE_FRACTION


def _as_uplift(document: dict[str, Any]) -> None:
    document["problem_type"] = "uplift"
    document["model_search"] = {"metric": "auuc", "metric_choices": ["auuc"]}


def make_root(config_root: Path, target: Path) -> Path:
    """A copy of `configs/` with the journey's catalogue and the three use cases described above."""
    shutil.copytree(config_root, target)
    assert not (target / "decide" / "catalogue.yaml").exists(), "the repository ships no catalogue"
    (target / "decide" / "catalogue.yaml").write_text(CATALOGUE, encoding="utf-8")

    def multi(document: dict[str, Any]) -> None:
        _as_uplift(document)
        _shared(document)
        uplift = document.setdefault("uplift", {})
        uplift["treatment_levels"] = list(LEVELS)
        uplift.setdefault("policy", {}).update(
            {
                "value_per_conversion": VALUE,
                "arm_action_ids": {"offer_a": "offer_a_sms", "offer_b": "offer_b_email"},
            }
        )

    def binary(document: dict[str, Any]) -> None:
        _as_uplift(document)
        _shared(document)
        document.setdefault("uplift", {}).setdefault("policy", {}).update(
            {
                "value_column": BINARY_VALUE,
                "margin_pct": 40.0,
                "horizon_months": 12,
                "budget_contacts": BINARY_BUDGET,
                "treat_action_id": "retail_offer_any",
            }
        )

    def risk(document: dict[str, Any]) -> None:
        _shared(document)
        document.setdefault("uplift", {}).setdefault("policy", {}).update(
            {"value_column": RISK_VALUE, "margin_pct": 30.0}
        )
        document["actions"]["bands"] = [
            {"name": "High", "min_score": 0.15, "action": "Serve ad", "action_id": "ad_any"},
            {"name": "Medium", "min_score": 0.08, "action": "Retarget", "action_id": "ad_any"},
            {"name": "Low", "min_score": 0.00, "action": "Suppress"},
        ]

    _edit(target, "win_back_campaign.yaml", multi)
    _edit(target, "retail_win_back.yaml", binary)
    _edit(target, "targeted_advertisement.yaml", risk)
    return target


# ---------------------------------------------------------------------------
# The customers: one key, shared consent
# ---------------------------------------------------------------------------
def customer_ids(n: int) -> np.ndarray:
    return np.char.add("K", np.char.zfill(np.arange(n).astype(str), 7))


def consent_columns(ids: np.ndarray, *, seed: int) -> pd.DataFrame:
    """Per-channel consent, drawn once per customer and repeated in every use case's file."""
    rng = np.random.default_rng(seed)
    n = len(ids)
    return pd.DataFrame(
        {
            KEY: ids,
            "sms_opt_in": np.where(rng.random(n) < 0.7, "true", "false"),
            "email_opt_in": np.where(rng.random(n) < 0.7, "yes", "no"),
        }
    )


def _with_consent(frame: pd.DataFrame, consent: pd.DataFrame) -> pd.DataFrame:
    return frame.drop(columns=["sms_opt_in", "email_opt_in"], errors="ignore").merge(
        consent, on=KEY, how="left", validate="one_to_one"
    )


@dataclass(frozen=True)
class Population:
    """The scoring population of every use case, and the truth the outcomes are drawn from."""

    consent: pd.DataFrame
    multi: OfferPopulation
    multi_frame: pd.DataFrame
    binary: UpliftDataset
    binary_frame: pd.DataFrame
    risk_frame: pd.DataFrame


def population() -> Population:
    ids = customer_ids(SCORE_ROWS)
    consent = consent_columns(ids, seed=71)
    multi = offer_population(SCORE_ROWS, seed=72)
    multi_frame = _with_consent(multi.frame.assign(**{KEY: ids}).drop(columns=[OUTCOME, "offer"]), consent)
    binary = make_uplift_data(SCORE_ROWS, seed=73)
    binary_frame = _with_consent(
        binary.frame.assign(**{KEY: ids}).drop(columns=[OUTCOME, "treatment"]), consent
    )
    binary = UpliftDataset(frame=binary.frame.assign(**{KEY: ids}), truth=binary.truth.assign(**{KEY: ids}))
    risk = generate(GenerationSpec(RISK, rows=SCORE_ROWS, variant="scoring", seed=74))
    risk_frame = _with_consent(_valued(risk.assign(**{KEY: ids}), seed=75), consent)
    return Population(consent, multi, multi_frame, binary, binary_frame, risk_frame)


def _valued(frame: pd.DataFrame, *, seed: int) -> pd.DataFrame:
    """A value per customer (rupees a month), so the risk model's list carries an expected gross value."""
    rng = np.random.default_rng(seed)
    return frame.assign(**{RISK_VALUE: np.round(rng.gamma(2.0, 300.0, size=len(frame.index)), 2)})


def training_frames() -> dict[str, pd.DataFrame]:
    """Each use case's training file; customers of their own, with the consent columns a client sends."""
    out: dict[str, pd.DataFrame] = {}
    trained_multi = offer_population(12_000, seed=81).frame
    out[MULTI] = trained_multi
    trained_binary = make_uplift_data(8_000, seed=82).frame
    consent = consent_columns(trained_binary[KEY].to_numpy().astype(str), seed=83)
    out[BINARY] = _with_consent(trained_binary, consent)
    risk = generate(GenerationSpec(RISK, rows=3_000, variant="clean", seed=84))
    out[RISK] = _with_consent(
        _valued(risk, seed=86), consent_columns(risk[KEY].to_numpy().astype(str), seed=85)
    )
    return out


# ---------------------------------------------------------------------------
# Driving the API
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class Api:
    client: TestClient
    data_dir: Path

    @property
    def storage(self) -> LocalStorage:
        return LocalStorage(self.data_dir)

    @property
    def registry(self) -> LocalModelRegistry:
        return LocalModelRegistry(self.data_dir / REGISTRY_FILENAME)

    def upload(self, frame: pd.DataFrame, *, use_case: str, mode: str) -> str:
        payload = frame.to_csv(index=False, lineterminator="\n").encode()
        response = self.client.post(
            "/uploads",
            files={"file": (f"{use_case}_{mode}.csv", payload, "text/csv")},
            data={"use_case": use_case, "mode": mode},
        )
        assert response.status_code == 201, response.text
        return str(response.json()["upload_id"])

    def start(self, path: str, body: dict[str, Any], *, run_id: str) -> str:
        with pytest.MonkeyPatch.context() as patch:
            patch.setattr(engine_runs, "new_run_id", lambda _moment=None: run_id)
            response = self.client.post(path, json=body)
        assert response.status_code == 202, response.text
        assert response.json()["run_id"] == run_id
        return run_id

    def finish(self, run_id: str) -> RunRecord:
        deadline = time.monotonic() + RUN_TIMEOUT_S
        while True:
            response = self.client.get(f"/runs/{run_id}")
            assert response.status_code == 200, response.text
            record = RunRecord.model_validate(response.json()["run"])
            if record.state in {RunState.DONE, RunState.FAILED, RunState.CANCELLED}:
                assert record.state is RunState.DONE, response.json()["status"]
                return record
            assert time.monotonic() < deadline, f"run {run_id} did not finish"
            time.sleep(0.2)

    def artefact(self, run_id: str, name: str) -> bytes:
        response = self.client.get(f"/runs/{run_id}/artefacts/{name}")
        assert response.status_code == 200, (name, response.text)
        content: bytes = response.content
        return content


@dataclass(frozen=True)
class SecuredApi:
    """The same store behind an app with sign-in on: one signed-in client per role."""

    app: FastAPI
    viewer: TestClient
    analyst: TestClient
    approver: TestClient
    analyst_id: str
    """The Analyst's user id, which the audit log names."""


def secured_api(root: Path, data_dir: Path, stack: ExitStack) -> SecuredApi:
    """A second app over the journey's store, sign-in on, as `tests/integration/production` builds one."""
    settings = Settings(auth_mode="local", env="local", data_dir=data_dir)
    app = create_app(config_root=root, data_dir=data_dir)
    app.state.settings = settings
    app.state.user_store = SqlUserStore(
        sqlite_engine(data_dir / PLATFORM_DB_FILENAME),
        session_ttl_seconds=settings.auth_session_ttl_seconds,
        iterations=FAST_ITERATIONS,
    )
    clients = {}
    ids = {}
    for name, role in (("viewer", Role.VIEWER), ("analyst", Role.ANALYST), ("approver", Role.APPROVER)):
        ids[name] = make_user(app, name, [role])
        clients[name] = stack.enter_context(TestClient(app, headers=bearer(app, ids[name])))
    return SecuredApi(app, clients["viewer"], clients["analyst"], clients["approver"], ids["analyst"])


@dataclass
class Journey:
    """Everything the steps produce, read by the tests below."""

    api: Api
    secured: SecuredApi
    root: Path
    population: Population
    train: dict[str, RunRecord] = field(default_factory=dict)
    score: dict[str, RunRecord] = field(default_factory=dict)
    pending: list[dict[str, Any]] = field(default_factory=list)
    promote_refusal: dict[str, Any] = field(default_factory=dict)


def _train(api: Api, use_case: str, frame: pd.DataFrame, run_id: str) -> RunRecord:
    upload_id = api.upload(frame, use_case=use_case, mode="train")
    if use_case == RISK:
        body: dict[str, Any] = {
            "use_case": use_case,
            "mode": "train",
            "upload_id": upload_id,
            "primary_key": KEY,
            "target": RISK_OUTCOME,
            "overrides": FAST_RISK,
        }
        return api.finish(api.start("/runs", body, run_id=run_id))
    uplift: dict[str, Any] = dict(FAST_UPLIFT)
    if use_case == BINARY:
        uplift["evidence"] = dict(EVIDENCE)
    body = {
        "use_case": use_case,
        "upload_id": upload_id,
        "primary_key": KEY,
        "target": OUTCOME,
        "treatment_column": "offer" if use_case == MULTI else "treatment",
        "overrides": {"uplift": uplift},
    }
    return api.finish(api.start("/uplift/runs", body, run_id=run_id))


def _score(api: Api, use_case: str, frame: pd.DataFrame, run_id: str, model: str | None) -> RunRecord:
    body: dict[str, Any] = {
        "use_case": use_case,
        "mode": "score",
        "upload_id": api.upload(frame, use_case=use_case, mode="score"),
        "primary_key": KEY,
    }
    if model is not None:
        body["model_version_id"] = model
    if use_case == RISK:
        body["overrides"] = FAST_RISK
    return api.finish(api.start("/runs", body, run_id=run_id))


@pytest.fixture(scope="module")
def journey(config_root: Path, tmp_path_factory: pytest.TempPathFactory) -> Iterator[Journey]:
    base = tmp_path_factory.mktemp("phase2-journey")
    root = make_root(config_root, base / "configs")
    data_dir = base / "data"
    data_dir.mkdir()
    frames = training_frames()
    people = population()
    with pytest.MonkeyPatch.context() as patch:
        patch.setenv("MARKETING_AI_HOLDOUT_SALT", SALT)
        patch.setenv("MARKETING_AI_CONFIG_DIR", str(root))
        with (
            ExitStack() as stack,
            TestClient(create_app(config_root=root, data_dir=data_dir)) as client,
        ):
            api = Api(client, data_dir)
            secured = secured_api(root, data_dir, stack)
            j = Journey(api=api, secured=secured, root=root, population=people)
            # 1. three use cases, trained through the API
            j.train[MULTI] = _train(api, MULTI, frames[MULTI], "r_20261009_0f200001")
            j.train[BINARY] = _train(api, BINARY, frames[BINARY], "r_20261009_0f200002")
            j.train[RISK] = _train(api, RISK, frames[RISK], "r_20261009_0f200003")
            # 2. approve with checks: what an Approver sees, then the decisions
            listed = secured.approver.get("/approvals")
            assert listed.status_code == 200, listed.text
            j.pending = listed.json()["items"]
            for use_case in (BINARY, RISK):
                version = j.train[use_case].model_version_id
                approved = secured.approver.post(
                    f"/models/{version}/approve", json={"approved_by": "ignored", "reason": "journey"}
                )
                assert approved.status_code == 200, approved.text
            refused = secured.approver.post(
                f"/models/{j.train[MULTI].model_version_id}/promote",
                json={"promoted_by": "approver", "reason": "by hand"},
            )
            j.promote_refusal = {"status": refused.status_code, "body": refused.json()}
            # 3. each use case scored: the multi-offer model by naming its version, the others by champion
            people_frames = {
                MULTI: people.multi_frame,
                BINARY: people.binary_frame,
                RISK: people.risk_frame,
            }
            named = {MULTI: j.train[MULTI].model_version_id, BINARY: None, RISK: None}
            for index, use_case in enumerate(USE_CASES, start=1):
                j.score[use_case] = _score(
                    api, use_case, people_frames[use_case], f"r_20261009_0f2001{index:02d}", named[use_case]
                )
            yield j


# ---------------------------------------------------------------------------
# Steps 4 and 5: the treat lists and the arbitration, through the API as an Analyst
# ---------------------------------------------------------------------------
def csv_frame(content: bytes) -> pd.DataFrame:
    return pd.read_csv(io.BytesIO(content), dtype=str, keep_default_na=False)


@pytest.fixture(scope="module")
def lists(journey: Journey) -> dict[str, pd.DataFrame]:
    """Each run's treat list as an Analyst downloads it (`GET /runs/{id}/treat_list.csv`)."""
    out = {}
    for use_case, record in journey.score.items():
        response = journey.secured.analyst.get(f"/runs/{record.run_id}/treat_list.csv")
        assert response.status_code == 200, response.text
        out[use_case] = csv_frame(response.content)
    return out


@dataclass(frozen=True)
class Arbitrated:
    body: dict[str, Any]
    table: pd.DataFrame
    """`GET /decide/arbitrated-treat-list.csv`, as text."""


@pytest.fixture(scope="module")
def arbitrated(journey: Journey, lists: dict[str, pd.DataFrame]) -> Arbitrated:
    del lists  # the lists exist before the arbitration asks for them
    response = journey.secured.analyst.post("/decide/arbitrate", json={"use_cases": list(USE_CASES)})
    assert response.status_code == 200, response.text
    download = journey.secured.analyst.get("/decide/arbitrated-treat-list.csv")
    assert download.status_code == 200, download.text
    return Arbitrated(response.json(), csv_frame(download.content))


# ---------------------------------------------------------------------------
# Step 6: campaigns given outcomes drawn from the planted truth, and measured
# ---------------------------------------------------------------------------
OFFER_POSITION: Final[dict[str, int]] = {"Offer A by SMS": 1, "Offer B by email": 2}
RISK_BASE: Final[float] = 0.10
RISK_LIFT: Final[float] = 0.25
TREATED_ON_DAYS_AGO: Final[int] = 150


@dataclass(frozen=True)
class Planted:
    """What a use case's campaign would see: the outcome of every customer, and the truth behind it."""

    outcomes: pd.DataFrame
    effect: pd.Series
    """The true effect on each customer (indexed by customer) of the contact this use case actually made;
    zero for a customer it did not contact."""


def planted_truth(j: Journey, table: pd.DataFrame) -> dict[str, Planted]:
    """Outcomes for each use case's campaign, drawn from the planted populations and the arbitrated list.

    A customer's outcome is the baseline draw plus the true effect of the action they were given and only
    of that action: a customer the arbitration gave to another use case is not moved by this one.
    """
    ids = customer_ids(SCORE_ROWS)
    n = len(ids)
    won = table[table["treat"] == "1"].set_index(KEY)
    winner = pd.Series(ids).map(won["winning_use_case"])
    offer = pd.Series(ids).map(won["offer"])
    out: dict[str, Planted] = {}

    people = j.population.multi
    tenure = people.frame["tenure_months"].to_numpy()
    base = np.where(people.segment == "sleeping_dogs", 0.35, 0.15) + 0.03 * (tenure / 72.0)
    position = offer.map(OFFER_POSITION).fillna(0).astype(int).to_numpy()
    mine = (winner == MULTI).to_numpy() & (position > 0)
    effect = np.where(mine, people.tau[np.arange(n), np.maximum(position - 1, 0)], 0.0)
    drawn = np.random.default_rng(91).random(n) < np.clip(base + effect, 0.0, 1.0)
    out[MULTI] = Planted(
        pd.DataFrame({KEY: ids, OUTCOME: drawn.astype(int)}), pd.Series(effect, index=ids, name="effect")
    )

    truth = j.population.binary.truth.set_index(KEY).loc[ids]
    mine = (winner == BINARY).to_numpy()
    effect = np.where(mine, (truth["p_treated"] - truth["p_control"]).to_numpy(), 0.0)
    drawn = np.random.default_rng(92).random(n) < np.clip(truth["p_control"].to_numpy() + effect, 0.0, 1.0)
    out[BINARY] = Planted(
        pd.DataFrame({KEY: ids, OUTCOME: drawn.astype(int)}), pd.Series(effect, index=ids, name="effect")
    )

    mine = (winner == RISK).to_numpy()
    effect = np.where(mine, RISK_LIFT, 0.0)
    drawn = np.random.default_rng(93).random(n) < RISK_BASE + effect
    out[RISK] = Planted(
        pd.DataFrame({KEY: ids, RISK_OUTCOME: drawn.astype(int)}), pd.Series(effect, index=ids, name="effect")
    )
    return out


@dataclass(frozen=True)
class Measured:
    campaign_id: str
    use_case: str
    planted: Planted
    early: dict[str, Any] | None
    """The answer to measuring before the outcome window was over (only where the file carried no dates)."""
    view: dict[str, Any]
    """`POST /campaigns/{id}/measure` once every customer's outcome is in."""


@pytest.fixture(scope="module")
def measured(journey: Journey, arbitrated: Arbitrated) -> dict[str, Measured]:
    analyst = journey.secured.analyst
    truth = planted_truth(journey, arbitrated.table)
    treated_on = (utc_now() - timedelta(days=TREATED_ON_DAYS_AGO)).date().isoformat()
    out: dict[str, Measured] = {}
    for entry in arbitrated.body["campaigns"]:
        use_case, campaign_id = entry["use_case_id"], entry["campaign_id"]
        planted = truth[use_case]
        early: dict[str, Any] | None = None
        if use_case == RISK:
            # The outcomes arrive with no treatment dates: the campaign began when its list was made, a
            # moment ago, so the 30-day window is not over and no number is given.
            undated = journey.api.upload(planted.outcomes, use_case=use_case, mode="score")
            added = analyst.post(f"/campaigns/{campaign_id}/outcomes", json={"upload_id": undated})
            assert added.status_code == 200, added.text
            answer = analyst.post(f"/campaigns/{campaign_id}/measure", json={})
            early = {"status": answer.status_code, "body": answer.json()}
        dated = journey.api.upload(
            planted.outcomes.assign(treated_on=treated_on), use_case=use_case, mode="score"
        )
        added = analyst.post(
            f"/campaigns/{campaign_id}/outcomes",
            json={"upload_id": dated, "treatment_date_column": "treated_on"},
        )
        assert added.status_code == 200, added.text
        answer = analyst.post(f"/campaigns/{campaign_id}/measure", json={})
        assert answer.status_code == 200, answer.text
        out[use_case] = Measured(campaign_id, use_case, planted, early, answer.json())
    return out


# ---------------------------------------------------------------------------
# Helpers the tests share
# ---------------------------------------------------------------------------
def flag(series: pd.Series) -> pd.Series:
    """A treat list's `1`/`0` text as booleans."""
    return series == "1"


def number(series: pd.Series) -> pd.Series:
    """A treat list's text as numbers; an empty cell is NaN."""
    return pd.to_numeric(series, errors="coerce")


def treated(frame: pd.DataFrame) -> pd.DataFrame:
    return frame[flag(frame["treat"])]


def policy_treated(frame: pd.DataFrame) -> pd.DataFrame:
    """The rows the use case's policy chose to treat: treated, and not treated at random (explore)."""
    return frame[flag(frame["treat"]) & ~flag(frame["explore"])]


def consent_of(journey: Journey) -> pd.DataFrame:
    return journey.population.consent.set_index(KEY)


def reached_by(frame: pd.DataFrame, consent: pd.DataFrame, channel: str) -> NDArray[np.bool_]:
    """Per customer of `frame`: has the customer agreed to be contacted on `channel`."""
    column = {"sms": ("sms_opt_in", "true"), "email": ("email_opt_in", "yes")}[channel]
    agreed: NDArray[np.bool_] = (consent.loc[frame[KEY], column[0]] == column[1]).to_numpy()
    return agreed


# ---------------------------------------------------------------------------
# 1 and 2. Three use cases trained; approve with checks; a model of three offers is never champion
# ---------------------------------------------------------------------------
def test_three_use_cases_over_one_customer_key_are_trained_through_the_api(journey: Journey) -> None:
    assert set(journey.train) == set(USE_CASES)
    for use_case, record in journey.train.items():
        assert record.state is RunState.DONE and record.use_case_id == use_case
        assert record.primary_key == KEY
    # The key is the same text in every use case's scoring file: one customer, one identity.
    keys = [
        set(frame[KEY])
        for frame in (
            journey.population.multi_frame,
            journey.population.binary_frame,
            journey.population.risk_frame,
        )
    ]
    assert keys[0] == keys[1] == keys[2] and len(keys[0]) == SCORE_ROWS
    # A channel's consent column travels in the file but is never something a model learns from.
    for use_case in (MULTI, BINARY):
        version = journey.api.registry.get(journey.train[use_case].model_version_id or "")
        card = journey.api.storage.read_model(model_card_key(version.predictor_key), UpliftModelCard)
        assert card.feature_columns and not {"sms_opt_in", "email_opt_in"} & set(card.feature_columns)
    assert tuple(journey.population.multi_frame.columns).count("sms_opt_in") == 1


def test_the_approvals_screen_shows_the_advisory_checks_of_the_campaign_effect_model(
    journey: Journey,
) -> None:
    waiting = {item["version"]["use_case_id"]: item for item in journey.pending}
    # The model of one offer and the risk model wait for an Approver; the model of three offers never does.
    assert set(waiting) == {BINARY, RISK}
    checks = {check["code"]: check for check in waiting[BINARY]["checks"]}
    assert set(checks) == {
        "UPLIFT_NOT_BETTER_THAN_RISK",
        "UPLIFT_UNSTABLE_ACROSS_FOLDS",
        "UPLIFT_MISCALIBRATED",
    }
    for check in checks.values():
        assert check["passed"] is not None, check  # the evidence was switched on: measured, never made up
        assert check["message"].strip()
    assert waiting[BINARY]["can_decide"] is True
    assert waiting[RISK]["checks"] == [], "a risk model has no campaign-effect checks"
    # M96's equal-budget comparison was written by the training run and is served read-only to a Viewer.
    served = journey.secured.viewer.get(f"/runs/{journey.train[BINARY].run_id}/risk-comparison")
    assert served.status_code == 200, served.text


def test_the_checks_are_advice_the_approver_acts_on_and_the_approved_models_become_champions(
    journey: Journey,
) -> None:
    response = journey.api.client.get("/models")
    assert response.status_code == 200, response.text
    versions = {item["version"]["use_case_id"]: item for item in response.json()["versions"]}
    for use_case in (BINARY, RISK):
        assert versions[use_case]["is_champion"] is True
        assert versions[use_case]["version"]["status"] == ModelStatus.CHAMPION.value
        assert versions[use_case]["version"]["model_id"] == journey.train[use_case].model_version_id
    # Approved by the signed-in Approver, not by the name typed in the request.
    assert versions[BINARY]["version"]["approved_by"] == "approver"


def test_a_model_of_three_offers_is_never_champion_and_is_scored_by_naming_its_version(
    journey: Journey,
) -> None:
    refusal = journey.promote_refusal
    assert refusal["status"] == 409 and refusal["body"]["detail"]["code"] == "MULTI_ARM_PROMOTION_REFUSED"
    version = journey.api.registry.get(journey.train[MULTI].model_version_id or "")
    assert version.status is ModelStatus.CANDIDATE
    assert journey.api.registry.get_champion(MULTI) is None
    assert journey.train[MULTI].champion is False
    # Scored by naming it; the other two by their champion, whose id the run records.
    assert journey.score[MULTI].model_version_id == journey.train[MULTI].model_version_id
    assert journey.score[BINARY].model_version_id == journey.train[BINARY].model_version_id
    assert journey.score[RISK].model_version_id == journey.train[RISK].model_version_id


# ---------------------------------------------------------------------------
# 3. Scored with a catalogue, suppression channels, consent and a persistent hold-out
# ---------------------------------------------------------------------------
def test_every_run_is_checked_against_and_stamps_the_catalogue(journey: Journey) -> None:
    digest = hashlib.sha256(CATALOGUE.encode("utf-8")).hexdigest()
    planned = {
        MULTI: {"offer_a_sms": ["sms"], "offer_b_email": ["email"]},
        BINARY: {"retail_offer_any": ["email", "sms"]},
        RISK: {"ad_any": ["email", "sms"]},
    }
    for use_case, record in journey.score.items():
        stamp = json.loads(journey.api.artefact(record.run_id, "catalogue_stamp.json"))
        assert stamp["catalogue_sha256"] == digest, use_case
        assert stamp["planned_channels"] == planned[use_case], use_case


def test_a_catalogue_that_breaks_the_regions_sms_rules_is_refused(journey: Journey, tmp_path: Path) -> None:
    """India's rules need a template id and a message category on every SMS action (`configs/regions/in.yaml`)."""
    broken = tmp_path / "configs"
    shutil.copytree(journey.root, broken)
    text = CATALOGUE.replace('    dlt_template_id: "1107000000000000001"\n', "", 1)
    assert text != CATALOGUE
    (broken / "decide" / "catalogue.yaml").write_text(text, encoding="utf-8")
    with TestClient(create_app(config_root=broken, data_dir=tmp_path / "data")) as client:
        response = client.get(f"/use-cases/{MULTI}")
    assert (
        response.status_code >= 400
    ), "a use case that names an action the region does not allow must not load"
    assert "ACTION_DLT_TEMPLATE_MISSING" in response.text


def test_one_persistent_holdout_and_an_explore_slice_are_shared_by_the_three_lists(
    journey: Journey, lists: dict[str, pd.DataFrame]
) -> None:
    held = {use_case: set(frame.loc[flag(frame["holdout"]), KEY]) for use_case, frame in lists.items()}
    assert held[MULTI] == held[BINARY] == held[RISK], "a universal hold-out is the same customers everywhere"
    assert abs(len(held[MULTI]) / SCORE_ROWS - HOLDOUT_FRACTION) < 0.03
    for use_case, frame in lists.items():
        assert len(frame) == SCORE_ROWS and frame[KEY].is_unique
        explored = frame[flag(frame["explore"])]
        assert len(explored) > 5, use_case
        assert not flag(explored["holdout"]).any(), use_case
        assert (
            flag(explored["treat"]).mean() > 0.8
        ), "an explored customer is treated although the policy left them out"
        assert set(frame["use_case"]) == {use_case}


# ---------------------------------------------------------------------------
# 4. The treat lists, downloaded as an Analyst
# ---------------------------------------------------------------------------
def test_only_an_analyst_downloads_a_treat_list_and_the_download_is_audited(
    journey: Journey, lists: dict[str, pd.DataFrame]
) -> None:
    del lists  # the Analyst's downloads have happened
    for record in journey.score.values():
        refused = journey.secured.viewer.get(f"/runs/{record.run_id}/treat_list.csv")
        assert refused.status_code == 403 and refused.json()["detail"]["code"] == "ROLE_REQUIRED"
    events = audit_log_at(journey.api.data_dir).query(
        AuditQuery(actor_id=journey.secured.analyst_id, action="runs.treat_list_download", limit=100)
    )
    assert {event.object_id for event in events if event.outcome == "success"} >= {
        record.run_id for record in journey.score.values()
    }


def test_the_uplift_lists_are_ranked_by_net_value_as_the_policy_chose(
    journey: Journey, lists: dict[str, pd.DataFrame]
) -> None:
    binary = lists[BINARY]
    scores = pd.read_parquet(io.BytesIO(journey.api.artefact(journey.score[BINARY].run_id, "scores.parquet")))
    scores = scores.astype({KEY: str}).set_index(KEY)
    chosen = policy_treated(binary)
    assert 0 < len(chosen) <= BINARY_BUDGET
    value = number(chosen["net_value"])
    assert value.notna().all() and (value > 0).all()
    # The list says what the run's own scores say, customer by customer.
    assert np.allclose(value.to_numpy(), scores.loc[chosen[KEY], "net_value"].round(2).to_numpy(), atol=0.011)
    # The budget bites, and the customers it takes are the best: none left out is worth more than one taken.
    intended = scores["intended_treatment"].astype(bool)
    held = scores["control_group"].astype(bool)
    assert (
        int((intended & ~held).sum()) == BINARY_BUDGET
    ), "the budget is spent on customers who are contacted"
    assert (intended & held).any(), "the hold-out takes its share of the policy's choice"
    candidates = (scores["segment"] == "persuadable") & scores["suppressed_reason"].isna()
    left_out = candidates & ~intended
    assert left_out.any()
    assert scores.loc[intended, "net_value"].min() >= scores.loc[left_out, "net_value"].max() - 1e-9
    assert set(chosen[KEY]) <= set(scores.index[intended])
    # The risk model's list is valued too, but as money the customer is expected to bring, not as extra money.
    risk = treated(lists[RISK])
    assert (risk["net_value"] == "").all() and number(risk["expected_gross_value"]).notna().all()


def test_the_offer_is_chosen_per_customer_on_the_multi_offer_list(
    journey: Journey, lists: dict[str, pd.DataFrame]
) -> None:
    frame = lists[MULTI]
    chosen = policy_treated(frame)
    assert set(chosen["offer"]) == {"Offer A by SMS", "Offer B by email"}, "both offers are in use"
    channel_of = {"Offer A by SMS": "sms", "Offer B by email": "email"}
    assert (chosen["channel"] == chosen["offer"].map(channel_of)).all()
    # The runner-up is the other offer, when the customer could have been given it, and it is not better.
    runner = chosen[chosen["runner_up_offer"] != ""]
    assert len(runner) > 0.2 * len(chosen)
    assert (runner["runner_up_offer"] != runner["offer"]).all()
    assert set(runner["runner_up_offer"]) <= set(channel_of)
    assert (number(runner["runner_up_net_value"]) <= number(runner["net_value"]) + 0.011).all()
    assert (number(chosen["net_value"]) > 0).all()
    # Exactly the arithmetic of the run: the chosen offer is the one worth most among those the customer
    # can be reached on, from the model's own per-offer scores.
    scores = pd.read_parquet(io.BytesIO(journey.api.artefact(journey.score[MULTI].run_id, "scores.parquet")))
    scores = scores.astype({KEY: str}).set_index(KEY)
    costs = {1: (0.5, 10.0), 2: (0.25, 300.0)}
    money = pd.DataFrame(
        {
            k: scores[f"uplift_arm_{k}"] * VALUE - (costs[k][0] + costs[k][1] * scores[f"p_treated_arm_{k}"])
            for k in (1, 2)
        }
    )
    consent = consent_of(journey)
    both = chosen[reached_by(chosen, consent, "sms") & reached_by(chosen, consent, "email")]
    assert len(both) > 100
    best = money.loc[both[KEY]].idxmax(axis=1).map({1: "Offer A by SMS", 2: "Offer B by email"})
    assert (best.to_numpy() == both["offer"].to_numpy()).all()
    given = np.where(both["offer"] == "Offer A by SMS", 1, 2)
    expected = money.loc[both[KEY]].to_numpy()[np.arange(len(both)), given - 1]
    assert np.allclose(number(both["net_value"]).to_numpy(), expected, atol=0.011)


def test_a_channel_is_only_one_the_customer_can_be_reached_on(
    journey: Journey, lists: dict[str, pd.DataFrame]
) -> None:
    consent = consent_of(journey)
    for use_case, frame in lists.items():
        sent = treated(frame)
        assert set(sent["channel"]) <= {"sms", "email"}, use_case
        planned_ok = [
            channel in contactable.split(",")
            for channel, contactable in zip(sent["channel"], sent["contactable_channels"], strict=True)
        ]
        assert all(planned_ok), use_case
        # The customer opted out of one channel is never sent on it, and the opt-out is not a suppression.
        no_sms = ~reached_by(frame, consent, "sms")
        no_email = ~reached_by(frame, consent, "email")
        assert no_sms.sum() > 500 and no_email.sum() > 500
        assert not (frame["channel"][no_sms] == "sms").any(), use_case
        assert not (frame["channel"][no_email] == "email").any(), use_case
        assert not frame.loc[no_sms, "contactable_channels"].str.contains("sms").any(), use_case
        assert not frame.loc[no_email, "contactable_channels"].str.contains("email").any(), use_case
        # An SMS opt-out who is open on email is still reached, by email.
        assert ((frame["channel"] == "email") & no_sms).sum() > 0, use_case


def test_nobody_in_the_holdout_is_ever_treated(lists: dict[str, pd.DataFrame]) -> None:
    for use_case, frame in lists.items():
        held = flag(frame["holdout"])
        assert held.sum() > 0
        assert not (held & flag(frame["treat"])).any(), use_case
        assert (frame.loc[held, "channel"] == "").all(), use_case


def test_the_reasons_are_in_plain_words(lists: dict[str, pd.DataFrame]) -> None:
    for use_case, frame in lists.items():
        sent = treated(frame)
        assert (sent["reason_1"] != "").mean() > 0.95, use_case
        texts: set[str] = set()
        for column in ("reason_1", "reason_2", "reason_3", "offer_reason", "offer", "runner_up_offer"):
            texts |= set(frame[column]) - {""}
        assert texts
        jargon = {text: jargon_in(text) for text in texts if jargon_in(text)}
        assert not jargon, (use_case, jargon)


# ---------------------------------------------------------------------------
# 5. Arbitration across the three use cases
# ---------------------------------------------------------------------------
def candidates_of(lists: dict[str, pd.DataFrame]) -> pd.DataFrame:
    """Every `treat = 1` row of the three lists: what each use case asked for."""
    parts = [frame[flag(frame["treat"])].assign(source=use_case) for use_case, frame in lists.items()]
    return pd.concat(parts, ignore_index=True)


def test_no_customer_has_more_than_one_action(lists: dict[str, pd.DataFrame], arbitrated: Arbitrated) -> None:
    table = arbitrated.table
    summary = arbitrated.body["summary"]
    asked = candidates_of(lists)
    per_customer = asked.groupby(KEY).size()
    assert (per_customer > 1).sum() > 500, "the use cases do compete for customers"
    assert len(table) == SCORE_ROWS and table[KEY].is_unique, "one row per customer"
    won = table[flag(table["treat"])]
    assert len(won) == len(per_customer) == summary["treated_customers"] == summary["customers_with_actions"]
    assert set(won[KEY]) == set(per_customer.index), "everyone who was asked for is acted on, once"
    assert (won["winning_use_case"] != "").all()
    assert (table.loc[~flag(table["treat"]), "winning_use_case"] == "").all()


def test_the_holdout_is_untouched_and_the_explore_actions_are_kept(
    lists: dict[str, pd.DataFrame], arbitrated: Arbitrated
) -> None:
    table = arbitrated.table.set_index(KEY)
    held = {key for frame in lists.values() for key in frame.loc[flag(frame["holdout"]), KEY]}
    assert held and not flag(table.loc[sorted(held), "treat"]).any()
    assert (table.loc[sorted(held), "arbitration_reason"] == "held_out").all()
    asked = candidates_of(lists)
    explored = asked[flag(asked["explore"])]
    once = explored.groupby(KEY).size()
    sole = explored[explored[KEY].isin(once.index[once == 1])].set_index(KEY)
    assert len(sole) > 50
    # Treated at random in exactly one use case: that action is the one the customer gets.
    assert flag(table.loc[sole.index, "treat"]).all()
    assert (table.loc[sole.index, "winning_use_case"] == sole["source"]).all()
    assert (table.loc[sole.index, "offer"] == sole["offer"]).all()
    assert (table.loc[sole.index, "channel"] == sole["channel"]).all()


def test_the_winning_action_is_that_use_cases_own_row(
    lists: dict[str, pd.DataFrame], arbitrated: Arbitrated
) -> None:
    won = arbitrated.table[flag(arbitrated.table["treat"])]
    for use_case, frame in lists.items():
        mine = won[won["winning_use_case"] == use_case].set_index(KEY)
        own = frame.set_index(KEY).loc[mine.index]
        assert len(mine) > 100
        for column in ("offer", "channel", "contactable_channels", "reason_1"):
            assert (mine[column] == own[column]).all(), (use_case, column)
        for column in ("net_value", "expected_gross_value"):
            assert np.allclose(
                number(mine[column]).fillna(-1).to_numpy(), number(own[column]).fillna(-1).to_numpy()
            ), (use_case, column)


def test_contested_customers_are_decided_by_value_only_between_values_of_one_kind(
    lists: dict[str, pd.DataFrame], arbitrated: Arbitrated
) -> None:
    table = arbitrated.table.set_index(KEY)
    asked = candidates_of(lists)
    asked = asked[~asked[KEY].isin(asked.loc[flag(asked["explore"]), KEY])]  # explore rows are decided first
    sets = {str(key): tuple(sorted(group)) for key, group in asked.groupby(KEY)["source"]}
    order = {MULTI: 0, BINARY: 1, RISK: 2}
    # Two incremental values (both in rupees of extra money): the larger wins.
    pair = [key for key, wanted in sets.items() if wanted == tuple(sorted((MULTI, BINARY)))]
    assert len(pair) > 100
    net = asked[asked[KEY].isin(pair)].pivot(index=KEY, columns="source", values="net_value")
    net = net.apply(pd.to_numeric)
    expected = np.where(net[MULTI] >= net[BINARY], MULTI, BINARY)
    assert (table.loc[net.index, "winning_use_case"].to_numpy() == expected).all()
    assert (table.loc[net.index, "arbitration_reason"] == "net_value").all()
    # A risk model's list holds money of another kind: never compared, so the request order decides.
    with_risk = [key for key, wanted in sets.items() if RISK in wanted and len(wanted) > 1]
    assert len(with_risk) > 100
    first = [min(sets[key], key=order.__getitem__) for key in with_risk]
    assert (table.loc[with_risk, "winning_use_case"].to_numpy() == np.asarray(first)).all()
    assert (table.loc[with_risk, "arbitration_reason"] == "request_order").all()


def test_the_conflicts_summary_counts_what_the_lists_hold(
    lists: dict[str, pd.DataFrame], arbitrated: Arbitrated, journey: Journey
) -> None:
    summary = arbitrated.body["summary"]
    asked = candidates_of(lists)
    per_customer = asked.groupby(KEY).size()
    assert summary["total_customers"] == SCORE_ROWS
    assert summary["customers_with_conflicts"] == int((per_customer > 1).sum())
    assert summary["dropped_actions_count"] == int((per_customer - 1).sum())
    assert summary["channel_capped_count"] == 0 and summary["holdout_blocked_actions"] == 0
    assert summary["run_ids"] == [journey.score[use_case].run_id for use_case in USE_CASES]
    table = arbitrated.table
    won = table[flag(table["treat"])]
    by_winner = won["winning_use_case"].value_counts().to_dict()
    assert summary["winning_by_use_case"] == by_winner
    assert sum(by_winner.values()) == summary["treated_customers"]
    asked_by = asked["source"].value_counts().to_dict()
    assert summary["dropped_by_use_case"] == {u: asked_by[u] - by_winner.get(u, 0) for u in USE_CASES}
    assert sum(summary["dropped_by_use_case"].values()) == summary["dropped_actions_count"]
    # How each customer was decided adds up to the reasons written on the rows.
    reasons = won["arbitration_reason"].value_counts().to_dict()
    assert summary["customers_decided_by_value"] == reasons.get("net_value", 0) + reasons.get(
        "expected_gross_value", 0
    )
    assert summary["customers_decided_by_priority"] == reasons.get("priority", 0)
    assert summary["customers_decided_by_request_order"] == reasons.get("request_order", 0) + reasons.get(
        "explore_request_order", 0
    )
    assert summary["customers_decided_by_explore"] == reasons.get("explore_treated", 0)
    # The Results screen reads the same numbers from `GET /decide/conflicts`.
    shown = journey.secured.viewer.get("/decide/conflicts")
    assert (
        shown.status_code == 200
        and shown.json()["customers_with_conflicts"] == summary["customers_with_conflicts"]
    )


def test_the_arbitrated_download_is_for_analysts_and_audited(
    journey: Journey, arbitrated: Arbitrated
) -> None:
    del arbitrated
    assert journey.secured.viewer.get("/decide/arbitrated-treat-list.csv").status_code == 403
    events = audit_log_at(journey.api.data_dir).query(
        AuditQuery(actor_id=journey.secured.analyst_id, action="decide.arbitrated_treat_list_download")
    )
    assert any(event.outcome == "success" for event in events)


# ---------------------------------------------------------------------------
# 6. Campaigns created by the arbitration, given outcomes and measured
# ---------------------------------------------------------------------------
def test_the_arbitration_made_one_campaign_per_use_case_and_asking_again_makes_no_more(
    journey: Journey, arbitrated: Arbitrated
) -> None:
    made = arbitrated.body["campaigns"]
    assert [entry["use_case_id"] for entry in made] == list(USE_CASES)
    assert {entry["outcome"] for entry in made} == {"created"}
    assert len({entry["campaign_id"] for entry in made}) == 3
    assert arbitrated.body["campaign_ids"] == [entry["campaign_id"] for entry in made]
    for entry in made:
        view = journey.secured.analyst.get(f"/campaigns/{entry['campaign_id']}")
        assert view.status_code == 200, view.text
        campaign = view.json()["campaign"]
        assert campaign["run_ids"] == [journey.score[entry["use_case_id"]].run_id]
        assert campaign["treatment_start_source"] == "run_finished"
        assert campaign["holdout_scope"] == "universal" and campaign["causal"] is True
        assert campaign["arbitration_id"].startswith("a_")
    again = journey.secured.analyst.post("/decide/arbitrate", json={"use_cases": list(USE_CASES)})
    assert again.status_code == 200, again.text
    assert {entry["outcome"] for entry in again.json()["campaigns"]} == {"reused"}
    assert again.json()["campaign_ids"] == arbitrated.body["campaign_ids"]


def test_each_campaigns_arms_hold_only_customers_its_use_case_won_or_its_holdout_kept_back(
    journey: Journey,
    lists: dict[str, pd.DataFrame],
    arbitrated: Arbitrated,
    measured: dict[str, Measured],
) -> None:
    """DEC-1311 (n). The campaign record is read from its stored assignment: no route returns it row by row."""
    storage = journey.api.storage
    table = arbitrated.table
    for use_case, outcome in measured.items():
        assignment = read_frame(storage, campaign_key(outcome.campaign_id, ASSIGNMENT_FILENAME))
        assignment[KEY] = assignment[KEY].astype(str)
        within = assignment[assignment["intended"]]
        compared = set(within.loc[within["arm"] == "treated", KEY])
        held_back = set(within.loc[within["arm"] == "holdout", KEY])
        won = table[flag(table["treat"]) & (table["winning_use_case"] == use_case) & ~flag(table["explore"])]
        own = lists[use_case]
        held_by_this = set(own.loc[flag(own["holdout"]), KEY])
        held_by_others = {
            key
            for other, frame in lists.items()
            if other != use_case
            for key in frame.loc[flag(frame["holdout"]), KEY]
        }
        assert compared and held_back, use_case
        assert compared <= set(won[KEY]) | held_by_others, use_case
        assert held_back <= held_by_this, use_case
        # Nothing a rival won is in either arm.
        rival = table[
            flag(table["treat"]) & (table["winning_use_case"] != use_case) & ~flag(table["explore"])
        ]
        assert not (set(rival[KEY]) - held_by_others) & (compared | held_back), use_case
        counts = outcome.view["campaign"]["counts"]
        assert counts["intended_treated"] == len(compared) and counts["intended_holdout"] == len(held_back)


def test_a_campaign_measured_too_early_gives_a_date_and_never_a_number(
    measured: dict[str, Measured],
) -> None:
    early = measured[RISK].early
    assert early is not None and early["status"] == 409
    body = early["body"]
    assert body["detail"]["code"] == "CAMPAIGN_NOT_MATURED"
    start = datetime.fromisoformat(measured[RISK].view["campaign"]["treatment_start"])
    assert body["results_available_on"] == (start + timedelta(days=30)).date().isoformat()
    assert set(body) == {"detail", "results_available_on"}, "no rate, no lift, no count"


def test_every_campaign_is_measured_with_an_effect_and_an_interval_that_holds_the_planted_truth(
    journey: Journey, measured: dict[str, Measured]
) -> None:
    storage = journey.api.storage
    for use_case, outcome in measured.items():
        view = outcome.view
        report, counts = view["report"], view["campaign"]["counts"]
        assert view["campaign"]["status"] == "measured" and view["verdict"] is not None, use_case
        assert report["status"] == "mature" and report["early_look"] is False and report["causal"] is True
        assert report["treated_rows"] == counts["intended_treated"], use_case
        assert report["control_rows"] == counts["intended_holdout"], use_case
        assert report["rows_without_outcome"] == 0 and report["rows_immature"] == 0
        lift = report["absolute_lift"]
        assert lift is not None and lift["ci_low"] is not None and lift["ci_high"] is not None
        # The planted truth: the average true effect on the customers who were contacted.
        assignment = read_frame(storage, campaign_key(outcome.campaign_id, ASSIGNMENT_FILENAME))
        within = assignment[assignment["intended"] & (assignment["arm"] == "treated")]
        truth = float(outcome.planted.effect.loc[within[KEY].astype(str)].mean())
        assert truth > 0.05, use_case
        assert lift["ci_low"] <= truth <= lift["ci_high"], (use_case, truth, lift)
        assert lift["ci_low"] > 0.0 and report["p_value"] < 0.05, use_case
        assert view["verdict"]["kind"] == "added", use_case
