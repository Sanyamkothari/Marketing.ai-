"""The plain-language catalogue explains every code and every setting, in plain words (Plan E M60, M63)."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from engine.config import Metric, advanced_settings_schema, load_all_use_cases
from engine.contracts import CHECK_CODES
from engine.pilot.help import code_help, known_codes, load_help, setting_help, setting_key
from engine.pilot.plain import jargon_in, terms_used
from engine.pilot.preflight import PREFLIGHT_CODES
from engine.uplift.contracts import UPLIFT_VALIDATION_CODES


@pytest.fixture(scope="module")
def catalogue(config_root: Path):
    return load_help(config_root)


def test_every_check_code_the_engine_can_raise_has_an_entry(catalogue) -> None:
    missing = sorted(known_codes() - set(catalogue.codes))
    assert missing == [], f"no plain-language entry for {missing}"


def test_the_known_codes_are_every_phase_1_to_3b_check_and_the_checkers_own() -> None:
    known = known_codes()
    assert known >= CHECK_CODES
    assert known >= UPLIFT_VALIDATION_CODES
    assert known >= PREFLIGHT_CODES
    assert {"DRIFT_WATCH", "DRIFT_DRIFTED", "THRESHOLD_FALLBACK"} <= known


def test_the_catalogue_explains_no_code_the_engine_cannot_raise(catalogue) -> None:
    assert sorted(set(catalogue.codes) - known_codes()) == []


def test_every_advanced_setting_of_every_use_case_has_a_meaning(catalogue) -> None:
    missing: set[str] = set()
    for config in load_all_use_cases().values():
        try:
            schema = advanced_settings_schema(config)
        except Exception:  # a use case with no advanced settings screen (the assistant) has none
            continue
        for stage in schema.stages:
            for field in stage.fields:
                if setting_help(field.path) is None:
                    missing.add(setting_key(field.path))
    assert sorted(missing) == []


def test_a_list_index_is_one_entry_for_every_band() -> None:
    assert setting_key("actions.bands[0].min_score") == setting_key("actions.bands[3].min_score")
    assert setting_help("actions.bands[1].min_score") is not None


def test_every_metric_a_model_can_be_chosen_on_has_a_plain_name(catalogue) -> None:
    assert sorted(m.value for m in Metric if m.value not in catalogue.metrics) == []


@pytest.mark.parametrize("section", ["codes", "settings"])
def test_no_text_uses_model_jargon(catalogue, section: str) -> None:
    offenders = []
    for key, entry in getattr(catalogue, section).items():
        for field, text in entry.model_dump().items():
            if jargon_in(text):
                offenders.append((key, field, jargon_in(text)))
    assert offenders == []


def test_the_glossary_and_metric_names_are_plain_too(catalogue) -> None:
    assert [t for t, text in catalogue.terms.items() if jargon_in(text)] == []
    assert [m for m, entry in catalogue.metrics.items() if jargon_in(entry.name)] == []


def test_a_title_is_short_and_a_fix_says_what_to_do(catalogue) -> None:
    for code, entry in catalogue.codes.items():
        assert len(entry.title) <= 70, code
        assert entry.fix[0].isupper(), code
        assert not re.search(r"\b(TODO|TBD)\b", entry.meaning + entry.fix), code


def test_an_unknown_code_has_no_entry_rather_than_an_invented_one() -> None:
    assert code_help("NOT_A_CODE") is None


def test_the_jargon_check_finds_a_stem_at_a_word_start_only() -> None:
    assert jargon_in("The AUC is 0.8") == ("auc",)
    assert jargon_in("Features were built") == ("feature",)
    assert jargon_in("Check the mapping screen and the features screen") == ()
    assert jargon_in("a caucus") == ()


def test_terms_used_finds_plurals() -> None:
    assert terms_used(["the top two deciles"], {"decile": "one tenth"}) == ("decile",)
