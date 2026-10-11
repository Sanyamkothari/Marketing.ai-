"""The outcome from events, shown back as a sentence (Plan L M157, DEC-1570 ... DEC-1574).

Every count an offer shows must be one onboarding's label builder measured on the files; these tests
recompute each one independently, with pandas, from the same rows.
"""

from __future__ import annotations

from datetime import date, timedelta
from pathlib import Path

import pandas as pd
import pytest

from engine.config import LabelType, get_roles, load_use_case
from engine.doctor.contracts import UseCaseReadiness
from engine.doctor.diagnose import LoadedFile, diagnose_file
from engine.doctor.outcomes import (
    OutcomeCatalogue,
    OutcomeFile,
    OutcomeOffer,
    OutcomeTemplate,
    attach_outcome_offers,
    event_word,
    load_outcome_catalogue,
    offer_outcome,
    sentence_for,
)
from engine.stages.ingest import profile_dataset

USE_CASE = "rca"
START = date(2025, 1, 1)
END = date(2025, 6, 30)
CUSTOMERS = 200
STOPPED = range(150, 200)
"""Customers whose recharges stop on 15 Feb: churned by any of the windows."""
LATE = range(100, 120)
"""Customers whose recharges stop on 25 May: quiet since, but no 60-day window after 31 May is over yet."""


def as_file(frame: pd.DataFrame, name: str, config_root: Path, upload_id: str) -> OutcomeFile:
    config = load_use_case(USE_CASE, config_root)
    profile = profile_dataset(
        frame,
        config,
        upload_id=upload_id,
        file_name=name,
        file_format="csv",
        file_size_bytes=0,
        delimiter=",",
        encoding="utf-8",
        row_count=len(frame),
    )
    loaded = LoadedFile(upload_id=upload_id, file_name=name, frame=frame, profile=profile)
    return OutcomeFile(diagnosis=diagnose_file(loaded, config, get_roles(config_root)), frame=frame)


def customers() -> pd.DataFrame:
    return pd.DataFrame(
        {
            "customer_id": [f"C{index:04d}" for index in range(CUSTOMERS)],
            "region": ["North", "South", "East", "West"] * (CUSTOMERS // 4),
            "plan_type": ["prepaid", "postpaid"] * (CUSTOMERS // 2),
        }
    )


def recharges() -> pd.DataFrame:
    rows: list[tuple[str, str, float]] = []
    for index in range(CUSTOMERS):
        stop = date(2025, 2, 15) if index in STOPPED else date(2025, 5, 25) if index in LATE else END
        day = START + timedelta(days=index % 7)
        while day <= stop:
            rows.append((f"C{index:04d}", day.isoformat(), 199.0 + index % 3))
            day += timedelta(days=10)
    # one customer recharges on the very last day, so the file runs to END
    rows.append(("C0000", END.isoformat(), 199.0))
    frame = pd.DataFrame(rows, columns=["customer_id", "recharge_date", "amount"])
    frame.insert(0, "recharge_id", [f"R{index:06d}" for index in range(len(frame))])
    return frame


def expected_yes(events: pd.DataFrame, snapshot: date, days: int) -> int:
    """Customers with no recharge in (snapshot, snapshot + days], counted directly."""
    times = pd.to_datetime(events["recharge_date"])
    low, high = pd.Timestamp(snapshot), pd.Timestamp(snapshot) + pd.Timedelta(days=days)
    active = set(events.loc[(times > low) & (times <= high), "customer_id"])
    return sum(1 for index in range(CUSTOMERS) if f"C{index:04d}" not in active)


@pytest.fixture
def files(config_root: Path) -> list[OutcomeFile]:
    return [
        as_file(customers(), "customers.csv", config_root, "u_list"),
        as_file(recharges(), "recharges_2025.csv", config_root, "u_events"),
    ]


def rca_offer(files: list[OutcomeFile], config_root: Path) -> OutcomeOffer | None:
    catalogue = load_outcome_catalogue(config_root)
    return offer_outcome(
        load_use_case(USE_CASE, config_root),
        catalogue.templates[USE_CASE],
        files,
        catalogue=catalogue,
        roles=get_roles(config_root),
    )


def test_the_offer_is_the_sentence_with_counts_the_label_builder_measured(
    files: list[OutcomeFile], config_root: Path
) -> None:
    offer = rca_offer(files, config_root)
    assert offer is not None
    assert offer.entity_upload_id == "u_list" and offer.entity_id_column == "customer_id"
    assert offer.event_upload_id == "u_events" and offer.date_column == "recharge_date"
    assert offer.event_id_column == "customer_id", "the IDs, not the recharge's own id"
    assert offer.event_word == "recharge"
    assert offer.customers == CUSTOMERS and offer.matched_customers == CUSTOMERS
    assert offer.data_end == END
    by_days = {choice.days: choice for choice in offer.choices}
    assert sorted(by_days) == [30, 60, 90]
    assert [choice.default for choice in offer.choices] == [False, True, False]
    sixty = by_days[60]
    assert (
        sixty.sentence == "A customer has churned when there is no recharge for 60 days after the snapshot."
    )
    assert sixty.label_spec.type is LabelType.EVENT_ABSENCE
    assert sixty.label_spec.horizon_days == 60 and sixty.label_spec.role == offer.event_role
    assert sixty.label_spec.name == "churn_next_60d"
    events = recharges()
    for days, measured_on in ((30, date(2025, 5, 31)), (60, date(2025, 4, 30)), (90, date(2025, 3, 31))):
        choice = by_days[days]
        assert choice.measured_on == measured_on
        assert choice.customers == CUSTOMERS
        assert choice.yes == expected_yes(events, measured_on, days)
        assert choice.no == CUSTOMERS - choice.yes
        assert choice.yes_rate == round(choice.yes / CUSTOMERS, 4)
        assert f"{choice.yes:,} of {CUSTOMERS:,} customers" in choice.summary


def test_customers_whose_window_is_not_over_are_not_counted_as_no(
    files: list[OutcomeFile], config_root: Path
) -> None:
    offer = rca_offer(files, config_root)
    assert offer is not None
    sixty = next(choice for choice in offer.choices if choice.days == 60)
    # 31 May and 30 Jun have 60 days after them that run past 30 Jun: censored, not counted.
    assert sixty.not_over == 2
    assert "2 later month-ends not counted" in sixty.summary
    # Counted at 30 Apr, the customers who stopped on 25 May recharged in May: they are "no" there,
    # and the 50 who stopped in February are the "yes". At 31 May they would have read as churned
    # without anyone knowing yet; that date is left out.
    assert sixty.yes == len(STOPPED)
    assert expected_yes(recharges(), date(2025, 5, 31), 60) == len(STOPPED) + len(LATE)
    assert "neither 'yes' nor 'no'" in offer.censoring


def test_a_file_without_dates_gets_no_offer(config_root: Path) -> None:
    no_dates = recharges().drop(columns=["recharge_date"])
    files = [
        as_file(customers(), "customers.csv", config_root, "u_list"),
        as_file(no_dates, "recharges_2025.csv", config_root, "u_events"),
    ]
    assert files[1].diagnosis.date_columns == ()
    assert rca_offer(files, config_root) is None


def test_no_customer_list_or_no_shared_ids_gets_no_offer(config_root: Path) -> None:
    only_events = [as_file(recharges(), "recharges_2025.csv", config_root, "u_events")]
    assert rca_offer(only_events, config_root) is None
    strangers = recharges().assign(customer_id=lambda frame: "X" + frame["customer_id"])
    files = [
        as_file(customers(), "customers.csv", config_root, "u_list"),
        as_file(strangers, "recharges_2025.csv", config_root, "u_events"),
    ]
    assert rca_offer(files, config_root) is None


def test_a_window_longer_than_the_file_is_offered_without_a_count(config_root: Path) -> None:
    short = recharges()
    short = short[pd.to_datetime(short["recharge_date"]) >= pd.Timestamp(2025, 5, 1)]
    files = [
        as_file(customers(), "customers.csv", config_root, "u_list"),
        as_file(short.reset_index(drop=True), "recharges.csv", config_root, "u_events"),
    ]
    offer = rca_offer(files, config_root)
    assert offer is not None
    ninety = next(choice for choice in offer.choices if choice.days == 90)
    assert ninety.measured_on is None and ninety.yes is None and ninety.customers is None
    assert ninety.summary.startswith("Not counted")
    thirty = next(choice for choice in offer.choices if choice.days == 30)
    assert thirty.measured_on == date(2025, 5, 31)
    assert thirty.yes == expected_yes(short, date(2025, 5, 31), 30)


def test_only_a_missing_use_case_is_given_an_offer(files: list[OutcomeFile], config_root: Path) -> None:
    config = load_use_case(USE_CASE, config_root)
    missing = UseCaseReadiness(
        use_case_id=USE_CASE, name=config.name, stage="Churn", status="missing", upload_id=None, summary="x"
    )
    ready = missing.model_copy(update={"status": "ready", "upload_id": "u_list"})
    attached = attach_outcome_offers((missing, ready), files, {USE_CASE: config}, config_root)
    assert attached[0].outcome_offer is not None
    assert attached[1].outcome_offer is None
    assert ready.outcome_offer is None and missing.outcome_offer is None


def test_event_words_come_from_the_list_or_the_files_kind() -> None:
    catalogue = OutcomeCatalogue(event_words=("recharge", "order"), kind_words={"activity": "activity"})
    assert event_word("Recharges_March.csv", "payments", catalogue) == "recharge"
    assert event_word("orders.xlsx", "orders", catalogue) == "order"
    assert event_word("export_2025.csv", "activity", catalogue) == "activity"
    assert event_word("export_2025.csv", "usage", catalogue) == "event"


def test_the_sentence_fills_its_blanks_with_articles() -> None:
    template = OutcomeTemplate(
        question="q",
        sentence="{An_entity} has come back when there is {an_event} within {days} days.",
        label_type="event_presence",
        event_kinds=("orders",),
        days=(30,),
        default_days=30,
    )
    assert sentence_for(template, entity="asset", event="order", days=30) == (
        "An asset has come back when there is an order within 30 days."
    )


def test_a_template_with_an_unknown_blank_or_default_is_refused() -> None:
    with pytest.raises(ValueError, match="unknown blank"):
        OutcomeTemplate(
            question="q",
            sentence="{customer} left",
            label_type="event_absence",
            event_kinds=("activity",),
            days=(30,),
            default_days=30,
        )
    with pytest.raises(ValueError, match="default_days"):
        OutcomeTemplate(
            question="q",
            sentence="{An_entity} left",
            label_type="event_absence",
            event_kinds=("activity",),
            days=(30,),
            default_days=60,
        )
    with pytest.raises(ValueError):
        OutcomeTemplate.model_validate(
            {
                "question": "q",
                "sentence": "s",
                "label_type": "value_threshold",
                "event_kinds": ["bills"],
                "days": [30],
                "default_days": 30,
            }
        )


def test_the_shipped_templates_name_real_use_cases_and_event_roles(config_root: Path) -> None:
    catalogue = load_outcome_catalogue(config_root)
    roles = get_roles(config_root)
    assert catalogue.templates, "configs/outcomes.yaml ships sentences"
    for use_case_id, template in catalogue.templates.items():
        config = load_use_case(use_case_id, config_root)
        assert config.target.column, use_case_id
        assert LabelType(template.label_type) in {LabelType.EVENT_ABSENCE, LabelType.EVENT_PRESENCE}
        for kind in template.event_kinds:
            assert roles.roles[kind].is_event, (use_case_id, kind)


def test_a_malformed_catalogue_names_the_file(tmp_path: Path) -> None:
    (tmp_path / "outcomes.yaml").write_text("templates: {rca: {days: lots}}\n", encoding="utf-8")
    with pytest.raises(ValueError, match=r"outcomes\.yaml"):
        load_outcome_catalogue(tmp_path)
    assert load_outcome_catalogue(tmp_path / "nowhere").templates == {}
