"""Memory per client (Plan L M159, DEC-1590 … DEC-1595): the store, the offer, retention and erasure."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from engine.access.roles import LOCAL_OPERATOR
from engine.agent.contracts import DataRecipe, DecidedBy, RecipeStep, RecipeStepKind, recipe_hash
from engine.doctor.memory import (
    MEMORY_PREFIX,
    TreatmentMemory,
    forget,
    memory_key,
    offer_for,
    recall,
    remember,
    remembered_recipe,
    shape_of,
)
from engine.intake.contracts import IntakeReport, SheetInfo
from engine.platform_db import sqlite_engine
from engine.privacy.contracts import RetentionCategory
from engine.privacy.erasure import erase, find_principal
from engine.privacy.layout import Store, StoreIndex, store_of
from engine.privacy.retention import apply_retention, plan_retention, retention_days_by_use_case
from engine.storage import LocalStorage, upload_key

USE_CASE = "targeted-advertisement"
COLUMNS = ("customer_id", "monthly_spend", "region", "converted_30d")
T0 = datetime(2026, 6, 1, tzinfo=UTC)


def recipe(recipe_id: str = "rc-month-one") -> DataRecipe:
    steps = (
        RecipeStep(
            order=1,
            kind=RecipeStepKind.MAP_VALUES,
            column="region",
            params={"mapping": {"Bombay": "Mumbai"}},
            decided_by=DecidedBy.USER,
        ),
        RecipeStep(
            order=2, kind=RecipeStepKind.DROP_COLUMN, column="monthly_spend", decided_by=DecidedBy.USER
        ),
    )
    return DataRecipe(
        recipe_id=recipe_id,
        use_case_id=USE_CASE,
        source_fingerprint="sha256:" + "0" * 64,
        steps=steps,
        recipe_hash=recipe_hash(steps),
        primary_key="customer_id",
        target="converted_30d",
        created_at=T0,
    )


def upload_record(storage: LocalStorage, upload_id: str, name: str = "billing_sept.csv") -> None:
    storage.write_text(
        upload_key(upload_id, "upload.json"),
        json.dumps(
            {"upload_id": upload_id, "file_name": name, "created_at": T0.isoformat(), "use_case_id": USE_CASE}
        ),
    )


def planted(root: Path, *, sheet: str | None = None) -> tuple[LocalStorage, TreatmentMemory]:
    storage = LocalStorage(root)
    upload_record(storage, "u_month_one")
    upload_record(storage, "u_month_one_prepared", "billing_sept.csv")
    storage.write_model(upload_key("u_month_one_prepared", "data_recipe.json"), recipe())
    memory = remember(
        storage,
        use_case_id=USE_CASE,
        columns=COLUMNS,
        sheet=sheet,
        intake_enabled=True,
        recipe=recipe(),
        recipe_upload_id="u_month_one_prepared",
        source_upload_id="u_month_one",
        clock=lambda: T0,
    )
    return storage, memory


def test_the_shape_is_the_columns_in_order_and_the_sheet() -> None:
    assert shape_of(COLUMNS) == shape_of(list(COLUMNS))
    assert shape_of(COLUMNS) != shape_of(tuple(reversed(COLUMNS))), "column order is part of the shape"
    assert shape_of(COLUMNS) != shape_of(COLUMNS, "Billing"), "so is the sheet"
    assert shape_of(COLUMNS, "Billing") == shape_of(COLUMNS, "Billing")
    assert shape_of(COLUMNS).startswith("shape:")


def test_remember_and_recall_store_names_and_ids_only(tmp_path: Path) -> None:
    storage, memory = planted(tmp_path)
    key = memory_key(USE_CASE, memory.shape_hash)
    assert key.startswith(f"{MEMORY_PREFIX}{USE_CASE}/") and storage.list_keys(MEMORY_PREFIX) == (key,)
    assert recall(storage, USE_CASE, shape_of(COLUMNS)) == memory
    assert (memory.recipe_id, memory.steps, memory.columns) == ("rc-month-one", 2, COLUMNS)
    stored = storage.read_text(key)
    assert "Bombay" not in stored and "Mumbai" not in stored, "the steps' values stay in the recipe"
    loaded = remembered_recipe(storage, memory)
    assert loaded is not None and loaded.recipe_id == "rc-month-one"
    assert recall(storage, "telco-churn", memory.shape_hash) is None, "memory is per use case"


def test_a_memory_whose_recipe_is_gone_is_not_recalled(tmp_path: Path) -> None:
    storage, memory = planted(tmp_path)
    storage.delete(upload_key("u_month_one_prepared", "data_recipe.json"))
    assert recall(storage, USE_CASE, memory.shape_hash) is None
    assert forget(storage, USE_CASE, memory.shape_hash) and not forget(storage, USE_CASE, memory.shape_hash)


def test_a_later_approval_of_the_same_shape_wins(tmp_path: Path) -> None:
    storage, _ = planted(tmp_path)
    storage.write_model(upload_key("u_two_prepared", "data_recipe.json"), recipe("rc-month-two"))
    remember(
        storage,
        use_case_id=USE_CASE,
        columns=COLUMNS,
        sheet=None,
        intake_enabled=True,
        recipe=recipe("rc-month-two"),
        recipe_upload_id="u_two_prepared",
        source_upload_id="u_two",
    )
    found = recall(storage, USE_CASE, shape_of(COLUMNS))
    assert found is not None and found.recipe_id == "rc-month-two"
    assert len(storage.list_keys(MEMORY_PREFIX)) == 1


def test_a_second_file_of_the_same_shape_is_offered_the_treatment(tmp_path: Path) -> None:
    storage, memory = planted(tmp_path)
    offer = offer_for(
        storage, use_case_id=USE_CASE, upload_id="u_month_two", columns=COLUMNS, intake=None, mode="train"
    )
    assert offer is not None and offer.kind == "same_shape" and offer.can_apply
    assert (offer.recipe_id, offer.steps, offer.source_file_name) == ("rc-month-one", 2, "billing_sept.csv")
    assert offer.sentence.startswith(
        "This looks like billing_sept.csv, which you prepared with Guided setup on 1 Jun 2026"
    )
    assert "nothing changes until you approve" in offer.sentence
    scoring = offer_for(
        storage, use_case_id=USE_CASE, upload_id="u_score", columns=COLUMNS, intake=None, mode="score"
    )
    assert scoring is not None and not scoring.can_apply, "a scoring file is prepared by its model's steps"
    assert memory.source_upload_id == "u_month_one"
    assert (
        offer_for(
            storage, use_case_id=USE_CASE, upload_id="u_month_one", columns=COLUMNS, intake=None, mode="train"
        )
        is None
    ), "the file the treatment was approved on is not offered its own treatment"


def test_a_file_of_another_shape_is_offered_nothing(tmp_path: Path) -> None:
    storage, _ = planted(tmp_path)
    for columns in (
        (*COLUMNS, "new_column"),
        COLUMNS[:-1],
        ("monthly_spend", "customer_id", "region", "converted_30d"),
    ):
        assert (
            offer_for(
                storage, use_case_id=USE_CASE, upload_id="u_x", columns=columns, intake=None, mode="train"
            )
            is None
        )
    assert (
        offer_for(
            storage, use_case_id="telco-churn", upload_id="u_x", columns=COLUMNS, intake=None, mode="train"
        )
        is None
    )


def workbook(read: str) -> IntakeReport:
    return IntakeReport(
        source_format="xlsx",
        applied=True,
        sheet=read,
        sheets=(
            SheetInfo(name="Summary", rows=10, looks_like_data=True, hidden=False, used=read == "Summary"),
            SheetInfo(name="Billing", rows=900, looks_like_data=True, hidden=False, used=read == "Billing"),
        ),
    )


def test_a_workbook_is_matched_by_its_sheet_too(tmp_path: Path) -> None:
    storage, _ = planted(tmp_path, sheet="Billing")
    same = offer_for(
        storage,
        use_case_id=USE_CASE,
        upload_id="u_book",
        columns=COLUMNS,
        intake=workbook("Billing"),
        mode="train",
    )
    assert same is not None and same.kind == "same_shape" and same.sheet == "Billing"
    other = offer_for(
        storage,
        use_case_id=USE_CASE,
        upload_id="u_book",
        columns=("label", "value"),
        intake=workbook("Summary"),
        mode="train",
    )
    assert other is not None and other.kind == "same_sheet" and not other.can_apply
    assert other.sheet == "Billing" and "“Billing”" in other.sentence


def test_retention_deletes_the_memory_with_its_recipe_or_at_its_own_deadline(
    tmp_path: Path, config_root: Path
) -> None:
    storage, memory = planted(tmp_path / "data")
    key = memory_key(USE_CASE, memory.shape_hash)
    assert store_of(key) is Store.DOCTOR_MEMORY
    assert StoreIndex(storage).memories[key].recipe_upload_id == "u_month_one_prepared"
    by_use_case, default = retention_days_by_use_case(config_root)
    days = by_use_case.get(USE_CASE, default)

    early = plan_retention(storage, config_root, now=T0 + timedelta(days=days - 1))
    assert key not in early.planned_keys()

    due = plan_retention(storage, config_root, now=T0 + timedelta(days=days + 1))
    (item,) = [i for i in due.items if i.category is RetentionCategory.DOCTOR_MEMORY]
    assert (item.key, item.use_case_id, item.retention_days) == (key, USE_CASE, days)
    assert upload_key("u_month_one_prepared", "data_recipe.json") in due.planned_keys()
    result = apply_retention(due, storage, None, LOCAL_OPERATOR)
    assert key in result.deleted and storage.list_keys(MEMORY_PREFIX) == ()


def test_retention_deletes_a_memory_whose_recipe_upload_is_gone(tmp_path: Path, config_root: Path) -> None:
    storage, memory = planted(tmp_path / "data")
    for prepared in storage.list_keys("uploads/u_month_one_prepared/"):
        storage.delete(prepared)
    plan = plan_retention(storage, config_root, now=T0 + timedelta(days=1))
    assert [i.key for i in plan.items if i.category is RetentionCategory.DOCTOR_MEMORY] == [
        memory_key(USE_CASE, memory.shape_hash)
    ]


def test_an_undated_memory_is_skipped_not_deleted(tmp_path: Path, config_root: Path) -> None:
    storage, memory = planted(tmp_path / "data")
    key = memory_key(USE_CASE, memory.shape_hash)
    document = json.loads(storage.read_text(key))
    document["remembered_at"] = None
    storage.write_text(key, json.dumps(document))
    plan = plan_retention(storage, config_root, now=T0 + timedelta(days=3650))
    assert key not in plan.planned_keys()
    assert any(
        s.category is RetentionCategory.DOCTOR_MEMORY and s.reason_code == "UNDATED" for s in plan.skipped
    )


SENTINEL = "ZQX-MEMORY-7731"


@pytest.mark.parametrize("planted_id", [True, False])
def test_erasure_deletes_a_memory_that_holds_the_id(
    tmp_path: Path, config_root: Path, planted_id: bool
) -> None:
    storage = LocalStorage(tmp_path / "data")
    upload_record(storage, "u_prepared")
    storage.write_model(upload_key("u_prepared", "data_recipe.json"), recipe())
    columns = (*COLUMNS, SENTINEL) if planted_id else COLUMNS  # a column named by an id: contrived, covered
    memory = remember(
        storage,
        use_case_id=USE_CASE,
        columns=columns,
        sheet=None,
        intake_enabled=True,
        recipe=recipe(),
        recipe_upload_id="u_prepared",
        source_upload_id="u_source",
    )
    key = memory_key(USE_CASE, memory.shape_hash)
    found = {location.key: location for location in find_principal(storage, SENTINEL).locations}
    assert (key in found) is planted_id
    if planted_id:
        assert found[key].store == Store.DOCTOR_MEMORY
    engine = sqlite_engine(tmp_path / "data" / "platform.db")
    outcome = erase(
        storage, SENTINEL, engine=engine, principal=LOCAL_OPERATOR, salt="s" * 24, config_root=config_root
    )
    engine.dispose()
    assert outcome.status == "completed"
    assert storage.exists(key) is not planted_id, "an entry holding the id is deleted, any other is kept"
    assert find_principal(storage, SENTINEL).locations == ()
