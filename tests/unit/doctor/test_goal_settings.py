"""Goals as data (AI-first P3): the window and the audience of a goal's build are settings read from
`configs/outcomes.yaml`, asked for when the goal says so, and decided as `goal_setting` items.

The settings schema and its loader, `open_setting_questions`, the window and audience as parameters of the build, the
window-neutral outcome column, and the build that declares its outcome (so the prescription's target follows it).
"""

from __future__ import annotations

import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from engine.agent.contracts import ProposalState, RecipeStepKind
from engine.agent.sql_step import MAX_COMPILED_SQL_CHARS, run_sql
from engine.config import config_root as root_of
from engine.config import load_use_case
from engine.doctor.buildable import BuildableOutcome, buildable_outcome, compile_build
from engine.doctor.case import CaseOption, CaseQuestion
from engine.doctor.decisions import decisions, decisions_from
from engine.doctor.goal_settings import GOAL_SETTING, GoalSettings, load_goal_settings, open_setting_questions
from engine.doctor.outcomes import OutcomeTemplate, load_outcome_catalogue, sentence_for
from engine.doctor.prescription import prescription_from_case
from engine.doctor.protocol import settle
from engine.storage import LocalStorage
from tests.unit.doctor import visit_support as vs
from tests.unit.doctor.prescription_support import step
from tests.unit.doctor.test_buildable import GOAL, loaded, log
from tests.unit.doctor.test_decisions import as_of_question, case_of, item

NOW = datetime(2026, 10, 12, 9, 0, tzinfo=UTC)


def facts(config_root: Path, **window: Any) -> BuildableOutcome:
    config = load_use_case(GOAL, config_root)
    found = buildable_outcome([loaded(log(1_500), config_root)], config, config_root, **window)
    assert found is not None and found.snapshot is not None
    return found


def with_enough(found: BuildableOutcome, *, active: bool, quiet: bool) -> BuildableOutcome:
    a, q = found.audiences
    return found.model_copy(
        update={
            "audiences": (a.model_copy(update={"enough": active}), q.model_copy(update={"enough": quiet}))
        }
    )


def settings_with(config_root: Path, **audience: Any) -> GoalSettings:
    """retail-win-back's settings with the audience block changed (what the configuration says, not the code)."""
    real = load_goal_settings(GOAL, config_root)
    assert real.template is not None and real.template.audience is not None
    block = real.template.audience.model_dump(mode="json")
    block.update(audience)
    raw = real.template.model_dump(mode="json")
    raw["settings"]["audience"] = block
    return GoalSettings(GOAL, OutcomeTemplate.model_validate(raw), real.entity, real.minimums)


def answered_setting(
    setting: str, value: Any, *, state: ProposalState = ProposalState.ACCEPTED, at: int = 0
) -> Any:
    made = item(
        f"gs-{setting}-{value}-{at}",
        "goal_setting",
        {"goal_id": GOAL, "setting": setting, "value": value},
        state=state,
        goal_id=GOAL,
    )
    return made.model_copy(update={"decided_at": datetime(2026, 10, 12, 9, at, tzinfo=UTC)})


def old_audience_answer(answer: str) -> CaseQuestion:
    return CaseQuestion(
        question_id="q.build_audience.x",
        code="BUILD_AUDIENCE",
        text="Which shoppers?",
        options=(
            CaseOption(option_id="active", label="All"),
            CaseOption(option_id="quiet", label="Only quiet"),
            CaseOption(option_id="none", label="Neither"),
        ),
        answer=answer,
        answered_by="person",
        answered_at=NOW,
        goal_id=GOAL,
    )


# -- the schema and its loader ----------------------------------------------------------------------------------
def test_every_template_in_outcomes_yaml_loads(config_root: Path) -> None:
    catalogue = load_outcome_catalogue(root_of(config_root))
    assert catalogue.templates, "the outcomes file lists templates"
    for goal_id, template in catalogue.templates.items():
        settings = load_goal_settings(goal_id, config_root)
        assert settings.template is template and settings.names[0] == "window_days"
        assert settings.default("window_days") in settings.allowed("window_days")
        assert settings.minimums == (
            load_use_case(goal_id, config_root).validation.min_rows,
            load_use_case(goal_id, config_root).validation.min_positive,
        )
        assert sentence_for(template, entity="customer", event="order", days=template.default_days)
        column = template.build.outcome_column
        assert column and not re.search(r"_\d+[dh]$", column), f"{goal_id}: {column} names no window"
        if template.audience is not None:
            assert {o.id for o in template.audience.options} == {"active", "quiet"}


def test_an_older_template_is_read_into_settings() -> None:
    template = OutcomeTemplate.model_validate(
        {
            "question": "q",
            "sentence": "{An_entity} has come back when there is {an_event} within {days} days.",
            "label_type": "event_presence",
            "event_kinds": ["orders"],
            "days": [30, 90],
            "default_days": 90,
            "audience": {"quiet_days": 60, "label": "{entities} quiet for {quiet_days} days"},
        }
    )
    assert (template.days, template.default_days) == ((30, 90), 90)
    assert template.settings.window_days.ask is False
    assert template.audience is not None and template.audience.ask is True
    quiet = template.audience.option("quiet")
    assert quiet is not None and quiet.quiet_days == 60 and template.audience.recommended is None
    with pytest.raises(ValueError, match="one or the other"):
        OutcomeTemplate.model_validate(
            {
                "question": "q",
                "sentence": "{An_entity} left",
                "label_type": "event_absence",
                "event_kinds": ["orders"],
                "days": [30],
                "default_days": 30,
                "settings": {"window_days": {"allowed": [30], "default": 30}},
            }
        )


def test_a_recommended_audience_needs_its_reason(config_root: Path) -> None:
    with pytest.raises(ValueError, match="because"):
        settings_with(config_root, because="")


# -- the window and the audience as parameters of the build -----------------------------------------------------
def test_sixty_days_changes_both_the_sql_interval_and_the_counts(config_root: Path) -> None:
    config = load_use_case(GOAL, config_root)
    frame = log(1_500)
    ninety = facts(config_root)
    sixty = facts(config_root, window_days=60)
    assert (ninety.window_days, sixty.window_days) == (90, 60)
    assert sixty.snapshot is not None and ninety.snapshot is not None and sixty.snapshot > ninety.snapshot
    assert (sixty.active_before, sixty.yes) != (ninety.active_before, ninety.yes)

    sql90, _ = compile_build(ninety, config, "t_orders")
    sql60, description = compile_build(sixty, config, "t_orders")
    after = "<= CAST($as_of AS DATE) + INTERVAL {} DAY"  # the outcome's window (the features have their own)
    assert after.format(60) in sql60 and after.format(90) not in sql60
    assert after.format(90) in sql90 and after.format(60) not in sql90
    assert "60 days after the snapshot" in description
    built = run_sql(
        sql60, {"orders": frame}, output_name="t", as_of=sixty.snapshot, max_chars=MAX_COMPILED_SQL_CHARS
    ).frame
    assert len(built) == sixty.active_before and int(built["reactivated"].sum()) == sixty.yes


def test_the_built_column_is_named_the_same_for_every_window(config_root: Path) -> None:
    config = load_use_case(GOAL, config_root)
    names = set()
    for days in (30, 60, 90):
        found = facts(config_root, window_days=days)
        assert found.outcome_column == "reactivated"
        sql, _ = compile_build(found, config, "t_orders")
        names.add(re.search(r' AS "?(\w+)"? FROM "f"', sql).group(1))  # type: ignore[union-attr]
    assert names == {"reactivated"}, "the window is in the query and the setting, never in the column"
    assert (
        config.target.column == "reactivated_90d"
    ), "the configured name is still the one looked for in a file"


def test_a_value_the_goal_does_not_allow_is_refused(config_root: Path) -> None:
    config = load_use_case(GOAL, config_root)
    frame = loaded(log(1_500), config_root)
    with pytest.raises(ValueError, match="45 days"):
        buildable_outcome([frame], config, config_root, window_days=45)
    with pytest.raises(ValueError, match="audience"):
        buildable_outcome([frame], config, config_root, audience_id="everyone")
    plain = load_use_case("targeted-advertisement", config_root)
    assert plain.target.column  # a goal that names no audience offers none to pick
    assert load_goal_settings("targeted-advertisement", config_root).names == ("window_days",)


def test_the_floors_are_judged_on_the_audience_picked(config_root: Path) -> None:
    config = load_use_case(GOAL, config_root)
    frame = loaded(log(1_500), config_root)
    both = buildable_outcome([frame], config, config_root)
    quiet = buildable_outcome([frame], config, config_root, audience_id="quiet")
    active = buildable_outcome([frame], config, config_root, audience_id="active")
    assert both is not None and quiet is not None and active is not None
    a, q = both.audiences
    assert both.enough == (a.enough or q.enough)
    assert quiet.enough == q.enough and active.enough == a.enough
    assert [x.model_dump() for x in quiet.audiences] == [x.model_dump() for x in both.audiences]


# -- the recommendation and "I don't know" ----------------------------------------------------------------------
def test_the_recommended_audience_comes_from_config_and_is_dropped_when_there_is_not_enough(
    config_root: Path,
) -> None:
    settings = load_goal_settings(GOAL, config_root)
    found = facts(config_root)
    assert settings.recommended("audience", with_enough(found, active=True, quiet=True)) == "quiet"
    assert settings.recommended("audience", with_enough(found, active=True, quiet=False)) is None
    assert settings.recommended("audience", None) is None
    # it is what the configuration says, not what the goal's words suggest
    other = settings_with(config_root, recommended="active", because="The whole base is wanted.")
    assert other.recommended("audience", with_enough(found, active=True, quiet=True)) == "active"
    unrecommended = settings_with(config_root, recommended=None, because="")
    assert unrecommended.recommended("audience", with_enough(found, active=True, quiet=True)) is None
    assert settings.recommended("window_days", None) == 90


def test_dont_know_falls_back_to_the_widest_reading_that_is_enough(config_root: Path) -> None:
    settings = load_goal_settings(GOAL, config_root)
    found = facts(config_root)
    assert settings.dont_know("audience", with_enough(found, active=True, quiet=True)) == "quiet"
    assert settings.dont_know("audience", with_enough(found, active=True, quiet=False)) == "active"
    assert settings.dont_know("audience", with_enough(found, active=False, quiet=True)) == "quiet"
    assert settings.dont_know("audience", with_enough(found, active=False, quiet=False)) is None
    unset = settings_with(config_root, dont_know=None)
    both = with_enough(found, active=True, quiet=True)
    widest = max(both.audiences, key=lambda a: a.customers).audience_id
    assert unset.dont_know("audience", both) == widest
    assert settings.dont_know("window_days", None) == 90


# -- the questions ----------------------------------------------------------------------------------------------
def test_the_audience_is_asked_with_counts_the_recommendation_and_a_safe_choice(config_root: Path) -> None:
    settings = load_goal_settings(GOAL, config_root)
    found = with_enough(facts(config_root), active=True, quiet=True)
    (ask,) = open_setting_questions(settings, case_of(active=GOAL), found)
    assert ask.code == GOAL_SETTING and ask.blocking and ask.goal_id == GOAL
    assert dict(ask.subject) == {"goal": GOAL, "setting": "audience"}
    active, quiet = found.audiences
    labels = {o.effect.option_id: o.label for o in ask.options}
    assert labels["active"].startswith("All ") and f"{active.customers:,} shoppers" in labels["active"]
    assert labels["quiet"].startswith("Only ") and f"{quiet.customers:,} shoppers" in labels["quiet"]
    assert "gone quiet" in labels["quiet"] and f"{quiet.yes:,} of them a yes" in labels["quiet"]
    assert ask.recommended == "quiet" and "stopped buying" in ask.why, "the configured reason"
    safe = next(o for o in ask.options if o.safe)
    assert safe.effect.option_id == "dont_know" and safe.effect.payload["value"] == "quiet"
    assert safe.effect.kind == "goal_setting"

    settled = settle(ask, now=NOW)
    assert settled.how == "ask" and settled.question is not None
    assert settled.question.recommended == "quiet" and settled.question.blocking
    assert [o.safe for o in settled.question.options].count(True) == 1
    effect = next(e for e in settled.effects if e.option_id == "active")
    assert effect.payload == {"goal_id": GOAL, "setting": "audience", "value": "active"}


def test_a_too_small_reading_says_so_and_is_not_recommended(config_root: Path) -> None:
    settings = load_goal_settings(GOAL, config_root)
    found = with_enough(facts(config_root), active=True, quiet=False)
    (ask,) = open_setting_questions(settings, case_of(active=GOAL), found)
    labels = {o.effect.option_id: o.label for o in ask.options}
    assert "too few to learn from" in labels["quiet"] and "too few" not in labels["active"]
    assert ask.recommended is None and "stopped buying" not in ask.why
    assert next(o for o in ask.options if o.safe).effect.payload["value"] == "active"
    nothing = with_enough(found, active=False, quiet=False)
    (hopeless,) = open_setting_questions(settings, case_of(active=GOAL), nothing)
    assert (
        next(o for o in hopeless.options if o.safe).effect.kind is None
    ), "no reading is enough: it decides nothing"


def test_a_window_that_asks_becomes_a_question_and_one_that_does_not_does_not(config_root: Path) -> None:
    settings = load_goal_settings(GOAL, config_root)
    assert settings.asks("window_days") is False and settings.asks("audience") is True
    assert (
        open_setting_questions(settings, case_of(active=GOAL), None) == []
    ), "no counts: no audience question"
    real = settings.template
    assert real is not None
    raw = real.model_dump(mode="json")
    raw["settings"]["window_days"]["ask"] = True
    asking = GoalSettings(GOAL, OutcomeTemplate.model_validate(raw), settings.entity, settings.minimums)
    (ask,) = open_setting_questions(asking, case_of(active=GOAL), None)
    assert dict(ask.subject)["setting"] == "window_days" and ask.recommended == "90"
    assert [o.effect.option_id for o in ask.options] == ["30", "60", "90", "dont_know"]
    assert ask.options[1].effect.payload["value"] == 60, "the days, as a number"
    assert asking.value("window_days", decisions(case_of(active=GOAL))) is None, "asked, not yet answered"


def test_a_setting_that_has_a_value_is_not_asked_again(config_root: Path) -> None:
    settings = load_goal_settings(GOAL, config_root)
    found = with_enough(facts(config_root), active=True, quiet=True)
    mine = case_of([answered_setting("audience", "active")], active=GOAL)
    assert open_setting_questions(settings, mine, found) == []
    older = case_of(questions=[old_audience_answer("quiet")], active=GOAL)
    assert open_setting_questions(settings, older, found) == []
    pending = case_of([answered_setting("audience", "active", state=ProposalState.PENDING)], active=GOAL)
    assert (
        len(open_setting_questions(settings, pending, found)) == 1
    ), "a proposal nobody ticked decides nothing"
    neither = case_of(questions=[old_audience_answer("none")], active=GOAL)
    assert len(open_setting_questions(settings, neither, found)) == 1, "'Neither' chose no audience"


# -- reading the settings ---------------------------------------------------------------------------------------
def test_an_older_build_audience_answer_is_read_as_the_audience_setting() -> None:
    found = decisions_from([], [old_audience_answer("quiet")])
    audience = found.setting(GOAL, "audience")
    assert audience is not None and audience.value == "quiet" and audience.by == "person"
    assert audience.source.startswith("question:q.build_audience")
    assert found.setting("another-goal", "audience") is None
    assert decisions_from([], [old_audience_answer("none")]).setting(GOAL, "audience") is None
    assert decisions_from([], [as_of_question("c0")]).of("setting") == ()
    # the person's newer choice as a setting wins over the older answer
    now = decisions_from([answered_setting("audience", "active")], [old_audience_answer("quiet")])
    chosen = now.setting(GOAL, "audience")
    assert chosen is not None and chosen.value == "active" and chosen.source.startswith("item:")


def test_the_newest_accepted_setting_wins_and_a_proposal_decides_nothing() -> None:
    found = decisions_from(
        [
            answered_setting("window_days", 90, at=1),
            answered_setting("window_days", 60, at=5),
            answered_setting("window_days", 30, state=ProposalState.PENDING, at=9),
        ]
    )
    window = found.setting(GOAL, "window_days")
    assert window is not None and window.value == 60 and window.name == "window_days"
    assert found.setting("another-goal", "window_days") is None


def test_an_older_window_setting_is_the_window_setting() -> None:
    older = item(
        "w1",
        "setting",
        {"path": "agent.outcome_window_days", "value": 45},
        goal_id=GOAL,
    )
    window = decisions_from([older]).setting(GOAL, "window_days")
    assert window is not None and window.value == 45


def test_the_value_is_what_was_decided_else_the_default_and_the_digest_follows_it(config_root: Path) -> None:
    settings = load_goal_settings(GOAL, config_root)
    start = decisions(case_of(active=GOAL))
    assert settings.value("window_days", start) == 90, "no decision: the default, proposed with its reason"
    assert settings.value("audience", start) is None, "it asks: nobody has said"
    sixty = decisions(case_of([answered_setting("window_days", 60)], active=GOAL))
    assert settings.value("window_days", sixty) == 60
    stale = decisions(case_of([answered_setting("window_days", 45)], active=GOAL))
    assert settings.value("window_days", stale) == 90, "a value the goal no longer allows is not used"
    assert len({settings.digest(start), settings.digest(sixty)}) == 2, "a changed window is a changed build"
    assert settings.digest(sixty) == settings.digest(
        decisions(case_of([answered_setting("window_days", 60)], active=GOAL))
    )


# -- the build declares its outcome -----------------------------------------------------------------------------
def build_item(column: str = "reactivated", **extra: Any) -> Any:
    return item(
        "build1",
        "recipe_step",
        {"buildable": {"upload_id": vs.FIRST}, "outcome_column": column, **extra},
        upload_id=vs.FIRST,
        goal_id=vs.GOAL_A,
    )


def test_an_accepted_build_declares_the_goals_outcome() -> None:
    found = decisions_from([build_item()])
    assert found.pair("outcome", vs.GOAL_A) == (vs.FIRST, "reactivated")
    assert found.outcome(vs.GOAL_A).source == "item:build1"  # type: ignore[union-attr]
    proposed = item(
        "build2", "recipe_step", {"buildable": {}, "outcome_column": "x"}, state=ProposalState.PENDING
    )
    assert (
        decisions_from([proposed]).of("outcome") == ()
    ), "the person's yes covers it: a proposal declares nothing"
    plain = item("s1", "recipe_step", {"step": {"kind": "parse_number"}}, goal_id=vs.GOAL_A)
    assert decisions_from([plain]).of("outcome") == (), "a step that is not a build declares nothing"


def test_the_prescriptions_target_is_the_builds_declared_outcome(tmp_path: Path) -> None:
    """The decided outcome role is the recipe's model target (`prescription.py`), so the window-neutral column the
    build makes is the column the model is trained on: no rename is needed between the build and the run."""
    storage = LocalStorage(tmp_path)
    w = vs.world()
    deps = vs.deps_for(storage, w)
    case = vs.open_case(storage, w)
    for _name, _op, act in vs.SCRIPT[:3]:  # resume, the key, the goal
        case = act(deps, case)
    fix = item(
        "fix1",
        "recipe_step",
        {"step": step(RecipeStepKind.PARSE_NUMBER, "monthly_spend").model_dump(mode="json")},
        upload_id=vs.FIRST,
        goal_id=vs.GOAL_A,
    )
    kept = [i for i in case.items if i.kind != "outcome_column"]
    case = case.model_copy(update={"items": [*kept, build_item(), fix]})
    prescription = prescription_from_case(case, storage=storage, files=tuple(w.files.values()))
    section = prescription.section()
    assert section is not None and section.roles["outcome"] == "reactivated"
    assert [recipe.target for recipe in prescription.recipes()] == ["reactivated"]
