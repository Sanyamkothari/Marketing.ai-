"""Cross-use-case arbitration API routes (Plan J M101, DEC-1311).

Provides:
- POST /decide/arbitrate (Analyst) - arbitrate actions across multiple use cases. The arbitrated list is
  written into each participating run (never to the store root, where retention does not look); one campaign per
  use case that holds customers back and won some, with `POST /campaigns`' terms, once per arbitration.
- GET /decide/arbitrate (Viewer) - view the latest arbitration summary.
- GET /decide/conflicts (Viewer) - view conflict statistics for the Results screen.
- GET /decide/arbitrated-treat-list.csv (Analyst, audited) - download arbitrated CSV (a participating run's copy).
- GET /decide/arbitrated-treat-list.parquet (Analyst, audited) - download arbitrated Parquet.
"""

from __future__ import annotations

import hashlib
import io
import json
from collections.abc import Sequence
from pathlib import Path
from typing import Any, Literal

import pandas as pd
from fastapi import APIRouter, Request, Response
from pydantic import Field

from api.access_policy import RoutePolicy, register
from api.deps import ConfigRootDep, StorageDep
from api.routes.campaigns import default_outcome_window, get_campaign_store
from api.routes.runs import load_run, requested_by
from api.routes.uplift import _finished_scoring_run
from api.routes.uploads import http_error, use_case_config
from api.schemas import ErrorResponse
from engine.access.roles import Role
from engine.config import ConfigError, ResolvedConfig, RunMode, StrictBase, UseCaseConfig, key_columns
from engine.contracts import RunRecord, RunState
from engine.decide.arbitrate import (
    ARBITRATED_TREAT_LIST_CSV,
    ARBITRATED_TREAT_LIST_PARQUET,
    ARBITRATION_SUMMARY_FILENAME,
    ArbitrationConfig,
    ArbitrationError,
    ArbitrationSummary,
    arbitrate_treat_lists,
    arbitrated_table,
    comparable_keys,
    csv_bytes,
    load_arbitration_config,
    parquet_bytes,
)
from engine.decide.treat_list import (
    TREAT_LIST_PARQUET,
    TreatListError,
    build_treat_list,
    ensure_treat_list,
    policy_intended,
)
from engine.measurement.campaign import Campaign, create_arbitrated_campaign
from engine.runs import RUN_CONFIG_FILENAME, RUN_FILENAME
from engine.stages.actions import CONTROL_GROUP_COLUMN
from engine.storage import Storage, StorageError, run_key
from engine.uplift.measure import measure_offered
from engine.utils.logging import get_logger

__all__ = ["router"]

_LOGGER = get_logger(__name__)

router: APIRouter = APIRouter(tags=["decide"])

register(
    {
        ("POST", "/decide/arbitrate"): RoutePolicy(
            role=Role.ANALYST,
            action="decide.arbitrate",
            purpose="arbitrate actions across use cases",
            object_type="decide",
        ),
        ("GET", "/decide/arbitrate"): RoutePolicy(
            role=Role.VIEWER,
            action="decide.arbitrate_read",
            purpose="see arbitration results",
            object_type="decide",
        ),
        ("GET", "/decide/conflicts"): RoutePolicy(
            role=Role.VIEWER,
            action="decide.conflicts_read",
            purpose="see arbitration conflicts",
            object_type="decide",
        ),
        ("GET", "/decide/arbitrated-treat-list.csv"): RoutePolicy(
            role=Role.ANALYST,
            action="decide.arbitrated_treat_list_download",
            purpose="download arbitrated treat list",
            object_type="decide",
            audit_reads=True,
        ),
        ("GET", "/decide/arbitrated-treat-list.parquet"): RoutePolicy(
            role=Role.ANALYST,
            action="decide.arbitrated_treat_list_parquet_download",
            purpose="download arbitrated treat list parquet",
            object_type="decide",
            audit_reads=True,
        ),
    }
)

_ERRORS: dict[int | str, dict[str, object]] = {
    400: {"model": ErrorResponse},
    404: {"model": ErrorResponse},
    409: {"model": ErrorResponse},
    422: {"model": ErrorResponse},
}


class ArbitrateRequest(StrictBase):
    """Body of `POST /decide/arbitrate`."""

    use_cases: list[str] | None = Field(
        default=None, description="The use cases to arbitrate across. If omitted, all available scoring runs."
    )
    use_case_ids: list[str] | None = Field(default=None, description="Alias for use_cases.")
    run_ids: list[str] | None = Field(
        default=None, description="Explicit scoring run IDs to arbitrate across."
    )


class ArbitratedCampaign(StrictBase):
    """What became of one use case's campaign in an arbitrated cycle."""

    use_case_id: str
    run_id: str
    campaign_id: str | None = Field(description="The campaign; null for a use case that has none.")
    outcome: Literal["created", "reused", "skipped"] = Field(
        description="`reused` when the same arbitration of the same runs already made it."
    )
    reason: str | None = Field(default=None, description="Why a use case has no campaign, in plain words.")


class ArbitrateResponse(StrictBase):
    """Response of `POST /decide/arbitrate`."""

    summary: ArbitrationSummary
    campaign_ids: list[str] = Field(
        description="The campaigns of the use cases that have one, created now or made by this arbitration before."
    )
    campaigns: list[ArbitratedCampaign] = Field(
        default_factory=list, description="Every use case: its campaign, or why it has none."
    )


def _find_latest_scoring_runs(
    storage: Storage, use_cases: Sequence[str] | None = None
) -> dict[str, RunRecord]:
    """The latest finished scoring run of each use case: by finish time, then creation time, then run id.

    Run ids end in random characters, so their order says nothing about which run is later.
    """
    latest: dict[str, RunRecord] = {}
    target_ucs = set(use_cases) if use_cases else None
    for key in storage.list_keys("runs/"):
        if not key.endswith(f"/{RUN_FILENAME}"):
            continue
        try:
            record = storage.read_model(key, RunRecord)
        except (StorageError, ValueError):
            continue
        if record.mode is not RunMode.SCORE or record.state is not RunState.DONE:
            continue
        uc = record.use_case_id
        if not uc or (target_ucs is not None and uc not in target_ucs):
            continue
        if uc not in latest or _recency(record) > _recency(latest[uc]):
            latest[uc] = record
    return latest


def _recency(record: RunRecord) -> tuple[Any, Any, str]:
    return (record.finished_at or record.created_at, record.created_at, record.run_id)


def _arbitration_id(run_ids: Sequence[str], config: ArbitrationConfig) -> str:
    """A name for these runs (in this order) arbitrated under these settings: the same inputs, the same id."""
    payload = json.dumps({"runs": list(run_ids), "settings": config.model_dump(mode="json")}, sort_keys=True)
    return "a_" + hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


def _campaign_config(storage: Storage, record: RunRecord, root: Path) -> UseCaseConfig:
    """The use case's config as `POST /campaigns` reads it; the settings the run was scored with when the use case is gone."""
    try:
        return use_case_config(record.use_case_id, root)
    except ConfigError:
        return storage.read_model(run_key(record.run_id, RUN_CONFIG_FILENAME), ResolvedConfig).config


@router.post(
    "/decide/arbitrate",
    response_model=ArbitrateResponse,
    status_code=200,
    responses=_ERRORS,
    summary="Arbitrate treatments across use cases",
)
def arbitrate_treatments(
    body: ArbitrateRequest,
    request: Request,
    root: ConfigRootDep,
    storage: StorageDep,
) -> ArbitrateResponse:
    # 1. Resolve participating runs
    runs: list[RunRecord] = []
    if body.run_ids:
        runs = [load_run(storage, rid) for rid in body.run_ids]
    else:
        uc_list = body.use_cases or body.use_case_ids
        latest_map = _find_latest_scoring_runs(storage, uc_list)
        if uc_list:
            missing = [uc for uc in uc_list if uc not in latest_map]
            if missing:
                raise http_error(
                    404,
                    "RUN_NOT_SCORED",
                    f"No finished scoring run found for use case(s): {', '.join(missing)}",
                )
            runs = [latest_map[uc] for uc in uc_list]
        else:
            # No use case named: every use case with a finished scoring run, in use case id order, so the
            # tie-break (the request's use case order) is the same on every call.
            runs = [latest_map[uc] for uc in sorted(latest_map)]

    if not runs:
        raise http_error(400, "NO_RUNS_TO_ARBITRATE", "No finished scoring runs found to arbitrate across.")

    # 2. Check primary key consistency, and that no use case is named twice (its rows would be counted twice)
    repeated = sorted({r.use_case_id for r in runs if [x.use_case_id for x in runs].count(r.use_case_id) > 1})
    if repeated:
        raise http_error(
            422,
            "ARBITRATION_USE_CASE_REPEATED",
            f"Two of the runs are for the same use case ({', '.join(repeated)}); give one run per use case.",
        )
    primary_keys = {key_columns(r.primary_key) for r in runs}  # a composite key is a list on the record
    if len(primary_keys) > 1:
        raise http_error(
            422,
            "PRIMARY_KEY_MISMATCH",
            f"Cannot arbitrate runs with different customer keys: {primary_keys}",
        )
    key_cols_tuple = key_columns(runs[0].primary_key)

    # 3. Load treat lists for all runs, and whom each use case's policy intended to contact
    treat_lists: list[pd.DataFrame] = []
    intended: list[pd.Series[Any] | None] = []
    for r in runs:
        try:
            ensure_treat_list(storage, r.run_id, config_root=root)
            tl_bytes = storage.read_bytes(run_key(r.run_id, TREAT_LIST_PARQUET))
            frame = pd.read_parquet(io.BytesIO(tl_bytes))
            if CONTROL_GROUP_COLUMN not in frame.columns:
                # Written before the list said who the run kept as its control (DEC-1311 (al)): build it again
                # from the run's own scores, so that customer is not treated by another use case.
                build_treat_list(storage, r.run_id, config_root=root)
                frame = pd.read_parquet(io.BytesIO(storage.read_bytes(run_key(r.run_id, TREAT_LIST_PARQUET))))
            treat_lists.append(frame)
            intended.append(policy_intended(storage, r.run_id))
        except TreatListError as exc:
            raise http_error(404 if exc.code == "RUN_NOT_FOUND" else 409, exc.code, exc.message) from exc
        except StorageError as exc:
            raise http_error(
                409,
                "RUN_NOT_SCORED",
                f"Run {r.run_id} has no treat list to arbitrate: its scores are not there.",
            ) from exc

    # 4. Perform arbitration
    try:
        cfg = load_arbitration_config(root)
    except ArbitrationError as exc:
        raise http_error(422, exc.code, exc.message) from exc
    arb_df, summary = arbitrate_treat_lists(treat_lists, config=cfg, key_cols=key_cols_tuple)
    run_ids = [r.run_id for r in runs]
    summary = summary.model_copy(update={"run_ids": run_ids})

    # 5. Write the arbitrated treat list into each participating run (where retention and erasure find it:
    # it names every customer of every use case) and the aggregate summary at the root.
    table = arbitrated_table(arb_df, key_cols=key_cols_tuple)
    pq_data = parquet_bytes(table)
    csv_data = csv_bytes(table)
    storage.write_model(f"decide/{ARBITRATION_SUMMARY_FILENAME}", summary)
    for r in runs:
        storage.write_bytes(run_key(r.run_id, ARBITRATED_TREAT_LIST_PARQUET), pq_data)
        storage.write_bytes(run_key(r.run_id, ARBITRATED_TREAT_LIST_CSV), csv_data)
        storage.write_model(run_key(r.run_id, ARBITRATION_SUMMARY_FILENAME), summary)

    # 6. One campaign per use case that has winners, comparing like with like (DEC-1311 (d))
    scopes = comparable_keys(treat_lists, intended, cfg, key_cols_tuple)
    arbitration_id = _arbitration_id(run_ids, cfg)
    campaign_store = get_campaign_store(request)
    made: list[ArbitratedCampaign] = []
    for r, scope in zip(runs, scopes, strict=True):
        uc = r.use_case_id
        config = _campaign_config(storage, r, root)
        if not measure_offered(config):
            made.append(
                _skipped(r, f"{config.name} holds nobody back or contacts nobody, so it has no campaign.")
            )
            continue
        if summary.winning_by_use_case.get(uc, 0) == 0:
            made.append(
                _skipped(r, f"{config.name} won no customer in this cycle, so there is nothing to measure.")
            )
            continue
        _finished_scoring_run(r)  # a run that is not a finished scoring run has no list that went out
        earlier = _earlier_campaign(campaign_store, r, arbitration_id)
        if earlier is not None:
            made.append(
                ArbitratedCampaign(
                    use_case_id=uc, run_id=r.run_id, campaign_id=earlier.campaign_id, outcome="reused"
                )
            )
            continue
        campaign = create_arbitrated_campaign(
            storage=storage,
            store=campaign_store,
            run_id=r.run_id,
            scope_keys=scope,
            arbitration_id=arbitration_id,
            outcome_window_days=default_outcome_window(config),
            created_by=requested_by(request) or "local",
        )
        made.append(
            ArbitratedCampaign(
                use_case_id=uc, run_id=r.run_id, campaign_id=campaign.campaign_id, outcome="created"
            )
        )

    return ArbitrateResponse(
        summary=summary,
        campaign_ids=[c.campaign_id for c in made if c.campaign_id is not None],
        campaigns=made,
    )


def _skipped(record: RunRecord, reason: str) -> ArbitratedCampaign:
    return ArbitratedCampaign(
        use_case_id=record.use_case_id,
        run_id=record.run_id,
        campaign_id=None,
        outcome="skipped",
        reason=reason,
    )


def _earlier_campaign(store: Any, record: RunRecord, arbitration_id: str) -> Campaign | None:
    """The campaign this same arbitration already made for the run, if any (posting twice makes one)."""
    for campaign in store.list(run_id=record.run_id, use_case_id=record.use_case_id, limit=200):
        if campaign.arbitration_id == arbitration_id:
            found: Campaign = campaign
            return found
    return None


@router.get(
    "/decide/arbitrate",
    response_model=ArbitrationSummary,
    summary="Get latest arbitration summary",
)
def get_arbitration_summary(storage: StorageDep) -> ArbitrationSummary:
    try:
        return storage.read_model(f"decide/{ARBITRATION_SUMMARY_FILENAME}", ArbitrationSummary)
    except StorageError as exc:
        raise http_error(404, "ARBITRATION_NOT_FOUND", "No arbitration has been performed yet.") from exc


@router.get(
    "/decide/conflicts",
    summary="Get arbitration conflicts summary for Results UI",
)
def get_arbitration_conflicts(storage: StorageDep) -> dict[str, Any]:
    try:
        summary = storage.read_model(f"decide/{ARBITRATION_SUMMARY_FILENAME}", ArbitrationSummary)
        return summary.model_dump()
    except StorageError:
        return {
            "total_customers": 0,
            "customers_with_actions": 0,
            "customers_with_conflicts": 0,
            "treated_customers": 0,
            "dropped_actions_count": 0,
            "channel_capped_count": 0,
            "winning_by_use_case": {},
            "dropped_by_use_case": {},
        }


def _arbitrated_file(storage: Storage, name: str) -> bytes:
    """The arbitrated list as the latest arbitration's first run still keeps it (each run has its own copy).

    No customer-level copy is kept at the store root: the copies sit in the runs, where retention and
    erasure find them, so a list is gone once every run that holds it has aged out.
    """
    try:
        summary = storage.read_model(f"decide/{ARBITRATION_SUMMARY_FILENAME}", ArbitrationSummary)
    except StorageError as exc:
        raise http_error(404, "ARBITRATION_NOT_FOUND", "No arbitration has been performed yet.") from exc
    for run_id in summary.run_ids:
        try:
            return storage.read_bytes(run_key(run_id, name))
        except StorageError:
            continue
    raise http_error(
        404,
        "ARBITRATION_NOT_FOUND",
        "No arbitrated treat list is kept any more: arbitrate again to make a new one.",
    )


@router.get(
    "/decide/arbitrated-treat-list.csv",
    summary="Download arbitrated treat list CSV",
)
def download_arbitrated_treat_list_csv(storage: StorageDep) -> Response:
    return Response(
        content=_arbitrated_file(storage, ARBITRATED_TREAT_LIST_CSV),
        media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="{ARBITRATED_TREAT_LIST_CSV}"'},
    )


@router.get(
    "/decide/arbitrated-treat-list.parquet",
    summary="Download arbitrated treat list Parquet",
)
def download_arbitrated_treat_list_parquet(storage: StorageDep) -> Response:
    return Response(
        content=_arbitrated_file(storage, ARBITRATED_TREAT_LIST_PARQUET),
        media_type="application/octet-stream",
        headers={"Content-Disposition": f'attachment; filename="{ARBITRATED_TREAT_LIST_PARQUET}"'},
    )
