"""`check_before_contact`: the pre-send compliance gate's decision summary for a planned list, counts only (Plan K
M141, DEC-1408 (f)).

The run and its gated list are M134's own (`tests/integration/compliance/test_gate_real_run.py`: a real uplift
scoring run, a consent ledger with WhatsApp grants for every third customer, a list built through
`build_treat_list` with the gate on). The tool reads what the gate wrote and must report it exactly - customers
allowed, moved and withheld, by reason, with the rule version and the gate's signed ledger entry - never a row:
no customer key appears in it. Over MCP every count of 1 to 9 is withheld (`api.mcp.suppress`). A list built with
the gate off, and a run with no list yet, are answered `checked: false` with the reason, never with zeros.

On the unchanged code there is no `check_before_contact` tool: these tests fail there.
"""

# ruff: noqa: F811, F401 - the imported pytest fixtures are used by name
from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any

import pytest

from api.mcp.suppress import suppress
from engine.access.roles import Principal, Role
from engine.agentic.registry import ToolContext
from engine.agentic.tools import default_registry
from engine.storage import LocalStorage, run_key
from tests.integration.compliance.test_gate_real_run import (
    DECISIONS_JSON,
    RUN_GATE,
    Built,
    Gated,
    _build,
    gated,
    on,
)
from tests.integration.decide.test_channel_consent_real_run import PRIMARY_KEY, world

pytestmark = pytest.mark.integration

VIEWER = Principal(user_id="u_vera", username="vera", roles=frozenset({Role.VIEWER}), kind="user")


def _call(storage: LocalStorage, gated: Gated, variant: str = "gate_on") -> dict[str, Any]:
    ctx = ToolContext(
        storage=storage,
        principal=VIEWER,
        config_root=gated.world.roots[variant],
        clock=lambda: datetime(2026, 10, 11, tzinfo=UTC),
    )
    result = default_registry().call("check_before_contact", {"run_id": RUN_GATE}, ctx)
    assert result.label == "Descriptive"
    return result.output


def test_the_gates_counts_for_the_planned_list_exactly_and_no_row(gated: Gated, on: Built) -> None:
    out = _call(on.storage, gated)
    stored = json.loads(on.storage.read_text(run_key(RUN_GATE, DECISIONS_JSON)))
    assert out["checked"] is True and out["reason"] is None and out["purpose"] == stored["purpose"]
    assert out["counts"] == {
        "customers": stored["customers"],
        "sent_rows": stored["customers"] - stored["withheld_rows"],
        "withheld_rows": stored["withheld_rows"],
        "rerouted_rows": stored["rerouted_rows"],
        "rows_without_consent_evidence": stored["rows_without_consent_evidence"],
    }
    assert out["counts"]["sent_rows"] == on.summary.treat_rows, "what the list sends is what the list says"
    assert out["withheld"] == stored["withheld"] == on.summary.compliance_withheld
    assert (out["rule_version"], out["decisions_sha256"]) == (
        stored["rule_version"],
        stored["decisions_sha256"],
    )
    assert (out["gate_evidence_id"], out["gate_ledger_entry_hash"]) == (
        stored["evidence_id"],
        stored["ledger_entry_hash"],
    )
    keys = set(on.out[PRIMARY_KEY].astype(str))
    assert not {value for value in json.dumps(out).split('"') if value in keys}, "no customer key, ever"

    # Over MCP a count of 1 to 9 is a small group: withheld, with the reasons that add up with it.
    served, withheld = suppress("check_before_contact", out)
    small = [value for value in stored["withheld"].values() if 0 < value < 10]
    if 0 < stored["withheld_rows"] < 10:
        assert served["counts"] is None and served["withheld"] == {} and withheld
    elif small:
        assert set(served["withheld"]) < set(stored["withheld"]) and withheld
    else:
        assert served == out and withheld == {}


def test_a_list_built_with_the_gate_off_or_not_built_is_not_checked(gated: Gated) -> None:
    off = _build(gated, "built_off_m141", "gate_off")
    out = _call(off.storage, gated, "gate_off")
    assert out["checked"] is False and out["counts"] is None and out["withheld"] == {}
    assert "gate off" in out["reason"]
    unbuilt = _call(LocalStorage(gated.snapshot), gated)
    assert unbuilt["checked"] is False and "No list has been built" in unbuilt["reason"]
