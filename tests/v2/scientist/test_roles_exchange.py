"""RecordingLLM / ReplayLLM and the stage CLI: answer the exact prompts with real models in between."""

from __future__ import annotations

import hashlib
import json
import socket
from pathlib import Path
from typing import Any

import pytest

from autonomy.agents.base import ask_json, load_prompt
from autonomy.agents.llm_exchange import (
    AnswerPending,
    RecordingLLM,
    ReplayLLM,
    load_records,
    prompt_id,
    role_of,
    schema_of,
)
from autonomy.agents.scientist_roles import (
    SAFE_DEFAULT_ID,
    CandidateEvidence,
    DataDoctorDiagnosis,
    EvidenceTable,
    Limits,
    Proposal,
    candidate_ids,
    default_proposal,
    rule_diagnosis,
    run_data_doctor,
)
from autonomy.scientist.profile import DataProfile, build_profile
from autonomy.scientist.run import EXIT_BAD_INPUT, EXIT_DONE, EXIT_PENDING, main
from engine.llm import LLMError
from tests.v2.scientist.test_roles_profile import make_events
from tests.v2.scientist.test_roles_roles import FEATURES, GOOD, adequate


@pytest.fixture(scope="module")
def profile() -> DataProfile:
    return adequate(build_profile(make_events(), features=FEATURES))


# --- ids, roles, schema --------------------------------------------------------------------------


def test_role_from_the_first_heading() -> None:
    assert role_of("# Data Doctor\n\nYou are...") == "data_doctor"
    assert role_of("preamble\n\n## Not this\n# Judge\n") == "judge"
    assert role_of("no heading here") == "unknown"
    for name, role in (("data_doctor", "data_doctor"), ("proposer", "proposer"), ("judge", "judge")):
        assert role_of(load_prompt(name)) == role


def test_prompt_id_is_stable_and_depends_on_every_part() -> None:
    a = prompt_id("judge", "sys", "prompt")
    assert a == prompt_id("judge", "sys", "prompt")
    assert a == "judge-" + hashlib.sha256(json.dumps(["judge", "sys", "prompt"]).encode()).hexdigest()[:12]
    assert (
        len(
            {
                a,
                prompt_id("proposer", "sys", "prompt"),
                prompt_id("judge", "sys2", "prompt"),
                prompt_id("judge", "sys", "prompt2"),
            }
        )
        == 4
    )


def test_schema_is_read_back_from_the_prompt() -> None:
    llm = RecordingLLM()
    with pytest.raises(AnswerPending):
        ask_json(llm, DataDoctorDiagnosis, system="# Data Doctor\nx", prompt="Diagnose.")
    assert llm.records[0]["schema"] == DataDoctorDiagnosis.model_json_schema()
    assert schema_of("no schema here") is None
    assert schema_of("matching this JSON schema:\nnot json") is None


# --- RecordingLLM --------------------------------------------------------------------------------


def test_recording_saves_id_role_system_prompt_schema_and_calls_no_model(tmp_path: Path) -> None:
    llm = RecordingLLM()
    system = load_prompt("data_doctor")
    with pytest.raises(AnswerPending) as stop:
        ask_json(llm, DataDoctorDiagnosis, system=system, prompt="Diagnose this.")
    (rec,) = llm.records
    assert set(rec) == {"id", "role", "system", "prompt", "schema"}
    assert rec["id"] == stop.value.prompt_id and rec["id"].startswith("data_doctor-")
    assert (
        rec["role"] == "data_doctor"
        and rec["system"] == system
        and rec["prompt"].startswith("Diagnose this.")
    )
    path = tmp_path / "deep" / "prompts.json"
    llm.save(path)
    assert load_records(path) == llm.records
    assert json.loads(path.read_text()) == llm.records


def test_recording_the_same_prompt_twice_keeps_one_record() -> None:
    llm = RecordingLLM()
    for _ in range(2):
        with pytest.raises(AnswerPending):
            llm.complete("p", system="# Judge\n")
    assert len(llm.records) == 1


def test_pending_is_not_an_llm_error_so_roles_do_not_fall_back_on_it(profile: DataProfile) -> None:
    assert not issubclass(AnswerPending, LLMError)
    with pytest.raises(AnswerPending):
        run_data_doctor(RecordingLLM(), profile)  # stops; does not return the rule-based diagnosis


# --- ReplayLLM -----------------------------------------------------------------------------------


def test_replay_answers_by_id_text_or_object() -> None:
    system, prompt = "# Judge\n", "p"
    pid = prompt_id("judge", system, prompt)
    assert ReplayLLM({pid: "hello"}).complete(prompt, system=system).text == "hello"
    obj = ReplayLLM({pid: {"a": 1}}).complete(prompt, system=system)
    assert json.loads(obj.text) == {"a": 1} and obj.model_id == "replay"


def test_replay_without_an_answer_falls_back_to_the_recorder_or_errors() -> None:
    with pytest.raises(LLMError):
        ReplayLLM({}).complete("p", system="# Judge\n")
    rec = RecordingLLM()
    with pytest.raises(AnswerPending):
        ReplayLLM({}, fallback=rec).complete("p", system="# Judge\n")
    assert len(rec.records) == 1


def test_replay_reports_unused_answers() -> None:
    r = ReplayLLM({"x-1": "a", prompt_id("judge", "# Judge\n", "p"): "b"})
    r.complete("p", system="# Judge\n")
    assert r.unused == ("x-1",)


def test_replay_from_file_rejects_a_list(tmp_path: Path) -> None:
    f = tmp_path / "a.json"
    f.write_text("[]")
    with pytest.raises(ValueError):
        ReplayLLM.from_file(f)


def test_a_replayed_bad_answer_records_a_repair_prompt() -> None:
    """ask_json asks again with the error: that prompt has a new id, so it is recorded, not replayed."""
    rec = RecordingLLM()
    system, prompt = "# Data Doctor\n", "Diagnose."
    # the first prompt as ask_json will build it
    probe = RecordingLLM()
    with pytest.raises(AnswerPending) as p:
        ask_json(probe, DataDoctorDiagnosis, system=system, prompt=prompt)
    replay = ReplayLLM({p.value.prompt_id: "this is not json"}, fallback=rec)
    with pytest.raises(AnswerPending) as repair:
        ask_json(replay, DataDoctorDiagnosis, system=system, prompt=prompt)
    assert repair.value.prompt_id != p.value.prompt_id
    assert (
        "Your previous answer was not valid" in rec.records[0]["prompt"]
        and "this is not json" in rec.records[0]["prompt"]
    )
    # with no recorder the repair round has no answer: an LLMError, which the roles turn into the default
    with pytest.raises(LLMError):
        ask_json(
            ReplayLLM({p.value.prompt_id: "this is not json"}),
            DataDoctorDiagnosis,
            system=system,
            prompt=prompt,
        )


# --- the CLI: diagnose -> propose -> judge -------------------------------------------------------


@pytest.fixture
def no_network(monkeypatch: pytest.MonkeyPatch) -> None:
    def refuse(*_a: Any, **_k: Any) -> None:
        raise AssertionError("the exchange must not touch the network")

    monkeypatch.setattr(socket.socket, "connect", refuse)
    monkeypatch.setattr(socket, "create_connection", refuse)


def write_profile(tmp_path: Path, profile: DataProfile) -> Path:
    path = tmp_path / "profile.json"
    path.write_text(profile.model_dump_json())
    return path


def evidence_file(tmp_path: Path, best: str = "c2_segment_rule") -> Path:
    ids = candidate_ids(Proposal.model_validate_json(GOOD))
    rows = tuple(
        CandidateEvidence(
            candidate_id=cid,
            n_eval=1000,
            gain_estimate=2.0 if cid == best else 0.5,
            gain_low=0.9 if cid == best else -0.4,
            gain_high=3.1 if cid == best else 1.4,
        )
        for cid in ids
    )
    table = EvidenceTable(
        unit="profit per customer", correction="holm", comparisons=3, current_value=10.0, candidates=rows
    )
    path = tmp_path / "evidence.json"
    path.write_text(table.model_dump_json())
    return path


def answer(records: list[dict[str, Any]], profile: DataProfile) -> dict[str, str]:
    """A stand-in for the real-model step: one valid answer per recorded prompt, by role."""
    out: dict[str, str] = {}
    for r in records:
        if r["role"] == "data_doctor":
            out[r["id"]] = rule_diagnosis(profile).model_dump_json()
        elif r["role"] == "proposer":
            out[r["id"]] = GOOD
        else:
            out[r["id"]] = json.dumps(
                {
                    "pick": "c2_segment_rule",
                    "too_good_to_be_true": [],
                    "explanation": "The segment rule has the best lower bound. It starts in Shadow.",
                    "needs_human": None,
                }
            )
    return out


def test_three_stages_round_trip(
    tmp_path: Path, profile: DataProfile, no_network: None, capsys: pytest.CaptureFixture[str]
) -> None:
    pf, ev_path = write_profile(tmp_path, profile), evidence_file(tmp_path)
    prompts, answers = tmp_path / "out" / "prompts.json", tmp_path / "answers.json"
    base = ["--dataset-profile", str(pf), "--record", str(prompts)]
    pool: dict[str, str] = {}

    def run(stage: str, *more: str) -> int:
        argv = [*base, "--stage", stage, *more]
        if pool:
            answers.write_text(json.dumps(pool))
            argv += ["--replay", str(answers)]
        return main(argv)

    # diagnose: nothing answered yet -> the doctor's prompt is saved, exit 3
    assert run("diagnose") == EXIT_PENDING
    recs = load_records(prompts)
    assert [r["role"] for r in recs] == ["data_doctor"] and recs[0]["schema"][
        "title"
    ] == "DataDoctorDiagnosis"
    pool.update(answer(recs, profile))
    # diagnose again with its answer: done, nothing left to record
    assert run("diagnose", "--out", str(tmp_path / "d.json")) == EXIT_DONE
    d = json.loads((tmp_path / "d.json").read_text())
    assert d["stage"] == "diagnose" and d["diagnosis"]["source"] == "model" and "proposal" not in d
    assert load_records(prompts) == []
    # propose: the doctor is replayed, only the proposer's prompt is new
    assert run("propose", "--max-contacts-per-week", "3") == EXIT_PENDING
    recs = load_records(prompts)
    assert [r["role"] for r in recs] == ["proposer"] and "Diagnosis:" in recs[0]["prompt"]
    pool.update(answer(recs, profile))
    assert run("propose", "--max-contacts-per-week", "3") == EXIT_DONE
    p = json.loads(capsys.readouterr().out)
    assert (
        p["candidate_ids"] == ["c1_send_to_all", "c2_segment_rule", "c3_bandit_agent"]
        and p["proposal"]["source"] == "model"
    )
    # judge: needs evidence; both earlier stages replayed; the judge prompt is recorded
    assert run("judge", "--evidence", str(ev_path), "--max-contacts-per-week", "3") == EXIT_PENDING
    recs = load_records(prompts)
    assert [r["role"] for r in recs] == ["judge"] and "Evidence rule's pick: c2_segment_rule" in recs[0][
        "prompt"
    ]
    pool.update(answer(recs, profile))
    assert (
        run(
            "judge",
            "--evidence",
            str(ev_path),
            "--max-contacts-per-week",
            "3",
            "--out",
            str(tmp_path / "j.json"),
        )
        == EXIT_DONE
    )
    j = json.loads((tmp_path / "j.json").read_text())["judge"]
    assert (j["final_pick"], j["rule_pick"], j["source"], j["vetoed_by"]) == (
        "c2_segment_rule",
        "c2_segment_rule",
        "model",
        None,
    )
    assert j["report"]["explanation"].startswith("The segment rule")


def test_invalid_answer_is_repaired_then_rejected_safely(
    tmp_path: Path, profile: DataProfile, no_network: None
) -> None:
    pf, prompts, answers = write_profile(tmp_path, profile), tmp_path / "p.json", tmp_path / "a.json"
    argv = ["--dataset-profile", str(pf), "--stage", "diagnose", "--record", str(prompts)]
    assert main(argv) == EXIT_PENDING
    (rec,) = load_records(prompts)
    answers.write_text(json.dumps({rec["id"]: "garbage"}))
    assert main([*argv, "--replay", str(answers)]) == EXIT_PENDING  # the repair prompt is next
    (repair,) = load_records(prompts)
    assert repair["id"] != rec["id"] and "garbage" in repair["prompt"]
    bad = json.loads(DataDoctorDiagnosis.model_dump_json(rule_diagnosis(profile)))
    bad["click_trust"] = "trust"  # parses, but breaks the profile's limit
    answers.write_text(json.dumps({rec["id"]: "garbage", repair["id"]: bad}))
    out = tmp_path / "o.json"
    assert main([*argv, "--replay", str(answers), "--out", str(out)]) == EXIT_DONE
    res = json.loads(out.read_text())["diagnosis"]
    assert res["source"] == "fallback" and any("click_trust" in x for x in res["problems"])
    assert res["value"]["click_trust"] != "trust"


def test_bad_inputs_exit_2(tmp_path: Path, profile: DataProfile, capsys: pytest.CaptureFixture[str]) -> None:
    pf, prompts = write_profile(tmp_path, profile), tmp_path / "p.json"
    base = ["--record", str(prompts)]
    assert main(["--dataset-profile", str(pf), "--stage", "judge", *base]) == EXIT_BAD_INPUT  # no evidence
    assert "--evidence" in capsys.readouterr().err
    assert (
        main(["--dataset-profile", str(tmp_path / "missing.json"), "--stage", "diagnose", *base])
        == EXIT_BAD_INPUT
    )
    junk = tmp_path / "junk.json"
    junk.write_text('{"customers": "many"}')
    assert main(["--dataset-profile", str(junk), "--stage", "diagnose", *base]) == EXIT_BAD_INPUT
    lst = tmp_path / "list.json"
    lst.write_text("[]")
    assert (
        main(["--dataset-profile", str(pf), "--stage", "diagnose", *base, "--replay", str(lst)])
        == EXIT_BAD_INPUT
    )


def test_unused_answers_are_warned_about(
    tmp_path: Path, profile: DataProfile, capsys: pytest.CaptureFixture[str]
) -> None:
    pf, prompts, answers = write_profile(tmp_path, profile), tmp_path / "p.json", tmp_path / "a.json"
    argv = ["--dataset-profile", str(pf), "--stage", "diagnose", "--record", str(prompts)]
    assert main(argv) == EXIT_PENDING
    (rec,) = load_records(prompts)
    answers.write_text(json.dumps({rec["id"]: rule_diagnosis(profile).model_dump_json(), "stale-123": "x"}))
    assert main([*argv, "--replay", str(answers), "--out", str(tmp_path / "o.json")]) == EXIT_DONE
    assert "stale-123" in capsys.readouterr().err


def test_stage_results_are_typed_and_inside_the_menu(tmp_path: Path, profile: DataProfile) -> None:
    """Whatever a real model answers, what comes out is a valid Proposal / DataDoctorDiagnosis."""
    pf, prompts, answers = write_profile(tmp_path, profile), tmp_path / "p.json", tmp_path / "a.json"
    argv = ["--dataset-profile", str(pf), "--stage", "propose", "--record", str(prompts)]
    pool: dict[str, str] = {}
    out = tmp_path / "o.json"
    for _ in range(4):
        if main([*argv, "--replay", str(answers), "--out", str(out)] if pool else argv) == EXIT_DONE:
            break
        pool.update(
            {
                r["id"]: ("{}" if r["role"] == "proposer" else answer([r], profile)[r["id"]])
                for r in load_records(prompts)
            }
        )
        answers.write_text(json.dumps(pool))
    res = json.loads(out.read_text())
    assert res["proposal"]["source"] == "fallback"  # "{}" is not a proposal
    assert Proposal.model_validate(res["proposal"]["value"]) == default_proposal(profile, Limits())
    assert SAFE_DEFAULT_ID not in res["candidate_ids"]


def test_evidence_that_does_not_match_the_proposal_exits_2(
    tmp_path: Path, profile: DataProfile, no_network: None, capsys: pytest.CaptureFixture[str]
) -> None:
    pf, prompts, answers = write_profile(tmp_path, profile), tmp_path / "p.json", tmp_path / "a.json"
    base = ["--dataset-profile", str(pf), "--record", str(prompts), "--max-contacts-per-week", "3"]
    assert main([*base, "--stage", "diagnose"]) == EXIT_PENDING
    pool = answer(load_records(prompts), profile)
    answers.write_text(json.dumps(pool))
    assert main([*base, "--stage", "propose", "--replay", str(answers)]) == EXIT_PENDING
    pool.update(answer(load_records(prompts), profile))
    answers.write_text(json.dumps(pool))
    one = EvidenceTable(
        unit="profit per customer",
        correction="holm",
        comparisons=1,
        candidates=(
            CandidateEvidence(
                candidate_id="c1_send_to_all", n_eval=1000, gain_estimate=1, gain_low=0.5, gain_high=2
            ),
        ),
    )
    ev_path = tmp_path / "one.json"
    ev_path.write_text(one.model_dump_json())
    capsys.readouterr()
    argv = [*base, "--stage", "judge", "--replay", str(answers), "--evidence", str(ev_path)]
    assert main(argv) == EXIT_BAD_INPUT
    assert "do not match" in capsys.readouterr().err
    assert main([*base[:-1], "0", "--stage", "diagnose"]) == EXIT_BAD_INPUT
    assert "at least 1" in capsys.readouterr().err
