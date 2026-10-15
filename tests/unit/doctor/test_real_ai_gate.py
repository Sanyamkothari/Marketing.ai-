"""The real-AI test (`--model product`, `make doctor-ai-gate`): it needs a Product AI and says so, resolves the
slot the way the Doctor does, and reports the seven safety checks without ever counting "not looked at" as a pass.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from pydantic import SecretStr

from engine.config import LlmConfig
from engine.doctor.evals import EvalError
from engine.settings import Settings
from scripts import plan_o_benchmark as bench
from tests.fixtures.doctor_eval.reference_model import ReferenceModel

NO_AI = "No Product AI is connected"


def _settings(tmp_path: Path, **kw: Any) -> Settings:
    return Settings(connections_key=SecretStr("A" * 40), data_dir=tmp_path / "data", **kw)


@pytest.fixture
def no_product_ai(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setattr("engine.settings.load_settings", lambda *a, **k: _settings(tmp_path))
    return tmp_path


def test_the_flags_parse_and_no_product_ai_is_a_clear_message_with_exit_2(
    no_product_ai: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    code = bench.main(
        [
            "--model",
            "product",
            "--k",
            "3",
            "--record",
            "--max-cost",
            "1",
            "--parts",
            "ai",
            "--out",
            str(no_product_ai),
        ]
    )
    err = capsys.readouterr().err
    assert code == 2
    assert NO_AI in err and "Connections" in err and "nothing was run" in err
    assert not list(
        no_product_ai.glob("plan_o_benchmark.*")
    ), "no report was written for a run that never happened"


def test_the_built_in_test_model_is_not_a_product_ai(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(
        "engine.settings.load_settings", lambda *a, **k: _settings(tmp_path, allow_fake_ai=True)
    )
    assert bench.main(["--model", "product", "--parts", "ai", "--out", str(tmp_path)]) == 2
    assert "built-in test model" in capsys.readouterr().err


def test_product_needs_the_ai_part_and_k_at_least_one() -> None:
    for argv in (
        ["--model", "product", "--parts", "tools"],
        ["--k", "0"],
        ["--model", "recorded", "--record"],
    ):
        with pytest.raises(SystemExit) as exit_:
            bench.main(argv)
        assert exit_.value.code == 2


def test_the_product_slot_is_resolved_as_the_doctor_does(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: dict[str, Any] = {}

    def resolve(llm: LlmConfig, *, slot: str, storage: Any, settings: Any, profile: Any = None) -> Any:
        seen.update(slot=slot, settings=settings)
        return SimpleNamespace(source="saved", llm=LlmConfig(), client=ReferenceModel())

    monkeypatch.setattr("engine.ai_service.resolve_client", resolve)
    settings = _settings(tmp_path)
    model = bench.choose_model("product", record=True, settings=settings)
    assert seen == {"slot": "product", "settings": settings}
    assert model.choice == "product" and model.inner is not None
    assert model.label.startswith("the Product AI, live (")


def test_a_live_run_reports_the_seven_checks_and_records_a_cassette(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr("engine.settings.load_settings", lambda *a, **k: _settings(tmp_path))
    monkeypatch.setattr(
        "engine.ai_service.resolve_client",
        lambda *a, **k: SimpleNamespace(source="saved", llm=LlmConfig(), client=ReferenceModel()),
    )
    cassette = tmp_path / "ai_path.json"
    monkeypatch.setattr(bench, "CASSETTE", cassette)
    monkeypatch.setattr(
        bench,
        "evaluate_ai",
        lambda model, *, k, cases=None, _real=bench.evaluate_ai: _real(
            model, k=k, cases=("messy", "injection_planted", "injection_control")
        ),
    )
    code = bench.main(
        ["--model", "product", "--record", "--k", "2", "--parts", "ai", "--out", str(tmp_path / "out")]
    )
    out = capsys.readouterr().out
    for number in "1234567":
        assert f"{number}. " in out
    assert "[    PASS    ] The AI answered" in out
    assert "[    PASS    ] 5. All 2 runs agree" in out
    assert "[    PASS    ] 7. A planted instruction" in out
    assert "not measured" in out.lower(), "row loss was not looked at with --parts ai, and it says so"
    assert code == 1, "a live gate that did not look at every check does not pass"
    assert (tmp_path / "out" / "safety_checks.json").exists()
    assert cassette.exists()


def _ai(**over: Any) -> dict[str, Any]:
    ok = SimpleNamespace(passed=True, problems=(), changed=())
    base: dict[str, Any] = {
        "k": 5,
        "calls": 12,
        "proposals": 4,
        "ungrounded": [],
        "questions": {"messy": 2},
        "pass_k": {"propose_step": ok},
        "cases": {"messy": {"passed": True, "problems": []}},
        "injection": {"propose_step": ok},
    }
    return base | over


def _report(**harms: Any) -> Any:
    from engine.doctor.evals import HarmCounts

    return SimpleNamespace(harms=HarmCounts(**harms))


def _status(checks: list[Any]) -> dict[str, str]:
    return {c.name.split(".")[0]: c.status for c in checks}


def test_every_check_passes_only_when_it_was_looked_at_and_clean() -> None:
    checks = bench.safety_checks(_report(), _ai())
    assert {c.status for c in checks} == {"pass"} and len(checks) == 8
    assert bench.gate_exit_code(checks, failures=[], live=True) == 0


def test_each_failure_is_named_and_fails_the_gate() -> None:
    ai = _ai(
        ungrounded=["messy (run 1): 42%"],
        questions={"messy": 4},
        pass_k={"propose_step": SimpleNamespace(passed=False, problems=("runs differ",), changed=())},
        injection={"propose_step": SimpleNamespace(passed=False, problems=(), changed=("note",))},
    )
    checks = bench.safety_checks(_report(silent_row_loss=3, pre_ticked_ai_step=1, pii_egress=2), ai)
    assert _status(checks) == {
        "The AI answered": "pass",
        "1": "fail",
        "2": "fail",
        "3": "fail",
        "4": "fail",
        "5": "fail",
        "6": "fail",
        "7": "fail",
    }
    assert bench.gate_exit_code(checks, failures=[], live=False) == 1


def test_an_ai_that_never_answered_cannot_pass_by_silence() -> None:
    checks = bench.safety_checks(_report(), _ai(calls=0, proposals=0))
    assert checks[0].status == "fail" and "prove nothing" in checks[0].detail
    assert bench.gate_exit_code(checks, failures=[], live=True) == 1


def test_a_check_that_was_not_looked_at_is_not_a_pass() -> None:
    checks = bench.safety_checks(_unmeasured(), _ai(k=1, injection={}))
    status = _status(checks)
    assert status["1"] == status["5"] == status["7"] == "not measured"
    assert bench.gate_exit_code(checks, failures=[], live=True) == 1
    assert bench.gate_exit_code(checks, failures=[], live=False) == 0, "a recorded replay may leave some out"


def _unmeasured() -> Any:
    from engine.doctor.evals import HarmCounts

    return SimpleNamespace(harms=HarmCounts(not_measured={"silent_row_loss": "no file was checked"}))


def test_a_case_passes_only_if_all_k_runs_pass_and_agree() -> None:
    def run(unacceptable: list[str], questions: int = 1) -> Any:
        return bench.AiRun(
            case="messy",
            by_tool={"propose_step": [], "propose_setting": []},
            unacceptable={"propose_step": unacceptable, "propose_setting": []},
            harms=[],
            leaks=0,
            pii_checked=True,
            calls=1,
            input_tokens=1,
            output_tokens=1,
            cost_usd=0.0,
            questions=questions,
        )

    assert bench.case_pass_k([run([]), run([])], k=2)["passed"]
    assert not bench.case_pass_k([run([]), run(["too fast"])], k=2)["passed"]
    assert not bench.case_pass_k([run([]), run([], questions=4)], k=2)["passed"]
    assert not bench.case_pass_k([run([])], k=2)["passed"], "fewer runs than k is not pass^k"


def test_no_product_ai_error_has_its_own_code(no_product_ai: Path) -> None:
    with pytest.raises(EvalError) as raised:
        bench.choose_model("product", record=False)
    assert raised.value.code == "EVAL_NO_PRODUCT_AI"


def test_no_product_ai_stops_before_the_other_parts_run(
    no_product_ai: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def must_not_run(*a: Any, **k: Any) -> Any:
        raise AssertionError("the tools part ran although no Product AI was connected")

    monkeypatch.setattr(bench, "evaluate_tools", must_not_run)
    argv = ["--model", "product", "--parts", "tools,ai", "--out", str(no_product_ai)]
    assert bench.main(argv) == 2


def test_turns_the_ai_did_not_answer_fail_the_gate() -> None:
    checks = bench.safety_checks(_report(), _ai(unanswered=2, turns=10))
    assert checks[0].status == "fail" and "did not answer 2 of 10" in checks[0].detail
    assert bench.gate_exit_code(checks, failures=[], live=True) == 1
    run = bench.AiRun(
        case="messy",
        by_tool={},
        unacceptable={},
        harms=[],
        leaks=0,
        pii_checked=True,
        calls=1,
        input_tokens=1,
        output_tokens=1,
        cost_usd=0.0,
        unanswered=1,
    )
    result = bench.case_pass_k([run], k=1)
    assert not result["passed"] and "did not answer 1 turn" in result["problems"][0]
