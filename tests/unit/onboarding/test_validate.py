"""The onboarding checks.

Every case is built as data a client could actually upload - a customer master with a repeated id, a
bills table whose keys carry leading zeros - because these checks exist to catch exactly those files
and a fixture that could not come out of a real system proves nothing about them.

Each code gets one file that trips it and one that does not; the pair is what shows the check reads
the data rather than the shape of the params.
"""

from __future__ import annotations

import pandas as pd

from engine.contracts import Severity
from engine.onboarding.specs import ONBOARDING_VALIDATION_CODES, SnapshotMode
from engine.onboarding.validate import (
    CHECK_ORDER,
    CHECK_REGISTRY,
    OnboardingCheckParams,
    SourceFacts,
    check_date_format_ambiguous,
    check_entity_attributes_not_time_versioned,
    check_entity_duplicate_keys,
    check_entity_key_unmapped,
    check_event_time_unmapped,
    check_event_time_unparseable,
    check_feature_all_null,
    check_feature_name_collision,
    check_future_events_leaked,
    check_join_key_coverage_low,
    check_join_key_unmapped,
    check_key_format_mismatch,
    check_multiple_entity_sources,
    check_no_entity_source,
    check_required_standard_column_unmapped,
    check_source_too_large,
    check_too_many_features,
    check_too_many_sources,
    run_onboarding_checks,
)

#: The eight plan section 7 codes that belong to labels.py, snapshots.py and the mapping stage.
CODES_OWNED_ELSEWHERE = {
    "LABEL_DEGENERATE_SNAPSHOT",
    "LABEL_HORIZON_CENSORED",
    "LABEL_ROLE_MISSING",
    "MAPPING_LOW_CONFIDENCE",
    "MAPPING_TYPE_CONFLICT",
    "SNAPSHOT_OUTSIDE_DATA_RANGE",
    "TOO_LITTLE_HISTORY",
    "VALUE_UNMAPPED",
}


def customers(
    keys=("c1", "c2", "c3"),
    *,
    mapped=("entity_key", "plan"),
    extra: dict[str, list[object]] | None = None,
    **overrides,
) -> SourceFacts:
    columns: dict[str, object] = {"entity_key": list(keys), "plan": ["gold"] * len(keys)}
    columns.update(extra or {})
    defaults = {
        "source_id": "src_customers",
        "role": "entity",
        "rows": len(keys),
        "mapped_standard": mapped,
        "frame": pd.DataFrame(columns),
    }
    return SourceFacts(**{**defaults, **overrides})


def bills(
    keys=("c1", "c2", "c3"),
    dates=("2025-01-31", "2025-02-28", "2025-03-31"),
    *,
    mapped=("entity_key", "event_time", "amount"),
    parse_dates=True,
    **overrides,
) -> SourceFacts:
    stamps = pd.to_datetime(list(dates)) if parse_dates else list(dates)
    defaults = {
        "source_id": "src_bills",
        "role": "bills",
        "rows": len(keys),
        "mapped_standard": mapped,
        "frame": pd.DataFrame({"entity_key": list(keys), "event_time": stamps, "amount": [10] * len(keys)}),
    }
    return SourceFacts(**{**defaults, **overrides})


def codes(findings) -> list[str]:
    return [finding.code for finding in findings]


def test_no_entity_source_fires_when_every_table_is_an_event_table():
    without = OnboardingCheckParams(sources=(bills(),))
    (finding,) = check_no_entity_source(without)
    assert finding.severity is Severity.ERROR
    assert finding.suggestion == "Add the table that has one row per customer."

    assert check_no_entity_source(OnboardingCheckParams(sources=(customers(), bills()))) == ()


def test_multiple_entity_sources_fires_only_above_one():
    two = OnboardingCheckParams(sources=(customers(), customers(source_id="src_crm")))
    (finding,) = check_multiple_entity_sources(two)
    assert finding.details["source_ids"] == ["src_customers", "src_crm"]

    assert check_multiple_entity_sources(OnboardingCheckParams(sources=(customers(),))) == ()


def test_entity_key_unmapped_looks_at_the_entity_table_only():
    params = OnboardingCheckParams(sources=(customers(mapped=("plan",)), bills(mapped=("event_time",))))
    (finding,) = check_entity_key_unmapped(params)
    assert finding.source_id == "src_customers"

    assert check_entity_key_unmapped(OnboardingCheckParams(sources=(customers(),))) == ()


def test_entity_duplicate_keys_counts_the_repeats_and_offers_a_dedupe_column():
    repeated = customers(
        ("c1", "c1", "c2"),
        extra={"updated_at": list(pd.to_datetime(["2025-01-01", "2025-06-01", "2025-01-01"]))},
    )
    (finding,) = check_entity_duplicate_keys(OnboardingCheckParams(sources=(repeated,)))
    assert (finding.details["rows"], finding.details["distinct_keys"]) == (3, 2)
    assert finding.details["duplicate_rows"] == 1
    assert finding.details["dedupe_by"] == "updated_at"
    assert "latest row per customer by 'updated_at'" in finding.suggestion

    assert check_entity_duplicate_keys(OnboardingCheckParams(sources=(customers(),))) == ()


def test_entity_duplicate_keys_offers_no_column_when_the_table_carries_no_date():
    repeated = customers(("c1", "c1"))
    (finding,) = check_entity_duplicate_keys(OnboardingCheckParams(sources=(repeated,)))
    assert finding.details["dedupe_by"] is None
    assert "latest row" not in finding.suggestion


def test_join_key_unmapped_looks_at_the_event_tables_only():
    params = OnboardingCheckParams(sources=(customers(mapped=("plan",)), bills(mapped=("event_time",))))
    (finding,) = check_join_key_unmapped(params)
    assert finding.source_id == "src_bills"

    assert check_join_key_unmapped(OnboardingCheckParams(sources=(customers(), bills()))) == ()


def test_event_time_unmapped_does_not_ask_the_entity_table_for_a_date():
    params = OnboardingCheckParams(sources=(customers(), bills(mapped=("entity_key", "amount"))))
    (finding,) = check_event_time_unmapped(params)
    assert finding.source_id == "src_bills"

    assert check_event_time_unmapped(OnboardingCheckParams(sources=(customers(), bills()))) == ()


def test_event_time_unparseable_fires_above_two_percent_and_names_the_dominant_format():
    messy = bills(
        keys=tuple(f"c{i}" for i in range(10)),
        dates=("01-Feb-2024",) * 8 + ("not a date", "n/a"),
        parse_dates=False,
    )
    (finding,) = check_event_time_unparseable(OnboardingCheckParams(sources=(messy,)))
    assert finding.details["unparsed_rows"] == 2
    assert finding.details["checked_rows"] == 10
    assert finding.details["suggested_format"] == "%d-%b-%Y"
    assert "80% of them" in finding.suggestion

    clean = bills(
        keys=tuple(f"c{i}" for i in range(10)),
        dates=("2024-02-01",) * 10,
        parse_dates=False,
    )
    assert check_event_time_unparseable(OnboardingCheckParams(sources=(clean,))) == ()


def test_date_format_ambiguous_quotes_a_real_value_from_the_column():
    ambiguous = bills(dates=("03/04/2025", "15/04/2025", "02/07/2025"), parse_dates=False)
    (finding,) = check_date_format_ambiguous(OnboardingCheckParams(sources=(ambiguous,)))
    assert "Is 03/04/2025 the 3rd of April or the 4th of March?" in finding.message
    assert finding.details["affected_rows"] == 2
    assert finding.severity is Severity.ERROR
    assert not finding.acknowledgeable

    unambiguous = bills(dates=("2025-04-03", "2025-04-15"), keys=("c1", "c2"), parse_dates=False)
    assert check_date_format_ambiguous(OnboardingCheckParams(sources=(unambiguous,))) == ()


def test_date_format_ambiguous_is_answered_by_pinning_the_format():
    ambiguous = bills(dates=("03/04/2025", "05/06/2025", "02/07/2025"), parse_dates=False)
    pinned = SourceFacts(
        source_id=ambiguous.source_id,
        role=ambiguous.role,
        rows=ambiguous.rows,
        mapped_standard=ambiguous.mapped_standard,
        frame=ambiguous.frame,
        event_time_format="%d/%m/%Y",
    )
    assert check_date_format_ambiguous(OnboardingCheckParams(sources=(pinned,))) == ()


def test_join_key_coverage_low_warns_below_eighty_and_errors_below_thirty():
    entity_keys = tuple(f"c{i}" for i in range(10))
    seven = bills(keys=(*entity_keys[:7], "x1", "x2", "x3"), dates=("2025-01-31",) * 10)
    (warning,) = check_join_key_coverage_low(OnboardingCheckParams(sources=(customers(entity_keys), seven)))
    assert warning.severity is Severity.WARNING
    assert warning.message == "Only 70% of bills rows belong to a known customer."
    assert warning.suggestion == "Check that the key columns match (e.g. leading zeros, prefixes)."

    two = bills(keys=(*entity_keys[:2], *(f"x{i}" for i in range(8))), dates=("2025-01-31",) * 10)
    (error,) = check_join_key_coverage_low(OnboardingCheckParams(sources=(customers(entity_keys), two)))
    assert error.severity is Severity.ERROR

    joined = bills(keys=entity_keys, dates=("2025-01-31",) * 10)
    assert check_join_key_coverage_low(OnboardingCheckParams(sources=(customers(entity_keys), joined))) == ()


def test_key_format_mismatch_names_the_transform_that_would_join_the_tables():
    padded = bills(keys=("007", "008", "009"))
    params = OnboardingCheckParams(sources=(customers(("7", "8", "9")), padded))
    (finding,) = check_key_format_mismatch(params)
    assert finding.details["transform"] == "lstrip_zeros"
    assert finding.severity is Severity.WARNING

    assert check_key_format_mismatch(OnboardingCheckParams(sources=(customers(), bills()))) == ()


def test_required_standard_column_unmapped_reads_every_table_before_complaining():
    params = OnboardingCheckParams(
        sources=(customers(), bills()),
        required_standard_columns=("plan", "amount", "tenure_months"),
    )
    (finding,) = check_required_standard_column_unmapped(params)
    assert finding.column == "tenure_months"

    satisfied = OnboardingCheckParams(sources=(customers(), bills()), required_standard_columns=("amount",))
    assert check_required_standard_column_unmapped(satisfied) == ()


def test_feature_all_null_fires_above_the_configured_fraction():
    params = OnboardingCheckParams(
        feature_null_fractions={"bills_count_7d": 0.99, "bills_count_90d": 0.4},
        drop_if_null_fraction_above=0.98,
    )
    (finding,) = check_feature_all_null(params)
    assert finding.column == "bills_count_7d"
    assert finding.severity is Severity.WARNING
    assert "window is too short or the filter matches nothing" in finding.suggestion

    assert check_feature_all_null(OnboardingCheckParams(feature_null_fractions={"a": 0.4})) == ()


def test_feature_name_collision_sees_a_feature_that_shadows_a_mapped_column():
    params = OnboardingCheckParams(sources=(customers(),), feature_names=("plan", "bills_count_90d"))
    (finding,) = check_feature_name_collision(params)
    assert finding.column == "plan"
    assert finding.details["occurrences"] == 2

    assert (
        check_feature_name_collision(OnboardingCheckParams(sources=(customers(),), feature_names=("x",)))
        == ()
    )


def test_too_many_features_counts_against_the_limit():
    params = OnboardingCheckParams(feature_names=("a", "b", "c"), max_features=2)
    (finding,) = check_too_many_features(params)
    assert finding.details == {"feature_count": 3, "max_features": 2}

    assert check_too_many_features(OnboardingCheckParams(feature_names=("a", "b"), max_features=2)) == ()


def test_entity_attributes_not_time_versioned_only_matters_for_periodic_snapshots():
    periodic = OnboardingCheckParams(sources=(customers(),), snapshot_mode=SnapshotMode.PERIODIC)
    (finding,) = check_entity_attributes_not_time_versioned(periodic)
    assert finding.message.startswith(
        "Customer attributes are taken as they are today, not as they were at each snapshot"
    )
    assert finding.severity is Severity.WARNING

    single = OnboardingCheckParams(sources=(customers(),), snapshot_mode=SnapshotMode.SINGLE)
    assert check_entity_attributes_not_time_versioned(single) == ()

    dated = customers(mapped=("entity_key", "event_time", "plan"))
    assert (
        check_entity_attributes_not_time_versioned(
            OnboardingCheckParams(sources=(dated,), snapshot_mode=SnapshotMode.PERIODIC)
        )
        == ()
    )


def test_future_events_leaked_is_an_error_nobody_may_acknowledge():
    (finding,) = check_future_events_leaked(OnboardingCheckParams(future_event_rows={"bills": 412}))
    assert finding.severity is Severity.ERROR
    assert not finding.acknowledgeable
    assert "412 bills events" in finding.message

    assert check_future_events_leaked(OnboardingCheckParams(future_event_rows={"bills": 0})) == ()


def test_source_too_large_measures_the_file_not_the_frame_it_was_read_into():
    big = bills(rows=2_000_000)
    params = OnboardingCheckParams(sources=(big,), max_source_rows=1_000_000)
    (finding,) = check_source_too_large(params)
    assert finding.details["rows"] == 2_000_000

    assert check_source_too_large(OnboardingCheckParams(sources=(bills(),), max_source_rows=10)) == ()


def test_too_many_sources_counts_the_tables():
    params = OnboardingCheckParams(sources=(customers(), bills(), bills(source_id="b2")), max_sources=2)
    (finding,) = check_too_many_sources(params)
    assert finding.details == {"source_count": 3, "max_sources": 2}

    assert check_too_many_sources(OnboardingCheckParams(sources=(customers(),), max_sources=2)) == ()


def test_the_registry_holds_exactly_the_codes_this_module_owns():
    assert set(CHECK_REGISTRY) == set(CHECK_ORDER)
    assert len(CHECK_ORDER) == len(set(CHECK_ORDER))
    assert set(CHECK_ORDER) <= ONBOARDING_VALIDATION_CODES
    assert ONBOARDING_VALIDATION_CODES - set(CHECK_ORDER) == CODES_OWNED_ELSEWHERE


def test_run_reports_errors_before_warnings():
    params = OnboardingCheckParams(
        sources=(customers(("c1", "c1", "c2")), bills(keys=("c1", "c2", "c3"))),
        feature_names=("a", "b", "c"),
        max_features=2,
    )
    assert codes(run_onboarding_checks(params)) == [
        "ENTITY_DUPLICATE_KEYS",
        "TOO_MANY_FEATURES",
        "JOIN_KEY_COVERAGE_LOW",
        "ENTITY_ATTRIBUTES_NOT_TIME_VERSIONED",
    ]


def test_a_check_that_cannot_read_a_column_does_not_stop_the_others():
    unreadable = customers(([1], [2]))
    params = OnboardingCheckParams(sources=(unreadable,), feature_names=("a", "b"), max_features=1)
    assert "TOO_MANY_FEATURES" in codes(run_onboarding_checks(params))


def test_no_check_raises_on_params_that_hold_nothing():
    params = OnboardingCheckParams()
    found = [finding.code for check in CHECK_REGISTRY.values() for finding in check(params)]
    assert found == ["NO_ENTITY_SOURCE"]
