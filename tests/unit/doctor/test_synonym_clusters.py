"""`synonym_clusters` (Plan O M238, DEC-1808 (d)): rules group spellings; merges are only pending proposals;
an AI's merge is verified and held to decision O10."""

from __future__ import annotations

import pandas as pd
import pytest

from engine.agent.config import AgentLevel
from engine.agent.contracts import ProposalSource, RecipeStepKind
from engine.agent.recipe import run_recipe
from engine.doctor.synonyms import (
    AiMergeRefusedError,
    check_ai_merge,
    find_synonym_clusters,
    load_value_synonyms,
    spelling_key,
)


def plans() -> pd.Series:
    return pd.Series(["Prepaid"] * 5 + ["Pre-paid", "PP", "Postpaid", "Postpaid", None], name="plan")


def test_pre_paid_pp_and_prepaid_merge_only_as_a_pending_proposal() -> None:
    series = plans()
    before = series.copy()
    report = find_synonym_clusters(series, column="plan")
    assert len(report.clusters) == 1
    cluster = report.clusters[0]
    assert cluster.canonical == "Prepaid"
    assert {m.spelling for m in cluster.members} == {"Prepaid", "Pre-paid", "PP"}
    assert set(cluster.rules) == {"same_letters", "initials"}
    step = report.step
    assert step is not None
    assert (step.kind, step.state, step.ticked, step.confidence, step.source) == (
        RecipeStepKind.MAP_VALUES, "pending", False, "check", ProposalSource.RULES,
    )  # fmt: skip
    assert step.step_params == {"mapping": {"PP": "Prepaid", "Pre-paid": "Prepaid"}}
    assert series.equals(before)  # nothing was merged by looking
    frame = pd.DataFrame({"id": range(len(series)), "plan": series})
    run = run_recipe(
        frame, [step.as_step(1)], upload_id="u", primary_key="id", target=None,
        levels=(AgentLevel.CLEAN,), max_failure_pct=5.0,
    )  # fmt: skip
    assert run.frame["plan"].value_counts().to_dict() == {"Prepaid": 7, "Postpaid": 2}
    assert run.receipt.steps[0].changed == step.changed == 2


def test_initials_that_fit_two_values_are_asked_never_merged() -> None:
    series = pd.Series(["Pre-paid", "Prepaid", "Post-paid", "PP", "Postpaid"])
    report = find_synonym_clusters(series, column="plan")
    assert report.ambiguous == ("PP",)
    assert all("PP" not in {m.spelling for m in c.members} for c in report.clusters)
    assert report.step is not None and "PP" not in report.step.step_params["mapping"]


def test_the_lexicon_merges_old_and_new_city_names_and_hinglish_yes() -> None:
    cities = find_synonym_clusters(pd.Series(["Mumbai", "Mumbai", "Bombay", "Pune"]), column="city")
    assert cities.step is not None and cities.step.step_params == {"mapping": {"Bombay": "Mumbai"}}
    assert cities.clusters[0].rules == ("lexicon",)
    yes = find_synonym_clusters(pd.Series(["Yes", "haan", "No", "nahi", "Yes"]), column="agreed")
    assert yes.step is not None and yes.step.step_params == {"mapping": {"haan": "Yes", "nahi": "No"}}


def test_na_is_never_read_as_no() -> None:
    report = find_synonym_clusters(pd.Series(["No", "NA", "Yes", "No"]), column="agreed")
    assert report.step is None


def test_a_one_letter_slip_merges_a_rare_spelling_into_a_common_one() -> None:
    series = pd.Series(["Mumbai"] * 10 + ["Mumabi"] + ["Nagpur"] * 3)
    report = find_synonym_clusters(series, column="city", lexicon=())
    assert report.step is not None and report.step.step_params == {"mapping": {"Mumabi": "Mumbai"}}
    assert report.clusters[0].rules == ("one_letter_slip",)
    # Two common spellings one letter apart are different values; so are spellings that differ in a digit.
    assert (
        find_synonym_clusters(pd.Series(["Silver"] * 5 + ["Sliver"] * 5), column="x", lexicon=()).step is None
    )
    assert (
        find_synonym_clusters(pd.Series(["Plan 10"] * 9 + ["Plan 11"]), column="x", lexicon=()).step is None
    )


def test_free_text_numbers_and_dates_are_left_alone() -> None:
    many = find_synonym_clusters(pd.Series([f"note {i}" for i in range(600)]), column="notes")
    assert many.step is None and many.note is not None
    assert find_synonym_clusters(pd.Series([1, 2, 3]), column="n").note == "not text"


def test_the_grouping_is_deterministic() -> None:
    assert find_synonym_clusters(plans(), column="plan") == find_synonym_clusters(plans(), column="plan")


def test_spelling_keys_keep_indian_vowel_signs() -> None:
    assert spelling_key("Pre-paid") == spelling_key("PRE PAID") == "prepaid"
    assert spelling_key("हाँ") != spelling_key("ह")
    assert all(len(group) >= 2 for group in load_value_synonyms())


# ---------------------------------------------------------------------------
# The AI's merge: verified by the rules, decision O10
# ---------------------------------------------------------------------------
def test_an_ai_merge_is_checked_and_labelled_as_the_ais() -> None:
    series = pd.Series(["Gold"] * 8 + ["Gld", "Golden"])
    step = check_ai_merge(series, column="tier", groups=[["Gold", "Gld"]], canonicals=["Gold"])
    assert (step.source, step.state, step.ticked, step.guard) == (ProposalSource.AI, "pending", False, "ok")
    assert step.step_params == {"mapping": {"Gld": "Gold"}}


@pytest.mark.parametrize(
    ("groups", "keep"),
    [([["Gold", "Platinum"]], ["Gold"]), ([["Gold"]], ["Gold"]), ([["Gold", "Gld"]], ["Silver"]), ([], [])],
)
def test_an_ai_merge_naming_spellings_the_column_lacks_is_refused(
    groups: list[list[str]], keep: list[str]
) -> None:
    series = pd.Series(["Gold"] * 8 + ["Gld"])
    with pytest.raises(AiMergeRefusedError) as refused:
        check_ai_merge(series, column="tier", groups=groups, canonicals=keep)
    assert refused.value.code == "SYNONYM_MERGE_INVALID"


def test_an_ai_merge_changing_over_30_percent_warns_and_over_60_is_refused() -> None:
    warn = pd.Series(["A1"] * 6 + ["B1"] * 4)
    step = check_ai_merge(warn, column="c", groups=[["A1", "B1"]], canonicals=["A1"])
    assert (step.guard, step.guard_code, step.share_pct) == ("warn", "AI_STEP_CHANGES_MANY", 40.0)
    refuse = pd.Series(["A1"] * 3 + ["B1"] * 7)
    with pytest.raises(AiMergeRefusedError) as refused:
        check_ai_merge(refuse, column="c", groups=[["A1", "B1"]], canonicals=["A1"])
    assert refused.value.code == "AI_STEP_CHANGES_TOO_MANY"
