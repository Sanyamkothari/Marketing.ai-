"""The programme readout: everyone not held back, against the universal holdout (Plan J M103, DEC-1313).

A campaign is measured on one list. A **programme** is everything the product (and the team) did to a
customer base over a period, and the universal holdout (`actions.holdout.scope: universal`, M92) is the
group of customers that no use case ever contacts, in any month. Comparing them with everyone else
answers the question a finance lead asks - "what is all of this worth?" - without needing any one
campaign's list:

    programme effect  =  outcome of all customers not in the holdout  -  outcome of the holdout's members

This is **intent to treat**: a customer outside the holdout may never have been contacted (they were
suppressed, or no list chose them), so the difference is the effect of running the programme, diluted
by everyone it did not reach. It is the honest number, and it is never presented as the effect of a
message on the customers who got it.

**Who is a member.** Membership is not stored per customer: it is `engine.holdout.assign.member_flags`, the
salted hash of the customer id that decided it when each list was scored (DEC-1302 (a)), at the
fraction the ledger recorded for the current epoch. So the customers in an uploaded outcomes file are
split exactly as every scoring run split them, whatever they were or were not scored for. It needs the
deployment's holdout salt and a ledger entry for the universal holdout (a scoring run on the universal
scope records one); with either missing, or with a salt that is not the recorded one, nothing is
computed (`PROGRAMME_NO_HOLDOUT`, `HOLDOUT_SALT_CHANGED`).

**What the universal holdout does and does not guarantee.** It keeps its members out of the lists of use cases
configured with `actions.holdout.scope: universal`. A use case on scope `run` or `use_case`, and any campaign
the team sent outside this tool, can still contact them, which narrows the difference. The readout therefore
says "every list scored with it", lists the scoring runs of the period that did not use it, and points to the
contact file as the way to count the held-back customers who were contacted anyway.

**The epoch must have begun before the period.** The split uses the ledger's current epoch (its fraction and
salt). For a period that began before that epoch started, the customers held back at the time cannot be
found, so the readout is refused (`CAMPAIGN_EPOCH_MISMATCH`), and so is one whose epoch was redrawn before
its outcomes were in, as for any campaign.

**One path.** The split is written as an ordinary campaign `assignment.parquet` (kind `programme`,
holdout scope `universal` with the epoch), and the result is `measure_campaign`'s, unchanged. An amount is
read as a plain difference in means: the adjusted estimate (CUPED, M102) needs a plan registered before the
outcomes are read, and a programme can only be read after its period has ended, so a plan or earlier-amount
column sent with the request is refused (`TEST_PLAN_INVALID`).

The share of held-back customers in the file is checked against the rule's fraction; a share far from it
(a sample-ratio mismatch) is noted, as the file may then not be the whole base.

The period is what makes the readout mature: its outcome window is the period's length, counted from
its first day, so the result is final only after the period has ended.
"""

from __future__ import annotations

from datetime import date
from typing import TYPE_CHECKING, Final

from pydantic import AwareDatetime, Field, model_validator

from engine.config import PrimaryKey, StrictBase, key_columns
from engine.contracts import Artefact

if TYPE_CHECKING:
    import pandas as pd

__all__ = [
    "PROGRAMME_CODES",
    "PROGRAMME_FILENAME",
    "PROGRAMME_LABEL",
    "PROGRAMME_NO_HOLDOUT",
    "ProgrammePeriod",
    "ProgrammeReadout",
    "period_window_days",
    "programme_assignment",
]

PROGRAMME_NO_HOLDOUT: Final[str] = "PROGRAMME_NO_HOLDOUT"
"""409 of `POST /campaigns/programme`: no universal holdout has been used, or the deployment has no holdout salt."""

PROGRAMME_CODES: Final[frozenset[str]] = frozenset({PROGRAMME_NO_HOLDOUT})
"""M103's programme code, joined into `engine.decide.codes.PLAN_J_CODES` at integration (one definition)."""

PROGRAMME_FILENAME: Final[str] = "programme.json"
"""`campaigns/<id>/programme.json`: the period, the holdout it used and the counts. Aggregate: no customer id."""

PROGRAMME_LABEL: Final[str] = "Causal, whole programme"


class ProgrammePeriod(StrictBase):
    """The days the programme ran over; the outcome is counted over all of them."""

    start: date = Field(description="First day of the period.")
    end: date = Field(description="Last day of the period, counted in it.")

    @model_validator(mode="after")
    def _in_order(self) -> ProgrammePeriod:
        if self.end < self.start:
            raise ValueError("The period cannot end before it starts.")
        return self


class ProgrammeReadout(Artefact):
    """`campaigns/<id>/programme.json`: how a programme was read, and what the holdout it used was."""

    campaign_id: str = Field(description="The programme campaign.")
    period_start: date = Field(description="First day of the period.")
    period_end: date = Field(description="Last day of the period.")
    holdout_epoch: int = Field(description="The universal holdout's epoch the split was drawn in.")
    holdout_fraction: float = Field(description="The share the ledger recorded for that epoch.")
    salt_id: str = Field(
        description="The short fingerprint of the salt the holdout was drawn with; never the salt."
    )
    customers: int = Field(description="Customers in the outcomes file.")
    holdout_members: int = Field(description="Of them, members of the universal holdout.")
    other_customers: int = Field(description="Of them, everyone else.")
    realised_share: float | None = Field(
        description="holdout_members / customers; null with no customer. It is binomial around the fraction."
    )
    outcome_kind: str = Field(description="`binary` (a rate) or `continuous` (an amount).")
    outcome_is_good: bool = Field(description="False when the programme aims to make the outcome rarer.")
    label: str = Field(description="What the numbers can claim, in a few words.")
    explanation: str = Field(
        description="Why, in plain sentences, including that this is the whole programme."
    )
    synthetic: bool = Field(
        default=False, description="True when the outcomes upload was marked as generated data."
    )
    notes: tuple[str, ...] = Field(default=(), description="Plain sentences worth reading beside the result.")
    computed_at: AwareDatetime = Field(description="When the readout was computed.")


def period_window_days(period: ProgrammePeriod) -> int:
    """Days in the period, both ends counted: the outcome window, so the result is final the day after it ends."""
    return (period.end - period.start).days + 1


def programme_assignment(
    frame: pd.DataFrame, *, primary_key: PrimaryKey, salt: str, fraction: float
) -> pd.DataFrame:
    """The programme's `assignment.parquet`: every customer of `frame`, held back if the universal holdout has them.

    `frame` holds the customer id column(s), already as text and unique (the outcomes file). Membership is
    `member_flags` on the id (the entity column alone under a two-column key), exactly as a scoring run
    decides it. Everyone else is in the contacted group and `intended`: the programme is read as intent
    to treat.
    """
    import pandas as pd

    from engine.holdout.assign import member_flags
    from engine.holdout.spec import UNIVERSAL_SCOPE_KEY
    from engine.keys import entity_column, is_composite, key_text

    columns = key_columns(primary_key)
    entity = entity_column(primary_key) if is_composite(primary_key) else columns[0]
    members = member_flags(
        key_text(frame[entity]).tolist(), salt=salt, scope_key=UNIVERSAL_SCOPE_KEY, fraction=fraction
    )
    out = frame[list(columns)].copy().reset_index(drop=True)
    out["arm"] = pd.Series(
        ["holdout" if member else "treated" for member in members.tolist()], index=out.index, dtype="string"
    )
    out["intended"] = pd.Series(True, index=out.index, dtype=bool)
    out["band"] = pd.Series(pd.NA, index=out.index, dtype="string")
    return out
