"""Which trained model "Score new data" starts on (DEC-959), in jsdom against REAL API responses.

The Setup form's Trained model select used to default to the use case's champion, once, and never
again. Three things went wrong on screen because of it:

* right after training on a prepared file, the new version waits for approval, so the select sat on
  the champion - trained on other columns - and scoring a file like the training file came back
  SCHEMA_MISMATCH;
* with a client picked in the header, Score mode offered whichever model was champion, whoever's
  tables it was trained on, and its Step 3 was locked until data existed - which, on the raw-tables
  card, needs the model first;
* picking another model left the previous model's SCHEMA_MISMATCH box on screen.

`ui/usecase.js` now starts on the model the user just trained, else the newest one trained on the
header client's tables, else the champion, keeps a model the user picked, and clears the old
validation when the model changes. The node test (`production/ui/usecase/score_model.test.mjs`,
which shares that directory's jsdom install as `ops/` and `approvals/` do) drives the real module;
everything its fake API answers with was answered by the app below: the use case, both uploads,
`GET /runs` with and without the client filter, `GET /models` before and after the training run,
that run's `GET /runs/{id}`, and the `409` a file gets from a model trained on other columns.
"""

from __future__ import annotations

import json
import os
import subprocess
from datetime import timedelta
from pathlib import Path
from typing import Any, Final

import pytest
from fastapi.testclient import TestClient

from api.main import create_app
from engine.config import ColumnType, Metric, ProblemType, RunMode
from engine.contracts import (
    FeatureSchema,
    FeatureSchemaColumn,
    ModelStatus,
    ModelVersion,
    RunRecord,
    RunStatus,
)
from engine.registry import REGISTRY_FILENAME, LocalModelRegistry
from engine.storage import LocalStorage, run_key
from engine.utils.time import utc_now
from tests.fixtures.make_run import RunSpec, write_run
from tests.fixtures.node import skip_without_jsdom
from tests.integration.test_api_uploads import CLEAN_CSV, DEMO_ID

pytestmark = pytest.mark.integration

NODE_DIR: Final[Path] = Path(__file__).resolve().parent / "production" / "ui"
"""The node package holding jsdom (shared with the Phase 4b screens' tests)."""

TESTS_DIR: Final[Path] = NODE_DIR / "usecase"
"""The Setup form's jsdom tests."""

PRIMARY_KEY: Final[str] = "customer_id"
TARGET: Final[str] = "converted_30d"
SCORE_CSV: Final[str] = "".join(line.rsplit(",", 1)[0] + "\n" for line in CLEAN_CSV.splitlines())
"""The training file without its outcome column: what a user scores after training on it."""

CLIENT: Final[str] = "c_demo"
NEW_CLIENT: Final[str] = "c_new"

CHAMPION: Final[str] = f"m_{DEMO_ID}_1"
"""Trained on a prepared file with other columns, and approved: the use case's champion."""
CLIENT_MODEL: Final[str] = f"m_{DEMO_ID}_2"
"""Trained later on `CLIENT`'s built dataset; never promoted."""
JUST_TRAINED: Final[str] = f"m_{DEMO_ID}_3"
"""What the user trains on the screen, on the file they then score; it waits for approval."""

RUNS: Final[dict[str, str]] = {
    CHAMPION: "r_20260801_0000000a",
    CLIENT_MODEL: "r_20260802_0000000b",
    JUST_TRAINED: "r_20260803_0000000c",
}


def _ok(response: Any, status: int = 200) -> Any:
    assert response.status_code == status, response.text
    return response.json()


def _template(storage: LocalStorage, config_root: Path) -> tuple[RunRecord, RunStatus]:
    """A finished training run's `run.json` and `status.json` (`make_run`), taken out of the store.

    Only its documents are kept, as the pattern the three training runs below are copied from: left
    in the store it would be a fourth training run, naming a model version nobody registered.
    """
    template = write_run(storage, RunSpec(DEMO_ID, mode=RunMode.TRAIN, rows=40, config_root=config_root))
    record = storage.read_model(run_key(template, "run.json"), RunRecord)
    status = storage.read_model(run_key(template, "status.json"), RunStatus)
    storage.delete(run_key(template, "run.json"))
    return record, status


def _train_run(
    storage: LocalStorage, template: tuple[RunRecord, RunStatus], model_id: str, **update: Any
) -> None:
    """The training run that produced `model_id`, finished, with `update` applied to its record."""
    record, status = template
    run_id = RUNS[model_id]
    fields = {**record.model_dump(), "run_id": run_id, "model_version_id": model_id, **update}
    storage.write_model(run_key(run_id, "run.json"), RunRecord.model_validate(fields))
    storage.write_model(run_key(run_id, "status.json"), status.model_copy(update={"run_id": run_id}))


def _runs(client: TestClient, **query: str) -> Any:
    """`GET /runs` for the use case, narrowed by `query`."""
    return _ok(client.get("/runs", params={"use_case": DEMO_ID, **query}))


def _read(out: Path, name: str) -> Any:
    return json.loads((out / f"{name}.json").read_text(encoding="utf-8"))


def _register(data_dir: Path, model_id: str, number: int, *, status: ModelStatus, column: str) -> None:
    """A version of `RUNS[model_id]` whose saved schema is the one column `column`."""
    run_id = RUNS[model_id]
    version = ModelVersion(
        model_id=model_id,
        use_case_id=DEMO_ID,
        version=number,
        run_id=run_id,
        created_at=utc_now() + timedelta(minutes=number),
        status=status,
        metric=Metric.ROC_AUC,
        metric_label="ROC-AUC",
        test_score=0.8 + number / 100,
        model_display_name="WeightedEnsemble_L2",
        schema_key=run_key(run_id, "schema.json"),
        run_config_key=run_key(run_id, "run_config.json"),
        predictor_key=run_key(run_id, "model"),
        engine_version="0.1.0",
        autogluon_version="1.6.3",
    )
    LocalModelRegistry(data_dir / REGISTRY_FILENAME).register(version)
    LocalStorage(data_dir).write_model(
        version.schema_key,
        FeatureSchema(
            use_case_id=DEMO_ID,
            model_version_id=model_id,
            primary_key=PRIMARY_KEY,
            target=TARGET,
            problem_type=ProblemType.BINARY_CLASSIFICATION,
            columns=(FeatureSchemaColumn(name=column, inferred_type=ColumnType.INTEGER),),
            row_count_at_fit=1_000,
            created_at=utc_now(),
        ),
    )


def write_fixtures(root: Path, config_root: Path) -> Path:
    """Every body the node test replays, answered by the real app over a seeded store."""
    out = root / "fixtures"
    out.mkdir(parents=True)
    data_dir = root / "data"
    data_dir.mkdir()
    storage = LocalStorage(data_dir)
    template = _template(storage, config_root)
    _train_run(storage, template, CHAMPION)
    _train_run(storage, template, CLIENT_MODEL, upload_id=None, dataset_id="ds_demo", client_id=CLIENT)
    _register(data_dir, CHAMPION, 1, status=ModelStatus.CHAMPION, column="monthly_spend")
    _register(data_dir, CLIENT_MODEL, 2, status=ModelStatus.CANDIDATE, column="tenure_months")

    bodies: dict[str, Any] = {}
    with TestClient(create_app(config_root=config_root, data_dir=data_dir)) as client:
        bodies["use_case"] = _ok(client.get(f"/use-cases/{DEMO_ID}"))
        for mode, csv in (("train", CLEAN_CSV), ("score", SCORE_CSV)):
            bodies[f"upload_{mode}"] = _ok(
                client.post(
                    "/uploads",
                    files={"file": (f"{mode}.csv", csv.encode(), "text/csv")},
                    data={"use_case": DEMO_ID, "mode": mode},
                ),
                201,
            )
        bodies["conflict"] = _ok(
            client.post(
                "/runs",
                json={
                    "use_case": DEMO_ID,
                    "mode": "score",
                    "upload_id": bodies["upload_score"]["upload_id"],
                    "primary_key": PRIMARY_KEY,
                    "model_version_id": CHAMPION,
                },
            ),
            409,
        )
        for when in ("before", "after"):
            if when == "after":  # the user's training run has finished and registered its model
                _train_run(storage, template, JUST_TRAINED)
                _register(
                    data_dir, JUST_TRAINED, 3, status=ModelStatus.PENDING_APPROVAL, column="tenure_months"
                )
                bodies["run_just_trained"] = _ok(client.get(f"/runs/{RUNS[JUST_TRAINED]}"))
            bodies[f"runs_{when}"] = _runs(client)
            bodies[f"models_{when}"] = _ok(client.get("/models", params={"use_case": DEMO_ID}))
            for client_id in (CLIENT, NEW_CLIENT):
                bodies[f"runs_{client_id}_{when}"] = _runs(client, mode="train", client_id=client_id)
    bodies["ids"] = {
        "champion": CHAMPION,
        "clientModel": CLIENT_MODEL,
        "justTrained": JUST_TRAINED,
        "justTrainedRun": RUNS[JUST_TRAINED],
        "client": CLIENT,
        "newClient": NEW_CLIENT,
    }
    for name, body in bodies.items():
        (out / f"{name}.json").write_text(json.dumps(body, indent=1), encoding="utf-8")
    return out


def test_the_fixtures_are_the_world_the_screen_is_tested_in(tmp_path: Path, config_root: Path) -> None:
    """Runs without node: what the node test relies on is really what the API answered."""
    out = write_fixtures(tmp_path, config_root)
    before = [(v["version"]["model_id"], v["is_champion"]) for v in _read(out, "models_before")["versions"]]
    assert before == [(CLIENT_MODEL, False), (CHAMPION, True)]
    after = _read(out, "models_after")["versions"]
    assert after[0]["version"]["model_id"] == JUST_TRAINED
    assert after[0]["version"]["status"] == "pending_approval", "the champion stays the champion"
    assert [r["run_id"] for r in _read(out, f"runs_{CLIENT}_before")["runs"]] == [RUNS[CLIENT_MODEL]]
    assert _read(out, f"runs_{NEW_CLIENT}_after")["runs"] == []
    assert [r["run_id"] for r in _read(out, "runs_after")["runs"]] == sorted(RUNS.values(), reverse=True)
    codes = [check["code"] for check in _read(out, "conflict")["validation"]["checks"]]
    assert "SCHEMA_MISMATCH" in codes, "the champion was trained on other columns"
    profile = _read(out, "upload_train")["profile"]
    assert profile["primary_key_candidates"][0] == PRIMARY_KEY
    assert profile["target_candidate"] == TARGET


def test_the_score_model_default_in_jsdom(tmp_path: Path, config_root: Path) -> None:
    node = skip_without_jsdom(NODE_DIR)  # REQUIRE_JSDOM=1 (CI) turns a skip into a failure
    out = write_fixtures(tmp_path, config_root)
    tests = sorted(str(p) for p in TESTS_DIR.glob("*.test.mjs"))
    assert tests, "no jsdom test was found"
    result = subprocess.run(
        [node, "--test", *tests],
        cwd=NODE_DIR,
        env={**os.environ, "PB_FIXTURES": str(out)},
        capture_output=True,
        text=True,
        timeout=180,
        check=False,
    )
    assert result.returncode == 0, result.stdout[-8000:] + result.stderr[-3000:]
