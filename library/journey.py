"""The whole product journey on a public randomised dataset, through the product's own API (Plan J M110).

`library/run_engine.py` trains one model on one file and reads its artefacts. A randomised dataset can
answer more than that: whether the campaign-effect model earns its place, which offer each customer
should get, what that list is worth, and whether the money can be proven. This module drives every one of
those steps the way a person does - through `api.main.create_app`'s routes, in-process - and collects
what each step's artefacts say. It owns no modelling and no statistics of its own except one: the
off-policy value of the chosen list on the evaluation rows (`_ips`), a plain inverse-probability mean of
known, randomised assignment, written out below so it can be checked by hand.

**The journey** (`run_journey`), each step recorded whichever way it falls:

1. **Split.** The prepared file is split once, by a fixed seed, into *training* rows and *evaluation*
   rows, stratified by treatment group and outcome. Every model, check and setting is fitted on the
   training rows; the evaluation rows are used only to measure, never to choose anything.
2. **Readiness and power.** The uplift checks (`TREATMENT_NOT_RANDOM`, arm sizes) on both parts, and the
   test planner (`POST /measurement/power-preview`, and the campaign's own `plan-preview`).
3. **Train.** The risk model (`POST /runs`, Phase 1) on the training rows, then the campaign-effect
   model of every offer (`POST /uplift/runs`, M100) on the same rows.
4. **Approval checks.** `engine.model_gates.approval_checks` - the Approver's screen's function: beats
   risk, stability across folds and calibration (M96).
5. **Treat list.** The evaluation rows' features are scored (`POST /runs`, mode score): the offer per
   customer by net value (M97/M100), the engine's own random control group.
6. **Measure, off-policy.** The chosen policy's value on the evaluation rows, by inverse probability
   weighting of their logged, random e-mail group, against sending nothing and against each e-mail to
   everyone, with 95% intervals.
7. **Measure, as a campaign.** The list is recorded as a campaign (`POST /campaigns`), a test plan is
   registered before any outcome is read, and its outcomes are the evaluation rows' own, by
   **replay**: a customer keeps their outcome only when the e-mail they were randomly sent is the one
   the list gave them (or none, for the engine's control group). Because the dataset's e-mail was drawn
   at random independently of the list, the kept customers are a random third of each arm, and the
   comparison stays a randomised one (Li et al., 2011). Two campaigns: conversion (yes/no) and spend
   (an amount, adjusted by the pre-registered `history` covariate, M102).
8. **Value Proof Pack** (M104) of each campaign, read back as JSON, HTML and PDF, and its provenance
   re-verified (`engine.pilot.proof.verify_provenance`).

Nothing here is engine code. Run ids are pinned (`<prefix>01` ...), so every seed the engine derives from
a run id is the same on every run; the split seed is fixed; and the risk model is LightGBM alone, so a
re-run on the same file reproduces the same numbers.
"""

from __future__ import annotations

import io
import json
import math
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any, Final

if TYPE_CHECKING:
    import numpy as np
    import numpy.typing as npt
    import pandas as pd
    from fastapi.testclient import TestClient

Z_95: Final = 1.959963984540054
"""The two-sided 95% normal quantile, as `engine.uplift.incrementality.Z_95` gives it."""
RUN_TIMEOUT_S: Final = 3600.0


# ---------------------------------------------------------------------------
# What a journey needs to know about its dataset
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class JourneySpec:
    """Everything dataset-specific about a journey. Nothing in the steps branches on the dataset."""

    dataset: str
    use_case: str
    primary_key: str
    treatment: str
    target: str
    levels: tuple[str, ...]
    """The treatment column's values, the control first."""
    outcomes: tuple[str, ...]
    """Every outcome column; none of them is uploaded for scoring."""
    amount: str
    """The amount outcome measured with CUPED."""
    covariate: str
    """The pre-campaign amount the amount is adjusted by (registered in the test plan)."""
    value_source: str
    """The column a customer's value per conversion is derived from."""
    value_column: str
    """The derived value column's name (in rupees)."""
    fx_inr_per_usd: float
    """The file's money is dollars; the engine's is rupees. An input assumption, recorded."""
    fx_note: str
    contact_cost_inr: float
    eval_share: float
    split_seed: int
    run_prefix: str
    """Run ids are `r_<date>_<prefix><nn>`; 6 hex characters."""
    run_date: str
    """`yyyymmdd` of the pinned run ids."""
    campaign_label: str
    """Every campaign name carries it: what kind of evidence this is."""
    risk_overrides: dict[str, Any] = field(default_factory=dict)
    uplift_overrides: dict[str, Any] = field(default_factory=dict)
    score_overrides: dict[str, Any] = field(default_factory=dict)
    """A scoring run resolves the use case afresh, so the value column is named again (M97)."""
    plan_metric: dict[str, str] = field(default_factory=dict)
    """Test-plan wording per outcome measured."""


HILLSTROM: Final = JourneySpec(
    dataset="hillstrom-email",
    use_case="hillstrom-email",
    primary_key="customer_id",
    treatment="segment",
    target="conversion",
    levels=("No E-Mail", "Mens E-Mail", "Womens E-Mail"),
    outcomes=("visit", "conversion", "spend"),
    amount="spend",
    covariate="history",
    value_source="history",
    value_column="value_inr",
    fx_inr_per_usd=83.0,
    fx_note=(
        "83 rupees per US dollar: an input assumption close to the 2024 average reference rate, used only to "
        "express the file's 2008 dollars in the engine's rupees. Every rupee figure scales with it."
    ),
    contact_cost_inr=0.05,
    eval_share=0.5,
    split_seed=20261010,
    run_prefix="111000",
    run_date="20261010",
    campaign_label="public dataset, retrospective",
    risk_overrides={
        "model_search.strategy": "fast",
        "model_search.time_limit_minutes": 10,
        "model_search.tuning_trials": 5,
        "model_search.candidates": ["LightGBM"],
        "governance.approval_required": False,
    },
    uplift_overrides={
        "uplift.treatment_levels": ["No E-Mail", "Mens E-Mail", "Womens E-Mail"],
        "uplift.policy.value_column": "value_inr",
        "governance.approval_required": False,
    },
    score_overrides={"uplift.policy.value_column": "value_inr"},
    plan_metric={
        "conversion": "bought within two weeks of the e-mail",
        "spend": "dollars spent within two weeks of the e-mail",
    },
)

JOURNEYS: Final[dict[str, JourneySpec]] = {HILLSTROM.dataset: HILLSTROM}


# ---------------------------------------------------------------------------
# Driving the API
# ---------------------------------------------------------------------------
class JourneyError(RuntimeError):
    """A step the journey cannot go on without was refused; the message says which and why."""


@dataclass
class _Api:
    client: TestClient
    data_dir: Path
    spec: JourneySpec
    run_count: int = 0

    def next_run_id(self) -> str:
        self.run_count += 1
        return f"r_{self.spec.run_date}_{self.spec.run_prefix}{self.run_count:02d}"

    def ok(self, response: Any, *expected: int) -> Any:
        codes = expected or (200,)
        if response.status_code not in codes:
            raise JourneyError(
                f"{response.request.method} {response.request.url.path}: {response.status_code} {response.text[:2000]}"
            )
        return (
            response.json()
            if response.content and response.headers.get("content-type", "").startswith("application/json")
            else response
        )

    def upload(self, frame: pd.DataFrame, *, mode: str, name: str) -> str:
        payload = frame.to_csv(index=False, lineterminator="\n").encode("utf-8")
        body = self.ok(
            self.client.post(
                "/uploads",
                files={"file": (f"{name}.csv", payload, "text/csv")},
                data={"use_case": self.spec.use_case, "mode": mode},
            ),
            201,
        )
        return str(body["upload_id"])

    def start(self, path: str, body: dict[str, Any]) -> tuple[str, Any]:
        """POST a run under a pinned id; return the id and the response (202, or a refusal)."""
        from engine import runs as engine_runs

        run_id = self.next_run_id()
        names = vars(engine_runs)  # the id a run is created under, pinned as the API tests pin it
        original = names["new_run_id"]
        names["new_run_id"] = lambda _moment=None: run_id
        try:
            response = self.client.post(path, json=body)
        finally:
            names["new_run_id"] = original
        return run_id, response

    def finish(self, run_id: str) -> dict[str, Any]:
        deadline = time.monotonic() + RUN_TIMEOUT_S
        while True:
            body = self.ok(self.client.get(f"/runs/{run_id}"))
            record = body["run"]
            if record["state"] in {"done", "failed", "cancelled"}:
                return dict(record)
            if time.monotonic() > deadline:
                raise JourneyError(f"run {run_id} did not finish in {RUN_TIMEOUT_S:.0f} s")
            time.sleep(0.5)

    def artefact(self, run_id: str, name: str) -> bytes:
        from engine.storage import LocalStorage, run_key

        return LocalStorage(self.data_dir).read_bytes(run_key(run_id, name))

    def artefact_json(self, run_id: str, name: str) -> Any:
        try:
            return json.loads(self.artefact(run_id, name))
        except Exception:  # an artefact a run did not write is a fact to record, not a crash
            return None


def _sha256(path: Path) -> str:
    import hashlib

    return hashlib.sha256(path.read_bytes()).hexdigest()


@contextmanager
def _app(config_root: Path, data_dir: Path) -> Iterator[TestClient]:
    from fastapi.testclient import TestClient

    from api.main import create_app

    with TestClient(create_app(config_root=config_root, data_dir=data_dir)) as client:
        yield client


# ---------------------------------------------------------------------------
# Step 1: the split, and the value column
# ---------------------------------------------------------------------------
def split_rows(frame: pd.DataFrame, spec: JourneySpec) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Training and evaluation rows: a seeded share of every (treatment group, outcome) stratum."""
    import numpy as np

    rng = np.random.default_rng(spec.split_seed)
    evaluation = np.zeros(len(frame), dtype=bool)
    strata = frame.groupby([spec.treatment, spec.target], sort=True).indices
    for _key, positions in sorted(strata.items(), key=lambda item: str(item[0])):
        chosen = rng.permutation(positions)[: round(spec.eval_share * len(positions))]
        evaluation[chosen] = True
    return frame.loc[~evaluation].reset_index(drop=True), frame.loc[evaluation].reset_index(drop=True)


def value_scale(train: pd.DataFrame, spec: JourneySpec) -> dict[str, Any]:
    """How a customer's value per conversion is derived from their spend history, from training rows only.

    One conversion is worth the customer's past-year spend scaled to the size of one order: the scale is
    the training rows' mean `amount` among converters over their mean `value_source`. In rupees at the
    journey's exchange-rate assumption. Revenue, before any margin.
    """
    converted = train[train[spec.target] == 1]
    mean_order = float(converted[spec.amount].mean())
    mean_history = float(converted[spec.value_source].mean())
    scale = mean_order / mean_history
    return {
        "converters": len(converted),
        "mean_order_usd": mean_order,
        "mean_history_usd": mean_history,
        "scale": scale,
        "fx_inr_per_usd": spec.fx_inr_per_usd,
        "fx_note": spec.fx_note,
        "value_per_conversion_inr": mean_order * spec.fx_inr_per_usd,
        "formula": f"{spec.value_column} = {spec.value_source} x scale x fx_inr_per_usd",
    }


def with_value(frame: pd.DataFrame, spec: JourneySpec, scale: float) -> pd.DataFrame:
    out = frame.copy()
    out[spec.value_column] = (out[spec.value_source] * scale * spec.fx_inr_per_usd).round(2)
    return out


# ---------------------------------------------------------------------------
# Step 6: the off-policy value of a policy on randomised rows
# ---------------------------------------------------------------------------
def _interval(terms: npt.NDArray[np.float64]) -> dict[str, float | None]:
    """mean ± z·sd/√n of per-row terms (`engine.uplift.ope`'s IPS interval)."""
    import numpy as np

    n = len(terms)
    mean = float(np.mean(terms))
    if n < 2:
        return {"value": mean, "ci_low": None, "ci_high": None}
    half = Z_95 * float(np.std(terms, ddof=1)) / math.sqrt(n)
    return {"value": mean, "ci_low": mean - half, "ci_high": mean + half}


def ips_value(
    logged: npt.NDArray[np.integer[Any]],
    outcome: npt.NDArray[np.float64],
    policy: npt.NDArray[np.integer[Any]],
    shares: npt.NDArray[np.float64],
) -> npt.NDArray[np.float64]:
    """Per-row terms of the inverse-probability estimate of a policy's mean outcome.

    `logged` is the arm each row was randomly given (0 the control), `policy` the arm the policy gives
    it, `shares` each arm's probability (the share of rows randomised to it). A row whose logged arm is
    the policy's counts `y / P(arm)`; every other row counts zero. Unbiased because the logged arm was
    drawn at random with these probabilities.
    """
    import numpy as np

    weight = 1.0 / shares[logged]
    terms: npt.NDArray[np.float64] = np.where(logged == policy, outcome * weight, 0.0)
    return terms


def off_policy(
    logged: npt.NDArray[np.integer[Any]],
    outcomes: dict[str, npt.NDArray[np.float64]],
    policies: dict[str, npt.NDArray[np.integer[Any]]],
    *,
    baseline: str,
    shares: npt.NDArray[np.float64],
) -> dict[str, Any]:
    """Each policy's value per customer and its difference from `baseline`, paired on the same rows."""
    out: dict[str, Any] = {}
    for outcome_name, y in outcomes.items():
        base_terms = ips_value(logged, y, policies[baseline], shares)
        rows: dict[str, Any] = {}
        for name, policy in policies.items():
            terms = ips_value(logged, y, policy, shares)
            rows[name] = {
                "value": _interval(terms),
                "difference_from_" + baseline: _interval(terms - base_terms),
            }
        out[outcome_name] = rows
    return out


# ---------------------------------------------------------------------------
# The journey
# ---------------------------------------------------------------------------
@dataclass
class JourneyOutcome:
    results: dict[str, Any]
    directory: Path


def _counts(frame: pd.DataFrame, spec: JourneySpec) -> dict[str, Any]:
    groups: dict[str, Any] = {}
    for level in spec.levels:
        part = frame[frame[spec.treatment] == level]
        groups[level] = {
            "rows": len(part),
            "conversions": int(part[spec.target].sum()),
            "conversion_rate": float(part[spec.target].mean()) if len(part) else None,
            "visit_rate": float(part["visit"].mean()) if "visit" in part.columns and len(part) else None,
            "mean_" + spec.amount: float(part[spec.amount].mean()) if len(part) else None,
        }
    return {"rows": len(frame), "conversions": int(frame[spec.target].sum()), "groups": groups}


def _uplift_checks_on(
    frame: pd.DataFrame, spec: JourneySpec, config_root: Path, upload_id: str, overrides: dict[str, Any]
) -> dict[str, Any]:
    """The six uplift checks on a frame (the same function `POST /uplift/runs` calls before training)."""
    from engine.config import resolve_config
    from engine.uplift.checks import run_uplift_checks
    from engine.uplift.flow import check_seed

    merged = {"problem_type": "uplift", "uplift.treatment_column": spec.treatment, **overrides}
    config = resolve_config(spec.use_case, merged, root=config_root).config
    checked = run_uplift_checks(
        frame,
        config,
        primary_key=spec.primary_key,
        target=spec.target,
        upload_id=upload_id,
        acknowledged=config.validation.acknowledged,
        seed=check_seed(upload_id),
    )
    return {
        "passed": checked.report.passed,
        "checks": [
            {"code": c.code, "severity": c.severity.value, "message": c.message, "details": c.details}
            for c in checked.report.checks
        ],
    }


def run_journey(
    spec: JourneySpec,
    *,
    csv_path: Path,
    config_root: Path,
    runs_dir: Path,
    extra_risk_overrides: dict[str, Any] | None = None,
    extra_uplift_overrides: dict[str, Any] | None = None,
) -> JourneyOutcome:
    """Run every step on `csv_path` and return what each one produced (see the module docstring).

    `runs_dir` holds the API's data directory for this journey; the CLI uses `library/.runs/`, a test a
    temporary directory. The extra overrides exist for the library's tests, which run the journey on the
    committed sample with smaller floors; the report records every override used.
    """
    import numpy as np
    import pandas as pd

    started = time.perf_counter()
    directory = runs_dir / spec.dataset / "journey"
    data_dir = directory / "data"
    if data_dir.exists():
        import shutil

        shutil.rmtree(data_dir)  # pinned run ids: a journey always starts from an empty store
    data_dir.mkdir(parents=True)
    risk_overrides = {**spec.risk_overrides, **(extra_risk_overrides or {})}
    uplift_overrides = {**spec.uplift_overrides, **(extra_uplift_overrides or {})}

    frame = pd.read_csv(csv_path)
    train, evaluation = split_rows(frame, spec)
    scale = value_scale(train, spec)
    train_v = with_value(train, spec, scale["scale"])
    eval_v = with_value(evaluation, spec, scale["scale"])
    results: dict[str, Any] = {
        "dataset": spec.dataset,
        "use_case": spec.use_case,
        "csv": str(csv_path),
        "csv_sha256": _sha256(csv_path),
        "rows": len(frame),
        "split": {
            "seed": spec.split_seed,
            "eval_share": spec.eval_share,
            "stratified_by": [spec.treatment, spec.target],
            "training": _counts(train, spec),
            "evaluation": _counts(evaluation, spec),
        },
        "value": scale,
        "contact_cost_inr": spec.contact_cost_inr,
        "risk_overrides": risk_overrides,
        "uplift_overrides": uplift_overrides,
        "label": spec.campaign_label,
        "steps": {},
    }
    steps: dict[str, Any] = results["steps"]

    with _app(config_root, data_dir) as client:
        api = _Api(client=client, data_dir=data_dir, spec=spec)
        train_upload = api.upload(train_v, mode="train", name="training")

        # -- 2. readiness: the uplift checks on both parts, the planner on the evaluation rows ----------
        steps["readiness"] = {
            "training_checks": _uplift_checks_on(train_v, spec, config_root, train_upload, uplift_overrides),
            "evaluation_checks": _uplift_checks_on(
                eval_v, spec, config_root, "u_evaluation_rows", uplift_overrides
            ),
        }
        control = results["split"]["training"]["groups"][spec.levels[0]]
        intended_guess = len(evaluation)
        power_body = {
            "eligible": intended_guess,
            "base_rate": control["conversion_rate"],
            "holdout_shares": [0.1, 0.2, 0.3, 0.5],
            "value_per_conversion": round(scale["value_per_conversion_inr"], 2),
            "contact_cost": spec.contact_cost_inr,
            "direction": "up",
        }
        replay_body = {**power_body, "eligible": max(1, intended_guess // len(spec.levels))}
        steps["power"] = {
            "evaluation_rows": {
                "request": power_body,
                "preview": api.ok(client.post("/measurement/power-preview", json=power_body)),
            },
            "replay_third": {
                "request": replay_body,
                "preview": api.ok(client.post("/measurement/power-preview", json=replay_body)),
            },
        }

        # -- 3a. the risk model (Phase 1) -----------------------------------------------------------------
        risk_started = time.perf_counter()
        risk_id, response = api.start(
            "/runs",
            {
                "use_case": spec.use_case,
                "mode": "train",
                "upload_id": train_upload,
                "primary_key": spec.primary_key,
                "target": spec.target,
                "overrides": risk_overrides,
            },
        )
        api.ok(response, 202)
        risk_record = api.finish(risk_id)
        steps["risk_model"] = _risk_summary(api, risk_id, risk_record, time.perf_counter() - risk_started)
        if risk_record["state"] != "done":
            raise JourneyError(f"the risk model did not train: {risk_record.get('error')}")

        # -- 3b. the campaign-effect model of every offer (M100) ---------------------------------------
        uplift_started = time.perf_counter()
        uplift_id, response = api.start(
            "/uplift/runs",
            {
                "use_case": spec.use_case,
                "upload_id": train_upload,
                "primary_key": spec.primary_key,
                "target": spec.target,
                "treatment_column": spec.treatment,
                "overrides": uplift_overrides,
            },
        )
        if response.status_code != 202:
            steps["uplift_model"] = {"refused": response.status_code, "body": response.json()}
            raise JourneyError(f"the campaign-effect run was refused: {response.text[:2000]}")
        uplift_record = api.finish(uplift_id)
        steps["uplift_model"] = _uplift_summary(
            api, uplift_id, uplift_record, time.perf_counter() - uplift_started
        )
        if uplift_record["state"] != "done":
            raise JourneyError(f"the campaign-effect model did not train: {uplift_record.get('error')}")

        # -- 4. approval checks (M96), as the Approver's screen reads them ------------------------------
        steps["approval"] = _approval(api, uplift_record)

        # -- 5. the treat list on the evaluation rows ---------------------------------------------------
        features = eval_v.drop(columns=[spec.treatment, *spec.outcomes])
        score_upload = api.upload(features, mode="score", name="evaluation_features")
        score_id, response = api.start(
            "/runs",
            {
                "use_case": spec.use_case,
                "mode": "score",
                "upload_id": score_upload,
                "primary_key": spec.primary_key,
                "model_version_id": uplift_record["model_version_id"],
                "overrides": spec.score_overrides,
            },
        )
        api.ok(response, 202)
        score_record = api.finish(score_id)
        if score_record["state"] != "done":
            raise JourneyError(f"the scoring run failed: {score_record.get('error')}")
        choice = pd.read_parquet(io.BytesIO(api.artefact(score_id, "offer_choice.parquet")))
        choice[spec.primary_key] = choice[spec.primary_key].astype(np.int64)
        steps["treat_list"] = _treat_summary(api, score_id, score_record, choice)

        # -- 6. off-policy value on the evaluation rows ------------------------------------------------
        steps["off_policy"] = _off_policy_step(spec, evaluation, choice)

        # -- 7 and 8. the campaigns (replay) and their Value Proof Packs ---------------------------------
        steps["campaigns"] = {}
        for outcome_kind, outcome in (("binary", spec.target), ("continuous", spec.amount)):
            steps["campaigns"][outcome] = _campaign(
                api,
                spec,
                score_id=score_id,
                evaluation=eval_v,
                choice=choice,
                train=train,
                outcome=outcome,
                outcome_kind=outcome_kind,
                scale=scale,
                directory=directory,
            )

    results["wall_clock_seconds"] = round(time.perf_counter() - started, 1)
    out = directory / "journey.results.json"
    out.write_text(json.dumps(results, indent=2, default=str), encoding="utf-8")
    return JourneyOutcome(results=results, directory=directory)


# ---------------------------------------------------------------------------
# Step summaries: what each run's artefacts say
# ---------------------------------------------------------------------------
def _risk_summary(api: _Api, run_id: str, record: dict[str, Any], seconds: float) -> dict[str, Any]:
    evaluation = api.artefact_json(run_id, "evaluation.json") or {}
    baseline = api.artefact_json(run_id, "baseline.json") or {}
    leaderboard = api.artefact_json(run_id, "leaderboard.json") or {}
    best = api.artefact_json(run_id, "best_model.json") or {}
    metrics = {m["id"]: m["value"] for m in evaluation.get("metrics", [])}
    return {
        "run_id": run_id,
        "state": record["state"],
        "error": record.get("error"),
        "model_version_id": record.get("model_version_id"),
        "wall_clock_seconds": round(seconds, 1),
        "models_trained": leaderboard.get("models_trained"),
        "best_model": best.get("display_name"),
        "test_metrics": metrics,
        "positive_rate": evaluation.get("positive_rate"),
        "baseline_name": baseline.get("baseline_name"),
        "beats_baseline": baseline.get("model_beats_baseline"),
        "baseline_rows": baseline.get("rows"),
    }


def _uplift_summary(api: _Api, run_id: str, record: dict[str, Any], seconds: float) -> dict[str, Any]:
    from engine.registry import REGISTRY_FILENAME, LocalModelRegistry

    evaluation = api.artefact_json(run_id, "uplift_evaluation.json") or {}
    version_id = record.get("model_version_id")
    status = None
    if version_id:
        version = LocalModelRegistry(api.data_dir / REGISTRY_FILENAME).get(version_id)
        status = version.status.value
    return {
        "run_id": run_id,
        "state": record["state"],
        "error": record.get("error"),
        "model_version_id": version_id,
        "model_status": status,
        "wall_clock_seconds": round(seconds, 1),
        "validation": api.artefact_json(run_id, "uplift_validation.json"),
        "evaluation": evaluation,
        "segments": api.artefact_json(run_id, "segments.json"),
        "policy": api.artefact_json(run_id, "policy_recommendation.json"),
        "arm_policy_value": api.artefact_json(run_id, "arm_policy_value.json"),
    }


def _approval(api: _Api, record: dict[str, Any]) -> dict[str, Any]:
    from engine.model_gates import approval_checks
    from engine.registry import REGISTRY_FILENAME, LocalModelRegistry
    from engine.storage import LocalStorage

    version = LocalModelRegistry(api.data_dir / REGISTRY_FILENAME).get(record["model_version_id"])
    checks = approval_checks(LocalStorage(api.data_dir), version)
    return {"checks": [c.model_dump() for c in checks]}


def _treat_summary(api: _Api, run_id: str, record: dict[str, Any], choice: pd.DataFrame) -> dict[str, Any]:
    summary = api.artefact_json(run_id, "offer_choice.json")
    ranking = api.artefact_json(run_id, "ranking_choice.json")
    response = api.ok(api.client.get(f"/runs/{run_id}/treat_list.csv"))  # built on first request (M98)
    import pandas as pd

    table = pd.read_csv(io.StringIO(response.content.decode("utf-8")), dtype=str, keep_default_na=False)
    treated = table[table["treat"] == "1"]
    # The scores' own action column (the first offer's list, re-ranked by the risk model when the J5
    # fallback applies) against whether the offer choice gave the customer an e-mail.
    scores = pd.read_parquet(
        io.BytesIO(api.artefact(run_id, "scores.parquet")), columns=["customer_id", "action"]
    )
    scores["customer_id"] = scores["customer_id"].astype(choice["customer_id"].dtype)
    both = scores.merge(choice[["customer_id", "offer_arm"]], on="customer_id", how="inner")
    action_vs_offer = {
        str(action): {
            "offered": int((group["offer_arm"] > 0).sum()),
            "not_offered": int((group["offer_arm"] == 0).sum()),
        }
        for action, group in both.groupby("action", sort=True)
    }
    return {
        "scores_action_vs_offer": action_vs_offer,
        "run_id": run_id,
        "state": record["state"],
        "offer_choice": summary,
        "ranking_choice": ranking,
        "treat_list_rows": len(table),
        "treat_rows": len(treated),
        "offer_counts": treated["offer"].value_counts().sort_index().to_dict(),
        "policy_offer_counts": choice["policy_offer_arm"].value_counts().sort_index().to_dict(),
        "columns": list(table.columns),
    }


def _off_policy_step(spec: JourneySpec, evaluation: pd.DataFrame, choice: pd.DataFrame) -> dict[str, Any]:
    import numpy as np

    joined = evaluation.merge(
        choice[[spec.primary_key, "policy_offer_arm"]], on=spec.primary_key, how="left", validate="one_to_one"
    )
    missing = int(joined["policy_offer_arm"].isna().sum())
    codes = {level: k for k, level in enumerate(spec.levels)}
    logged = joined[spec.treatment].map(codes).to_numpy(dtype=np.int_)
    chosen = joined["policy_offer_arm"].fillna(0).to_numpy(dtype=np.int_)
    shares = np.bincount(logged, minlength=len(spec.levels)) / float(len(logged))
    policies: dict[str, Any] = {"no_email": np.zeros_like(logged), "chosen": chosen}
    for k, level in enumerate(spec.levels[1:], start=1):
        policies["everyone_" + level] = np.full_like(logged, k)
    contacts = {name: int((policy > 0).sum()) for name, policy in policies.items()}
    outcomes = {
        spec.target: joined[spec.target].to_numpy(dtype=np.float64),
        "visit": joined["visit"].to_numpy(dtype=np.float64),
        spec.amount: joined[spec.amount].to_numpy(dtype=np.float64),
    }
    estimates = off_policy(logged, outcomes, policies, baseline="no_email", shares=shares)
    # Chosen against the better of the e-mails sent to everyone, paired on the same rows.
    paired: dict[str, Any] = {}
    for outcome_name, y in outcomes.items():
        chosen_terms = ips_value(logged, y, chosen, shares)
        for k, level in enumerate(spec.levels[1:], start=1):
            blanket = ips_value(logged, y, np.full_like(logged, k), shares)
            paired.setdefault(outcome_name, {})["chosen_minus_everyone_" + level] = _interval(
                chosen_terms - blanket
            )
    # Money: the amount in rupees, less the e-mails sent, against sending nothing (revenue, before margin).
    rows = len(logged)
    money: dict[str, Any] = {}
    scale = rows * spec.fx_inr_per_usd
    for name in policies:
        diff = estimates[spec.amount][name]["difference_from_no_email"]
        cost = spec.contact_cost_inr * contacts[name]
        low, high = diff["ci_low"], diff["ci_high"]
        money[name] = {
            "incremental_revenue_inr": diff["value"] * scale,
            "contact_cost_inr": cost,
            "net_inr": diff["value"] * scale - cost,
            "net_ci_low_inr": None if low is None else low * scale - cost,
            "net_ci_high_inr": None if high is None else high * scale - cost,
        }
    return {
        "rows": rows,
        "rows_without_policy": missing,
        "arm_shares": {level: float(shares[k]) for k, level in enumerate(spec.levels)},
        "policy_shares": {level: float(np.mean(chosen == k)) for k, level in enumerate(spec.levels)},
        "contacts": contacts,
        "estimates": estimates,
        "chosen_against_everyone": paired,
        "money_on_evaluation_rows": money,
        "estimator": "inverse probability weighting with the evaluation rows' own arm shares; 95% normal intervals",
    }


def _campaign(
    api: _Api,
    spec: JourneySpec,
    *,
    score_id: str,
    evaluation: pd.DataFrame,
    choice: pd.DataFrame,
    train: pd.DataFrame,
    outcome: str,
    outcome_kind: str,
    scale: dict[str, Any],
    directory: Path,
) -> dict[str, Any]:
    """Record the list as a campaign, register its plan, add the replayed outcomes, measure, build the pack."""
    import numpy as np
    import pandas as pd

    from engine.measurement.campaign import ASSIGNMENT_FILENAME, campaign_key, read_frame
    from engine.pilot.proof import ProofView, verify_provenance
    from engine.storage import LocalStorage

    client = api.client
    storage = LocalStorage(api.data_dir)
    name = f"Hillstrom e-mail ({outcome}) - {spec.campaign_label}"
    view = api.ok(
        client.post("/campaigns", json={"run_id": score_id, "outcome_window_days": 0, "name": name}), 201
    )
    campaign = view["campaign"]
    campaign_id = campaign["campaign_id"]
    start = pd.Timestamp(campaign["treatment_start"])

    # The plan, registered before any outcome is read; its inputs come from the training rows only.
    control = train[train[spec.treatment] == spec.levels[0]]
    plan: dict[str, Any] = {
        "metric": spec.plan_metric[outcome],
        "outcome_column": outcome,
        "outcome_kind": outcome_kind,
        "analysis_date": start.date().isoformat(),
        "expectation": "Retrospective replay of a public randomised e-mail test; no expectation was set in advance.",
    }
    if outcome_kind == "binary":
        plan["positive_label"] = "1"
        plan["base_rate"] = float(control[outcome].mean())
        plan["base_rate_source"] = "the training rows' no-e-mail group"
    else:
        rho = float(np.corrcoef(control[outcome], control[spec.covariate])[0, 1])
        plan["covariate_column"] = spec.covariate
        plan["outcome_sd"] = float(control[outcome].std(ddof=1))
        plan["expected_rho2"] = min(rho * rho, 0.99)
    registered = api.ok(client.post(f"/campaigns/{campaign_id}/plan", json=plan), 200, 201)
    worth = scale["value_per_conversion_inr"] if outcome_kind == "binary" else spec.fx_inr_per_usd
    preview = api.ok(
        client.get(
            f"/campaigns/{campaign_id}/plan-preview",
            params={"value_per_conversion": round(worth, 2), "contact_cost": spec.contact_cost_inr},
        )
    )

    # Replay: keep a customer's outcome only when their random e-mail is the one the list gave them.
    assignment = read_frame(storage, campaign_key(campaign_id, ASSIGNMENT_FILENAME))
    assignment[spec.primary_key] = assignment[spec.primary_key].astype(np.int64)
    assignment = assignment[
        [spec.primary_key, "arm", "intended"]
    ]  # its `segment` is the model's, not the file's
    frame = assignment.merge(
        choice[[spec.primary_key, "offer_arm", "policy_offer_arm"]], on=spec.primary_key, how="left"
    ).merge(evaluation, on=spec.primary_key, how="left", validate="one_to_one")
    codes = {level: k for k, level in enumerate(spec.levels)}
    logged = frame[spec.treatment].map(codes).to_numpy(dtype=np.int_)
    intended = frame["intended"].to_numpy(dtype=bool)
    treated = (frame["arm"] == "treated").to_numpy(dtype=bool)
    held = (frame["arm"] == "holdout").to_numpy(dtype=bool)
    given = np.where(treated, frame["offer_arm"].fillna(0).to_numpy(dtype=np.int_), 0)
    action = np.where(held, 0, given)
    kept = intended & (treated | held) & (logged == action)
    columns = [spec.primary_key, outcome]
    out = frame.loc[kept, columns].copy()
    outcomes_body: dict[str, Any] = {"outcome_column": outcome}
    if outcome_kind == "binary":
        outcomes_body["positive_label"] = "1"
    else:
        out[spec.covariate] = frame.loc[kept, spec.covariate].to_numpy()
        out["history_date"] = (start - timedelta(days=1)).date().isoformat()
        outcomes_body["covariate_column"] = spec.covariate
        outcomes_body["covariate_date_column"] = "history_date"
    upload_id = api.upload(out, mode="score", name=f"replayed_{outcome}")
    api.ok(client.post(f"/campaigns/{campaign_id}/outcomes", json={"upload_id": upload_id, **outcomes_body}))
    measured = api.ok(client.post(f"/campaigns/{campaign_id}/measure", json={}))

    # The campaign's own value inputs (M104), from the training rows and the stated assumptions.
    if outcome_kind == "binary":
        value_inputs = {
            "value_per_outcome": round(scale["value_per_conversion_inr"], 2),
            "outcome_is_good": True,
            "value_basis": "Mean spend of a converting customer in the training rows, in rupees at the assumed rate; revenue before margin",
            "contact_cost": spec.contact_cost_inr,
        }
    else:
        value_inputs = {
            "value_per_outcome": spec.fx_inr_per_usd,
            "outcome_is_good": True,
            "value_basis": "One dollar of spend, in rupees at the assumed rate; revenue before margin",
            "contact_cost": spec.contact_cost_inr,
        }
    api.ok(client.put(f"/pilot/proof/{campaign_id}/value", json=value_inputs))

    proof_json = client.get(f"/pilot/proof/{campaign_id}", params={"format": "json"})
    pack: dict[str, Any] = {"status": proof_json.status_code}
    if proof_json.status_code == 200:
        view_model = ProofView.model_validate_json(proof_json.content)
        verify_provenance(view_model, storage)  # raises when any figure does not resolve
        html = client.get(f"/pilot/proof/{campaign_id}", params={"format": "html"})
        pdf = client.get(f"/pilot/proof/{campaign_id}", params={"format": "pdf"})
        (directory / f"proof_{outcome}.html").write_bytes(html.content)
        (directory / f"proof_{outcome}.pdf").write_bytes(pdf.content)
        pack.update(
            {
                "provenance_verified": True,
                "html_status": html.status_code,
                "pdf_status": pdf.status_code,
                "pdf_bytes": len(pdf.content),
                "view": json.loads(proof_json.content),
            }
        )
    else:
        pack["refusal"] = proof_json.json()
    return {
        "campaign_id": campaign_id,
        "name": name,
        "campaign": measured.get("campaign"),
        "plan": registered,
        "plan_preview": preview,
        "replay": {
            "intended": int(intended.sum()),
            "intended_treated": int((intended & treated).sum()),
            "intended_held_back": int((intended & held).sum()),
            "kept_treated": int((kept & treated).sum()),
            "kept_held_back": int((kept & held).sum()),
            "kept_by_offer": {
                spec.levels[k]: int((kept & treated & (given == k)).sum()) for k in range(1, len(spec.levels))
            },
        },
        "report": measured.get("report"),
        "verdict": measured.get("verdict"),
        "value_inputs": value_inputs,
        "proof": pack,
    }
