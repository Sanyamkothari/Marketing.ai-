"""Take the screenshots `docs/START_HERE.md` shows (Plan H M84).

    python -m scripts.capture_start_here [--out docs/screenshots/start_here] [--no-train]

Starts the app on a free local port with a fresh, empty data folder (a temporary directory that is
deleted afterwards), drives it the way a new user would, and writes 1280x800 PNGs:

1. Home, Connections (empty) and the PostgreSQL set-up form - before anything exists.
2. A use case's Guided setup, with a real helper session: a synthetic, deliberately messy file for
   Targeted Advertisement is uploaded through the page's own file input.
3. Unless `--no-train`: one fast training run and one scoring run through the API (a minute or two),
   then Results, the scoring run's results and its "4 Measure the campaign" step.
4. Settings, with Advanced tools opened.

Every row is synthetic (`tests/fixtures/make_data.py`): no real person, no secret, no personal data.
Needs the dev extra (Playwright for Python) and a Chromium that Playwright can find
(`PLAYWRIGHT_BROWSERS_PATH`); it never downloads one.
"""

from __future__ import annotations

import argparse
import contextlib
import io
import socket
import sys
import tempfile
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Final

import httpx
import numpy as np
import pandas as pd
import uvicorn
from PIL import Image
from playwright.sync_api import Page, ViewportSize, sync_playwright
from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
from tests.fixtures.make_data import GenerationSpec, generate

from api.main import create_app

ROOT: Final[Path] = Path(__file__).resolve().parent.parent
DEFAULT_OUT: Final[Path] = ROOT / "docs" / "screenshots" / "start_here"
VIEWPORT: Final[ViewportSize] = {"width": 1280, "height": 800}

USE_CASE: Final[str] = "targeted-advertisement"
PRIMARY_KEY: Final[str] = "customer_id"
TARGET: Final[str] = "converted_30d"
FAST: Final[dict[str, Any]] = {
    "model_search": {"time_limit_minutes": 1, "strategy": "fast", "tuning_trials": 5},
}
"""The integration tests' one-minute search: enough for a real model, quick enough for a script."""
RUN_TIMEOUT_S: Final[float] = 900.0


# --- the server ------------------------------------------------------------------------------------


def free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


@contextmanager
def serve(data_dir: Path) -> Iterator[str]:
    """The app on a free port in this process, on `data_dir`; yields its base URL."""
    port = free_port()
    config = uvicorn.Config(create_app(data_dir=data_dir), host="127.0.0.1", port=port, log_level="warning")
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.monotonic() + 60
    while not server.started:
        if time.monotonic() > deadline or not thread.is_alive():
            raise RuntimeError("the app did not start")
        time.sleep(0.1)
    try:
        yield f"http://127.0.0.1:{port}"
    finally:
        server.should_exit = True
        thread.join(timeout=30)


# --- synthetic data --------------------------------------------------------------------------------


def messy_file(rows: int = 2_000, seed: int = 7) -> bytes:
    """Synthetic training data written the way people write it, so the helper has work to show:
    percentages and money as text, yes/no in four spellings, one category in three cases, and the
    generator's own column that gives the answer away."""
    frame = generate(GenerationSpec(use_case_id=USE_CASE, rows=rows, variant="leaky_column"))
    rng = np.random.default_rng(seed)
    frame["ad_ctr_90d"] = [f"{v * 100:.1f}%" if pd.notna(v) else None for v in frame["ad_ctr_90d"]]
    spend = rng.gamma(2.0, 600.0, size=rows)
    frame["monthly_spend"] = [f"Rs. {value:,.2f}" for value in spend]
    frame["is_premium"] = rng.choice(["Y", "yes", "N", "No"], size=rows)
    frame["plan_tier"] = [
        value.upper() if i % 3 == 0 else value.title() if i % 3 == 1 else value
        for i, value in enumerate(frame["plan_tier"].astype(str))
    ]
    return frame.to_csv(index=False, lineterminator="\n").encode()


def clean_file(rows: int, seed: int, *, with_target: bool) -> bytes:
    frame = generate(GenerationSpec(use_case_id=USE_CASE, rows=rows, seed=seed))
    if not with_target:
        frame = frame.drop(columns=[TARGET])
    return frame.to_csv(index=False, lineterminator="\n").encode()


# --- the API ---------------------------------------------------------------------------------------


def upload(api: httpx.Client, payload: bytes, *, mode: str) -> str:
    response = api.post(
        "/uploads",
        files={"file": (f"synthetic_{mode}.csv", payload, "text/csv")},
        data={"use_case": USE_CASE, "mode": mode},
    )
    response.raise_for_status()
    return str(response.json()["upload_id"])


def wait_for(api: httpx.Client, run_id: str) -> dict[str, Any]:
    deadline = time.monotonic() + RUN_TIMEOUT_S
    while time.monotonic() < deadline:
        body: dict[str, Any] = api.get(f"/runs/{run_id}").json()
        run: dict[str, Any] = body.get("run", body)
        if run.get("state") in {"done", "failed", "cancelled"}:
            if run.get("state") != "done":
                raise RuntimeError(f"run {run_id} ended {run.get('state')}: {run.get('error')}")
            return run
        time.sleep(3)
    raise RuntimeError(f"run {run_id} did not finish in {RUN_TIMEOUT_S:.0f}s")


def train_and_score(base: str) -> str:
    """One fast training run, then a scoring run with the model it made; returns the scoring run's id."""
    with httpx.Client(base_url=base, timeout=120) as api:
        train_upload = upload(api, clean_file(3_000, 11, with_target=True), mode="train")
        started = api.post(
            "/runs",
            json={
                "use_case": USE_CASE,
                "mode": "train",
                "upload_id": train_upload,
                "primary_key": PRIMARY_KEY,
                "target": TARGET,
                "overrides": FAST,
            },
        )
        started.raise_for_status()
        trained = wait_for(api, str(started.json()["run_id"]))
        score_upload = upload(api, clean_file(1_500, 23, with_target=False), mode="score")
        scored = api.post(
            "/runs",
            json={
                "use_case": USE_CASE,
                "mode": "score",
                "upload_id": score_upload,
                "primary_key": PRIMARY_KEY,
                "model_version_id": trained.get("model_version_id"),
            },
        )
        scored.raise_for_status()
        return str(wait_for(api, str(scored.json()["run_id"]))["run_id"])


# --- the browser -----------------------------------------------------------------------------------


def settle(page: Page, ms: int = 800) -> None:
    # A page that keeps polling (a run's results) never goes quiet; it is still ready to be shot.
    with contextlib.suppress(PlaywrightTimeoutError):
        page.wait_for_load_state("networkidle", timeout=20_000)
    page.wait_for_timeout(ms)


def shoot(page: Page, out: Path, name: str) -> None:
    """A 1280x800 PNG, reduced to a 256-colour palette so the docs stay small."""
    raw = page.screenshot(full_page=False)
    with Image.open(io.BytesIO(raw)) as image:
        small = image.convert("RGB").quantize(colors=256, method=Image.Quantize.MEDIANCUT)
        small.save(out / f"{name}.png", optimize=True)
    print(f"  {name}.png")


def scroll_to(page: Page, selector: str, offset: int = 90) -> None:
    page.evaluate(
        "([sel, off]) => { const el = document.querySelector(sel);"
        " if (el) window.scrollTo(0, el.getBoundingClientRect().top + window.scrollY - off); }",
        [selector, offset],
    )
    page.wait_for_timeout(300)


def go(page: Page, base: str, hash_: str) -> None:
    page.goto(f"{base}/ui/{hash_}")
    settle(page)


def capture(base: str, out: Path, *, train: bool, messy: Path) -> None:
    with sync_playwright() as pw:
        browser = pw.chromium.launch()
        context = browser.new_context(viewport=VIEWPORT, color_scheme="light")
        context.add_init_script(
            "try { localStorage.setItem('marketing-ai:pilot-tour-seen', '1'); } catch (e) {}"
        )
        page = context.new_page()

        go(page, base, "#/")
        shoot(page, out, "01_home")
        go(page, base, "#/connections")
        shoot(page, out, "02_connections_empty")
        scroll_to(page, "#cn-add")
        shoot(page, out, "03_connections_add")
        go(page, base, "#/connections/new/postgres")
        shoot(page, out, "04_connection_form")

        go(page, base, f"#/uc/{USE_CASE}")
        shoot(page, out, "05_guided_setup_start")
        page.set_input_files("#ag-file", str(messy))
        page.wait_for_selector("[data-ag-group], [data-ag-stop]", timeout=120_000)
        settle(page)
        scroll_to(page, "[data-ag-group]")
        shoot(page, out, "06_guided_setup_suggestions")

        if train:
            print("  training and scoring (a minute or two)...")
            score_run = train_and_score(base)
            go(page, base, "#/results")
            shoot(page, out, "07_results")
            go(page, base, f"#/uc/{USE_CASE}/run/{score_run}")
            shoot(page, out, "08_run_results")
            page.wait_for_selector("#measure-step", timeout=30_000)
            scroll_to(page, "#measure-step")
            shoot(page, out, "09_measure_campaign")

        go(page, base, "#/settings")
        page.click("[data-settings-advanced] summary")
        page.wait_for_timeout(300)
        shoot(page, out, "10_settings")
        browser.close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0] if __doc__ else None)
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT, help="where the PNGs go")
    parser.add_argument("--no-train", action="store_true", help="skip the runs (no Results shots)")
    args = parser.parse_args(argv)
    out: Path = args.out
    out.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="start_here_") as tmp:
        data_dir = Path(tmp) / "data"
        data_dir.mkdir()
        messy = Path(tmp) / "targeted_advertisement_synthetic_messy.csv"
        messy.write_bytes(messy_file())
        with serve(data_dir) as base:
            print(f"app on {base}, data in {data_dir}")
            capture(base, out, train=not args.no_train, messy=messy)
    return 0


if __name__ == "__main__":
    sys.exit(main())
