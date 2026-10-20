"""`/mandates`: draft, sign, certify, check and revoke mandates, and publish the Action Catalogue.

A mandate is the signed box autonomous marketing acts in (`autonomy.mandate`). This router is the
way people put one in place: a marketer drafts it, each required signer signs it, and `certify`
turns it on only when the signatures satisfy the `SignaturePolicy` (a marketing lead always,
finance above a threshold, two different people at least). Until then nothing may act on it.
`check-action` is the same gate the runtime will call before a send, so anyone can ask "would this
action be allowed, and if not, why?". Every change lands in the hash-chained `MandateLedger`.

The routes hold no rules of their own: they parse, call `autonomy.mandate` and wrap the answer.
State lives in a `MandateRegistry`, injected through `get_mandate_registry`, so tests (and later
the database-backed version) swap it with `app.dependency_overrides`.

**Time.** Logic never reads the clock. Each body may carry `now`; when it does not, the registry's
`clock` supplies it. This router is the edge of the system, so that is the one place a real clock
is allowed, and tests always pass `now` (or inject a fixed clock).

**Money and margins.** `budget_ceiling_inr` is shorthand for `budget_ceiling` in INR.
`margin_floor_pct` here is a percentage (18 means 18%); the model stores it as a share (0.18).
"""

from __future__ import annotations

import json
import re
import threading
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import AwareDatetime, Field, ValidationError, model_validator

from autonomy.contracts import Action, AudienceFilter, Channel, Contract, CustomerState, OfferOption
from autonomy.mandate.catalogue import (
    ActionCatalogue,
    CatalogueContent,
    CatalogueDiff,
    CatalogueError,
    CatalogueTemplate,
    CatalogueVersion,
    ChannelRate,
    catalogue_costs,
    check_action_against_catalogue,
)
from autonomy.mandate.enforce import ActionCosts, SpendToDate, check_action
from autonomy.mandate.models import (
    FrequencyCap,
    Mandate,
    MandateSignature,
    MandateStatus,
    QuietHours,
    SignatureAlgorithm,
    SignaturePolicy,
    SignerRole,
)
from autonomy.mandate.signing import (
    KeyRing,
    MandateLedger,
    Signer,
    body_hash,
    sign_mandate,
    verify_mandate,
    verify_signature,
    with_signature,
)

router = APIRouter(prefix="/mandates", tags=["mandates"])

RESERVED_IDS = frozenset({"catalogue"})
"""Path segments under /mandates that are routes, so no mandate may take them as its id."""

DEFAULT_VALID_DAYS = 30


# --- state ----------------------------------------------------------------------------------------


def _system_clock() -> datetime:
    return datetime.now(UTC)


@dataclass
class _Record:
    """What the router keeps beside the mandate itself (which lives in the ledger)."""

    expected_signers: tuple[SignerSpec, ...]
    catalogue_version: int | None


@dataclass
class MandateRegistry:
    """Everything the router reads and writes. In memory; inject your own for tests or storage.

    * `policy` decides who must sign; `policies` overrides it per currency, because a finance
      threshold of 10,000 means something different in USD and in INR.
    * `keys` is what verifiers know (each key id bound to one person and their roles).
    * `custodial` holds signers the server may sign with when a request names a key instead of
      carrying a signature: development and tests only, until people sign on their own devices.
    """

    policy: SignaturePolicy = field(default_factory=SignaturePolicy)
    policies: Mapping[str, SignaturePolicy] = field(default_factory=dict)
    keys: KeyRing = field(default_factory=KeyRing)
    custodial: Mapping[str, Signer] = field(default_factory=dict)
    catalogue: ActionCatalogue = field(default_factory=ActionCatalogue)
    ledger: MandateLedger = field(default_factory=MandateLedger)
    clock: Callable[[], datetime] = _system_clock
    records: dict[str, _Record] = field(default_factory=dict)
    lock: threading.Lock = field(default_factory=threading.Lock, init=False, repr=False, compare=False)

    def policy_for(self, mandate: Mandate) -> SignaturePolicy:
        return self.policies.get(mandate.currency, self.policy)

    def now(self, given: datetime | None) -> datetime:
        return given if given is not None else self.clock()

    def get(self, mandate_id: str) -> Mandate:
        found = self.ledger.latest(mandate_id)
        if found is None or mandate_id not in self.records:
            raise HTTPException(status_code=404, detail=f"no mandate {mandate_id!r}")
        return found


_DEFAULT_REGISTRY: MandateRegistry | None = None
_DEFAULT_LOCK = threading.Lock()


def get_mandate_registry() -> MandateRegistry:
    """The process-wide in-memory registry. Override this dependency to inject another."""
    global _DEFAULT_REGISTRY
    with _DEFAULT_LOCK:
        if _DEFAULT_REGISTRY is None:
            _DEFAULT_REGISTRY = MandateRegistry()
        return _DEFAULT_REGISTRY


RegistryDep = Annotated[MandateRegistry, Depends(get_mandate_registry)]


# --- request and response bodies ------------------------------------------------------------------

_HOUR = re.compile(r"^(\d{1,2})(?::00)?$")


def _hour(value: int | str) -> int:
    """21, "21" or "21:00" -> 21. Quiet hours are whole hours, so "21:30" is refused, not rounded."""
    if isinstance(value, int):
        return value
    m = _HOUR.match(value.strip())
    if m is None:
        raise ValueError(f"{value!r} is not a whole hour such as 21 or '21:00'")
    return int(m.group(1))


class QuietHoursIn(Contract):
    start: int | str = 21
    end: int | str = 9
    timezone: str = "UTC"

    def to_model(self) -> QuietHours:
        return QuietHours(start=_hour(self.start), end=_hour(self.end), timezone=self.timezone)


class FrequencyCapIn(Contract):
    """`max_touches` (or its alias `max_per_customer`) in any `window_days` days."""

    max_touches: int | None = Field(default=None, ge=0)
    max_per_customer: int | None = Field(default=None, ge=0)
    window_days: int = Field(default=7, ge=1, le=30)

    def to_model(self) -> FrequencyCap:
        if None not in (self.max_touches, self.max_per_customer) and (
            self.max_touches != self.max_per_customer
        ):
            raise ValueError("max_touches and max_per_customer are the same limit; give one")
        n = self.max_touches if self.max_touches is not None else self.max_per_customer
        return FrequencyCap(max_touches=2 if n is None else n, window_days=self.window_days)


class BudgetIn(Contract):
    amount: float = Field(ge=0, allow_inf_nan=False)
    currency: str = Field(min_length=3, max_length=3)


class SignerSpec(Contract):
    """A person expected to sign, in a role. `key_id` names their key when it is known up front."""

    role: SignerRole
    signer_id: str = Field(min_length=1)
    key_id: str | None = None


class MandateDraft(Contract):
    """A new mandate, in the shape of the founder's example request.

    Give the budget as `budget_ceiling_inr`, or as `budget_ceiling` (a number with `currency`, or
    `{amount, currency}`). A bare number with no currency is refused rather than guessed: the same
    500,000 is a very different ceiling in INR and in USD. Channels may be left out when a catalogue is published: they are then the
    channels of the approved templates. The validity window defaults to 30 days from `now`.
    """

    mandate_id: str = Field(min_length=1, max_length=200, pattern=r"^[A-Za-z0-9_.:-]+$")
    business_id: str = Field(default="default", min_length=1)
    goal_ids: tuple[str, ...] = Field(min_length=1)
    budget_ceiling_inr: float | None = Field(default=None, ge=0, allow_inf_nan=False)
    budget_ceiling: Annotated[float, Field(ge=0, allow_inf_nan=False)] | BudgetIn | None = None
    currency: str | None = Field(default=None, min_length=3, max_length=3)
    max_daily_spend: float | None = Field(default=None, ge=0, allow_inf_nan=False)
    margin_floor_pct: float = Field(default=0.0, ge=0, lt=100, description="A percentage: 18 = 18%")
    quiet_hours: QuietHoursIn = QuietHoursIn()
    frequency_cap: FrequencyCapIn = FrequencyCapIn()
    channels: tuple[Channel, ...] = ()
    approved_templates: tuple[str, ...] = ()
    approved_offers: tuple[str, ...] = ()
    cohort_exclusions: tuple[AudienceFilter, ...] = ()
    valid_from: AwareDatetime | None = None
    valid_until: AwareDatetime | None = None
    signers: tuple[SignerSpec, ...] = ()
    catalogue_version: int | None = Field(
        default=None, ge=1, description="Pin a catalogue version; default the latest, if any"
    )
    created_by: str = ""
    note: str = ""
    now: AwareDatetime | None = None

    @model_validator(mode="after")
    def _one_budget(self) -> MandateDraft:
        if (self.budget_ceiling_inr is None) == (self.budget_ceiling is None):
            raise ValueError("give exactly one of budget_ceiling_inr and budget_ceiling")
        return self

    def budget(self) -> tuple[float, str]:
        if self.budget_ceiling_inr is not None:
            if self.currency not in (None, "INR"):
                raise ValueError("budget_ceiling_inr is in INR; drop currency or use budget_ceiling")
            return self.budget_ceiling_inr, "INR"
        if isinstance(self.budget_ceiling, BudgetIn):
            if self.currency not in (None, self.budget_ceiling.currency):
                raise ValueError("currency disagrees with budget_ceiling.currency")
            return self.budget_ceiling.amount, self.budget_ceiling.currency
        assert self.budget_ceiling is not None  # the validator guarantees one of the two
        if self.currency is None:
            raise ValueError("budget_ceiling as a number needs a currency (or use budget_ceiling_inr)")
        return float(self.budget_ceiling), self.currency


class SignRequest(Contract):
    """One signature. Either carry it (`signature` + `key_id` + `signed_at` + `algorithm`, made by
    the signer over `signature_message`), or name a server-held key with `key_id` (or neither, to
    use the custodial key registered to `signer_id`)."""

    role: SignerRole
    signer_id: str = Field(min_length=1)
    key_id: str | None = Field(default=None, min_length=1)
    signature: str | None = Field(default=None, min_length=1)
    signed_at: AwareDatetime | None = None
    algorithm: SignatureAlgorithm | None = None
    now: AwareDatetime | None = None

    @model_validator(mode="after")
    def _complete(self) -> SignRequest:
        if self.signature is not None and (
            self.key_id is None or self.signed_at is None or self.algorithm is None
        ):
            raise ValueError("a carried signature needs key_id, signed_at and algorithm")
        return self


class ActorRequest(Contract):
    actor: str = ""
    now: AwareDatetime | None = None


class RevokeRequest(Contract):
    """Who revokes. (The ledger entry has no field for a reason yet; see docs/v2/MANDATE.md.)"""

    actor: str = Field(min_length=1)
    now: AwareDatetime | None = None


class CheckActionRequest(Contract):
    """The action to test. `costs` left out are taken from the mandate's catalogue version."""

    action: Action
    costs: ActionCosts | None = None
    state: CustomerState
    now: AwareDatetime
    spend: SpendToDate | None = None
    pending_touches: int = Field(default=0, ge=0)


class MandateView(Contract):
    mandate: Mandate
    body_hash: str
    status: MandateStatus = Field(description="As of the last change; expiry is applied on check")
    unmet: tuple[str, ...] = Field(description="What the signatures still lack; empty when enough")
    expected_signers: tuple[SignerSpec, ...] = ()
    catalogue_version: int | None = None
    ledger_head: str


class CheckActionResult(Contract):
    allowed: bool
    violations: tuple[str, ...]
    mandate_id: str
    mandate_version: int
    catalogue_version: int | None = None
    costs: ActionCosts


class CataloguePublish(Contract):
    offers: tuple[OfferOption, ...] = ()
    channels: tuple[ChannelRate, ...] = ()
    templates: tuple[CatalogueTemplate, ...] = ()
    created_by: str = Field(min_length=1)
    now: AwareDatetime | None = None
    allow_unchanged: bool = False


class CatalogueDiffView(Contract):
    diff: CatalogueDiff
    summary: str


class CatalogueHistoryItem(Contract):
    version: int
    created_at: AwareDatetime
    created_by: str
    summary: str
    content_hash: str


class MandateLedgerView(Contract):
    mandate_id: str
    entries: tuple[dict[str, Any], ...]
    chain_ok: bool
    chain_problem: str = ""
    head: str


# --- helpers --------------------------------------------------------------------------------------


def _errors(exc: ValidationError) -> Any:
    """Pydantic's errors as plain JSON, without the inputs (which may be models JSON cannot carry)."""
    return json.loads(exc.json(include_context=False, include_input=False, include_url=False))


def _view(reg: MandateRegistry, mandate: Mandate) -> MandateView:
    rec = reg.records[mandate.mandate_id]
    return MandateView(
        mandate=mandate,
        body_hash=body_hash(mandate),
        status=mandate.status,
        unmet=tuple(verify_mandate(mandate, reg.policy_for(mandate), reg.keys)),
        expected_signers=rec.expected_signers,
        catalogue_version=rec.catalogue_version,
        ledger_head=reg.ledger.head,
    )


def _pinned_catalogue(reg: MandateRegistry, version: int | None) -> CatalogueVersion | None:
    try:
        if version is not None:
            return reg.catalogue.get(version)
        return reg.catalogue.latest()
    except CatalogueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


def _build_mandate(draft: MandateDraft, catalogue: CatalogueVersion | None, now: datetime) -> Mandate:
    """The draft as a `Mandate`, checked against the catalogue it pins (when one is published)."""
    amount, currency = draft.budget()
    channels = draft.channels
    if catalogue is not None:
        unknown = catalogue.unknown_templates(draft.approved_templates)
        if unknown:
            raise ValueError(f"not in catalogue v{catalogue.version}: templates {', '.join(unknown)}")
        missing_offers = [o for o in draft.approved_offers if catalogue.offer(o) is None and o != "none"]
        if missing_offers:
            raise ValueError(f"not in catalogue v{catalogue.version}: offers {', '.join(missing_offers)}")
        if not channels:
            found = (catalogue.template(t) for t in draft.approved_templates)
            channels = tuple(dict.fromkeys(t.channel for t in found if t is not None))
    if not channels:
        raise ValueError("name the channels, or approve templates from a published catalogue")
    valid_from = draft.valid_from or now
    return Mandate(
        mandate_id=draft.mandate_id,
        business_id=draft.business_id,
        goal_ids=draft.goal_ids,
        budget_ceiling=amount,
        currency=currency,
        max_daily_spend=draft.max_daily_spend,
        margin_floor_pct=draft.margin_floor_pct / 100.0,
        quiet_hours=draft.quiet_hours.to_model(),
        frequency_cap=draft.frequency_cap.to_model(),
        channels=channels,
        approved_template_ids=draft.approved_templates,
        approved_offer_ids=draft.approved_offers,
        cohort_exclusions=draft.cohort_exclusions,
        valid_from=valid_from,
        valid_until=draft.valid_until or valid_from + timedelta(days=DEFAULT_VALID_DAYS),
        status=MandateStatus.DRAFT,
        created_by=draft.created_by,
        note=draft.note,
    )


def _custodial_key(reg: MandateRegistry, req: SignRequest, rec: _Record) -> Signer:
    """The server-held signer for this request: the named key, the one listed in the draft for this
    person and role, or the single custodial key registered to this person."""
    key_id = req.key_id
    if key_id is None:
        key_id = next(
            (
                s.key_id
                for s in rec.expected_signers
                if s.signer_id == req.signer_id and s.role is req.role and s.key_id
            ),
            None,
        )
    if key_id is None:
        owned = [k for k in reg.custodial if reg.keys.owner(k) == req.signer_id]
        if len(owned) != 1:
            raise HTTPException(
                status_code=400, detail=f"name a key_id for {req.signer_id!r} or carry a signature"
            )
        key_id = owned[0]
    signer = reg.custodial.get(key_id)
    if signer is None:
        raise HTTPException(status_code=400, detail=f"no server-held key {key_id!r}")
    return signer


def _awaiting(codes: list[str], rec: _Record, policy: SignaturePolicy) -> list[dict[str, str]]:
    """The listed signers who could fill each missing role, so the 409 says whom to chase."""
    out: list[dict[str, str]] = []
    for code in codes:
        if not code.startswith("missing_role:"):
            continue
        name = code.split(":", 1)[1]
        roles = set(policy.finance_roles) if name == "finance" else {SignerRole(name)}
        for s in rec.expected_signers:
            if s.role in roles:
                out.append({"role": s.role.value, "signer_id": s.signer_id, "for": code})
    return out


# --- catalogue routes (declared first, so 'catalogue' is never read as a mandate id) ---------------


@router.get("/catalogue", response_model=CatalogueVersion)
def get_catalogue(reg: RegistryDep, version: Annotated[int | None, Query(ge=1)] = None) -> CatalogueVersion:
    """One catalogue version, or the latest when `version` is left out."""
    try:
        return reg.catalogue.get("latest") if version is None else reg.catalogue.get(version)
    except CatalogueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.post("/catalogue", response_model=CatalogueVersion, status_code=201)
def publish_catalogue(body: CataloguePublish, reg: RegistryDep) -> CatalogueVersion:
    """Publish the next catalogue version. Old versions are kept, unchanged, forever."""
    try:
        content = CatalogueContent(offers=body.offers, channels=body.channels, templates=body.templates)
    except ValidationError as exc:
        raise HTTPException(status_code=422, detail=_errors(exc)) from exc
    try:
        return reg.catalogue.publish(
            content,
            created_by=body.created_by,
            now=reg.now(body.now),
            allow_unchanged=body.allow_unchanged,
        )
    except CatalogueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.get("/catalogue/diff", response_model=CatalogueDiffView)
def diff_catalogue(
    reg: RegistryDep,
    a: Annotated[int, Query(ge=1, description="From this version")],
    b: Annotated[int | None, Query(ge=1, description="To this version; default the latest")] = None,
) -> CatalogueDiffView:
    try:
        diff = reg.catalogue.diff(a, "latest") if b is None else reg.catalogue.diff(a, b)
    except CatalogueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return CatalogueDiffView(diff=diff, summary=diff.summary())


@router.get("/catalogue/history", response_model=list[CatalogueHistoryItem])
def catalogue_history(reg: RegistryDep) -> list[CatalogueHistoryItem]:
    return [
        CatalogueHistoryItem(
            version=v.version,
            created_at=v.created_at,
            created_by=v.created_by,
            summary=v.diff_from_previous.summary(),
            content_hash=v.content_hash,
        )
        for v in reg.catalogue.history()
    ]


# --- mandate routes -------------------------------------------------------------------------------


@router.post("", response_model=MandateView, status_code=201)
def draft_mandate(body: MandateDraft, reg: RegistryDep) -> MandateView:
    """Draft a mandate. Nothing may act on it until it is signed and certified."""
    if body.mandate_id in RESERVED_IDS:
        raise HTTPException(status_code=400, detail=f"{body.mandate_id!r} is a reserved name")
    now = reg.now(body.now)
    with reg.lock:
        if body.mandate_id in reg.records:
            raise HTTPException(status_code=409, detail=f"mandate {body.mandate_id!r} already exists")
        catalogue = _pinned_catalogue(reg, body.catalogue_version)
        try:
            mandate = _build_mandate(body, catalogue, now)
        except (ValueError, ValidationError) as exc:
            detail: Any = _errors(exc) if isinstance(exc, ValidationError) else str(exc)
            raise HTTPException(status_code=422, detail=detail) from exc
        reg.ledger.append("created", mandate, actor=body.created_by or "unknown", at=now)
        reg.records[mandate.mandate_id] = _Record(
            expected_signers=body.signers,
            catalogue_version=catalogue.version if catalogue else None,
        )
        return _view(reg, mandate)


@router.get("/{mandate_id}", response_model=MandateView)
def get_mandate(mandate_id: str, reg: RegistryDep) -> MandateView:
    return _view(reg, reg.get(mandate_id))


@router.get("/{mandate_id}/ledger", response_model=MandateLedgerView)
def mandate_ledger(mandate_id: str, reg: RegistryDep) -> MandateLedgerView:
    """This mandate's history, and whether the whole chain still verifies."""
    reg.get(mandate_id)
    report = reg.ledger.verify_chain(reg.keys)
    return MandateLedgerView(
        mandate_id=mandate_id,
        entries=tuple(
            e.model_dump(mode="json", include={"seq", "kind", "actor", "at", "version", "entry_hash"})
            for e in reg.ledger.history(mandate_id)
        ),
        chain_ok=report.ok,
        chain_problem=report.problem,
        head=reg.ledger.head,
    )


@router.post("/{mandate_id}/sign", response_model=MandateView)
def sign(mandate_id: str, body: SignRequest, reg: RegistryDep) -> MandateView:
    """Add one verified signature. The mandate waits for `certify` even once the policy is met."""
    now = reg.now(body.now)
    with reg.lock:
        mandate = reg.get(mandate_id)
        rec = reg.records[mandate_id]
        if mandate.effective_status(now) in (MandateStatus.REVOKED, MandateStatus.EXPIRED):
            raise HTTPException(status_code=409, detail=f"the mandate is {mandate.effective_status(now)}")
        if rec.expected_signers and not any(
            s.signer_id == body.signer_id and s.role is body.role for s in rec.expected_signers
        ):
            raise HTTPException(
                status_code=403, detail=f"{body.signer_id!r} is not listed to sign as {body.role.value}"
            )
        if body.signature is not None:
            assert body.key_id is not None and body.signed_at is not None and body.algorithm is not None
            sig = MandateSignature(
                person_id=body.signer_id,
                role=body.role,
                key_id=body.key_id,
                algorithm=body.algorithm,
                body_hash=body_hash(mandate),
                signed_at=body.signed_at,
                signature=body.signature,
            )
        else:
            signer = _custodial_key(reg, body, rec)
            sig = sign_mandate(mandate, signer, person_id=body.signer_id, role=body.role, now=now)
        if not verify_signature(mandate, sig, reg.keys):
            raise HTTPException(status_code=403, detail="the signature does not verify for this person")
        try:
            signed = with_signature(mandate, sig, policy=reg.policy_for(mandate), keys=reg.keys)
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        # Signing never switches a mandate on by itself: that is `certify`'s single, recorded step.
        if mandate.status is not MandateStatus.ACTIVE:
            signed = signed.model_copy(update={"status": MandateStatus.PENDING_SIGNATURES})
        reg.ledger.append("signed", signed, actor=body.signer_id, at=now)
        return _view(reg, signed)


@router.post("/{mandate_id}/certify", response_model=MandateView, status_code=201)
def certify(mandate_id: str, body: ActorRequest, reg: RegistryDep) -> MandateView:
    """Turn the mandate on (201), or 409 with every requirement still missing and whom to ask."""
    now = reg.now(body.now)
    with reg.lock:
        mandate = reg.get(mandate_id)
        rec = reg.records[mandate_id]
        status = mandate.effective_status(now)
        if status in (MandateStatus.REVOKED, MandateStatus.EXPIRED):
            raise HTTPException(status_code=409, detail={"message": f"the mandate is {status}"})
        if status is MandateStatus.ACTIVE:
            return _view(reg, mandate)
        policy = reg.policy_for(mandate)
        missing = verify_mandate(mandate, policy, reg.keys)
        if missing:
            raise HTTPException(
                status_code=409,
                detail={
                    "message": "the signatures do not yet satisfy the signature policy",
                    "missing": missing,
                    "awaiting": _awaiting(missing, rec, policy),
                },
            )
        active = mandate.model_copy(update={"status": MandateStatus.ACTIVE})
        reg.ledger.append("activated", active, actor=body.actor or "system", at=now)
        return _view(reg, active)


@router.post("/{mandate_id}/check-action", response_model=CheckActionResult)
def check(mandate_id: str, body: CheckActionRequest, reg: RegistryDep) -> CheckActionResult:
    """Would this action be allowed? Every violation is listed, from the mandate and the catalogue."""
    mandate = reg.get(mandate_id)
    rec = reg.records[mandate_id]
    catalogue = reg.catalogue.get(rec.catalogue_version) if rec.catalogue_version is not None else None
    costs = body.costs
    if costs is None:
        offer_cost, channel_cost = (
            catalogue_costs(catalogue, body.action, currency=mandate.currency) if catalogue else (0.0, 0.0)
        )
        costs = ActionCosts(offer_cost=offer_cost, channel_cost=channel_cost)
    violations = check_action(
        mandate,
        body.action,
        costs,
        body.state,
        body.now,
        spend=body.spend,
        pending_touches=body.pending_touches,
    )
    if catalogue is not None:
        violations += check_action_against_catalogue(catalogue, body.action, currency=mandate.currency)
    return CheckActionResult(
        allowed=not violations,
        violations=tuple(violations),
        mandate_id=mandate_id,
        mandate_version=mandate.version,
        catalogue_version=rec.catalogue_version,
        costs=costs,
    )


@router.post("/{mandate_id}/revoke", response_model=MandateView)
def revoke(mandate_id: str, body: RevokeRequest, reg: RegistryDep) -> MandateView:
    """Withdraw the mandate for good. Every later check fails with `mandate_not_active`."""
    now = reg.now(body.now)
    with reg.lock:
        mandate = reg.get(mandate_id)
        if mandate.status is MandateStatus.REVOKED:
            raise HTTPException(status_code=409, detail="the mandate is already revoked")
        revoked = mandate.model_copy(update={"status": MandateStatus.REVOKED})
        reg.ledger.append("revoked", revoked, actor=body.actor, at=now)
        return _view(reg, revoked)
