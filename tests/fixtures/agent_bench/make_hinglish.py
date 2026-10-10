"""Synthetic Indian-style exports with Hinglish and abbreviated headers (Plan K M142, DEC-1409).

Every value is generated here from a seed: a column that would hold mobile numbers holds surrogate keys
(`M` and a counter), customer ids are `G` and a counter, dates count back from one fixed day. No real customer, number or name
exists in any of it.

Each `CaseSpec` lists the file's columns in order, with the kind of values each holds, and the
hand-written answer: which header plays which of the six roles (the ID, the outcome, the date the
file describes, consent, opt-out, last contact), or `None` when the file has no such column. The
answer is written from what a person who knows the export would say, not from what the rules find:
a few headers (`last_nudge_dt`, `lan_no`) are ones the lexicon cannot read, on purpose, and count as
misses.

`SPLIT` marks each case `dev` (the first twelve, written before the lexicon was run on anything and
looked at while fixing it) or `held_out` (the other twelve, written after and not used to tune).
`rules_accuracy` reports both.
"""

from __future__ import annotations

import random
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Any, Final

import pandas as pd

__all__ = ["CASES", "ROLE_ORDER", "SPLIT", "CaseSpec", "hinglish_frame", "hinglish_names"]

ROLE_ORDER: Final[tuple[str, ...]] = ("id", "outcome", "date", "consent", "opt_out", "last_contact")
ROWS: Final[int] = 2_000
TODAY: Final[date] = date(2026, 9, 30)
"""The day the synthetic exports were "taken"; every date counts back from it."""


@dataclass(frozen=True)
class CaseSpec:
    name: str
    columns: tuple[tuple[str, str], ...]
    """(header, kind of values) in file order."""
    expected: dict[str, str | None]
    """role -> the header that plays it, or None when the file has none."""

    def __post_init__(self) -> None:
        headers = [header for header, _ in self.columns]
        if len(set(headers)) != len(headers):
            raise ValueError(f"{self.name}: a header is used twice")
        if set(self.expected) != set(ROLE_ORDER):
            raise ValueError(f"{self.name}: the answer must give all of {ROLE_ORDER}")
        for role, header in self.expected.items():
            if header is not None and header not in headers:
                raise ValueError(f"{self.name}: {role} -> {header!r} is not a column")


def _spec(name: str, columns: str, **expected: str | None) -> CaseSpec:
    """`columns` is `header:kind` pairs separated by `|`, so a header may hold a space."""
    pairs = tuple(tuple(part.rsplit(":", 1)) for part in columns.split("|"))
    answer: dict[str, str | None] = dict.fromkeys(ROLE_ORDER)
    answer.update(expected)
    return CaseSpec(name, tuple((h, k) for h, k in pairs), answer)


CASES: Final[tuple[CaseSpec, ...]] = (
    # ----------------------------------------------------------------------- dev: the first twelve
    _spec(
        "telco_prepaid_abbr",
        "cust_mob_no:mobile|churn_flg:outcome|snpsht_dt:snapshot|mkt_optin:consent|dnd_flag:optout|"
        "last_cntct_dt:date_recent|last_rchg_dt:date_recent|avg_mthly_spnd:money|circle:city|tenure_mths:count|arpu_3m:money",
        id="cust_mob_no",
        outcome="churn_flg",
        date="snpsht_dt",
        consent="mkt_optin",
        opt_out="dnd_flag",
        last_contact="last_cntct_dt",
    ),
    _spec(
        "telco_hinglish",
        "grahak_id:id|cust_mob_no:mobile|kharida_30d:outcome|tareekh:snapshot|sahmati:consent|"
        "pichhla_sampark:date_recent|pehla_order:date_old|kul_kharch:money|avg_mthly_spnd:money|shehar:city",
        id="grahak_id",
        outcome="kharida_30d",
        date="tareekh",
        consent="sahmati",
        last_contact="pichhla_sampark",
    ),
    _spec(
        "retail_runon",
        "custid:id|conv_30d:outcome|data_dt:snapshot|optin:consent|optout:optout|"
        "lastcontactdate:date_recent|ord_cnt:count|aov:money|pincode:pin",
        id="custid",
        outcome="conv_30d",
        date="data_dt",
        consent="optin",
        opt_out="optout",
        last_contact="lastcontactdate",
    ),
    _spec(
        "bank_cards",
        "acct_no:id_num|dflt_flg:outcome|as_on_dt:snapshot|consent_flg:consent|dnc_flag:optout|"
        "last_comm_dt:date_recent|cr_lmt:money|util_pct:pct|dpd_max:count|branch_cd:city|open_dt:date_old",
        id="acct_no",
        outcome="dflt_flg",
        date="as_on_dt",
        consent="consent_flg",
        opt_out="dnc_flag",
        last_contact="last_comm_dt",
    ),
    _spec(
        "insurance_renewal",
        "policy_no:id|renewed_flag:outcome|report_dt:snapshot|comm_consent:consent|unsubscribed:optout|"
        "last_contacted_on:date_recent|prem_amt:money|sum_assured:money|agent_cd:plan|dob:date_old",
        id="policy_no",
        outcome="renewed_flag",
        date="report_dt",
        consent="comm_consent",
        opt_out="unsubscribed",
        last_contact="last_contacted_on",
    ),
    _spec(
        "fintech_lending",
        "user_id:id|responded:outcome|run_date:snapshot|mktg_consent:consent|unsub_flag:optout|"
        "lst_contact_dt:date_recent|loan_amt:money|cibil:count|city:city",
        id="user_id",
        outcome="responded",
        date="run_date",
        consent="mktg_consent",
        opt_out="unsub_flag",
        last_contact="lst_contact_dt",
    ),
    _spec(
        "ecom_titlecase",
        "Customer Id:id|Khareeda:outcome|Snapshot Date:snapshot|Marketing OptIn:consent|Do Not Contact:optout|"
        "Last Contacted:date_recent|Total Orders:count|Kul Kharch:money|State:city",
        id="Customer Id",
        outcome="Khareeda",
        date="Snapshot Date",
        consent="Marketing OptIn",
        opt_out="Do Not Contact",
        last_contact="Last Contacted",
    ),
    _spec(
        "dth_hinglish",
        "subscriber_id:id|recharge_kiya:outcome|tareekh_dt:snapshot|sahmati_flag:consent|dnd_flag:optout|"
        "pichhla_sampark_dt:date_recent|kul_rashi:money|shehar:city",
        id="subscriber_id",
        outcome="recharge_kiya",
        date="tareekh_dt",
        consent="sahmati_flag",
        opt_out="dnd_flag",
        last_contact="pichhla_sampark_dt",
    ),
    _spec(
        "kirana_orders",
        "retailer_id:id|reorder_flag:outcome|report_wk_dt:snapshot|last_visit_dt:date_recent|"
        "sku_cnt:count|outlet_type:plan|pin:pin",
        id="retailer_id",
        outcome="reorder_flag",
        date="report_wk_dt",
    ),
    _spec(
        "edtech_whatsapp",
        "learner_id:id|is_converted:outcome|as_of_date:snapshot|whatsapp_optin:consent|wa_dnd:optout|"
        "last_nudge_dt:date_recent|course_cnt:count|city:city",
        id="learner_id",
        outcome="is_converted",
        date="as_of_date",
        consent="whatsapp_optin",
        opt_out="wa_dnd",
        last_contact="last_nudge_dt",
    ),
    _spec(
        "savings_hinglish",
        "khata_sankhya:id_num|product_kharida:outcome|tarikh:snapshot|ijazat:consent|dnd_status:optout|"
        "aakhri_sampark_dt:date_recent|balance_amt:money|shehar:city",
        id="khata_sankhya",
        outcome="product_kharida",
        date="tarikh",
        consent="ijazat",
        opt_out="dnd_status",
        last_contact="aakhri_sampark_dt",
    ),
    _spec(
        "upi_wallet",
        "msisdn:mobile|repeat_txn_30d:outcome|ts_snapshot:snapshot|promo_consent:consent|optedout:optout|"
        "last_comm_ts:date_recent|txn_cnt:count|avg_txn_amt:money",
        id="msisdn",
        outcome="repeat_txn_30d",
        date="ts_snapshot",
        consent="promo_consent",
        opt_out="optedout",
        last_contact="last_comm_ts",
    ),
    # ------------------------------------------------------------------- held out: the other twelve
    _spec(
        "postpaid_bills",
        "sub_id:id|bill_acct_no:id_num|churned_90d:outcome|data_as_on:snapshot|consent_given:consent|"
        "dnd_registered:optout|last_outreach_dt:date_recent|bill_cycle_dt:date_recent|arpu:money",
        id="sub_id",
        outcome="churned_90d",
        date="data_as_on",
        consent="consent_given",
        opt_out="dnd_registered",
        last_contact="last_outreach_dt",
    ),
    _spec(
        "auto_loans",
        "cust_no:id|npa_flag:outcome|snap_dt:snapshot|marketing_consent:consent|do_not_call:optout|"
        "last_call_dt:date_recent|emi_amt:money|dealer_cd:plan|tenure_mths:count",
        id="cust_no",
        outcome="npa_flag",
        date="snap_dt",
        consent="marketing_consent",
        opt_out="do_not_call",
        last_contact="last_call_dt",
    ),
    _spec(
        "housing_finance",
        "lan_no:id_num|prepaid_flag:outcome|report_date:snapshot|ok_to_contact:consent|"
        "last_contacted_dt:date_recent|loan_amt:money|roi_pct:pct|branch:city",
        id="lan_no",
        outcome="prepaid_flag",
        date="report_date",
        consent="ok_to_contact",
        last_contact="last_contacted_dt",
    ),
    _spec(
        "dth_recharge",
        "VC_NO:id_num|RECHARGED_30D:outcome|SNAPSHOT_DT:snapshot|MKT_CONSENT:consent|OPT_OUT_FLG:optout|"
        "LAST_RCHG_DT:date_recent|LAST_CNTCT_DT:date_recent|PACK_AMT:money|ZONE:city",
        id="VC_NO",
        outcome="RECHARGED_30D",
        date="SNAPSHOT_DT",
        consent="MKT_CONSENT",
        opt_out="OPT_OUT_FLG",
        last_contact="LAST_CNTCT_DT",
    ),
    _spec(
        "food_delivery_camel",
        "userId:id|orderedAgain:outcome|asOfDate:snapshot|marketingOptIn:consent|doNotDisturb:optout|"
        "lastContactedAt:date_recent|lastOrderDate:date_recent|aov:money|cityName:city",
        id="userId",
        outcome="orderedAgain",
        date="asOfDate",
        consent="marketingOptIn",
        opt_out="doNotDisturb",
        last_contact="lastContactedAt",
    ),
    _spec(
        "gold_loan",
        "grahek_id:id|khareed_flag:outcome|dinank:snapshot|anumati:consent|sms_unsub:optout|"
        "pichhla_sampark:date_recent|gold_wt:money|shehar:city|first_loan_dt:date_old",
        id="grahek_id",
        outcome="khareed_flag",
        date="dinank",
        consent="anumati",
        opt_out="sms_unsub",
        last_contact="pichhla_sampark",
    ),
    _spec(
        "pharmacy_chain",
        "member_cd:id|redeemed_offer:outcome|extract_dt:snapshot|promo_optin:consent|dnd:optout|"
        "last_sms_dt:date_recent|bill_cnt:count|store_cd:city",
        id="member_cd",
        outcome="redeemed_offer",
        date="extract_dt",
        consent="promo_optin",
        opt_out="dnd",
        last_contact="last_sms_dt",
    ),
    _spec(
        "mobile_postpaid_short",
        "cid:id|cnvrt:outcome|dt:snapshot|optin:consent|dnc:optout|lst_cntct:date_recent|"
        "rchg_amt:money|circle:city",
        id="cid",
        outcome="cnvrt",
        date="dt",
        consent="optin",
        opt_out="dnc",
        last_contact="lst_cntct",
    ),
    _spec(
        "travel_app",
        "traveller_id:id|booked_30d:outcome|run_dt:snapshot|email_consent:consent|unsubscribed_flag:optout|"
        "last_email_dt:date_recent|trip_cnt:count|home_city:city",
        id="traveller_id",
        outcome="booked_30d",
        date="run_dt",
        consent="email_consent",
        opt_out="unsubscribed_flag",
        last_contact="last_email_dt",
    ),
    _spec(
        "no_flags_hinglish",
        "grahak_sankhya:id_num|dobara_kharida:outcome|tareekh:snapshot|kul_kharch:money|pehla_order:date_old|"
        "avg_bill_val:money|shehar:city",
        id="grahak_sankhya",
        outcome="dobara_kharida",
        date="tareekh",
    ),
    _spec(
        "wide_telecom",
        "msisdn:mobile|cust_id:id|attr_flag:outcome|snapshot_date:snapshot|mkt_consent_flg:consent|"
        "dnd_flg:optout|last_contact_date:date_recent|data_usage_gb:money|voice_mins:count|recharge_cnt_3m:count|"
        "signup_dt:date_old|circle:city",
        id="cust_id",
        outcome="attr_flag",
        date="snapshot_date",
        consent="mkt_consent_flg",
        opt_out="dnd_flg",
        last_contact="last_contact_date",
    ),
    _spec(
        "yes_no_values",
        "customer_code:id|bought_after_ad:outcome_yn|snapshot_dt:snapshot|consent:consent_yn|do_not_contact:optout_yn|"
        "last_contacted:date_recent|visits_7d:count|region:city",
        id="customer_code",
        outcome="bought_after_ad",
        date="snapshot_dt",
        consent="consent",
        opt_out="do_not_contact",
        last_contact="last_contacted",
    ),
    # No outcome column at all, but a date of the last recharge or order: Guided setup stops, and drafts
    # a lapse outcome with example customers (M93's lapse label, DEC-1303 (d)).
    _spec(
        "lapse_no_outcome_telco",
        "cust_mob_no:mobile|tareekh:snapshot|last_rchg_dt:date_recent|pehla_order:date_old|"
        "avg_mthly_spnd:money|circle:city|tenure_mths:count",
        id="cust_mob_no",
        date="tareekh",
    ),
    _spec(
        "lapse_no_outcome_retail",
        "grahak_id:id|snapshot_dt:snapshot|last_order_date:date_recent|kul_kharch:money|shehar:city|"
        "order_cnt:count",
        id="grahak_id",
        date="snapshot_dt",
    ),
    _spec(
        "ambiguous_ids",
        "order_id:id|customer_ref:id|churn_label:outcome|extract_date:snapshot|consent:consent|"
        "contact_name:name|last_contact_dt:date_recent|spend_total:money",
        id="customer_ref",
        outcome="churn_label",
        date="extract_date",
        consent="consent",
        last_contact="last_contact_dt",
    ),
)

SPLIT: Final[dict[str, str]] = {
    spec.name: ("dev" if index < 12 else "held_out") for index, spec in enumerate(CASES)
}


def _values(kind: str, header: str, rng: random.Random, rows: int, offset: int) -> list[Any]:
    """One column of `rows` values of `kind`."""
    if kind == "id":
        return [f"G{offset * 10 + i:05d}" for i in range(rows)]
    if kind == "id_num":
        return [100_000 + offset + 3 * i for i in range(rows)]
    if kind == "mobile":
        # A surrogate key standing where a ten-digit number would be: the rules read the header and the
        # profile's counts, never a number, and no phone-shaped value belongs in a fixture.
        return [f"M{offset * 10 + i:05d}" for i in range(rows)]
    if kind in {"outcome", "outcome_yn"}:
        flags = [1 if rng.random() < 0.28 else 0 for _ in range(rows)]
        return ["Y" if f else "N" for f in flags] if kind == "outcome_yn" else flags
    if kind in {"consent", "consent_yn"}:
        flags = [1 if rng.random() < 0.85 else 0 for _ in range(rows)]
        return ["Y" if f else "N" for f in flags] if kind == "consent_yn" else flags
    if kind in {"optout", "optout_yn"}:
        flags = [1 if rng.random() < 0.08 else 0 for _ in range(rows)]
        return ["Y" if f else "N" for f in flags] if kind == "optout_yn" else flags
    if kind == "snapshot":
        days = [TODAY - timedelta(days=7 * w) for w in range(6)]
        return [days[i % len(days)].isoformat() for i in range(rows)]
    if kind == "date_recent":
        return [
            None if rng.random() < 0.15 else (TODAY - timedelta(days=rng.randint(1, 120))).isoformat()
            for _ in range(rows)
        ]
    if kind == "date_old":
        return [(TODAY - timedelta(days=rng.randint(200, 2_500))).isoformat() for _ in range(rows)]
    if kind == "money":
        return [round(rng.uniform(40, 4_000), 2) for _ in range(rows)]
    if kind == "count":
        return [rng.randint(0, 40) for _ in range(rows)]
    if kind == "pct":
        return [round(rng.random(), 3) for _ in range(rows)]
    if kind == "city":
        cities = ["Pune", "Jaipur", "Lucknow", "Indore", "Kochi", "Surat", "Nagpur"]
        return [rng.choice(cities) for _ in range(rows)]
    if kind == "plan":
        return [rng.choice(["A", "B", "C", "D"]) for _ in range(rows)]
    if kind == "pin":
        return [rng.choice(["411001", "302001", "226001", "452001", "682001"]) for _ in range(rows)]
    if kind == "name":
        return [rng.choice(["Asha", "Ravi", "Meena", "Imran", "Kavya"]) for _ in range(rows)]
    raise ValueError(f"{header}: unknown kind {kind!r}")


def hinglish_frame(spec: CaseSpec, rows: int = ROWS) -> pd.DataFrame:
    """The spec's file: the same frame every time (seeded by the case name)."""
    rng = random.Random(f"m142-{spec.name}")
    offset = rng.randint(1, 900)
    data = {header: _values(kind, header, rng, rows, offset) for header, kind in spec.columns}
    return pd.DataFrame(data)


def hinglish_names() -> tuple[str, ...]:
    return tuple(spec.name for spec in CASES)
