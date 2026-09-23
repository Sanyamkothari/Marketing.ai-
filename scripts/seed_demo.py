"""Seed the demo environment: "Demo Telecom", end to end, through the platform's own API (Plan E M63).

    python -m scripts.seed_demo [--data-dir data] [--seed 20260923] [--force]

Run once, before a demo, on the machine (or against the storage) the demo will be given from. It
takes a few minutes, because it really does what a pilot does - which is the point: the screens a
visitor then clicks through show artefacts the engine made, not pictures of them, and nothing in the
demo trains while anybody watches.

1. Raw tables for 2,000 synthetic subscribers (`tests/fixtures/raw/make_raw.py`), plus the month
   after them and one broken extract with a single planted problem (the customer table repeats some
   customer IDs, `ENTITY_DUPLICATE_KEYS`), all kept under `pilot/demo/raw/` for the pre-flight check.
2. The client "Demo Telecom": every table uploaded, its detected role confirmed, the suggested
   mapping saved, the use case's suggested features and churn definition kept, a dataset built.
3. A churn model trained on it (the fast search), approved and made champion.
4. Next month's tables replayed through the same recipe and scored: bands, actions, reasons and a
   10% control group.
5. The churn campaign measured: outcomes are *simulated* for the scored customers - a customer
   leaves with a chance equal to their score, and contact cuts it by a planted quarter - and
   uploaded through outcome ingestion (the synthetic tables are dated in the past, so the 60-day
   window has passed).
6. A win-back uplift model trained on a past randomised campaign (`tests/fixtures/make_uplift_data.py`),
   made champion of the win-back use case, a new campaign scored with it, and that campaign measured
   on simulated outcomes the generator knows the true effect of.
7. Value inputs for both campaigns, labelled as demo assumptions, and the manifest
   (`pilot/demo/demo.json`) that `GET /pilot/demo` serves when `MARKETING_AI_DEMO_MODE=true`.

Synthetic only: no public or non-commercial dataset is read (Plan D, R2), and the manifest lists the
generators it used. Needs a checkout with the dev extra (`make setup`): it drives the API in-process
through FastAPI's test client and imports the generators from `tests/fixtures/`.
"""

from __future__ import annotations

import argparse
import io
import logging
import sys
import tempfile
import time
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
from fastapi.testclient import TestClient

from api.main import create_app
from engine.pilot.demo import (
    DEMO_CLIENT_NAME,
    DEMO_MANIFEST_KEY,
    EXCLUDED_DATA,
    DemoCampaign,
    DemoManifest,
    load_demo,
    raw_key,
)
from engine.pilot.roi import RoiInputs, save_roi_inputs
from engine.settings import build_storage, settings

COMMAND = "python -m scripts.seed_demo"
logger = logging.getLogger("seed_demo")

CHURN_USE_CASE = "telco-churn"
WINBACK_USE_CASE = "win-back-campaign"
CUSTOMERS = 2_000
USAGE_ROWS = 40_000
TIMEOUT_S = 1_800.0
TABLE_ROLES = {
    "customers": "entity",
    "bills": "bills",
    "payments": "payments",
    "complaints": "complaints",
    "usage": "usage",
    "activity": "activity",
}
CORRECTIONS: dict[str, dict[str, str]] = {
    "complaints": {"event_time": "TICKET_DT", "resolved_time": "RESOLVED_DT"}
}
"""What a person fixes on the mapping screen: the suggester reads the complaint's resolution date as
its event date (MAPPING_LOW_CONFIDENCE says it is unsure), and the analyst swaps them back."""

MAPPING_FIELDS = (
    "client_id",
    "source_id",
    "use_case",
    "role",
    "columns",
    "unmapped_source",
    "missing_required",
    "value_maps",
)
FAST_TRAIN = {
    "model_search.strategy": "fast",
    "model_search.time_limit_minutes": 2,
    "model_search.tuning_trials": 5,
    "model_search.candidates": ["LightGBM"],
    "model_search.ensemble": False,
}
"""A demo is seeded, not searched: one tree family, so every customer's reasons come from the fast
exact explainer rather than the sampling one (minutes instead of seconds on the ensemble)."""
FAST_UPLIFT: dict[str, Any] = {
    "uplift": {
        "base_model": "lightgbm",
        "bootstrap_samples": 50,
        "min_arm_rows": 200,
        "min_arm_positives": 20,
        "segments": {"persuadable_min_uplift": 0.05},
    },
    "governance": {"approval_required": False},
}
CONTACT_EFFECT = 0.25
"""The planted effect of the churn campaign: contact removes a quarter of a customer's chance of leaving."""

DATA_SOURCES = (
    "tests/fixtures/raw/make_raw.py (synthetic subscribers, seeded)",
    "tests/fixtures/make_uplift_data.py (synthetic win-back campaign, seeded, known effect)",
    "scripts/seed_demo.py (simulated campaign outcomes, planted effect stated in the manifest)",
)


class SeedError(RuntimeError):
    """A step of the seed did not go as a pilot's would; the message says which."""


def _ok(response: Any, status: int = 200) -> Any:
    if response.status_code != status:
        raise SeedError(
            f"{response.request.method} {response.request.url.path} -> {response.status_code}: {response.text[:600]}"
        )
    return response.json()


def _wait_run(client: TestClient, run_id: str) -> dict[str, Any]:
    deadline = time.monotonic() + TIMEOUT_S
    while time.monotonic() < deadline:
        body = _ok(client.get(f"/runs/{run_id}"))
        state = body["run"]["state"]
        if state in ("done", "failed", "cancelled"):
            if state != "done":
                raise SeedError(f"run {run_id} ended {state}: {body['status'].get('error')}")
            run: dict[str, Any] = body["run"]
            return run
        time.sleep(0.5)
    raise SeedError(f"run {run_id} did not finish in {TIMEOUT_S:.0f}s")


def _wait_build(client: TestClient, dataset_id: str) -> dict[str, Any]:
    deadline = time.monotonic() + TIMEOUT_S
    while time.monotonic() < deadline:
        body: dict[str, Any] = _ok(client.get(f"/datasets/{dataset_id}"))
        if body["status"]["state"] in ("done", "failed", "cancelled"):
            return body
        time.sleep(0.3)
    raise SeedError(f"the build of {dataset_id} did not finish in {TIMEOUT_S:.0f}s")


def _onboard(
    client: TestClient, client_id: str, tables: dict[str, Path]
) -> tuple[str, dict[str, str], list[str]]:
    """Upload every table, confirm its role, save the suggested mapping, save the recipe."""
    source_ids: dict[str, str] = {}
    for name, path in tables.items():
        created = _ok(
            client.post(
                f"/clients/{client_id}/sources", files={"file": (path.name, path.read_bytes(), "text/csv")}
            ),
            201,
        )
        source_ids[name] = created["source_id"]
        _ok(
            client.patch(
                f"/clients/{client_id}/sources/{created['source_id']}", json={"role": TABLE_ROLES[name]}
            )
        )
    mapping_ids = []
    for name, source_id in source_ids.items():
        suggested = _ok(
            client.post(
                f"/clients/{client_id}/mappings/suggest",
                json={"source_id": source_id, "use_case": CHURN_USE_CASE},
            )
        )
        body = {field: suggested[field] for field in MAPPING_FIELDS}
        fixes = CORRECTIONS.get(name, {})
        if fixes:
            columns = [
                c for c in body["columns"] if c["standard"] not in fixes and c["source"] not in fixes.values()
            ]
            columns += [
                {
                    "source": source,
                    "standard": standard,
                    "transform": None,
                    "confidence": 1.0,
                    "decided_by": "user",
                }
                for standard, source in fixes.items()
            ]
            body["columns"] = columns
            body["unmapped_source"] = [c for c in body["unmapped_source"] if c not in fixes.values()]
        _ok(client.put(f"/clients/{client_id}/mappings/{suggested['mapping_id']}", json=body))
        mapping_ids.append(suggested["mapping_id"])
    schema = _ok(client.get(f"/use-cases/{CHURN_USE_CASE}/standard-schema"))
    roles = set(TABLE_ROLES.values())
    entity = source_ids["customers"]
    spec = _ok(
        client.post(
            f"/clients/{client_id}/onboarding-specs",
            json={
                "use_case": CHURN_USE_CASE,
                "entity_source_id": entity,
                "event_source_ids": sorted(s for s in source_ids.values() if s != entity),
                "mapping_ids": sorted(mapping_ids),
                "feature_spec": {"features": [f for f in schema["suggested_features"] if f["role"] in roles]},
                "label_spec": schema["label"],
                "snapshot_spec": {},
            },
        ),
        201,
    )
    return spec["spec_id"], source_ids, sorted(mapping_ids)


def _build(client: TestClient, client_id: str, spec_id: str, mode: str) -> tuple[str, dict[str, Any]]:
    response = client.post("/datasets", json={"client_id": client_id, "spec_id": spec_id, "mode": mode})
    if response.status_code not in (201, 202):
        raise SeedError(f"POST /datasets -> {response.status_code}: {response.text[:600]}")
    dataset_id: str = response.json()["dataset_id"]
    return dataset_id, _wait_build(client, dataset_id)


def _upload(client: TestClient, frame: pd.DataFrame, *, use_case: str, mode: str, name: str) -> str:
    payload = frame.to_csv(index=False, lineterminator="\n").encode()
    created = _ok(
        client.post(
            "/uploads", files={"file": (name, payload, "text/csv")}, data={"use_case": use_case, "mode": mode}
        ),
        201,
    )
    upload_id: str = created["upload_id"]
    return upload_id


def _scores(client: TestClient, run_id: str) -> pd.DataFrame:
    response = client.get(f"/runs/{run_id}/artefacts/scores.csv")
    if response.status_code != 200:
        raise SeedError(f"scores.csv of {run_id}: {response.status_code}")
    return pd.read_csv(io.BytesIO(response.content), dtype=str)


def _churn_outcomes(scores: pd.DataFrame, score_field: str, target: str, *, seed: int) -> pd.DataFrame:
    """Simulated: a customer leaves with probability = their score; contact removes CONTACT_EFFECT of it."""
    rng = np.random.default_rng(seed)
    leave = scores[score_field].astype(float).clip(0.0, 1.0).to_numpy()
    control = scores["control_group"].str.lower().eq("true").to_numpy()
    suppressed = scores["suppressed_reason"].fillna("").str.len().gt(0).to_numpy()
    contacted = ~control & ~suppressed
    leave = np.where(contacted, leave * (1.0 - CONTACT_EFFECT), leave)
    return pd.DataFrame(
        {"entity_key": scores["entity_key"], target: (rng.random(len(scores)) < leave).astype(int)}
    )


def seed(data_dir: Path | None, *, rng_seed: int, force: bool) -> DemoManifest:
    from tests.fixtures.make_uplift_data import make_uplift_data, make_winback_campaign, outcomes_for
    from tests.fixtures.raw.make_raw import make_duplicate_entities, make_next_month, make_raw

    current = settings()
    storage = build_storage(
        current if data_dir is None else current.model_copy(update={"data_dir": data_dir})
    )
    existing = load_demo(storage)
    if existing is not None and not force:
        logger.info("demo already seeded at %s; --force to seed again", existing.seeded_at.isoformat())
        return existing

    app = create_app(data_dir=data_dir) if data_dir is not None else create_app()
    with tempfile.TemporaryDirectory(prefix="demo-raw-") as scratch, TestClient(app) as client:
        root = Path(scratch)
        raw = make_raw(root / "clean", customers=CUSTOMERS, usage_rows=USAGE_ROWS, seed=rng_seed)
        month = make_next_month(raw, root / "next_month")
        broken = make_duplicate_entities(
            root / "broken", customers=CUSTOMERS, usage_rows=USAGE_ROWS, seed=rng_seed
        )
        for variant, tables in (("clean", raw), ("broken", broken)):
            for path in tables.paths:
                storage.write_bytes(raw_key(variant, path.name), path.read_bytes())
        clean = {name: getattr(raw, name) for name in TABLE_ROLES}

        # --- the churn pilot -------------------------------------------------------------------------
        client_id = _ok(client.post("/clients", json={"name": DEMO_CLIENT_NAME, "industry": "telecom"}), 201)[
            "client_id"
        ]
        spec_id, _, _ = _onboard(client, client_id, clean)
        train_dataset, built = _build(client, client_id, spec_id, "train")
        if built["status"]["state"] != "done":
            raise SeedError(f"the demo's training build ended {built['status']['state']}")
        logger.info("demo: dataset %s built", train_dataset)
        run = _ok(
            client.post(
                "/runs",
                json={
                    "use_case": CHURN_USE_CASE,
                    "mode": "train",
                    "dataset_id": train_dataset,
                    "overrides": FAST_TRAIN,
                },
            ),
            202,
        )
        trained = _wait_run(client, run["run_id"])
        model_id = trained["model_version_id"]
        if not trained.get("champion"):
            _ok(client.post(f"/models/{model_id}/approve", json={"approved_by": "Demo seeder"}))
        logger.info("demo: churn model %s is champion", model_id)

        # the broken extract: one planted problem, for the readiness exercise
        broken_client = _ok(
            client.post(
                "/clients", json={"name": f"{DEMO_CLIENT_NAME} (broken extract)", "industry": "telecom"}
            ),
            201,
        )["client_id"]
        broken_spec, _, _ = _onboard(
            client, broken_client, {name: getattr(broken, name) for name in TABLE_ROLES}
        )
        broken_dataset, _ = _build(client, broken_client, broken_spec, "train")
        broken_report = _ok(client.get(f"/datasets/{broken_dataset}/report"))
        codes = sorted({c["code"] for c in broken_report["checks"] if c["severity"] == "error"})
        if codes != ["ENTITY_DUPLICATE_KEYS"]:
            raise SeedError(f"the broken extract should block on exactly one problem, not {codes}")

        # next month, scored through the same recipe
        month_tables = {name: getattr(month, name) for name in TABLE_ROLES}
        month_sources = []
        for name, path in month_tables.items():
            created = _ok(
                client.post(
                    f"/clients/{client_id}/sources",
                    files={"file": (f"next_{path.name}", path.read_bytes(), "text/csv")},
                ),
                201,
            )
            _ok(
                client.patch(
                    f"/clients/{client_id}/sources/{created['source_id']}", json={"role": TABLE_ROLES[name]}
                )
            )
            month_sources.append(created["source_id"])
        replayed = _ok(
            client.post(
                f"/clients/{client_id}/onboarding-specs/{spec_id}/replay",
                json={"source_ids": month_sources, "mapping_ids": []},
            )
        )
        score_spec = replayed["spec_id"]
        score_dataset, scored_build = _build(client, client_id, score_spec, "score")
        if scored_build["status"]["state"] != "done":
            raise SeedError(f"the demo's scoring build ended {scored_build['status']['state']}")
        score = _ok(
            client.post(
                "/runs", json={"use_case": CHURN_USE_CASE, "mode": "score", "dataset_id": score_dataset}
            ),
            202,
        )
        _wait_run(client, score["run_id"])
        churn_scores = _scores(client, score["run_id"])

        # the churn campaign, measured through outcome ingestion (the dataset's key is composite, which
        # the uplift route refuses): the tables are dated in the past, so the 60-day window has passed
        target = trained["target"]
        outcomes = _churn_outcomes(churn_scores, "churn_prob", target, seed=rng_seed)
        _ok(
            client.post(
                f"/runs/{score['run_id']}/outcomes",
                files={"file": ("churn_outcomes.csv", outcomes.to_csv(index=False).encode(), "text/csv")},
                data={"outcome_column": target},
            ),
            201,
        )

        # --- the win-back uplift campaign ------------------------------------------------------------
        history = make_uplift_data(10_000, seed=rng_seed % 1000 + 7)
        uplift_upload = _upload(
            client, history.frame, use_case=WINBACK_USE_CASE, mode="train", name="winback_history.csv"
        )
        uplift = _ok(
            client.post(
                "/uplift/runs",
                json={
                    "use_case": WINBACK_USE_CASE,
                    "upload_id": uplift_upload,
                    "primary_key": "customer_id",
                    "target": "reactivated_90d",
                    "treatment_column": "treatment",
                    "overrides": FAST_UPLIFT,
                },
            ),
            202,
        )
        uplift_run = _wait_run(client, uplift["run_id"])
        _ok(
            client.post(
                f"/models/{uplift_run['model_version_id']}/promote",
                json={
                    "promoted_by": "Demo seeder",
                    "reason": "demo: the uplift model runs the win-back campaign",
                },
            )
        )
        campaign = make_winback_campaign(
            8_000, seed=rng_seed % 1000 + 11, sent_at=datetime.now(UTC) - timedelta(days=120)
        )
        population = campaign.frame.drop(
            columns=["reactivated_90d", "treatment", "treatment_date"], errors="ignore"
        ).copy()
        population["marketing_opt_in"] = [index % 10 != 3 for index in range(len(population.index))]
        winback_upload = _upload(
            client, population, use_case=WINBACK_USE_CASE, mode="score", name="winback_population.csv"
        )
        winback = _ok(
            client.post(
                "/runs",
                json={
                    "use_case": WINBACK_USE_CASE,
                    "mode": "score",
                    "upload_id": winback_upload,
                    "primary_key": "customer_id",
                },
            ),
            202,
        )
        _wait_run(client, winback["run_id"])
        winback_scores = _scores(client, winback["run_id"])
        treated = set(winback_scores.loc[winback_scores["action"] == "Treat", "customer_id"])
        observed = outcomes_for(campaign, treated_keys=treated, seed=rng_seed % 1000 + 5)
        observed["treatment_date"] = campaign.frame["treatment_date"].to_numpy()
        observed_upload = _upload(
            client, observed, use_case=WINBACK_USE_CASE, mode="score", name="winback_outcomes.csv"
        )
        _ok(
            client.post(
                f"/runs/{winback['run_id']}/campaign-results",
                json={
                    "upload_id": observed_upload,
                    "outcome_column": "reactivated_90d",
                    "treatment_date_column": "treatment_date",
                    "outcome_window_days": 90,
                    "campaign_id": "demo-winback-may",
                },
            )
        )

    for run_id, inputs in (
        (
            score["run_id"],
            RoiInputs(
                value_per_outcome=4_200.0,
                outcome_is_good=False,
                value_basis="Average revenue of a retained customer over the next 12 months (demo assumption)",
                offer_cost=150.0,
                contact_cost=2.0,
                entered_by="Demo seeder",
                note="Demo assumptions, not a client's figures.",
            ),
        ),
        (
            winback["run_id"],
            RoiInputs(
                value_per_outcome=2_600.0,
                value_basis="Average revenue of a won-back customer over the next 12 months (demo assumption)",
                offer_cost=250.0,
                contact_cost=3.0,
                entered_by="Demo seeder",
                note="Demo assumptions, not a client's figures.",
            ),
        ),
    ):
        save_roi_inputs(storage, run_id, inputs)

    manifest = DemoManifest(
        client_id=client_id,
        broken_client_id=broken_client,
        use_case_id=CHURN_USE_CASE,
        train_dataset_id=train_dataset,
        broken_dataset_id=broken_dataset,
        planted_problem="ENTITY_DUPLICATE_KEYS",
        train_run_id=trained["run_id"],
        champion_model_id=model_id,
        score_dataset_id=score_dataset,
        score_run_id=score["run_id"],
        uplift_use_case_id=WINBACK_USE_CASE,
        uplift_run_id=uplift_run["run_id"],
        uplift_score_run_id=winback["run_id"],
        campaigns=(
            DemoCampaign(
                use_case_id=CHURN_USE_CASE,
                title="Retention offers to customers at risk of leaving",
                score_run_id=score["run_id"],
                outcome_column="churn_next_60d",
                simulated_effect=(
                    "Simulated outcomes: a customer leaves with a chance equal to their score, and "
                    f"contact removes {CONTACT_EFFECT:.0%} of their chance of leaving."
                ),
            ),
            DemoCampaign(
                use_case_id=WINBACK_USE_CASE,
                title="Win-back offers to persuadable lapsed customers",
                score_run_id=winback["run_id"],
                outcome_column="reactivated_90d",
                simulated_effect=(
                    "Simulated outcomes from the generator's planted segments: an offer moves persuadable "
                    "customers a lot, sure things and lost causes hardly, and puts some sleeping dogs off."
                ),
            ),
        ),
        data_sources=DATA_SOURCES,
        seeded_at=datetime.now(UTC),
        seed=rng_seed,
    )
    lowered = " ".join(manifest.data_sources).lower()
    if any(excluded in lowered for excluded in EXCLUDED_DATA):
        raise SeedError("the demo may not contain excluded data")
    storage.write_model(DEMO_MANIFEST_KEY, manifest)
    return manifest


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog=COMMAND, description="Seed the Demo Telecom environment.")
    parser.add_argument(
        "--data-dir", type=Path, default=None, help="artefact root (default: MARKETING_AI_DATA_DIR or data/)"
    )
    parser.add_argument(
        "--seed", type=int, default=20260923, help="seed of every generator (default 20260923)"
    )
    parser.add_argument("--force", action="store_true", help="seed again even when a demo exists")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    started = time.monotonic()
    try:
        manifest = seed(args.data_dir, rng_seed=args.seed, force=args.force)
    except SeedError as exc:
        print(f"seed failed: {exc}", file=sys.stderr)
        return 1
    print(
        f"Demo Telecom seeded in {time.monotonic() - started:.0f}s: client {manifest.client_id}, "
        f"champion {manifest.champion_model_id}, campaigns {', '.join(c.score_run_id for c in manifest.campaigns)}."
    )
    print("Start it with: MARKETING_AI_DEMO_MODE=true make run   (then open http://localhost:8000/ui)")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
