"""Plan L M160: the messy-file benchmark - thirty files shaped like real exports, through the whole journey.

Each file is one of Home's use cases' own data (`tests/fixtures/make_data.generate`, no client data) made messy
the way exports are: a workbook or a CSV, title rows, a "Grand Total" and a note at the end, empty rows, a
column repeated, money written as text, words meaning "no value" in a number column, and the outcome column
under another spelling. Each goes through the product as a person without a data team would take it, with no
person in the loop:

1. `POST /uploads` (intake reads it);
2. `POST /doctor/diagnosis` (what it can power);
3. the Data Doctor visit (`POST /cases`, `/visit`, `/goal`): answer every blocking question with its
   recommended option (or the first), leave the suggestions as the Doctor pre-ticked them (the sure ones), and
   repeat until the visit rests at APPROVE or stops;
4. `POST /cases/{id}/approval` (Approve): the Doctor checks the prepared file and hands it on when it is ready.

It measures plan §11's figures that a machine can measure: files that fail at upload, files that reach an
approved run (the visit's hand-off) without Manual setup, questions asked per file, and the second month's
clicks to Approve (the same file shape again). Time for a non-technical person cannot be measured here; the
report says so.

    python -m scripts.plan_l_benchmark            # writes reports/plan_l_benchmark.{md,json}
"""

from __future__ import annotations

import argparse
import csv
import io
import json
import re
import shutil
import tempfile
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Final

REPO: Final[Path] = Path(__file__).resolve().parent.parent
USE_CASES: Final[tuple[str, ...]] = (
    "targeted-advertisement",
    "payment-propensity",
    "order-fulfillment",
    "fault-prediction",
    "rca",
)
XLSX: Final[str] = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
MAX_ROUNDS: Final[int] = 40
"""Calls one visit may make before the benchmark gives up on it (each answer or tick is one)."""
GOAL_DOUBTS: Final[tuple[str, ...]] = (
    "d.outcome_missing.",
    "d.outcome_gone.",
    "d.time_missing.",
    "d.consent_missing.",
)
"""The ids of the doubts that say the goal cannot go on: what its outcome, date or consent is is not found."""
HANDED_ON: Final[frozenset[str]] = frozenset({"ready", "ready_with_warnings"})
"""The readiness verdicts the visit hands on."""


@dataclass(frozen=True)
class Messy:
    """What one benchmark file has wrong with it."""

    name: str
    use_case: str
    fmt: str = "csv"
    titles: int = 0
    footer: bool = False
    empty_rows: bool = False
    repeated: bool = False
    money_text: bool = False
    missing_words: bool = False
    outcome_case: bool = False
    seed: int = 0


def cases() -> tuple[Messy, ...]:
    """Thirty files: every use case six times, each with its own mix of problems."""
    mixes: tuple[dict[str, Any], ...] = (
        {},
        {"titles": 3, "footer": True},
        {"fmt": "xlsx", "titles": 2, "footer": True, "empty_rows": True},
        {"missing_words": True, "money_text": True},
        {"fmt": "xlsx", "repeated": True, "outcome_case": True, "missing_words": True},
        {
            "fmt": "xlsx",
            "titles": 1,
            "footer": True,
            "repeated": True,
            "money_text": True,
            "missing_words": True,
            "empty_rows": True,
            "outcome_case": True,
        },
    )
    out: list[Messy] = []
    for index, use_case in enumerate(USE_CASES):
        for number, mix in enumerate(mixes):
            out.append(
                Messy(
                    name=f"{index * 6 + number + 1:02d}_{use_case}_{number}",
                    use_case=use_case,
                    seed=number,
                    **mix,
                )
            )
    return tuple(out)


def _cell(value: Any) -> Any:
    """One cell as a plain Python value: empty for every kind of pandas missing, numpy scalars unwrapped."""
    import pandas as pd

    try:
        if pd.isna(value):
            return None
    except (TypeError, ValueError):
        return value
    item = getattr(value, "item", None)
    return item() if callable(item) and not hasattr(value, "isoformat") else value


def grid(case: Messy, config_root: Path) -> list[list[Any]]:
    import numpy as np
    from tests.fixtures.make_data import GenerationSpec, generate

    from engine.config import load_use_case

    frame = generate(GenerationSpec(use_case_id=case.use_case, rows=3000, variant="clean", seed=case.seed))
    config = load_use_case(case.use_case, config_root)
    target = str(config.target.column)
    numbers = [
        c for c in frame.columns if c != target and frame[c].dtype.kind in "if" and frame[c].nunique() > 10
    ]
    rng = np.random.default_rng(case.seed)
    if case.money_text and numbers:
        column = numbers[0]
        frame[column] = [f"₹{value:,.2f}" if value == value else None for value in frame[column]]
    if case.missing_words and len(numbers) > 1:
        column = numbers[1]
        frame[column] = frame[column].astype(object)
        frame.loc[rng.choice(len(frame), size=90, replace=False), column] = "-"
    if case.outcome_case:
        frame = frame.rename(columns={target: target.upper()})
    header = [str(c) for c in frame.columns]
    rows = [[_cell(v) for v in row] for row in frame.itertuples(index=False)]
    if case.repeated:
        header.append(header[-1])
        for row in rows:
            row.append(row[-1])
    width = len(header)
    out: list[list[Any]] = [[f"Export for {case.use_case}"] + [None] * (width - 1)] if case.titles else []
    out += [["Generated 2026-10-01"] + [None] * (width - 1)] * max(0, case.titles - 1)
    out.append(header)
    for index, row in enumerate(rows):
        out.append([str(v) if hasattr(v, "isoformat") else v for v in row])
        if case.empty_rows and index in (100, 2000):
            out.append([None] * width)
    if case.footer:
        out.append(["Grand Total"] + [None] * (width - 1))
        out.append(["Generated by the CRM export tool"] + [None] * (width - 1))
    return out


def file_bytes(case: Messy, config_root: Path) -> tuple[str, bytes, str]:
    rows = grid(case, config_root)
    if case.fmt == "xlsx":
        from openpyxl import Workbook  # type: ignore[import-untyped]

        book = Workbook()
        sheet = book.active
        sheet.title = "Data"
        for row in rows:
            sheet.append(row)
        buffer = io.BytesIO()
        book.save(buffer)
        return f"{case.name}.xlsx", buffer.getvalue(), XLSX
    text = io.StringIO()
    writer = csv.writer(text, lineterminator="\n")
    for row in rows:
        writer.writerow(["" if v is None else v for v in row])
    return f"{case.name}.csv", text.getvalue().encode("utf-8"), "text/csv"


@dataclass
class Outcome:
    name: str
    use_case: str
    uploaded: bool = False
    upload_error: str | None = None
    doctor_status: str | None = None
    session_status: str | None = None
    """Where the Data Doctor visit got to: `ready` when it rests at APPROVE with an ID and an outcome settled and
    nothing open that Approve would refuse (`not_ready_because`), else `stopped` and why in `stop_reason`."""
    stop_reason: str | None = None
    questions: int = 0
    ticks: int = 0
    applied: bool = False
    apply_error: str | None = None
    intake_notes: int = 0
    second_month_clicks: int | None = None
    seconds: float = 0.0
    case_id: str | None = None
    problems: list[str] = field(default_factory=list)


Choose = Callable[[dict[str, Any]], str | None]
"""`(question) -> option id` for a person who knows more than the rules do; None: the recommended option."""


@contextmanager
def unified_app(config_root: Path) -> Iterator[tuple[Any, Path]]:
    """The API over a copy of `config_root` with the Data Doctor's cases on, and its data folder."""
    from fastapi.testclient import TestClient

    from api.main import create_app

    with tempfile.TemporaryDirectory() as work:
        root = Path(work) / "configs"
        shutil.copytree(config_root, root)
        engine_yaml = root / "engine.yaml"
        text = engine_yaml.read_text(encoding="utf-8")
        engine_yaml.write_text(
            re.sub(r"(\n  data_doctor:[^\n]*\n    unified:) false", r"\1 true", text, count=1),
            encoding="utf-8",
        )
        data_dir = Path(work) / "data"
        with TestClient(create_app(config_root=root, data_dir=data_dir)) as client:
            yield client, data_dir


def _failed(response: Any, what: str, outcome: Outcome) -> None:
    detail = (
        response.json().get("detail", {})
        if response.headers.get("content-type", "").startswith("application/json")
        else {}
    )
    outcome.apply_error = (
        f"{what} {response.status_code}: {detail.get('code') if isinstance(detail, dict) else detail}"
    )


def _stopped_because(case: dict[str, Any]) -> str:
    """Why a case is not at the hand-off, in the Doctor's own words: the visit's stop, else what its plan found
    (stops first), else the goal's doubts and the data it asks for, else what it waits for, else its state."""
    visit = case.get("visit") or {}
    if visit.get("stopped"):
        return str(visit["stopped"].get("message") or visit["stopped"].get("code"))
    found = (visit.get("plan_check") or {}).get("findings", [])
    texts = [f"{f['code']}: {f['text']}" for f in found if f.get("level") in ("stop", "flag", "note")]
    texts += [
        f"{d['doubt_id']}: {d['because']}"
        for d in case.get("doubts") or []
        if d["doubt_id"].startswith(GOAL_DOUBTS)
    ]
    texts += [
        f"{q['code']}: {q['text']}"
        for q in case.get("questions") or []
        if q["answer"] is None and q["code"] == "DATA_REQUESTED"
    ]
    if texts:
        return " ".join(texts)
    waiting = visit.get("waiting_for")
    return f"the visit rests at {case['state']}" + (f", waiting for {waiting}" if waiting else "")


def not_ready_because(case: dict[str, Any], goal: str) -> str | None:
    """Why a case is not ready to Approve, or None when it is: it rests at APPROVE; no blocking question is open;
    no stop of the plan stands without the person's yes (Approve would answer `APPROVAL_STOP_OPEN`); an ID and the
    goal's outcome are settled; and none of the goal's own doubts (the outcome, the date or the consent it cannot
    find) and no data request is open. The other doubts are the Doctor's notes about the file (a column renamed since
    last time, a date that is not newer): shown, not a reason to wait.
    The driver never says that yes itself: a stop is the person's."""
    if case["state"] != "approve":
        return _stopped_because(case)
    visit = case.get("visit") or {}
    said = {a.get("key") for a in visit.get("acknowledged") or []}
    stops = [
        f
        for f in (visit.get("plan_check") or {}).get("findings", [])
        if (f.get("level") == "stop" or f.get("hard")) and f.get("key") not in said
    ]
    key, outcome = roles_of_case(case, goal)
    asking = any(q["blocking"] and q["answer"] is None for q in case["questions"])
    open_requests = any(d["doubt_id"].startswith(GOAL_DOUBTS) for d in case.get("doubts") or []) or any(
        q["code"] == "DATA_REQUESTED" and q["answer"] is None for q in case["questions"]
    )
    if not (stops or asking or open_requests or key is None or outcome is None):
        return None
    reason = _stopped_because(case)
    missing = [what for what, name in (("ID", key), ("outcome", outcome)) if name is None]
    return reason if not missing or stops or open_requests else f"no {' or '.join(missing)} settled: {reason}"


def _drive(
    client: Any,
    upload_id: str,
    use_case: str,
    outcome: Outcome,
    *,
    data_dir: Path,
    choose: Choose | None = None,
    ticked: list[dict[str, Any]] | None = None,
) -> dict[str, Any] | None:
    """The Data Doctor visit from start to Approve; what a run would read, or None.

    Open a case on the file, let the Doctor visit, choose the goal, answer every blocking question (the
    recommended option, else the first; `choose` may know better), tick the sure suggestions the Doctor left
    unticked (it pre-ticks most; anything it marks `check` stays unticked, as "accept the recommended" left it),
    then Approve. Every suggestion it ticks is appended to `ticked`. The answer is `{upload_id, primary_key, target,
    overrides, case, report}`: the prepared file the readiness report was written on, the roles it read, and the
    case as it ended.
    """
    from engine.doctor.readiness import readiness_key
    from engine.doctor.readiness_contracts import DataReadinessReport
    from engine.storage import LocalStorage

    def call(url: str, body: dict[str, Any], what: str) -> dict[str, Any] | None:
        response = client.post(url, json=body)
        if response.status_code not in (200, 201):
            _failed(response, what, outcome)
            return None
        return dict(response.json())

    case = call("/cases", {"upload_ids": [upload_id]}, "open")
    if case is None:
        return None
    outcome.case_id = base = case["case_id"]
    path = f"/cases/{base}"
    case = call(f"{path}/visit", {}, "visit")
    case = case and call(f"{path}/goal", {"version": case["version"], "goal_id": use_case}, "goal")
    for _round in range(MAX_ROUNDS):
        if case is None:
            return None
        blocking = [q for q in case["questions"] if q["blocking"] and q["answer"] is None]
        pending = [
            i
            for i in case["items"]
            if i["state"] == "pending" and i["confidence"] == "sure" and not i.get("parked_for")
        ]
        if blocking:
            question = blocking[0]
            picked = (choose(question) if choose else None) or question.get("recommended")
            picked = picked or question["options"][0]["option_id"]
            outcome.questions += 1
            case = call(
                f"{path}/answers",
                {"version": case["version"], "question_id": question["question_id"], "option_id": picked},
                "answer",
            )
        elif pending and case["state"] == "approve":
            outcome.ticks += 1
            if ticked is not None:
                ticked.append(pending[0])
            case = call(
                f"{path}/decisions",
                {"version": case["version"], "item_id": pending[0]["item_id"], "accept": True},
                "decide",
            )
        else:
            break
    if case is None:
        return None
    why_not = not_ready_because(case, use_case)
    outcome.session_status = "ready" if why_not is None else "stopped"
    if why_not is not None:
        outcome.stop_reason = why_not
        return None
    approved = call(f"{path}/approval", {"version": case["version"]}, "approve")
    if approved is None:
        return None
    case = approved
    reports = [r for r in case["readiness"] if r["goal_id"] == use_case]
    if case["state"] != "follow_up" or not reports or reports[-1]["verdict"] not in HANDED_ON:
        verdict = f"verdict {reports[-1]['verdict']}; " if reports else ""
        outcome.apply_error = f"not handed on: {verdict}{_stopped_because(case)}"[:240]
        return None
    storage = LocalStorage(data_dir)
    report = storage.read_model(
        readiness_key(
            client_id=case["client_id"], case_id=base, goal=use_case, report_hash=reports[-1]["report_hash"]
        ),
        DataReadinessReport,
    )
    outcome.applied = True
    roles = report.roles
    return {
        "upload_id": report.source.upload_id,
        "primary_key": (
            roles.primary_key[0] if len(roles.primary_key) == 1 else list(roles.primary_key) or None
        ),
        "target": roles.target,
        "overrides": dict(report.resolved_overrides),
        "case": case,
        "report": report,
    }


def roles_of_case(case: dict[str, Any], goal: str) -> tuple[str | None, str | None]:
    """`(ID column, outcome column)` a case has settled: its accepted key and the goal's accepted outcome."""

    def column(kind: str, of_goal: bool) -> str | None:
        found = [
            str(i["payload"]["column"])
            for i in case["items"]
            if i["kind"] == kind
            and i["state"] == "accepted"
            and (not of_goal or i.get("goal_id") == goal)
            and i["payload"].get("column")
        ]
        return found[-1] if found else None

    return column("key", False), column("outcome_column", True)


def run_case(client: Any, case: Messy, config_root: Path, data_dir: Path) -> Outcome:
    outcome = Outcome(name=case.name, use_case=case.use_case)
    started = time.perf_counter()
    name, data, kind = file_bytes(case, config_root)
    response = client.post(
        "/uploads", files={"file": (name, data, kind)}, data={"use_case": case.use_case, "mode": "train"}
    )
    if response.status_code != 201:
        outcome.upload_error = str(response.json().get("detail", {}).get("code"))
        outcome.seconds = time.perf_counter() - started
        return outcome
    outcome.uploaded = True
    body = response.json()
    outcome.intake_notes = len((body.get("intake") or {}).get("notes") or [])
    diagnosis = client.post("/doctor/diagnosis", json={"upload_ids": [body["upload_id"]]})
    if diagnosis.status_code == 200:
        verdict = next((u for u in diagnosis.json()["use_cases"] if u["use_case_id"] == case.use_case), None)
        outcome.doctor_status = verdict["status"] if verdict else None
    _drive(client, body["upload_id"], case.use_case, outcome, data_dir=data_dir)
    if outcome.applied:
        # Second month: the same export again; count the clicks a person needs to Approve.
        month2 = Messy(**{**asdict(case), "seed": case.seed + 50})
        name2, data2, kind2 = file_bytes(month2, config_root)
        again = client.post(
            "/uploads",
            files={"file": (name2, data2, kind2)},
            data={"use_case": case.use_case, "mode": "train"},
        )
        if again.status_code == 201:
            second = Outcome(name=case.name, use_case=case.use_case)
            _drive(client, again.json()["upload_id"], case.use_case, second, data_dir=data_dir)
            # one click per question answered, one per suggestion ticked, one to Approve
            outcome.second_month_clicks = second.questions + second.ticks + 1 if second.applied else None
    outcome.seconds = time.perf_counter() - started
    return outcome


def run(selected: tuple[Messy, ...] | None = None, config_root: Path | None = None) -> list[Outcome]:
    root = config_root or REPO / "configs"
    with unified_app(root) as (client, data_dir):
        return [run_case(client, case, root, data_dir) for case in (selected or cases())]


def summary(outcomes: list[Outcome]) -> dict[str, Any]:
    total = len(outcomes)
    applied = [o for o in outcomes if o.applied]
    clicks = [o.second_month_clicks for o in outcomes if o.second_month_clicks is not None]
    return {
        "files": total,
        "failed_at_upload": sum(1 for o in outcomes if not o.uploaded),
        "reached_approved_run_without_manual_setup": len(applied),
        "share_reaching_approved_run": round(len(applied) / total, 3) if total else None,
        "questions_per_file": round(sum(o.questions for o in outcomes) / total, 2) if total else None,
        "second_month_clicks_median": sorted(clicks)[len(clicks) // 2] if clicks else None,
        "doctor_status": {
            s: sum(1 for o in outcomes if o.doctor_status == s)
            for s in ("ready", "check", "too_small", "missing", None)
        },
    }


def report(outcomes: list[Outcome]) -> str:
    s = summary(outcomes)
    lines = [
        "# Plan L M160: the messy-file benchmark",
        "",
        "Generated by `python -m scripts.plan_l_benchmark` (no client data: each file is a Home use case's own",
        "generated data made messy the way exports are). Every file is driven through upload, Data Doctor, the",
        "and the Data Doctor visit to Approve with no person in the loop: every blocking question is answered",
        "with its recommended option (else its first), the sure suggestions are ticked and the others left",
        "unticked. A person would do at least as well; this is a floor, not a forecast.",
        "",
        "Read it with care: the data under the mess is generated, so it is cleaner than a real export (no",
        "wrong IDs, no unit mix-ups, the outcome always present). The benchmark proves that the mess intake",
        "handles no longer stops anyone; it does not prove that every real file is this easy. The second month",
        "here opens a new case on the new file; the Doctor's remembered answers are what bring it to one click.",
        "",
        "## Plan §11's measures",
        "",
        "| Measure | Target | Measured |",
        "|---|---|---|",
        f"| Files that fail at upload | < 2% | {s['failed_at_upload']} of {s['files']} |",
        f"| Files that reach an approved run without Manual setup | ≥ 80% | {s['reached_approved_run_without_manual_setup']} of {s['files']} ({s['share_reaching_approved_run']:.0%}) |",
        f"| Questions asked per file | ≤ 3 on average | {s['questions_per_file']} |",
        f"| Second month: clicks to Approve | 1 | median {s['second_month_clicks_median']} (each question, each tick, Approve) |",
        "| Time from first file to an approved run, non-technical person | < 20 minutes | not measurable here: needs a person (pilot) |",
        "",
        "## Every file",
        "",
        "| File | Use case | Uploaded | Intake notes | Data Doctor | Questions | Visit | Approved | Why not |",
        "|---|---|---|---|---|---|---|---|---|",
    ]
    for o in outcomes:
        why = o.upload_error or o.apply_error or (o.stop_reason or "")[:90]
        lines.append(
            f"| {o.name} | {o.use_case} | {'yes' if o.uploaded else 'no'} | {o.intake_notes} | {o.doctor_status or '—'} | "
            f"{o.questions} | {o.session_status or '—'} | {'yes' if o.applied else 'no'} | {why.replace('|', '/') if not o.applied else ''} |"
        )
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--out", type=Path, default=REPO / "reports")
    args = parser.parse_args(argv)
    outcomes = run()
    args.out.mkdir(parents=True, exist_ok=True)
    (args.out / "plan_l_benchmark.md").write_text(report(outcomes), encoding="utf-8")
    (args.out / "plan_l_benchmark.json").write_text(
        json.dumps(
            {"summary": summary(outcomes), "files": [asdict(o) for o in outcomes]}, indent=1, default=str
        ),
        encoding="utf-8",
    )
    print(json.dumps(summary(outcomes), indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
