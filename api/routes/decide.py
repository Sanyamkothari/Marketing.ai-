"""Cross-use-case arbitration API routes (Plan J M101, DEC-1311).

Provides:
- POST /decide/arbitrate (Analyst) - arbitrate actions across multiple use cases.
- GET /decide/arbitrate (Viewer) - view the latest arbitration summary.
- GET /decide/conflicts (Viewer) - view conflict statistics for the Results screen.
- GET /decide/arbitrated-treat-list.csv (Analyst, audited) - download arbitrated CSV.
- GET /decide/arbitrated-treat-list.parquet (Analyst, audited) - download arbitrated Parquet.
"""

from __future__ import annotations

import io
from collections.abc import Sequence
from typing import Any

import pandas as pd
from fastapi import APIRouter, Request, Response
from pydantic import Field

from api.access_policy import RoutePolicy, register
from api.deps import ConfigRootDep, StorageDep
from api.routes.campaigns import get_campaign_store
from api.routes.runs import load_run
from api.routes.uploads import http_error
from api.schemas import ErrorResponse
from engine.access.roles import Role
from engine.config import RunMode, StrictBase
from engine.contracts import RunRecord, RunState
from engine.decide.arbitrate import (
    ARBITRATED_TREAT_LIST_CSV,
    ARBITRATED_TREAT_LIST_PARQUET,
    ARBITRATION_SUMMARY_FILENAME,
    ArbitrationSummary,
    arbitrate_treat_lists,
    arbitrated_table,
    csv_bytes,
    load_arbitration_config,
    parquet_bytes,
)
from engine.decide.treat_list import (
    TREAT_LIST_PARQUET,
    ensure_treat_list,
)
from engine.measurement.campaign import create_arbitrated_campaign
from engine.runs import RUN_FILENAME
from engine.storage import Storage, StorageError, run_key
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


class ArbitrateResponse(StrictBase):
    """Response of `POST /decide/arbitrate`."""

    summary: ArbitrationSummary
    campaign_ids: list[str] = Field(description="Campaigns created per use case for measurement.")


def _find_latest_scoring_runs(
    storage: Storage, use_cases: Sequence[str] | None = None
) -> dict[str, RunRecord]:
    latest: dict[str, RunRecord] = {}
    target_ucs = set(use_cases) if use_cases else None
    for key in sorted(storage.list_keys("runs/"), reverse=True):
        if not key.endswith(f"/{RUN_FILENAME}"):
            continue
        try:
            record = storage.read_model(key, RunRecord)
        except (StorageError, ValueError):
            continue
        if record.mode is not RunMode.SCORE or record.state is not RunState.DONE:
            continue
        uc = record.use_case_id
        if target_ucs is not None and uc not in target_ucs:
            continue
        if uc and uc not in latest:
            latest[uc] = record
            if target_ucs is not None and len(latest) == len(target_ucs):
                break
    return latest


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
            runs = list(latest_map.values())

    if not runs:
        raise http_error(400, "NO_RUNS_TO_ARBITRATE", "No finished scoring runs found to arbitrate across.")

    # 2. Check primary key consistency
    primary_keys = {r.primary_key for r in runs}
    if len(primary_keys) > 1:
        raise http_error(
            422,
            "PRIMARY_KEY_MISMATCH",
            f"Cannot arbitrate runs with different customer keys: {primary_keys}",
        )
    key_cols = runs[0].primary_key
    key_cols_tuple = (key_cols,) if isinstance(key_cols, str) else tuple(key_cols)

    # 3. Load treat lists for all runs
    treat_lists: list[pd.DataFrame] = []
    for r in runs:
        try:
            ensure_treat_list(storage, r.run_id, config_root=root)
            tl_bytes = storage.read_bytes(run_key(r.run_id, TREAT_LIST_PARQUET))
            tl_df = pd.read_parquet(io.BytesIO(tl_bytes))
            treat_lists.append(tl_df)
        except Exception as exc:
            raise http_error(
                409, "RUN_NOT_SCORED", f"Failed loading treat list for run {r.run_id}: {exc}"
            ) from exc

    # 4. Perform arbitration
    cfg = load_arbitration_config(root)
    arb_df, summary = arbitrate_treat_lists(treat_lists, config=cfg, key_cols=key_cols_tuple)

    # 5. Write arbitrated treat list files
    table = arbitrated_table(arb_df, key_cols=key_cols_tuple)
    pq_data = parquet_bytes(table)
    csv_data = csv_bytes(table)

    storage.write_bytes(f"decide/{ARBITRATED_TREAT_LIST_PARQUET}", pq_data)
    storage.write_bytes(f"decide/{ARBITRATED_TREAT_LIST_CSV}", csv_data)
    storage.write_model(f"decide/{ARBITRATION_SUMMARY_FILENAME}", summary)

    # Mirror to participating runs for run-level artefact access
    for r in runs:
        storage.write_bytes(run_key(r.run_id, ARBITRATED_TREAT_LIST_PARQUET), pq_data)
        storage.write_bytes(run_key(r.run_id, ARBITRATED_TREAT_LIST_CSV), csv_data)

    # 6. Create campaigns for each use case from winning rows
    campaign_store = get_campaign_store(request)
    created_campaign_ids: list[str] = []

    for r in runs:
        uc = r.use_case_id
        winning_rows = arb_df[arb_df["treat"] & (arb_df["winning_use_case"] == uc)]
        winning_keys = set(winning_rows[key_cols_tuple[0]].astype(str)) if not winning_rows.empty else set()
        campaign = create_arbitrated_campaign(
            storage=storage,
            store=campaign_store,
            run_id=r.run_id,
            winning_keys=winning_keys,
        )
        created_campaign_ids.append(campaign.campaign_id)

    return ArbitrateResponse(summary=summary, campaign_ids=created_campaign_ids)


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


@router.get(
    "/decide/arbitrated-treat-list.csv",
    summary="Download arbitrated treat list CSV",
)
def download_arbitrated_treat_list_csv(storage: StorageDep) -> Response:
    key = f"decide/{ARBITRATED_TREAT_LIST_CSV}"
    try:
        data = storage.read_bytes(key)
        return Response(
            content=data,
            media_type="text/csv",
            headers={"Content-Disposition": f'attachment; filename="{ARBITRATED_TREAT_LIST_CSV}"'},
        )
    except StorageError as exc:
        raise http_error(404, "ARBITRATION_NOT_FOUND", "No arbitrated treat list found.") from exc


@router.get(
    "/decide/arbitrated-treat-list.parquet",
    summary="Download arbitrated treat list Parquet",
)
def download_arbitrated_treat_list_parquet(storage: StorageDep) -> Response:
    key = f"decide/{ARBITRATED_TREAT_LIST_PARQUET}"
    try:
        data = storage.read_bytes(key)
        return Response(
            content=data,
            media_type="application/octet-stream",
            headers={"Content-Disposition": f'attachment; filename="{ARBITRATED_TREAT_LIST_PARQUET}"'},
        )
    except StorageError as exc:
        raise http_error(404, "ARBITRATION_NOT_FOUND", "No arbitrated treat list found.") from exc
