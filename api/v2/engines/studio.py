"""The Creative & Compliance Studio: assemble one customer's message and certify it, or say why not.

`POST /studio/assemble` takes an approved template (or its id in a library), one customer's facts,
the channel, the creative arm (a theme) and the country, and returns the payload that customer would
receive with an audit of every check it passed or failed:

- **rules**: the Critic's deterministic brand rules on the template (lengths, slots, offers, links,
  formats; `autonomy.agents.critic.check_rules`);
- **pack checks**: the country's compliance pack (`autonomy.compliance_packs`), on the template and on
  the filled words (India: the filled SMS must still be the registered DLT template);
- **slot check**: every `{{slot}}` is one the renderer knows and fills, and which fell back to a
  default because the facts lacked it;
- **PII leaks**: a customer fact that identifies a person (email, phone, surname, address, ids) must
  not appear in the payload, except through a slot made for it (`{{first_name}}`), and no slot may
  carry something shaped like an email address or phone number;
- **hallucination check**: every number in the words a customer reads must come from the facts,
  the offer, the brand's settings or the numbers the caller vouches for (`allowed_numbers`).

The payload is CERTIFIED only when every check passes. Rendering is `autonomy.channels.render`, the
same code the dispatcher sends with, so what the studio certifies is what would go out. No model is
called unless `judge` is set, and then the Critic's judge reviews the template (fail closed: asked
for a judge and none is configured, the payload is not certified).

Time is passed in (`now`), never read from the clock. The template library and the judge's LLM come
from dependencies (`get_template_library`, `get_judge_llm`) the app overrides; by default the
library is whatever the request carries and there is no judge.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from typing import Annotated, Literal
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, HTTPException
from pydantic import AwareDatetime, BaseModel, ConfigDict, Field

from autonomy.agents.claims import check_numbers
from autonomy.agents.creative import ALLOWED_SLOTS
from autonomy.agents.critic import AssetRef, BrandRules, Critic, check_rules
from autonomy.channels.render import (
    FALLBACK_FIRST_NAME,
    FALLBACK_RECOMMENDED_PRODUCT,
    FALLBACK_TOP_CATEGORY,
    KNOWN_SLOTS,
    SLOT_FILLERS,
    Rendered,
    RenderError,
    render,
    slots_in,
)
from autonomy.compliance_packs import (
    CombinedPack,
    CompliancePack,
    DltRegistration,
    IndiaPack,
    PackCheck,
    PackSettings,
    Purpose,
    normalise_country,
    pack_for,
)
from autonomy.contracts import (
    NO_OFFER,
    Channel,
    ContentItem,
    ContentStatus,
    CustomerState,
    Format,
    OfferKind,
    OfferOption,
    Theme,
)
from engine.llm import LLMClient

router = APIRouter(prefix="/studio", tags=["studio"])

FactValue = str | int | float | bool | None

PII_FACT_KEYS: frozenset[str] = frozenset(
    {
        "email", "email_address", "phone", "phone_number", "mobile", "msisdn", "whatsapp", "whatsapp_number",
        "last_name", "surname", "family_name", "full_name", "name",
        "address", "street", "address_line1", "address_line2", "postcode", "postal_code", "zip", "zip_code",
        "pincode", "dob", "date_of_birth", "birthday", "birth_date",
        "aadhaar", "pan", "ssn", "passport", "national_id", "tax_id", "customer_id", "external_id",
        "ip", "ip_address", "device_id",
    }
)  # fmt: skip
"""Facts that identify a person. They may shape a message (a first name) but never appear in it raw."""

SLOT_FACT_SOURCES: dict[str, frozenset[str]] = {"first_name": frozenset({"first_name", "name", "full_name"})}
"""The facts a slot is made to carry: `{{first_name}}` may show the first word of a `name`."""

_EMAIL = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
_PHONE = re.compile(r"\+?\d[\d\s().-]{7,}\d")
_URL = re.compile(r"(?:https?://|www\.)[^\s<>\"'()]+", re.IGNORECASE)
_NUMBER = re.compile(r"\d{1,3}(?:,\d{3})+(?:\.\d+)?|\d+(?:\.\d+)?")
_FALLBACKS: dict[str, str] = {
    "first_name": FALLBACK_FIRST_NAME,
    "top_category": FALLBACK_TOP_CATEGORY,
    "recommended_product": FALLBACK_RECOMMENDED_PRODUCT,
}


class StudioError(ValueError):
    """A request the studio cannot act on (no template, an unknown id); carries its HTTP status."""

    def __init__(self, message: str, status_code: int = 400) -> None:
        super().__init__(message)
        self.status_code = status_code


# --- request and response ------------------------------------------------------------------------


class AssembleRequest(BaseModel):
    """One customer's message to assemble. Give `template`, or `template_id`, or a `creative_arm`."""

    model_config = ConfigDict(extra="forbid")

    template: ContentItem | None = None
    template_id: str | None = Field(default=None, description="A content_id in the library")
    library: tuple[ContentItem, ...] = Field(
        default=(), description="Templates to pick from, alongside the server's own library"
    )
    customer_facts: dict[str, FactValue] = Field(default_factory=dict)
    channel: Channel | None = Field(default=None, description="Defaults to the template's channel")
    creative_arm: Theme | None = Field(
        default=None, description="The theme to send; picks the template when none is named"
    )
    country: str | None = Field(default=None, description="ISO code; defaults to the facts' 'country'")
    purpose: Purpose = Purpose.PROMOTIONAL
    offer: OfferOption | None = Field(default=None, description="The offer the decision granted, if any")
    allowed_domains: tuple[str, ...] = ()
    approved_assets: tuple[AssetRef, ...] = ()
    pack_settings: PackSettings = Field(default_factory=PackSettings)
    cta_url: str | None = None
    sender_id: str | None = Field(
        default=None, description="SMS header; India takes it from the DLT registration"
    )
    allowed_numbers: tuple[float, ...] = Field(
        default=(),
        description="Numbers the brand vouches for (store hours, a short code) beyond facts and offer",
    )
    now: AwareDatetime
    judge: bool = False


class PackCheckOut(BaseModel):
    pack: str
    code: str
    passed: bool
    detail: str

    @classmethod
    def of(cls, check: PackCheck) -> PackCheckOut:
        return cls(pack=check.pack, code=check.code, passed=check.passed, detail=check.detail)


class Media(BaseModel):
    kind: Literal["image", "video"]
    url: str


class RenderedPayload(BaseModel):
    header: str | None = None
    subject: str | None = None
    body: str
    cta_url: str | None = None
    media: list[Media] = Field(default_factory=list)
    template_name: str | None = None
    template_params: list[str] = Field(default_factory=list)


class SlotCheck(BaseModel):
    passed: bool
    used: list[str] = Field(default_factory=list)
    filled: dict[str, str] = Field(default_factory=dict)
    fallbacks: list[str] = Field(default_factory=list, description="Slots the facts did not cover")
    problems: list[str] = Field(default_factory=list)


class PiiLeak(BaseModel):
    fact: str | None = Field(description="The fact that leaked, or None for a PII-shaped slot value")
    slot: str | None = None
    reason: str


class HallucinationCheck(BaseModel):
    passed: bool
    numbers: list[str] = Field(default_factory=list, description="Every number the customer reads")
    unsupported: list[str] = Field(default_factory=list, description="Numbers no fact or offer backs")


class JudgeAudit(BaseModel):
    requested: bool
    ran: bool = False
    passed: bool | None = None
    notes: list[str] = Field(default_factory=list)


class CriticAudit(BaseModel):
    rule_problems: list[str] = Field(default_factory=list)
    pack: str
    pack_checks: list[PackCheckOut] = Field(default_factory=list)
    slot_check: SlotCheck
    pii_leaks: list[PiiLeak] = Field(default_factory=list)
    hallucination_check: HallucinationCheck
    judge: JudgeAudit


class AssembleResponse(BaseModel):
    status: Literal["CERTIFIED", "REJECTED"]
    content_id: str
    channel: Channel
    country: str | None
    rendered_payload: RenderedPayload | None
    critic_audit: CriticAudit
    reasons: list[str] = Field(default_factory=list, description="Why it was rejected; empty when certified")


# --- assembly ------------------------------------------------------------------------------------


def _pick_template(req: AssembleRequest, library: Sequence[ContentItem]) -> ContentItem:
    if req.template is not None:
        item = req.template
    else:
        pool = [*req.library, *library]
        if req.template_id is not None:
            found = next((t for t in pool if t.content_id == req.template_id), None)
            if found is None:
                raise StudioError(f"template {req.template_id!r} is not in the library", status_code=404)
            item = found
        elif req.creative_arm is not None:
            fitting = sorted(
                (
                    t
                    for t in pool
                    if t.theme is req.creative_arm
                    and (req.channel is None or t.channel is req.channel)
                    and t.status is not ContentStatus.REJECTED
                    and t.status is not ContentStatus.RETIRED
                    and (req.offer is None or t.offer_id == req.offer.offer_id)
                ),
                key=lambda t: (t.status is not ContentStatus.APPROVED, t.content_id),
            )
            if not fitting:
                raise StudioError(f"no template in the library for the {req.creative_arm.value} arm", 404)
            item = fitting[0]
        else:
            raise StudioError("give a template, a template_id or a creative_arm")
    if req.channel is not None and item.channel is not req.channel:
        raise StudioError(f"template {item.content_id} is for {item.channel.value}, not {req.channel.value}")
    if req.creative_arm is not None and item.theme is not req.creative_arm:
        raise StudioError(
            f"template {item.content_id} is the {item.theme.value} arm, not {req.creative_arm.value}"
        )
    return item


def _fact_text(value: FactValue) -> str:
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)


def _state_and_profile(req: AssembleRequest, country: str | None) -> tuple[CustomerState, dict[str, str]]:
    """The minimal customer state and profile `render` reads, from the facts."""
    facts = req.customer_facts
    profile = {k: _fact_text(v) for k, v in facts.items() if v is not None}
    timezone = profile.get("timezone", "UTC")
    try:
        ZoneInfo(timezone)
    except (KeyError, ValueError):
        timezone = "UTC"
    top = profile.get("top_category", "").strip()
    state = CustomerState(
        customer_id=profile.get("customer_id", "studio_preview"),
        as_of=req.now,
        timezone=timezone,
        country=country,
        top_categories=(top,) if top else (),
    )
    return state, profile


def _slot_values(
    item: ContentItem,
    state: CustomerState,
    profile: Mapping[str, str],
    offer: OfferOption | None,
    req: AssembleRequest,
) -> tuple[SlotCheck, dict[str, str]]:
    used = slots_in(f"{item.subject or ''}\n{item.body}")
    problems = [
        f"unknown slot {{{{{s}}}}}: the renderer cannot fill it" for s in used if s not in KNOWN_SLOTS
    ]
    problems += [
        f"slot {{{{{s}}}}} is not one the brand allows"
        for s in used
        if s in KNOWN_SLOTS and s != "weekday" and s not in ALLOWED_SLOTS
    ]
    filled: dict[str, str] = {}
    for slot in used:
        if slot == "weekday":
            filled[slot] = req.now.astimezone(ZoneInfo(state.timezone)).strftime("%A")
        elif slot in SLOT_FILLERS:
            try:
                filled[slot] = SLOT_FILLERS[slot](state, profile, offer)
            except RenderError as exc:
                problems.append(str(exc))
    fallbacks = [s for s, v in filled.items() if _FALLBACKS.get(s) == v]
    return (
        SlotCheck(passed=not problems, used=used, filled=filled, fallbacks=fallbacks, problems=problems),
        filled,
    )


def _pii_leaks(req: AssembleRequest, filled: Mapping[str, str], payload_text: str) -> list[PiiLeak]:
    leaks: list[PiiLeak] = []
    folded = payload_text.casefold()
    for key, raw in sorted(req.customer_facts.items(), key=lambda kv: kv[0]):
        value = _fact_text(raw).strip()
        if len(value) < 3:
            continue
        is_pii = key.lower() in PII_FACT_KEYS or bool(_EMAIL.fullmatch(value) or _PHONE.fullmatch(value))
        if not is_pii or value.casefold() not in folded:
            continue
        carried_by = [
            slot
            for slot, sources in SLOT_FACT_SOURCES.items()
            if key.lower() in sources and filled.get(slot, "").casefold() == value.casefold()
        ]
        if not carried_by:
            leaks.append(PiiLeak(fact=key, reason=f"the payload shows the customer's {key}"))
    for slot, value in filled.items():
        if _EMAIL.search(value):
            leaks.append(PiiLeak(fact=None, slot=slot, reason=f"{{{{{slot}}}}} carries an email address"))
        elif _PHONE.search(value):
            leaks.append(PiiLeak(fact=None, slot=slot, reason=f"{{{{{slot}}}}} carries a phone number"))
    return leaks


def _numbers_in(text: str) -> list[float]:
    out: list[float] = []
    for raw in _NUMBER.findall(text):
        try:
            out.append(float(raw.replace(",", "")))
        except ValueError:
            continue
    return out


def _hallucination(req: AssembleRequest, offer: OfferOption | None, readable: str) -> HallucinationCheck:
    """Every number in the words read must be backed by a fact, the offer or a vouched-for number."""
    backed: dict[str, float] = {}
    sources: list[float] = list(req.allowed_numbers)
    for key, raw in req.customer_facts.items():
        # An identifier's digits (a phone number, a PIN code, an id) back no claim in the copy.
        if isinstance(raw, bool) or raw is None or key.lower() in PII_FACT_KEYS:
            continue
        if isinstance(raw, int | float):
            sources.append(float(raw))
        else:
            sources.extend(_numbers_in(raw))
    if offer is not None and offer.kind is not OfferKind.NONE:
        sources.append(offer.amount)
        sources.extend(_numbers_in(offer.label))
    if req.pack_settings.postal_address:
        sources.extend(_numbers_in(req.pack_settings.postal_address))
    for i, value in enumerate(sources):
        backed[f"n{i}"] = value
    text = _URL.sub(" ", readable)
    numbers = [m.group(0) for m in _NUMBER.finditer(text)]
    # rel_tol 0: copy may round a value to the precision it is written in, never stand in for another.
    unsupported = check_numbers(text, backed, rel_tol=0.0)
    return HallucinationCheck(passed=not unsupported, numbers=numbers, unsupported=unsupported)


def _host_allowed(url: str, allowed: Sequence[str]) -> bool:
    host = (urlsplit(url if "://" in url else f"http://{url}").hostname or "").lower()
    return any(host == d or host.endswith(f".{d}") for d in (a.lower().strip() for a in allowed) if d)


def _dlt_registration(
    pack: CompliancePack, item: ContentItem, purpose: Purpose
) -> tuple[IndiaPack, DltRegistration] | None:
    """The SMS's DLT registration and the India pack that found it (alone or inside a combined pack)."""
    candidates = pack.packs if isinstance(pack, CombinedPack) else (pack,)
    for candidate in candidates:
        if isinstance(candidate, IndiaPack):
            reg = candidate.registration_for(item, purpose)
            if reg is not None:
                return candidate, reg
    return None


def _sender_check(
    pack: CompliancePack, item: ContentItem, purpose: Purpose, header: str | None
) -> list[PackCheck]:
    """A DLT SMS must go out under the header its template is registered with, whatever the caller asked."""
    found = _dlt_registration(pack, item, purpose)
    if found is None:
        return []
    india, reg = found
    return [
        india.check(
            "dlt:sender_header",
            (header or "").strip() == reg.header.strip(),
            f"the SMS goes out under header {header!r}, but DLT template {reg.template_id} is "
            f"registered with header {reg.header!r}",
        )
    ]


def _payload(
    rendered: Rendered, item: ContentItem, req: AssembleRequest, pack: CompliancePack, channel: Channel
) -> RenderedPayload:
    links = _URL.findall(rendered.body)
    header = req.sender_id
    if channel is Channel.SMS and header is None:
        found = _dlt_registration(pack, item, req.purpose)
        header = found[1].header if found is not None else None
    kind: Literal["image", "video"] = "video" if rendered.format is Format.VIDEO else "image"
    return RenderedPayload(
        header=header,
        subject=rendered.subject,
        body=rendered.body,
        cta_url=req.cta_url or (links[0] if links else None),
        media=[Media(kind=kind, url=u) for u in rendered.asset_urls],
        template_name=item.template_name,
        template_params=list(rendered.template_params),
    )


def assemble(
    req: AssembleRequest,
    library: Sequence[ContentItem] = (),
    judge_llm: LLMClient | None = None,
) -> AssembleResponse:
    """Render the template for this customer and certify it, or reject it with every reason."""
    item = _pick_template(req, library)
    channel = item.channel
    country = normalise_country(req.country or _fact_text(req.customer_facts.get("country")))
    offer = req.offer
    offers = (NO_OFFER,) if offer is None or offer.offer_id == NO_OFFER.offer_id else (NO_OFFER, offer)
    rules = BrandRules(
        offers=offers,
        allowed_domains=req.allowed_domains,
        approved_assets=req.approved_assets,
        purpose=req.purpose,
        pack_settings=req.pack_settings,
    )
    pack = pack_for(country, req.pack_settings)

    brand_problems = check_rules(item, rules, packs=())
    rule_problems = list(brand_problems)
    if item.status in (ContentStatus.REJECTED, ContentStatus.RETIRED):
        # The Critic (perhaps its judge) already turned this template down, or it was withdrawn;
        # re-running the cheap rules here must not quietly overturn that.
        rule_problems.append(f"template {item.content_id} is {item.status.value}; it cannot be certified")
    if req.cta_url and not _host_allowed(req.cta_url, req.allowed_domains):
        rule_problems.append(f"the call-to-action links to a domain that is not allowed: {req.cta_url}")
    checks = pack.content_checks(item, channel, req.purpose)

    state, profile = _state_and_profile(req, country)
    slot_check, filled = _slot_values(item, state, profile, offer, req)
    payload: RenderedPayload | None = None
    rendered: Rendered | None
    try:
        rendered = render(item, state, profile, offer, req.now)
    except RenderError as exc:
        slot_check = slot_check.model_copy(
            update={"passed": False, "problems": list(dict.fromkeys([*slot_check.problems, str(exc)]))}
        )
        rendered = None
    if rendered is not None:
        payload = _payload(rendered, item, req, pack, channel)
        checks.extend(pack.rendered_checks(rendered.body, item, channel, req.purpose))
        if channel is Channel.SMS:
            checks.extend(_sender_check(pack, item, req.purpose, payload.header))

    readable = f"{payload.subject or ''}\n{payload.body}" if payload is not None else ""
    payload_text = (
        "\n".join([payload.header or "", readable, payload.cta_url or "", *(m.url for m in payload.media)])
        if payload is not None
        else ""
    )
    leaks = _pii_leaks(req, filled, payload_text) if payload is not None else []
    hallucination = (
        _hallucination(req, offer, readable)
        if payload is not None
        # Nothing to read, so nothing certified; the slot check already says why.
        else HallucinationCheck(passed=False)
    )

    judge = JudgeAudit(requested=req.judge)
    if req.judge:
        if judge_llm is None:
            judge = JudgeAudit(requested=True, ran=False, passed=False, notes=["no judge is configured"])
        else:
            [reviewed] = Critic(judge_llm).review([item], rules)
            judge = JudgeAudit(
                requested=True,
                ran=not brand_problems,  # the Critic does not call its judge on rule-breaking copy
                passed=reviewed.status is ContentStatus.APPROVED,
                notes=list(reviewed.critic_notes),
            )

    reasons = [
        *(f"rule: {p}" for p in rule_problems),
        *(f"pack: {c.detail}" for c in checks if not c.passed),
        *(f"slot: {p}" for p in slot_check.problems),
        *(f"pii: {leak.reason}" for leak in leaks),
        *(f"number not backed by the facts or offer: {n}" for n in hallucination.unsupported),
        *([f"judge: {judge.notes[-1] if judge.notes else 'did not pass'}"] if judge.passed is False else []),
    ]
    return AssembleResponse(
        status="REJECTED" if reasons else "CERTIFIED",
        content_id=item.content_id,
        channel=channel,
        country=country,
        rendered_payload=payload,
        critic_audit=CriticAudit(
            rule_problems=rule_problems,
            pack=pack.name,
            pack_checks=[PackCheckOut.of(c) for c in checks],
            slot_check=slot_check,
            pii_leaks=leaks,
            hallucination_check=hallucination,
            judge=judge,
        ),
        reasons=list(dict.fromkeys(reasons)),
    )


# --- the route -----------------------------------------------------------------------------------


def get_template_library() -> Sequence[ContentItem]:
    """The server's template library for `template_id` lookups. The app overrides this dependency."""
    return ()


def get_judge_llm() -> LLMClient | None:
    """The judge's LLM for `judge: true`. None by default, so a judged request is never certified."""
    return None


@router.post("/assemble", response_model=AssembleResponse)
def assemble_route(
    req: AssembleRequest,
    library: Annotated[Sequence[ContentItem], Depends(get_template_library)],
    judge_llm: Annotated[LLMClient | None, Depends(get_judge_llm)],
) -> AssembleResponse:
    """Assemble one customer's message and certify it (200 with REJECTED when a check fails)."""
    try:
        return assemble(req, library, judge_llm)
    except StudioError as exc:
        raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc
