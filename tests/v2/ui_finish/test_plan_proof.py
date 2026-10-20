"""Plan, Proof, Operator, Getting started and Goal after the plan-proof polish (docs/v2/DESIGN.md section 6).

What this pins, and why:
- Plan says "Checked by the editor" ONCE, over the messages the editor passed on every check, so four ready
  messages no longer repeat it (nor their own "Don't use this one" button, now a quiet icon with that description).
  A message with a check that failed or was not run keeps its own line: what needs a look is said on the message.
- Proof's forecast sentence is a whole sentence, and while it is too early to tell, the result card shows what the
  answer waits for from the API's own numbers: the groups, the days each contact is given, and the day the first
  ones count (only when that day is still ahead, never a made-up date in the past).
- Operator on a phone draws 50 decisions with "Show all" for the rest, and keeps the customer lookup above them.
- Getting started and Goal keep their promises after the trim.

The JavaScript half runs in plain node (the helpers need no DOM), and prints JSON for the checks here.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path
from typing import Any, Final

import pytest

ROOT: Final[Path] = Path(__file__).resolve().parents[3]
UI: Final[Path] = ROOT / "ui" / "v2"

SCRIPT: Final[str] = """
const plan = await import("./ui/v2/screens/plan.js");
const proof = await import("./ui/v2/screens/proof.js");
const op = await import("./ui/v2/screens/operator.js");
const all = plan.CHECKS.map((c) => ({ check: c.check, passed: true }));
const review = (over) => ({ content_id: "c", passed: true, reviewed_by: "critic", checks: all, ...over });
const out = {
  everyCheck: plan.passedEveryCheck(review({}), true),
  noFacts: plan.passedEveryCheck(review({}), false),
  oneNotRun: plan.passedEveryCheck(review({ checks: all.slice(1) }), true),
  oneFailed: plan.passedEveryCheck(review({ checks: [{ check: "honesty", passed: false }, ...all.slice(1)] }), true),
  failedVerdict: plan.passedEveryCheck(review({ passed: false }), true),
  unchecked: plan.passedEveryCheck(review({ passed: null, reviewed_by: null }), true),
  listEmail: plan.groupChecklist([{ channel: "email" }]),
  listMixed: plan.groupChecklist([{ channel: "email" }, { channel: "whatsapp" }]),
  ahead: proof.firstCountsOn("2026-10-10T09:00:00Z", 14, "2026-10-20T17:00:00Z"),
  passed: proof.firstCountsOn("2026-09-01T09:00:00Z", 14, "2026-10-20T17:00:00Z"),
  noSince: proof.firstCountsOn(null, 14, "2026-10-20T17:00:00Z"),
  noDays: proof.firstCountsOn("2026-10-10T09:00:00Z", null, "2026-10-20T17:00:00Z"),
  badDays: proof.firstCountsOn("2026-10-10T09:00:00Z", "soon", "2026-10-20T17:00:00Z"),
  phoneRows: op.PHONE_ROWS,
  wide: op.rowsToShow(200, false, false),
  phone: op.rowsToShow(200, true, false),
  phoneAll: op.rowsToShow(200, true, true),
  phoneFew: op.rowsToShow(12, true, false),
  narrowYes: op.isNarrow({ matchMedia: (q) => ({ matches: q.includes("max-width") }) }),
  narrowNo: op.isNarrow({}),
  narrowThrows: op.isNarrow({ matchMedia: () => { throw new Error("no"); } }),
};
console.log(JSON.stringify(out));
"""


def node_command() -> list[str]:
    """`node`, plus the flag that reads ui/v2's `.js` files as ES modules on a node older than 22.7."""
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not installed; the JS half of this test needs it")
    out = subprocess.run([node, "--version"], capture_output=True, text=True, timeout=10, check=False)
    match = re.match(r"v(\d+)\.(\d+)", out.stdout.strip())
    version = (int(match.group(1)), int(match.group(2))) if match else (0, 0)
    if version < (20, 10):
        pytest.skip(f"node {out.stdout.strip()} is too old to load ES modules by syntax")
    return [node] if version >= (22, 7) else [node, "--experimental-detect-module"]


@pytest.fixture(scope="module")
def js() -> dict[str, Any]:
    done = subprocess.run(
        [*node_command(), "--input-type=module", "-e", SCRIPT],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert done.returncode == 0, done.stdout[-2000:] + done.stderr[-2000:]
    result: dict[str, Any] = json.loads(done.stdout.strip().splitlines()[-1])
    return result


def read(name: str) -> str:
    return (UI / "screens" / name).read_text(encoding="utf-8")


def test_only_a_message_passed_on_every_check_joins_the_group(js: dict[str, Any]) -> None:
    assert js["everyCheck"] is True
    # without the plan facts' checklist there is nothing to group by: each message keeps its own line
    assert js["noFacts"] is False
    for case in ("oneNotRun", "oneFailed", "failedVerdict", "unchecked"):
        assert js[case] is False, case


def test_the_group_line_holds_the_whole_checklist_once(js: dict[str, Any]) -> None:
    for words in (
        "On brand",
        "No false claims",
        "No made-up numbers",
        "Unsubscribe line",
        "Follows the channel's rules",
    ):
        assert words in js["listEmail"], words
    assert "not needed" not in js["listEmail"]
    assert "Unsubscribe line (not needed on WhatsApp)" in js["listMixed"]


def test_plan_says_checked_by_the_editor_once_and_turns_off_with_an_icon() -> None:
    src = read("plan.js")
    assert (
        src.count('described("Checked by the editor"') == 2
    ), "one per message that needs a look, one for the group"
    assert "message-group-head" in src
    assert (
        "Don't use this one. Optional: a message that passed the editor needs no approval one by one." in src
    )
    assert (
        '"aria-label": `Don\'t use this one: message ${item.content_id}`' in src
    ), "the icon's words for touch"
    css = read("plan.css")
    assert ".icon-off" in css and "border-radius: 50%" in css, "the icon is drawn in CSS, not an emoji"
    assert not re.search(r"#[0-9a-fA-F]{3,8}\b", css), "plan.css uses the tokens"


def test_plan_says_pauses_the_goal_once_per_group() -> None:
    src = read("plan.js")
    assert '["pause_goal", "pause_channel"].map' in src
    assert "stop-group" in src


def test_first_counts_on_uses_only_the_apis_dates(js: dict[str, Any]) -> None:
    assert js["ahead"] == "2026-10-24T09:00:00.000Z", "since plus the days each contact is given"
    assert js["passed"] is None, "a day already past is never shown as something to wait for"
    assert js["noSince"] is None and js["noDays"] is None
    assert js["badDays"] is None, "a horizon that is not a number never throws or makes a date up"


def test_proof_forecast_is_one_whole_sentence() -> None:
    src = read("proof.js")
    assert "Over the ${" not in src, "the old fragment is gone"
    assert (
        "`Its forecast covers the ${count(expected.horizon_days)} days after each contact it decided on${since}.`"
        in src
    )
    assert "nothing was sent, because this goal is in Shadow mode." in src


def test_proof_too_early_card_shows_what_it_waits_for() -> None:
    src = read("proof.js")
    for label in (
        "The agent decides",
        "The simple rule decides",
        "People we never contact",
        "Each contact counts after",
        "Spend so far",
    ):
        assert f'described("{label}"' in src, label
    assert "pf-early-strip" in src
    # said once: the folded method card leaves its group counts out when the result card shows them
    assert "methodCard(proof, early)" in src and "!groupsShown" in src


def test_operator_draws_fifty_on_a_phone(js: dict[str, Any]) -> None:
    assert js["phoneRows"] == 50
    assert js["wide"] == 200 and js["phone"] == 50 and js["phoneAll"] == 200 and js["phoneFew"] == 12
    assert js["narrowYes"] is True and js["narrowNo"] is False and js["narrowThrows"] is False
    src = read("operator.js")
    assert "Show all ${count(matching.length)}" in src
    # the customer lookup is added to the grid before the decisions card, so it stays above the long list
    assert src.index('card("Customer lookup"') < src.index('card("Decisions"')


def test_getting_started_keeps_its_promises() -> None:
    src = read("onboarding.js")
    assert '"Getting started is safe: it watches before it sends."' in src
    assert "not yet run with a real business" in src
    assert '"You stay in control"' in src
    assert "You can always lower it." in src
    assert "Step ${at} of 5" not in src, "the current step's pill says where you are"
    assert "3–10%" in src and "not a number about your shop" in src


def test_goal_keeps_its_promises() -> None:
    src = read("goal.js")
    assert "Say who, what you want them to do, and how much to spend." in src, "now the label's (i)"
    assert "Filled in when your words name an amount." in src, "now the budget's (i)"
    assert '"Every new goal starts in Shadow."' in src
    assert "Nothing is sent yet." in src, "in that line's description"
    assert "Want me to handle them?" in src and "Or pick one it found for you" in src
