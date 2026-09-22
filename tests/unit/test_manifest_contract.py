"""`RunManifest`'s Phase 2/3a/4a fields: present, optional, and null rather than zero when unknown.

The manifest is the one flat record per run, and the three branches each add something to it: the
dataset a run consumed, what it spent on language models, and where it ran. The shapes are settled
here so none of them has to change a field later - `engine/contracts.py` is append-only (§4), so
"later" would mean "never".
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from engine.contracts import (
    ComputeBackend,
    ComputeInfo,
    CostEstimate,
    DatasetFingerprint,
    LLMUsage,
    RunManifest,
    load_artefact,
)


def manifest(**changes: object) -> RunManifest:
    base: dict[str, object] = {
        "run_id": "20260922T101500Z-ab12",
        "primary_key": "customer_id",
        "dataset_fingerprint": DatasetFingerprint(hash="a" * 64, algorithm="sha256", n_rows=10, columns=()),
        "seed": 7,
        "duration_s": 1.5,
        "cost_estimate": CostEstimate(compute_seconds=1.5, basis="local run, nothing billed"),
        "created_at": datetime(2026, 9, 22, 10, 15, tzinfo=UTC),
    }
    return RunManifest.model_validate({**base, **changes})


def test_a_local_run_carries_none_of_the_optional_blocks() -> None:
    """Absent is not zero: a run that called no model has no usage, not a usage of nothing."""
    built = manifest()
    assert built.llm_usage is None
    assert built.compute is None
    assert built.dataset_id is None
    assert built.client_id is None


def test_the_manifest_round_trips_a_composite_key() -> None:
    built = manifest(primary_key=["account_id", "line_id"])
    restored = load_artefact("run_manifest.json", built.model_dump_json())
    assert restored == built


def test_the_manifest_round_trips_every_new_block() -> None:
    built = manifest(
        dataset_id="ds-1",
        client_id="client-1",
        llm_usage=LLMUsage(calls=2, input_tokens=100, output_tokens=40, model_ids=("a", "b")),
        compute=ComputeInfo(
            backend=ComputeBackend.SAGEMAKER,
            job_arn="arn:aws:sagemaker:REGION:ACCOUNT:training-job/NAME",
            instance_type="ml.m5.xlarge",
            duration_s=42.0,
            cost_estimate_usd=0.19,
        ),
    )
    restored = load_artefact("run_manifest.json", built.model_dump_json())
    assert restored == built


def test_usage_and_compute_report_no_cost_rather_than_zero() -> None:
    assert LLMUsage(calls=1, input_tokens=1, output_tokens=1).cost_estimate_usd is None
    assert ComputeInfo(backend=ComputeBackend.LOCAL, duration_s=1.0).cost_estimate_usd is None


def test_a_local_run_has_no_job_arn_and_no_instance_type() -> None:
    """Neither exists locally, and a placeholder would be indistinguishable from a real one."""
    local = ComputeInfo(backend=ComputeBackend.LOCAL, duration_s=1.0)
    assert local.job_arn is None
    assert local.instance_type is None


def test_the_manifest_still_forbids_an_unknown_key() -> None:
    with pytest.raises(ValueError, match="extra_forbidden"):
        manifest(spent_usd=3)
