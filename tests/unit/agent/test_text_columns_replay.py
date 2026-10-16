"""`0000123` stays `0000123` through a recipe's replay (Plan O M240, DEC-1810 (c); review fix).

Reading a column as text only when a caller remembers to ask is not enough: the file is read again at every
replay. The recipe therefore records the columns it was approved with as text (`DataRecipe.text_columns`,
from `ReadResult.text_columns`) and the real replay entry points - `write_derived_upload` for a scoring file
and `prepare_in_memory` for the dry run - read them the same way. A recipe that recorded none reads the file
as it always did.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from api.routes.agent_recipes import prepare_in_memory, write_derived_upload
from api.schemas import UploadRecord
from engine.agent.config import AgentLevel
from engine.agent.contracts import DataRecipe, RecipeStep, RecipeStepKind, recipe_hash
from engine.agent.dataset_recipe import replay_identity
from engine.config import RunMode, load_use_case
from engine.stages.ingest import read_upload
from engine.storage import LocalStorage, upload_key
from engine.utils.time import utc_now

USE_CASE = "targeted-advertisement"
MONTH_2 = "customer_id,plan,spend\n" '0000125,Gold,"₹2,000"\n' "0000199,Silver,15\n" "0000123,Gold,99\n"
STEPS = (
    RecipeStep(
        order=1,
        kind=RecipeStepKind.PARSE_NUMBER,
        column="spend",
        params={"decimal": ".", "percent_to_fraction": True},
    ),
)


@pytest.fixture
def storage(tmp_path: Path) -> LocalStorage:
    return LocalStorage(tmp_path / "store")


def recipe(*, text_columns: tuple[str, ...] = ()) -> DataRecipe:
    return DataRecipe(
        recipe_id="rc-1",
        use_case_id=USE_CASE,
        source_fingerprint="sha256:v1:test",
        steps=STEPS,
        recipe_hash=recipe_hash(STEPS),
        primary_key="customer_id",
        levels=(AgentLevel.CLEAN,),
        max_failure_pct=5.0,
        text_columns=text_columns,
        created_at=utc_now(),
    )


def upload(storage: LocalStorage, upload_id: str = "u2") -> UploadRecord:
    key = upload_key(upload_id, "source.csv")
    storage.write_bytes(key, MONTH_2.encode())
    return UploadRecord(
        upload_id=upload_id,
        use_case_id=USE_CASE,
        mode=RunMode.SCORE,
        file_name="month2.csv",
        file_format="csv",
        file_size_bytes=len(MONTH_2),
        delimiter=",",
        encoding="utf-8",
        row_count=3,
        column_count=3,
        source_key=key,
        profile_key=upload_key(upload_id, "profile.json"),
        fingerprint_key=upload_key(upload_id, "fingerprint.json"),
        fingerprint_hash="sha256:v1:test",
        created_at=utc_now(),
    )


def test_a_recipe_that_recorded_no_text_columns_replays_exactly_as_it_always_did(
    storage: LocalStorage,
) -> None:
    source = upload(storage)
    derived = write_derived_upload(storage, load_use_case(USE_CASE), source, recipe())
    frame = read_upload(storage, derived.record.source_key).frame
    assert frame["customer_id"].tolist() == [125, 199, 123]  # the zeros are gone: the unchanged default


def test_the_replay_of_a_scoring_file_reads_the_recorded_columns_as_text(storage: LocalStorage) -> None:
    source = upload(storage)
    derived = write_derived_upload(
        storage, load_use_case(USE_CASE), source, recipe(text_columns=("customer_id",))
    )
    frame = read_upload(storage, derived.record.source_key).frame
    assert frame["customer_id"].tolist() == ["0000125", "0000199", "0000123"]
    assert frame["spend"].tolist() == pytest.approx([2000.0, 15.0, 99.0])  # the recipe still ran


def test_the_dry_run_reads_the_file_the_same_way(storage: LocalStorage) -> None:
    source = upload(storage)
    prepared = prepare_in_memory(
        storage, load_use_case(USE_CASE), source, recipe(text_columns=("customer_id",))
    )
    assert isinstance(prepared, tuple)
    frame, rows = prepared
    assert rows == 3 and frame["customer_id"].tolist() == ["0000125", "0000199", "0000123"]


def test_the_columns_a_read_used_as_text_are_what_the_approval_records_and_the_replay_applies(
    storage: LocalStorage,
) -> None:
    """Approval: read the training file with `keep_leading_zeros`; record `ReadResult.text_columns`; replay."""
    source = upload(storage, "train")
    approval_read = read_upload(storage, source.source_key, keep_leading_zeros=True)
    assert approval_read.text_columns == ("customer_id",)
    approved = recipe(text_columns=approval_read.text_columns)
    scoring = upload(storage, "u2")
    derived = write_derived_upload(storage, load_use_case(USE_CASE), scoring, approved)
    replayed = read_upload(storage, derived.record.source_key).frame
    training = read_upload(storage, source.source_key, keep_leading_zeros=True).frame
    joined = training.merge(replayed, on="customer_id")
    assert sorted(joined["customer_id"]) == ["0000123", "0000125", "0000199"]  # the two files still join


def test_a_read_with_no_options_records_no_text_columns(storage: LocalStorage) -> None:
    source = upload(storage)
    assert read_upload(storage, source.source_key).text_columns == ()
    assert read_upload(storage, source.source_key, text_columns=("plan",)).text_columns == ("plan",)


def test_text_columns_are_part_of_what_a_replay_depends_on() -> None:
    config = load_use_case(USE_CASE)
    plain, text = recipe(), recipe(text_columns=("customer_id",))
    assert not plain.prepares_like(text) and plain.prepares_like(recipe())
    assert replay_identity(plain, config) != replay_identity(text, config)
    # A recipe that recorded none keeps the identity it had before the field existed.
    assert replay_identity(plain, config) == replay_identity(recipe(), config)


def test_an_old_recipe_file_without_the_field_still_loads() -> None:
    document = recipe().model_dump(mode="json")
    del document["text_columns"]
    assert DataRecipe.model_validate(document).text_columns == ()


def test_preparing_the_same_data_again_reuses_the_prepared_file(storage: LocalStorage) -> None:
    source = upload(storage)
    config = load_use_case(USE_CASE)
    first = write_derived_upload(storage, config, source, recipe())
    again = write_derived_upload(storage, config, source, recipe())
    assert again.record.upload_id == first.record.upload_id
    assert len([k for k in storage.list_keys("uploads/") if k.endswith("/upload.json")]) == 1
