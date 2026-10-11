"""The words each customer of a message arm will receive, checked by the pre-send gate (Plan K M145, DEC-1412).

M134's gate (`engine.compliance.handoff`) checks one approved `message_text` per catalogue action. When a run's
message arms are in force (M140, `engine.decide.variant_arms`), a customer of a variant arm is sent that
variant's own words, not their band's message: so the gate checks, per customer and channel, the words that
customer will actually receive.

* **control_template** - the band's catalogue action and its `message_text`, checked as M134 checks it.
* **variant_<letter>** - the approved template the arm sends to the customer's group (`copy_unit`) on the
  channel, read from the run's `copy_batch.json` (the batch the arms were drawn from, never written again), checked
  against **its own** registered catalogue action (`ArmTemplate.catalogue_action_id`, the action whose
  `template_ref` names those exact words):

  1. no approved words of the variant on the channel - `COMPLIANCE_VARIANT_NO_COPY`: the customer cannot get
     their variant there, so that channel is closed to them (never another message in its place);
  2. no catalogue action registering the words on the channel, or, on a channel whose region rule requires a
     DLT template, no `dlt_template_text` (or no real `dlt_template_id`) on it - `COMPLIANCE_VARIANT_UNREGISTERED`:
     the words cannot be checked, so they do not go (fail closed, where M134 notes an unchecked action and lets
     it pass);
  3. the DLT matcher on the variant's text against that action's `dlt_template_text` -
     `COMPLIANCE_TEMPLATE_MISMATCH`;
  4. every link in the variant's subject and text against the URL whitelist - `COMPLIANCE_URL_NOT_WHITELISTED`.

* **no_message** - the held-back customers: never contacted, never gated.

The catalogue the messages are tied to is the one the arms were drawn under (`variant_arms.json`
`catalogue_sha256`, which registers the variants' words), and only when the arms were drawn over this run's own
stamp (`run_catalogue_sha256` equal to the run's `catalogue_stamp.json`): a run with no stamp, or arms drawn over
another stamp, fails closed as M134 does (`COMPLIANCE_CATALOGUE_CHANGED`).

Each (arm, group, channel) is checked **once**, however many customers it reaches; `engine.compliance.gate.decide`
then applies the verdicts by column operations, as for an action's. A variant's placeholders (`{{field}}`) are
checked as the template holds them, each in a `{#var#}` slot, not as a client's tool fills them per customer.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Final

from pydantic import Field

from engine.compliance.gate import MessageVerdict
from engine.contracts import Artefact

if TYPE_CHECKING:
    import numpy as np

    from engine.decide.catalogue import ActionCatalogue
    from engine.decide.variant_arms import VariantRows

__all__ = [
    "VARIANT_LEVEL_PREFIX",
    "VariantMessageCheck",
    "VariantMessages",
    "VariantWords",
    "is_variant_level",
    "variant_message_checks",
    "variant_messages",
]

VARIANT_LEVEL_PREFIX: Final[str] = "variant_"
"""An arm level that sends a variant's own words (`engine.decide.variant_arms.variant_level`)."""
_PLACEHOLDER: Final[re.Pattern[str]] = re.compile(r"\s*<[^>]*>\s*")


def is_variant_level(level: object) -> bool:
    """True for a variant arm (`variant_a`, ...); False for `control_template`, `no_message` and None."""
    return isinstance(level, str) and level.startswith(VARIANT_LEVEL_PREFIX)


def _missing(value: str | None) -> bool:
    """Empty, or a placeholder in angle brackets such as `<your DLT template id>` (the catalogue's own rule)."""
    return value is None or not value.strip() or _PLACEHOLDER.fullmatch(value) is not None


@dataclass(frozen=True)
class VariantWords:
    """One approved template a variant arm sends: to one group, on one channel, and the action registering it."""

    arm: str
    unit: str
    channel: str
    template_id: str
    template_ref: str
    catalogue_action_id: str
    subject: str | None
    text: str


@dataclass(frozen=True)
class VariantMessages:
    """What the gate needs of a run's message arms in force: per row of the list, the arm drawn and the group;
    the words of every arm template; and the catalogue and copy batch the arms were drawn under."""

    arm: np.ndarray
    """Per scored row of the list (aligned to the gate's `key_frame`): the arm drawn, None outside the arms."""
    unit: np.ndarray
    """Per scored row: the band or segment the customer's copy was written for, None outside the arms."""
    words: Mapping[tuple[str, str, str], VariantWords]
    """(arm, unit, channel) -> the approved template the arm sends there."""
    levels: tuple[str, ...]
    """The message arms, in order: the control template, then each variant."""
    batch_id: str
    batch_created_at: str
    catalogue_sha256: str
    """The catalogue the arms were drawn under and their words registered in (`variant_arms.json`)."""
    templates: tuple[tuple[str, str, str, str, str], ...] = field(default=())
    """(arm, unit, channel, template_ref, catalogue action) of every arm template, sorted: part of the rules."""
    run_catalogue_sha256: str | None = None
    """The run's own catalogue stamp the arms were drawn over (`variant_arms.json` `run_catalogue_sha256`); null
    when the run had none. The gate stands the arms' catalogue in for the run's stamp only when this is the stamp
    the gate is given: otherwise every row with a catalogue action is `COMPLIANCE_CATALOGUE_CHANGED`."""

    def rules(self) -> dict[str, Any]:
        """The arms' part of the gate's `rules` document (only on a run whose arms are in force)."""
        return {
            "batch_id": self.batch_id,
            "batch_created_at": self.batch_created_at,
            "catalogue_sha256": self.catalogue_sha256,
            "run_catalogue_sha256": self.run_catalogue_sha256,
            "templates": [list(item) for item in self.templates],
        }


def variant_messages(rows: VariantRows) -> VariantMessages:
    """The gate's input from the treat list's reading of the run's arms (`engine.decide.variant_arms.variant_rows`).

    The words are the templates of the copy batch the arms were drawn from, which `variant_rows` has already
    checked is the run's batch with every arm template still approved and its words still the registered ones.
    """
    by_id = {template.template_id: template for template in rows.batch.templates}
    words: dict[tuple[str, str, str], VariantWords] = {}
    for item in rows.arms.templates:
        template = by_id[item.template_id]
        words[(item.arm, item.unit, item.channel)] = VariantWords(
            arm=item.arm,
            unit=item.unit,
            channel=item.channel,
            template_id=item.template_id,
            template_ref=item.template_ref,
            catalogue_action_id=item.catalogue_action_id,
            subject=template.subject,
            text=template.text,
        )
    return VariantMessages(
        arm=rows.arm,
        unit=rows.unit,
        words=words,
        levels=tuple(rows.arms.arms),
        batch_id=rows.arms.batch_id,
        batch_created_at=rows.arms.batch_created_at.isoformat(),
        catalogue_sha256=rows.arms.catalogue_sha256,
        run_catalogue_sha256=rows.arms.run_catalogue_sha256,
        templates=tuple(
            sorted(
                (item.arm, item.unit, item.channel, item.template_ref, item.catalogue_action_id)
                for item in rows.arms.templates
            )
        ),
    )


class VariantMessageCheck(Artefact):
    """What was checked of one variant's words, for one group on one channel, and why not when it was not."""

    arm: str = Field(description="The variant arm.")
    unit: str = Field(description="The band or segment its words were written for.")
    channel: str = Field(description="The channel.")
    template_id: str | None = Field(description="The approved template sent there; null when there is none.")
    catalogue_action_id: str | None = Field(
        description="The catalogue action registering its words; null when there is no template."
    )
    no_copy: bool = Field(description="True when the variant has no approved words on this channel.")
    unregistered: bool = Field(
        description="True when the words have no registered template to check them against (fail closed)."
    )
    template_checked: bool = Field(description="Whether the DLT matcher ran on the words.")
    template_mismatch: bool | None = Field(description="Its verdict; null when it did not run.")
    urls_checked: bool = Field(description="Whether the links were checked against the whitelist.")
    urls_not_whitelisted: int | None = Field(description="Links not on the list; null when not checked.")
    notes: tuple[str, ...] = Field(default=(), description="Why a check did not run, in plain words.")


def variant_message_checks(
    catalogue: ActionCatalogue | None,
    catalogue_sha256: str | None,
    wanted: Iterable[tuple[str, str, str]],
    words: Mapping[tuple[str, str, str], VariantWords],
    *,
    dlt_channels: tuple[str, ...],
    whitelist: Any,
    var_max_chars: int | None,
) -> tuple[dict[tuple[str, str, str], MessageVerdict], tuple[VariantMessageCheck, ...]]:
    """The verdict of every wanted (arm, unit, channel), and what was checked of each (see the module docstring).

    `catalogue` is None when it is not the one the arms were drawn under: then nothing is checked (the rows are
    `COMPLIANCE_CATALOGUE_CHANGED`) and both results are empty.
    """
    from engine.compliance.dlt import TemplateMatcher, parse_template
    from engine.compliance.urls import find_links

    verdicts: dict[tuple[str, str, str], MessageVerdict] = {}
    checks: list[VariantMessageCheck] = []
    if catalogue is None:
        return verdicts, ()
    matchers: dict[str, Any] = {}
    for arm, unit, channel in sorted(set(wanted)):
        found = words.get((arm, unit, channel))
        notes: list[str] = []
        if found is None:
            notes.append(f"The variant has no approved words on {channel}: it is not sent there.")
            verdicts[(arm, unit, channel)] = MessageVerdict(no_copy=True)
            checks.append(
                VariantMessageCheck(
                    arm=arm,
                    unit=unit,
                    channel=channel,
                    template_id=None,
                    catalogue_action_id=None,
                    no_copy=True,
                    unregistered=False,
                    template_checked=False,
                    template_mismatch=None,
                    urls_checked=False,
                    urls_not_whitelisted=None,
                    notes=tuple(notes),
                )
            )
            continue
        action = catalogue.get_action(found.catalogue_action_id)
        registered = action is not None and channel in action.channels
        unregistered = not registered
        mismatch: bool | None = None
        if not registered:
            notes.append(
                f"No catalogue action registers the variant's words on {channel}: they cannot be checked, so "
                "they are not sent."
            )
        elif channel in dlt_channels:
            assert action is not None
            if action.dlt_template_text is None or _missing(action.dlt_template_id):
                unregistered = True
                notes.append(
                    f"The region requires a DLT template on {channel} and the variant's action has no "
                    "dlt_template_text (or no dlt_template_id) to check its words against: they are not sent."
                )
            else:
                matcher = matchers.get(action.action_id)
                if matcher is None:
                    matcher = TemplateMatcher(
                        parse_template(action.dlt_template_text), var_max_chars=var_max_chars
                    )
                    matchers[action.action_id] = matcher
                mismatch = not matcher.matches(found.text)
        else:
            notes.append(f"The region does not require a DLT template on {channel}: no template check.")
        bad: int | None = None
        if whitelist is None:
            notes.append("No URL whitelist is configured: links were not checked.")
        elif not whitelist.applies_to(channel):
            notes.append(f"The URL whitelist does not cover {channel}: links were not checked.")
        else:
            links = [*find_links(found.subject or ""), *find_links(found.text)]
            bad = sum(1 for url in links if not whitelist.allows(url))
        verdicts[(arm, unit, channel)] = MessageVerdict(
            template_mismatch=bool(mismatch),
            url_not_whitelisted=bool(bad),
            catalogue_ref=f"{catalogue_sha256}#{found.catalogue_action_id}",
            unregistered=unregistered,
        )
        checks.append(
            VariantMessageCheck(
                arm=arm,
                unit=unit,
                channel=channel,
                template_id=found.template_id,
                catalogue_action_id=found.catalogue_action_id,
                no_copy=False,
                unregistered=unregistered,
                template_checked=mismatch is not None,
                template_mismatch=mismatch,
                urls_checked=bad is not None,
                urls_not_whitelisted=bad,
                notes=tuple(notes),
            )
        )
    return verdicts, tuple(checks)
