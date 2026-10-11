"""Take the screenshot set for visual review (Plan M, M181 part A).

    python -m scripts.ui_screenshots OUT_DIR                      # both layouts, desktop and phone, replayed demo
    python -m scripts.ui_screenshots OUT_DIR --layouts classic    # one layout
    python -m scripts.ui_screenshots OUT_DIR --pages overview,wizard
    python -m scripts.ui_screenshots OUT_DIR --base http://127.0.0.1:8771 --layouts campaigns

Every main page in each layout, at 1440x900 (desktop) and 390x844 (phone), full page height, into OUT_DIR as
`<layout>-<page>-<desktop|phone>.png`, plus `INDEX.md` listing them, the pages that drew nothing, the console errors a
page raised and the pages that scroll sideways on the phone. OUT_DIR is whatever folder you name; nothing is written
anywhere else and the folder is not meant to be committed.

By default the real `ui/` is served with the API answers recorded from the validated Hillstrom demo
(`tests/fixtures/ui_replay.py`), so it needs no seeded store and no network. `--base URL` photographs a running
application instead (for example one serving `scripts/seed_validated.py`'s store); the layout it shows is the one it
is configured with (`MARKETING_AI_UI_LAYOUT`), so name it with `--layouts` to label the files. The campaigns layout
exists once `ui/layout.js` (M174) is in the tree; without it that layout is skipped, and says so.

Needs playwright and a Chromium (`PLAYWRIGHT_BROWSERS_PATH=/opt/pw-browsers` where the machine keeps it).
`docs/ux/USABILITY_TEST.md` says when a reviewer uses this set.
"""

from __future__ import annotations

import argparse
import os
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Any, Final

from tests.fixtures.ui_replay import ReplayServer, has_campaigns_layout

SIZES: Final[dict[str, tuple[int, int]]] = {"desktop": (1440, 900), "phone": (390, 844)}

TRAIN_RISK: Final[str] = "r_20261010_11100001"
SCORE_RISK: Final[str] = "r_20261010_11100004"
CAMPAIGN: Final[str] = "c_20261011_c9276210"

# (page name, layout, hash). The names are the same across layouts where the page is the same page.
PAGES: Final[tuple[tuple[str, str, str], ...]] = (
    ("home", "classic", "#/"),
    ("results", "classic", "#/results"),
    ("goal-setup", "classic", "#/uc/hillstrom-email"),
    ("scored-run", "classic", f"#/uc/hillstrom-email/run/{SCORE_RISK}"),
    ("contact-list", "classic", f"#/uc/hillstrom-email/output/{SCORE_RISK}"),
    ("campaign", "classic", f"#/campaigns/{CAMPAIGN}"),
    ("reports", "classic", "#/reports"),
    ("connections", "classic", "#/connections"),
    ("settings", "classic", "#/settings"),
    ("glossary", "classic", "#/help/glossary"),
    ("overview", "campaigns", "#/overview"),
    ("campaigns", "campaigns", "#/campaigns"),
    ("wizard", "campaigns", "#/campaigns/new"),
    ("campaign", "campaigns", f"#/campaigns/{CAMPAIGN}"),
    ("data", "campaigns", "#/data"),
    ("reports", "campaigns", "#/reports"),
    ("settings", "campaigns", "#/settings"),
    ("glossary", "campaigns", "#/help/glossary"),
)

SETTLE_MS: Final[int] = 2000


def parse(argv: Sequence[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="python -m scripts.ui_screenshots", description=__doc__.split("\n\n")[0]
    )
    parser.add_argument(
        "out_dir", type=Path, help="the folder the PNGs and INDEX.md go to (created if absent)"
    )
    parser.add_argument("--layouts", default="classic,campaigns", help="comma-separated: classic, campaigns")
    parser.add_argument(
        "--pages", default="", help="comma-separated page names to take (default: every page)"
    )
    parser.add_argument(
        "--base", default="", help="photograph a running application at this URL instead of the replay"
    )
    return parser.parse_args(argv)


def capture(
    browser: Any, base: str, out_dir: Path, layout: str, names: set[str], *, offline: bool
) -> list[dict[str, Any]]:
    """Take the pages of one layout at both sizes; return one record per picture."""
    records: list[dict[str, Any]] = []
    for name, page_layout, fragment in PAGES:
        if page_layout != layout or (names and name not in names):
            continue
        for size, (width, height) in SIZES.items():
            context = browser.new_context(viewport={"width": width, "height": height})
            if offline:  # the replay: nothing but the replay server is reachable (no web fonts either)
                context.route(
                    "**/*",
                    lambda route: route.continue_() if route.request.url.startswith(base) else route.abort(),
                )
            page = context.new_page()
            errors: list[str] = []
            page.on("console", lambda m, errors=errors: errors.append(m.text) if m.type == "error" else None)
            page.on("pageerror", lambda e, errors=errors: errors.append(str(e)))
            page.goto(f"{base}/ui/{fragment}", wait_until="load")
            page.wait_for_timeout(SETTLE_MS)
            file = f"{layout}-{name}-{size}.png"
            page.screenshot(path=str(out_dir / file), full_page=True)
            sideways = bool(
                page.evaluate("() => document.documentElement.scrollWidth > window.innerWidth + 1")
            )
            blank = not page.evaluate(
                "() => (document.querySelector('#app') || document.body).innerText.trim().length > 20"
            )
            records.append(
                {
                    "file": file,
                    "layout": layout,
                    "page": name,
                    "size": size,
                    "hash": fragment,
                    "sideways": sideways and size == "phone",
                    "blank": blank,
                    "errors": [e for e in errors if "Failed to load resource" not in e][:3],
                }
            )
            context.close()
    return records


def write_index(out_dir: Path, records: list[dict[str, Any]], skipped: list[str]) -> Path:
    lines = ["# Screenshot set", "", "Made by `python -m scripts.ui_screenshots`. Not for committing.", ""]
    lines += ["| Picture | Address | Notes |", "|---|---|---|"]
    for r in records:
        notes = []
        if r["blank"]:
            notes.append("drew nothing")
        if r["sideways"]:
            notes.append("scrolls sideways on the phone")
        if r["errors"]:
            notes.append("console: " + "; ".join(e[:80] for e in r["errors"]))
        lines.append(f"| `{r['file']}` | `{r['hash']}` | {', '.join(notes) or 'ok'} |")
    if skipped:
        lines += ["", "## Skipped", "", *[f"- {s}" for s in skipped]]
    index = out_dir / "INDEX.md"
    index.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return index


def main(argv: Sequence[str] | None = None) -> int:
    args = parse(argv)
    layouts = [x.strip() for x in args.layouts.split(",") if x.strip()]
    unknown = [x for x in layouts if x not in {"classic", "campaigns"}]
    if unknown:
        print(f"unknown layout: {', '.join(unknown)} (use classic or campaigns)", file=sys.stderr)
        return 2
    names = {x.strip() for x in args.pages.split(",") if x.strip()}
    out_dir: Path = args.out_dir
    out_dir.mkdir(parents=True, exist_ok=True)

    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        print(
            "playwright is not installed; pip install playwright and set PLAYWRIGHT_BROWSERS_PATH",
            file=sys.stderr,
        )
        return 2

    skipped: list[str] = []
    records: list[dict[str, Any]] = []
    chromium = Path(os.environ.get("PLAYWRIGHT_BROWSERS_PATH", "/opt/pw-browsers")) / "chromium"
    with sync_playwright() as p:
        launch: dict[str, Any] = {"executable_path": str(chromium)} if chromium.exists() else {}
        browser = p.chromium.launch(**launch)
        for layout in layouts:
            if layout == "campaigns" and not args.base and not has_campaigns_layout():
                skipped.append("campaigns layout: ui/layout.js (Plan M M174) is not in this tree")
                print(skipped[-1])
                continue
            if args.base:
                records += capture(browser, args.base.rstrip("/"), out_dir, layout, names, offline=False)
            else:
                with ReplayServer(ui_layout=layout) as server:
                    records += capture(browser, server.base, out_dir, layout, names, offline=True)
        browser.close()
    index = write_index(out_dir, records, skipped)
    print(f"{len(records)} pictures and {index}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
