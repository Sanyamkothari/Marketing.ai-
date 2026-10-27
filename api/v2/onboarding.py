"""`/v2/onboarding`: from a connected shop's history to a running Shadow plan in one click.

| Route | What it does |
|---|---|
| `GET /v2/onboarding/proposals` | The goal proposals sized in money from the shop's own history, ranked, the best preselected (`autonomy.onboarding.scan_opportunities`) |
| `GET /v2/onboarding/limits?opportunity_id=` | The limits learned from history, "what we found -> what we will use", sized for that proposal (the preselected one by default) |
| `GET /v2/onboarding/margin` | The required margin question in plain words, the shop's answer so far, and a warning when it looks unusual |
| `POST /v2/onboarding/margin` | Answer it (percent, above 0 and below 100); stored as the shop's margin, which the proof counts profit with |
| `POST /v2/onboarding/answers` | The same answer, returned with the proposals sized by it |
| `POST /v2/onboarding/accept` | One call: the proposal becomes a goal, the limits a signed mandate, and the plan runs in Shadow |
| `GET /v2/onboarding/mandates` | The mandates signed here, with their body hash, what is unmet and the ledger head |

**What accept does.** It reads the history once more (the same numbers the person saw, unless the
data moved), creates the goal with the proposal's words and the learned budget, builds the mandate
from the learned terms (`mandate_from_limits`) and signs it as the owner. The goal's plan (its
brief) takes the proposal's audience and the envelope the mandate allows
(`autonomy.mandate.mandate_to_envelope`, never looser than the Strategist's own plan), and the goal
is approved at the level asked for: Shadow by default, which decides and sends nothing. A level that
sends (Approve, Autopilot) needs the mandate to be active.

**Who signs.** `single_owner=true` (the default, for a one-person shop) uses the documented
single-owner path (`autonomy.onboarding.limits.SINGLE_OWNER_POLICY`): the owner signs alone as
marketing lead, the signed note says so, and above the finance threshold a second person is still
needed. `single_owner=false` keeps the two-person rule: the owner's signature is recorded and the
mandate waits (`pending_signatures`), and the answer says who else must sign.

**Keys.** Signatures are HMAC-SHA256 (the light CI has no Ed25519 library; docs/v2/MANDATE.md has the
upgrade path), one key per person derived from `AUTONOMY_MANDATE_SECRET`. Without that variable a
random secret is drawn when the process starts, so signatures verify only while it runs, which
matches the in-memory ledger here; the answer says so. Set the variable in production.

**The margin is required.** `accept` answers 409 until the shop has answered the margin question (or set
`AUTONOMY_MARGIN_RATE`): there is no hidden default. Until it is answered, the proof shows revenue only
and says profit needs the margin (`api.v2.honest_proof`; docs/v4/ONBOARDING_MARGIN.md).

The routes are published in `docs/v2/openapi.json` (`IN_SCHEMA`), and `tests/v2/api/test_api.py`'s
route list names them.
"""

from __future__ import annotations

import hashlib
import hmac
import os
import threading
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import Field
from starlette.applications import Starlette

from api.v2.deps import ServiceError, Services, get_services
from api.v2.honest_proof import MARGIN_EXPLANATION, MARGIN_QUESTION
from autonomy.contracts import (
    AutonomyLevel,
    Brief,
    Channel,
    Contract,
    Goal,
    GoalKind,
    GoalStatus,
    OfferOption,
)
from autonomy.mandate import Mandate, MandateStatus, SignaturePolicy, SignerRole, mandate_to_envelope
from autonomy.mandate.signing import HmacSigner, KeyRing, MandateLedger, body_hash
from autonomy.onboarding import (
    SINGLE_OWNER_POLICY,
    LearnedLimits,
    OpportunityScan,
    Proposal,
    ShopHistory,
    learn_limits,
    mandate_from_limits,
    offer_for,
    scan_opportunities,
    sign_as_owner,
)

__all__ = [
    "HISTORY_DAYS",
    "IN_SCHEMA",
    "MANDATE_SECRET_ENV",
    "REFILL_UNREACHABLE",
    "AcceptRequest",
    "AcceptResult",
    "MarginAnswer",
    "MarginStatus",
    "OnboardingDesk",
    "attach_onboarding",
    "get_desk",
    "router",
]

IN_SCHEMA = True
"""Whether these routes appear in /openapi.json (see the module docstring)."""
router = APIRouter(prefix="/v2/onboarding", tags=["onboarding"], include_in_schema=IN_SCHEMA)

MANDATE_SECRET_ENV = "AUTONOMY_MANDATE_SECRET"
HISTORY_DAYS = 730
"""How far back the scan reads: the Shopify connector's order history lane."""
DESK_STATE_KEY = "v2_onboarding"
CACHE_SECONDS = 600
"""The scan is re-read at most every 10 minutes (and on a new answer or a new goal): a shop's history
does not change in minutes, and a large shop's scan takes seconds."""
_TICK = timedelta(microseconds=1)
REFILL_UNREACHABLE: Mapping[str, str] = {
    "opp_refill_due": (
        "Refill reminders are not preselected: this server does not put refill scores on each "
        "customer yet, so a refill plan would reach no one."
    )
}
"""For a deployment whose runtime has no catalogue enrichment (the production runtime today)."""


# --- state ---------------------------------------------------------------------------------------


@dataclass
class _Signed:
    mandate: Mandate
    goal_id: str
    single_owner: bool
    unmet: tuple[str, ...]


@dataclass
class OnboardingDesk:
    """What the onboarding routes keep: the shop's answer, the keys, the ledger and the last scan."""

    secret: bytes
    persistent_key: bool
    keys: KeyRing = field(default_factory=KeyRing)
    ledger: MandateLedger = field(default_factory=MandateLedger)
    signers: dict[str, HmacSigner] = field(default_factory=dict)
    signed: dict[str, _Signed] = field(default_factory=dict)
    margin_rate: float | None = None
    unreachable: dict[str, str] = field(default_factory=dict)
    """Opportunity id -> why this deployment cannot reach its audience yet: shown, never preselected
    (`REFILL_UNREACHABLE` where the runtime puts no refill scores on customers)."""
    _cache: tuple[tuple[object, ...], OpportunityScan, ShopHistory] | None = None
    lock: threading.Lock = field(default_factory=threading.Lock, repr=False, compare=False)

    @classmethod
    def from_env(
        cls, environ: Mapping[str, str] | None = None, *, unreachable: Mapping[str, str] | None = None
    ) -> OnboardingDesk:
        env = os.environ if environ is None else environ
        given = (env.get(MANDATE_SECRET_ENV) or "").strip()
        blocked = dict(unreachable or {})
        if given:
            return cls(secret=given.encode(), persistent_key=True, unreachable=blocked)
        return cls(secret=os.urandom(32), persistent_key=False, unreachable=blocked)

    def signer_for(self, person_id: str) -> HmacSigner:
        """This person's key (derived from the desk's secret), registered as marketing lead."""
        if person_id not in self.signers:
            key_id = f"onboarding:{person_id}"
            secret = hmac.new(self.secret, person_id.encode(), hashlib.sha256).digest()
            self.keys.add_hmac(key_id, secret, person_id=person_id, roles=(SignerRole.MARKETING_LEAD,))
            self.signers[person_id] = HmacSigner(key_id, secret)
        return self.signers[person_id]

    def scan(self, services: Services, margin_rate: float | None) -> tuple[OpportunityScan, ShopHistory]:
        """The proposals for the shop as it is now (cached for `CACHE_SECONDS`)."""
        now = services.clock()
        # Running or paused goals hold their kind (a paused one is never bypassed by a new goal); a
        # draft nobody approved does not, so it never blocks the one-click accept as "already running".
        taken = frozenset(
            g.kind for g in services.workspace.goals() if g.status in (GoalStatus.ACTIVE, GoalStatus.PAUSED)
        )
        # The shop's own prices of one message, in its currency (docs/v3/CURRENCY.md); none: the documented
        # dollar-sized defaults (the demo and the fakes have no prices).
        prices = services.shop_prices()
        money: dict[str, Any] = (
            {} if prices is None else {"channel_costs": prices.costs(), "currency": prices.shop_currency()}
        )
        key: tuple[object, ...] = (
            int(now.timestamp() // CACHE_SECONDS),
            margin_rate,
            tuple(sorted(taken)),
            prices.money_key() if prices is not None else None,
        )
        if self._cache is not None and self._cache[0] == key:
            return self._cache[1], self._cache[2]
        events = services.events.scan(since=now - timedelta(days=HISTORY_DAYS), until=now + _TICK)
        history = ShopHistory.build(events, services.states.scan(), now)
        found = scan_opportunities(
            (),
            (),
            now,
            margin_rate=margin_rate,
            taken=taken,
            history=history,
            unreachable=self.unreachable,
            **money,
        )
        self._cache = (key, found, history)
        return found, history


def attach_onboarding(app: Starlette, desk: OnboardingDesk) -> None:
    setattr(app.state, DESK_STATE_KEY, desk)


def get_desk(request: Request) -> OnboardingDesk:
    desk = getattr(request.app.state, DESK_STATE_KEY, None)
    if not isinstance(desk, OnboardingDesk):
        desk = OnboardingDesk.from_env()
        attach_onboarding(request.app, desk)
    return desk


DeskDep = Annotated[OnboardingDesk, Depends(get_desk)]
ServicesDep = Annotated[Services, Depends(get_services)]


# --- bodies --------------------------------------------------------------------------------------


MARGIN_WARN_BELOW_PCT = 5.0
"""An answer under this is probably a fraction typed as a percent (0.4 for 40%) or a mistake: warned, kept."""
MARGIN_WARN_ABOVE_PCT = 80.0
"""An answer over this is rare outside digital goods: it is warned about, and kept."""


class MarginAnswer(Contract):
    margin_pct: float = Field(
        gt=0,
        lt=100,
        description="Out of every 100 of sales, how much is left after product cost, shipping and payment fees",
    )


class MarginStatus(Contract):
    """The margin question, the shop's answer so far, and what to say about it."""

    question: str = MARGIN_QUESTION
    explanation: str = MARGIN_EXPLANATION
    answered: bool
    margin_pct: float | None = Field(default=None, description="The answer, 0-100; None until answered")
    margin_rate: float | None = Field(default=None, description="The same as stored: 0-1")
    source: str = Field(description="`your answer`, `settings` (AUTONOMY_MARGIN_RATE) or `not set`")
    min_pct: float = 0.0
    max_pct: float = 100.0
    warn_below_pct: float = MARGIN_WARN_BELOW_PCT
    warn_above_pct: float = MARGIN_WARN_ABOVE_PCT
    warning: str | None = None
    note: str = Field(description="What the answer changes, or what is missing until it is given")


class AcceptRequest(Contract):
    opportunity_id: str | None = Field(default=None, description="Left out: the preselected proposal")
    margin_pct: float | None = Field(
        default=None, gt=0, lt=100, description="The shop's margin, if not given before"
    )
    owner_id: str = Field(default="owner", min_length=1, max_length=200)
    single_owner: bool = Field(
        default=True, description="A one-person shop: the documented single-owner path"
    )
    offers: bool = Field(default=False, description="Allow offers up to the shop's usual discount depth")
    # Foundation fix 5 (docs/v3/OFFER_CODES.md): the shop's code(s) for that offer, so its redemptions
    # are charged to the budget. Left out, they are listed as unattributed discounts instead.
    offer_codes: tuple[str, ...] = Field(
        default=(), description="With offers: the shop's discount code(s) for the offer, e.g. WELCOME10"
    )
    offer_code_prefixes: tuple[str, ...] = Field(
        default=(), description="With offers: prefixes of the per-customer unique codes for the offer"
    )
    autonomy: AutonomyLevel = AutonomyLevel.SHADOW


class MandateRecord(Contract):
    mandate: Mandate
    goal_id: str
    body_hash: str
    unmet: tuple[str, ...]
    single_owner: bool
    ledger_head: str


class AcceptResult(Contract):
    goal: Goal
    brief: Brief
    proposal: Proposal
    limits: LearnedLimits
    mandate: MandateRecord
    words: str
    notes: tuple[str, ...] = ()


_UNMET_WORDS: dict[str, str] = {
    "missing_role:marketing_lead": "the marketing lead's signature",
    "missing_role:finance": "a finance signer (the budget is above the finance threshold)",
    "two_person_rule": "a second person's signature",
}

# --- helpers -------------------------------------------------------------------------------------


def _shop_costs(services: Services) -> dict[Channel, float] | None:
    """The shop's price of one message per channel in its currency; None: the documented defaults."""
    prices = services.shop_prices()
    return prices.costs() if prices is not None else None


def _rate(margin_pct: float | None, desk: OnboardingDesk, services: Services) -> float | None:
    """The margin to size with: the one given now, else the desk's answer, else the setting."""
    if margin_pct is not None:
        return round(margin_pct / 100.0, 4)
    return desk.margin_rate if desk.margin_rate is not None else services.margin_rate


def _margin_warning(margin_pct: float) -> str | None:
    if margin_pct < MARGIN_WARN_BELOW_PCT:
        return (
            f"{margin_pct:g}% is very low. If you meant {margin_pct * 100:g}%, type it as {margin_pct * 100:g}, "
            "not as a fraction. Keep it only if only that much is left of each sale."
        )
    if margin_pct > MARGIN_WARN_ABOVE_PCT:
        return (
            f"{margin_pct:g}% is very high. After product cost, shipping and payment fees most shops keep "
            "much less; a margin set too high makes the proof show more profit than there is."
        )
    return None


def _store_margin(desk: OnboardingDesk, services: Services, margin_pct: float) -> float:
    """Keep the answer on the desk and on the services (the proof reads it from there); returns the 0-1 rate."""
    rate = round(margin_pct / 100.0, 4)
    if not 0.0 < rate < 1.0:
        raise HTTPException(status_code=422, detail="The margin must be above 0 and below 100 (percent).")
    desk.margin_rate = rate
    services.set_margin_rate(rate)
    return rate


def _margin_status(desk: OnboardingDesk, services: Services) -> MarginStatus:
    rate = desk.margin_rate if desk.margin_rate is not None else services.margin_rate
    if rate is None:
        return MarginStatus(
            answered=False,
            source="not set",
            note="Not answered yet. Until it is, the proof shows revenue only and says profit needs your margin.",
        )
    pct = round(rate * 100.0, 2)
    return MarginStatus(
        answered=True,
        margin_pct=pct,
        margin_rate=rate,
        source="your answer" if desk.margin_rate is not None else "settings",
        warning=_margin_warning(pct),
        note="The proof counts every purchase that carries no margin of its own at this margin. "
        "Set AUTONOMY_MARGIN_RATE to keep it after a restart.",
    )


def _pick(found: OpportunityScan, opportunity_id: str | None) -> Proposal:
    wanted = opportunity_id or found.preselected_id
    if wanted is None:
        raise HTTPException(
            status_code=409,
            detail=found.not_enough or "No proposal is ready yet; pick one by its opportunity_id.",
        )
    proposal = next((p for p in found.proposals if p.opportunity_id == wanted), None)
    if proposal is None:
        raise HTTPException(status_code=404, detail=f"no proposal {wanted!r}")
    return proposal


def _record(desk: OnboardingDesk, signed: _Signed) -> MandateRecord:
    return MandateRecord(
        mandate=signed.mandate,
        goal_id=signed.goal_id,
        body_hash=body_hash(signed.mandate),
        unmet=signed.unmet,
        single_owner=signed.single_owner,
        ledger_head=desk.ledger.head,
    )


def _plan(brief: Brief, proposal: Proposal, mandate: Mandate, offers: tuple[OfferOption, ...]) -> Brief:
    """The goal's plan inside the mandate: the proposal's audience, the envelope the mandate allows."""
    shared = tuple(c for c in brief.envelope.channels if c in mandate.channels)
    base = brief.envelope.model_copy(
        update={
            "channels": shared or mandate.channels,
            "offers": (*brief.envelope.offers, *(o for o in offers if o not in brief.envelope.offers)),
        }
    )
    envelope = mandate_to_envelope(mandate, offers, base=base)
    rationale = f"{proposal.why} Limits learned from your history and signed as the mandate."
    return brief.model_copy(
        update={
            "audience": proposal.audience,
            "envelope": envelope,
            "rationale": rationale,
            "created_by": f"onboarding ({brief.created_by})",
        }
    )


def _messages_in_approved_voice(request: Request, goal_id: str) -> list[str]:
    """When the shop's voice is approved already, write the new goal's messages in it now (the brand
    step's `write_goal_libraries`); otherwise approving the voice writes them."""
    from api.v2.onboarding_brand import approved_kit, write_goal_libraries

    found = approved_kit(request)
    if found is None:
        return ["Approve the voice in the next step and the plan gets its messages."]
    brand, kit = found
    return write_goal_libraries(request.app.state, brand, kit, goal_ids=(goal_id,))


# --- routes --------------------------------------------------------------------------------------


@router.get("/proposals")
def proposals(
    services: ServicesDep,
    desk: DeskDep,
    margin_pct: Annotated[float | None, Query(gt=0, lt=100)] = None,
) -> OpportunityScan:
    """Goal proposals sized in money from the shop's own history, best first, one preselected."""
    with desk.lock:
        found, _ = desk.scan(services, _rate(margin_pct, desk, services))
    return found


@router.get("/limits")
def limits(
    services: ServicesDep,
    desk: DeskDep,
    opportunity_id: str | None = None,
    offers: bool = False,
    margin_pct: Annotated[float | None, Query(gt=0, lt=100)] = None,
) -> LearnedLimits:
    """The limits learned from history, as found -> used, sized for one proposal."""
    rate = _rate(margin_pct, desk, services)
    with desk.lock:
        found, history = desk.scan(services, rate)
    proposal = None
    if opportunity_id is not None or found.preselected_id is not None:
        proposal = _pick(found, opportunity_id)
    return learn_limits(
        history,
        services.clock(),
        proposal=proposal,
        margin_rate=rate,
        offers_on=offers,
        channel_costs=_shop_costs(services),
    )


@router.get("/margin")
def get_margin(services: ServicesDep, desk: DeskDep) -> MarginStatus:
    """The required margin question, the answer so far (if any) and a warning when it looks unusual."""
    with desk.lock:
        return _margin_status(desk, services)


@router.post("/margin")
def set_margin(body: MarginAnswer, services: ServicesDep, desk: DeskDep) -> MarginStatus:
    """Answer the margin question. 0 < margin < 100 (else 422); under 5 or over 80 is kept with a warning."""
    with desk.lock:
        _store_margin(desk, services, body.margin_pct)
        return _margin_status(desk, services)


@router.post("/answers")
def answer(body: MarginAnswer, services: ServicesDep, desk: DeskDep) -> OpportunityScan:
    """Store the shop's margin (asked once); the proposals come back sized with it."""
    with desk.lock:
        rate = _store_margin(desk, services, body.margin_pct)
        found, _ = desk.scan(services, rate)
    return found


@router.get("/mandates")
def mandates(desk: DeskDep) -> list[MandateRecord]:
    """The mandates signed here, newest first."""
    with desk.lock:
        rows = sorted(
            desk.signed.values(), key=lambda s: (s.mandate.valid_from, s.mandate.mandate_id), reverse=True
        )
        return [_record(desk, s) for s in rows]


@router.post("/accept")
def accept(body: AcceptRequest, request: Request, services: ServicesDep, desk: DeskDep) -> AcceptResult:
    """One click: the chosen proposal becomes a goal, the learned limits a signed mandate, and the plan
    runs at the level asked for (Shadow by default)."""
    with desk.lock:
        if body.margin_pct is not None:
            _store_margin(desk, services, body.margin_pct)
        rate = _rate(None, desk, services)
        if rate is None:  # no hidden default: the proof's profit rests on it (docs/v4/ONBOARDING_MARGIN.md)
            raise HTTPException(
                status_code=409,
                detail=f"Answer this first: {MARGIN_QUESTION} (margin_pct, above 0 and below 100). "
                "Nothing was written.",
            )
        found, history = desk.scan(services, rate)
        proposal = _pick(found, body.opportunity_id)
        if proposal.already_running:
            raise HTTPException(status_code=409, detail="A goal of this kind is already running or paused.")
        now = services.clock()
        learned = learn_limits(
            history,
            now,
            proposal=proposal,
            margin_rate=rate,
            offers_on=body.offers,
            channel_costs=_shop_costs(services),
        )
        terms = learned.terms
        try:  # refused before a goal is stored or a mandate signed, so a retry leaves nothing behind
            services.check_new_goal_prices(terms.currency, terms.channels)
        except ServiceError as exc:
            raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc
        offers: tuple[OfferOption, ...] = ()
        if terms.offers_on and terms.max_offer_pct is not None:
            try:
                offer = offer_for(
                    terms.max_offer_pct,
                    history.mean_order_value() or 0.0,
                    codes=body.offer_codes,
                    code_prefixes=body.offer_code_prefixes,
                )
            except ValueError as exc:  # a code the checkout could never report (empty, a comma, "none")
                raise HTTPException(status_code=422, detail=str(exc)) from exc
            offers = (offer,)
        try:
            goal, brief = services.create_goal(
                proposal.goal_text, terms.budget_ceiling, terms.currency, proposal.kind
            )
        except ServiceError as exc:
            raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc
        templates = tuple(c.content_id for c in services.workspace.content(goal_id=goal.goal_id))
        draft = mandate_from_limits(
            terms,
            business_id=services.business_id,
            goal_ids=(goal.goal_id,),
            now=now,
            created_by=body.owner_id,
            offers=offers,
            template_ids=templates,
            single_owner=body.single_owner,
        )
        policy = SINGLE_OWNER_POLICY if body.single_owner else SignaturePolicy()
        desk.ledger.append("created", draft, actor=body.owner_id, at=now)
        signed, unmet = sign_as_owner(
            draft, desk.signer_for(body.owner_id), desk.keys, owner_id=body.owner_id, policy=policy, now=now
        )
        desk.ledger.append("signed", signed, actor=body.owner_id, at=now)
        if signed.status is MandateStatus.ACTIVE:
            desk.ledger.append("activated", signed, actor=body.owner_id, at=now)
        record = _Signed(signed, goal.goal_id, body.single_owner, tuple(unmet))
        desk.signed[signed.mandate_id] = record
        plan = _plan(brief, proposal, signed, offers)
        services.workspace.put_brief(plan)
        level = body.autonomy
        notes: list[str] = list(learned.notes)
        if level is not AutonomyLevel.SHADOW and signed.status is not MandateStatus.ACTIVE:
            notes.append(
                f"{level.value.title()} needs an active mandate; the plan runs in Shadow until it is signed."
            )
            level = AutonomyLevel.SHADOW
        try:
            goal, plan = services.approve(goal.goal_id, level)
        except ServiceError as exc:
            raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc
        if not desk.persistent_key:
            notes.append(
                f"Signed with a key that lasts while this server runs; set {MANDATE_SECRET_ENV} to keep "
                "signatures checkable after a restart."
            )
        notes.extend(_messages_in_approved_voice(request, goal.goal_id))
        if not templates:
            notes.append(
                "The mandate lists no message templates (none existed when it was signed); the messages "
                "come from the voice approved in the brand step and pass its rules."
            )
        if proposal.kind is GoalKind.REPEAT_PURCHASE and proposal.opportunity_id == "opp_refill_due":
            notes.append(
                "Refill audiences read refill scores on each customer; they need the catalogue enrichment."
            )
        if unmet:
            who = "waiting for " + " and ".join(_UNMET_WORDS.get(code, code) for code in unmet)
        else:
            who = "signed by you alone (single-owner path)" if body.single_owner else "signed"
        mode = (
            "decides every day and sends nothing"
            if level is AutonomyLevel.SHADOW
            else f"runs in {level.value}"
        )
        words = f"{proposal.title} is running: it {mode}. Limits {who}."
        return AcceptResult(
            goal=goal,
            brief=plan,
            proposal=proposal,
            limits=learned,
            mandate=_record(desk, record),
            words=words,
            notes=tuple(notes),
        )
