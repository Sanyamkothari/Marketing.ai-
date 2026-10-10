"""The scenario suite's fixture world, built by the product's own code (Plan K M132, design §5.1).

Nothing here writes a run artefact by hand. An uplift model is trained through `Pipeline.run_train`
(LightGBM, 50 bootstrap resamples, as `tests/integration/decide/test_channel_consent_real_run.py` trains it)
and a campaign file is scored through `Pipeline.run_score` with per-channel consent columns, so the run's
`channel_contactability.json` is the real one: some customers opted out of SMS, everybody may be emailed.
Campaigns are created and a test plan registered through the product's own API (`POST /campaigns`, `POST
/campaigns/{id}/plan`).

The configuration root is a copy of `configs/` with the deployment's choices a scenario needs:

* `agentic/agentic.yaml`: `agentic.enabled: true`;
* `use_cases/win_back_campaign.yaml`: SMS and email channels; an offer budget of ₹5,000
  (`uplift.policy.total_budget`) and a one-cent cost cap per run (`governance.max_run_cost_usd`);
* `use_cases/telco_churn.yaml`: a persistent holdout (`actions.holdout: {scope: use_case, fraction: 0.10}`);
* `decide/arbitration.yaml`: one contact per customer, at most 500 by email and 400 by SMS;
* `decide/catalogue.yaml`: an SMS action, as the M99 test catalogue.

Two deployments answer: `local` (no cloud bill, so no cost estimate) and `sagemaker` (billed at list price
with a two-hour limit, so the one-cent cap is exceeded).
"""

from __future__ import annotations

import shutil
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any, Final

import numpy as np
import pandas as pd
import yaml
from fastapi.testclient import TestClient

from api.main import create_app
from engine.access.roles import Principal, Role
from engine.agentic.ledger import signer_for_data_dir
from engine.agentic.registry import ToolContext
from engine.agentic.service import agentic_context
from engine.config import ResolvedConfig, RunMode, resolve_config
from engine.contracts import RunState
from engine.jobs import CancelToken, NullJobRunner
from engine.measurement.campaign import SqlCampaignStore
from engine.pipeline import Pipeline, StageContext
from engine.platform_db import PLATFORM_DB_FILENAME, sqlite_engine
from engine.registry import LocalModelRegistry
from engine.settings import Settings
from engine.storage import LocalStorage, run_key, upload_key
from tests.fixtures.make_uplift_data import make_uplift_data, make_winback_campaign
from tests.fixtures.settings import sagemaker_settings

USE_CASE: Final[str] = "win-back-campaign"
PERSISTENT_USE_CASE: Final[str] = "telco-churn"
PRIMARY_KEY: Final[str] = "customer_id"
TARGET: Final[str] = "reactivated_90d"
TREATMENT: Final[str] = "treatment"
TRAIN_ROWS: Final[int] = 6_000
SCORE_ROWS: Final[int] = 1_200
TRAIN_RUN: Final[str] = "r_20261010_a1320001"
SCORE_RUN: Final[str] = "r_20261010_a1320002"
NOW: Final[datetime] = datetime(2026, 10, 10, 12, 0, tzinfo=UTC)
ANALYSIS_DATE: Final[date] = date(2030, 1, 1)
OFFER_BUDGET_INR: Final[float] = 5_000.0
COST_CAP_USD: Final[float] = 0.01
EMAIL_CAP: Final[int] = 500
SMS_CAP: Final[int] = 400

UPLIFT: Final[dict[str, Any]] = {
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
CHANNELS: Final[dict[str, Any]] = {
    "sms": {"consent_column": "sms_opt_in"},
    "email": {"consent_column": "email_opt_in", "contactable_column": "email_valid"},
}
CATALOGUE: Final[str] = """\
# A test catalogue: an SMS-only action, nothing else.
actions:
  - action_id: winback_sms
    label: Win-back offer by SMS
    channel: sms
    offer_cost: 20.0
    contact_cost: 0.15
"""
ARBITRATION: Final[str] = f"""\
contact_cap_per_customer: 1
channel_caps:
  email: {EMAIL_CAP}
  sms: {SMS_CAP}
"""

PRINCIPALS: Final[dict[str, Principal]] = {
    "analyst": Principal(
        user_id="u_asha", username="asha", roles=frozenset({Role.ANALYST, Role.APPROVER}), kind="user"
    ),
    "approver": Principal(user_id="u_ravi", username="ravi", roles=frozenset({Role.APPROVER}), kind="user"),
    "viewer": Principal(user_id="u_vera", username="vera", roles=frozenset({Role.VIEWER}), kind="user"),
}


def _edit_yaml(path: Path, change: Callable[[dict[str, Any]], None]) -> None:
    document = yaml.safe_load(path.read_text(encoding="utf-8"))
    change(document)
    path.write_text(yaml.safe_dump(document, sort_keys=False), encoding="utf-8")


def build_config_root(source: Path, target: Path) -> Path:
    """`configs/` with the deployment choices the scenarios need (see the module docstring)."""
    shutil.copytree(source, target)
    (target / "agentic" / "agentic.yaml").write_text("agentic:\n  enabled: true\n", encoding="utf-8")
    (target / "decide" / "catalogue.yaml").write_text(CATALOGUE, encoding="utf-8")
    (target / "decide" / "arbitration.yaml").write_text(ARBITRATION, encoding="utf-8")

    def win_back(document: dict[str, Any]) -> None:
        document.setdefault("actions", {}).setdefault("suppression", {})["channels"] = CHANNELS
        document.setdefault("uplift", {}).setdefault("policy", {})["total_budget"] = OFFER_BUDGET_INR
        document.setdefault("governance", {})["max_run_cost_usd"] = COST_CAP_USD

    def telco(document: dict[str, Any]) -> None:
        document.setdefault("actions", {})["holdout"] = {"scope": "use_case", "fraction": 0.10}

    _edit_yaml(target / "use_cases" / "win_back_campaign.yaml", win_back)
    _edit_yaml(target / "use_cases" / "telco_churn.yaml", telco)
    return target


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


def scoring_frame() -> pd.DataFrame:
    """The campaign file: one customer in four opted out of SMS; everyone may be emailed."""
    campaign = make_winback_campaign(SCORE_ROWS, seed=11).frame
    frame = campaign.drop(columns=[TARGET, TREATMENT, "treatment_date"], errors="ignore").reset_index(
        drop=True
    )
    position = np.arange(len(frame.index))
    frame["sms_opt_in"] = np.where(position % 4 == 1, "false", "true")
    frame["email_opt_in"] = "yes"
    frame["email_valid"] = 1
    return frame


@dataclass(frozen=True)
class World:
    config_root: Path
    data_dir: Path
    run_id: str
    planned_campaign: str
    """A campaign with a frozen test plan (`POST /campaigns/{id}/plan`)."""
    plan_hash: str
    open_campaign: str
    """A campaign with no test plan yet."""

    @property
    def storage(self) -> LocalStorage:
        return LocalStorage(self.data_dir)

    def client(self) -> TestClient:
        return TestClient(create_app(config_root=self.config_root, data_dir=self.data_dir))

    def context(self, *, principal: str = "analyst", deployment: str = "local") -> ToolContext:
        """A checked context (`engine.agentic.service.agentic_context`) as `principal` on `deployment`."""
        settings: Settings = (
            sagemaker_settings(sagemaker_max_runtime_seconds=7_200)
            if deployment == "sagemaker"
            else Settings()
        )
        engine = sqlite_engine(self.data_dir / PLATFORM_DB_FILENAME)
        return agentic_context(
            storage=self.storage,
            principal=PRINCIPALS[principal],
            config_root=self.config_root,
            platform_engine=engine,
            signer=signer_for_data_dir(self.data_dir),
            settings=settings,
            campaigns=SqlCampaignStore(engine),
            actor="agent:eval",
            clock=lambda: NOW,
        )

    def plan_body(self, **extra: Any) -> dict[str, Any]:
        return {
            "metric": "reactivated within 90 days",
            "outcome_column": TARGET,
            "analysis_date": ANALYSIS_DATE.isoformat(),
            "mde_pp": 5.0,
            "base_rate": 0.05,
            **extra,
        }

    def new_campaign(self, client: TestClient) -> str:
        response = client.post("/campaigns", json={"run_id": self.run_id})
        assert response.status_code == 201, response.text
        return str(response.json()["campaign"]["campaign_id"])


def build_world(source_configs: Path, base: Path) -> World:
    """Train, score, create two campaigns and register one plan (see the module docstring)."""
    root = build_config_root(source_configs, base / "configs")
    data_dir = base / "data"
    data_dir.mkdir()
    storage = LocalStorage(data_dir)
    registry = LocalModelRegistry(data_dir / "registry.db")
    with _config_dir(root):
        resolved = resolve_config(USE_CASE, UPLIFT, root=root)
        train_upload = upload_key("u_a13200000001", "source.csv")
        _write_csv(storage, train_upload, make_uplift_data(TRAIN_ROWS, seed=7).frame)
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
        storage.write_model(run_key(SCORE_RUN, "run_config.json"), resolved)  # as `POST /runs` does
        source = upload_key("u_a13200000002", "source.csv")
        _write_csv(storage, source, scoring_frame())
        score = _context(
            storage,
            registry,
            resolved,
            SCORE_RUN,
            mode=RunMode.SCORE,
            upload=source,
            target=None,
            model_version_id=record.model_version_id,
        )
        scored = Pipeline(storage, registry, NullJobRunner()).run_score(score)
        assert scored.state is RunState.DONE, scored.error
    assert storage.exists(run_key(SCORE_RUN, "channel_contactability.json")), "the run recorded no channels"
    world = World(root, data_dir, SCORE_RUN, "", "", "")
    with world.client() as client:
        planned = world.new_campaign(client)
        response = client.post(f"/campaigns/{planned}/plan", json=world.plan_body())
        assert response.status_code == 201, response.text
        plan_hash = str(response.json()["plan_hash"])
        open_campaign = world.new_campaign(client)
    return World(root, data_dir, SCORE_RUN, planned, plan_hash, open_campaign)


class _config_dir:  # noqa: N801 - a context manager used like a function
    """`MARKETING_AI_CONFIG_DIR` pointed at the world's root while the runs execute, as a deployment runs."""

    def __init__(self, root: Path) -> None:
        import pytest

        self._patch = pytest.MonkeyPatch()
        self._root = root

    def __enter__(self) -> None:
        self._patch.setenv("MARKETING_AI_CONFIG_DIR", str(self._root))
        self._patch.delenv("MARKETING_AI_CLIENT_ID", raising=False)

    def __exit__(self, *exc: object) -> None:
        self._patch.undo()
