"""The mandate: the signed box an autonomous marketer may act in, and who has to sign it.

A `Brief`'s `Envelope` says what the agent may do for one goal, approved once by one person. A
`Mandate` is the governance version of the same idea, kept by the business rather than the
Strategist: a versioned, signed statement of the hard limits (budget ceiling, daily spend, margin
floor, quiet hours, frequency cap, channels, templates, offers, excluded cohorts, validity window)
that no autonomous action may cross, whatever any goal or brief says.

Who must sign is a `SignaturePolicy`: a marketing lead always, finance (a CFO or finance signer)
when the budget is large, and never fewer than two distinct people (the two-person rule), so no
single person can hand the agent a budget on their own.

The models here are plain data. Hashing and signatures live in `autonomy.mandate.signing`, and the
per-action checks in `autonomy.mandate.enforce`.
"""

from __future__ import annotations

import itertools
import re
from datetime import UTC, datetime
from enum import StrEnum
from zoneinfo import ZoneInfo

from pydantic import AwareDatetime, Field, field_validator, model_validator

from autonomy.contracts import AudienceFilter, Channel, Contract

__all__ = [
    "FINANCE_ROLES",
    "MISSING_FINANCE",
    "MISSING_MARKETING_LEAD",
    "TWO_PERSON_RULE",
    "FrequencyCap",
    "Mandate",
    "MandateSignature",
    "MandateStatus",
    "QuietHours",
    "SignatureAlgorithm",
    "SignaturePolicy",
    "SignerRole",
    "to_utc",
]


def to_utc(value: datetime) -> datetime:
    """The same instant in UTC. Every stored time is normalised so the canonical hash of a mandate
    does not change when someone writes the same moment in another time zone."""
    if value.tzinfo is None:
        raise ValueError("times must carry a time zone")
    return value.astimezone(UTC)


def _valid_zone(name: str) -> str:
    try:
        ZoneInfo(name)
    except (KeyError, ValueError) as exc:
        raise ValueError(f"unknown time zone {name!r}") from exc
    return name


class MandateStatus(StrEnum):
    DRAFT = "draft"  # being written; nothing may act on it
    PENDING_SIGNATURES = "pending_signatures"  # submitted; waiting for the signers the policy needs
    ACTIVE = "active"  # fully signed; actions are checked against it
    EXPIRED = "expired"  # past `valid_until`
    REVOKED = "revoked"  # withdrawn by a person; final


class SignerRole(StrEnum):
    MARKETING_LEAD = "marketing_lead"
    CFO = "cfo"
    FINANCE = "finance"
    COMPLIANCE = "compliance"
    APPROVER = "approver"  # a second person with no special role, enough for the two-person rule


FINANCE_ROLES: tuple[SignerRole, ...] = (SignerRole.CFO, SignerRole.FINANCE)
"""Either of these satisfies the finance signature on a large budget."""


class SignatureAlgorithm(StrEnum):
    ED25519 = "ed25519"  # asymmetric: verifiers hold only public keys (the target)
    HMAC_SHA256 = "hmac-sha256"  # symmetric fallback when no Ed25519 library is installed


class QuietHours(Contract):
    """No message lands from `start` up to (not including) `end`, in the customer's local time.

    The window may wrap midnight (21 -> 9 means 21:00-08:59); equal start and end means no quiet
    hours. `timezone` is used only for a customer whose own zone is blank or not a valid IANA name.
    """

    start: int = Field(default=21, ge=0, le=23)
    end: int = Field(default=9, ge=0, le=23)
    timezone: str = "UTC"

    @field_validator("timezone")
    @classmethod
    def _zone(cls, v: str) -> str:
        return _valid_zone(v)

    def is_quiet(self, local_hour: int) -> bool:
        if self.start == self.end:
            return False
        if self.start < self.end:
            return self.start <= local_hour < self.end
        return local_hour >= self.start or local_hour < self.end


class FrequencyCap(Contract):
    """At most `max_touches` messages to one customer in any `window_days` days, across goals.

    `window_days` is at most 30 because customer state keeps 30 days of send times.
    """

    max_touches: int = Field(default=2, ge=0)
    window_days: int = Field(default=7, ge=1, le=30)


class MandateSignature(Contract):
    """One person's signature over one mandate body, in one role.

    The signature covers the mandate's body hash together with the person, role, key and time (see
    `autonomy.mandate.signing.signature_message`), so a signature cannot be moved to another
    mandate, another version, another role or another person without failing verification.
    """

    person_id: str = Field(min_length=1)
    role: SignerRole
    key_id: str = Field(min_length=1)
    algorithm: SignatureAlgorithm
    body_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    signed_at: AwareDatetime
    signature: str = Field(min_length=1)

    @field_validator("signed_at")
    @classmethod
    def _utc(cls, v: datetime) -> datetime:
        return to_utc(v)


_CURRENCY = re.compile(r"^[A-Z]{3}$")


class Mandate(Contract):
    """The hard limits on autonomous action for one business, one version at a time.

    Everything except `signers` and `status` is the *body*: the part that is hashed and signed.
    Changing any limit therefore needs a new version and new signatures; revoking or expiring a
    mandate changes only its status and keeps old signatures checkable.
    """

    mandate_id: str = Field(min_length=1)
    business_id: str = Field(min_length=1)
    goal_ids: tuple[str, ...] = Field(min_length=1, description="The goals this mandate governs")
    version: int = Field(default=1, ge=1)
    budget_ceiling: float = Field(ge=0, description="Total spend allowed over the validity window")
    currency: str = "USD"
    max_daily_spend: float | None = Field(default=None, ge=0, description="Per calendar day (UTC)")
    margin_floor_pct: float = Field(
        default=0.0,
        ge=0,
        lt=1,
        description="Expected margin after the offer's cost, as a share of revenue, must stay at or "
        "above this (0.2 = 20%)",
    )
    quiet_hours: QuietHours = QuietHours()
    frequency_cap: FrequencyCap = FrequencyCap()
    channels: tuple[Channel, ...] = Field(min_length=1)
    approved_template_ids: tuple[str, ...] = Field(
        default=(), description="Content ids that may be sent; an action without one is refused"
    )
    approved_offer_ids: tuple[str, ...] = Field(
        default=(), description="Offer ids that may be used; 'none' (no offer) is always allowed"
    )
    cohort_exclusions: tuple[AudienceFilter, ...] = Field(
        default=(), description="A customer matching any one of these is never contacted"
    )
    valid_from: AwareDatetime
    valid_until: AwareDatetime
    signers: tuple[MandateSignature, ...] = Field(default=(), description="Signatures collected so far")
    status: MandateStatus = MandateStatus.DRAFT
    created_by: str = ""
    note: str = Field(default="", description="Why this version exists, in plain words")

    @field_validator("valid_from", "valid_until")
    @classmethod
    def _utc(cls, v: datetime) -> datetime:
        return to_utc(v)

    @field_validator("currency")
    @classmethod
    def _currency(cls, v: str) -> str:
        if not _CURRENCY.match(v):
            raise ValueError("currency is a three-letter ISO code such as USD")
        return v

    @model_validator(mode="after")
    def _sane(self) -> Mandate:
        if self.valid_until <= self.valid_from:
            raise ValueError("valid_until must be after valid_from")
        if self.max_daily_spend is not None and self.max_daily_spend > self.budget_ceiling:
            raise ValueError("max_daily_spend cannot exceed budget_ceiling")
        if len(set(self.channels)) != len(self.channels):
            raise ValueError("channels are listed once each")
        return self

    def is_valid_at(self, at: datetime) -> bool:
        """True when `at` falls in [valid_from, valid_until)."""
        return self.valid_from <= at < self.valid_until

    def effective_status(self, now: datetime) -> MandateStatus:
        """The status as of `now`: an active mandate past `valid_until` reads as expired."""
        if self.status is MandateStatus.ACTIVE and now >= self.valid_until:
            return MandateStatus.EXPIRED
        return self.status


# Codes `SignaturePolicy.unmet` returns.
MISSING_MARKETING_LEAD = "missing_role:marketing_lead"
MISSING_FINANCE = "missing_role:finance"
TWO_PERSON_RULE = "two_person_rule"


class SignaturePolicy(Contract):
    """Who has to sign before a mandate can become active.

    * `always_required` roles sign every mandate (a marketing lead by default).
    * When `budget_ceiling` is above `finance_threshold`, one of `finance_roles` signs as well.
    * Each required role is filled by a *different* person: one person holding two roles cannot
      fill both. And at least `min_distinct_signers` different people sign (the two-person rule),
      so even a small mandate needs a second pair of eyes.

    This checks roles and people only. Whether each signature is genuine is
    `autonomy.mandate.signing.verify_mandate`'s job, which calls this after the cryptography.
    """

    always_required: tuple[SignerRole, ...] = (SignerRole.MARKETING_LEAD,)
    finance_threshold: float = Field(default=10_000.0, ge=0)
    finance_roles: tuple[SignerRole, ...] = FINANCE_ROLES
    min_distinct_signers: int = Field(default=2, ge=1)

    def needs_finance(self, mandate: Mandate) -> bool:
        return mandate.budget_ceiling > self.finance_threshold

    def required_slots(self, mandate: Mandate) -> list[tuple[str, frozenset[SignerRole]]]:
        """Each slot is (missing-code, roles that can fill it); each slot needs its own person."""
        slots: list[tuple[str, frozenset[SignerRole]]] = [
            (f"missing_role:{role.value}", frozenset({role})) for role in self.always_required
        ]
        if self.needs_finance(mandate):
            slots.append((MISSING_FINANCE, frozenset(self.finance_roles)))
        return slots

    def unmet(self, mandate: Mandate, signatures: tuple[MandateSignature, ...] | None = None) -> list[str]:
        """The policy's unmet requirements, as codes; empty when the signatures are enough.

        `signatures` defaults to the mandate's own `signers`. Pass only the verified ones when the
        caller has checked the cryptography (as `verify_mandate` does).
        """
        sigs = mandate.signers if signatures is None else signatures
        people_by_role: dict[SignerRole, set[str]] = {}
        for s in sigs:
            people_by_role.setdefault(s.role, set()).add(s.person_id)
        slots = self.required_slots(mandate)
        codes: list[str] = []
        # A slot nobody can fill at all is reported by name.
        for code, roles in slots:
            if not any(people_by_role.get(r) for r in roles):
                codes.append(code)
        if not codes and not _assign_distinct(slots, people_by_role):
            # Every slot has someone, but not enough different people to fill them all.
            codes.append(TWO_PERSON_RULE)
        distinct = {s.person_id for s in sigs}
        if len(distinct) < self.min_distinct_signers and TWO_PERSON_RULE not in codes:
            codes.append(TWO_PERSON_RULE)
        return codes


def _assign_distinct(
    slots: list[tuple[str, frozenset[SignerRole]]], people_by_role: dict[SignerRole, set[str]]
) -> bool:
    """True when every slot can be given a different person who signed in one of its roles.

    The slots are few (two or three), so trying every combination is cheap and obviously right.
    """
    candidates = [sorted({p for r in roles for p in people_by_role.get(r, set())}) for _, roles in slots]
    return any(len(set(combo)) == len(combo) for combo in itertools.product(*candidates))
