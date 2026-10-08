"""Plan J M100 acceptance: a default (one treatment level) uplift run writes byte-identical artefacts.

One training run and one scoring run of `win-back-campaign` go through the product's own API (the real
upload, checks, uplift train flow and score flow), with every id pinned so the seeds are fixed. The
artefacts' bytes are reduced to SHA-256 digests after the only things that differ between two runs of
the same code are masked: wall-clock timestamps and durations. The digests of the code before M100 are
checked in (`golden/m100_binary_digests.json`); `python -m tests.fixtures.decide.m100_binary --update`
rewrites them, and must only ever be run on a commit whose default uplift artefacts are known good.
"""

from __future__ import annotations

import hashlib
import io
import json
import re
import sys
import tempfile
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Final

import pandas as pd

GOLDEN: Final[Path] = Path(__file__).resolve().parent / "golden" / "m100_binary_digests.json"

USE_CASE: Final[str] = "win-back-campaign"
TRAIN_RUN: Final[str] = "r_20261008_0c100001"
SCORE_RUN: Final[str] = "r_20261008_0c100002"
UPLOADS: Final[tuple[str, str]] = ("u_0c1000000001", "u_0c1000000002")

TRAIN_JSON: Final[tuple[str, ...]] = (
    "uplift_validation.json",
    "uplift_evaluation.json",
    "qini_curve.json",
    "segments.json",
    "policy_recommendation.json",
    "split.json",
    "run_config.json",
    "feature_importance.json",
    "model/uplift_model.json",
)
SCORE_JSON: Final[tuple[str, ...]] = (
    "segments.json",
    "policy_recommendation.json",
    "scoring_summary.json",
    "ranking_choice.json",
    "uplift_drift.json",
    "run_config.json",
)

_TIMESTAMP = re.compile(r"\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})?")
_DURATIONS = re.compile(r'"(duration_s|seconds|elapsed_s|estimated_refit_seconds|refit_seconds)": [0-9.eE+-]+')
_SPOKEN_DURATION = re.compile(r"about [0-9.,]+ (seconds?|minutes?|hours?)")


def normalise(text: str) -> str:
    """The artefact with wall-clock timestamps and durations masked; nothing else is touched.

    M96's fold-refit estimate is measured from this run's own fit time, so it is a duration too.
    """
    masked = _TIMESTAMP.sub("<time>", text)
    masked = _SPOKEN_DURATION.sub(r"about <n> \1", masked)
    return _DURATIONS.sub(r'"\1": <seconds>', masked)


def _digest(data: bytes | str) -> str:
    raw = data.encode("utf-8") if isinstance(data, str) else data
    return hashlib.sha256(raw).hexdigest()


@contextmanager
def _pinned(run_id: str, upload_ids: list[str]) -> Iterator[None]:
    import pytest

    import api.routes.uploads as uploads_route
    from engine import runs as engine_runs

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(engine_runs, "new_run_id", lambda _moment=None: run_id)
        patch.setattr(uploads_route, "new_upload_id", lambda: upload_ids.pop(0))
        yield


def _upload(client: Any, frame: pd.DataFrame, *, mode: str) -> str:
    payload = frame.to_csv(index=False, lineterminator="\n").encode()
    response = client.post(
        "/uploads",
        files={"file": (f"{mode}.csv", payload, "text/csv")},
        data={"use_case": USE_CASE, "mode": mode},
    )
    assert response.status_code == 201, response.text
    return str(response.json()["upload_id"])


def _finish(client: Any, run_id: str) -> None:
    deadline = time.monotonic() + 600.0
    while True:
        response = client.get(f"/runs/{run_id}")
        assert response.status_code == 200, response.text
        state = response.json()["run"]["state"]
        if state in {"done", "failed", "cancelled"}:
            assert state == "done", response.json()["status"]
            return
        assert time.monotonic() < deadline, f"run {run_id} did not finish"
        time.sleep(0.2)


def default_run_digests(config_root: Path, data_dir: Path) -> dict[str, str]:
    """`{artefact: sha256}` of a default uplift training run and a scoring run of its model."""
    from fastapi.testclient import TestClient

    from api.main import create_app
    from engine.storage import LocalStorage, run_key
    from tests.fixtures.make_uplift_data import make_uplift_data, make_winback_campaign
    from tests.integration.uplift.test_uplift_api import FAST_OVERRIDES, scoring_frame

    data_dir.mkdir(parents=True, exist_ok=True)
    storage = LocalStorage(data_dir)
    digests: dict[str, str] = {}
    with TestClient(create_app(config_root=config_root, data_dir=data_dir)) as client:
        ids = list(UPLOADS)
        with _pinned(TRAIN_RUN, ids):
            upload_id = _upload(client, make_uplift_data(6_000, seed=41).frame, mode="train")
            body = {
                "use_case": USE_CASE,
                "upload_id": upload_id,
                "primary_key": "customer_id",
                "target": "reactivated_90d",
                "treatment_column": "treatment",
                "overrides": FAST_OVERRIDES,
            }
            response = client.post("/uplift/runs", json=body)
            assert response.status_code == 202, response.text
        _finish(client, TRAIN_RUN)
        model_id = client.get(f"/runs/{TRAIN_RUN}").json()["run"]["model_version_id"]
        with _pinned(SCORE_RUN, ids):
            score_upload = _upload(client, scoring_frame(make_winback_campaign(3_000, seed=43)), mode="score")
            response = client.post(
                "/runs",
                json={
                    "use_case": USE_CASE,
                    "mode": "score",
                    "upload_id": score_upload,
                    "primary_key": "customer_id",
                    "model_version_id": model_id,
                },
            )
            assert response.status_code == 202, response.text
        _finish(client, SCORE_RUN)
    for name in TRAIN_JSON:
        digests[f"train/{name}"] = _digest(normalise(storage.read_text(run_key(TRAIN_RUN, name))))
    holdout = pd.read_parquet(io.BytesIO(storage.read_bytes(run_key(TRAIN_RUN, "uplift_holdout.parquet"))))
    digests["train/uplift_holdout.parquet(columns)"] = _digest(",".join(map(str, holdout.columns)))
    digests["train/uplift_holdout.parquet(rows)"] = _digest(holdout.to_csv(index=False, lineterminator="\n"))
    for name in SCORE_JSON:
        digests[f"score/{name}"] = _digest(normalise(storage.read_text(run_key(SCORE_RUN, name))))
    digests["score/scores.csv"] = _digest(storage.read_bytes(run_key(SCORE_RUN, "scores.csv")))
    scores = pd.read_parquet(io.BytesIO(storage.read_bytes(run_key(SCORE_RUN, "scores.parquet"))))
    digests["score/scores.parquet(rows)"] = _digest(scores.to_csv(index=False, lineterminator="\n"))
    return digests


def main(argv: list[str]) -> int:
    if "--update" not in argv:
        print("usage: python -m tests.fixtures.decide.m100_binary --update", file=sys.stderr)
        return 2
    root = Path(__file__).resolve().parents[3]
    with tempfile.TemporaryDirectory() as tmp:
        digests = default_run_digests(root / "configs", Path(tmp) / "data")
    GOLDEN.parent.mkdir(parents=True, exist_ok=True)
    GOLDEN.write_text(json.dumps(digests, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(f"wrote {GOLDEN}")
    return 0


if __name__ == "__main__":  # pragma: no cover - the maintainer's update path
    raise SystemExit(main(sys.argv[1:]))
