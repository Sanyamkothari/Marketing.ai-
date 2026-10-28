"""A policy for every proposal kind (Plan O M233, DEC-1803): default deny, one rule set per PROPOSE kind.

The rules are pure functions of `(DataProposal, DataPolicyContext)`, so each test builds the facts by hand
(`policy_cases`); `test_policy_runtime.py` checks the same rules where Plan G's handlers ask them.
"""

from __future__ import annotations

import dataclasses
import shutil
from pathlib import Path
from typing import Any

import pytest

from engine.access.roles import Role
from engine.agent.advisor import NEVER_TICKED_STEPS
from engine.agent.config import AgentLevel
from engine.agent.contracts import RecipeStepKind, StepReceipt
from engine.agent.edits import PERSON_STEP_KINDS
from engine.agent.loop import CHAT_STEP_KINDS, DOCTOR_STEP_KINDS, NEVER_SUGGESTED_BY_CHAT
from engine.agent.recipe import EXCEL_SERIAL_PARAMS
from engine.agent.recommend import NEVER_RECOMMENDED
from engine.agentic.config import KNOWN_RULES, load_policy
from engine.agentic.errors import AgenticError
from engine.agentic.policy import RULES
from engine.config import ADVISORY_PATHS
from engine.doctor import policy as pol
from engine.doctor.catalogue import CATALOGUE
from engine.doctor.policy import (
    ApprovalFacts,
    DataPolicyConfig,
    DataProposal,
    SettingChange,
    StepEffect,
    StepFacts,
    allowed,
    evaluate,
    load_data_policy,
    policy_kind_for,
)
from engine.doctor.registry import DataToolKind, data_registry
from engine.doctor.reshape_guard import ReshapeItem
from tests.unit.doctor.policy_cases import (
    ANALYST,
    APPROVER,
    PII,
    PK,
    PLAIN,
    TARGET,
    VIEWER,
    base,
    context,
    shipped,
    step,
)


def codes(decisions: tuple[Any, ...]) -> set[str]:
    return {d.code for d in decisions if not d.allowed}


def rules_of(kind: str) -> Any:
    return shipped().rule_sets[kind]


def ai_step(kind: str, facts: StepFacts, **change: Any) -> DataProposal:
    made = DataProposal(
        kind=kind,
        source="ai",
        proposed_by="u-asha",
        steps=(facts,),
        looked_at=True,
        effect=StepEffect(changed=5, failed=0, out_of=100, rows_tried=100),
    )
    return dataclasses.replace(made, **change)


# ---------------------------------------------------------------------------
# Default deny
# ---------------------------------------------------------------------------
def test_a_propose_kind_with_no_rule_set_is_refused_with_policy_no_rules() -> None:
    # `data.define_outcome` was here until M239 (DEC-1809) gave it its rule set, `data.convert_currency` until M242
    # (DEC-1812) and `data.propose_time_settings` until M244 (DEC-1814); a kind still unbuilt stands in.
    for kind in ("data.map_consent", "data.drift_check", "data.anything"):
        decisions = evaluate(DataProposal(kind=kind, source="rules"), context())
        assert codes(decisions) == {"POLICY_NO_RULES"} and not allowed(decisions)


def test_a_file_without_a_data_section_refuses_every_data_proposal(tmp_path: Path, config_root: Path) -> None:
    copy = tmp_path / "configs"
    shutil.copytree(config_root / "agentic", copy / "agentic")
    text = (copy / "agentic" / "policy.yaml").read_text(encoding="utf-8")
    (copy / "agentic" / "policy.yaml").write_text(text[: text.index("\ndata:")] + "\n", encoding="utf-8")
    empty = load_data_policy(copy)
    assert empty.rule_sets == {}
    subject = base("data.recipe_step", rules_of("data.recipe_step"))
    assert codes(evaluate(subject, context(empty))) == {"POLICY_NO_RULES"}
    assert (
        load_policy(copy).rule_sets == load_policy(config_root).rule_sets
    ), "Plan K's rule sets read as before"


def test_a_missing_policy_file_is_refused(tmp_path: Path) -> None:
    with pytest.raises(AgenticError) as refused:
        load_data_policy(tmp_path)
    assert refused.value.code == "CONFIG_INVALID"
    assert pol.policy_or_deny(tmp_path).rule_sets == {}, "a policy that cannot be read allows nothing"


def test_a_rule_set_that_names_an_unknown_rule_is_refused(tmp_path: Path, config_root: Path) -> None:
    copy = tmp_path / "configs"
    shutil.copytree(config_root / "agentic", copy / "agentic")
    path = copy / "agentic" / "policy.yaml"
    path.write_text(
        path.read_text(encoding="utf-8").replace(
            "rules: [data.setting]", "rules: [data.setting, data.trust_me]"
        ),
        encoding="utf-8",
    )
    with pytest.raises(AgenticError) as refused:
        load_data_policy(copy)
    assert refused.value.code == "CONFIG_INVALID" and "data.trust_me" in refused.value.message


def test_a_proposal_not_shaped_as_its_kind_is_refused() -> None:
    steps = base("data.recipe_step", rules_of("data.recipe_step"))
    as_setting = dataclasses.replace(steps, kind="data.setting")
    no_steps = dataclasses.replace(steps, steps=())
    assert codes(evaluate(as_setting, context())) == {"POLICY_PAYLOAD_INVALID"}
    assert codes(evaluate(no_steps, context())) == {"POLICY_PAYLOAD_INVALID"}


def test_disabling_every_rule_never_allows_anything() -> None:
    for kind, rules in shipped().rule_sets.items():
        assert codes(evaluate(base(kind, rules), context(), rules={})) == {"POLICY_NO_RULES"}, kind


def test_every_shipped_rule_set_allows_its_own_proposal() -> None:
    for kind, rules in shipped().rule_sets.items():
        decisions = evaluate(base(kind, rules), context())
        assert allowed(decisions), (kind, [d for d in decisions if not d.allowed])


def test_plan_k_policy_is_untouched() -> None:
    assert set(RULES) == set(KNOWN_RULES)
    assert not set(pol.DATA_RULE_NAMES) & set(KNOWN_RULES)


# ---------------------------------------------------------------------------
# One rule set for every PROPOSE kind
# ---------------------------------------------------------------------------
def _declared_tools() -> dict[str, tuple[str, frozenset[RecipeStepKind]]]:
    """Every tool declared so far that proposes something: its policy kind and the step kinds it proposes.

    M232's registered PROPOSE tools, and the wave-1 specs not yet registered (M237 `tools_files`, M238
    `tools_rows`, M240 `tools_formats`, M241 `tools_locale`), including READ tools that return pending steps.
    """
    from engine.doctor import tools_files, tools_formats, tools_locale, tools_rows

    found: dict[str, tuple[str, frozenset[RecipeStepKind]]] = {}
    for tool in data_registry().list(kind=DataToolKind.PROPOSE):
        assert tool.policy_kind is not None
        found[tool.name] = (tool.policy_kind, frozenset())
    for files_spec in tools_files.TOOLS:
        if files_spec.kind == "propose":
            kinds = (
                {RecipeStepKind.NORMALISE_KEY}
                if files_spec.name in {"normalise_key", "build_dataset"}
                else set()
            )
            found[files_spec.name] = (files_spec.policy_kind, frozenset(kinds))
    for row_spec in tools_rows.ROW_TOOLS:
        if row_spec.kind == "propose" or row_spec.step_kinds:
            found[row_spec.name] = (policy_kind_for(row_spec.name), frozenset(row_spec.step_kinds))
    for locale_spec in tools_locale.LOCALE_TOOLS:
        if locale_spec.kind == "propose":
            found[locale_spec.name] = (
                policy_kind_for(locale_spec.name),
                frozenset(RecipeStepKind(k) for k in locale_spec.proposes),
            )
    for format_spec in tools_formats.FORMAT_TOOLS:
        if format_spec.name == "find_excel_serial_dates":  # a READ tool whose result is a pending parse_date
            found[format_spec.name] = (
                policy_kind_for(format_spec.name),
                frozenset({RecipeStepKind.PARSE_DATE}),
            )
    return found


def test_every_declared_propose_tool_has_a_rule_set_that_allows_what_it_proposes() -> None:
    declared = _declared_tools()
    assert {"propose_step", "propose_setting", "edit_by_hand", "join_files", "keep_latest_per_id"} <= set(
        declared
    )
    assert {"parse_indian_numbers", "indian_booleans", "find_excel_serial_dates", "split_file"} <= set(
        declared
    )
    for name, (kind, step_kinds) in declared.items():
        rules = shipped().rule_sets.get(kind)
        assert rules is not None, f"{name} ({kind}) has no rule set"
        assert step_kinds <= set(rules.step_kinds), (name, step_kinds - set(rules.step_kinds))


def test_the_catalogues_unbuilt_propose_kinds_stay_refused_until_their_milestone_adds_rules() -> None:
    built = {kind for kind, _ in _declared_tools().values()}
    unbuilt = [
        row.name for row in CATALOGUE if row.kind == "propose" and policy_kind_for(row.name) not in built
    ]
    assert "map_consent" in unbuilt
    # Plan O M242 (DEC-1812 (k)) and M239 (DEC-1809): convert_currency and the outcome tools are built, with
    # their rule sets.
    assert "convert_currency" not in unbuilt
    assert "define_outcome" not in unbuilt and "map_outcome_values" not in unbuilt
    for name in unbuilt:
        assert policy_kind_for(name) not in shipped().rule_sets, name


def test_the_rule_sets_hold_the_rules_the_code_holds_today() -> None:
    """The hard-coded rules are read from the rule sets too; the code keeps its own as the second check."""
    policy = shipped()
    # The Data Doctor's chat may also leave out the rows a rule names (a deliberate change: "drop the test accounts");
    # Guided setup's chat does not, its own list (`CHAT_STEP_KINDS`) is checked first and still refuses it.
    assert set(policy.rule_sets["data.recipe_step"].step_kinds) == set(DOCTOR_STEP_KINDS)
    assert set(DOCTOR_STEP_KINDS) - set(CHAT_STEP_KINDS) == {RecipeStepKind.REMOVE_ROWS}
    assert set(policy.rule_sets["data.person_edit"].step_kinds) == set(PERSON_STEP_KINDS)
    assert set(policy.never_ticked_steps) == set(NEVER_TICKED_STEPS)
    assert (
        set(policy.settings_never_suggested) == NEVER_SUGGESTED_BY_CHAT | NEVER_RECOMMENDED | ADVISORY_PATHS
    )
    chat_dates = policy.rule_sets["data.recipe_step"].params[RecipeStepKind.PARSE_DATE]
    assert not EXCEL_SERIAL_PARAMS & set(chat_dates.allowed), "Excel day numbers stay the rules' fix (M240)"


# ---------------------------------------------------------------------------
# Each rule, with the code the same refusal has today
# ---------------------------------------------------------------------------
def test_the_chat_may_suggest_only_its_kinds() -> None:
    rules = rules_of("data.recipe_step")
    for kind in (RecipeStepKind.CORRECT_CELLS, RecipeStepKind.COMBINE_ROWS):
        subject = ai_step("data.recipe_step", StepFacts(kind, PLAIN))
        assert "AGENT_STEP_NOT_ALLOWED" in codes(evaluate(subject, context())), kind
    person = DataProposal(
        kind="data.person_edit", source="person", steps=(StepFacts(RecipeStepKind.PARSE_NUMBER, PLAIN),)
    )
    assert codes(evaluate(person, context())) == {"AGENT_EDIT_NOT_ALLOWED"}
    assert allowed(evaluate(ai_step("data.recipe_step", step(RecipeStepKind.MAP_VALUES, rules)), context()))


def test_leaving_rows_out_is_allowed_by_a_listed_rule_only() -> None:
    """The Data Doctor's chat may leave rows out by `equals`, `in`, `starts_with`, `is_empty`, `before` or `after`;
    any other parameter or operator is refused with the recipe's own code."""
    rules = rules_of("data.recipe_step")
    ok = step(RecipeStepKind.REMOVE_ROWS, rules, params={"operator": "starts_with", "values": ["999"]})
    assert allowed(evaluate(ai_step("data.recipe_step", ok), context()))
    for params in (
        {"operator": "regex", "values": ["9.*"]},
        {"operator": "equals", "values": ["x"], "column": "y"},
    ):
        bad = step(RecipeStepKind.REMOVE_ROWS, rules, params=params)
        assert "RECIPE_STEP_INVALID" in codes(evaluate(ai_step("data.recipe_step", bad), context())), params


def test_never_the_id_or_the_outcome() -> None:
    rules = rules_of("data.recipe_step")
    for column in (PK, TARGET):
        subject = ai_step("data.recipe_step", step(RecipeStepKind.MAP_VALUES, rules, column=column))
        assert codes(evaluate(subject, context())) == {"RECIPE_STEP_INVALID"}, column
    derived = DataProposal(
        kind="data.person_edit",
        source="person",
        steps=(
            StepFacts(RecipeStepKind.SPLIT_COLUMN, PLAIN, {"separator": "-", "part": 0}, new_column=TARGET),
        ),
    )
    assert codes(evaluate(derived, context())) == {"RECIPE_STEP_INVALID"}
    # Leaving out the rows whose ID begins 999 changes no ID (a deliberate change, with the Doctor's chat): allowed on the
    # ID, never on the outcome.
    leaves = {"operator": "starts_with", "values": ["999"]}
    by_id = step(RecipeStepKind.REMOVE_ROWS, rules, column=PK, params=leaves)
    assert allowed(evaluate(ai_step("data.recipe_step", by_id), context()))
    by_outcome_rows = step(RecipeStepKind.REMOVE_ROWS, rules, column=TARGET, params=leaves)
    assert codes(evaluate(ai_step("data.recipe_step", by_outcome_rows), context())) == {"RECIPE_STEP_INVALID"}
    latest = rules_of("data.keep_latest_per_id")
    by_outcome = step(
        RecipeStepKind.KEEP_LATEST_PER_ID, latest, params={"time_column": TARGET, "ties": "refuse"}
    )
    subject = dataclasses.replace(base("data.keep_latest_per_id", latest), steps=(by_outcome,))
    assert codes(evaluate(subject, context())) == {"RECIPE_STEP_INVALID"}


def test_personal_data_can_only_be_hidden() -> None:
    rules = rules_of("data.recipe_step")
    mapped = ai_step("data.recipe_step", step(RecipeStepKind.MAP_VALUES, rules, column=PII))
    hidden = ai_step("data.recipe_step", StepFacts(RecipeStepKind.DROP_COLUMN, PII))
    assert codes(evaluate(mapped, context())) == {"AGENT_STEP_NOT_ALLOWED"}
    assert allowed(evaluate(hidden, context()))


def test_levels_follow_the_use_case_and_the_rule_set() -> None:
    rules = rules_of("data.recipe_step")
    split = ai_step("data.recipe_step", step(RecipeStepKind.SPLIT_COLUMN, rules))
    assert allowed(evaluate(split, context()))
    assert codes(evaluate(split, context(levels=(AgentLevel.CLEAN,)))) == {"RECIPE_STEP_INVALID"}
    latest = base("data.keep_latest_per_id", rules_of("data.keep_latest_per_id"))
    assert allowed(evaluate(latest, context()))
    assert codes(evaluate(latest, context(levels=(AgentLevel.CLEAN, AgentLevel.DERIVE)))) == {
        "RECIPE_STEP_INVALID"
    }


def test_decision_o3_puts_reshape_in_every_goals_levels_only_with_data_doctor_on() -> None:
    from engine.doctor.reshape_guard import doctor_levels

    assert AgentLevel.RESHAPE in doctor_levels((AgentLevel.CLEAN, AgentLevel.DERIVE))


def test_the_setting_allow_list() -> None:
    ok = DataProposal(kind="data.setting", source="ai", setting=SettingChange("split.test_fraction", 0.2))
    assert allowed(evaluate(ok, context()))
    for path, value in (
        ("prepare.exclude_columns", [PLAIN]),
        ("validation.min_rows", 10),
        ("features.selection", "auto"),
        ("no.such.setting", 1),
        ("split.test_fraction", 5.0),
    ):
        refused = dataclasses.replace(ok, setting=SettingChange(path, value))
        assert codes(evaluate(refused, context())) == {"AGENT_SETTING_NOT_ALLOWED"}, path


def test_never_ticked_kinds_and_ai_proposals_never_start_ticked() -> None:
    impossible = rules_of("data.impossible_values")
    ranged = dataclasses.replace(base("data.impossible_values", impossible), ticked=True, confidence="sure")
    assert codes(evaluate(ranged, context())) == {"POLICY_NEVER_TICKED"}
    chat = ai_step(
        "data.recipe_step", step(RecipeStepKind.MAP_VALUES, rules_of("data.recipe_step")), ticked=True
    )
    assert codes(evaluate(chat, context())) == {"POLICY_NEVER_TICKED"}
    units = dataclasses.replace(base("data.detect_units", rules_of("data.detect_units")), ticked=True)
    sure = dataclasses.replace(units, confidence="sure")
    assert allowed(
        evaluate(sure, context())
    ), "a rules suggestion of an ordinary kind may start ticked when sure"
    checked = dataclasses.replace(units, confidence="check")
    assert codes(evaluate(checked, context())) == {"POLICY_NEVER_TICKED"}, "M238: a `check` step is pending"
    person = DataProposal(
        kind="data.person_edit",
        source="person",
        steps=(step(RecipeStepKind.VALID_RANGE, impossible),),
        ticked=True,
    )
    assert allowed(evaluate(person, context())), "the person's own edit is ticked by them"


# ---------------------------------------------------------------------------
# What the wave-1 milestones asked for
# ---------------------------------------------------------------------------
def test_m240_excel_day_numbers_are_the_rules_fix_never_the_chats() -> None:
    chat = rules_of("data.recipe_step")
    serial = StepFacts(RecipeStepKind.PARSE_DATE, PLAIN, {"excel_serial": True})
    assert codes(evaluate(ai_step("data.recipe_step", serial), context())) == {"RECIPE_STEP_INVALID"}
    assert allowed(evaluate(ai_step("data.recipe_step", step(RecipeStepKind.PARSE_DATE, chat)), context()))
    rules = rules_of("data.excel_serial_dates")
    own = base("data.excel_serial_dates", rules)
    assert own.steps[0].params == {"excel_serial": True} and allowed(evaluate(own, context()))
    ordered = dataclasses.replace(
        own, steps=(StepFacts(RecipeStepKind.PARSE_DATE, PLAIN, {"dayfirst": True}),)
    )
    assert codes(evaluate(ordered, context())) == {"RECIPE_STEP_INVALID"}
    unsure = dataclasses.replace(own, ticked=True, confidence="check")
    assert codes(evaluate(unsure, context())) == {"POLICY_NEVER_TICKED"}


def test_m241_parse_indian_numbers_rule_set() -> None:
    rules = rules_of("data.parse_indian_numbers")
    own = base("data.parse_indian_numbers", rules)
    assert own.steps[0].params == {"decimal": ".", "indian_scale_words": True}
    assert allowed(evaluate(own, context()))
    assert allowed(evaluate(dataclasses.replace(own, ticked=True, confidence="sure"), context()))
    assert codes(evaluate(dataclasses.replace(own, ticked=True), context())) == {"POLICY_NEVER_TICKED"}
    for params in (
        {"decimal": "."},
        {"decimal": ".", "indian_scale_words": False},
        {"decimal": ";", "indian_scale_words": True},
        {"indian_scale_words": True, "unit": "INR"},
    ):
        broken = dataclasses.replace(own, steps=(StepFacts(RecipeStepKind.PARSE_NUMBER, PLAIN, params),))
        assert codes(evaluate(broken, context())) == {"RECIPE_STEP_INVALID"}, params
    for column in (PK, TARGET):
        on_key = dataclasses.replace(own, steps=(dataclasses.replace(own.steps[0], column=column),))
        assert codes(evaluate(on_key, context())) == {"RECIPE_STEP_INVALID"}
    ai_ticked = dataclasses.replace(own, source="ai", looked_at=True, ticked=True, confidence="sure")
    assert codes(evaluate(ai_ticked, context())) == {"POLICY_NEVER_TICKED"}


def _booleans(true: list[str], false: list[str], source: str = "rules") -> DataProposal:
    rules = rules_of("data.indian_booleans")
    own = base("data.indian_booleans", rules)
    facts = StepFacts(RecipeStepKind.MAP_BOOLEAN, PLAIN, {"true_values": true, "false_values": false})
    return dataclasses.replace(own, steps=(facts,), source=source)  # type: ignore[arg-type]


def test_m241_indian_booleans_both_sides_from_the_lexicons_never_na() -> None:
    assert allowed(evaluate(_booleans(["haan", "Haa"], ["nahi"]), context()))
    assert allowed(evaluate(_booleans(["हाँ"], ["नहीं"]), context()))
    # Malformed steps keep `check_recipe`'s code today; only `na` and unknown rules spellings are new refusals.
    assert codes(evaluate(_booleans(["haan"], []), context())) == {"RECIPE_STEP_INVALID"}
    assert codes(evaluate(_booleans(["haan"], ["haan"]), context())) == {"RECIPE_STEP_INVALID"}
    assert codes(evaluate(_booleans(["haan"], ["NA"]), context())) == {"POLICY_BOOLEAN_SPELLING"}
    assert codes(evaluate(_booleans(["haan", "sahi"], ["nahi"]), context())) == {"POLICY_BOOLEAN_SPELLING"}


def test_new_patterns_with_permission_an_ai_spelling_is_allowed_never_ticked_and_measured() -> None:
    """Requirement 11: `haa`/`nahi`-like spellings no rule knows may be proposed by the AI, from the fixed step
    kinds, grounded in a look, tried on the first rows, held to O10, never ticked."""
    chat = rules_of("data.recipe_step")
    facts = step(RecipeStepKind.MAP_BOOLEAN, chat, params={"true_values": ["ho"], "false_values": ["nako"]})
    proposal = ai_step("data.recipe_step", facts)
    assert allowed(evaluate(proposal, context()))
    assert codes(evaluate(dataclasses.replace(proposal, ticked=True), context())) == {"POLICY_NEVER_TICKED"}
    assert codes(evaluate(dataclasses.replace(proposal, looked_at=False), context())) == {
        "AGENT_STEP_UNGROUNDED"
    }
    assert codes(evaluate(dataclasses.replace(proposal, effect=None), context())) == {"POLICY_STEP_NOT_TRIED"}
    lost = StepEffect(changed=100, failed=70, out_of=100, rows_tried=100)
    assert codes(evaluate(dataclasses.replace(proposal, effect=lost), context())) == {
        "AI_STEP_CHANGES_TOO_MANY"
    }
    with_na = step(RecipeStepKind.MAP_BOOLEAN, chat, params={"true_values": ["ho"], "false_values": ["na"]})
    assert codes(evaluate(ai_step("data.recipe_step", with_na), context())) == {"POLICY_BOOLEAN_SPELLING"}


def test_o10_change_share_refuses_above_60_and_warns_above_30() -> None:
    merge = step(RecipeStepKind.MAP_VALUES, rules_of("data.recipe_step"))
    for changed, verdict in ((70, "refuse"), (40, "warn"), (20, "ok")):
        effect = StepEffect(changed=changed, failed=0, out_of=100, rows_tried=100)
        decisions = evaluate(ai_step("data.recipe_step", merge, effect=effect), context())
        share = next(d for d in decisions if d.rule == pol.CHANGE_SHARE)
        if verdict == "refuse":
            assert codes(decisions) == {"AI_STEP_CHANGES_TOO_MANY"}
        else:
            assert allowed(decisions)
            assert share.detail.get("warning") == ("AI_STEP_CHANGES_MANY" if verdict == "warn" else None)
    rules_made = dataclasses.replace(
        base("data.synonym_clusters", rules_of("data.synonym_clusters")),
        effect=StepEffect(changed=90, failed=0, out_of=100, rows_tried=100),
    )
    assert allowed(evaluate(rules_made, context())), "O10 holds the AI's steps, not the rules'"
    retyped = ai_step("data.recipe_step", step(RecipeStepKind.PARSE_NUMBER, rules_of("data.recipe_step")))
    every_value = dataclasses.replace(
        retyped, effect=StepEffect(changed=100, failed=2, out_of=100, rows_tried=100)
    )
    assert allowed(evaluate(every_value, context())), "re-typing every value is not a 100% change"


def test_m238_keep_latest_is_asked_never_ticked_and_guarded_at_approval() -> None:
    rules = rules_of("data.keep_latest_per_id")
    own = base("data.keep_latest_per_id", rules)
    assert allowed(evaluate(own, context()))
    assert codes(evaluate(dataclasses.replace(own, asked=False), context())) == {"RESHAPE_GUARD_MISSING"}
    sure = dataclasses.replace(own, ticked=True, confidence="sure")
    assert codes(evaluate(sure, context())) == {"POLICY_NEVER_TICKED"}, "never ticked, even when sure"
    receipt = StepReceipt(
        order=1, kind=RecipeStepKind.KEEP_LATEST_PER_ID, column=PK, rows=100, changed=0, failed=0
    )
    from engine.agent.contracts import RecipeStep

    as_step = RecipeStep(
        order=1, kind=RecipeStepKind.KEEP_LATEST_PER_ID, column=PK, params=dict(own.steps[0].params)
    )
    good = ReshapeItem(
        step=as_step, ticked_when_offered=False, question_answered=True, leak_check="full", receipt=receipt
    )
    approval = ApprovalFacts(
        approver=ANALYST, approved_sentence=own.sentence, current_sentence=own.sentence, reshape=(good,)
    )
    assert allowed(evaluate(own, context(approval=approval)))
    unanswered = dataclasses.replace(approval, reshape=(dataclasses.replace(good, question_answered=False),))
    refused = evaluate(own, context(approval=unanswered))
    assert codes(refused) == {"RESHAPE_GUARD_MISSING"}
    narrow = dataclasses.replace(approval, reshape=(dataclasses.replace(good, leak_check="narrow"),))
    assert codes(evaluate(own, context(approval=narrow))) == {"RESHAPE_GUARD_MISSING"}
    assert codes(evaluate(own, context(approval=dataclasses.replace(approval, reshape=())))) == {
        "RESHAPE_GUARD_MISSING"
    }


def test_approve_class_items_need_their_exact_sentence_their_role_and_another_approver() -> None:
    joined = base("data.join_files", rules_of("data.join_files"))
    small = dataclasses.replace(joined, rows_out=99)  # 1% dropped: not APPROVE-class
    large = dataclasses.replace(joined, rows_out=97)  # 3% dropped
    assert allowed(evaluate(small, context()))
    assert (
        next(d for d in evaluate(small, context()) if d.rule == pol.APPROVE_CLASS).detail["approve_class"]
        is False
    )
    marked = next(d for d in evaluate(large, context()) if d.rule == pol.APPROVE_CLASS)
    assert marked.allowed and marked.detail["approve_class"] is True and marked.detail["role"] == "analyst"
    assert codes(evaluate(dataclasses.replace(large, sentence=None), context())) == {"POLICY_PAYLOAD_INVALID"}
    unknown = dataclasses.replace(joined, rows_in=None, rows_out=None, sentence=None)
    assert codes(evaluate(unknown, context())) == {"POLICY_PAYLOAD_INVALID"}, "unknown drops count as large"

    def at(approver: Any, approved: str | None = large.sentence, required: bool = False) -> set[str]:
        facts = ApprovalFacts(
            approver=approver,
            approved_sentence=approved,
            current_sentence=large.sentence,
            approval_required=required,
        )
        return codes(evaluate(large, context(approval=facts)))

    assert at(ANALYST) == set()
    assert at(VIEWER) == {"TOOL_ROLE_REQUIRED"}
    assert at(ANALYST, approved="Join the files.") == {"APPROVAL_SENTENCE_CHANGED"}
    assert at(ANALYST, approved=None) == {"APPROVAL_SENTENCE_CHANGED"}
    assert at(ANALYST, required=True) == {"POLICY_ROLE_SEPARATION"}, "u-asha proposed it"
    assert at(APPROVER, required=True) == {"TOOL_ROLE_REQUIRED"}, "an Approver without the Analyst role"
    both = APPROVER.model_copy(update={"roles": frozenset({Role.ANALYST, Role.APPROVER})})
    assert at(both, required=True) == set()
    split = base("data.split_file", rules_of("data.split_file"))
    assert (
        next(d for d in evaluate(split, context()) if d.rule == pol.APPROVE_CLASS).detail["approve_class"]
        is True
    )


def test_the_policy_reads_only_what_the_server_computed() -> None:
    """A proposal cannot widen the facts: the ID, the outcome, personal columns and levels are the context's."""
    rules = rules_of("data.recipe_step")
    subject = ai_step("data.recipe_step", step(RecipeStepKind.MAP_VALUES, rules, column=PII))
    assert codes(evaluate(subject, context())) == {"AGENT_STEP_NOT_ALLOWED"}
    assert allowed(evaluate(subject, context(personal_columns=frozenset()))), "facts come from the context"


def test_the_shipped_policy_round_trips(config_root: Path) -> None:
    again = DataPolicyConfig.model_validate(load_policy(config_root).data)
    assert again == shipped()
    assert {
        "POLICY_NEVER_TICKED",
        "POLICY_STEP_NOT_TRIED",
        "POLICY_BOOLEAN_SPELLING",
        "APPROVAL_SENTENCE_CHANGED",
    } == pol.DATA_POLICY_CODES


def test_the_policy_is_reread_when_the_file_changes(tmp_path: Path, config_root: Path) -> None:
    copy = tmp_path / "configs"
    shutil.copytree(config_root / "agentic", copy / "agentic")
    assert load_data_policy(copy).approve_drop_share_pct == 2.0
    path = copy / "agentic" / "policy.yaml"
    path.write_text(
        path.read_text(encoding="utf-8").replace(
            "approve_drop_share_pct: 2.0", "approve_drop_share_pct: 5.25"
        ),
        encoding="utf-8",
    )
    assert load_data_policy(copy).approve_drop_share_pct == 5.25
