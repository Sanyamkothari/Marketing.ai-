"""Record the API answers behind every main page of the app running on the validated Hillstrom demo (Plan M, M170).

The visible-text check (`tests/integration/production/ui/glossary/visible_text.test.mjs`) draws the real pages in jsdom
against a *replayed* API, so a change to a page's words is judged against exactly what a marketer's screen is made of.
This script is how that replay file, `tests/fixtures/ui_demo/api.json`, was made. It is a developer tool, run by hand
when the demo changes; the tests never run it.

    # 1. seed the validated demo store (see scripts/seed_validated.py) and serve it
    MARKETING_AI_DATA_DIR=<store>/data/hillstrom-email/journey/data uvicorn api.main:app --port 8771
    # 2. visit every page in Chromium and record every API answer the page asked for
    PLAYWRIGHT_BROWSERS_PATH=/opt/pw-browsers python -m tests.fixtures.capture_ui_demo http://127.0.0.1:8771

Only GET answers are kept (a page never needs a write to draw itself), keyed `GET <path>?<sorted query>`, with the
status, the content type and the body (JSON as JSON, anything else as text). A request that failed (a real 404 that a
page turns into its empty state) is kept too, so the page draws the same empty state it does for a person. The demo
data is the public Hillstrom e-mail experiment; no customer's data is in the file.
"""

from __future__ import annotations

import contextlib
import json
import sys
from pathlib import Path
from typing import Any, Final
from urllib.parse import parse_qsl, urlencode, urlparse

OUT: Final[Path] = Path(__file__).resolve().parent / "ui_demo" / "api.json"

USE_CASE: Final[str] = "hillstrom-email"
TRAIN_RISK: Final[str] = "r_20261010_11100001"
TRAIN_UPLIFT: Final[str] = "r_20261010_11100002"
SCORE_UPLIFT: Final[str] = "r_20261010_11100003"
SCORE_RISK: Final[str] = "r_20261010_11100004"
CAMPAIGN_CONVERSION: Final[str] = "c_20261011_c9276210"
CAMPAIGN_SPEND: Final[str] = "c_20261011_5ee8a5d4"

PAGES: Final[list[tuple[str, tuple[str, ...]]]] = [
    ("#/", ()),
    ("#/results", ()),
    ("#/settings", ()),
    ("#/connections", ()),
    ("#/data", ()),
    ("#/data/check", ()),
    ("#/doctor", ()),
    ("#/uc/win-back-campaign", ()),
    ("#/uc/win-back-campaign", ("Manual setup",)),
    ("#/uc/win-back-campaign", ("Manual setup", "Score new data")),
    (f"#/uc/{USE_CASE}", ()),
    (f"#/uc/{USE_CASE}", ("Manual setup",)),
    (f"#/uc/{USE_CASE}/run/{TRAIN_RISK}", ()),
    (f"#/uc/{USE_CASE}/data/{TRAIN_RISK}", ()),
    (f"#/uc/{USE_CASE}/model/{TRAIN_RISK}", ()),
    (f"#/uc/{USE_CASE}/output/{TRAIN_RISK}", ()),
    (f"#/uc/{USE_CASE}/run/{SCORE_RISK}", ()),
    (f"#/uc/{USE_CASE}/data/{SCORE_RISK}", ()),
    (f"#/uc/{USE_CASE}/model/{SCORE_RISK}", ()),
    (f"#/uc/{USE_CASE}/output/{SCORE_RISK}", ()),
    (f"#/campaign/{USE_CASE}/{SCORE_RISK}", ()),
    (f"#/uc/{USE_CASE}/run/{TRAIN_UPLIFT}", ()),
    (f"#/uc/{USE_CASE}/data/{TRAIN_UPLIFT}", ()),
    (f"#/uplift/{USE_CASE}/model/{TRAIN_UPLIFT}", ()),
    (f"#/uplift/{USE_CASE}/output/{TRAIN_UPLIFT}", ()),
    (f"#/uc/{USE_CASE}/run/{SCORE_UPLIFT}", ()),
    (f"#/uc/{USE_CASE}/data/{SCORE_UPLIFT}", ()),
    (f"#/uc/{USE_CASE}/output/{SCORE_UPLIFT}", ()),
    (f"#/campaigns/{CAMPAIGN_CONVERSION}", ()),
    (f"#/campaigns/{CAMPAIGN_SPEND}", ()),
    (f"#/pilot/proof/{CAMPAIGN_CONVERSION}", ()),
    (f"#/pilot/proof/{CAMPAIGN_SPEND}", ()),
    ("#/pilot", ()),
    (f"#/pilot/value/{SCORE_RISK}", ()),
    ("#/uplift", ()),
    ("#/monitoring/runs", ()),
    (f"#/monitoring/runs/{SCORE_RISK}", ()),
    ("#/monitoring/alerts", ()),
    ("#/monitoring/schedules", ()),
    ("#/audit", ()),
    ("#/cost", ()),
    ("#/approvals", ()),
    ("#/pilot/kit", ()),
]


def key_of(method: str, url: str) -> str:
    """`GET /path?a=1&b=2` with the query sorted, so the replay does not depend on parameter order."""
    parsed = urlparse(url)
    query = urlencode(sorted(parse_qsl(parsed.query, keep_blank_values=True)))
    return f"{method.upper()} {parsed.path}" + (f"?{query}" if query else "")


def main(base: str) -> None:
    from playwright.sync_api import sync_playwright

    recorded: dict[str, Any] = {}
    with sync_playwright() as p:
        browser = p.chromium.launch(executable_path="/opt/pw-browsers/chromium")
        for fragment, clicks in PAGES:
            context = browser.new_context(viewport={"width": 1440, "height": 900})
            page = context.new_page()

            def on_response(response: Any) -> None:
                request = response.request
                if request.resource_type not in {"fetch", "xhr"} or request.method != "GET":
                    return
                path = urlparse(response.url).path
                if path.startswith("/ui") or path.endswith((".pdf", ".png", ".ico")):
                    return
                content_type = response.headers.get("content-type", "")
                try:
                    body: Any = response.json() if "json" in content_type else response.text()
                except Exception:  # a body the browser has already dropped
                    return
                recorded[key_of("GET", response.url)] = {
                    "status": response.status,
                    "content_type": content_type.split(";")[0],
                    "body": body,
                }

            page.on("response", on_response)
            page.goto(f"{base}/ui{fragment}", wait_until="load")
            page.wait_for_timeout(2500)
            for label in clicks:
                page.get_by_text(label, exact=True).first.click()
                page.wait_for_timeout(1200)
            with contextlib.suppress(Exception):  # a page that polls never goes idle
                page.wait_for_load_state("networkidle", timeout=8000)
            context.close()
        browser.close()
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(
        json.dumps(dict(sorted(recorded.items())), indent=1, sort_keys=False) + "\n", encoding="utf-8"
    )
    size = OUT.stat().st_size
    print(f"{len(recorded)} answers, {size / 1e6:.2f} MB -> {OUT}")


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8771")
