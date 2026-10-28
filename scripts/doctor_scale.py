"""Time and peak memory of Data Doctor's whole-file check on a real file (docs/plans/DATA_DOCTOR_SCALE.md).

Usage: .venv/bin/python scripts/doctor_scale.py PATH_TO_CSV [--key COLUMN] [--date COLUMN]

It reads the file the way the whole-file check does (every row, IDs as text), then runs a simple plan over it
(`materialize_plan` at `Full`): parse one date column, drop the last column, and count by the key column. Each phase
prints its seconds and the process's peak memory so far (`ru_maxrss`, a high-water mark: it only rises). No cell
value is printed.
"""

from __future__ import annotations

import argparse
import resource
import sys
import tempfile
import time
from datetime import UTC, datetime
from pathlib import Path

from engine.agent.contracts import AgentConfidence, ProposalSource, ProposalState, RecipeStep, RecipeStepKind
from engine.agentic.canonical import canonical_bytes, sha256_hex
from engine.contracts import DatasetProfile
from engine.doctor.case import CaseFile, CaseItem
from engine.doctor.diagnose import LoadedFile
from engine.doctor.materialize import Full, materialize_plan
from engine.doctor.plan_check import plan_check_of
from engine.stages import ingest
from engine.storage import LocalStorage


def _peak_mb() -> float:
    return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024


def _item(item_id: str, kind: str, payload: dict[str, object], now: datetime) -> CaseItem:
    return CaseItem(
        item_id=item_id,
        kind=kind,
        source=ProposalSource.RULES,
        confidence=AgentConfidence.SURE,
        state=ProposalState.ACCEPTED,
        upload_id="u1",
        payload=payload,  # type: ignore[arg-type]
        payload_digest=sha256_hex(canonical_bytes(payload))[:32],
        proposed_at=now,
    )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("path", type=Path)
    parser.add_argument("--key", default="CustomerID")
    parser.add_argument("--date", default="InvoiceDate")
    args = parser.parse_args()
    now = datetime.now(UTC)
    with tempfile.TemporaryDirectory() as tmp:
        storage = LocalStorage(Path(tmp))
        storage.write_bytes("uploads/u1/source.csv", args.path.read_bytes())
        t0 = time.perf_counter()
        read = ingest.read_upload(
            storage,
            "uploads/u1/source.csv",
            file_format="csv",
            row_cap=10_000_000,
            keep_all_rows=True,
            keep_leading_zeros=True,
        )
        frame = read.all_rows if read.all_rows is not None else read.frame
        t_read = time.perf_counter() - t0
        print(f"read      {t_read:7.2f} s  rows={len(frame):,}  peak={_peak_mb():,.0f} MB")
        loaded = LoadedFile(
            upload_id="u1",
            file_name=args.path.name,
            frame=frame,
            profile=DatasetProfile.model_construct(row_count=len(frame)),
        )
        last = str(frame.columns[-1])
        steps = [
            (RecipeStepKind.PARSE_DATE, args.date, {"dayfirst": False}),
            (RecipeStepKind.DROP_COLUMN, last, {}),
        ]
        items = [_item("key.1", "key", {"column": args.key}, now)]
        for n, (kind, column, params) in enumerate(steps, start=1):
            step = RecipeStep(order=n, kind=kind, column=column, params=params)  # type: ignore[arg-type]
            items.append(_item(f"step.{n}", "recipe_step", {"step": step.model_dump(mode="json")}, now))
        case_file = CaseFile(upload_id="u1", file_name=args.path.name, added_at=now, added_by="bench")
        t1 = time.perf_counter()
        prepared = materialize_plan([case_file], items, [loaded], Full())
        t_plan = time.perf_counter() - t1
        print(f"plan      {t_plan:7.2f} s  peak={_peak_mb():,.0f} MB")
        check = plan_check_of(prepared, basis="bench")
        print(f"findings  {len(check.findings)} ({', '.join(sorted({f.code for f in check.findings}))})")
        print(
            f"total     {t_read + t_plan:7.2f} s  peak={_peak_mb():,.0f} MB  cpu={time.process_time():.1f} s"
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
