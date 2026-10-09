"""The exchange rate an Admin saves, and what runs cost each month (Plan J M108, DEC-1318).

**The exchange rate** is one fact in the platform database's `platform_setting` table, put there by an
Admin (`PUT /cost/fx-rate`, audited) with the source it was read from and the day it was read. It is
the only thing that makes a rupee figure exist: there is no default rate, no built-in table and no
call to a currency service. Delete it and every rupee figure disappears.

**The monthly spend** adds up what the runs of each calendar month cost (a run belongs to the month it was
created in, the one date the scan filters and files by), from the cost figure each
run recorded when it ended (`run_manifest.json`'s `cost_estimate`, AWS's billable seconds at the list
price, and the AI text spend when it was priced). It is a list-price estimate, not a bill, and the
view says how many runs of the month had no figure at all (a run on this machine, or one the price
list could not price): a month where none had one has no amount, not a zero.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING, Final

from pydantic import Field

from engine.aws.run_cost import FxRate, InrAmount, inr_amount
from engine.config import StrictBase
from engine.contracts import RunManifest, RunRecord, RunState
from engine.runs import RUN_FILENAME, RUN_MANIFEST_FILENAME
from engine.storage import Storage, StorageError, run_key
from engine.utils.logging import get_logger, log_failure

if TYPE_CHECKING:
    from sqlalchemy import Engine

__all__ = [
    "FX_SETTING_KEY",
    "SPEND_BASIS",
    "FxStore",
    "SpendMonth",
    "SpendView",
    "monthly_spend",
]

_LOGGER = get_logger(__name__)

FX_SETTING_KEY: Final[str] = "cost_fx_rate"
"""The `platform_setting` key the saved exchange rate lives under."""

SPEND_BASIS: Final[str] = (
    "Estimates at AWS's published list price, in US dollars, from what each finished run recorded. "
    "They are not a bill: they leave out discounts, free allowances and tax, and a run with no recorded "
    "figure is counted but not priced."
)

_FINISHED: Final[frozenset[RunState]] = frozenset({RunState.DONE, RunState.FAILED, RunState.CANCELLED})


class FxStore:
    """The saved exchange rate, in one database's `platform_setting` table."""

    def __init__(self, engine: Engine) -> None:
        from engine.platform_db import PLATFORM_SETTING_TABLE, create_tables

        self._engine = engine
        create_tables(engine, (PLATFORM_SETTING_TABLE,))

    def get(self) -> FxRate | None:
        """The saved rate, or `None` when there is none (or the stored value cannot be read)."""
        from sqlmodel import Session

        from engine.platform_db import PlatformSettingRow

        with Session(self._engine) as session:
            row = session.get(PlatformSettingRow, FX_SETTING_KEY)
            raw = None if row is None else row.value
        if raw is None:
            return None
        try:
            return FxRate.model_validate_json(raw)
        except ValueError as exc:  # an unreadable rate is no rate: rupees disappear rather than guess
            log_failure(_LOGGER, "cost.fx_unreadable", exc)
            return None

    def put(self, rate: FxRate, *, at: datetime) -> None:
        from sqlalchemy.exc import IntegrityError
        from sqlmodel import Session

        from engine.platform_db import PlatformSettingRow

        value = rate.model_dump_json()
        with Session(self._engine) as session:
            row = session.get(PlatformSettingRow, FX_SETTING_KEY)
            if row is None:
                session.add(PlatformSettingRow(key=FX_SETTING_KEY, value=value, updated_at=at))
            else:
                row.value = value
                row.updated_at = at
                session.add(row)
            try:
                session.commit()
            except IntegrityError:  # another process saved first: ours replaces theirs
                session.rollback()
                existing = session.get(PlatformSettingRow, FX_SETTING_KEY)
                if existing is None:
                    raise
                existing.value = value
                existing.updated_at = at
                session.add(existing)
                session.commit()

    def clear(self) -> bool:
        """Forget the rate; True iff there was one."""
        from sqlmodel import Session

        from engine.platform_db import PlatformSettingRow

        with Session(self._engine) as session:
            row = session.get(PlatformSettingRow, FX_SETTING_KEY)
            if row is None:
                return False
            session.delete(row)
            session.commit()
            return True


class SpendMonth(StrictBase):
    """One calendar month (UTC) of finished runs, filed by the month each was created in."""

    month: str = Field(description="YYYY-MM.")
    runs: int = Field(description="Finished runs that month.")
    priced_runs: int = Field(description="Of those, runs that recorded a cost figure.")
    unpriced_runs: int = Field(description="Of those, runs with none (on this machine, or not priceable).")
    estimated_usd: float | None = Field(
        description="The recorded figures added up; null when no run had one."
    )
    inr: InrAmount | None = Field(description="The same in rupees; null without a saved exchange rate.")


class SpendView(StrictBase):
    """`GET /cost/spend`: the last months of recorded run costs, newest first."""

    months: tuple[SpendMonth, ...]
    basis: str
    currency: str = "USD"


def _month_back(moment: datetime, back: int) -> tuple[int, int]:
    index = moment.year * 12 + (moment.month - 1) - back
    return index // 12, index % 12 + 1


def _run_cost(storage: Storage, run_id: str) -> float | None:
    """What one run recorded it cost, or `None` when it recorded nothing priced."""
    try:
        manifest = storage.read_model(run_key(run_id, RUN_MANIFEST_FILENAME), RunManifest)
    except (StorageError, ValueError):
        return None
    parts = [manifest.cost_estimate.estimated_usd]
    if manifest.llm_usage is not None:
        parts.append(manifest.llm_usage.cost_estimate_usd)
    priced = [part for part in parts if part is not None]
    return sum(priced) if priced else None


def monthly_spend(storage: Storage, *, months: int, fx: FxRate | None, now: datetime) -> SpendView:
    """The last `months` calendar months of finished runs (by creation month) and what they recorded costing."""
    wanted = [_month_back(now.astimezone(UTC), back) for back in range(months)]
    oldest = min(wanted)
    totals: dict[tuple[int, int], list[float | None]] = {key: [] for key in wanted}
    for key in sorted(storage.list_keys("runs/"), reverse=True):
        if not key.endswith(f"/{RUN_FILENAME}"):
            continue
        try:
            record = storage.read_model(key, RunRecord)
        except (StorageError, ValueError):
            continue
        created = record.created_at.astimezone(UTC)
        if (created.year, created.month) < oldest:
            break  # run ids sort by creation: nothing older can fall in the window
        if record.state not in _FINISHED:
            continue
        # Filed by the month the run was created in - the same date the scan above stops on - so a run
        # that started before the window and ended inside it is never half in, half out.
        bucket = totals.get((created.year, created.month))
        if bucket is not None:
            bucket.append(_run_cost(storage, record.run_id))
    rows: list[SpendMonth] = []
    for year, month in wanted:
        costs = totals[(year, month)]
        priced = [cost for cost in costs if cost is not None]
        usd = round(sum(priced), 4) if priced else None
        rows.append(
            SpendMonth(
                month=f"{year:04d}-{month:02d}",
                runs=len(costs),
                priced_runs=len(priced),
                unpriced_runs=len(costs) - len(priced),
                estimated_usd=usd,
                inr=inr_amount(usd, fx),
            )
        )
    return SpendView(months=tuple(rows), basis=SPEND_BASIS)
