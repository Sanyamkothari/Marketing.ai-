"""The gate on a run whose message arms are in force: each rule of a variant's own words (Plan K M145, DEC-1412).

`variant_message_checks` is run over a real catalogue (`load_catalogue`, with the shipped India region file, so
SMS needs a DLT template) and a real URL whitelist file; `decide` is given its verdicts as the hand-off gives them;
`gate_treat_list` then decides, writes and signs for a small list whose customers are drawn into the control
template and two variants, one of them planned on SMS then email. The end-to-end path - a real scoring run, its
copy written and approved through the API, the arms drawn, the treat list built - is
`tests/integration/compliance/test_gate_variant_arms.py`.

On the unchanged code (`engine.compliance.variants` does not exist, the gate checks the band's action only) these
tests fail.
"""

from __future__ import annotations

import io
import shutil
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
import pyarrow as pa
import pytest

from engine.agentic.canonical import digest
from engine.agentic.ledger import DecisionLedger, signer_for_data_dir
from engine.compliance.config import load_url_whitelist
from engine.compliance.gate import REASON_ORDER, GateRules, MessageVerdict, decide
from engine.compliance.handoff import dlt_channels, gate_treat_list
from engine.compliance.variants import VariantMessages, VariantWords, is_variant_level, variant_message_checks
from engine.config import load_yaml
from engine.decide.catalogue import catalogue_sha256, clear_catalogue_cache, load_catalogue
from engine.platform_db import sqlite_engine
from engine.storage import LocalStorage, run_key
from tests.unit.compliance.switch import set_gate_switch

RUN = "r_20261011_0c145a01"
KEY = "customer_id"
BAND_TEMPLATE = "Dear customer, come back for Rs {#numeric#} off - Acme"
BAND_MESSAGE = "Dear customer, come back for Rs 200 off - Acme"
A_SMS = "Hi {{first_name}}, your plan is back at shop.example.in/back\nReply STOP to opt out"
A_EMAIL = "Hi {{first_name}}, your plan is back. See shop.example.in/back for the details."
B_SMS = "Hi {{first_name}}, your line is ready whenever you are.\nReply STOP to opt out"
C_SMS = "Hi {{first_name}}, a new plan is waiting.\nReply STOP to opt out"
CATALOGUE = f"""\
region: IN
actions:
  - action_id: winback
    label: Win-back
    channels: [sms, email]
    dlt_template_id: "1107000000000000001"
    message_category: promotional
    dlt_template_text: "{BAND_TEMPLATE}"
    message_text: "{BAND_MESSAGE}"
  - action_id: var_a_sms
    label: Variant A on SMS
    channels: [sms]
    template_ref: "copy:aaaaaaaaaaaaaaa1"
    dlt_template_id: "1107000000000000011"
    message_category: promotional
    dlt_template_text: "Hi {{#var#}}, your plan is back at {{#var#}}\\nReply STOP to opt out"
  - action_id: var_a_email
    label: Variant A by e-mail
    channels: [email]
    template_ref: "copy:aaaaaaaaaaaaaaa2"
  - action_id: var_b_sms
    label: Variant B on SMS
    channels: [sms]
    template_ref: "copy:bbbbbbbbbbbbbbb1"
    dlt_template_id: "1107000000000000012"
    message_category: promotional
    dlt_template_text: "Hi {{#var#}}, your line is ready when you like.\\nReply STOP to opt out"
  - action_id: var_c_sms
    label: Variant C on SMS, registered without its template text
    channels: [sms]
    template_ref: "copy:ccccccccccccccc1"
    dlt_template_id: "1107000000000000013"
    message_category: promotional
  - action_id: var_d_email_only
    label: Variant D, registered on e-mail only
    channels: [email]
    template_ref: "copy:ddddddddddddddd1"
"""
WHITELIST = "channels: [sms, email]\nurls: [shop.example.in]\n"


def _words(
    arm: str, channel: str, action: str, text: str, *, unit: str = "High", subject: str | None = None
) -> VariantWords:
    letter = arm[-1].upper()
    return VariantWords(
        arm=arm,
        unit=unit,
        channel=channel,
        template_id=f"{unit}-{channel}-{letter}",
        template_ref=f"copy:{letter.lower() * 15}{1 if channel == 'sms' else 2}",
        catalogue_action_id=action,
        subject=subject,
        text=text,
    )


WORDS: dict[tuple[str, str, str], VariantWords] = {
    ("variant_a", "High", "sms"): _words("variant_a", "sms", "var_a_sms", A_SMS),
    ("variant_a", "High", "email"): _words(
        "variant_a", "email", "var_a_email", A_EMAIL, subject="Your plan, at shop.example.in/back"
    ),
    ("variant_b", "High", "sms"): _words("variant_b", "sms", "var_b_sms", B_SMS),
    ("variant_c", "High", "sms"): _words("variant_c", "sms", "var_c_sms", C_SMS),
    ("variant_d", "High", "sms"): _words("variant_d", "sms", "var_d_email_only", C_SMS),
}


@pytest.fixture
def root(tmp_path: Path, config_root: Path) -> Iterator[Path]:
    target = tmp_path / "configs"
    shutil.copytree(config_root, target)
    (target / "decide" / "catalogue.yaml").write_text(CATALOGUE, encoding="utf-8")
    (target / "compliance" / "url_whitelist.yaml").write_text(WHITELIST, encoding="utf-8")
    set_gate_switch(target, on=True)
    clear_catalogue_cache()
    yield target
    clear_catalogue_cache()


def _checks(root: Path, wanted: list[tuple[str, str, str]], *, catalogue: Any = True) -> tuple[Any, Any]:
    loaded = load_catalogue(root)
    region = load_yaml(root / "regions" / "in.yaml")
    whitelist, _source = load_url_whitelist(root, region)
    return variant_message_checks(
        loaded if catalogue else None,
        catalogue_sha256(root),
        wanted,
        WORDS,
        dlt_channels=dlt_channels(region),
        whitelist=whitelist,
        var_max_chars=30,
    )


# ---------------------------------------------------------------------------
# One rule at a time: `variant_message_checks`
# ---------------------------------------------------------------------------
def test_words_that_match_their_own_registered_template_and_list_their_links_pass(root: Path) -> None:
    verdicts, checks = _checks(root, [("variant_a", "High", "sms")])
    verdict = verdicts[("variant_a", "High", "sms")]
    assert verdict == MessageVerdict(catalogue_ref=f"{catalogue_sha256(root)}#var_a_sms")
    (check,) = checks
    assert check.template_checked and check.template_mismatch is False
    assert check.urls_checked and check.urls_not_whitelisted == 0
    assert check.catalogue_action_id == "var_a_sms" and check.template_id == "High-sms-A"


def test_words_that_break_their_template_are_a_mismatch(root: Path) -> None:
    verdicts, checks = _checks(root, [("variant_b", "High", "sms")])
    assert verdicts[("variant_b", "High", "sms")].template_mismatch is True
    assert checks[0].template_mismatch is True and checks[0].unregistered is False


def test_a_link_not_on_the_whitelist_is_found_in_the_subject_too(root: Path) -> None:
    unlisted = {
        **WORDS,
        ("variant_a", "High", "email"): _words(
            "variant_a", "email", "var_a_email", A_EMAIL, subject="Your plan, at deals-x.in/back"
        ),
    }
    loaded = load_catalogue(root)
    region = load_yaml(root / "regions" / "in.yaml")
    whitelist, _source = load_url_whitelist(root, region)
    verdicts, checks = variant_message_checks(
        loaded,
        catalogue_sha256(root),
        [("variant_a", "High", "email")],
        unlisted,
        dlt_channels=dlt_channels(region),
        whitelist=whitelist,
        var_max_chars=30,
    )
    assert verdicts[("variant_a", "High", "email")].url_not_whitelisted is True
    assert checks[0].urls_not_whitelisted == 1
    # E-mail needs no DLT template in India: the words are not matched, and that is said.
    assert checks[0].template_checked is False and checks[0].unregistered is False
    assert any("does not require a DLT template on email" in note for note in checks[0].notes)
    listed, _ = _checks(root, [("variant_a", "High", "email")])
    assert listed[("variant_a", "High", "email")].url_not_whitelisted is False


def test_words_with_no_template_text_on_a_dlt_channel_are_not_sent(root: Path) -> None:
    verdicts, checks = _checks(root, [("variant_c", "High", "sms")])
    verdict = verdicts[("variant_c", "High", "sms")]
    assert verdict.unregistered is True and verdict.template_mismatch is False
    assert checks[0].template_checked is False
    assert any("dlt_template_text" in note for note in checks[0].notes)


def test_an_action_that_does_not_register_the_channel_fails_closed(root: Path) -> None:
    verdicts, checks = _checks(root, [("variant_d", "High", "sms")])
    assert verdicts[("variant_d", "High", "sms")].unregistered is True
    assert any("No catalogue action registers" in note for note in checks[0].notes)
    gone = {**WORDS, ("variant_d", "High", "sms"): _words("variant_d", "sms", "var_missing", C_SMS)}
    loaded = load_catalogue(root)
    region = load_yaml(root / "regions" / "in.yaml")
    found, _ = variant_message_checks(
        loaded,
        catalogue_sha256(root),
        [("variant_d", "High", "sms")],
        gone,
        dlt_channels=dlt_channels(region),
        whitelist=None,
        var_max_chars=30,
    )
    assert found[("variant_d", "High", "sms")].unregistered is True


def test_a_channel_the_variant_has_no_words_on_is_closed_to_it(root: Path) -> None:
    verdicts, checks = _checks(root, [("variant_b", "High", "email")])
    verdict = verdicts[("variant_b", "High", "email")]
    assert verdict.no_copy is True and verdict.catalogue_ref is None
    assert checks[0].no_copy is True and checks[0].template_id is None


def test_each_arm_group_and_channel_is_checked_once(root: Path) -> None:
    wanted = [("variant_a", "High", "sms")] * 1000 + [("variant_b", "High", "sms")] * 1000
    verdicts, checks = _checks(root, wanted)
    assert len(verdicts) == 2 and len(checks) == 2


def test_a_catalogue_other_than_the_arms_checks_nothing(root: Path) -> None:
    verdicts, checks = _checks(root, [("variant_a", "High", "sms")], catalogue=False)
    assert verdicts == {} and checks == ()


def test_only_variant_levels_send_a_variant_s_words() -> None:
    assert is_variant_level("variant_a") and is_variant_level("variant_d")
    assert not any(is_variant_level(level) for level in ("control_template", "no_message", None, ""))


# ---------------------------------------------------------------------------
# `decide`: the two new reasons, after the six M134 reasons
# ---------------------------------------------------------------------------
def _customers(rows: list[tuple[str, str | None]]) -> pa.Table:
    return pa.table(
        {
            KEY: pa.array([key for key, _ in rows], pa.string()),
            "action_id": pa.array([action for _, action in rows], pa.string()),
        }
    )


def test_the_new_reasons_come_last_so_other_rows_list_theirs_as_before() -> None:
    assert REASON_ORDER[:6] == (
        "COMPLIANCE_NO_CHANNEL",
        "COMPLIANCE_CONSENT",
        "COMPLIANCE_WHATSAPP_NO_OPT_IN",
        "COMPLIANCE_CATALOGUE_CHANGED",
        "COMPLIANCE_TEMPLATE_MISMATCH",
        "COMPLIANCE_URL_NOT_WHITELISTED",
    )
    assert REASON_ORDER[6:8] == ("COMPLIANCE_VARIANT_NO_COPY", "COMPLIANCE_VARIANT_UNREGISTERED")
    # Plan M M184 (DEC-1614 (c)): the strict gate's reason comes after them, for the same reason.
    assert REASON_ORDER[8:] == ("COMPLIANCE_MESSAGE_NOT_CHECKED",)


def test_decide_lists_no_copy_and_unregistered_and_a_changed_catalogue_alone() -> None:
    customers = _customers([("C1", "k_no_copy"), ("C2", "k_unregistered"), ("C3", "plain")])
    planned = {None: (), "k_no_copy": ("sms",), "k_unregistered": ("sms",), "plain": ("sms",)}
    messages = {
        ("k_no_copy", "sms"): MessageVerdict(no_copy=True),
        ("k_unregistered", "sms"): MessageVerdict(unregistered=True, catalogue_ref="sha#var_c_sms"),
        ("plain", "sms"): MessageVerdict(catalogue_ref="sha#winback"),
    }
    table = decide(
        customers, key_columns=(KEY,), planned=planned, rules=GateRules(rule_version="r"), messages=messages
    )
    reasons = {row[KEY]: list(row["reasons"]) for row in table.to_pylist()}
    assert reasons == {
        "C1": ["COMPLIANCE_VARIANT_NO_COPY"],
        "C2": ["COMPLIANCE_VARIANT_UNREGISTERED"],
        "C3": [],
    }
    changed = decide(
        customers,
        key_columns=(KEY,),
        planned=planned,
        rules=GateRules(rule_version="r", catalogue_changed=True),
        messages=messages,
    )
    reasons = {row[KEY]: list(row["reasons"]) for row in changed.to_pylist()}
    # A variant with no words on the channel is known without the catalogue; nothing else is read from it.
    assert reasons == {
        "C1": ["COMPLIANCE_CATALOGUE_CHANGED", "COMPLIANCE_VARIANT_NO_COPY"],
        "C2": ["COMPLIANCE_CATALOGUE_CHANGED"],
        "C3": ["COMPLIANCE_CATALOGUE_CHANGED"],
    }


# ---------------------------------------------------------------------------
# `gate_treat_list` with message arms: the list, the file, the summary
# ---------------------------------------------------------------------------
# customer: (treated, arm drawn, band action, the run's channel, contactable on sms)
ROWS: dict[str, tuple[bool, str | None, str | None, str | None, bool]] = {
    "C1": (True, "control_template", "winback", "sms", True),  # the band's message: passes on SMS
    "C2": (True, "variant_a", "winback", "sms", True),  # A's SMS words pass: sent on SMS
    "C3": (True, "variant_a", "winback", "sms", False),  # not on SMS: A's e-mail words, sent by e-mail
    "C4": (True, "variant_b", "winback", "sms", True),  # B breaks its template; no e-mail words: withheld
    "C5": (True, "variant_c", "winback", "sms", True),  # C has no template text; no e-mail words: withheld
    "C6": (True, None, "winback", "sms", True),  # outside the copy's groups: the band's message
    "C7": (False, "variant_a", "winback", None, True),  # not contacted: never gated
}
LEVELS = ("control_template", "variant_a", "variant_b", "variant_c")


STAMP = "0" * 64
"""The run's own catalogue stamp, from before the variants were registered: the stamp the arms were drawn over."""


def _variants(root: Path, arms_sha: str | None = None, run_sha: str | None = STAMP) -> VariantMessages:
    keys = list(ROWS)
    return VariantMessages(
        arm=np.asarray([ROWS[k][1] for k in keys], dtype=object),
        unit=np.asarray(["High" if ROWS[k][1] else None for k in keys], dtype=object),
        words={key: value for key, value in WORDS.items() if key[0] != "variant_d"},
        levels=LEVELS,
        batch_id="b1",
        batch_created_at=datetime(2026, 10, 11, tzinfo=UTC).isoformat(),
        catalogue_sha256=arms_sha or str(catalogue_sha256(root)),
        run_catalogue_sha256=run_sha,
        templates=tuple(
            sorted(
                (w.arm, w.unit, w.channel, w.template_ref, w.catalogue_action_id)
                for k, w in WORDS.items()
                if k[0] != "variant_d"
            )
        ),
    )


def _gate(
    root: Path,
    tmp_path: Path,
    *,
    variants: VariantMessages | None,
    stamp: str | None,
    no_email: frozenset[str] = frozenset(),
) -> tuple[Any, pd.DataFrame]:
    storage = LocalStorage(tmp_path / "data")
    keys = list(ROWS)
    flags = pd.DataFrame(
        {
            KEY: keys,
            "contactable_sms": [ROWS[k][4] for k in keys],
            "contactable_email": [k not in no_email for k in keys],
        }
    )
    buffer = io.BytesIO()
    flags.to_parquet(buffer, index=False)
    storage.write_bytes(run_key(RUN, "channel_contactability.parquet"), buffer.getvalue())
    outcome = gate_treat_list(
        storage,
        run_id=RUN,
        use_case_id="win-back-campaign",
        client_id=None,
        key_frame=pd.DataFrame({KEY: keys}).astype("object"),
        treat=np.asarray([ROWS[k][0] for k in keys], dtype=bool),
        channel=np.asarray([ROWS[k][3] for k in keys], dtype=object),
        actions=np.asarray([ROWS[k][2] for k in keys], dtype=object),
        planned_by_action={"winback": ("sms", "email")},
        configured=("sms",),
        stamp_sha256=stamp,
        config_root=root,
        ledger=DecisionLedger(
            sqlite_engine(tmp_path / "ledger.db"),
            signer=signer_for_data_dir(tmp_path / "keys"),
            policy_version="p" * 64,
        ),
        variants=variants,
    )
    decisions = pd.read_parquet(io.BytesIO(storage.read_bytes(run_key(RUN, "compliance_decisions.parquet"))))
    return outcome, decisions


def test_each_customer_is_sent_on_the_first_channel_their_own_words_pass(root: Path, tmp_path: Path) -> None:
    # The run was stamped before the variants were registered: the arms' catalogue is the reference.
    outcome, decisions = _gate(root, tmp_path, variants=_variants(root), stamp=STAMP)
    assert list(outcome.treat) == [True, True, True, False, False, True, False]
    assert list(outcome.channel) == ["sms", "sms", "email", None, None, "sms", None]
    rows = {
        (row[KEY], row["channel"]): (row["action_id"], list(row["reasons"]), row["variant_arm"])
        for row in decisions.to_dict("records")
    }
    assert rows == {
        ("C1", "sms"): ("winback", [], "control_template"),
        ("C1", "email"): ("winback", [], "control_template"),
        ("C2", "sms"): ("var_a_sms", [], "variant_a"),
        ("C2", "email"): ("var_a_email", [], "variant_a"),
        ("C3", "sms"): ("var_a_sms", ["COMPLIANCE_CONSENT"], "variant_a"),
        ("C3", "email"): ("var_a_email", [], "variant_a"),
        ("C4", "sms"): ("var_b_sms", ["COMPLIANCE_TEMPLATE_MISMATCH"], "variant_b"),
        ("C4", "email"): (None, ["COMPLIANCE_VARIANT_NO_COPY"], "variant_b"),
        ("C5", "sms"): ("var_c_sms", ["COMPLIANCE_VARIANT_UNREGISTERED"], "variant_c"),
        ("C5", "email"): (None, ["COMPLIANCE_VARIANT_NO_COPY"], "variant_c"),
        ("C6", "sms"): ("winback", [], None),
        ("C6", "email"): ("winback", [], None),
    }
    sha = catalogue_sha256(root)
    refs = {(row[KEY], row["channel"]): row["catalogue_ref"] for row in decisions.to_dict("records")}
    assert refs[("C3", "email")] == f"{sha}#var_a_email" and refs[("C4", "email")] is None
    assert refs[("C1", "sms")] == f"{sha}#winback"


def test_the_summary_counts_per_reason_and_per_arm_and_names_the_words(root: Path, tmp_path: Path) -> None:
    outcome, _ = _gate(root, tmp_path, variants=_variants(root), stamp=STAMP)
    summary = outcome.summary
    assert summary.withheld == {
        "COMPLIANCE_TEMPLATE_MISMATCH": 1,
        "COMPLIANCE_VARIANT_NO_COPY": 2,
        "COMPLIANCE_VARIANT_UNREGISTERED": 1,
    }
    assert summary.withheld_rows == 2 and summary.rerouted_rows == 1
    assert summary.withheld_by_arm == {"control_template": 0, "variant_a": 0, "variant_b": 1, "variant_c": 1}
    assert digest(summary.rules) == summary.rule_version
    assert summary.rules["variant_arms"]["batch_id"] == "b1"
    assert summary.rules["variant_arms"]["catalogue_sha256"] == catalogue_sha256(root)
    checked = {(item.arm, item.channel): item for item in summary.variant_messages or ()}
    assert set(checked) == {
        ("variant_a", "sms"),
        ("variant_a", "email"),
        ("variant_b", "sms"),
        ("variant_b", "email"),
        ("variant_c", "sms"),
        ("variant_c", "email"),
    }
    assert checked[("variant_b", "email")].no_copy is True
    # The band's message is checked as M134 checks it, once per action and channel.
    assert {(item.action_id, item.channel) for item in summary.messages} == {
        ("winback", "sms"),
        ("winback", "email"),
    }


def test_a_catalogue_other_than_the_arms_fails_closed(root: Path, tmp_path: Path) -> None:
    outcome, decisions = _gate(
        root,
        tmp_path,
        variants=_variants(root, arms_sha="9" * 64, run_sha=catalogue_sha256(root)),
        stamp=catalogue_sha256(root),
    )
    assert not outcome.treat.any()
    assert outcome.summary.variant_messages == () and outcome.summary.messages == ()
    assert set(outcome.summary.withheld) >= {"COMPLIANCE_CATALOGUE_CHANGED"}
    assert decisions["reasons"].map(lambda value: "COMPLIANCE_CATALOGUE_CHANGED" in list(value)).all()


@pytest.mark.parametrize(
    ("stamp", "run_sha"),
    [
        pytest.param(None, None, id="the run has no stamp"),
        pytest.param(None, STAMP, id="no stamp, arms drawn over one"),
        pytest.param(STAMP, None, id="arms drawn over no stamp"),
        pytest.param(STAMP, "1" * 64, id="arms drawn over another stamp"),
    ],
)
def test_arms_stand_in_for_the_run_s_stamp_only_when_drawn_over_it(
    root: Path, tmp_path: Path, stamp: str | None, run_sha: str | None
) -> None:
    """M134 fails closed on a run with no stamp; the arms' catalogue does not lift that, nor tie another run's.

    The arms' own catalogue is the current one, so only the tie to the run's stamp can fail here.
    """
    outcome, decisions = _gate(root, tmp_path, variants=_variants(root, run_sha=run_sha), stamp=stamp)
    assert not outcome.treat.any()
    assert outcome.summary.messages == () and outcome.summary.variant_messages == ()
    contacted = sum(1 for row in ROWS.values() if row[0])
    assert outcome.summary.withheld["COMPLIANCE_CATALOGUE_CHANGED"] == contacted
    assert decisions["reasons"].map(lambda value: "COMPLIANCE_CATALOGUE_CHANGED" in list(value)).all()
    assert outcome.summary.rules["variant_arms"]["run_catalogue_sha256"] == run_sha


def test_without_arms_a_run_with_no_stamp_still_fails_closed(root: Path, tmp_path: Path) -> None:
    outcome, _ = _gate(root, tmp_path, variants=None, stamp=None)
    assert not outcome.treat.any()
    contacted = sum(1 for row in ROWS.values() if row[0])
    assert outcome.summary.withheld["COMPLIANCE_CATALOGUE_CHANGED"] == contacted


def test_the_summary_counts_the_customers_each_arm_s_words_stopped(root: Path, tmp_path: Path) -> None:
    """B's and C's customers are withheld for their words; a consent-only withholding is not counted."""
    outcome, _ = _gate(root, tmp_path, variants=_variants(root), stamp=STAMP)
    assert outcome.summary.withheld_for_words_by_arm == {
        "control_template": 0,
        "variant_a": 0,
        "variant_b": 1,
        "variant_c": 1,
    }
    # C3 (variant A) reachable on neither channel: withheld for their consent alone, which is not A's words.
    consent = _gate(
        root, tmp_path / "consent", variants=_variants(root), stamp=STAMP, no_email=frozenset({"C3"})
    )
    assert consent[0].summary.withheld_by_arm == {
        "control_template": 0,
        "variant_a": 1,
        "variant_b": 1,
        "variant_c": 1,
    }
    assert consent[0].summary.withheld_for_words_by_arm == {
        "control_template": 0,
        "variant_a": 0,
        "variant_b": 1,
        "variant_c": 1,
    }
    no_email = VariantMessages(
        **{
            **_variants(root).__dict__,
            "words": {
                key: value
                for key, value in WORDS.items()
                if key[0] != "variant_d" and key != ("variant_a", "High", "email")
            },
        }
    )
    words_gone = _gate(root, tmp_path / "words", variants=no_email, stamp=STAMP)[0]
    # Without A's e-mail words, C3 (no SMS consent) meets VARIANT_NO_COPY on e-mail: A's words, so it counts.
    assert words_gone.summary.withheld_for_words_by_arm == {
        "control_template": 0,
        "variant_a": 1,
        "variant_b": 1,
        "variant_c": 1,
    }


def test_without_arms_the_gate_is_m134_s(root: Path, tmp_path: Path) -> None:
    outcome, decisions = _gate(root, tmp_path, variants=None, stamp=catalogue_sha256(root))
    assert "variant_arm" not in decisions.columns
    assert "variant_arms" not in outcome.summary.rules
    assert outcome.summary.variant_messages is None and outcome.summary.withheld_by_arm is None
    assert outcome.summary.withheld_for_words_by_arm is None
    dumped = outcome.summary.model_dump(mode="json")
    assert "variant_messages" not in dumped and "withheld_by_arm" not in dumped
    assert "withheld_for_words_by_arm" not in dumped
    # Every contacted customer gets the band's message, checked as M134 checks it; C3 moves to e-mail.
    assert list(outcome.treat) == [True, True, True, True, True, True, False]
    assert set(decisions["action_id"]) == {"winback"}


# ---------------------------------------------------------------------------
# A list the gate decided before it checked each variant's words is built again
# ---------------------------------------------------------------------------
def test_a_stored_list_with_arms_but_no_variant_check_awaits_the_gate(root: Path, tmp_path: Path) -> None:
    from engine.decide.treat_list import TREAT_LIST_SUMMARY_FILENAME, TreatListSummary, treat_list_awaits_gate

    storage = LocalStorage(tmp_path / "lists")
    base = TreatListSummary(
        run_id=RUN, use_case_id="win-back-campaign", total_rows=7, treat_rows=5, created_at=datetime.now(UTC)
    )
    gated = {"compliance_rule_version": "r" * 64}
    arms = {"variant_arm_counts": {"control_template": 2, "variant_a": 3}}
    cases = {
        "ungated": (base, True),
        "gated, no arms": (base.model_copy(update=gated), False),
        "gated before M145, with arms": (base.model_copy(update={**gated, **arms}), True),
        "gated with the arms' words": (
            base.model_copy(update={**gated, **arms, "variant_arm_withheld": {"variant_a": 0}}),
            False,
        ),
    }
    for name, (summary, expected) in cases.items():
        storage.write_model(run_key(RUN, TREAT_LIST_SUMMARY_FILENAME), summary)
        assert treat_list_awaits_gate(storage, RUN, config_root=root) is expected, name
    set_gate_switch(root, on=False)
    assert (
        treat_list_awaits_gate(storage, RUN, config_root=root) is False
    ), "with the gate off, served as it is"
