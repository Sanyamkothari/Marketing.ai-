"""Mandate core: the two-person rule, signatures, the ledger chain, every violation code, the envelope
projection and the spend pacer."""

from __future__ import annotations

import itertools
from datetime import UTC, datetime, timedelta

import pytest
from pydantic import ValidationError

from autonomy.contracts import (
    NO_OFFER,
    Action,
    AudienceFilter,
    Bid,
    Channel,
    ChannelStats,
    CustomerState,
    Envelope,
    OfferKind,
    OfferOption,
)
from autonomy.decide import rules
from autonomy.mandate import (
    ALL_CODES,
    ActionCosts,
    FrequencyCap,
    HmacSigner,
    KeyRing,
    Mandate,
    MandateLedger,
    MandateStatus,
    QuietHours,
    SignaturePolicy,
    SignerRole,
    SpendToDate,
    TokenBucket,
    body_hash,
    check_action,
    check_bid,
    enforce,
    mandate_to_envelope,
    sign_mandate,
    verify_mandate,
    verify_signature,
    with_signature,
)
from autonomy.mandate.models import MISSING_FINANCE, MISSING_MARKETING_LEAD, TWO_PERSON_RULE
from autonomy.mandate.signing import BAD_SIGNATURE, BODY_HASH_MISMATCH, Ed25519Signer, ed25519_available

NOW = datetime(2026, 10, 20, 14, 0, tzinfo=UTC)
POLICY = SignaturePolicy(finance_threshold=10_000)

ALICE = HmacSigner("k_alice", b"alice-secret-0123456789")
BOB = HmacSigner("k_bob", b"bob-secret-0123456789abc")
CAROL = HmacSigner("k_carol", b"carol-secret-0123456789")

TEN_OFF = OfferOption(
    offer_id="pct10", label="10% off", kind=OfferKind.PERCENT_OFF, amount=10, cost_per_redemption=6.0
)
FREE_SHIP = OfferOption(
    offer_id="freeship", label="Free shipping", kind=OfferKind.FREE_SHIPPING, cost_per_redemption=4.0
)


def keys() -> KeyRing:
    ring = KeyRing()
    ring.add_hmac("k_alice", b"alice-secret-0123456789", person_id="alice", roles=[SignerRole.MARKETING_LEAD])
    ring.add_hmac("k_bob", b"bob-secret-0123456789abc", person_id="bob", roles=[SignerRole.CFO])
    ring.add_hmac(
        "k_carol",
        b"carol-secret-0123456789",
        person_id="carol",
        roles=[SignerRole.APPROVER, SignerRole.MARKETING_LEAD],
    )
    return ring


def mandate(**over: object) -> Mandate:
    fields: dict[str, object] = {
        "mandate_id": "man_1",
        "business_id": "demo",
        "goal_ids": ("goal_winback",),
        "budget_ceiling": 5000.0,
        "max_daily_spend": 400.0,
        "margin_floor_pct": 0.2,
        "quiet_hours": QuietHours(start=21, end=9),
        "frequency_cap": FrequencyCap(max_touches=2, window_days=7),
        "channels": (Channel.EMAIL, Channel.WHATSAPP),
        "approved_template_ids": ("c_email_1", "c_wa_1"),
        "approved_offer_ids": ("pct10",),
        "cohort_exclusions": (AudienceFilter(field="segment", op="eq", value="vip"),),
        "valid_from": NOW - timedelta(days=1),
        "valid_until": NOW + timedelta(days=30),
        "status": MandateStatus.ACTIVE,
    }
    fields.update(over)
    return Mandate.model_validate(fields)


def signed(m: Mandate, *pairs: tuple[HmacSigner, str, SignerRole]) -> Mandate:
    ring = keys()
    out = m.model_copy(update={"status": MandateStatus.PENDING_SIGNATURES, "signers": ()})
    for signer, person, role in pairs:
        out = with_signature(
            out, sign_mandate(out, signer, person_id=person, role=role, now=NOW), policy=POLICY, keys=ring
        )
    return out


def state(**over: object) -> CustomerState:
    fields: dict[str, object] = {
        "customer_id": "c1",
        "as_of": NOW,
        "timezone": "UTC",
        "purchases": 2,
        "revenue": 200.0,
        "channels": {Channel.EMAIL: ChannelStats(address_known=True, consent=True)},
    }
    fields.update(over)
    return CustomerState.model_validate(fields)


def action(**over: object) -> Action:
    fields: dict[str, object] = {
        "goal_id": "goal_winback",
        "channel": Channel.EMAIL,
        "content_id": "c_email_1",
        "send_at": NOW,
    }
    fields.update(over)
    return Action.model_validate(fields)


GOOD_COSTS = ActionCosts(
    offer_cost=6.0, channel_cost=0.01, revenue_if_converted=100.0, margin_if_converted=40.0
)


# --- the two-person rule and the signature policy -----------------------------------------------------


def test_small_mandate_needs_lead_and_a_second_person() -> None:
    m = mandate(status=MandateStatus.DRAFT)
    lead_only = signed(m, (ALICE, "alice", SignerRole.MARKETING_LEAD))
    assert lead_only.status is MandateStatus.PENDING_SIGNATURES
    assert verify_mandate(lead_only, POLICY, keys()) == [TWO_PERSON_RULE]

    both = signed(m, (ALICE, "alice", SignerRole.MARKETING_LEAD), (CAROL, "carol", SignerRole.APPROVER))
    assert both.status is MandateStatus.ACTIVE
    assert verify_mandate(both, POLICY, keys()) == []


def test_no_marketing_lead_is_reported() -> None:
    m = signed(mandate(), (BOB, "bob", SignerRole.CFO), (CAROL, "carol", SignerRole.APPROVER))
    assert m.status is MandateStatus.PENDING_SIGNATURES
    assert MISSING_MARKETING_LEAD in verify_mandate(m, POLICY, keys())


def test_large_budget_needs_finance_from_a_different_person() -> None:
    big = mandate(budget_ceiling=50_000.0, status=MandateStatus.DRAFT)
    no_finance = signed(
        big, (ALICE, "alice", SignerRole.MARKETING_LEAD), (CAROL, "carol", SignerRole.APPROVER)
    )
    assert verify_mandate(no_finance, POLICY, keys()) == [MISSING_FINANCE]
    ok = signed(big, (ALICE, "alice", SignerRole.MARKETING_LEAD), (BOB, "bob", SignerRole.CFO))
    assert ok.status is MandateStatus.ACTIVE


def test_one_person_cannot_fill_two_roles() -> None:
    ring = KeyRing()
    ring.add_hmac(
        "k_dave",
        b"dave-secret-0123456789",
        person_id="dave",
        roles=[SignerRole.MARKETING_LEAD, SignerRole.CFO],
    )
    dave = HmacSigner("k_dave", b"dave-secret-0123456789")
    big = mandate(budget_ceiling=50_000.0)
    m = big
    for role in (SignerRole.MARKETING_LEAD, SignerRole.CFO):
        m = with_signature(
            m, sign_mandate(m, dave, person_id="dave", role=role, now=NOW), policy=POLICY, keys=ring
        )
    assert m.status is MandateStatus.PENDING_SIGNATURES
    assert verify_mandate(m, POLICY, ring) == [TWO_PERSON_RULE]


def test_a_key_cannot_sign_for_someone_else_or_another_role() -> None:
    m = mandate()
    as_bob = sign_mandate(m, ALICE, person_id="bob", role=SignerRole.MARKETING_LEAD, now=NOW)
    assert not verify_signature(m, as_bob, keys())
    as_cfo = sign_mandate(m, ALICE, person_id="alice", role=SignerRole.CFO, now=NOW)
    assert not verify_signature(m, as_cfo, keys())
    with pytest.raises(ValueError, match="does not verify"):
        with_signature(m, as_cfo, policy=POLICY, keys=keys())


# --- tamper detection -----------------------------------------------------------------------------------


def test_changing_any_limit_breaks_the_signatures() -> None:
    m = signed(mandate(), (ALICE, "alice", SignerRole.MARKETING_LEAD), (CAROL, "carol", SignerRole.APPROVER))
    assert verify_mandate(m, POLICY, keys()) == []
    for change in (
        {"budget_ceiling": 9000.0},
        {"channels": (Channel.EMAIL, Channel.SMS)},
        {"valid_until": NOW + timedelta(days=90)},
        {"margin_floor_pct": 0.0},
    ):
        tampered = m.model_copy(update=change)
        codes = verify_mandate(tampered, POLICY, keys())
        assert BODY_HASH_MISMATCH in codes and TWO_PERSON_RULE in codes


def test_forged_signature_bytes_fail() -> None:
    m = signed(mandate(), (ALICE, "alice", SignerRole.MARKETING_LEAD), (CAROL, "carol", SignerRole.APPROVER))
    first = m.signers[0]
    forged = first.model_copy(update={"signature": "0" * len(first.signature)})
    bad = m.model_copy(update={"signers": (forged, m.signers[1])})
    assert BAD_SIGNATURE in verify_mandate(bad, POLICY, keys())


def test_status_is_not_part_of_the_body_but_times_are_normalised() -> None:
    m = mandate()
    assert body_hash(m) == body_hash(m.model_copy(update={"status": MandateStatus.REVOKED}))
    from zoneinfo import ZoneInfo

    same_moment = mandate(valid_from=(NOW - timedelta(days=1)).astimezone(ZoneInfo("Asia/Kolkata")))
    assert body_hash(same_moment) == body_hash(m)


@pytest.mark.skipif(not ed25519_available(), reason="cryptography not installed")
def test_ed25519_signatures_verify_with_only_the_public_key() -> None:
    signer = Ed25519Signer("k_ed", bytes(range(32)))
    ring = KeyRing()
    ring.add_ed25519("k_ed", signer.public_key_bytes(), person_id="erin", roles=[SignerRole.MARKETING_LEAD])
    m = mandate()
    sig = sign_mandate(m, signer, person_id="erin", role=SignerRole.MARKETING_LEAD, now=NOW)
    assert verify_signature(m, sig, ring)
    assert not verify_signature(m.model_copy(update={"budget_ceiling": 1.0}), sig, ring)


# --- the ledger -----------------------------------------------------------------------------------------


def _ledger() -> MandateLedger:
    ledger = MandateLedger()
    draft = mandate(status=MandateStatus.DRAFT)
    ledger.append("created", draft, actor="alice", at=NOW)
    active = signed(draft, (ALICE, "alice", SignerRole.MARKETING_LEAD), (CAROL, "carol", SignerRole.APPROVER))
    ledger.append("activated", active, actor="carol", at=NOW + timedelta(minutes=5))
    v2 = signed(
        mandate(version=2, budget_ceiling=8000.0, status=MandateStatus.DRAFT),
        (ALICE, "alice", SignerRole.MARKETING_LEAD),
        (CAROL, "carol", SignerRole.APPROVER),
    )
    ledger.append("activated", v2, actor="carol", at=NOW + timedelta(days=1))
    return ledger


def test_chain_verifies_and_links() -> None:
    ledger = _ledger()
    report = ledger.verify_chain(keys())
    assert report.ok and report.entries == 3
    entries = ledger.entries()
    assert entries[0].prev_hash == "0" * 64
    assert all(b.prev_hash == a.entry_hash for a, b in itertools.pairwise(entries))
    latest = ledger.latest("man_1")
    assert latest is not None and latest.version == 2
    assert ledger.head == entries[-1].entry_hash


def test_editing_an_entry_breaks_the_chain() -> None:
    entries = _ledger().entries()
    edited = entries[1].mandate.model_copy(update={"budget_ceiling": 1_000_000.0})
    entries[1] = entries[1].model_copy(update={"mandate": edited})
    report = MandateLedger(entries).verify_chain()
    assert not report.ok and report.first_bad_seq == 2 and report.problem == "hash_mismatch"


def test_rewritten_hash_is_caught_by_the_next_link() -> None:
    from autonomy.mandate.signing import compute_entry_hash

    entries = _ledger().entries()
    e = entries[0]
    rewritten = e.model_copy(update={"actor": "mallory"})
    rewritten = rewritten.model_copy(
        update={
            "entry_hash": compute_entry_hash(
                prev_hash=e.prev_hash,
                seq=e.seq,
                kind=e.kind,
                actor="mallory",
                at=e.at,
                mandate_id=e.mandate_id,
                version=e.version,
                body_hash_hex=e.body_hash,
                mandate=e.mandate,
            )
        }
    )
    entries[0] = rewritten
    report = MandateLedger(entries).verify_chain()
    assert not report.ok and report.first_bad_seq == 2 and report.problem == "chain_broken"


def test_dropped_entry_and_bad_signature_are_caught() -> None:
    entries = _ledger().entries()
    assert MandateLedger([entries[0], entries[2]]).verify_chain().problem == "seq_gap"
    other_keys = KeyRing()
    report = MandateLedger(entries).verify_chain(other_keys)
    assert not report.ok and report.problem == BAD_SIGNATURE and report.first_bad_seq == 2


def test_changed_body_needs_a_new_version() -> None:
    ledger = MandateLedger()
    ledger.append("created", mandate(), actor="alice", at=NOW)
    with pytest.raises(ValueError, match="higher version"):
        ledger.append("created", mandate(budget_ceiling=1000.0), actor="alice", at=NOW)
    ledger.append("revoked", mandate(status=MandateStatus.REVOKED), actor="alice", at=NOW)
    assert ledger.verify_chain().ok


# --- enforcement: each violation code ---------------------------------------------------------------------


def test_a_clean_action_passes() -> None:
    assert check_action(mandate(), action(offer_id="pct10"), GOOD_COSTS, state(), NOW) == []


@pytest.mark.parametrize(
    ("over_mandate", "over_action", "costs", "over_state", "spend", "code"),
    [
        ({"status": MandateStatus.PENDING_SIGNATURES}, {}, None, {}, None, enforce.NOT_ACTIVE),
        ({"status": MandateStatus.REVOKED}, {}, None, {}, None, enforce.NOT_ACTIVE),
        ({}, {"send_at": NOW + timedelta(days=31)}, None, {}, None, enforce.OUTSIDE_VALIDITY),
        ({}, {"goal_id": "goal_other"}, None, {}, None, enforce.GOAL_NOT_COVERED),
        ({}, {"channel": Channel.SMS}, None, {}, None, enforce.CHANNEL_NOT_ALLOWED),
        ({}, {"content_id": "c_rogue"}, None, {}, None, enforce.TEMPLATE_NOT_APPROVED),
        ({}, {"content_id": None}, None, {}, None, enforce.TEMPLATE_NOT_APPROVED),
        ({}, {"offer_id": "freeship"}, None, {}, None, enforce.OFFER_NOT_APPROVED),
        ({}, {"send_at": NOW.replace(hour=23)}, None, {}, None, enforce.QUIET_HOURS),
        ({}, {}, None, {"contacts_7d": 2}, None, enforce.FREQUENCY_CAP),
        (
            {},
            {"offer_id": "pct10"},
            ActionCosts(offer_cost=30.0, revenue_if_converted=100.0, margin_if_converted=40.0),
            {},
            None,
            enforce.MARGIN_FLOOR,
        ),
        ({}, {}, None, {}, SpendToDate(total=4999.995, today=0.0), enforce.BUDGET_CEILING),
        ({}, {}, None, {}, SpendToDate(total=100.0, today=399.995), enforce.DAILY_SPEND),
        ({}, {}, None, {"segment": "vip"}, None, enforce.COHORT_EXCLUDED),
    ],
)
def test_each_violation_code(
    over_mandate: dict[str, object],
    over_action: dict[str, object],
    costs: ActionCosts | None,
    over_state: dict[str, object],
    spend: SpendToDate | None,
    code: str,
) -> None:
    codes = check_action(
        mandate(**over_mandate),
        action(**over_action),
        costs or GOOD_COSTS,
        state(**over_state),
        NOW,
        spend=spend,
    )
    assert codes == [code]


def test_every_code_is_covered_and_listed() -> None:
    assert len(set(ALL_CODES)) == len(ALL_CODES) == 12
    assert all(c.startswith("mandate_") for c in ALL_CODES)


def test_expired_by_time_and_all_violations_reported_together() -> None:
    later = NOW + timedelta(days=40)
    codes = check_action(mandate(), action(send_at=later, channel=Channel.SMS), GOOD_COSTS, state(), later)
    assert (
        codes[:2] == [enforce.NOT_ACTIVE, enforce.OUTSIDE_VALIDITY] and enforce.CHANNEL_NOT_ALLOWED in codes
    )


def test_quiet_hours_use_the_customer_zone() -> None:
    m = mandate()
    # 14:00 UTC is 23:00 in Tokyo and 07:00 in Los Angeles: both quiet; 10:00 in New York is not.
    for tz, quiet in (("Asia/Tokyo", True), ("America/Los_Angeles", True), ("America/New_York", False)):
        codes = check_action(m, action(), GOOD_COSTS, state(timezone=tz), NOW)
        assert (enforce.QUIET_HOURS in codes) is quiet, tz
    # A broken zone falls back to the mandate's default zone.
    m_tokyo = mandate(quiet_hours=QuietHours(start=21, end=9, timezone="Asia/Tokyo"))
    assert enforce.QUIET_HOURS in check_action(
        m_tokyo, action(), GOOD_COSTS, state(timezone="Not/AZone"), NOW
    )


def test_frequency_window_counts_recent_sends_and_pending() -> None:
    m = mandate(frequency_cap=FrequencyCap(max_touches=2, window_days=3))
    old = state(recent_contacts=(NOW - timedelta(days=5), NOW - timedelta(days=4)))
    assert check_action(m, action(), GOOD_COSTS, old, NOW) == []
    recent = state(recent_contacts=(NOW - timedelta(days=4), NOW - timedelta(days=1)))
    assert check_action(m, action(), GOOD_COSTS, recent, NOW) == []
    assert check_action(m, action(), GOOD_COSTS, recent, NOW, pending_touches=1) == [enforce.FREQUENCY_CAP]


def test_short_window_counts_sends_the_list_does_not_explain() -> None:
    """contacts_7d says two sends this week but no send times are listed: either could fall in a
    3-day window, so the cap of 2 is already reached."""
    m = mandate(frequency_cap=FrequencyCap(max_touches=2, window_days=3))
    assert check_action(m, action(), GOOD_COSTS, state(contacts_7d=2), NOW) == [enforce.FREQUENCY_CAP]
    # A list that explains the count is used exactly: both sends are older than 3 days.
    listed = state(contacts_7d=2, recent_contacts=(NOW - timedelta(days=5), NOW - timedelta(days=4)))
    assert check_action(m, action(), GOOD_COSTS, listed, NOW) == []
    # Unexplained 30-day sends count too, but not twice for the ones contacts_7d already reports.
    assert enforce.touches_in_window(state(contacts_7d=1, contacts_30d=3), NOW, 3) == 3


def test_margin_unknown_passes_only_without_an_offer_cost() -> None:
    m = mandate()
    no_offer = ActionCosts(channel_cost=0.01)
    assert check_action(m, action(), no_offer, state(), NOW) == []
    unknown_offer = ActionCosts(offer_cost=6.0)
    assert check_action(m, action(offer_id="pct10"), unknown_offer, state(), NOW) == [enforce.MARGIN_FLOOR]


def test_check_bid_uses_average_order_value_as_revenue() -> None:
    bid = Bid(
        goal_id="goal_winback",
        action=action(offer_id="pct10"),
        uplift=0.05,
        value_if_converted=40.0,
        offer_cost=6.0,
        channel_cost=0.01,
        expected_net=-4.0,
    )
    offers = (TEN_OFF,)
    assert check_bid(mandate(), bid, state(), NOW, offers=offers) == []  # (40 - 6) / 100 = 34% >= 20%
    assert check_bid(mandate(), bid, state(revenue=800.0), NOW, offers=offers) == [
        enforce.MARGIN_FLOOR
    ]  # 34 / 400


def test_check_bid_margin_uses_the_full_cost_per_redemption() -> None:
    """A bid's offer_cost is the expected cost (cost x chance of redemption); the converting order
    pays the full cost, so a 30-off offer on a 40 margin / 100 order breaks a 20% floor."""
    rich = TEN_OFF.model_copy(update={"cost_per_redemption": 30.0})
    bid = Bid(
        goal_id="goal_winback",
        action=action(offer_id="pct10"),
        uplift=0.05,
        value_if_converted=40.0,
        offer_cost=1.5,  # 30 x 5% redemption
        channel_cost=0.01,
        expected_net=0.5,
    )
    s = state(purchases=1, revenue=100.0)
    assert check_bid(mandate(), bid, s, NOW, offers=(rich,)) == [enforce.MARGIN_FLOOR]
    # Not described at all: it cannot show it keeps the floor.
    assert check_bid(mandate(), bid, s, NOW) == [enforce.MARGIN_FLOOR]
    # Spend still counts the expected cost: 1.51 fits under a daily cap with 2 left.
    assert (
        check_bid(mandate(margin_floor_pct=0.0), bid, s, NOW, offers=(rich,), spend=SpendToDate(today=398.0))
        == []
    )


# --- the envelope projection ------------------------------------------------------------------------------


def test_envelope_conversion_keeps_the_decide_rules_working() -> None:
    m = mandate()
    env = mandate_to_envelope(m, (TEN_OFF, FREE_SHIP), spent=1000.0)
    assert env.channels == (Channel.EMAIL, Channel.WHATSAPP)
    assert [o.offer_id for o in env.offers] == ["none", "pct10"]
    assert env.budget == 4000.0 and env.max_daily_spend == 400.0
    assert env.max_contacts_per_week == 2
    assert (env.quiet_start_hour, env.quiet_end_hour) == (21, 9)
    assert rules.is_quiet_hour(23, env) and not rules.is_quiet_hour(12, env)


def test_envelope_takes_the_tighter_of_mandate_and_base() -> None:
    base = Envelope(
        channels=(Channel.EMAIL,),
        budget=3000.0,
        max_daily_spend=100.0,
        max_contacts_per_week=1,
        exploration_floor=0.1,
    )
    env = mandate_to_envelope(mandate(), (TEN_OFF,), base=base)
    assert env.budget == 3000.0 and env.max_daily_spend == 100.0 and env.max_contacts_per_week == 1
    assert env.exploration_floor == 0.1
    assert env.channels == (Channel.EMAIL,)  # only what both allow
    # The base did not list pct10, so the brief never approved it for this goal.
    assert [o.offer_id for o in env.offers] == ["none"]


def test_envelope_keeps_the_bases_offer_description_and_both_quiet_windows() -> None:
    held = TEN_OFF.model_copy(update={"needs_approval": True})
    base = Envelope(
        channels=(Channel.EMAIL, Channel.SMS),
        offers=(NO_OFFER, held, FREE_SHIP),
        budget=10_000.0,
        quiet_start_hour=20,
        quiet_end_hour=8,
    )
    env = mandate_to_envelope(mandate(), (TEN_OFF,), base=base)
    assert env.offers == (NO_OFFER, held)  # approved by both; the base's needs_approval is kept
    assert (env.quiet_start_hour, env.quiet_end_hour) == (20, 9)  # 20-8 and 21-9 together
    # Windows that do not touch become the smallest single window covering both.
    lunch = base.model_copy(update={"quiet_start_hour": 12, "quiet_end_hour": 14})
    env = mandate_to_envelope(mandate(), base=lunch)
    assert (env.quiet_start_hour, env.quiet_end_hour) == (21, 14)
    assert all(rules.is_quiet_hour(h, env) for h in (12, 13, 21, 23, 3, 8))
    with pytest.raises(ValueError, match="no channel"):
        mandate_to_envelope(mandate(), base=base.model_copy(update={"channels": (Channel.SMS,)}))
    with pytest.raises(ValueError, match="no hour"):
        mandate_to_envelope(
            mandate(), base=base.model_copy(update={"quiet_start_hour": 9, "quiet_end_hour": 21})
        )


@pytest.mark.parametrize(("touches", "window", "weekly"), [(2, 7, 2), (4, 30, 4), (1, 1, 7), (1, 3, 2)])
def test_weekly_cap_is_never_looser(touches: int, window: int, weekly: int) -> None:
    env = mandate_to_envelope(mandate(frequency_cap=FrequencyCap(max_touches=touches, window_days=window)))
    assert env.max_contacts_per_week == weekly
    assert env.offers == (NO_OFFER,)


# --- the token bucket ----------------------------------------------------------------------------------


def test_bucket_paces_and_refills() -> None:
    b = TokenBucket.for_daily_cap(240.0, NOW, burst_share=0.25)
    assert b.capacity == 60.0
    ok, b = b.try_spend(60.0, NOW)
    assert ok and b.tokens == 0.0
    ok, b = b.try_spend(1.0, NOW)
    assert not ok
    # (240 - 60) / 24h = 7.5 an hour.
    assert b.available(NOW + timedelta(hours=2)) == pytest.approx(15.0)
    assert b.wait_seconds(15.0, NOW) == pytest.approx(7200.0)
    assert b.wait_seconds(61.0, NOW) is None
    assert b.available(NOW + timedelta(days=3)) == 60.0
    assert b.available(NOW - timedelta(hours=1)) == 0.0  # time never runs backwards


def test_no_rolling_day_spends_more_than_the_cap() -> None:
    cap = 100.0
    b = TokenBucket.for_daily_cap(cap, NOW)
    amounts: list[tuple[datetime, float]] = []
    t = NOW
    for _ in range(48 * 12):  # every 5 minutes for two days, as greedily as allowed
        amount = b.available(t)
        ok, b = b.try_spend(amount, t)
        assert ok
        amounts.append((t, amount))
        t += timedelta(minutes=5)
    for i, (start, _) in enumerate(amounts):
        window = sum(a for w, a in amounts[i:] if w < start + timedelta(days=1))
        assert window <= cap + 1e-6


def test_bucket_for_mandate_and_bad_inputs() -> None:
    assert TokenBucket.for_mandate(mandate(max_daily_spend=None), NOW) is None
    b = TokenBucket.for_mandate(mandate(), NOW)
    assert b is not None and b.capacity == 100.0
    with pytest.raises(ValueError, match="negative"):
        b.try_spend(-1.0, NOW)
    with pytest.raises(ValueError, match="burst_share"):
        TokenBucket.for_daily_cap(10.0, NOW, burst_share=1.0)
    with pytest.raises(ValidationError, match="capacity"):
        TokenBucket(capacity=10.0, refill_per_second=0.0, tokens=50.0, updated_at=NOW)


# --- model validation -----------------------------------------------------------------------------------


def test_mandate_rejects_nonsense() -> None:
    with pytest.raises(ValidationError):
        mandate(valid_until=NOW - timedelta(days=2))
    with pytest.raises(ValidationError):
        mandate(max_daily_spend=6000.0)
    with pytest.raises(ValidationError):
        mandate(channels=())
    with pytest.raises(ValidationError):
        mandate(quiet_hours={"start": 21, "end": 9, "timezone": "Mars/Base"})
    with pytest.raises(ValidationError):
        mandate(currency="dollars")
