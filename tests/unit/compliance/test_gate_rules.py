"""One fixture per rule of the pre-send gate, each against its expected decision file (Plan K M134, DEC-1405).

`decide` is given what the gate is given in a real hand-off, each part produced by the code that produces it:

* consent from per-channel contactability flags - for the consent-basis rules, the flags M131's own code works
  out (`ConsentLedger.classify` with the India rules, through `contactability_masks`) over a real ledger;
* the per-message verdicts from `message_checks` over a real catalogue (`load_catalogue`, with the shipped
  India region file) and a real URL whitelist file;
* the WhatsApp opt-ins from `channel_grants` over a real ledger.

The expected files (`tests/unit/compliance/expected/*.json`) are written by hand: every row, every reason,
every reference. The end-to-end path - a real scoring run, its treat list, the signed ledger entry - is
`tests/integration/compliance/test_gate_real_run.py`.
"""

from __future__ import annotations

import json
import shutil
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
import pyarrow as pa
import pytest

from engine.compliance.dlt import DLT_TAGS
from engine.compliance.gate import GateRules, MessageVerdict, decide
from engine.compliance.handoff import dlt_channels, message_checks
from engine.compliance.optin import channel_grants
from engine.config import ConfigError, UseCaseConfig, load_use_case, load_yaml
from engine.decide.catalogue import catalogue_sha256, clear_catalogue_cache, load_catalogue
from engine.decide.contactability import contactability_masks, contactable_column
from engine.platform_db import sqlite_engine
from engine.privacy.consent import ConsentBasisRules, ConsentLedger
from engine.privacy.contracts import ConsentStatus

EXPECTED = Path(__file__).parent / "expected"
KEYS = ("customer_id",)
RULE_VERSION = "rules-under-test"
CONTACT_REF = "channel_contactability.parquet#sha256=" + "c" * 64
PURPOSE = "marketing_communication"
CLIENT = "acme"
T0 = datetime(2026, 11, 2, 9, 0, tzinfo=UTC)
INDIA = ConsentBasisRules(
    channels=("sms", "call", "ivr"), inquiry_valid_days=7, legacy_requires_operator_registration=True
)

TEMPLATE = "Dear customer, your code {#alphanumeric#} gives Rs {#numeric#} off. Shop at {#url#} - Acme"
MESSAGE_OK = (
    "Dear customer, your code DIWALI50 gives Rs 500 off. Shop at https://shop.example.in/diwali - Acme"
)
MESSAGE_EDITED = MESSAGE_OK.replace(" off.", " off today!")
"""Edited after registration: a literal of the template changed."""
MESSAGE_BAD_LINK = MESSAGE_OK.replace("https://shop.example.in/diwali", "https://bit.ly/x9")
"""The template's shape, with a short link the client has not whitelisted."""
CATALOGUE = f"""\
region: IN
actions:
  - action_id: offer_ok
    label: Diwali offer
    channels: [sms, email]
    dlt_template_id: "1107000000000000001"
    message_category: promotional
    dlt_template_text: "{TEMPLATE}"
    message_text: "{MESSAGE_OK}"
  - action_id: offer_mismatch
    label: Diwali offer, edited after registration
    channels: [sms, email]
    dlt_template_id: "1107000000000000001"
    message_category: promotional
    dlt_template_text: "{TEMPLATE}"
    message_text: "{MESSAGE_EDITED}"
  - action_id: offer_bad_link
    label: Diwali offer with a short link
    channels: [sms]
    dlt_template_id: "1107000000000000001"
    message_category: promotional
    dlt_template_text: "{TEMPLATE}"
    message_text: "{MESSAGE_BAD_LINK}"
  - action_id: offer_unregistered
    label: An offer with no template text
    channels: [sms]
    dlt_template_id: "1107000000000000002"
    message_category: promotional
"""
WHITELIST = "channels: [sms]\nurls: [shop.example.in]\n"


def _customers(rows: list[tuple[str, str | None]]) -> pa.Table:
    return pa.table(
        {
            "customer_id": pa.array([key for key, _ in rows], pa.string()),
            "action_id": pa.array([action for _, action in rows], pa.string()),
        }
    )


def _contactability(keys: list[str], **flags: list[bool]) -> pa.Table:
    """The table as `channel_contactability.parquet` holds it: the key column, then a flag per column."""
    return pa.table(
        {"customer_id": pa.array(keys, pa.string()), **{k: pa.array(v) for k, v in flags.items()}}
    )


def _expected(name: str, **values: str) -> list[dict[str, Any]]:
    text = (EXPECTED / f"{name}.json").read_text(encoding="utf-8")
    for key, value in values.items():
        text = text.replace("{" + key + "}", value)
    loaded: list[dict[str, Any]] = json.loads(text)
    return loaded


def _rules(**changes: Any) -> GateRules:
    return GateRules(
        rule_version=RULE_VERSION,
        purpose=PURPOSE,
        contactability_ref=CONTACT_REF,
        opt_in_channels=("whatsapp",),
        **changes,
    )


@pytest.fixture
def root(tmp_path: Path, config_root: Path) -> Path:
    """A config root with the catalogue above, the shipped India region file and a whitelist for sms."""
    target = tmp_path / "configs"
    (target / "decide").mkdir(parents=True)
    (target / "regions").mkdir()
    (target / "compliance").mkdir()
    shutil.copy(config_root / "regions" / "in.yaml", target / "regions" / "in.yaml")
    (target / "decide" / "catalogue.yaml").write_text(CATALOGUE, encoding="utf-8")
    (target / "compliance" / "url_whitelist.yaml").write_text(WHITELIST, encoding="utf-8")
    clear_catalogue_cache()
    return target


def _verdicts(
    root: Path, planned: dict[str | None, tuple[str, ...]]
) -> dict[tuple[str, str], MessageVerdict]:
    from engine.compliance.config import load_url_whitelist

    catalogue = load_catalogue(root)
    region = load_yaml(root / "regions" / "in.yaml")
    whitelist, _ = load_url_whitelist(root, region)
    verdicts, _checks = message_checks(
        catalogue,
        catalogue_sha256(root),
        planned,
        dlt_channels=dlt_channels(region),
        whitelist=whitelist,
        var_max_chars=30,
    )
    return verdicts


# ---------------------------------------------------------------------------
# Consent
# ---------------------------------------------------------------------------
def test_consent_a_channel_the_customer_is_not_contactable_on_is_refused() -> None:
    """Opted out of SMS, of both, not covered by the file, and a channel the use case does not configure."""
    customers = _customers(
        [("C1", None), ("C2", None), ("C3", None), ("C4", None), ("C5", "push_offer"), ("C6", "push_offer")]
    )
    contactability = _contactability(
        ["C1", "C2", "C3", "C5", "C6"],
        contactable_sms=[True, False, False, True, True],
        contactable_email=[True, True, False, True, True],
        all_channel_consent=[True, True, True, True, False],
    )
    table = decide(
        customers,
        key_columns=KEYS,
        planned={None: ("sms", "email"), "push_offer": ("push",)},
        rules=_rules(),
        contactability=contactability,
    )
    assert table.to_pylist() == _expected("consent")


def _india_flags(tmp_path: Path) -> tuple[list[str], dict[str, np.ndarray]]:
    """M131's per-channel flags over a real ledger, with India's consent-basis rules, through its own code."""
    ledger = ConsentLedger(sqlite_engine(tmp_path / "platform.db"), salt="gate-rules-salt")
    long_ago, recent = T0 - timedelta(days=60), T0 - timedelta(days=3)
    grants = {
        "B1": (long_ago, "explicit", None),
        "B2": (T0 - timedelta(days=10), "inquiry", None),  # lapsed: older than 7 days
        "B3": (recent, "inquiry", None),
        "B4": (long_ago, "legacy", None),  # registration not recorded: not registered
        "B5": (long_ago, "legacy", False),
        "B6": (long_ago, "legacy", True),
    }
    for key, (at, basis, registered) in grants.items():
        ledger.record(
            client_id=CLIENT,
            principal_id=key,
            purpose=PURPOSE,
            status=ConsentStatus.GRANTED,
            source="test",
            recorded_at=at,
            basis=basis,
            operator_registered=registered,
        )
    keys = list(grants)
    base = load_use_case("win-back-campaign")
    document = base.model_dump(mode="json", by_alias=True)
    document["actions"]["suppression"]["channels"] = {"sms": {}, "email": {}}
    config = UseCaseConfig.model_validate(document)
    valid = {
        channel: np.isin(
            keys,
            sorted(ledger.classify(CLIENT, PURPOSE, keys, T0, channel=channel, basis_rules=INDIA).valid),
        )
        for channel in ("sms", "email")
    }
    masks, _ = contactability_masks(
        pd.DataFrame({"customer_id": keys}), None, config, row_key="customer_id", ledger_valid=valid
    )
    return keys, masks


def test_consent_basis_a_lapsed_inquiry_and_an_unregistered_legacy_consent_close_sms_only(
    tmp_path: Path,
) -> None:
    keys, masks = _india_flags(tmp_path)
    contactability = _contactability(keys, **{contactable_column(ch): list(m) for ch, m in masks.items()})
    table = decide(
        _customers([(key, None) for key in keys]),
        key_columns=KEYS,
        planned={None: ("sms", "email")},
        rules=_rules(),
        contactability=contactability,
    )
    assert table.to_pylist() == _expected("consent_basis")


# ---------------------------------------------------------------------------
# The message: an undeclared call, the template, the links
# ---------------------------------------------------------------------------
def test_an_undeclared_automated_call_is_refused_earlier_at_catalogue_load(root: Path) -> None:
    """No decision file: the catalogue never loads, so no run plans the action (M131's rule, kept)."""
    text = CATALOGUE + "  - action_id: offer_call\n    label: Robo call\n    channels: [call]\n"
    (root / "decide" / "catalogue.yaml").write_text(text, encoding="utf-8")
    clear_catalogue_cache()
    with pytest.raises(ConfigError) as caught:
        load_catalogue(root)
    assert caught.value.code == "ACTION_A2P_UNDECLARED"


def test_template_a_message_that_is_not_the_registered_template_is_refused_on_sms(root: Path) -> None:
    planned: dict[str | None, tuple[str, ...]] = {
        None: (),
        "offer_ok": ("sms", "email"),
        "offer_mismatch": ("sms", "email"),
        "offer_unregistered": ("sms",),
    }
    table = decide(
        _customers([("T1", "offer_ok"), ("T2", "offer_mismatch"), ("T3", "offer_unregistered")]),
        key_columns=KEYS,
        planned=planned,
        rules=_rules(),
        contactability=_contactability(
            ["T1", "T2", "T3"], contactable_sms=[True] * 3, contactable_email=[True] * 3
        ),
        messages=_verdicts(root, planned),
    )
    assert table.to_pylist() == _expected("template_mismatch", catalogue_sha256=str(catalogue_sha256(root)))


def test_url_a_link_that_is_not_on_the_whitelist_is_refused(root: Path) -> None:
    planned: dict[str | None, tuple[str, ...]] = {None: (), "offer_ok": ("sms",), "offer_bad_link": ("sms",)}
    table = decide(
        _customers([("U1", "offer_ok"), ("U2", "offer_bad_link")]),
        key_columns=KEYS,
        planned=planned,
        rules=_rules(),
        contactability=_contactability(["U1", "U2"], contactable_sms=[True, True]),
        messages=_verdicts(root, planned),
    )
    assert table.to_pylist() == _expected("url_not_whitelisted", catalogue_sha256=str(catalogue_sha256(root)))


def test_the_message_checks_say_what_was_not_checked_and_why(root: Path) -> None:
    from engine.compliance.config import load_url_whitelist

    region = load_yaml(root / "regions" / "in.yaml")
    whitelist, _ = load_url_whitelist(root, region)
    assert dlt_channels(region) == ("sms",), "the India file requires a DLT template on sms only"
    _, checks = message_checks(
        load_catalogue(root),
        catalogue_sha256(root),
        {None: (), "offer_ok": ("sms", "email"), "offer_unregistered": ("sms",)},
        dlt_channels=dlt_channels(region),
        whitelist=whitelist,
        var_max_chars=30,
    )
    by_pair = {(check.action_id, check.channel): check for check in checks}
    sms = by_pair[("offer_ok", "sms")]
    assert (sms.template_checked, sms.template_mismatch, sms.urls_checked, sms.urls_not_whitelisted) == (
        True,
        False,
        True,
        0,
    )
    email = by_pair[("offer_ok", "email")]
    assert (
        not email.template_checked and email.template_mismatch is None and email.urls_not_whitelisted is None
    )
    assert any("does not require a DLT template on email" in note for note in email.notes)
    assert any("does not cover email" in note for note in email.notes)
    blank = by_pair[("offer_unregistered", "sms")]
    assert not blank.template_checked and not blank.urls_checked
    assert any("no dlt_template_text" in note for note in blank.notes)
    assert DLT_TAGS == ("var", "numeric", "alphanumeric", "url", "cbn")


def test_a_catalogue_with_a_tag_this_product_does_not_know_is_refused_at_load(root: Path) -> None:
    text = CATALOGUE.replace("{#numeric#}", "{#amount#}", 1)
    (root / "decide" / "catalogue.yaml").write_text(text, encoding="utf-8")
    clear_catalogue_cache()
    with pytest.raises(ConfigError) as caught:
        load_catalogue(root)
    assert caught.value.code == "CATALOGUE_INVALID"
    assert "{#amount#}" in caught.value.message


# ---------------------------------------------------------------------------
# WhatsApp opt-in, no channel, a changed catalogue
# ---------------------------------------------------------------------------
def test_whatsapp_without_an_opt_in_record_on_whatsapp_itself_is_refused(tmp_path: Path) -> None:
    ledger = ConsentLedger(sqlite_engine(tmp_path / "platform.db"), salt="gate-rules-salt")
    before, after = T0 - timedelta(days=10), T0 - timedelta(days=1)

    def put(
        key: str, *, channel: str | None, status: str = "granted", at: datetime = before, **extra: Any
    ) -> None:
        ledger.record(
            client_id=CLIENT,
            principal_id=key,
            purpose=PURPOSE,
            status=ConsentStatus(status),
            source="test",
            recorded_at=at,
            channel=channel,
            **extra,
        )

    put("W1", channel=None)  # seq 1: an all-channel grant ...
    put("W1", channel="WhatsApp")  # seq 2: ... and the opt-in, on WhatsApp itself
    put("W2", channel=None)  # seq 3: an all-channel grant is not an opt-in
    put("W3", channel="whatsapp")  # seq 4: an opt-in ...
    put("W3", channel="whatsapp", status="withdrawn", at=after)  # seq 5: ... withdrawn since
    put("W4", channel="whatsapp", expires_at=after)  # seq 6: an opt-in that has expired
    put("W5", channel="sms")  # seq 7: a grant on another channel
    keys = ["W1", "W2", "W3", "W4", "W5"]
    grants = channel_grants(
        ledger, client_id=CLIENT, purpose=PURPOSE, principals=keys, channel="whatsapp", at=T0
    )
    assert grants == {"W1": 2}
    opt_ins = pa.table(
        {
            "customer_id": pa.array(list(grants), pa.string()),
            "channel": pa.array(["whatsapp"] * len(grants), pa.string()),
            "consent_seq": pa.array(list(grants.values()), pa.int64()),
        }
    )
    table = decide(
        _customers([(key, None) for key in keys]),
        key_columns=KEYS,
        planned={None: ("whatsapp", "email")},
        rules=_rules(),
        contactability=_contactability(keys, contactable_email=[True] * 5),
        opt_ins=opt_ins,
    )
    assert table.to_pylist() == _expected("whatsapp_no_opt_in")


def test_no_channel_and_a_changed_catalogue_are_never_allowed() -> None:
    table = decide(
        _customers([("N1", None), ("N2", "offer_ok")]),
        key_columns=KEYS,
        planned={None: (), "offer_ok": ("sms",)},
        rules=_rules(catalogue_changed=True),
        contactability=_contactability(["N1", "N2"], contactable_sms=[True, True]),
    )
    assert table.to_pylist() == _expected("no_channel_catalogue_changed")


def test_rows_join_on_every_key_column_never_by_position() -> None:
    """A composite key, with the contactability file in another order: each row reads its own customer."""
    customers = pa.table(
        {
            "account": pa.array(["A", "A", "B"], pa.string()),
            "snapshot": pa.array(["2026-10-01", "2026-11-01", "2026-10-01"], pa.string()),
            "action_id": pa.array([None, None, None], pa.string()),
        }
    )
    contactability = pa.table(
        {
            "account": pa.array(["B", "A", "A"]),
            "snapshot": pa.array(["2026-10-01", "2026-11-01", "2026-10-01"]),
            "contactable_sms": pa.array([False, True, False]),
        }
    )
    table = decide(
        customers,
        key_columns=("account", "snapshot"),
        planned={None: ("sms",)},
        rules=_rules(),
        contactability=contactability,
    )
    assert table.column("allowed").to_pylist() == [False, True, False]
    assert table.column_names[:2] == ["account", "snapshot"]
