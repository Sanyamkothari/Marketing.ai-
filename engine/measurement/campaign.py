"""The campaign record (Plan J M94, DEC-1304 (a), (b), (d)): one object every measurement attaches to.

Before M94 a measured result hung off a scoring run (`runs/<id>/incrementality_report.json`), and a
campaign sent days after the list was made was measured from the run's finish time. A **campaign**
is the thing that actually went out: which customers were treated and held back (its *assignment*),
when it went out (its *treatment start*), and later its outcomes, its registered test plan and its
report. The Proof Pack, revenue outcomes, audits of other tools' campaigns and learning all attach
to it.

**Kinds.** `scored`: built from one of our own finished scoring runs, whose scores say who was
treated, who was held back at random and who was suppressed. `external`: a campaign another tool ran
and the user uploads (M103); declared here so the record never changes shape.

**Where it lives.** The record is a row of the platform database (`campaign`, alembic `0006`), the
same database as the audit trail, through `CampaignStore`; a test or a laptop without one uses
`InMemoryCampaignStore`. Its artefacts live in the artefact store under `campaigns/<id>/`:

==============================  ==========================================================================
`campaign.json`                 a copy of the record, so the privacy jobs (which read the store, never the
                                database) know the campaign's key columns and creation date
`assignment.parquet`            one row per scored customer: the key column(s), `arm` (treated, holdout or
                                suppressed), `intended` (inside the population the campaign is measured
                                on), `band`, `segment` when the run has one, and `explore` /
                                `explore_probability` from the run's `holdout_assignment.parquet` (M92)
                                when it wrote one
`outcomes.parquet`              the key column(s), the outcome column and, when named, the per-row treatment
                                date - copied from the outcomes upload, nothing else
`incrementality_report.json`    the measured report (`engine.measurement.measure.measure_campaign`)
`test_plan.json`                the registered test plan in force; `test_plan_v<n>.json` keeps every version
==============================  ==========================================================================

`assignment.parquet` and `outcomes.parquet` hold one row per customer: they are registered as
row-level artefacts in `configs/privacy.yaml` and `engine.privacy.layout`, so erasure rewrites them on
their key columns and retention deletes them with the campaign (DEC-1304 (j)).

**Like with like (DEC-1304 (b)).** `intended` marks the customers the campaign is measured on, and
both arms are compared inside it: for an uplift run, the rows its policy intended to treat
(`intended_treatment`, DEC-624); for a propensity run, the eligible rows of the campaign's treat
bands, or every eligible row (intent to treat) when it names none. Suppressed rows were never
eligible for either arm and are never intended.

`pandas` is imported inside the function bodies, never at module level, so `import engine` stays fast.
"""

from __future__ import annotations

import io
import secrets
import threading
from collections.abc import Mapping, Sequence
from datetime import datetime
from enum import StrEnum
from typing import TYPE_CHECKING, Any, Final, Literal, Protocol, runtime_checkable

from pydantic import AwareDatetime, Field
from sqlalchemy import Column, DateTime
from sqlalchemy.engine import Engine
from sqlmodel import Field as SQLField
from sqlmodel import Session, SQLModel, col, select

from engine.config import PrimaryKey, StrictBase, key_columns
from engine.contracts import Artefact
from engine.holdout.spec import PERSISTENT_SCOPES, HoldoutLedgerEntry, HoldoutScope, HoldoutSpec
from engine.platform_db import create_tables
from engine.utils.time import utc_now

if TYPE_CHECKING:
    import pandas as pd

    from engine.storage import Storage

__all__ = [
    "ARM_HOLDOUT",
    "ARM_SUPPRESSED",
    "ARM_TREATED",
    "ASSIGNMENT_FILENAME",
    "CAMPAIGNS_PREFIX",
    "CAMPAIGN_EPOCH_MISMATCH",
    "CAMPAIGN_FILENAME",
    "CAMPAIGN_NOT_FOUND",
    "CAMPAIGN_NOT_MATURED",
    "CAMPAIGN_OUTCOMES_MISSING",
    "CAMPAIGN_TABLE",
    "INTENDED_COLUMN",
    "OUTCOMES_FILENAME",
    "REPORT_FILENAME",
    "ROW_LEVEL_CAMPAIGN_FILES",
    "TEST_PLAN_FILENAME",
    "AssignmentCounts",
    "Campaign",
    "CampaignKind",
    "CampaignOutcomes",
    "CampaignRow",
    "CampaignStatus",
    "CampaignStore",
    "InMemoryCampaignStore",
    "SqlCampaignStore",
    "as_scores_frame",
    "assignment_counts",
    "build_assignment",
    "campaign_key",
    "create_arbitrated_campaign",
    "epoch_mismatch",
    "holdout_identity",
    "new_campaign_id",
    "read_frame",
    "save_campaign",
    "test_plan_version_filename",
    "write_frame",
]

# ---------------------------------------------------------------------------
# Codes (DEC-1304). Each is in `engine.decide.codes.PLAN_J_CODES` with a plain-language entry in
# `configs/pilot/help.yaml` (added when M94 was merged; PARALLEL_WORK_PROTOCOL.md §3.2).
# ---------------------------------------------------------------------------
CAMPAIGN_NOT_FOUND: Final[str] = "CAMPAIGN_NOT_FOUND"
CAMPAIGN_NOT_MATURED: Final[str] = "CAMPAIGN_NOT_MATURED"
CAMPAIGN_OUTCOMES_MISSING: Final[str] = "CAMPAIGN_OUTCOMES_MISSING"
CAMPAIGN_EPOCH_MISMATCH: Final[str] = "CAMPAIGN_EPOCH_MISMATCH"
"""The campaign's control group is not one persistent holdout epoch's (DEC-1304 (l), M92's epochs)."""

# ---------------------------------------------------------------------------
# Where the artefacts live
# ---------------------------------------------------------------------------
CAMPAIGNS_PREFIX: Final[str] = "campaigns/"
CAMPAIGN_FILENAME: Final[str] = "campaign.json"
ASSIGNMENT_FILENAME: Final[str] = "assignment.parquet"
OUTCOMES_FILENAME: Final[str] = "outcomes.parquet"
REPORT_FILENAME: Final[str] = "incrementality_report.json"
"""The same name as a run's report (`engine.uplift.contracts.INCREMENTALITY_FILENAME`): one contract."""
TEST_PLAN_FILENAME: Final[str] = "test_plan.json"
ROW_LEVEL_CAMPAIGN_FILES: Final[tuple[str, ...]] = (ASSIGNMENT_FILENAME, OUTCOMES_FILENAME)
"""The campaign files holding one row per customer (mirrored by `retention.row_level_campaign_artefacts`)."""

CAMPAIGN_TABLE: Final[str] = "campaign"

ARM_TREATED: Final[str] = "treated"
ARM_HOLDOUT: Final[str] = "holdout"
ARM_SUPPRESSED: Final[str] = "suppressed"
INTENDED_COLUMN: Final[str] = "intended"

_CONTROL_COLUMN: Final[str] = "control_group"
_SUPPRESSED_COLUMN: Final[str] = "suppressed_reason"
_INTENDED_TREATMENT_COLUMN: Final[str] = "intended_treatment"
_BAND_COLUMN: Final[str] = "band"
_SEGMENT_COLUMN: Final[str] = "segment"
_EXPLORE_COLUMN: Final[str] = "explore"
_EXPLORE_PROBABILITY_COLUMN: Final[str] = "explore_probability"


def campaign_key(campaign_id: str, filename: str) -> str:
    """Storage key of a campaign artefact: `campaigns/<id>/<filename>`."""
    return f"{CAMPAIGNS_PREFIX}{campaign_id}/{filename}"


def test_plan_version_filename(version: int) -> str:
    """`test_plan_v<n>.json`: one file per registered version, every one kept."""
    return f"test_plan_v{version}.json"


def new_campaign_id(now: datetime | None = None) -> str:
    """`c_<yyyymmdd>_<8 hex>`, shaped like a run id so a listing of `campaigns/` reads in order."""
    moment = utc_now() if now is None else now
    return f"c_{moment.strftime('%Y%m%d')}_{secrets.token_hex(4)}"


# ---------------------------------------------------------------------------
# The record
# ---------------------------------------------------------------------------
class CampaignKind(StrEnum):
    """Where a campaign's assignment came from."""

    SCORED = "scored"
    """From one of our own finished scoring runs."""
    EXTERNAL = "external"
    """Run by another tool and uploaded for audit (M103)."""


class CampaignStatus(StrEnum):
    LIVE = "live"
    """Created; outcomes not measured yet."""
    MEASURED = "measured"
    """A report has been stored."""


class AssignmentCounts(StrictBase):
    """How many customers are in each arm of the assignment: counts only, never a key."""

    rows: int = Field(description="Customers in the assignment.")
    suppressed: int = Field(description="Suppressed: never eligible for either arm.")
    treated: int = Field(description="Eligible and not held back.")
    holdout: int = Field(description="Held back at random.")
    intended: int = Field(description="Inside the population the campaign is measured on.")
    intended_treated: int = Field(description="Treated customers inside that population.")
    intended_holdout: int = Field(description="Held-back customers inside that population.")
    explore: int = Field(default=0, description="Rows of the explore slice (M92), when the run has one.")


class CampaignOutcomes(StrictBase):
    """The outcomes file a campaign was given, and how to read it."""

    upload_id: str = Field(description="The outcomes upload.")
    file_name: str = Field(description="Its name, as uploaded.")
    outcome_column: str = Field(description="The outcome column.")
    outcome_named: bool = Field(
        default=False,
        description=(
            "True when the person named the column; false when it was found in the file and so is taken "
            "to be the use case's own outcome (which way round it counts, as step 4 reads it)."
        ),
    )
    positive_label: str | None = Field(default=None, description="The value that counts as a conversion.")
    treatment_date_column: str | None = Field(
        default=None,
        description="Per-row treatment date in the file; the campaign's treatment start when null.",
    )
    rows: int = Field(description="Rows copied into `outcomes.parquet`.")
    added_at: AwareDatetime = Field(description="When it was added.")


class Campaign(Artefact):
    """One campaign: what went out, to whom, when, and how it is measured."""

    campaign_id: str = Field(description="`c_<yyyymmdd>_<8 hex>`.")
    kind: CampaignKind = Field(description="`scored` (from our scoring run) or `external` (M103).")
    name: str = Field(description="A short name for the Results list.")
    use_case_id: str | None = Field(default=None, description="The use case whose list it sent.")
    run_ids: tuple[str, ...] = Field(default=(), description="The scoring run(s) the assignment came from.")
    primary_key: PrimaryKey = Field(description="The key column(s) of the assignment and outcomes files.")
    treatment_start: AwareDatetime = Field(description="When the campaign actually went out (UTC).")
    treatment_start_source: Literal["entered", "run_finished"] = Field(
        description="`entered` when the person gave the date; `run_finished` for the scoring run's finish time."
    )
    outcome_window_days: int | None = Field(
        default=None, description="Days after treatment the outcome is counted over; null: every row mature."
    )
    population: Literal["intended", "bands", "eligible"] = Field(
        description=(
            "Who the arms are compared within: an uplift run's intended set, the named treat bands, or "
            "every eligible customer (intent to treat)."
        )
    )
    bands: tuple[str, ...] | None = Field(
        default=None, description="The treat bands, for `population: bands`."
    )
    causal: bool = Field(description="True when the engine drew the holdout at random.")
    causal_basis: Literal["engine_random", "declared_random", "verified_random", "not_random"] = Field(
        description="Why it is (or is not) causal: `engine_random` for a holdout our actions stage drew."
    )
    holdout_scope: HoldoutScope = Field(
        default="run",
        description="The holdout the scoring run drew (M92): `run` (a per-run draw), `use_case` or `universal`.",
    )
    holdout_scope_key: str | None = Field(
        default=None, description="`universal`, or the use-case id under `use_case`; null under `run`."
    )
    holdout_epoch: int | None = Field(
        default=None,
        description="The persistent holdout's epoch the assignment was drawn in (M92); null under `run`.",
    )
    counts: AssignmentCounts = Field(description="Customers in each arm.")
    status: CampaignStatus = Field(description="`live` until a report is stored, then `measured`.")
    outcomes: CampaignOutcomes | None = Field(default=None, description="The outcomes file, once added.")
    test_plan_hash: str | None = Field(default=None, description="`plan_hash` of the test plan in force.")
    test_plan_version: int | None = Field(default=None, description="Its version.")
    created_at: AwareDatetime = Field(description="When the record was created.")
    created_by: str = Field(description="The signed-in user id that created it.")
    measured_at: AwareDatetime | None = Field(
        default=None, description="When the stored report was measured."
    )


# ---------------------------------------------------------------------------
# The assignment
# ---------------------------------------------------------------------------
def build_assignment(
    scores: pd.DataFrame,
    *,
    primary_key: PrimaryKey,
    bands: Sequence[str] | None = None,
    holdout: pd.DataFrame | None = None,
    treated_keys: Sequence[Any] | set[Any] | None = None,
) -> pd.DataFrame:
    """`assignment.parquet` from a scoring run's scores (DEC-1304 (b)); see the module docstring.

    The key column(s) keep their own dtype, so the measurement joins the outcomes exactly as it joins
    them against the scores (`engine.uplift.incrementality._joined_keys`). Raises `ValueError` for a
    missing column, a repeated key, or treat bands on an uplift run (measured within its intended set).

    `holdout` is the run's `holdout_assignment.parquet` (M92), which an engaged run writes beside its
    scores: `scores.*` never carries the explore flag (DEC-1302 (c)), so `explore` and
    `explore_probability` are taken from it, joined on the key. A scored customer the file does not
    list was not explored. Without it, an `explore` column of the scores is kept as it is.

    `treated_keys` (Plan J M101, DEC-1311): When measuring an arbitrated cycle, restricts the treated
    arm to winning customer keys. Customers intended for treatment that lost arbitration are marked
    suppressed so each use case's campaign measures only its winning rows.
    """
    import pandas as pd

    from engine.uplift.incrementality import _flag, _joined_keys, _require_unique, _suppressed

    columns = key_columns(primary_key)
    missing = [name for name in (*columns, _CONTROL_COLUMN) if name not in scores.columns]
    if missing:
        raise ValueError(f"The run's scores have no column {', '.join(repr(name) for name in missing)}.")
    _require_unique(_joined_keys(scores, columns), what="The run's scores")
    uplift_run = _INTENDED_TREATMENT_COLUMN in scores.columns
    if uplift_run and bands is not None:
        raise ValueError(
            "An uplift run is measured within the customers its policy intended to treat; bands do not apply to it."
        )
    if not uplift_run and bands is not None and _BAND_COLUMN not in scores.columns:
        raise ValueError(f"The run's scores have no column {_BAND_COLUMN!r}.")

    frame = scores.reset_index(drop=True)
    suppressed = (
        _suppressed(frame[_SUPPRESSED_COLUMN])
        if _SUPPRESSED_COLUMN in frame.columns
        else pd.Series(False, index=frame.index)
    ).to_numpy(dtype=bool)
    held_out = _flag(frame[_CONTROL_COLUMN]).to_numpy(dtype=bool)
    arm = pd.Series(ARM_TREATED, index=frame.index, dtype="string")
    arm[held_out] = ARM_HOLDOUT
    arm[suppressed] = ARM_SUPPRESSED  # a suppressed row is in neither arm, whatever its control flag
    if treated_keys is not None:
        row_keys = _joined_keys(frame, columns)
        not_winning = (arm == ARM_TREATED) & ~row_keys.isin(set(treated_keys))
        arm[not_winning] = ARM_SUPPRESSED
    if uplift_run:
        intended = _flag(frame[_INTENDED_TREATMENT_COLUMN]).to_numpy(dtype=bool)
    elif bands is not None:
        wanted = {str(band) for band in bands}
        intended = frame[_BAND_COLUMN].astype("string").isin(wanted).fillna(value=False).to_numpy(dtype=bool)
    else:
        intended = pd.Series(True, index=frame.index).to_numpy(dtype=bool)
    out = frame[list(columns)].copy()
    out["arm"] = arm
    out[INTENDED_COLUMN] = pd.Series(intended & (arm != ARM_SUPPRESSED), index=frame.index, dtype=bool)
    out[_BAND_COLUMN] = (
        frame[_BAND_COLUMN].astype("string")
        if _BAND_COLUMN in frame.columns
        else pd.Series(pd.NA, index=frame.index, dtype="string")
    )
    if _SEGMENT_COLUMN in frame.columns:
        out[_SEGMENT_COLUMN] = frame[_SEGMENT_COLUMN].astype("string")
    if holdout is not None:
        explore, probability = _explore_from_holdout(frame, holdout, columns)
        out[_EXPLORE_COLUMN] = explore
        out[_EXPLORE_PROBABILITY_COLUMN] = probability
    elif _EXPLORE_COLUMN in frame.columns:
        out[_EXPLORE_COLUMN] = _flag(frame[_EXPLORE_COLUMN]).astype(bool)
    return out


def _explore_from_holdout(
    frame: pd.DataFrame, holdout: pd.DataFrame, columns: tuple[str, ...]
) -> tuple[pd.Series, pd.Series]:
    """`(explore, explore_probability)` for each row of `frame`, read from a `holdout_assignment` table."""
    import pandas as pd

    from engine.uplift.incrementality import _flag, _joined_keys, _require_unique

    wanted = (*columns, _EXPLORE_COLUMN, _EXPLORE_PROBABILITY_COLUMN)
    missing = [name for name in wanted if name not in holdout.columns]
    if missing:
        raise ValueError(
            f"The run's holdout assignment has no column {', '.join(repr(name) for name in missing)}."
        )
    table = holdout.reset_index(drop=True)
    keys = _joined_keys(table, columns)
    _require_unique(keys, what="The run's holdout assignment")
    explored = pd.Series(_flag(table[_EXPLORE_COLUMN]).to_numpy(dtype=bool), index=keys.to_numpy())
    chance = pd.Series(
        pd.to_numeric(table[_EXPLORE_PROBABILITY_COLUMN], errors="coerce").fillna(0.0).to_numpy(dtype=float),
        index=keys.to_numpy(),
    )
    ours = _joined_keys(frame, columns).to_numpy()
    explore = explored.reindex(ours, fill_value=False).astype(bool).to_numpy()
    probability = chance.reindex(ours, fill_value=0.0).astype("float64").to_numpy()
    return (
        pd.Series(explore, index=frame.index, dtype=bool),
        pd.Series(probability, index=frame.index, dtype="float64"),
    )


def as_scores_frame(frame: pd.DataFrame) -> pd.DataFrame:
    """A frame `measure_incrementality` reads: a run's scores unchanged, or an assignment translated.

    An assignment says `arm`; the measurement reads `control_group` and `suppressed_reason`. The
    translation is exact - holdout rows are the control group, suppressed rows carry a reason - so
    the same customers land in the same arms whichever of the two files is measured.
    """
    import pandas as pd

    if _CONTROL_COLUMN in frame.columns or "arm" not in frame.columns:
        return frame
    arm = frame["arm"].astype("string")
    out = frame.copy()
    out[_CONTROL_COLUMN] = (arm == ARM_HOLDOUT).fillna(value=False).astype(bool)
    reason = pd.Series(pd.NA, index=frame.index, dtype="string")
    reason[(arm == ARM_SUPPRESSED).fillna(value=False).to_numpy(dtype=bool)] = ARM_SUPPRESSED
    out[_SUPPRESSED_COLUMN] = reason
    return out


def assignment_counts(assignment: pd.DataFrame) -> AssignmentCounts:
    """How many customers are in each arm, read off an assignment frame."""
    arm = assignment["arm"].astype("string")
    intended = assignment[INTENDED_COLUMN].astype(bool)
    treated = arm == ARM_TREATED
    holdout = arm == ARM_HOLDOUT
    explore = (
        int(assignment[_EXPLORE_COLUMN].astype(bool).sum()) if _EXPLORE_COLUMN in assignment.columns else 0
    )
    return AssignmentCounts(
        rows=len(assignment.index),
        suppressed=int((arm == ARM_SUPPRESSED).sum()),
        treated=int(treated.sum()),
        holdout=int(holdout.sum()),
        intended=int(intended.sum()),
        intended_treated=int((intended & treated).sum()),
        intended_holdout=int((intended & holdout).sum()),
        explore=explore,
    )


def write_frame(storage: Storage, key: str, frame: pd.DataFrame) -> None:
    """Write a frame as Parquet, without its index."""
    with storage.open_write(key) as sink:
        frame.to_parquet(sink, index=False)


def read_frame(storage: Storage, key: str) -> pd.DataFrame:
    """Read a Parquet artefact back."""
    import pandas as pd

    return pd.read_parquet(io.BytesIO(storage.read_bytes(key)))


# ---------------------------------------------------------------------------
# The store
# ---------------------------------------------------------------------------
class CampaignRow(SQLModel, table=True):
    """One campaign: a few columns to find it by, and the record itself as JSON. Created by `0006`."""

    __tablename__ = CAMPAIGN_TABLE

    campaign_id: str = SQLField(primary_key=True)
    kind: str
    use_case_id: str | None = SQLField(default=None, index=True)
    run_id: str | None = SQLField(default=None, index=True)
    status: str
    created_at: datetime = SQLField(sa_column=Column("created_at", DateTime(timezone=True), nullable=False))
    updated_at: datetime = SQLField(sa_column=Column("updated_at", DateTime(timezone=True), nullable=False))
    record_json: str


@runtime_checkable
class CampaignStore(Protocol):
    """Where campaign records live. `create` refuses an id that exists; `save` replaces one that does."""

    def create(self, campaign: Campaign) -> Campaign: ...

    def save(self, campaign: Campaign) -> Campaign: ...

    def get(self, campaign_id: str) -> Campaign | None: ...

    def list(
        self, *, run_id: str | None = None, use_case_id: str | None = None, limit: int = 100
    ) -> tuple[Campaign, ...]: ...


class SqlCampaignStore:
    """`CampaignStore` on the platform database (`campaign`; Alembic's `0006` owns it on Postgres)."""

    def __init__(self, engine: Engine) -> None:
        self._engine = engine
        create_tables(engine, (CAMPAIGN_TABLE,))

    def create(self, campaign: Campaign) -> Campaign:
        with Session(self._engine) as session:
            if session.get(CampaignRow, campaign.campaign_id) is not None:
                raise ValueError(f"A campaign {campaign.campaign_id!r} already exists.")
            session.add(_row(campaign))
            session.commit()
        return campaign

    def save(self, campaign: Campaign) -> Campaign:
        with Session(self._engine) as session:
            row = session.get(CampaignRow, campaign.campaign_id)
            if row is None:
                raise KeyError(campaign.campaign_id)
            fresh = _row(campaign)
            row.status = fresh.status
            row.updated_at = fresh.updated_at
            row.record_json = fresh.record_json
            session.add(row)
            session.commit()
        return campaign

    def get(self, campaign_id: str) -> Campaign | None:
        with Session(self._engine) as session:
            row = session.get(CampaignRow, campaign_id)
            return None if row is None else Campaign.model_validate_json(row.record_json)

    def list(
        self, *, run_id: str | None = None, use_case_id: str | None = None, limit: int = 100
    ) -> tuple[Campaign, ...]:
        statement = select(CampaignRow)
        if run_id is not None:
            statement = statement.where(col(CampaignRow.run_id) == run_id)
        if use_case_id is not None:
            statement = statement.where(col(CampaignRow.use_case_id) == use_case_id)
        statement = statement.order_by(
            col(CampaignRow.created_at).desc(), col(CampaignRow.campaign_id).desc()
        )
        with Session(self._engine) as session:
            rows = session.exec(statement.limit(limit)).all()
            return tuple(Campaign.model_validate_json(row.record_json) for row in rows)


def _row(campaign: Campaign) -> CampaignRow:
    return CampaignRow(
        campaign_id=campaign.campaign_id,
        kind=campaign.kind.value,
        use_case_id=campaign.use_case_id,
        run_id=campaign.run_ids[0] if campaign.run_ids else None,
        status=campaign.status.value,
        created_at=campaign.created_at,
        updated_at=utc_now(),
        record_json=campaign.model_dump_json(),
    )


class InMemoryCampaignStore:
    """`CampaignStore` in a dictionary: for tests, and for code that runs without a platform database."""

    def __init__(self) -> None:
        self._campaigns: dict[str, Campaign] = {}
        self._lock = threading.Lock()

    def create(self, campaign: Campaign) -> Campaign:
        with self._lock:
            if campaign.campaign_id in self._campaigns:
                raise ValueError(f"A campaign {campaign.campaign_id!r} already exists.")
            self._campaigns[campaign.campaign_id] = campaign
        return campaign

    def save(self, campaign: Campaign) -> Campaign:
        with self._lock:
            if campaign.campaign_id not in self._campaigns:
                raise KeyError(campaign.campaign_id)
            self._campaigns[campaign.campaign_id] = campaign
        return campaign

    def get(self, campaign_id: str) -> Campaign | None:
        with self._lock:
            return self._campaigns.get(campaign_id)

    def list(
        self, *, run_id: str | None = None, use_case_id: str | None = None, limit: int = 100
    ) -> tuple[Campaign, ...]:
        with self._lock:
            found = [
                campaign
                for campaign in self._campaigns.values()
                if (run_id is None or (campaign.run_ids[:1] == (run_id,)))
                and (use_case_id is None or campaign.use_case_id == use_case_id)
            ]
        found.sort(key=lambda campaign: (campaign.created_at, campaign.campaign_id), reverse=True)
        return tuple(found[:limit])


def save_campaign(
    store: CampaignStore, storage: Storage, campaign: Campaign, *, create: bool = False
) -> Campaign:
    """Create or replace the record, and mirror it to `campaigns/<id>/campaign.json` for the privacy jobs."""
    saved = store.create(campaign) if create else store.save(campaign)
    storage.write_model(campaign_key(saved.campaign_id, CAMPAIGN_FILENAME), saved)
    return saved


# ---------------------------------------------------------------------------
# Holdout epochs (M92; DEC-1304 (l))
# ---------------------------------------------------------------------------
_RUN_HOLDOUT: Final[tuple[HoldoutScope, str | None, int | None]] = ("run", None, None)


def holdout_identity(spec: HoldoutSpec | None) -> tuple[HoldoutScope, str | None, int | None]:
    """`(scope, scope_key, epoch)` of a run's holdout; a run without `holdout_assignment.json` drew per run."""
    if spec is None or spec.scope == "run":
        return _RUN_HOLDOUT
    return spec.scope, spec.scope_key, spec.epoch


def epoch_mismatch(
    campaign: Campaign,
    run_specs: Mapping[str, HoldoutSpec | None],
    ledger: HoldoutLedgerEntry | None,
    *,
    outcomes_in_by: datetime,
) -> str | None:
    """Why `campaign` cannot be measured against its control group, or None when it can (pure).

    * **Two epochs.** The runs behind the assignment (`run_specs`, each run's `HoldoutSpec` from
      `holdout_assignment.json`, None for a run that drew per run) must share one holdout: one scope,
      one key, one epoch. Customers held back under two epochs were not held back by one rule.
    * **The run's epoch no longer matches.** That holdout must still be the one the campaign recorded
      when it was created (`holdout_scope`, `holdout_scope_key`, `holdout_epoch`).
    * **A new epoch before the outcomes were in.** `ledger` is the persistent holdout's current entry.
      A later epoch (a lowered share or a new salt) releases or reshuffles held-back customers, who may
      then be contacted. If that happened before `outcomes_in_by` (the end of the outcome window) the
      control group is no longer clean. The ledger keeps only the current epoch's start, so when more
      than one epoch has started since, the first may have begun inside the window: refused too.
    """
    seen = {holdout_identity(spec) for spec in run_specs.values()}
    if len(seen) > 1:
        epochs = sorted(f"epoch {epoch}" if epoch is not None else "a per-run draw" for _, _, epoch in seen)
        return (
            "This campaign's customers were held back in more than one way ("
            + ", ".join(epochs)
            + "), so its two groups were not chosen by one rule. Record a campaign for each scoring run instead."
        )
    recorded = (campaign.holdout_scope, campaign.holdout_scope_key, campaign.holdout_epoch)
    if seen and seen != {recorded}:
        ((_, _, now),) = seen
        return (
            f"The scoring run's control group is no longer the one recorded with this campaign (epoch "
            f"{campaign.holdout_epoch if campaign.holdout_epoch is not None else 'none'}, now "
            f"{now if now is not None else 'none'}). Record the campaign again from the run."
        )
    epoch = campaign.holdout_epoch
    if campaign.holdout_scope not in PERSISTENT_SCOPES or epoch is None or ledger is None:
        return None
    if ledger.epoch == epoch:
        return None
    if ledger.epoch < epoch:  # the ledger cannot go back; a restored database is not the holdout drawn
        return (
            f"The control group on record is epoch {ledger.epoch}, older than this campaign's ({epoch}), so "
            "the campaign's control group cannot be confirmed."
        )
    if ledger.epoch == epoch + 1 and ledger.started_at >= outcomes_in_by:
        return None  # the control group was redrawn only after every outcome was in
    if ledger.epoch == epoch + 1:
        return (
            f"The control group was redrawn (epoch {ledger.epoch}) on {ledger.started_at.date().isoformat()}, "
            f"before this campaign's outcomes were all in ({outcomes_in_by.date().isoformat()}). Customers "
            "held back for it may have been contacted since, so the comparison is not clean."
        )
    return (
        f"The control group has been redrawn {ledger.epoch - epoch} times since this campaign (epoch {epoch}, "
        f"now {ledger.epoch}), and when it first changed is not recorded, so it may have changed before the "
        "outcomes were all in. The comparison cannot be shown to be clean."
    )


def create_arbitrated_campaign(
    storage: Storage,
    store: CampaignStore,
    run_id: str,
    winning_keys: Sequence[str] | set[str],
    *,
    name: str | None = None,
    bands: Sequence[str] | None = None,
    treatment_start: datetime | None = None,
    outcome_window_days: int | None = None,
    created_by: str = "system",
) -> Campaign:
    """Create a campaign from a scoring run measuring only its winning rows from arbitration.

    DEC-1311 (Plan J M101): Measuring an arbitrated cycle creates one campaign per use case
    from its winning rows, so each use case's effect stays measurable.
    """
    import io

    import pandas as pd

    from engine.contracts import RunRecord
    from engine.holdout.assign import HOLDOUT_ASSIGNMENT_FILENAME, run_holdout_spec
    from engine.runs import RUN_FILENAME
    from engine.stages import export
    from engine.storage import run_key

    record = storage.read_model(run_key(run_id, RUN_FILENAME), RunRecord)
    scores_key = run_key(run_id, export.SCORES_PARQUET)
    if storage.exists(scores_key):
        scores = pd.read_parquet(io.BytesIO(storage.read_bytes(scores_key)))
    else:
        scores = pd.read_csv(io.BytesIO(storage.read_bytes(run_key(run_id, export.SCORES_CSV))))

    h_key = run_key(run_id, HOLDOUT_ASSIGNMENT_FILENAME)
    holdout_table = pd.read_parquet(io.BytesIO(storage.read_bytes(h_key))) if storage.exists(h_key) else None

    assignment = build_assignment(
        scores,
        primary_key=record.primary_key,
        bands=bands,
        holdout=holdout_table,
        treated_keys=winning_keys,
    )
    counts = assignment_counts(assignment)

    holdout_scope, holdout_key, holdout_epoch = holdout_identity(run_holdout_spec(storage, record.run_id))
    start = treatment_start or record.created_at
    camp_name = (
        name or f"{record.use_case_name or record.use_case_id} (arbitrated), sent {start.day} {start:%b %Y}"
    )
    now = utc_now()
    cid = new_campaign_id(now)

    uplift_run = "intended_treatment" in scores.columns
    campaign = Campaign(
        campaign_id=cid,
        kind=CampaignKind.SCORED,
        name=camp_name,
        use_case_id=record.use_case_id,
        run_ids=(record.run_id,),
        primary_key=record.primary_key,
        treatment_start=start,
        treatment_start_source="entered" if treatment_start is not None else "run_finished",
        outcome_window_days=outcome_window_days,
        population="intended" if uplift_run else ("bands" if bands else "eligible"),
        bands=tuple(bands) if bands else None,
        causal=counts.holdout > 0,
        causal_basis="engine_random" if counts.holdout > 0 else "not_random",
        holdout_scope=holdout_scope,
        holdout_scope_key=holdout_key,
        holdout_epoch=holdout_epoch,
        counts=counts,
        status=CampaignStatus.LIVE,
        created_at=now,
        created_by=created_by,
    )

    write_frame(storage, campaign_key(cid, ASSIGNMENT_FILENAME), assignment)
    return save_campaign(store, storage, campaign, create=True)
