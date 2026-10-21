"""Run the scientist loop one stage at a time, answering the prompts with real models in between.

    python -m autonomy.scientist.run --dataset-profile profile.json --stage diagnose --record prompts.json
    # ... answer each prompt in prompts.json (e.g. with a Sonnet sub-agent) into answers.json: {id: response}
    python -m autonomy.scientist.run --dataset-profile profile.json --stage propose \\
        --record prompts.json --replay answers.json
    python -m autonomy.scientist.run --dataset-profile profile.json --stage judge \\
        --evidence evidence.json --record prompts.json --replay answers.json --out decision.json

A stage runs every earlier stage first, answering them from `--replay`, then the requested stage. The
first prompt with no answer is saved to `--record` (`{id, role, system, prompt, schema}`) and the run
stops with exit code 3: nothing calls a model and nothing is guessed. When the stage has its answers,
the typed result (with where each part came from, model or rule-based fallback, and why a model answer
was rejected) is written to `--out` or printed, and the exit code is 0. Exit code 2 is a bad input.

`--dataset-profile` is a JSON `DataProfile` (see `autonomy.scientist.profile`): numbers only.
`--evidence` (the judge stage) is a JSON `EvidenceTable`: the held-out gains of each proposed candidate.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from autonomy.agents.llm_exchange import AnswerPending, RecordingLLM, ReplayLLM
from autonomy.agents.scientist_roles import (
    EvidenceTable,
    Limits,
    candidate_ids,
    run_data_doctor,
    run_judge,
    run_proposer,
)
from autonomy.scientist.profile import DataProfile

STAGES = ("diagnose", "propose", "judge")
EXIT_DONE = 0
EXIT_BAD_INPUT = 2
EXIT_PENDING = 3


def _staged(value: Any, source: str, problems: Sequence[str]) -> dict[str, Any]:
    return {"value": value.model_dump(mode="json"), "source": source, "problems": list(problems)}


def run_stage(
    profile: DataProfile,
    stage: str,
    llm: ReplayLLM | RecordingLLM,
    *,
    limits: Limits,
    evidence: EvidenceTable | None = None,
) -> dict[str, Any]:
    """The result of `stage` (and the stages before it). Raises `AnswerPending` at the first prompt with
    no answer."""
    out: dict[str, Any] = {"stage": stage}
    diagnosis = run_data_doctor(llm, profile)
    out["diagnosis"] = _staged(diagnosis.value, diagnosis.source, diagnosis.problems)
    if stage == "diagnose":
        return out
    proposal = run_proposer(llm, profile, diagnosis.value, limits)
    out["proposal"] = _staged(proposal.value, proposal.source, proposal.problems)
    out["candidate_ids"] = list(candidate_ids(proposal.value))
    if stage == "propose":
        return out
    if evidence is None:
        raise ValueError("the judge stage needs --evidence")
    decision = run_judge(llm, profile, proposal.value, evidence)
    out["judge"] = {
        "final_pick": decision.final_pick,
        "rule_pick": decision.rule_pick,
        "source": decision.source,
        "vetoed_by": decision.vetoed_by,
        "problems": list(decision.problems),
        "code_flags": [f.model_dump(mode="json") for f in decision.code_flags],
        "report": decision.report.model_dump(mode="json"),
    }
    return out


def main(argv: Sequence[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        prog="python -m autonomy.scientist.run", description=__doc__.split("\n\n")[0]
    )
    ap.add_argument("--dataset-profile", type=Path, required=True, help="JSON DataProfile (numbers only)")
    ap.add_argument("--stage", choices=STAGES, required=True)
    ap.add_argument("--record", type=Path, required=True, help="where prompts that need answers are saved")
    ap.add_argument("--replay", type=Path, help="JSON {prompt id: response} of answered prompts")
    ap.add_argument("--evidence", type=Path, help="JSON EvidenceTable (judge stage)")
    ap.add_argument("--max-contacts-per-week", type=int, default=Limits().max_contacts_per_week)
    ap.add_argument("--out", type=Path, help="write the stage result here (default: print it)")
    args = ap.parse_args(argv)

    try:
        profile = DataProfile.model_validate_json(args.dataset_profile.read_text(encoding="utf-8"))
        evidence = (
            EvidenceTable.model_validate_json(args.evidence.read_text(encoding="utf-8"))
            if args.evidence
            else None
        )
        if args.stage == "judge" and evidence is None:
            raise ValueError("the judge stage needs --evidence")
        recorder = RecordingLLM()
        llm: ReplayLLM | RecordingLLM = (
            ReplayLLM.from_file(args.replay, fallback=recorder) if args.replay else recorder
        )
    except (OSError, ValueError, ValidationError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_BAD_INPUT

    try:
        limits = Limits(max_contacts_per_week=args.max_contacts_per_week)
        result = run_stage(profile, args.stage, llm, limits=limits, evidence=evidence)
    except ValueError as exc:  # a limit out of range, or evidence that does not match the proposal
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_BAD_INPUT
    except AnswerPending as pending:
        recorder.save(args.record)
        print(
            f"{len(recorder.records)} prompt(s) saved to {args.record}; answer them into a JSON "
            f"{{id: response}} file and rerun with --replay (waiting on {pending.prompt_id})",
            file=sys.stderr,
        )
        return EXIT_PENDING
    recorder.save(args.record)
    if isinstance(llm, ReplayLLM) and llm.unused:
        print(f"warning: answers not used by any prompt: {', '.join(llm.unused)}", file=sys.stderr)
    text = json.dumps(result, indent=2, ensure_ascii=False) + "\n"
    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(text, encoding="utf-8")
    else:
        sys.stdout.write(text)
    return EXIT_DONE


if __name__ == "__main__":
    raise SystemExit(main())
