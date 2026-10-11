"""Message variants as measured arms (Plan K M140, DEC-1407).

`engine.generative.win_back` writes up to four variants (A to D) of a message per band or segment and
channel, and a person approves them; before this module nothing randomised them or measured them. The
published randomised evidence on model-written marketing copy is sobering (about +1% sessions with a
reward model at Nextdoor, -6.5% for unranked output, no measurable effect at Mercari), so a variant is
worth sending only if it is tested. This module makes approved variants **treatment levels** in M100's
sense (DEC-1310): the no-message holdout first, then the control template and every variant, each
measured against the same held-back customers by `measure_campaign`'s several-offer branch, every
message together by M104's `offers_combined`, and reported per arm in the Value Proof Pack, which names
a winner only when the comparison between messages says so (:func:`compare_variant_arms`).

**Opt-in.** A use case declares `generative.variant_arms: true`; :func:`build_variant_arms` refuses
otherwise (`VARIANT_ARMS_NOT_MADE`), and nothing else reads this module's files unless they exist. So a
run that never built variant arms - every run by default - is byte for byte what it was.

**Arms in force.** Every reader (the treat list, a campaign opened from the run, arbitration) uses the arms
only while they are in force (:func:`variant_arms_in_force`): the use case still turns them on, and the
run's `copy_batch.json` is still the batch they were drawn from, with every arm's template still approved and
still saying the words its catalogue registration names. Otherwise the arms are set aside and the run is
read as one that drew none: turning the setting off, regenerating an arm's template or writing the copy
again never hands off a template whose words were not registered. The routes that change the copy also
delete the arm files and rebuild the treat list (:func:`set_aside_variant_arms`).

**The arms.**

* `no_message`: the run's own held-back customers (Phase 1's control group, or M92's persistent holdout),
  read, never redrawn. It is the control level every arm is measured against.
* `control_template`: the message the customer would get without variants (their band's catalogue
  action, the client's own registered template). It is always an arm, so a variant has to beat what is
  sent today, not only silence.
* `variant_a`, `variant_b`, ...: each approved variant letter. A letter is an arm only when it is approved
  for **every** group the copy was written for and on every channel it was written for; otherwise its
  arm would reach only some groups and a comparison pooled over the groups would compare unlike
  customers. At least two variants are needed (:data:`MIN_VARIANTS`): with one, there is nothing to
  choose between the drafts, and the list stays as it was (the M140 acceptance "one variant: unchanged").

**Registered before randomised.** Every variant used as an arm is entered in the action catalogue as an
explicit action whose `template_ref` is the variant's text reference (:func:`variant_template_ref`,
`copy:` and 16 hex digits of a SHA-256 of its channel, subject and text). An SMS variant must also carry
a registered `dlt_template_id` (not empty, not a placeholder in angle brackets); otherwise the build is
refused with `VARIANT_TEMPLATE_UNREGISTERED`, naming the templates and the reference to register them
under. A variant on another channel with no catalogue action is refused with `VARIANT_ACTION_MISSING`.
Every treated customer of a variant arm must be shown the template to send: when the treat list names no
channel for them and the copy was written for several channels (or names a channel the copy was not
written for), the build is refused with `VARIANT_ARMS_NOT_MADE` and nothing is kept.
The reference binds the registration to the exact words, so a later batch whose variant A says
something else is not covered by this batch's registration. The catalogue file itself is the client's
and is never written here; the build records the SHA-256 of the file it checked against, beside the
run's own stamp (M99), so neither can be mistaken for the other.

**Randomised, seeded, key-hashed.** Each customer the copy was written for gets arm number
`(draw(f"{seed}:variant_arm:{key}") * K) >> 64` of the K message arms in order, where `draw` is M92's
own 64-bit SHA-256 draw (`engine.holdout.assign.draw`), `seed` is the run's seed
(`engine.utils.ids.seed_from`) and `key` is the customer's key text. The draw depends on the customer's
own key only: never on the row's position, the file's order or who else is in it; the same run gives the
same arms every time; and every arm is an equal share of every group, so pooling the groups compares
like with like.

**What is written.** `variant_arms.parquet` (row-level: key, `copy_unit`, `variant_arm`; one row per
customer in the groups the copy was written for, held back or not, so the campaign can be measured
within exactly those customers) and `variant_arms.json` (aggregate). The treat list is rebuilt from them
(`engine.decide.treat_list`): it gains `variant_arm` and `variant_template` on a run with variant arms
only. When a campaign is opened from the run, its assignment carries each contacted customer's arm and
is cut to the same groups (`engine.measurement.cycle.open_campaign`), and measuring it writes
`variant_comparison.json` beside the report.

**Measured on a yes/no outcome.** `measure_campaign` compares several arms on a yes/no outcome only, and
without a pre-campaign covariate. A campaign with message arms measured on an amount, or adjusted by a
covariate, is measured as a whole campaign, as it was before the arms, and no comparison is written.

**Checked before they are sent (Plan K M145, DEC-1412).** With the pre-send compliance gate on, the treat list
reads the arms in force once (:func:`variant_rows`) before the gate, which checks each customer of a variant arm on
that variant's own words and registered action (`engine.compliance.variants`); the arm columns are then added from
the treat flags and channels the gate left, so a withheld customer shows no arm. A campaign opened from the run
sets aside, whole, every arm the gate stopped by its words or sent to nobody (:func:`variant_arms_not_sent`): its
customers are put in neither campaign arm, as `scope_keys` does, and `variant_comparison.json` lists it as not sent,
with no difference. Every other arm keeps each drawn customer, sent or withheld for their consent (intention to
treat): the draw is independent of the customer, so the remaining arms and the no-message group stay comparable.

**Not arbitrated.** Arbitration across use cases keeps a fixed set of columns and opens campaigns without
arms, so a run whose arms are in force is refused there (`VARIANT_ARMS_NOT_ARBITRATED`), rather than
silently losing which message each customer gets.

**Not built.** Synthetic personas: the proposal allows them to screen drafts only, later.

`pandas` and `numpy` are imported inside function bodies, so `import engine` stays fast.
"""

from __future__ import annotations

import hashlib
import io
import re
from collections.abc import Mapping, Sequence
from datetime import datetime
from pathlib import Path
from statistics import NormalDist
from typing import TYPE_CHECKING, Any, Final

from pydantic import AwareDatetime, Field

from engine.contracts import Artefact
from engine.uplift.contracts import ConfidenceValue

if TYPE_CHECKING:
    import pandas as pd

    from engine.config import PrimaryKey, UseCaseConfig
    from engine.generative.contracts import CopyBatch, CopyTemplate
    from engine.storage import Storage
    from engine.uplift.contracts import IncrementalityReport

__all__ = [
    "CONTROL_TEMPLATE_LEVEL",
    "COPY_UNIT_COLUMN",
    "MIN_VARIANTS",
    "NO_MESSAGE_LEVEL",
    "VARIANT_ACTION_MISSING",
    "VARIANT_ARMS_FILENAME",
    "VARIANT_ARMS_NOT_ARBITRATED",
    "VARIANT_ARMS_NOT_MADE",
    "VARIANT_ARMS_PARQUET",
    "VARIANT_ARM_CODES",
    "VARIANT_ARM_COLUMN",
    "VARIANT_COMPARISON_FILENAME",
    "VARIANT_REF_PREFIX",
    "VARIANT_TEMPLATE_COLUMN",
    "VARIANT_TEMPLATE_UNREGISTERED",
    "ArmTemplate",
    "VariantArms",
    "VariantArmsError",
    "VariantComparison",
    "VariantNotSent",
    "VariantPair",
    "VariantRows",
    "build_variant_arms",
    "compare_variant_arms",
    "draw_arms",
    "is_variant_template_ref",
    "set_aside_variant_arms",
    "variant_arms_in_force",
    "variant_arms_not_sent",
    "variant_arms_of",
    "variant_campaign_scope",
    "variant_level",
    "variant_measure_arguments",
    "variant_rows",
    "variant_template_ref",
    "variant_treat_list_columns",
]

# ---------------------------------------------------------------------------
# Codes (DEC-1407): joined into Plan K's code list at integration, each with a plain help entry.
# ---------------------------------------------------------------------------
VARIANT_TEMPLATE_UNREGISTERED: Final[str] = "VARIANT_TEMPLATE_UNREGISTERED"
"""409: an SMS variant chosen as an arm has no catalogue action with a registered template id."""
VARIANT_ACTION_MISSING: Final[str] = "VARIANT_ACTION_MISSING"
"""409: a variant chosen as an arm (not SMS) has no catalogue action carrying its text reference."""
VARIANT_ARMS_NOT_MADE: Final[str] = "VARIANT_ARMS_NOT_MADE"
"""409: no variant arms were made (turned off, no copy, fewer than two variants approved everywhere, ...)."""
VARIANT_ARMS_NOT_ARBITRATED: Final[str] = "VARIANT_ARMS_NOT_ARBITRATED"
"""409: a run whose message arms are in force is not arbitrated across use cases (the arms would be lost)."""
VARIANT_ARM_CODES: Final[frozenset[str]] = frozenset(
    {
        VARIANT_TEMPLATE_UNREGISTERED,
        VARIANT_ACTION_MISSING,
        VARIANT_ARMS_NOT_MADE,
        VARIANT_ARMS_NOT_ARBITRATED,
    }
)

# ---------------------------------------------------------------------------
# Names
# ---------------------------------------------------------------------------
VARIANT_ARMS_FILENAME: Final[str] = "variant_arms.json"
"""Run file: the arms, their templates and catalogue actions, and how many customers each got."""
VARIANT_ARMS_PARQUET: Final[str] = "variant_arms.parquet"
"""Run file, row-level: one row per customer of the groups the copy was written for."""
VARIANT_COMPARISON_FILENAME: Final[str] = "variant_comparison.json"
"""Campaign file (`campaigns/<id>/`): the messages compared with each other, counts and intervals only."""

VARIANT_ARM_COLUMN: Final[str] = "variant_arm"
"""The arm level of a customer (null for a held-back customer and outside the groups)."""
VARIANT_TEMPLATE_COLUMN: Final[str] = "variant_template"
"""Treat list: the approved template the customer's arm sends on their channel; empty for the control template."""
COPY_UNIT_COLUMN: Final[str] = "copy_unit"
"""The band or segment the customer's copy was written for."""

NO_MESSAGE_LEVEL: Final[str] = "no_message"
CONTROL_TEMPLATE_LEVEL: Final[str] = "control_template"
VARIANT_LEVEL_PREFIX: Final[str] = "variant_"
VARIANT_REF_PREFIX: Final[str] = "copy:"
"""A catalogue `template_ref` that names a copy variant's exact words (:func:`variant_template_ref`)."""
MIN_VARIANTS: Final[int] = 2
"""Variant arms are made only with at least this many variants approved for every group and channel."""
FAMILY_ALPHA: Final[float] = 0.05
"""The chance, over every pair of messages together, of naming a winner that is not better (Bonferroni)."""

_SMS: Final[str] = "sms"
_REF_DIGITS: Final[int] = 16
_DRAW_LABEL: Final[str] = "variant_arm"
_PLACEHOLDER: Final[re.Pattern[str]] = re.compile(r"\s*<[^>]*>\s*")
_PERSUADABLE: Final[str] = "persuadable"
_UPLIFT_SEGMENT: Final[str] = "uplift_segment"

_METHOD: Final[str] = (
    "Each customer the copy was written for got one message arm, in equal shares, from a SHA-256 draw of the "
    "run's seed and the customer's own key: never from the row's position. Held-back customers get no "
    "message and are the group every arm is measured against."
)
_CONTROL_TEMPLATE_NOTE: Final[str] = (
    "control_template is the message the customer would get without variants: their band's own action and "
    "template, as sent today. Send the template named in variant_template to the customers of each variant arm."
)
_STAMP_NOTE: Final[str] = (
    "The catalogue was changed after the run was scored (to register the variants, for example): the run's "
    "channels and costs are still the ones it was scored with; the variants were checked against the "
    "catalogue as it is now."
)


class VariantArmsError(Exception):
    """No variant arms were made; `code` is one of :data:`VARIANT_ARM_CODES`, `message` says why in plain words."""

    def __init__(self, code: str, message: str, *, status: int = 409) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.status = status


class ArmTemplate(Artefact):
    """One approved template an arm sends: to one group, on one channel."""

    arm: str = Field(description="The arm level, `variant_<letter>`.")
    unit: str = Field(description="The band or segment the template was written for.")
    channel: str = Field(description="The channel it is sent on.")
    template_id: str = Field(description="The template's id in `copy_batch.json`.")
    template_ref: str = Field(
        description="`copy:` and 16 hex digits of its words' SHA-256: what the catalogue names."
    )
    catalogue_action_id: str = Field(description="The catalogue action that registers it.")
    dlt_template_id: str | None = Field(
        default=None,
        description="The registered template id of that action; null when the channel needs none.",
    )


class VariantArms(Artefact):
    """`variant_arms.json`: the message arms of one scoring run's treat list (Plan K M140, DEC-1407)."""

    run_id: str = Field(description="The scoring run.")
    use_case_id: str = Field(description="Its use case.")
    batch_id: str = Field(description="The copy batch the variants come from.")
    batch_created_at: AwareDatetime = Field(
        description="`created_at` of that batch: a batch written again later is another batch."
    )
    treatment_levels: tuple[str, ...] = Field(
        description="The no-message level first, then every message arm: M100's `treatment_levels` shape."
    )
    control_level: str = Field(description="The level every arm is measured against: no message.")
    arms: tuple[str, ...] = Field(
        description="The message arms, in order: the control template, then each variant."
    )
    units: tuple[str, ...] = Field(description="The bands or segments the copy was written for.")
    channels: tuple[str, ...] = Field(description="The channels the copy was written for.")
    templates: tuple[ArmTemplate, ...] = Field(description="Every template a variant arm sends.")
    variants_left_out: dict[str, str] = Field(
        default_factory=dict,
        description="Variant letters with an approved template that are not arms, and why.",
    )
    assigned_rows: int = Field(description="Customers given an arm (the copy's audience).")
    arm_rows: dict[str, int] = Field(description="Customers per arm.")
    held_back_rows: int = Field(description="Held-back customers of the same groups: the no-message arm.")
    seed: int = Field(description="The run's seed the draw used.")
    method: str = Field(description="How the arms were drawn, in plain words.")
    control_template_note: str = Field(description="What the control template is, in plain words.")
    catalogue_sha256: str = Field(description="SHA-256 of the catalogue the variants were checked against.")
    run_catalogue_sha256: str | None = Field(
        default=None,
        description="SHA-256 of the catalogue the run was scored with (`catalogue_stamp.json`), or null.",
    )
    catalogue_note: str | None = Field(
        default=None, description="Says the two differ, when they do.", exclude_if=lambda value: value is None
    )
    created_at: AwareDatetime = Field(description="When the arms were drawn (UTC).")


class VariantPair(Artefact):
    """Two messages compared directly: the first's outcome rate minus the second's."""

    first: str = Field(description="An arm level.")
    second: str = Field(description="Another arm level.")
    difference: ConfidenceValue = Field(
        description="first's rate minus second's, with its interval at the family's confidence (Newcombe)."
    )


class VariantNotSent(Artefact):
    """A message arm the compliance gate stopped (Plan K M145, DEC-1412): set aside, not compared."""

    arm: str = Field(description="The arm level.")
    reason: str = Field(description="Why it is not compared, in plain words.")
    difference: ConfidenceValue | None = Field(
        default=None,
        description="Always null: a message that was not sent as drawn has no measured difference.",
    )


class VariantComparison(Artefact):
    """`campaigns/<id>/variant_comparison.json`: which message did best, named only when the interval says so.

    Every pair of message arms is compared directly (both were randomised among the same customers), with
    the Newcombe interval at the Bonferroni level `1 - family_alpha / family_size` over every pair: then all
    the intervals hold together with at least `1 - family_alpha` confidence, so a message named the winner
    is better than every other with that confidence, whichever message the data made the leader.
    """

    campaign_id: str | None = Field(description="The campaign.")
    report_computed_at: AwareDatetime = Field(
        description="`computed_at` of the report it was read from: a file from an older measurement is not read."
    )
    arms: tuple[str, ...] = Field(description="The message arms compared, in order.")
    family_alpha: float = Field(description="The chance, over every pair together, of a false winner.")
    family_size: int = Field(description="How many pairs are compared: the Bonferroni divisor.")
    family_confidence: float = Field(
        description="1 - family_alpha / family_size: each pair's interval level."
    )
    pairs: tuple[VariantPair, ...] = Field(description="Every pair, the earlier arm first.")
    leader: str = Field(description="The arm with the highest measured rate.")
    winner: str | None = Field(
        default=None, description="The leader, when its interval is above every other arm's; null otherwise."
    )
    closest: str | None = Field(
        default=None, description="The arm closest to the leader (the smallest lower bound)."
    )
    margin: ConfidenceValue | None = Field(
        default=None, description="The leader's rate minus the closest arm's, at the family's confidence."
    )
    summary: str = Field(description="One plain sentence.")
    not_sent: tuple[VariantNotSent, ...] | None = Field(
        default=None,
        description=(
            "Plan K M145: the message arms the compliance gate stopped, set aside from the campaign and from every "
            "pair (`Campaign.variant_arms_set_aside`). Absent when every arm drawn was sent."
        ),
        exclude_if=lambda value: value is None,
    )


# ---------------------------------------------------------------------------
# Small rules
# ---------------------------------------------------------------------------
def variant_level(letter: str) -> str:
    """The arm level of a variant letter, spelled as M100 spells a level (`engine.uplift.config.level_text`)."""
    return f"{VARIANT_LEVEL_PREFIX}{letter.strip().lower()}"


def variant_template_ref(template: CopyTemplate) -> str:
    """`copy:` and 16 hex digits of the SHA-256 of the template's channel, subject and text.

    What a catalogue action's `template_ref` names to register this variant's exact words: another batch's
    variant with the same letter and other words has another reference.
    """
    payload = "\x00".join((template.channel, template.subject or "", template.text)).encode("utf-8")
    return VARIANT_REF_PREFIX + hashlib.sha256(payload).hexdigest()[:_REF_DIGITS]


def is_variant_template_ref(value: str | None) -> bool:
    """True for a catalogue `template_ref` that names a copy variant (:func:`variant_template_ref`)."""
    return isinstance(value, str) and value.startswith(VARIANT_REF_PREFIX)


def _missing(value: str | None) -> bool:
    """Empty, or a placeholder in angle brackets such as `<your DLT template id>` (the catalogue's own rule)."""
    return value is None or not value.strip() or _PLACEHOLDER.fullmatch(value) is not None


def _unit_of(template: CopyTemplate) -> str:
    return template.segment if template.segment is not None else template.band


# ---------------------------------------------------------------------------
# Building the arms
# ---------------------------------------------------------------------------
def build_variant_arms(
    storage: Storage,
    run_id: str,
    *,
    use_case: UseCaseConfig,
    config_root: Path | None = None,
    now: datetime | None = None,
) -> VariantArms:
    """Draw the message arms of a scoring run's copy, write them, and rebuild the run's treat list.

    Raises :class:`VariantArmsError` with `VARIANT_ARMS_NOT_MADE` when the use case does not turn variant
    arms on, the run is not a finished scoring run with copy, it chose among several offers, fewer than
    :data:`MIN_VARIANTS` variants are approved for every group and channel, or the rebuilt treat list cannot
    name the template a variant customer is sent; `VARIANT_TEMPLATE_UNREGISTERED` when an SMS variant has no
    registered template id in the catalogue; `VARIANT_ACTION_MISSING` when another variant has no catalogue
    action. A `TreatListError` from rebuilding the treat list is raised as it is. Whatever the refusal, no
    arm file is kept and the treat list is the one without arms. See the module docstring.
    """
    from engine.config import RunMode
    from engine.contracts import RunRecord, RunState
    from engine.decide.catalogue import (
        CATALOGUE_STAMP_FILENAME,
        CatalogueStamp,
        catalogue_or_none,
        catalogue_sha256,
    )
    from engine.decide.offer_run import OFFER_CHOICE_FILENAME
    from engine.decide.treat_list import build_treat_list
    from engine.generative.contracts import COPY_BATCH_FILENAME, CopyBatch
    from engine.runs import RUN_FILENAME
    from engine.storage import StorageError, run_key
    from engine.utils.ids import seed_from
    from engine.utils.time import utc_now

    if not use_case.generative.variant_arms:
        raise VariantArmsError(
            VARIANT_ARMS_NOT_MADE,
            "This use case does not turn message variants into measured arms (generative.variant_arms).",
        )
    try:
        record = storage.read_model(run_key(run_id, RUN_FILENAME), RunRecord)
    except StorageError as exc:
        raise VariantArmsError(VARIANT_ARMS_NOT_MADE, f"No run with id {run_id!r}.", status=404) from exc
    if record.mode is not RunMode.SCORE or record.state is not RunState.DONE:
        raise VariantArmsError(VARIANT_ARMS_NOT_MADE, "Variant arms are drawn over a finished scoring run.")
    try:
        batch = storage.read_model(run_key(run_id, COPY_BATCH_FILENAME), CopyBatch)
    except StorageError as exc:
        raise VariantArmsError(
            VARIANT_ARMS_NOT_MADE, "This run has no campaign copy yet: generate and approve it first."
        ) from exc
    if storage.exists(run_key(run_id, OFFER_CHOICE_FILENAME)):
        raise VariantArmsError(
            VARIANT_ARMS_NOT_MADE,
            "This run chose among several offers per customer; message variants are tested on a run of one offer.",
        )

    units, channels, arms, left_out = _arm_letters(batch)
    if len(arms) < MIN_VARIANTS:
        reasons = "; ".join(f"{letter}: {why}" for letter, why in sorted(left_out.items()))
        raise VariantArmsError(
            VARIANT_ARMS_NOT_MADE,
            "Variant arms need at least two variants approved for every group and every channel the copy was "
            "written for; with fewer the list stays as it is."
            + (f" Not usable: {reasons}." if reasons else ""),
        )
    templates = _registered(batch, arms, catalogue_or_none(config_root))
    current_sha = catalogue_sha256(config_root)
    assert current_sha is not None  # `_registered` refuses every arm when there is no catalogue

    seed = seed_from(run_id)
    levels = (CONTROL_TEMPLATE_LEVEL, *(variant_level(letter) for letter in arms))
    frame = _assign(storage, record, batch, levels=levels, seed=seed)

    try:
        stamp = storage.read_model(run_key(run_id, CATALOGUE_STAMP_FILENAME), CatalogueStamp)
        run_sha: str | None = stamp.catalogue_sha256
    except StorageError:
        run_sha = None
    arm_values = frame[VARIANT_ARM_COLUMN]
    counts = arm_values.value_counts()
    arms_file = VariantArms(
        run_id=run_id,
        use_case_id=record.use_case_id,
        batch_id=batch.batch_id,
        batch_created_at=batch.created_at,
        treatment_levels=(NO_MESSAGE_LEVEL, *levels),
        control_level=NO_MESSAGE_LEVEL,
        arms=levels,
        units=units,
        channels=channels,
        templates=templates,
        variants_left_out=left_out,
        assigned_rows=int(arm_values.notna().sum()),
        arm_rows={level: int(counts.get(level, 0)) for level in levels},
        held_back_rows=int(arm_values.isna().sum()),
        seed=seed,
        method=_METHOD,
        control_template_note=_CONTROL_TEMPLATE_NOTE,
        catalogue_sha256=current_sha,
        run_catalogue_sha256=run_sha,
        catalogue_note=_STAMP_NOTE if run_sha is not None and run_sha != current_sha else None,
        created_at=now or utc_now(),
    )
    buffer = io.BytesIO()
    frame.to_parquet(buffer, index=False)
    storage.write_bytes(run_key(run_id, VARIANT_ARMS_PARQUET), buffer.getvalue())
    storage.write_model(run_key(run_id, VARIANT_ARMS_FILENAME), arms_file)
    # The hand-off file must say which message each customer gets: rebuilt from the run's own artefacts. On
    # any refusal the arm files go again, so no later rebuild or campaign acts on arms the person was refused.
    try:
        build_treat_list(storage, run_id, config_root=config_root)
        unnamed = _variant_rows_without_template(storage, run_id)
    except Exception:
        set_aside_variant_arms(storage, run_id, config_root=config_root)
        raise
    if unnamed:
        set_aside_variant_arms(storage, run_id, config_root=config_root)
        raise VariantArmsError(
            VARIANT_ARMS_NOT_MADE,
            f"The treat list cannot say which template to send to {unnamed} customers of a variant arm: the "
            "copy was written for "
            + ", ".join(channels)
            + ", and the list names no channel for them that the "
            "copy covers. Write the copy for one channel, or give the run its channels per customer.",
        )
    return arms_file


def _variant_rows_without_template(storage: Storage, run_id: str) -> int:
    """Treated customers of a variant arm (not the control template) whose treat-list row names no template."""
    import pandas as pd

    from engine.decide.treat_list import TREAT_LIST_PARQUET
    from engine.storage import run_key

    table = pd.read_parquet(io.BytesIO(storage.read_bytes(run_key(run_id, TREAT_LIST_PARQUET))))
    if VARIANT_ARM_COLUMN not in table.columns:
        return 0
    arm = table[VARIANT_ARM_COLUMN].astype("string")
    variant = arm.str.startswith(VARIANT_LEVEL_PREFIX).fillna(value=False).to_numpy(dtype=bool)
    return int((variant & table[VARIANT_TEMPLATE_COLUMN].isna().to_numpy(dtype=bool)).sum())


def _arm_letters(
    batch: CopyBatch,
) -> tuple[tuple[str, ...], tuple[str, ...], tuple[str, ...], dict[str, str]]:
    """`(units, channels, arm letters, letters left out with why)` of a batch's approved templates."""
    from engine.generative.contracts import CopyStatus
    from engine.generative.win_back import VARIANT_LETTERS

    units = tuple(dict.fromkeys(_unit_of(template) for template in batch.templates))
    channels = tuple(dict.fromkeys(template.channel for template in batch.templates))
    approved = {
        (_unit_of(template), template.channel, template.variant)
        for template in batch.templates
        if template.status is CopyStatus.APPROVED
    }
    arms: list[str] = []
    left_out: dict[str, str] = {}
    for letter in VARIANT_LETTERS:
        have = [
            (unit, channel) for unit in units for channel in channels if (unit, channel, letter) in approved
        ]
        if not have:
            continue
        if len(have) < len(units) * len(channels):
            missing = [
                f"{unit} on {channel}"
                for unit in units
                for channel in channels
                if (unit, channel) not in have
            ]
            left_out[letter] = "not approved for " + ", ".join(missing)
            continue
        arms.append(letter)
    return units, channels, tuple(arms), left_out


def _registered(batch: CopyBatch, letters: Sequence[str], catalogue: Any) -> tuple[ArmTemplate, ...]:
    """Every arm template with the catalogue action that registers it, or the refusal (see the module docstring)."""
    from engine.generative.contracts import CopyStatus

    by_ref: dict[str, Any] = {}
    if catalogue is not None:
        for action in catalogue.actions:
            if is_variant_template_ref(action.template_ref):
                by_ref.setdefault(action.template_ref, action)
    found: list[ArmTemplate] = []
    unregistered: list[str] = []
    missing: list[str] = []
    for template in batch.templates:
        if template.variant not in letters or template.status is not CopyStatus.APPROVED:
            continue
        ref = variant_template_ref(template)
        action = by_ref.get(ref)
        usable = action is not None and template.channel in action.channels
        if template.channel == _SMS and (action is None or not usable or _missing(action.dlt_template_id)):
            unregistered.append(f"{template.template_id} ({ref})")
            continue
        if not usable:
            missing.append(f"{template.template_id} ({ref})")
            continue
        assert action is not None
        found.append(
            ArmTemplate(
                arm=variant_level(template.variant),
                unit=_unit_of(template),
                channel=template.channel,
                template_id=template.template_id,
                template_ref=ref,
                catalogue_action_id=action.action_id,
                dlt_template_id=None if _missing(action.dlt_template_id) else action.dlt_template_id,
            )
        )
    if unregistered:
        raise VariantArmsError(
            VARIANT_TEMPLATE_UNREGISTERED,
            "An SMS variant can be sent only once its words are registered as a template: add a catalogue "
            "action on sms with template_ref set to the reference in brackets and its registered "
            "dlt_template_id, for " + ", ".join(unregistered) + ".",
        )
    if missing:
        raise VariantArmsError(
            VARIANT_ACTION_MISSING,
            "Every variant is entered in the action catalogue before it is sent: add an action on its channel "
            "with template_ref set to the reference in brackets, for " + ", ".join(missing) + ".",
        )
    return tuple(sorted(found, key=lambda item: (item.arm, item.unit, item.channel)))


def draw_arms(keys: Sequence[str], levels: Sequence[str], *, seed: int) -> Any:
    """Each key's arm: `levels[(draw(f"{seed}:variant_arm:{key}") * len(levels)) >> 64]` (see the module docstring).

    M92's 64-bit SHA-256 draw of the run's seed and the customer's own key text, so the arm never depends on
    the key's position or on the other keys; integer arithmetic, so the shares are exactly equal in law.
    Returns an object array of levels, one per key, in `keys` order.
    """
    import numpy as np

    from engine.holdout.assign import draw

    k = len(levels)
    if k < 1:
        raise ValueError("Arms are drawn among at least one level.")
    table = np.asarray(levels, dtype=object)
    picks = np.fromiter(
        ((draw(f"{seed}:{_DRAW_LABEL}:{key}") * k) >> 64 for key in keys), dtype=np.int64, count=len(keys)
    )
    return table[picks]


def _assign(
    storage: Storage, record: Any, batch: CopyBatch, *, levels: Sequence[str], seed: int
) -> pd.DataFrame:
    """`variant_arms.parquet`: one row per customer of the copy's groups, the copy's audience with an arm."""
    import numpy as np
    import pandas as pd

    from engine.config import sole_key
    from engine.generative.contracts import COPY_MESSAGES_FILENAME
    from engine.keys import key_text
    from engine.stages.export import SCORES_CSV
    from engine.storage import StorageError, run_key

    primary_key = sole_key(record.primary_key, what=f"Variant arms for run {record.run_id}")
    try:
        text = storage.read_text(run_key(record.run_id, SCORES_CSV))
        messages = pd.read_csv(
            io.StringIO(storage.read_text(run_key(record.run_id, COPY_MESSAGES_FILENAME))),
            dtype={"entity_key": "string"},
        )
    except StorageError as exc:
        raise VariantArmsError(
            VARIANT_ARMS_NOT_MADE, "The run's scores or its rendered copy are gone: generate the copy again."
        ) from exc
    scores = pd.read_csv(io.StringIO(text), dtype={primary_key: "string"})
    # The copy names each customer as `engine.generative.win_back` read the scores (`str` of the value pandas
    # parsed); the same file parsed that way gives each customer's copy name, row for row of one parse.
    copy_names = pd.read_csv(io.StringIO(text), usecols=[primary_key])[primary_key].astype(str)
    if not copy_names.is_unique:
        raise VariantArmsError(
            VARIANT_ARMS_NOT_MADE,
            "Two customers of this run share one name in the copy, so arms cannot be drawn.",
        )
    names = copy_names.to_numpy()

    suppressed = scores["suppressed_reason"].notna().to_numpy(dtype=bool)
    held = (
        scores["control_group"].astype(str).str.lower().isin(("true", "1")).to_numpy(dtype=bool) & ~suppressed
    )
    if batch.segment_by == _UPLIFT_SEGMENT:
        in_groups = (scores.get("segment", pd.Series(pd.NA, index=scores.index)) == _PERSUADABLE).fillna(
            False
        )
        if "intended_treatment" in scores.columns:
            in_groups &= scores["intended_treatment"].astype(str).str.lower().isin(("true", "1"))
        groups = in_groups.to_numpy(dtype=bool)
    else:
        written = set(batch.audience.per_band)
        groups = scores["band"].astype(str).isin(written).to_numpy(dtype=bool)
    in_scope = groups & ~suppressed

    unit_column = "segment" if "segment" in messages.columns else "band"
    units = messages.drop_duplicates("entity_key").set_index("entity_key")[unit_column].astype(str)
    copy_unit = pd.Series(names).map(units).to_numpy(dtype=object)
    audience = in_scope & ~held
    covered = pd.notna(copy_unit)
    if bool((audience != covered).any()):
        raise VariantArmsError(
            VARIANT_ARMS_NOT_MADE,
            "The copy was written for other customers than this run's scores now hold: generate the copy again.",
        )

    keys = key_text(scores[primary_key]).to_numpy(dtype=object)
    arm = np.full(len(scores), None, dtype=object)
    chosen = np.flatnonzero(audience)
    arm[chosen] = draw_arms([str(key) for key in keys[chosen]], levels, seed=seed)
    out = pd.DataFrame(
        {
            primary_key: scores[primary_key],
            COPY_UNIT_COLUMN: pd.Series(copy_unit, dtype="string").where(audience),
            VARIANT_ARM_COLUMN: pd.Series(arm, dtype="string"),
        }
    )
    return out.loc[in_scope].reset_index(drop=True)


# ---------------------------------------------------------------------------
# Readers: the treat list and the campaign
# ---------------------------------------------------------------------------
def variant_arms_of(storage: Storage, run_id: str) -> VariantArms | None:
    """The run's `variant_arms.json`, or None when it drew none (every run by default).

    The file as written, in force or not: readers that act on the arms use :func:`variant_arms_in_force`.
    """
    from engine.storage import StorageError, run_key

    key = run_key(run_id, VARIANT_ARMS_FILENAME)
    if not storage.exists(key):
        return None
    try:
        return storage.read_model(key, VariantArms)
    except StorageError:
        return None


def variant_arms_in_force(
    storage: Storage,
    run_id: str,
    *,
    use_case: UseCaseConfig | None = None,
    config_root: Path | None = None,
) -> VariantArms | None:
    """The run's arms while they still hold, else None (see "Arms in force" in the module docstring).

    `use_case` is the use case as it is now; without it, it is read from `config_root`, and only when the run
    has arms at all, so a run without them reads no configuration here. A use case that can no longer be read
    sets the arms aside.
    """
    from engine.config import ConfigError, load_use_case

    arms = variant_arms_of(storage, run_id)
    if arms is None:
        return None
    if use_case is None:
        try:
            use_case = load_use_case(arms.use_case_id, config_root)
        except ConfigError:
            return None
    if not use_case.generative.variant_arms:
        return None
    return arms if _matches_copy(storage, run_id, arms) else None


def _matches_copy(storage: Storage, run_id: str, arms: VariantArms) -> bool:
    """The run's copy is the batch the arms were drawn from, each arm template approved and its words registered."""
    return _copy_in_force(storage, run_id, arms) is not None


def _copy_in_force(storage: Storage, run_id: str, arms: VariantArms) -> CopyBatch | None:
    """The run's `copy_batch.json` when it is the batch the arms were drawn from, every arm template still
    approved with its words the registered ones; None otherwise."""
    from engine.generative.contracts import COPY_BATCH_FILENAME, CopyBatch
    from engine.storage import StorageError, run_key

    try:
        batch = storage.read_model(run_key(run_id, COPY_BATCH_FILENAME), CopyBatch)
    except StorageError:
        return None
    return batch if _batch_matches(batch, arms) else None


def _batch_matches(batch: CopyBatch, arms: VariantArms) -> bool:
    from engine.generative.contracts import CopyStatus

    if batch.batch_id != arms.batch_id or batch.created_at != arms.batch_created_at:
        return False
    by_id = {template.template_id: template for template in batch.templates}
    for item in arms.templates:
        template = by_id.get(item.template_id)
        if (
            template is None
            or template.status is not CopyStatus.APPROVED
            or variant_template_ref(template) != item.template_ref
        ):
            return False
    return True


def set_aside_variant_arms(
    storage: Storage, run_id: str, *, config_root: Path | None = None, only_if_stale: bool = False
) -> bool:
    """Delete the run's arm files and rebuild its treat list without them; True when there were arms.

    `only_if_stale`: only when they are no longer in force (an approved arm template was regenerated, the copy
    written again, the setting turned off). The treat list is rebuilt only when one was built before; a
    rebuild the run refuses leaves the list as it was (the readers ignore arms that are gone).
    """
    from engine.decide.treat_list import (
        TREAT_LIST_CSV,
        TREAT_LIST_PARQUET,
        TREAT_LIST_SUMMARY_FILENAME,
        TreatListError,
        build_treat_list,
    )
    from engine.storage import run_key

    files = [run_key(run_id, name) for name in (VARIANT_ARMS_FILENAME, VARIANT_ARMS_PARQUET)]
    if not any(storage.exists(key) for key in files):
        return False
    if only_if_stale and variant_arms_in_force(storage, run_id, config_root=config_root) is not None:
        return False
    for key in files:
        storage.delete(key)
    if storage.exists(run_key(run_id, TREAT_LIST_SUMMARY_FILENAME)):
        try:
            build_treat_list(storage, run_id, config_root=config_root)
        except TreatListError:
            # A list that still names the arms is not served again: it is built afresh (or refused in plain
            # words) on its next request. A list without them is left as it was handed off.
            if _treat_list_names_arms(storage, run_id):
                for name in (TREAT_LIST_CSV, TREAT_LIST_PARQUET, TREAT_LIST_SUMMARY_FILENAME):
                    storage.delete(run_key(run_id, name))
    return True


def _treat_list_names_arms(storage: Storage, run_id: str) -> bool:
    import pandas as pd

    from engine.decide.treat_list import TREAT_LIST_PARQUET
    from engine.storage import StorageError, run_key

    try:
        data = storage.read_bytes(run_key(run_id, TREAT_LIST_PARQUET))
    except StorageError:
        return False
    return VARIANT_ARM_COLUMN in pd.read_parquet(io.BytesIO(data), columns=None).columns


def _arm_table(storage: Storage, run_id: str, primary_key: PrimaryKey) -> pd.DataFrame | None:
    """`variant_arms.parquet` indexed by the key text the treat list spells; None when absent."""
    import pandas as pd

    from engine.decide.treat_list import _join_keys
    from engine.storage import StorageError, run_key

    try:
        data = storage.read_bytes(run_key(run_id, VARIANT_ARMS_PARQUET))
    except StorageError:
        return None
    table = pd.read_parquet(io.BytesIO(data))
    table.index = pd.Index(_join_keys(table, primary_key))
    return table[~table.index.duplicated(keep="first")]


class TreatListVariants:
    """The two treat-list columns of a run with variant arms, aligned to the treat list's rows."""

    def __init__(
        self, arm: pd.Series[Any], template: pd.Series[Any], counts: dict[str, int], note: str
    ) -> None:
        self.arm = arm
        self.template = template
        self.counts = counts
        self.note = note


class VariantRows:
    """A run's message arms in force, read once for a treat list (Plan K M145, DEC-1412): the arms, the copy batch
    they were drawn from, and per treat-list row the arm drawn and the group, None outside the arms."""

    def __init__(self, arms: VariantArms, batch: CopyBatch, arm: Any, unit: Any) -> None:
        self.arms = arms
        self.batch = batch
        self.arm = arm
        self.unit = unit


def variant_rows(
    storage: Storage,
    run_id: str,
    primary_key: PrimaryKey,
    row_keys: pd.Series[Any],
    *,
    config_root: Path | None = None,
) -> VariantRows | None:
    """Each treat-list row's arm and group, joined on every key column; None unless the arms are in force.

    Read before the compliance gate (`engine.decide.treat_list`, step 5c), so the gate checks each customer on the
    words their arm sends (`engine.compliance.variants`), and then given to :func:`variant_treat_list_columns`
    (step 5d): the arms, the copy and `variant_arms.parquet` are read once. The rule for "in force" is
    :func:`variant_arms_in_force`'s, with the copy batch it checked kept.
    """
    import numpy as np
    import pandas as pd

    from engine.config import ConfigError, load_use_case

    arms = variant_arms_of(storage, run_id)
    if arms is None:
        return None
    try:
        use_case = load_use_case(arms.use_case_id, config_root)
    except ConfigError:
        return None
    if not use_case.generative.variant_arms:
        return None
    batch = _copy_in_force(storage, run_id, arms)
    if batch is None:
        return None
    table = _arm_table(storage, run_id, primary_key)
    if table is None:
        return None
    index = pd.Index(row_keys.to_numpy())
    arm = table[VARIANT_ARM_COLUMN].reindex(index).astype("object").to_numpy()
    unit = table[COPY_UNIT_COLUMN].reindex(index).astype("object").to_numpy()
    empty = np.full(len(index), None, dtype=object)
    return VariantRows(
        arms, batch, np.where(pd.notna(arm), arm, empty), np.where(pd.notna(unit), unit, empty)
    )


def variant_treat_list_columns(
    storage: Storage,
    run_id: str,
    primary_key: PrimaryKey,
    row_keys: pd.Series[Any],
    *,
    treat: Any,
    channel: pd.Series[Any],
    config_root: Path | None = None,
    rows: VariantRows | None = None,
) -> TreatListVariants | None:
    """`variant_arm` and `variant_template` per treat-list row, joined on every key column; None without arms.

    None also when the run's arms are not in force (:func:`variant_arms_in_force`): the list is then the one
    without arms, byte for byte. A customer is shown their arm only when the list treats them (`treat = 1`);
    the template is the approved one their arm sends to their group on their channel (the copy's only channel
    when the list names none), empty for the control template and for a channel the copy was not written for
    (:func:`build_variant_arms` refuses arms that would leave a variant customer without one). `rows` is
    :func:`variant_rows` already read for this list (Plan K M145); read here when not given.
    """
    import numpy as np
    import pandas as pd

    if rows is None:
        rows = variant_rows(storage, run_id, primary_key, row_keys, config_root=config_root)
    if rows is None:
        return None
    arms = rows.arms
    index = pd.Index(row_keys.to_numpy())
    treated = np.asarray(treat, dtype=bool)
    arm = rows.arm
    unit = rows.unit
    arm = np.where(treated & pd.notna(arm), arm, np.full(len(index), None, dtype=object))
    lookup: dict[tuple[Any, Any, Any], str] = {
        (item.arm, item.unit, item.channel): item.template_id for item in arms.templates
    }
    only = arms.channels[0] if len(arms.channels) == 1 else None
    channels = channel.astype("object").to_numpy()
    template = np.full(len(index), None, dtype=object)
    for position in np.flatnonzero(pd.notna(arm)):
        sent_on = channels[position] if isinstance(channels[position], str) and channels[position] else only
        template[position] = lookup.get((arm[position], unit[position], sent_on))
    arm_series = pd.Series(arm, dtype="object")
    counts = {level: int((arm_series == level).sum()) for level in arms.arms}
    return TreatListVariants(
        arm=arm_series,
        template=pd.Series(template, dtype="object"),
        counts=counts,
        note=arms.control_template_note,
    )


def variant_campaign_scope(
    storage: Storage, run_id: str, primary_key: PrimaryKey, *, use_case: UseCaseConfig
) -> tuple[pd.Index[Any], pd.Series[Any]] | None:
    """`(customers in the copy's groups, each one's arm)` by key text, for a campaign opened from the run.

    The campaign is measured within exactly the groups the copy was written for, held back or not, so the
    no-message group and every arm cover the same customers (`build_assignment`'s `scope_keys`). None for a
    run without variant arms, or whose arms are not in force for `use_case` as it is now.
    """
    if variant_arms_in_force(storage, run_id, use_case=use_case) is None:
        return None
    table = _arm_table(storage, run_id, primary_key)
    if table is None:
        return None
    return table.index, table[VARIANT_ARM_COLUMN]


_NOT_SENT_WORDS: Final[str] = (
    "Not sent: the compliance gate stopped its words for every customer drawn into it, so it is not compared."
)
_NOT_SENT_SOME_WORDS: Final[str] = (
    "Set aside: the compliance gate stopped its words for some of the customers drawn into it, so the message "
    "was not sent as drawn and is not compared."
)
_NOT_SENT_NOBODY: Final[str] = (
    "Not sent: the compliance gate allowed none of the customers drawn into it, so it is not compared."
)


def variant_arms_not_sent(storage: Storage, run_id: str) -> dict[str, str]:
    """The message arms the compliance gate stopped on the run's treat list, each with why (Plan K M145, DEC-1412).

    An arm is stopped when the gate withheld any of its customers for the words they would be sent (a reason of
    `engine.compliance.handoff.WORD_CODES`: chosen by the arm, so not spread evenly across the arms as a
    customer's consent is), or when it sent the arm to none of its customers. A campaign sets such an arm aside
    whole. Empty unless the list was decided by the gate with the arms in force (`variant_arm_withheld` set), so
    a run without the gate, or without arms, is unchanged.
    """
    from engine.decide.treat_list import TREAT_LIST_SUMMARY_FILENAME, TreatListSummary
    from engine.storage import StorageError, run_key

    try:
        summary = storage.read_model(run_key(run_id, TREAT_LIST_SUMMARY_FILENAME), TreatListSummary)
    except StorageError:
        return {}
    if summary.variant_arm_withheld is None or summary.variant_arm_counts is None:
        return {}
    words = summary.variant_arm_words_withheld or {}
    out: dict[str, str] = {}
    for arm, withheld in summary.variant_arm_withheld.items():
        sent = summary.variant_arm_counts.get(arm, 0)
        if words.get(arm, 0) > 0:
            out[arm] = _NOT_SENT_WORDS if sent == 0 else _NOT_SENT_SOME_WORDS
        elif withheld > 0 and sent == 0:
            out[arm] = _NOT_SENT_NOBODY
    return out


def with_variant_arms(
    assignment: pd.DataFrame, arms: pd.Series[Any], primary_key: PrimaryKey
) -> pd.DataFrame:
    """The campaign's assignment with `variant_arm` for each contacted customer, joined on the key text."""
    import pandas as pd

    from engine.decide.treat_list import _join_keys
    from engine.measurement.campaign import ARM_TREATED

    out = assignment.copy()
    joined = arms.reindex(pd.Index(_join_keys(out, primary_key).to_numpy())).astype("string").to_numpy()
    treated = (out["arm"].astype("string") == ARM_TREATED).fillna(value=False).to_numpy(dtype=bool)
    column = pd.Series(joined, index=out.index, dtype="string")
    out[VARIANT_ARM_COLUMN] = column.where(treated, other=pd.NA)
    return out


def variant_measure_arguments(
    assignment: pd.DataFrame, *, outcome_kind: str = "binary", covariate: str | None = None
) -> dict[str, Any]:
    """`measure_campaign`'s several-offer arguments for a campaign with message arms; empty without them.

    The arms are the levels the assignment names, the control template first and the variants in letter
    order; the control level is the no-message holdout. Every message together is added (`combine_offers`),
    which the Value Proof Pack's whole-campaign lines read. Empty, too, for an amount (`outcome_kind` other
    than binary) or with a covariate: `measure_campaign` compares arms on a yes/no outcome without one, so such
    a campaign is measured as a whole, as it was before the arms.
    """
    if VARIANT_ARM_COLUMN not in assignment.columns or outcome_kind != "binary" or covariate is not None:
        return {}
    named = assignment[VARIANT_ARM_COLUMN].dropna().astype(str).str.strip()
    present = set(named[named != ""])
    if not present:
        return {}
    arms = [CONTROL_TEMPLATE_LEVEL] if CONTROL_TEMPLATE_LEVEL in present else []
    arms += sorted(level for level in present if level != CONTROL_TEMPLATE_LEVEL)
    return {
        "arm_column": VARIANT_ARM_COLUMN,
        "arms": arms,
        "control_level": NO_MESSAGE_LEVEL,
        "combine_offers": True,
    }


# ---------------------------------------------------------------------------
# Comparing the messages
# ---------------------------------------------------------------------------
def compare_variant_arms(
    report: IncrementalityReport, *, set_aside: Mapping[str, str] | None = None
) -> VariantComparison | None:
    """Every pair of message arms compared directly, and the winner when there is one (see the class).

    Read from the report's own `arms` counts (measured once, by `measure_campaign`): None when the report
    has fewer than two arms with measured customers. `set_aside` (Plan K M145): the arms the campaign set aside
    because the compliance gate stopped them (`Campaign.variant_arms_set_aside`), each with why; never compared,
    listed in `not_sent`.
    """
    from engine.uplift.incrementality import newcombe_interval

    aside = dict(set_aside or {})
    measured = [
        arm
        for arm in report.arms or ()
        if arm.treated_rows
        and arm.treated_conversions is not None
        and arm.treated_rows > 0
        and arm.arm not in aside
    ]
    if len(measured) < 2:
        return None
    family = len(measured) * (len(measured) - 1) // 2
    level = 1.0 - FAMILY_ALPHA / family
    z = NormalDist().inv_cdf(1.0 - FAMILY_ALPHA / (2.0 * family))
    counts: Mapping[str, tuple[int, int]] = {
        arm.arm: (int(arm.treated_conversions or 0), int(arm.treated_rows or 0)) for arm in measured
    }

    def pair(first: str, second: str) -> ConfidenceValue:
        x1, n1 = counts[first]
        x2, n2 = counts[second]
        value, low, high = newcombe_interval(x1, n1, x2, n2, z=z)
        return ConfidenceValue(value=value, ci_low=low, ci_high=high, confidence_level=level)

    names = [arm.arm for arm in measured]
    pairs = tuple(
        VariantPair(first=first, second=second, difference=pair(first, second))
        for i, first in enumerate(names)
        for second in names[i + 1 :]
    )
    leader = max(names, key=lambda name: (counts[name][0] / counts[name][1], -names.index(name)))
    against = {other: pair(leader, other) for other in names if other != leader}
    closest = min(against, key=lambda other: (against[other].ci_low or 0.0, names.index(other)))
    margin = against[closest]
    wins = all(interval.ci_low is not None and interval.ci_low > 0.0 for interval in against.values())
    summary = (
        f"{leader} did better than every other message, at the comparison's confidence."
        if wins
        else "No message is named the best: the leader's range is not above every other message's."
    )
    if aside:
        summary += f" Not sent, so not compared: {', '.join(aside)}."
    return VariantComparison(
        campaign_id=report.campaign_id,
        report_computed_at=report.computed_at,
        arms=tuple(names),
        family_alpha=FAMILY_ALPHA,
        family_size=family,
        family_confidence=level,
        pairs=pairs,
        leader=leader,
        winner=leader if wins else None,
        closest=closest,
        margin=margin,
        summary=summary,
        not_sent=(
            tuple(VariantNotSent(arm=arm, reason=reason) for arm, reason in aside.items()) if aside else None
        ),
    )
