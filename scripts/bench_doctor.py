"""Benchmark the Data Doctor: take each library dataset through the real visit with a scripted person.

Roadmap section 3.1. For every dataset the harness fetches the file (outside the repo), opens a case through
the real API (FastAPI TestClient, a fresh data dir each time) and plays a person who takes the first offered
goal, answers each open question with the recommended (else first) option, accepts every pending proposal,
confirms, acknowledges the stops that may be acknowledged and approves. It records whether the case reached
ready, why not, how long it took, the peak memory and what the AI cost. A fetch that fails (no network) is a
result row, never a crash; so is a dataset that blows up mid-visit (each runs in its own process).

    .venv/bin/python scripts/bench_doctor.py --out-dir /tmp/bench-out
    .venv/bin/python scripts/bench_doctor.py --only online-retail --local online-retail=/path/to/log.csv
    .venv/bin/python scripts/bench_doctor.py --live          # the configured model instead of the fake AI

By default the AI is the fake grounded one (no paid model). The fake does not read the data, so a run measures
the rules, the visit and the plumbing; `--live` measures the AI as well.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
import resource
import shutil
import socket
import subprocess
import sys
import tempfile
import time
from datetime import date
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parents[1]

# dataset folder -> the use case its upload is made for
DATASETS: dict[str, str] = {
    "online-retail": "retail-win-back",
    "health-insurance-cross-sell": "insurance-cross-sell",
    "uci-bank-marketing": "bank-term-deposit",
    "hillstrom-email": "hillstrom-email",
    "uci-credit-default": "card-default-propensity",
    "telco-customer-churn": "telco-churn",
}
DEFAULT_DATA_DIR = Path(tempfile.gettempdir()) / "marketing-ai-bench-data"
MAX_ROUNDS = 12
CHILD_TIMEOUT_S = 3600
FETCH_TIMEOUT_S = 120


# ---------------------------------------------------------------------------
# Fetching
# ---------------------------------------------------------------------------
def fetch(name: str, data_dir: Path, local: Path | None = None) -> Path:
    """The dataset's raw file under `data_dir/<name>`; the library's own fetcher, pointed away from the repo."""
    socket.setdefaulttimeout(FETCH_TIMEOUT_S)  # urlopen has none: an unreachable host must not hang the run
    target = data_dir / name
    target.mkdir(parents=True, exist_ok=True)
    spec = importlib.util.spec_from_file_location(
        f"_bench_fetch_{name}", REPO / "library" / name / "fetch.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    module.DATA = target  # the fetchers write wherever these point
    module.RAW = target / module.RAW.name
    if local is not None:
        shutil.copyfile(local, module.RAW)
    elif not module.RAW.exists():
        module.download()
    return Path(module.RAW)


# ---------------------------------------------------------------------------
# One visit
# ---------------------------------------------------------------------------
def _setup_environment(live: bool) -> None:
    if not live:
        os.environ["MARKETING_AI_ALLOW_FAKE_AI"] = "true"


def _first_option(question: dict[str, Any]) -> str | None:
    options = [o["option_id"] for o in question.get("options", [])]
    recommended = question.get("recommended")
    return recommended if recommended in options else (options[0] if options else None)


def _pick_goal(case: dict[str, Any], wanted: str) -> tuple[str | None, list[dict[str, str]]]:
    """The first goal the screen offers (the use case the file was made for, when it is among them).

    Only `ready` and `check` goals are offered to the person; `missing` and `too_small` are not."""
    offered = [
        {"goal_id": str(u["use_case_id"]), "status": str(u["status"])}
        for u in (case.get("diagnosis") or {}).get("use_cases", [])
        if u.get("status") in ("ready", "check")
    ]
    ids = [g["goal_id"] for g in offered]
    if wanted in ids:
        return wanted, offered
    return (ids[0] if ids else None), offered


def run_visit(
    path: Path, use_case: str, *, live: bool = False, data_dir: Path | None = None
) -> dict[str, Any]:
    """Play the scripted person over `path`; return the record (see the module docstring)."""
    _setup_environment(live)
    from fastapi.testclient import TestClient

    from api.main import create_app
    from engine.doctor.protocol import item_sentence

    record: dict[str, Any] = {"rows": None, "steps": []}
    owned = data_dir is None
    work = Path(tempfile.mkdtemp(prefix="bench-doctor-")) if owned else data_dir
    started = time.perf_counter()
    try:
        with TestClient(create_app(config_root=REPO / "configs", data_dir=work)) as client:
            r = client.post(
                "/uploads",
                files={"file": (path.name, path.read_bytes(), "text/csv")},
                data={"use_case": use_case, "mode": "train"},
            )
            if r.status_code != 201:
                record["steps"].append(f"upload: HTTP {r.status_code} {r.text[:200]}")
                return _finish(record, None, None, started, f"upload refused: HTTP {r.status_code}")
            record["rows"] = (r.json().get("profile") or {}).get("row_count")
            case = client.post("/cases", json={"upload_ids": [r.json()["upload_id"]]}).json()
            cid = case["case_id"]
            client.post(f"/cases/{cid}/ai-consent", json={})

            def post(route: str, body: dict[str, Any]) -> bool:
                nonlocal case
                resp = client.post(f"/cases/{cid}/{route}", json={"version": case["version"], **body})
                if resp.status_code == 200:
                    case = resp.json()
                    return True
                if (
                    "does not need its own yes" not in resp.text
                ):  # most changes are approved together: not news
                    record["steps"].append(f"{route}: HTTP {resp.status_code} {resp.text[:160]}")
                return False

            resp = client.post(f"/cases/{cid}/visit", json={})
            if resp.status_code == 200:
                case = resp.json()
            else:
                record["steps"].append(f"visit: HTTP {resp.status_code} {resp.text[:160]}")
            goal, offered = _pick_goal(case, use_case)
            record["goals_offered"] = offered
            record["goal"] = goal
            record["expected_goal_offered"] = any(g["goal_id"] == use_case for g in offered)
            if goal is not None:
                post("goal", {"goal_id": goal, "op_id": "bench-goal"})
            hard: list[str] = []

            def sweep_stops() -> bool:
                """Acknowledge the stops that may be; note the ones that may not. True if anything was acknowledged."""
                acted = False
                view = client.get(f"/cases/{cid}/screen").json().get("view", {})
                for f in (view.get("ledger") or {}).get("findings", []):
                    if f.get("level") != "stop" or f.get("acknowledged"):
                        continue
                    if f.get("acknowledgeable", True):
                        acted |= post("acknowledgements", {"key": f["key"], "reason": "bench"})
                    elif f.get("code") not in hard:
                        hard.append(str(f.get("code")))
                return acted

            confirmed: set[str] = set()
            for _round in range(MAX_ROUNDS):
                acted = False
                for q in [q for q in case["questions"] if q.get("answer") is None]:
                    option = _first_option(q)
                    if option is not None and post(
                        "answers", {"question_id": q["question_id"], "option_id": option}
                    ):
                        acted = True
                for it in [i for i in case["items"] if i.get("state") == "pending"]:
                    acted |= post("decisions", {"item_id": it["item_id"], "accept": True})
                for it in [
                    i for i in case["items"] if i.get("state") == "accepted" and i["item_id"] not in confirmed
                ]:
                    confirmed.add(it["item_id"])
                    sentence = item_sentence(it.get("title", ""), it.get("payload", {}))
                    acted |= post("confirmations", {"item_id": it["item_id"], "sentence": sentence})
                acted |= sweep_stops()
                if not acted:  # nothing left to do: approve, then read what the final check found
                    post("approval", {})  # 200 even when the final check fails: look at the screen either way
                    acted = sweep_stops()
                if not acted:
                    break
            record["hard_stops"] = hard
            screen = client.get(f"/cases/{cid}/screen").json()
            return _finish(record, case, screen.get("view", {}), started, None)
    finally:
        if owned:
            shutil.rmtree(work, ignore_errors=True)


def _finish(
    record: dict[str, Any],
    case: dict[str, Any] | None,
    view: dict[str, Any] | None,
    started: float,
    error: str | None,
) -> dict[str, Any]:
    record["wall_s"] = round(time.perf_counter() - started, 2)
    record["peak_mem_mb"] = round(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024, 1)
    if case is None or view is None:
        record.update(reached_ready=False, state=None, reason=error, ai_calls=0, ai_tokens=0)
        return record
    verdict = (view.get("verdict") or {}).get("key")
    open_blocking = [q for q in case["questions"] if q.get("answer") is None and q.get("blocking")]
    reason = None
    if verdict not in ("ready", "ready_with_warnings"):
        if record.get("hard_stops"):
            reason = "hard stop: " + ", ".join(record["hard_stops"])
        elif open_blocking:
            reason = "blocking question: " + open_blocking[0]["text"][:160]
        elif not record.get("goal"):
            reason = "no goal offered"
        else:
            stops = [
                str(f.get("code"))
                for f in (view.get("ledger") or {}).get("findings", [])
                if f.get("level") == "stop" and not f.get("acknowledged")
            ]
            reason = f"verdict {verdict}: " + (
                "open stops " + ", ".join(stops)
                if stops
                else str((view.get("verdict") or {}).get("next") or "")[:160]
            )
    record.update(
        reached_ready=verdict in ("ready", "ready_with_warnings"),
        verdict=verdict,
        state=case["state"],
        reason=reason,
        ai_calls=case["counters"]["model_calls"],
        ai_tokens=case["counters"]["tokens"],
    )
    return record


# ---------------------------------------------------------------------------
# All datasets
# ---------------------------------------------------------------------------
def _run_in_child(path: Path, use_case: str, live: bool) -> dict[str, Any]:
    """One dataset in its own process: its peak memory is its own, and a crash is a result.

    The record comes back through a file, not a pipe: a helper process the visit leaves behind would hold a pipe
    open and the parent would wait for it forever.
    """
    with tempfile.TemporaryDirectory(prefix="bench-worker-") as scratch:
        result, log = Path(scratch) / "result.json", Path(scratch) / "worker.log"
        command = [sys.executable, str(Path(__file__).resolve()), "--worker", str(path), use_case]
        command += ["--result", str(result)] + (["--live"] if live else [])
        try:
            with log.open("wb") as sink:
                done = subprocess.run(command, stdout=sink, stderr=sink, timeout=CHILD_TIMEOUT_S, cwd=REPO)
        except subprocess.TimeoutExpired:
            return {"reached_ready": False, "reason": f"timed out after {CHILD_TIMEOUT_S}s"}
        if result.exists():
            return dict(json.loads(result.read_text(encoding="utf-8")))
        if done.returncode < 0:  # SIGKILL is the kernel's out-of-memory killer, usually
            return {
                "reached_ready": False,
                "reason": f"worker killed by signal {-done.returncode} (out of memory?)",
            }
        tail = (log.read_text(errors="replace").strip().splitlines() or ["no output"])[-1]
        return {"reached_ready": False, "reason": f"worker crashed (exit {done.returncode}): {tail[:200]}"}


def run_all(
    names: list[str], data_dir: Path, locals_: dict[str, Path], *, live: bool = False
) -> list[dict[str, Any]]:
    results: list[dict[str, Any]] = []
    for name in names:
        row: dict[str, Any] = {"dataset": name, "use_case": DATASETS[name], "fetched": False}
        try:
            path = fetch(name, data_dir, locals_.get(name))
            row["fetched"] = True
            row["file_mb"] = round(path.stat().st_size / 1e6, 1)
            row.update(_run_in_child(path, DATASETS[name], live))
        except Exception as exc:  # no network, a changed checksum, a missing file: a result, not a crash
            row.update(reached_ready=False, reason=f"fetch failed: {type(exc).__name__}: {str(exc)[:160]}")
        results.append(row)
    return results


def markdown_table(results: list[dict[str, Any]]) -> str:
    head = "| Dataset | Ready | State | Why not | Wall s | Peak MB | AI calls | AI tokens |\n|---|---|---|---|---|---|---|---|\n"
    rows = []
    for r in results:
        why = (r.get("reason") or "").replace("|", "/").replace("\n", " ")
        rows.append(
            f"| {r['dataset']} | {'yes' if r.get('reached_ready') else 'no'} | {r.get('state') or '-'} | {why or '-'} "
            f"| {r.get('wall_s', '-')} | {r.get('peak_mem_mb', '-')} | {r.get('ai_calls', '-')} | {r.get('ai_tokens', '-')} |"
        )
    return head + "\n".join(rows) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument(
        "--data-dir", type=Path, default=DEFAULT_DATA_DIR, help="where the datasets are fetched to"
    )
    parser.add_argument(
        "--out-dir",
        type=Path,
        default=DEFAULT_DATA_DIR / "out",
        help="where bench_doctor.json/.md go (outside the repository by default)",
    )
    parser.add_argument(
        "--only", action="append", choices=sorted(DATASETS), help="run just these (repeatable)"
    )
    parser.add_argument(
        "--local", action="append", default=[], metavar="NAME=PATH", help="use a file you already have"
    )
    parser.add_argument("--live", action="store_true", help="use the configured model, not the fake AI")
    parser.add_argument("--worker", nargs=2, metavar=("PATH", "USE_CASE"), help=argparse.SUPPRESS)
    parser.add_argument("--result", type=Path, help=argparse.SUPPRESS)
    args = parser.parse_args(argv)

    if args.worker:
        record = run_visit(Path(args.worker[0]), args.worker[1], live=args.live)
        (args.result or Path("/dev/stdout")).write_text(json.dumps(record), encoding="utf-8")
        return 0
    sys.path.insert(0, str(REPO))
    locals_ = {k: Path(v) for k, _, v in (item.partition("=") for item in args.local)}
    results = run_all(args.only or list(DATASETS), args.data_dir, locals_, live=args.live)
    args.out_dir.mkdir(parents=True, exist_ok=True)
    stamp = {"date": date.today().isoformat(), "ai": "live" if args.live else "fake grounded"}
    (args.out_dir / "bench_doctor.json").write_text(
        json.dumps({**stamp, "results": results}, indent=1, ensure_ascii=False), encoding="utf-8"
    )
    table = markdown_table(results)
    (args.out_dir / "bench_doctor.md").write_text(table, encoding="utf-8")
    print(table)
    return 0


if __name__ == "__main__":
    sys.path.insert(0, str(REPO))
    raise SystemExit(main())
