"""India: TRAI's DLT rules for SMS, the NCPR do-not-disturb registry, and the DPDP Act on consent.

What the pack enforces, and where each rule comes from:

- **DLT templates (TRAI TCCCPR 2018).** Every commercial SMS must be a content template registered on
  an operator's DLT platform, sent under a registered header (sender id) by a registered principal
  entity. So an SMS template here must match a `DltRegistration` the brand gave us, which carries a
  well-formed header and entity id. The registered text tags its variable slots `{#var#}`,
  `{#numeric#}`, `{#alphanumeric#}`, `{#url#}` or `{#cbn#}` and no other way; each variable takes at
  most 30 characters, and the filled message must still be the template with every literal
  character kept exactly (operators reject a near miss). v1's `engine.compliance.dlt` is the full
  automaton; this is the same rule set, re-written small so the v2 core stays free of v1 imports.
- **Quiet hours.** Promotional SMS and calls only from 9am to 9pm, so 21:00-09:00 local is quiet.
  Transactional and service messages are exempt.
- **NCPR.** A promotional SMS or call to a number on the National Customer Preference Register needs
  a registered consent, so the gateway must look the number up first.
- **DPDP Act 2023.** Consent to marketing must be free, specific, informed and unambiguous (so
  explicit, never pre-ticked) and as easy to withdraw as to give; every promotional message
  therefore carries a clear opt-out. WhatsApp also needs an opt-in to WhatsApp itself (Meta policy,
  and v1's `engine.compliance.optin`).

An inquiry-based consent lasts only 7 days (the TCCCPR amendment v1's `configs/regions/in.yaml`
encodes); `consent_requirements` names that in its basis but leaves `valid_days` unset, because an
explicit consent lasts until withdrawn and the ledger, not the pack, knows how each was obtained.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from autonomy.channels.render import SLOT_PATTERN
from autonomy.compliance_packs.base import (
    MESSAGING_CHANNELS,
    TELECOM_CHANNELS,
    BasePack,
    ChannelLike,
    ConsentRequirement,
    Disclosure,
    DltRegistration,
    PackCheck,
    Purpose,
    QuietWindow,
    SmsTemplateRules,
    channel_name,
)
from autonomy.contracts import Channel, ContentItem

DLT_TAGS: tuple[str, ...] = ("var", "numeric", "alphanumeric", "url", "cbn")
"""The only variable tags a DLT template may use (as v1's `engine.compliance.dlt.DLT_TAGS`)."""

DLT_VAR_MAX_CHARS = 30
"""The longest value a `{#var#}`, `{#numeric#}` or `{#alphanumeric#}` slot takes."""

DLT_TEMPLATE_MAX_CHARS = 2000
"""The longest template the DLT portals accept. A message this long splits into many SMS parts; the
Critic's own channel limit (one segment) is the tighter rule for marketing copy."""

INDIA_SMS_RULES = SmsTemplateRules(
    requires_registered_template=True,
    requires_header=True,
    requires_entity_id=True,
    placeholder_tags=DLT_TAGS,
    var_max_chars=DLT_VAR_MAX_CHARS,
    template_max_chars=DLT_TEMPLATE_MAX_CHARS,
    header_patterns={
        Purpose.PROMOTIONAL.value: r"\d{6}",
        Purpose.TRANSACTIONAL.value: r"[A-Za-z]{6}",
        Purpose.SERVICE.value: r"[A-Za-z]{6}",
    },
    entity_id_pattern=r"\d{19}",
    template_id_pattern=r"\d{19}",
    opt_out_keyword="STOP",
)

OPT_OUT = Disclosure(
    code="opt_out",
    description="a clear way to opt out (for example 'Reply STOP to opt out' or 'Unsubscribe')",
    phrases=("opt out", "opt-out", "optout", "unsubscribe", "to stop receiving", "stop receiving"),
    patterns=(r"\b(?:reply|sms|send|text|txt|type)\s+stop\b", r"\bstop\s+to\s+\d{4,}\b"),
)
"""DPDP: withdrawing consent must be as easy as giving it, so a promotional message says how."""

_DLT_TAG = re.compile(r"\{#([^#{}]*)#\}")
_SENTINEL = "\x00"
"""Stands in for a `{{slot}}` while matching a template; a literal can never contain it."""


@dataclass(frozen=True)
class _Compiled:
    """A registration's text split into literal runs and tags, in order."""

    parts: tuple[tuple[str, str], ...]
    """('lit', text) or ('tag', name)."""


def parse_dlt(text: str) -> tuple[_Compiled | None, list[str]]:
    """The template's literal runs and tags, or None and the reasons it is not a valid DLT template."""
    problems: list[str] = []
    if not text.strip():
        return None, ["the registered DLT template is empty"]
    if SLOT_PATTERN.search(text):
        problems.append("the registered DLT template uses {{slot}} placeholders; DLT tags slots as {#var#}")
    parts: list[tuple[str, str]] = []
    position = 0
    for found in _DLT_TAG.finditer(text):
        tag = found.group(1).strip().lower()
        if tag not in DLT_TAGS:
            problems.append(f"{found.group(0)!r} is not a DLT variable tag ({', '.join(DLT_TAGS)})")
        if found.start() > position:
            parts.append(("lit", text[position : found.start()]))
        parts.append(("tag", tag))
        position = found.end()
    if position < len(text):
        parts.append(("lit", text[position:]))
    literal = "".join(t for kind, t in parts if kind == "lit")
    if "{#" in literal or "#}" in literal:
        problems.append("the registered DLT template has a malformed {#...#} tag")
    if len(text) > DLT_TEMPLATE_MAX_CHARS:
        problems.append(
            f"the registered DLT template is {len(text)} characters; at most {DLT_TEMPLATE_MAX_CHARS}"
        )
    if problems:
        return None, problems
    return _Compiled(tuple(parts)), []


def _tag_value_ok(tag: str, value: str, max_chars: int) -> bool:
    """Whether a filled value is of the tag's type and within the length limit."""
    if not value:
        return False
    if tag == "url":
        return bool(re.fullmatch(r"(?:https?://|www\.)\S+", value, re.IGNORECASE))
    if tag == "cbn":
        return bool(re.fullmatch(r"\+?\d[\d -]{5,18}\d", value))
    if len(value) > max_chars:
        return False
    if tag == "numeric":
        return bool(re.fullmatch(r"\d[\d.,]*", value))
    if tag == "alphanumeric":
        return value.isascii() and value.isalnum()
    return True


def _pattern(compiled: _Compiled) -> re.Pattern[str]:
    """Literals exact; each tag a lazy group (typed and length-checked afterwards, per group)."""
    pieces = [re.escape(t) if kind == "lit" else r"([\s\S]+?)" for kind, t in compiled.parts]
    return re.compile("".join(pieces))


def match_dlt(compiled: _Compiled, text: str, *, max_chars: int = DLT_VAR_MAX_CHARS) -> list[str] | None:
    """None when `text` is the template with each tag filled by a valid value; else the problems.

    `text` may still hold `{{slot}}` placeholders (a template being reviewed): each must sit inside a
    tag, and a tag holding one is only typed once the slot is filled, at render time.
    """
    marked = SLOT_PATTERN.sub(_SENTINEL, text)
    found = _pattern(compiled).fullmatch(marked)
    if found is None:
        return ["the text is not the registered DLT template (every literal character must match)"]
    tags = [t for kind, t in compiled.parts if kind == "tag"]
    problems: list[str] = []
    for tag, value in zip(tags, found.groups(), strict=True):
        if _SENTINEL in value:
            continue
        if not _tag_value_ok(tag, value, max_chars):
            problems.append(
                f"{{#{tag}#}} holds {value[:40]!r}, which is not a valid {tag} value of at most {max_chars} characters"
                if tag not in ("url", "cbn")
                else f"{{#{tag}#}} holds {value[:40]!r}, which is not a valid {tag}"
            )
    return problems or None


class IndiaPack(BasePack):
    """India in depth: DLT for SMS, NCPR, promotional quiet hours and DPDP consent and opt-out."""

    name = "IN"
    country_codes = frozenset({"IN", "IND"})

    @property
    def sms_template_rules(self) -> SmsTemplateRules:
        return INDIA_SMS_RULES

    def quiet_window(
        self, channel: ChannelLike = Channel.SMS, purpose: Purpose = Purpose.PROMOTIONAL
    ) -> QuietWindow | None:
        if purpose is Purpose.PROMOTIONAL and channel_name(channel) in TELECOM_CHANNELS:
            return QuietWindow(start_hour=21, end_hour=9)
        return None

    def required_disclosures(
        self, channel: ChannelLike, purpose: Purpose = Purpose.PROMOTIONAL
    ) -> tuple[Disclosure, ...]:
        if purpose is Purpose.PROMOTIONAL and channel_name(channel) in MESSAGING_CHANNELS:
            return (OPT_OUT,)
        return ()

    def consent_requirements(
        self, channel: ChannelLike, purpose: Purpose = Purpose.PROMOTIONAL
    ) -> ConsentRequirement:
        name = channel_name(channel)
        if purpose is not Purpose.PROMOTIONAL:
            return ConsentRequirement(
                kind="none",
                basis="DPDP legitimate use: a transactional or service message the customer expects",
            )
        return ConsentRequirement(
            kind="explicit",
            basis="DPDP Act 2023 s.6: free, specific, informed, unambiguous consent"
            + (
                "; TRAI TCCCPR: consent registered on DLT for NCPR numbers (an inquiry-based one lasts 7 days)"
                if name in TELECOM_CHANNELS
                else ""
            )
            + ("; WhatsApp Business policy: an opt-in to WhatsApp itself" if name == "whatsapp" else ""),
            channel_specific=name == "whatsapp" or name in TELECOM_CHANNELS,
            pre_ticked_allowed=False,
            withdrawal_as_easy_as_grant=True,
        )

    def needs_dnd_check(self, channel: ChannelLike, purpose: Purpose = Purpose.PROMOTIONAL) -> bool:
        return purpose is Purpose.PROMOTIONAL and channel_name(channel) in TELECOM_CHANNELS

    def dnd_registry(self) -> str:
        return "NCPR (TRAI National Customer Preference Register)"

    # --- DLT ---------------------------------------------------------------------------------------

    def registration_for(
        self, item: ContentItem, purpose: Purpose = Purpose.PROMOTIONAL
    ) -> DltRegistration | None:
        """The DLT registration this SMS item is sent as.

        When the item names a template (`template_name` holds the DLT template id) it is that one or
        none. Otherwise the first registration, by template id, whose text the item's body matches
        and whose purpose is the message's.
        """
        templates = self.settings.dlt_templates
        if item.template_name:
            return next((t for t in templates if t.template_id == item.template_name), None)
        for reg in sorted(templates, key=lambda t: t.template_id):
            compiled, problems = parse_dlt(reg.text)
            if compiled is None or problems or reg.purpose is not purpose:
                continue
            if match_dlt(compiled, item.body) is None:
                return reg
        return None

    def registration_checks(self, reg: DltRegistration, purpose: Purpose) -> list[PackCheck]:
        """The ids and template text of one registration: well formed and of the right purpose."""
        rules = INDIA_SMS_RULES
        header_rule = rules.header_patterns.get(purpose.value, r"[A-Za-z0-9]{6}")
        _, text_problems = parse_dlt(reg.text)
        return [
            self.check(
                "dlt:header",
                bool(re.fullmatch(header_rule, reg.header.strip())),
                f"DLT template {reg.template_id} needs a registered {purpose.value} header "
                f"(sender id matching {header_rule}); it has {reg.header!r}",
            ),
            self.check(
                "dlt:entity_id",
                bool(
                    rules.entity_id_pattern and re.fullmatch(rules.entity_id_pattern, reg.entity_id.strip())
                ),
                f"DLT template {reg.template_id} needs the brand's principal entity id (19 digits); "
                f"it has {reg.entity_id!r}",
            ),
            self.check(
                "dlt:template_id",
                bool(
                    rules.template_id_pattern
                    and re.fullmatch(rules.template_id_pattern, reg.template_id.strip())
                ),
                f"DLT template id {reg.template_id!r} is not a 19-digit DLT id",
            ),
            self.check(
                "dlt:placeholder_format",
                not text_problems,
                "; ".join(text_problems) or "the registered DLT text is malformed",
            ),
            self.check(
                "dlt:category",
                reg.purpose is purpose,
                f"DLT template {reg.template_id} is registered as {reg.purpose.value}, "
                f"but this is a {purpose.value} message",
            ),
        ]

    def content_checks(
        self, item: ContentItem, channel: ChannelLike | None = None, purpose: Purpose = Purpose.PROMOTIONAL
    ) -> list[PackCheck]:
        checks = self.disclosure_checks(item, channel, purpose)
        if channel_name(channel if channel is not None else item.channel) != "sms":
            return checks
        reg = self.registration_for(item, purpose)
        if reg is None:
            named = f" {item.template_name!r}" if item.template_name else ""
            return [
                *checks,
                self.check(
                    "dlt:registered_template",
                    False,
                    f"an SMS needs a registered DLT template{named}; none of the brand's "
                    f"{len(self.settings.dlt_templates)} registered template(s) matches this body",
                ),
            ]
        checks.append(
            self.check("dlt:registered_template", True, "", f"matches DLT template {reg.template_id}")
        )
        checks.extend(self.registration_checks(reg, purpose))
        compiled, _ = parse_dlt(reg.text)
        if compiled is not None:
            problems = match_dlt(compiled, item.body)
            checks.append(
                self.check(
                    "dlt:template_match",
                    problems is None,
                    f"the body does not fit DLT template {reg.template_id}: {'; '.join(problems or [])}",
                )
            )
        return checks

    def rendered_checks(
        self,
        body: str,
        item: ContentItem,
        channel: ChannelLike | None = None,
        purpose: Purpose = Purpose.PROMOTIONAL,
    ) -> list[PackCheck]:
        """The filled SMS must still be its registered template, each variable at most 30 characters."""
        if channel_name(channel if channel is not None else item.channel) != "sms":
            return []
        reg = self.registration_for(item, purpose)
        compiled = parse_dlt(reg.text)[0] if reg is not None else None
        if reg is None or compiled is None:
            return [self.check("dlt:rendered_match", False, "the SMS has no valid registered DLT template")]
        problems = match_dlt(compiled, body)
        return [
            self.check(
                "dlt:rendered_match",
                problems is None,
                f"the filled SMS is not DLT template {reg.template_id}: {'; '.join(problems or [])}",
            )
        ]
