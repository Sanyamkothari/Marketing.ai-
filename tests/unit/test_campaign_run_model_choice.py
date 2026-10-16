"""A campaign run on next month's file uses the model just learned when the current champion cannot read that file."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from api.routes.campaign_runs import _model_for_file
from engine.campaign_run import StepError
from engine.config import ProblemType
from engine.contracts import ColumnType, FeatureSchema, FeatureSchemaColumn, ModelStatus
from engine.storage import LocalStorage
from engine.utils.time import utc_now


def _store(storage: LocalStorage, key: str, *names: str, status: ModelStatus = ModelStatus.CANDIDATE) -> Any:
    schema = FeatureSchema(
        use_case_id="targeted-advertisement",
        model_version_id="m",
        primary_key="customer_id",
        target="converted_30d",
        problem_type=ProblemType.BINARY_CLASSIFICATION,
        columns=tuple(FeatureSchemaColumn(name=n, inferred_type=ColumnType.FLOAT) for n in names),
        row_count_at_fit=100,
        created_at=utc_now(),
    )
    storage.write_model(key, schema)
    return SimpleNamespace(schema_key=key, model_id=key[0], status=status, promoted_at=None)


def test_the_champion_stays_unless_a_column_it_needs_is_missing_from_the_file(tmp_path: Path) -> None:
    storage = LocalStorage(tmp_path)
    champion = _store(storage, "a/schema.json", "visits", "ad_ctr_90d", status=ModelStatus.CHAMPION)
    same = _store(storage, "b/schema.json", "visits", "ad_ctr_90d", "plan")
    renamed = _store(storage, "c/schema.json", "visits", "ad_click_rate_90d")
    assert _model_for_file(storage, champion, same, approval_required=True) == "a"
    assert (
        _model_for_file(storage, champion, renamed, approval_required=False) == "c"
    )  # nobody asked for approval
    assert (
        _model_for_file(
            storage, SimpleNamespace(schema_key="gone.json", model_id="g"), renamed, approval_required=True
        )
        == "g"
    )


def test_an_unapproved_new_model_is_not_handed_to_the_list_while_approval_is_required(tmp_path: Path) -> None:
    storage = LocalStorage(tmp_path)
    champion = _store(storage, "a/schema.json", "visits", "ad_ctr_90d", status=ModelStatus.CHAMPION)
    renamed = _store(
        storage, "c/schema.json", "visits", "ad_click_rate_90d"
    )  # a candidate nobody has approved
    with pytest.raises(StepError) as stopped:
        _model_for_file(storage, champion, renamed, approval_required=True)
    assert stopped.value.code == "MODEL_NOT_APPROVED" and "'ad_ctr_90d'" in stopped.value.message
    approved = _store(storage, "e/schema.json", "visits", status=ModelStatus.CHAMPION)
    assert _model_for_file(storage, champion, approved, approval_required=True) == "e"
