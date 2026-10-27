"""`/v2/onboarding`: from a connected shop's history to a running Shadow plan in one click.

| Route | What it does |
|---|---|
| `GET /v2/onboarding/proposals` | The goal proposals sized in money from the shop's own history, ranked, the best preselected (`autonomy.onboarding.scan_opportunities`) |
| `GET /v2/onboarding/limits?opportunity_id=` | The limits learned from history, "what we found -> what we will use", sized for that proposal (the preselected one by default) |
| `POST /v2/onboarding/answers` | The one question only the shop can answer (its margin); stored, and every number after uses it |
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
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import Field
from starlette.applications import Starlette

from api.v2.deps import ServiceError, Services, get_services
from autonomy.contracts import (
    AutonomyLevel,
    Brief,
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
        key: tuple[object, ...] = (int(now.timestamp() // CACHE_SECONDS), margin_rate, tuple(sorted(taken)))
        if self._cache is not None and self._cache[0] == key:
            return self._cache[1], self._cache[2]
        events = services.events.scan(since=now - timedelta(days=HISTORY_DAYS), until=now + _TICK)
        history = ShopHistory.build(events, services.states.scan(), now)
        found = scan_opportunities(
            (), (), now, margin_rate=margin_rate, taken=taken, history=history, unreachable=self.unreachable
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


class MarginAnswer(Contract):
    margin_pct: float = Field(gt=0, lt=100, description="Typical gross margin, percent of the order value")


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


def _rate(margin_pct: float | None, desk: OnboardingDesk) -> float | None:
    return round(margin_pct / 100.0, 4) if margin_pct is not None else desk.margin_rate


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
        found, _ = desk.scan(services, _rate(margin_pct, desk))
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
    rate = _rate(margin_pct, desk)
    with desk.lock:
        found, history = desk.scan(services, rate)
    proposal = None
    if opportunity_id is not None or found.preselected_id is not None:
        proposal = _pick(found, opportunity_id)
    return learn_limits(history, services.clock(), proposal=proposal, margin_rate=rate, offers_on=offers)


@router.post("/answers")
def answer(body: MarginAnswer, services: ServicesDep, desk: DeskDep) -> OpportunityScan:
    """Store the shop's margin (asked once); the proposals come back sized with it."""
    with desk.lock:
        desk.margin_rate = round(body.margin_pct / 100.0, 4)
        found, _ = desk.scan(services, desk.margin_rate)
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
            desk.margin_rate = round(body.margin_pct / 100.0, 4)
        rate = desk.margin_rate
        found, history = desk.scan(services, rate)
        proposal = _pick(found, body.opportunity_id)
        if proposal.already_running:
            raise HTTPException(status_code=409, detail="A goal of this kind is already running or paused.")
        now = services.clock()
        learned = learn_limits(history, now, proposal=proposal, margin_rate=rate, offers_on=body.offers)
        terms = learned.terms
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
