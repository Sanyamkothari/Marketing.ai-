"""How several files fit together (Plan L M155, DEC-1551, DEC-1552): links to the customer list, and stacks."""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pandas as pd

from engine.config import get_roles, load_use_case
from engine.doctor.contracts import Diagnosis
from engine.doctor.diagnose import LoadedFile, diagnose_file
from engine.doctor.joins import file_links, file_stacks, how_files_fit
from engine.stages.ingest import profile_dataset

USE_CASE = "targeted-advertisement"


def loaded(frame: pd.DataFrame, name: str, config_root: Path, upload_id: str) -> LoadedFile:
    profile = profile_dataset(
        frame,
        load_use_case(USE_CASE, config_root),
        upload_id=upload_id,
        file_name=name,
        file_format="csv",
        file_size_bytes=0,
        delimiter=",",
        encoding="utf-8",
        row_count=len(frame),
    )
    return LoadedFile(upload_id=upload_id, file_name=name, frame=frame, profile=profile)


def diagnosed(files: list[LoadedFile], config_root: Path) -> Diagnosis:
    config, roles = load_use_case(USE_CASE, config_root), get_roles(config_root)
    return Diagnosis(files=tuple(diagnose_file(file, config, roles) for file in files), use_cases=())


def customers(count: int = 200) -> pd.DataFrame:
    return pd.DataFrame(
        {
            "customer_id": [f"CUST-{index:05d}" for index in range(1, count + 1)],
            "city": [("Pune", "Delhi", "Chennai")[index % 3] for index in range(count)],
            "joined": pd.date_range("2023-01-01", periods=count, freq="D").strftime("%Y-%m-%d"),
        }
    )


def bills(month: int, rows: int = 400, *, customer_ids: list[object] | None = None) -> pd.DataFrame:
    rng = np.random.default_rng(month)
    ids = customer_ids or [f"CUST-{1 + index % 200:05d}" for index in range(rows)]
    return pd.DataFrame(
        {
            "customer_id": ids[:rows],
            "invoice_date": [f"2025-{month:02d}-{1 + index % 28:02d}" for index in range(rows)],
            "amount": rng.gamma(2.0, 300.0, size=rows).round(2),
        }
    )


def test_a_bills_log_keyed_without_the_prefix_and_zeros_is_found_with_its_fix(config_root: Path) -> None:
    plain = [1 + index % 200 for index in range(400)]  # 123, where the list says CUST-00123
    files = [
        loaded(customers(), "customers.csv", config_root, "u_list"),
        loaded(
            bills(1).assign(customer_id=plain).rename(columns={"customer_id": "cust_no"}),
            "bills_2025.csv",
            config_root,
            "u_bills",
        ),
    ]
    diagnosis = diagnosed(files, config_root)
    kinds = {file.upload_id: file.kind for file in diagnosis.files}
    assert kinds == {"u_list": "entity", "u_bills": "bills"}

    (link,) = file_links(files, diagnosis.files, get_roles(config_root))

    assert (link.upload_id, link.column) == ("u_bills", "cust_no")
    assert (link.list_upload_id, link.list_column) == ("u_list", "customer_id")
    assert link.coverage == 0.0
    assert link.fixed_coverage == 1.0
    assert link.fix is not None and "'CUST-'" in link.fix and "zeros" in link.fix
    assert link.transform is None  # a prefix is not one onboarding transform
    assert "0%" in link.summary and "100%" in link.summary
    assert not link.sampled


def test_leading_zeros_alone_are_the_onboarding_transform(config_root: Path) -> None:
    listed = customers().assign(customer_id=[f"{index:05d}" for index in range(1, 201)])
    log = bills(2).assign(customer_id=[str(1 + index % 200) for index in range(400)])
    files = [
        loaded(listed, "customers.csv", config_root, "u_list"),
        loaded(log, "bills.csv", config_root, "u_b"),
    ]
    (link,) = file_links(files, diagnosed(files, config_root).files, get_roles(config_root))
    assert link.transform == "lstrip_zeros"
    assert (link.coverage, link.fixed_coverage) == (0.0, 1.0)


def test_a_log_that_meets_the_list_needs_no_fix_and_its_share_is_measured(config_root: Path) -> None:
    ids: list[object] = [f"CUST-{1 + index % 250:05d}" for index in range(400)]  # 50 of 250 are not listed
    files = [
        loaded(customers(), "customers.csv", config_root, "u_list"),
        loaded(bills(3, customer_ids=ids), "bills.csv", config_root, "u_b"),
    ]
    (link,) = file_links(files, diagnosed(files, config_root).files, get_roles(config_root))
    expected = round(sum(1 for value in ids if int(str(value)[5:]) <= 200) / len(ids), 4)
    assert link.coverage == expected
    assert link.fix is None and link.fixed_coverage is None and link.transform is None


def test_without_a_customer_list_there_are_no_links(config_root: Path) -> None:
    files = [loaded(bills(1), "bills_jan.csv", config_root, "u_1")]
    assert file_links(files, diagnosed(files, config_root).files, get_roles(config_root)) == ()


def test_two_monthly_files_with_the_same_columns_can_be_stacked(config_root: Path) -> None:
    files = [
        loaded(bills(2), "bills_feb.csv", config_root, "u_feb"),
        loaded(bills(1), "bills_jan.csv", config_root, "u_jan"),
        loaded(customers(), "customers.csv", config_root, "u_list"),
    ]
    (stack,) = file_stacks(files, diagnosed(files, config_root).files)
    assert stack.upload_ids == ("u_feb", "u_jan")  # file-name order
    assert stack.kind == "bills"
    assert stack.columns == ("customer_id", "invoice_date", "amount")
    assert stack.rows == 800
    assert stack.drift == ()
    assert "same columns" in stack.summary


def test_a_renamed_and_an_added_column_are_reported_as_drift(config_root: Path) -> None:
    march = bills(3).rename(columns={"amount": "bill_amount"})
    april = bills(4).assign(discount=0.0)
    files = [
        loaded(bills(1), "bills_01.csv", config_root, "u_1"),
        loaded(march, "bills_03.csv", config_root, "u_3"),
        loaded(april, "bills_04.csv", config_root, "u_4"),
    ]
    (stack,) = file_stacks(files, diagnosed(files, config_root).files)
    assert stack.upload_ids == ("u_1", "u_3", "u_4")
    assert stack.columns == ("customer_id", "invoice_date")
    drift = {item.upload_id: item for item in stack.drift}
    assert [(r.before, r.after) for r in drift["u_3"].renamed] == [("amount", "bill_amount")]
    assert drift["u_3"].added == () and drift["u_3"].missing == ()
    assert drift["u_4"].added == ("discount",) and not drift["u_4"].renamed
    assert "amount is called bill_amount" in stack.summary


def test_files_of_different_kinds_or_shapes_are_not_stacked(config_root: Path) -> None:
    files = [
        loaded(bills(1), "bills.csv", config_root, "u_b"),
        loaded(customers(), "customers.csv", config_root, "u_c"),
        loaded(pd.DataFrame({"topic": ["a", "b"], "owner": ["x", "y"]}), "notes.csv", config_root, "u_n"),
    ]
    assert file_stacks(files, diagnosed(files, config_root).files) == ()


def test_how_files_fit_fills_both_fields(config_root: Path) -> None:
    files = [
        loaded(customers(), "customers.csv", config_root, "u_list"),
        loaded(bills(1), "bills_jan.csv", config_root, "u_jan"),
        loaded(bills(2), "bills_feb.csv", config_root, "u_feb"),
    ]
    fitted = how_files_fit(diagnosed(files, config_root), files, get_roles(config_root))
    assert {link.upload_id for link in fitted.links} == {"u_jan", "u_feb"}
    assert all(link.coverage == 1.0 for link in fitted.links)
    assert [stack.upload_ids for stack in fitted.stacks] == [("u_feb", "u_jan")]


def test_ids_no_tidy_up_can_join_are_said_to_be_so_with_no_fix(config_root: Path) -> None:
    log = bills(4).assign(customer_id=[f"ACC{index:04d}X" for index in range(400)])
    files = [
        loaded(customers(), "customers.csv", config_root, "u_list"),
        loaded(log, "bills.csv", config_root, "u_b"),
    ]
    (link,) = file_links(files, diagnosed(files, config_root).files, get_roles(config_root))
    assert link.coverage == 0.0
    assert link.fix is None and link.fixed_coverage is None and link.transform is None
    assert "no tidy-up" in link.summary


def test_a_share_is_never_rounded_up_to_all(config_root: Path) -> None:
    ids: list[object] = [f"CUST-{1 + index % 200:05d}" for index in range(999)]
    ids.append("CUST-99999")
    files = [
        loaded(customers(), "customers.csv", config_root, "u_list"),
        loaded(bills(5, rows=1000, customer_ids=ids), "bills.csv", config_root, "u_b"),
    ]
    (link,) = file_links(files, diagnosed(files, config_root).files, get_roles(config_root))
    assert link.coverage == 0.999
    assert link.summary.startswith("over 99% of the rows of bills.csv")


def test_a_file_longer_than_the_rows_read_is_said_to_be_sampled(config_root: Path) -> None:
    files = [
        loaded(customers(), "customers.csv", config_root, "u_list"),
        loaded(bills(6), "bills.csv", config_root, "u_b"),
    ]
    diagnosis = diagnosed(files, config_root)
    longer = tuple(
        item.model_copy(update={"rows": item.rows * 2}) if item.upload_id == "u_b" else item
        for item in diagnosis.files
    )
    (link,) = file_links(files, longer, get_roles(config_root))
    assert link.sampled and "rows of bills.csv read" in link.summary
