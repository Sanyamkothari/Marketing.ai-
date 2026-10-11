"""Plan M M171 (DEC-1601): one contact list, checked on a real run.

A scoring run is written with the product's own code (`tests/fixtures/decide/treat_runs.py`), the real app is asked
for every answer the pages read, and the real pages (`ui/pages.js`, `ui/usecase.js`, through jsdom) are drawn from
those answers by `one_contact_list.mjs`. What a person would see is then compared, number by number, with the run's
own files, counted here with pandas and not read from any summary the pages used:

* one primary download button on the Output page and on the Results view, and it is the treat list's route, so the
  file a marketer gets is byte for byte what `GET /runs/{id}/treat_list.csv` (unchanged) serves;
* the scored list is still there, under Technical details, as "All scores (for analysts)";
* the sentence's figures are the files' own counts: customers in the bands the headline names, customers with
  `treat = 1`, the comparison group, and each contact rule's rows. Nothing in it is typed;
* a run whose treat list cannot be built offers the scored list under the same label, with a one-line note.

Needs node and the jsdom of `tests/integration/production/ui` (skipped with the command that installs them,
failed under `REQUIRE_JSDOM=1`).
"""

from __future__ import annotations

import io
import json
import os
import re
import subprocess
from pathlib import Path
from typing import Any, Final

import pandas as pd
import pytest
from fastapi.testclient import TestClient

from api.main import create_app
from engine.config import RunMode
from engine.contracts import RunRecord, RunState, RunStatus
from engine.decide.treat_list import TREAT_LIST_CSV
from engine.runs import RUN_CONFIG_FILENAME, RUN_FILENAME, STATUS_FILENAME
from engine.stages import export
from engine.stages.actions import APPLIED_RULES_ATTR
from engine.storage import LocalStorage, run_key
from tests.fixtures.decide.treat_runs import USE_CASE, write_run
from tests.fixtures.node import skip_without_jsdom

pytestmark = pytest.mark.integration

HERE: Final[Path] = Path(__file__).resolve().parent
NODE_DIR: Final[Path] = HERE.parent / "production" / "ui"
ROWS: Final[int] = 400


def _client(data_dir: Path, config_root: Path) -> TestClient:
    return TestClient(create_app(config_root=config_root, data_dir=data_dir))


def _ask(client: TestClient, answers: dict[str, Any], path: str, **params: Any) -> Any:
    response = client.get(path, params=params or None)
    body: Any
    try:
        body = response.json()
    except ValueError:
        body = response.text
    answers[f"GET {path}"] = {"status": response.status_code, "body": body}
    return response


def _page(
    tmp_path: Path, config_root: Path, *, breakable: bool = False
) -> tuple[dict[str, Any], LocalStorage, str]:
    """Write a run, ask the real app for what the pages read, replay it to the real pages in jsdom."""
    node = skip_without_jsdom(NODE_DIR)
    data_dir = tmp_path
    storage = LocalStorage(data_dir)
    run_id = "r_20261001_0d000001"
    written = write_run(storage, run_id, rows=ROWS, holdout=True)
    # The scoring summary the pipeline's export stage writes, built by the same function from the written scores.
    scored = written.scores.drop(columns=[c for c in written.scores.columns if c.startswith("reason_")])
    scored.attrs[APPLIED_RULES_ATTR] = ("opted_out",)  # the rule the actions stage ran, as it records it
    summary = export.summarise(
        scored,
        written.config,
        run_id=run_id,
        model_version_id=f"m_{USE_CASE}_1",
        model_display_name="LightGBM",
        primary_key="customer_id",
        drift=None,
        files={export.SCORES_CSV: run_key(run_id, export.SCORES_CSV)},
    )
    storage.write_model(run_key(run_id, "scoring_summary.json"), summary)
    # What the finished run's own record and status say, so `GET /runs/{id}` answers as it does for a real run.
    record = storage.read_model(run_key(run_id, RUN_FILENAME), RunRecord)
    storage.write_model(
        run_key(run_id, RUN_FILENAME),
        record.model_copy(
            update={"artefacts": {"scoring_summary.json": run_key(run_id, "scoring_summary.json")}}
        ),
    )
    storage.write_model(
        run_key(run_id, STATUS_FILENAME),
        RunStatus(
            run_id=run_id,
            mode=RunMode.SCORE,
            state=RunState.DONE,
            updated_at=record.finished_at or record.created_at,
            stages=(),
            progress_pct=100,
        ),
    )
    if breakable:
        storage.delete(run_key(run_id, RUN_CONFIG_FILENAME))  # a run whose treat list cannot be built
    fixtures = tmp_path / "fixtures"
    fixtures.mkdir()
    answers: dict[str, Any] = {}
    with _client(data_dir, config_root) as client:
        run = _ask(client, answers, f"/runs/{run_id}")
        assert run.status_code == 200, run.text
        _ask(client, answers, "/runs", use_case=USE_CASE)
        _ask(client, answers, "/models", use_case=USE_CASE)
        _ask(client, answers, f"/runs/{run_id}/artefacts/scoring_summary.json")
        treat = _ask(client, answers, f"/runs/{run_id}/artefacts/treat_list_summary.json")
        assert treat.status_code == (409 if breakable else 200), treat.text
        use_case = client.get(f"/use-cases/{USE_CASE}")
        assert use_case.status_code == 200, use_case.text
        (fixtures / "use_case.json").write_text(json.dumps(use_case.json()), encoding="utf-8")
        scores = client.get(f"/runs/{run_id}/scores.csv")
        assert scores.status_code == 200
        (tmp_path / "scores.csv").write_bytes(scores.content)
        if not breakable:
            served = client.get(f"/runs/{run_id}/treat_list.csv")
            assert served.status_code == 200
            (tmp_path / "treat_list.csv").write_bytes(served.content)
    (fixtures / "answers.json").write_text(json.dumps(answers), encoding="utf-8")
    (fixtures / "ids.json").write_text(json.dumps({"run_id": run_id}), encoding="utf-8")
    result = subprocess.run(
        [node, str(HERE / "one_contact_list.mjs")],
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
        env={**os.environ, "CL_FIXTURES": str(fixtures)},
    )
    assert result.returncode == 0, result.stdout[-3000:] + result.stderr[-3000:]
    return json.loads(result.stdout), storage, run_id


def _count(n: int) -> str:
    return f"{n:,}"


def test_the_pages_offer_one_download_and_it_is_the_treat_list_the_route_serves(
    tmp_path: Path, config_root: Path
) -> None:
    seen, storage, run_id = _page(tmp_path, config_root)
    for name in ("output", "results"):
        page = seen[name]
        assert len(page["primaryDownloads"]) == 1, (name, page["primaryDownloads"])
        primary = page["primaryDownloads"][0]
        assert primary["text"] == "Download contact list", (name, primary)
        assert primary["href"].endswith(f"/runs/{run_id}/treat_list.csv"), (name, primary)
        # the treat list's own download is not a second button, and there is no "(CSV)" or "treat list" label
        assert "Download treat list" not in page["visibleText"]
        assert page["note"] is None, "the treat list exists, so there is nothing to say about a fallback"
        assert page["scoresLinkUnderTechnical"], f"{name}: the scored list moved under Technical details"
    # The route is unchanged: what it serves is the file the builder wrote, byte for byte.
    served = (tmp_path / "treat_list.csv").read_bytes()
    assert served == storage.read_bytes(run_key(run_id, TREAT_LIST_CSV))
    scores = (tmp_path / "scores.csv").read_bytes()
    assert served != scores, "the contact list and the scored list are different files"
    assert seen["output"]["primaryButtons"] == 1, "one primary button on the Output page"
    assert seen["treatSummaryRequests"] == 1, "the Output page and the Results view share one request"


def test_the_sentence_is_computed_from_the_files_counts(tmp_path: Path, config_root: Path) -> None:
    seen, _, _ = _page(tmp_path, config_root)
    scores = pd.read_csv(tmp_path / "scores.csv", dtype=str, keep_default_na=False)
    treat = pd.read_csv(
        io.BytesIO((tmp_path / "treat_list.csv").read_bytes()), dtype=str, keep_default_na=False
    )
    # Counted from the files here, independently of the summaries the page used.
    worth = int(scores["band"].isin(["High", "Medium"]).sum())
    contacted = int((treat["treat"] == "1").sum())
    comparison = int((treat["holdout"] == "1").sum())
    rules = {
        str(reason): int(rows)
        for reason, rows in scores.loc[scores["suppressed_reason"] != "", "suppressed_reason"]
        .value_counts()
        .items()
    }
    assert worth > contacted > 0, "the fixture must have a gap to explain"
    assert (
        comparison > 0 and rules
    ), "the fixture has a comparison group and customers left out by a contact rule"

    for name in ("output", "results"):
        sentence = seen[name]["sentence"]
        assert sentence is not None, name
        assert sentence.startswith(
            f"{_count(worth)} customers are worth contacting; {_count(contacted)} will be contacted."
        ), sentence
        assert (
            f"{_count(comparison)} customers in the whole list are held back at random as the comparison group"
            in sentence
        )
        words = {
            "opted_out": "opted out of marketing",
            "consent_false": "have not given consent",
            "recently_contacted": "were contacted recently",
        }
        for reason, rows in rules.items():
            assert f"{_count(rows)} customers {words[reason]}" in sentence, (reason, sentence)
        # every figure in the sentence is one of the files' counts
        figures = {int(f.replace(",", "")) for f in re.findall(r"\d[\d,]*", sentence)}
        allowed = {worth, contacted, comparison, *rules.values()}
        assert figures <= allowed, (figures - allowed, sentence)


def test_a_run_without_a_treat_list_offers_the_scored_list_under_the_same_label(
    tmp_path: Path, config_root: Path
) -> None:
    seen, _, run_id = _page(tmp_path, config_root, breakable=True)
    assert seen["contactList"]["hasSummary"] is False and seen["contactList"]["treatError"]
    for name in ("output", "results"):
        page = seen[name]
        assert len(page["primaryDownloads"]) == 1, (name, page["primaryDownloads"])
        primary = page["primaryDownloads"][0]
        assert primary["text"] == "Download contact list"
        assert primary["href"].endswith(f"/runs/{run_id}/scores.csv"), primary
        assert page["note"] and "list of every customer scored" in page["note"], page["note"]
        # no second "scores" link when the scored list is the one file on offer
        assert not page["scoresLinkUnderTechnical"]
