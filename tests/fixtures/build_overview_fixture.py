"""Build the recorded answer of `GET /overview` for the visible-text check and the Overview tests (Plan M, M175).

`tests/fixtures/capture_ui_demo.py` records what the pages of the app on the validated Hillstrom demo asked for. The
Overview page asks for one more answer, `GET /overview`, which is not in a recording made before M175 existed. The
answer is *assembled*, never invented: `engine.measurement.overview.build_overview` is run over the answers the same
recording already holds, `GET /campaigns/summary`, `GET /campaigns` and each campaign's `GET /campaigns/<id>/plan`,
which is exactly what the route reads. "A connection or an upload exists" is read from the same recording
(`GET /connections`, or any `GET /uploads/<id>/...` answer), never assumed.

    python -m tests.fixtures.build_overview_fixture            # add or refresh the key in tests/fixtures/ui_demo/api.json
    python -m tests.fixtures.build_overview_fixture --check    # exit 1 when the recorded key is not what the builder gives

`tests/integration/overview/test_overview_fixture.py` runs the check, so the recording cannot drift from the code that
builds the answer. A re-recording with `capture_ui_demo` after the route exists records the real answer instead; the
check then compares the two, which is the cross-check this builder exists for.
"""

from __future__ import annotations

import json
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Final

from engine.measurement.campaign import Campaign
from engine.measurement.overview import Overview, build_overview
from engine.measurement.plan import TestPlan
from engine.measurement.summary import CampaignSummary

RECORDING: Final[Path] = Path(__file__).resolve().parent / "ui_demo" / "api.json"
KEY: Final[str] = "GET /overview"
BUILT_AT: Final[datetime] = datetime(2026, 10, 11, 12, 0, tzinfo=UTC)
"""The moment the demo answer is "built": after the demo's two campaigns were measured (11 October 2026)."""


def overview_from(recorded: dict[str, Any]) -> Overview:
    """The Overview the route would answer on the recorded demo, from the other answers of the recording."""
    summary = CampaignSummary.model_validate(recorded["GET /campaigns/summary"]["body"])
    campaigns = tuple(Campaign.model_validate(c) for c in recorded["GET /campaigns"]["body"]["campaigns"])

    def plan_of(campaign_id: str) -> TestPlan | None:
        answer = recorded.get(f"GET /campaigns/{campaign_id}/plan")
        plan = answer["body"].get("plan") if answer and answer["status"] == 200 else None
        return TestPlan.model_validate(plan) if plan else None

    # "A connection or an upload exists", read as the route reads it: saved connections, else uploaded files. The recording
    # holds `GET /connections` and one `GET /uploads/<id>/ask` per upload the app was asked about.
    connected = bool(recorded.get("GET /connections", {}).get("body", {}).get("connections")) or any(
        key.startswith("GET /uploads/") for key in recorded
    )
    return build_overview(
        summary=summary, campaigns=campaigns, plan_of=plan_of, connected=connected, now=BUILT_AT
    )


def answer_for(recorded: dict[str, Any]) -> dict[str, Any]:
    """The recording's entry for `GET /overview`."""
    body = json.loads(overview_from(recorded).model_dump_json())
    return {"status": 200, "content_type": "application/json", "body": body}


def render(recorded: dict[str, Any]) -> str:
    """The recording as the capture script writes it: sorted keys, one space of indent."""
    return json.dumps(dict(sorted(recorded.items())), indent=1, sort_keys=False) + "\n"


def main(argv: list[str]) -> int:
    recorded: dict[str, Any] = json.loads(RECORDING.read_text(encoding="utf-8"))
    built = answer_for(recorded)
    if "--check" in argv:
        same = recorded.get(KEY) == built
        print("up to date" if same else f"{KEY} in {RECORDING.name} differs from what the builder gives")
        return 0 if same else 1
    recorded[KEY] = built
    RECORDING.write_text(render(recorded), encoding="utf-8")
    print(f"{KEY} written to {RECORDING}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
