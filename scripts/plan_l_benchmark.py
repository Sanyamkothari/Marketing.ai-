"""Plan L M160: the messy-file benchmark - thirty files shaped like real exports, through the whole journey.

Each file is one of Home's use cases' own data (`tests/fixtures/make_data.generate`, no client data) made messy
the way exports are: a workbook or a CSV, title rows, a "Grand Total" and a note at the end, empty rows, a
column repeated, money written as text, words meaning "no value" in a number column, and the outcome column
under another spelling. Each goes through the product as a person without a data team would take it, with no
person in the loop:

1. `POST /uploads` (intake reads it);
2. `POST /doctor/diagnosis` (what it can power);
3. `POST /doctor/handoff` to its use case, when Data Doctor says it can start;
4. Guided setup: start, answer every blocking question with its recommended option (or the first), accept
   the recommended suggestions, and repeat until the session is ready or stops;
5. `POST .../apply` (Approve).

It measures plan §11's figures that a machine can measure: files that fail at upload, files that reach an
approved run without Manual setup, questions asked per file, and the second month's clicks to Approve (the
same file shape again). Time for a non-technical person cannot be measured here; the report says so.

    python -m scripts.plan_l_benchmark            # writes reports/plan_l_benchmark.{md,json}
"""

from __future__ import annotations

import argparse
import csv
import io
import json
import tempfile
import time
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
MAX_ROUNDS: Final[int] = 8


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
    stop_reason: str | None = None
    questions: int = 0
    applied: bool = False
    apply_error: str | None = None
    intake_notes: int = 0
    second_month_clicks: int | None = None
    seconds: float = 0.0
    problems: list[str] = field(default_factory=list)


def _drive(client: Any, upload_id: str, use_case: str, outcome: Outcome) -> str | None:
    """Guided setup from start to Approve; the applied upload id, or None."""
    base = f"/uploads/{upload_id}/agent-session"
    started = client.post(base, json={"use_case": use_case})
    if started.status_code not in (200, 201):
        outcome.apply_error = f"start {started.status_code}"
        return None
    session = started.json()["session"]
    for _round in range(MAX_ROUNDS):
        if session["status"] in ("stopped", "ready"):
            break
        open_questions = [q for q in session["questions"] if q.get("blocking") and q.get("answer") is None]
        if open_questions:
            question = open_questions[0]
            options = question["options"]
            pick = next((o for o in options if "recommended" in o["label"].lower()), options[0])
            outcome.questions += 1
            answered = client.post(
                f"{base}/answers",
                json={"question_id": question["question_id"], "option_id": pick["option_id"]},
            )
            if answered.status_code != 200:
                outcome.apply_error = (
                    f"answer {answered.status_code}: {answered.json().get('detail', {}).get('code')}"
                )
                return None
            session = answered.json()["session"]
            continue
        decided = client.post(f"{base}/decisions", json={"accept_recommended": True})
        if decided.status_code != 200:
            outcome.apply_error = f"decide {decided.status_code}"
            return None
        session = decided.json()["session"]
        if session["status"] == "needs_review":
            pending = [p for p in session["proposals"] if p["state"] == "pending"]
            if pending:
                rejected = client.post(
                    f"{base}/decisions",
                    json={
                        "decisions": [{"proposal_id": p["proposal_id"], "state": "rejected"} for p in pending]
                    },
                )
                session = rejected.json()["session"] if rejected.status_code == 200 else session
    outcome.session_status = session["status"]
    outcome.stop_reason = session.get("stop_reason")
    if session["status"] != "ready":
        return None
    applied = client.post(f"{base}/apply")
    if applied.status_code != 200:
        detail = applied.json().get("detail", {})
        outcome.apply_error = (
            f"apply {applied.status_code}: {detail.get('code') if isinstance(detail, dict) else detail}"
        )
        return None
    outcome.applied = True
    return str(applied.json()["upload_id"])


def run_case(client: Any, case: Messy, config_root: Path) -> Outcome:
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
    upload_id = body["upload_id"]
    if outcome.doctor_status in ("ready", "check"):
        handed = client.post("/doctor/handoff", json={"upload_id": upload_id, "use_case": case.use_case})
        if handed.status_code in (200, 201):
            upload_id = handed.json()["upload_id"]
    _drive(client, upload_id, case.use_case, outcome)
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
            _drive(client, again.json()["upload_id"], case.use_case, second)
            # one click to accept the recommended set, one per question answered, one to Approve
            outcome.second_month_clicks = 1 + second.questions + 1 if second.applied else None
    outcome.seconds = time.perf_counter() - started
    return outcome


def run(selected: tuple[Messy, ...] | None = None, config_root: Path | None = None) -> list[Outcome]:
    from fastapi.testclient import TestClient

    from api.main import create_app

    root = config_root or REPO / "configs"
    with (
        tempfile.TemporaryDirectory() as data_dir,
        TestClient(create_app(config_root=root, data_dir=Path(data_dir))) as client,
    ):
        return [run_case(client, case, root) for case in (selected or cases())]


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
        "hand-off and Guided setup to Approve with no person in the loop: every blocking question is answered",
        "with its recommended option (else its first), the recommended suggestions are accepted and the others",
        "rejected. A person would do at least as well; this is a floor, not a forecast.",
        "",
        "## Plan §11's measures",
        "",
        "| Measure | Target | Measured |",
        "|---|---|---|",
        f"| Files that fail at upload | < 2% | {s['failed_at_upload']} of {s['files']} |",
        f"| Files that reach an approved run without Manual setup | ≥ 80% | {s['reached_approved_run_without_manual_setup']} of {s['files']} ({s['share_reaching_approved_run']:.0%}) |",
        f"| Questions asked per file | ≤ 3 on average | {s['questions_per_file']} |",
        f"| Second month: clicks to Approve | 1 | median {s['second_month_clicks_median']} (accept recommended, each question, Approve) |",
        "| Time from first file to an approved run, non-technical person | < 20 minutes | not measurable here: needs a person (pilot) |",
        "",
        "## Every file",
        "",
        "| File | Use case | Uploaded | Intake notes | Data Doctor | Questions | Guided setup | Approved | Why not |",
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
