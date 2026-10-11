"""The save-desk card: one customer's retention call, read from what the product already stored (Plan K M144).

A call-centre agent who is about to speak to a customer asks for a **card**: why the model scored the customer
as it did, the offer and channel the run recommends, what the offer is worth in rupees, and whether the customer
is likely to stay anyway, so a discount would only cost money. It is a **read-only lookup**: nothing here
computes, trains, scores, writes or sends. Every line is a value a finished scoring run already wrote:

| Line | Read from | Label |
|---|---|---|
| top reasons | `treat_list.parquet` `reason_1..3` (M98's template phrases, `engine.decide.reasons`) | Estimated |
| offer and channel | `treat_list.parquet` `treat`, `offer`, `channel`, `contactable_channels` (M92, M99, M100) | Estimated |
| net value | `treat_list.parquet` `net_value` (M97); the list's own range from `policy_recommendation.json` | Estimated |
| expected gross value | `treat_list.parquet` `expected_gross_value` (M97), never incremental | Estimated |
| likely to stay anyway | `treat_list.parquet` `segment == sure_thing` (the uplift segments, `engine.uplift.contracts.Segment`) | Estimated |
| kept back on purpose, or not to be contacted | `treat_list.parquet` `control_group`, `holdout`, `suppression_reason` | Descriptive |

The labels are `engine.agentic.registry.EvidenceLabel`: **nothing on the card is Causal**. A model's
recommendation has not been shown to work on this customer group; the only proof is a measured campaign, and
that is what the pilot in `docs/AGENTIC.md` is for.

**Rules only (DEC-1411 (a)).** Every sentence is a fixed template of this module or one of M98's phrases. The
optional paraphrase through M139's claims renderer is not built (M139 is a separate branch): the card carries
`text_source: "rules"` and no model is called, so no sentence can hold a number the stored artefacts do not.

**Customer ids.** The customer is named by `customer_key` in the request **body**, never in a URL (DEC-746). The
response does not echo it. A refusal's message does not name it. Nothing here logs a key or a value; the route
records only a salted hash of the key in the audit event (`principal_hash`).

**Nulls with a plain reason.** A line a run did not compute is `null` with a sentence saying why (an uplift
run's "stay anyway" flag on a run that ranked by chance to convert is `null`, not `false`).

**The holdout stays held back (I4).** A customer the run kept back (its own control group, or M92's persistent
holdout) gets **no offer** on the card, whatever segment they are in, and a customer a suppression rule covers
gets "do not contact"; neither arm of the pilot can change that.

The pilot (a registered design, randomised card access across staff) is `PilotDesign` below; the module ends
with the staff randomisation and the design-effect power. Imports of pandas/pyarrow sit inside functions.
"""

from __future__ import annotations

import hashlib
import io
import math
import threading
from collections import deque
from collections.abc import Callable, Sequence
from datetime import datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Annotated, Any, Final, Literal

import yaml
from pydantic import Field, ValidationError, field_validator, model_validator

from engine.agentic.errors import AgenticError
from engine.agentic.registry import EvidenceLabel
from engine.config import StrictBase
from engine.utils.time import utc_now

if TYPE_CHECKING:
    from engine.storage import Storage

__all__ = [
    "CARD_SETTINGS_FILE",
    "PILOT_FILE",
    "CardRateLimiter",
    "CardRequest",
    "PilotDesign",
    "PilotPlanning",
    "PilotPower",
    "SaveDeskCard",
    "SaveDeskSettings",
    "assign_card_access",
    "build_card",
    "card_access_allowed",
    "load_pilot",
    "load_settings",
    "pilot_power",
]

CARD_SETTINGS_FILE: Final[str] = "agentic/save_desk.yaml"
PILOT_FILE: Final[str] = "agentic/save_desk_pilot.yaml"
ID_PATTERN: Final[str] = r"^[A-Za-z0-9][A-Za-z0-9_\-]{0,63}$"
"""A run id: letters, digits, `_` and `-` (the same pattern as `engine.agentic.tools.ID_PATTERN`)."""

MAX_KEY_CHARS: Final[int] = 200
_MAX_TRACKED: Final[int] = 10_000
"""People tracked by the rate limiter before the least recently seen are forgotten; bounds memory."""

# ---------------------------------------------------------------------------
# The request, the card
# ---------------------------------------------------------------------------


class CardRequest(StrictBase):
    """Body of `POST /agentic/save-desk/card`: the run, and the customer's key. Never in a URL (DEC-746)."""

    run_id: str = Field(pattern=ID_PATTERN, description="The finished scoring run whose treat list is read.")
    customer_key: str = Field(
        min_length=1,
        max_length=MAX_KEY_CHARS,
        description=(
            "The customer's key as the run's own key column holds it. A run keyed by several columns "
            "(customer and snapshot date) takes the parts in the run's order, joined by `|`."
        ),
    )

    @field_validator("customer_key")
    @classmethod
    def _not_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("The customer key is empty.")
        return value


class LabelledLine(StrictBase):
    """What every line of the card carries: how far to trust it, and the stored file it was read from."""

    label: EvidenceLabel = Field(
        description="Causal, Estimated, Not proven or Descriptive: never Causal here."
    )
    source: str = Field(description="The stored file and column the line was read from.")


class ReasonLine(LabelledLine):
    text: str = Field(description="One reason in business words (M98's template, or the model's own text).")


class RecommendationLine(LabelledLine):
    make_offer: bool = Field(description="True only for a customer the run chose to contact.")
    offer: str | None = Field(default=None, description="The recommended offer; null when there is none.")
    channel: str | None = Field(default=None, description="The channel to use; null when none is planned.")
    contactable_channels: tuple[str, ...] | None = Field(
        default=None,
        description="Channels the customer may be contacted on; null when the run did not record them.",
    )
    advice: str = Field(description="What to do, in one sentence.")
    why_not: str | None = Field(default=None, description="Why there is no offer, when there is none.")


class MoneyLine(LabelledLine):
    unit: Literal["rupees"] = "rupees"
    customer_net_value: float | None = Field(
        description="What an offer to this customer is expected to add, in rupees (incremental); null with a reason."
    )
    list_low: float | None = Field(
        description="The low end of the whole list's expected net value, in rupees; null without one."
    )
    list_high: float | None = Field(
        description="The high end of the whole list's expected net value, in rupees; null without one."
    )
    list_contacts: int | None = Field(description="Contacts that range is for; null without one.")
    gross_value: float | None = Field(
        description="Expected gross value in rupees: NOT incremental, it counts what the customer would do anyway."
    )
    note: str = Field(description="What the figures are, and why any is missing.")


class StayAnywayLine(LabelledLine):
    flag: bool | None = Field(
        description="True: likely to stay anyway, do not discount. False: not in that group. Null: cannot be said."
    )
    segment: str | None = Field(
        default=None, description="The model's group for this customer, when it has one."
    )
    reason: str = Field(description="Why, in one sentence.")


class KeptBackLine(LabelledLine):
    kind: Literal["control_group", "holdout", "suppressed"] = Field(
        description="Why no offer is to be made: kept back to measure the campaign, or a rule covers them."
    )
    text: str


class SaveDeskCard(StrictBase):
    """The card. Aggregates nothing and echoes no key: one customer's stored values with their labels."""

    run_id: str
    use_case_id: str | None = None
    recommendation: RecommendationLine
    reasons: tuple[ReasonLine, ...] = Field(description="Up to three, strongest first; may be empty.")
    reasons_note: str | None = Field(default=None, description="Why there are none, when there are none.")
    net_value: MoneyLine
    stay_anyway: StayAnywayLine
    kept_back: KeptBackLine | None = Field(
        default=None,
        description="Set when no offer is to be made because the customer is kept back or covered by a rule.",
    )
    text_source: Literal["rules"] = Field(
        default="rules", description="Every sentence is a fixed template; no model wrote any of it."
    )
    caution: str = Field(description="What the card is and is not.")


# ---------------------------------------------------------------------------
# Rules-only wording (every sentence a fixed template; none holds a digit)
# ---------------------------------------------------------------------------
CAUTION: Final[str] = (
    "This card is the model's recommendation, worked out before the call. It has not been shown to work "
    "for this group of customers: only a measured campaign does that."
)
_NO_REASONS: Final[str] = "The run recorded no reasons for this customer."
_NO_SEGMENT: Final[str] = (
    "This run ranks customers by their chance to respond, not by what an offer changes, so it cannot say "
    "who would stay anyway."
)
_STAY_YES: Final[str] = (
    "The model expects this customer to stay without an offer, so a discount would cost money and change "
    "nothing. Do not discount."
)
_STAY_NO: Final[str] = "The model does not place this customer among those who would stay anyway."
_CONTROL: Final[str] = (
    "This customer is kept back on purpose so the campaign can be measured. Make no offer, whatever they say."
)
_HOLDOUT: Final[str] = (
    "This customer is in the group the business keeps back from every offer so results can be measured. "
    "Make no offer."
)
_SUPPRESSED: Final[dict[str, str]] = {
    "consent_false": "has not agreed to be contacted",
    "opted_out": "has opted out",
    "recently_contacted": "was contacted recently",
}
_ACTION_ADVICE: Final[dict[str, str]] = {
    "Don't treat (converts anyway)": "Likely to stay anyway: do not discount.",
    "Don't treat (won't convert)": "An offer is not expected to change the outcome for this customer.",
    "Never treat (contact makes it worse)": "Contact is expected to make this worse: do not offer.",
    "Don't treat (below cost)": "The offer is expected to cost more than it returns.",
    "Don't treat (over budget)": "Outside this campaign's offer budget.",
}
_NOT_CONFIGURED: Final[str] = "Net value was not configured for this run, so there is no rupee figure."
_GROSS_NOTE: Final[str] = (
    "Expected gross value is not incremental: it counts what the customer would do without an offer."
)
_NET_NOTE: Final[str] = "Net value is incremental: what an offer is expected to add, after its cost."
_LIST_NOTE: Final[str] = "The range is for the whole list, not for this customer."
_HELD_BACK_VALUE: Final[str] = "No offer is to be made, so there is no figure for this customer."

_RESERVED_COLUMNS: Final[tuple[str, ...]] = (
    "treat",
    "holdout",
    "suppression_reason",
    "offer",
    "channel",
    "contactable_channels",
    "offer_reason",
    "net_value",
    "expected_gross_value",
    "reason_1",
    "reason_2",
    "reason_3",
    "use_case",
)

# ---------------------------------------------------------------------------
# Reading the row
# ---------------------------------------------------------------------------


def _key_columns(storage: Storage, run_id: str) -> tuple[str, ...]:
    from engine.config import RunMode, key_columns
    from engine.contracts import RunRecord
    from engine.runs import RUN_FILENAME
    from engine.storage import StorageError, run_key

    try:
        record = storage.read_model(run_key(run_id, RUN_FILENAME), RunRecord)
    except StorageError as exc:
        raise AgenticError("RUN_NOT_FOUND", f"No run with id {run_id!r}.", status=404) from exc
    if record.mode is not RunMode.SCORE:
        raise AgenticError(
            "RUN_NOT_SCORED",
            "A save-desk card is read from a scoring run; this run trained a model instead.",
            status=409,
        )
    return key_columns(record.primary_key)


def _candidates(part: str) -> list[str]:
    """The texts a stored key part may take: as typed, and as the consent ledger writes it (`00104` is `104`)."""
    from engine.privacy.consent import principal_key

    typed = part.strip()
    found = [typed]
    normal = principal_key(typed)
    if normal != typed:
        found.append(normal)
    return found


def _read_row(storage: Storage, run_id: str, columns: tuple[str, ...], customer_key: str) -> dict[str, Any]:
    """The one treat-list row the key names. Refuses a missing list, an unknown customer, an ambiguous key."""
    import pyarrow.parquet as pq

    from engine.decide.treat_list import TREAT_LIST_PARQUET
    from engine.keys import KEY_SEPARATOR
    from engine.storage import StorageError, run_key

    parts = customer_key.split(KEY_SEPARATOR)
    if len(parts) != len(columns):
        raise AgenticError(
            "SAVE_DESK_KEY_INVALID",
            f"This run identifies a customer by {len(columns)} value(s) joined by {KEY_SEPARATOR!r}; "
            f"the key given has {len(parts)}.",
            status=422,
        )
    key = run_key(run_id, TREAT_LIST_PARQUET)
    if not storage.exists(key):
        raise AgenticError(
            "SAVE_DESK_NO_TREAT_LIST",
            "This run has no treat list yet. Open the run's contact list once (the Results page, or "
            "treat_list.csv) so it is built, then ask again.",
            status=409,
        )
    filters = [(name, "in", _candidates(part)) for name, part in zip(columns, parts, strict=True)]
    try:
        table = pq.read_table(io.BytesIO(storage.read_bytes(key)), filters=filters)  # type: ignore[no-untyped-call]
    except (StorageError, OSError, ValueError) as exc:
        raise AgenticError(
            "TOOL_ARTEFACT_UNREADABLE",
            f"treat_list.parquet for {run_id} is stored but could not be read ({type(exc).__name__}); it was not used. "
            "Build it again from the run.",
            status=500,
            detail={"file": TREAT_LIST_PARQUET, "object_id": run_id},
        ) from exc
    if table.num_rows == 0:
        raise AgenticError(
            "SAVE_DESK_CUSTOMER_NOT_FOUND",
            "No customer with that key is in this run's treat list (it may have been erased, or the key may "
            "be written differently in the run).",
            status=404,
        )
    if table.num_rows > 1:
        raise AgenticError(
            "SAVE_DESK_KEY_AMBIGUOUS",
            "More than one row of this run's treat list has that key, so no single customer can be shown.",
            status=409,
        )
    missing = [name for name in _RESERVED_COLUMNS if name not in table.column_names]
    if missing:
        raise AgenticError(
            "TOOL_ARTEFACT_UNREADABLE",
            f"treat_list.parquet for {run_id} lacks {', '.join(missing)}; it is from an older version. "
            "Build it again from the run.",
            status=500,
            detail={"file": TREAT_LIST_PARQUET, "object_id": run_id},
        )
    row: dict[str, Any] = {name: table[name][0].as_py() for name in table.column_names}
    return row


def _clean(value: Any) -> Any:
    """None for a null of any kind (None, NaN); the value otherwise."""
    if value is None:
        return None
    if isinstance(value, float) and math.isnan(value):
        return None
    return value


def _text(value: Any) -> str | None:
    cleaned = _clean(value)
    if cleaned is None:
        return None
    text = str(cleaned).strip()
    return text or None


def _list_range(storage: Storage, run_id: str) -> tuple[float | None, float | None, int | None]:
    """The whole list's net value range and contacts from `policy_recommendation.json`, else nulls."""
    from engine.agentic.artefacts import read_stored_model
    from engine.storage import run_key
    from engine.uplift.contracts import POLICY_FILENAME, PolicyRecommendation

    policy = read_stored_model(storage, run_key(run_id, POLICY_FILENAME), PolicyRecommendation)
    if policy is None or policy.net_value_low is None or policy.net_value_high is None:
        return None, None, None
    return policy.net_value_low, policy.net_value_high, policy.contacts_recommended


def _recommendation(row: dict[str, Any], kept_back: KeptBackLine | None) -> RecommendationLine:
    offer, channel = _text(row["offer"]), _text(row["channel"])
    contactable = _text(row["contactable_channels"])
    channels = (
        None
        if contactable is None
        else tuple(part.strip() for part in contactable.split(",") if part.strip())
    )
    source = "treat_list.parquet: treat, offer, channel"
    if kept_back is not None:
        return RecommendationLine(
            label="Descriptive",
            source=kept_back.source,
            make_offer=False,
            contactable_channels=channels,
            advice=kept_back.text,
            why_not=kept_back.text,
        )
    if bool(row["treat"]):
        where = f" by {channel}" if channel else ""
        return RecommendationLine(
            label="Estimated",
            source=source,
            make_offer=True,
            offer=offer,
            channel=channel,
            contactable_channels=channels,
            advice=f"The run recommends an offer{where}.",
        )
    reason = _text(row["offer_reason"])
    advice = _ACTION_ADVICE.get(offer or "", reason or "The run did not choose this customer for an offer.")
    return RecommendationLine(
        label="Estimated",
        source=source,
        make_offer=False,
        contactable_channels=channels,
        advice=advice,
        why_not=advice,
    )


def _kept_back(row: dict[str, Any]) -> KeptBackLine | None:
    if bool(_clean(row.get("control_group"))):  # absent from a treat list written before M100 part B
        return KeptBackLine(
            label="Descriptive",
            source="treat_list.parquet: control_group",
            kind="control_group",
            text=_CONTROL,
        )
    if _clean(row["holdout"]) is True:
        return KeptBackLine(
            label="Descriptive", source="treat_list.parquet: holdout", kind="holdout", text=_HOLDOUT
        )
    reason = _text(row["suppression_reason"])
    if reason is not None:
        why = _SUPPRESSED.get(reason, f"is covered by a suppression rule ({reason})")
        return KeptBackLine(
            label="Descriptive",
            source="treat_list.parquet: suppression_reason",
            kind="suppressed",
            text=f"Do not contact: this customer {why}.",
        )
    return None


def _reasons(row: dict[str, Any]) -> tuple[ReasonLine, ...]:
    lines: list[ReasonLine] = []
    for slot in ("reason_1", "reason_2", "reason_3"):
        text = _text(row[slot])
        if text is not None:
            lines.append(ReasonLine(label="Estimated", source=f"treat_list.parquet: {slot}", text=text))
    return tuple(lines)


def _stay_anyway(row: dict[str, Any], segment_column: str | None) -> StayAnywayLine:
    source = "treat_list.parquet: segment"
    if segment_column is None:
        return StayAnywayLine(label="Estimated", source=source, flag=None, reason=_NO_SEGMENT)
    segment = _text(row[segment_column])
    if segment is None:
        return StayAnywayLine(
            label="Estimated",
            source=source,
            flag=None,
            reason="The run did not place this customer in a group.",
        )
    from engine.uplift.contracts import Segment

    sure = segment == Segment.SURE_THING.value
    return StayAnywayLine(
        label="Estimated", source=source, flag=sure, segment=segment, reason=_STAY_YES if sure else _STAY_NO
    )


def _money(row: dict[str, Any], storage: Storage, run_id: str, kept_back: KeptBackLine | None) -> MoneyLine:
    source = "treat_list.parquet: net_value, expected_gross_value; policy_recommendation.json"
    if kept_back is not None:
        return MoneyLine(
            label="Descriptive",
            source=source,
            customer_net_value=None,
            list_low=None,
            list_high=None,
            list_contacts=None,
            gross_value=None,
            note=_HELD_BACK_VALUE,
        )
    net = _clean(row["net_value"])
    gross = _clean(row["expected_gross_value"])
    low, high, contacts = _list_range(storage, run_id)
    notes: list[str] = []
    if net is not None:
        notes.append(_NET_NOTE)
    if gross is not None:
        notes.append(_GROSS_NOTE)
    if net is None and gross is None:
        notes.append(_NOT_CONFIGURED)
    if low is not None:
        notes.append(_LIST_NOTE)
    return MoneyLine(
        label="Estimated",
        source=source,
        customer_net_value=None if net is None else float(net),
        list_low=low,
        list_high=high,
        list_contacts=contacts,
        gross_value=None if gross is None else float(gross),
        note=" ".join(notes),
    )


def build_card(storage: Storage, given: CardRequest) -> SaveDeskCard:
    """The card for one customer of a finished scoring run, read from its stored treat list. Writes nothing."""
    columns = _key_columns(storage, given.run_id)
    row = _read_row(storage, given.run_id, columns, given.customer_key)
    segment_column = "segment" if "segment" in row else None
    kept_back = _kept_back(row)
    reasons = _reasons(row)
    return SaveDeskCard(
        run_id=given.run_id,
        use_case_id=_text(row["use_case"]),
        recommendation=_recommendation(row, kept_back),
        reasons=reasons,
        reasons_note=None if reasons else _NO_REASONS,
        net_value=_money(row, storage, given.run_id, kept_back),
        stay_anyway=_stay_anyway(row, segment_column),
        kept_back=kept_back,
        caution=CAUTION,
    )


# ---------------------------------------------------------------------------
# Settings and the rate limit
# ---------------------------------------------------------------------------


class SaveDeskSettings(StrictBase):
    """`configs/agentic/save_desk.yaml`: how often one person may ask for a card."""

    schema_version: Literal[1] = 1
    rate_limit_per_minute: Annotated[int, Field(ge=1, le=600)] = Field(
        default=30, description="Cards one signed-in person may ask for in a minute."
    )


def _read_yaml(path: Path) -> object:
    try:
        return yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except (OSError, UnicodeDecodeError, yaml.YAMLError) as exc:
        raise AgenticError("CONFIG_INVALID", f"{path.name} could not be read: {exc}.", status=500) from exc


def load_settings(config_root: Path | None = None) -> SaveDeskSettings:
    """`save_desk.yaml` under the root; the defaults when the file is not there."""
    from engine.config import config_root as resolve_root

    path = resolve_root(config_root) / CARD_SETTINGS_FILE
    if not path.is_file():
        return SaveDeskSettings()
    try:
        return SaveDeskSettings.model_validate(_read_yaml(path))
    except ValidationError as exc:
        raise AgenticError(
            "CONFIG_INVALID", f"{CARD_SETTINGS_FILE} is not valid: {exc}.", status=500
        ) from exc


class CardRateLimiter:
    """At most `limit` cards per person in any sliding minute (in memory, per process, like the sign-in throttle).

    Looking customers up one after another is how a copy of the customer list is made; the card is for one
    customer in front of one agent. The clock is injected, so the rule is tested without sleeping. Nothing
    here holds a customer key: the keys of the table are the signed-in person's id.
    """

    def __init__(self, limit: int, *, clock: Callable[[], datetime] = utc_now) -> None:
        if limit < 1:
            raise ValueError("the limit must be at least 1")
        self._limit = limit
        self._clock = clock
        self._seen: dict[str, deque[datetime]] = {}
        self._lock = threading.Lock()

    def allow(self, person: str) -> int | None:
        """None when `person` may ask now (and the ask is counted); else the whole seconds to wait."""
        now = self._clock()
        window = timedelta(minutes=1)
        with self._lock:
            hits = self._seen.get(person)
            if hits is None:
                if len(self._seen) >= _MAX_TRACKED:
                    self._seen.pop(next(iter(self._seen)))
                hits = self._seen[person] = deque()
            while hits and now - hits[0] >= window:
                hits.popleft()
            if len(hits) >= self._limit:
                return max(1, math.ceil((hits[0] + window - now).total_seconds()))
            hits.append(now)
            return None


# ---------------------------------------------------------------------------
# The pilot: a design, not a result
# ---------------------------------------------------------------------------
Arm = Literal["card", "no_card"]


class PilotPlanning(StrictBase):
    """What the pilot is sized with. Every number is an assumption to replace with the client's own."""

    staff_count: Annotated[int, Field(ge=4)] = Field(description="Staff who take save calls in the pilot.")
    calls_per_staff: Annotated[int, Field(ge=1)] = Field(
        description="Save calls each handles in the outcome window (the cluster size)."
    )
    base_retention: Annotated[float, Field(gt=0.0, lt=1.0)] = Field(
        description="Share of customers expected to stay without the card, 0 to 1."
    )
    assumed_icc: Annotated[float, Field(ge=0.0, lt=1.0)] = Field(
        description=(
            "How alike one staff member's customers are, beyond chance (intra-cluster correlation). "
            "An ASSUMPTION: it is not measured here, and the plan must say where the number came from."
        )
    )
    icc_source: Annotated[str, Field(min_length=1, max_length=200)] = Field(
        description="Where that number comes from, in words (for example: the client's last year of calls)."
    )
    alpha: Annotated[float, Field(gt=0.0, lt=1.0)] = 0.05
    power: Annotated[float, Field(gt=0.0, lt=1.0)] = 0.8


class PilotDesign(StrictBase):
    """`configs/agentic/save_desk_pilot.yaml`: the randomised pilot, registered beside its test plan."""

    schema_version: Literal[1] = 1
    status: Literal["example", "active"] = Field(
        description=(
            "`example`: a template, not enforced. `active`: only staff in `card_access` are served a card "
            "(everyone else is refused), so access really is the randomised one."
        )
    )
    name: Annotated[str, Field(min_length=1, max_length=120)]
    unit: Literal["staff_member"] = Field(
        default="staff_member",
        description="What is randomised: the staff member, never the call or the customer.",
    )
    card_share: Annotated[float, Field(gt=0.0, lt=1.0)] = Field(
        default=0.5, description="Share of staff given the card."
    )
    salt: Annotated[str, Field(min_length=8, max_length=120)] = Field(
        description="Fixes the draw: the same staff list and salt give the same arms."
    )
    outcome_window_days: Annotated[int, Field(ge=1, le=365)] = Field(
        description="Days after the call in which a customer counts as retained."
    )
    planning: PilotPlanning
    test_plan: dict[str, Any] = Field(
        description="The body registered at `POST /campaigns/{campaign_id}/plan` (`TestPlanInput`)."
    )
    card_access: tuple[str, ...] = Field(
        default=(), description="Staff user ids given the card, written by the randomisation step."
    )
    no_card: tuple[str, ...] = Field(
        default=(), description="Staff user ids who work as usual, written by the randomisation step."
    )
    measurement_note: str = Field(
        description="What the existing measurement can and cannot claim for a clustered pilot (not claimed)."
    )

    @field_validator("test_plan")
    @classmethod
    def _plan_is_one_the_product_accepts(cls, value: dict[str, Any]) -> dict[str, Any]:
        from engine.measurement.plan import TestPlanInput

        TestPlanInput.model_validate(value)
        return value

    @model_validator(mode="after")
    def _arms_are_disjoint(self) -> PilotDesign:
        both = set(self.card_access) & set(self.no_card)
        if both:
            raise ValueError("A staff member is in both arms.")
        if self.status == "active" and not (self.card_access and self.no_card):
            raise ValueError("An active pilot needs staff in both arms: run the randomisation first.")
        return self


def load_pilot(config_root: Path | None = None) -> PilotDesign | None:
    """The pilot design under the root, or None when there is no file. An invalid file is `CONFIG_INVALID`."""
    from engine.config import config_root as resolve_root

    path = resolve_root(config_root) / PILOT_FILE
    if not path.is_file():
        return None
    try:
        return PilotDesign.model_validate(_read_yaml(path))
    except ValidationError as exc:
        raise AgenticError("CONFIG_INVALID", f"{PILOT_FILE} is not valid: {exc}.", status=500) from exc


def card_access_allowed(design: PilotDesign | None, person: str) -> bool:
    """Whether `person` may be served a card: always without an active pilot; in the card arm with one."""
    if design is None or design.status != "active":
        return True
    return person in design.card_access


def assign_card_access(staff: Sequence[str], *, salt: str, card_share: float = 0.5) -> dict[str, Arm]:
    """Complete randomisation of **staff members** (the clusters) into `card` and `no_card`.

    Each person gets a number from a hash of `salt` and their id, the staff are put in that order and the first
    `round(card_share x n)` (at least one, at most all but one) get the card. So the split is exact (not a coin
    flip per person, which can give 5 against 12 by chance), does not depend on the order the list came in, and is
    reproducible from the salt alone. It is written into the design before the pilot starts and not redrawn.
    """
    if not 0.0 < card_share < 1.0:
        raise ValueError("card_share must be between 0 and 1.")
    names = [person.strip() for person in staff]
    if any(not name for name in names):
        raise ValueError("A staff id is empty.")
    if len(set(names)) != len(names):
        raise ValueError("A staff id is repeated.")
    if len(names) < 2:
        raise ValueError("A pilot needs at least two staff members.")
    ordered = sorted(names, key=lambda name: hashlib.sha256(f"{salt}\x00{name}".encode()).hexdigest())
    n_card = min(max(1, math.floor(card_share * len(names) + 0.5)), len(names) - 1)
    return {name: ("card" if position < n_card else "no_card") for position, name in enumerate(ordered)}


class PilotPower(StrictBase):
    """The pilot's sensitivity with staff as the clusters, beside what ignoring clusters would have said."""

    staff_card: int
    staff_no_card: int
    customers_card: int
    customers_no_card: int
    design_effect: float = Field(
        description="1 + (calls per staff - 1) x ICC: how much clustering inflates the spread."
    )
    effective_customers_card: int
    effective_customers_no_card: int
    smallest_detectable_change_pp: float | None = Field(
        description="The smallest change in retention, in percentage points, the pilot reliably sees; null with a reason."
    )
    ignoring_clusters_pp: float | None = Field(
        description="What the same customers would have seen if they were independent: too optimistic."
    )
    staff_per_arm_for_target: int | None = Field(
        default=None, description="Staff each arm needs to see `target_pp`, when a target was given."
    )
    reason: str | None = Field(default=None, description="Why a figure is null.")
    assumption: str = Field(description="The ICC used and that it is assumed, not measured.")


def pilot_power(
    planning: PilotPlanning, *, card_share: float = 0.5, target_pp: float | None = None
) -> PilotPower:
    """The planner's power for two proportions with the design effect of clustering by staff member.

    Customers in one staff member's calls are not independent (the same person, script and mood), so `n` calls
    carry the information of only `n / DEFF` independent ones, `DEFF = 1 + (m - 1) x ICC` for `m` calls per
    staff member. The sizes handed to `engine.measurement.planner.mde_two_proportions` are those effective
    sizes. This is a **planning** figure from an assumed ICC (Donner and Klar, 2000); it is not a measurement
    and the product's measurement does not yet use clustered standard errors (`docs/AGENTIC.md`).
    """
    from engine.measurement import planner

    n_card = max(1, min(planning.staff_count - 1, math.floor(card_share * planning.staff_count + 0.5)))
    n_plain = planning.staff_count - n_card
    deff = 1.0 + (planning.calls_per_staff - 1) * planning.assumed_icc
    customers = (n_card * planning.calls_per_staff, n_plain * planning.calls_per_staff)
    effective = (max(1, math.floor(customers[0] / deff)), max(1, math.floor(customers[1] / deff)))
    clustered = planner.mde_two_proportions(
        effective[0], effective[1], planning.base_retention, planning.alpha, planning.power
    )
    naive = planner.mde_two_proportions(
        customers[0], customers[1], planning.base_retention, planning.alpha, planning.power
    )
    staff_needed: int | None = None
    reason = clustered.reason
    if target_pp is not None:
        sizes = planner.n_for_mde(
            planning.base_retention,
            target_pp / 100.0,
            planning.alpha,
            planning.power,
            control_ratio=n_plain / n_card,
        )
        if sizes.n_treat is None:
            reason = reason or sizes.reason
        else:
            staff_needed = math.ceil(sizes.n_treat * deff / planning.calls_per_staff)
    return PilotPower(
        staff_card=n_card,
        staff_no_card=n_plain,
        customers_card=customers[0],
        customers_no_card=customers[1],
        design_effect=deff,
        effective_customers_card=effective[0],
        effective_customers_no_card=effective[1],
        smallest_detectable_change_pp=clustered.points,
        ignoring_clusters_pp=naive.points,
        staff_per_arm_for_target=staff_needed,
        reason=reason,
        assumption=(
            f"Assumes an intra-cluster correlation of {planning.assumed_icc} ({planning.icc_source}); "
            "it is not measured."
        ),
    )
