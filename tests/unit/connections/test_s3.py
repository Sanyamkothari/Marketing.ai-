"""The S3 connector against moto (Plan H M80): test steps, browse, preview, fetch and their limits."""

from __future__ import annotations

import io
from collections.abc import Iterator

import boto3
import pandas as pd
import pytest
from moto import mock_aws

from engine.connections.base import PREVIEW_MAX_BYTES, ConnectorError, Selection, StepName, StepStatus
from engine.connections.s3 import S3Connector, _principal_arn

BUCKET = "acme-exports"
REGION = "ap-south-1"
CSV = "customer_id,email,spend\n1,alice@example.com,10\n2,bob@example.com,20\n3,carol@example.com,30\n"


@pytest.fixture
def s3(monkeypatch: pytest.MonkeyPatch) -> Iterator[object]:
    for name in ("AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "AWS_SESSION_TOKEN", "AWS_PROFILE"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")
    with mock_aws():
        client = boto3.client("s3", region_name=REGION)
        client.create_bucket(Bucket=BUCKET, CreateBucketConfiguration={"LocationConstraint": REGION})
        client.put_object(Bucket=BUCKET, Key="exports/customers.csv", Body=CSV.encode())
        client.put_object(Bucket=BUCKET, Key="exports/2026/orders.csv", Body=b"id,total\n1,5\n")
        client.put_object(Bucket=BUCKET, Key="exports/readme.pdf", Body=b"%PDF")
        yield client


CONFIG = {"bucket": BUCKET}
KEYS = {"access_key_id": "testing", "secret_access_key": "testing"}


def statuses(report: object) -> dict[str, str]:
    return {s.name.value: s.status.value for s in report.steps}  # type: ignore[attr-defined]


def test_a_reachable_bucket_passes_every_step_and_finds_its_region(s3: object) -> None:
    report = S3Connector().test(CONFIG, KEYS)
    assert report.ok
    got = statuses(report)
    assert [s.name for s in report.steps] == list(StepName)
    assert got["reach"] == got["sign_in"] == got["list"] == got["read_sample"] == "ok"
    listed = next(s for s in report.steps if s.name is StepName.LIST)
    assert "Found 2 CSV or Parquet files" in listed.message
    # moto has no IAM policy simulator and the bucket is private: the check says it cannot tell.
    assert got["read_only_check"] == "skipped"


def test_blank_keys_use_the_machines_own_sign_in(s3: object) -> None:
    assert S3Connector().test(CONFIG, {}).ok


def test_a_missing_bucket_fails_the_list_step_with_a_plain_fix(s3: object) -> None:
    report = S3Connector().test({"bucket": "no-such-bucket", "region": REGION}, KEYS)
    assert not report.ok
    got = statuses(report)
    assert got["reach"] == "ok" and got["sign_in"] == "ok"
    listed = next(s for s in report.steps if s.name is StepName.LIST)
    assert listed.status is StepStatus.FAILED
    assert listed.message == "There is no bucket called no-such-bucket."
    assert listed.fix and "spelling" in listed.fix
    assert got["read_sample"] == got["read_only_check"] == "skipped"


def test_a_bucket_anyone_can_write_to_is_a_warning(s3: object) -> None:
    owner = s3.get_bucket_acl(Bucket=BUCKET)["Owner"]  # type: ignore[attr-defined]
    s3.put_bucket_acl(  # type: ignore[attr-defined]
        Bucket=BUCKET,
        AccessControlPolicy={
            "Owner": owner,
            "Grants": [
                {
                    "Grantee": {"Type": "Group", "URI": "http://acs.amazonaws.com/groups/global/AllUsers"},
                    "Permission": "WRITE",
                }
            ],
        },
    )
    report = S3Connector().test(CONFIG, KEYS)
    check = next(s for s in report.steps if s.name is StepName.READ_ONLY_CHECK)
    assert check.status is StepStatus.WARNING
    assert "Anyone" in check.message
    assert report.ok, "a caution is not a failure"


@pytest.mark.parametrize(("can_write", "status"), [(True, StepStatus.WARNING), (False, StepStatus.OK)])
def test_the_iam_verdict_decides_the_read_only_check(
    s3: object, monkeypatch: pytest.MonkeyPatch, can_write: bool, status: StepStatus
) -> None:
    monkeypatch.setattr(S3Connector, "_simulate_writes", lambda *_a, **_k: can_write)
    report = S3Connector().test(CONFIG, KEYS)
    check = next(s for s in report.steps if s.name is StepName.READ_ONLY_CHECK)
    assert check.status is status
    if can_write:
        assert check.fix and "only read" in check.fix


def test_only_one_key_is_a_sign_in_failure(s3: object) -> None:
    report = S3Connector().test(CONFIG, {"access_key_id": "testing"})
    sign_in = next(s for s in report.steps if s.name is StepName.SIGN_IN)
    assert sign_in.status is StepStatus.FAILED and "both" in (sign_in.fix or "")


def test_browse_lists_folders_and_files_and_marks_what_can_be_imported(s3: object) -> None:
    top = S3Connector().browse({**CONFIG, "prefix": "exports/"}, KEYS, "")
    assert top.path == "exports/" and top.parent is None
    by_name = {i.name: i for i in top.items}
    assert by_name["2026"].kind == "folder" and by_name["2026"].path == "exports/2026/"
    assert by_name["customers.csv"].importable and by_name["customers.csv"].size_bytes == len(CSV)
    assert not by_name["readme.pdf"].importable
    inner = S3Connector().browse({**CONFIG, "prefix": "exports/"}, KEYS, "exports/2026/")
    assert inner.parent == "exports/" and [i.name for i in inner.items] == ["orders.csv"]


def test_a_path_outside_the_connections_folder_is_refused(s3: object) -> None:
    with pytest.raises(ConnectorError) as caught:
        S3Connector().browse({**CONFIG, "prefix": "exports/"}, KEYS, "private/")
    assert caught.value.code == "CONNECTION_OUTSIDE_FOLDER"


def test_preview_is_at_most_twenty_rows_with_personal_data_masked(s3: object) -> None:
    preview = S3Connector().preview(CONFIG, KEYS, Selection(path="exports/customers.csv"))
    assert preview.columns == ("customer_id", "email", "spend")
    assert len(preview.rows) == 3
    assert preview.rows[0][1] == "[REDACTED:email]"
    assert "alice@example.com" not in repr(preview)


def test_a_large_csv_preview_reads_only_its_start_and_never_a_cut_line(s3: object) -> None:
    rows = "\n".join(f"{i},name{i},{'x' * 50}" for i in range(20_000))
    s3.put_object(Bucket=BUCKET, Key="big.csv", Body=("id,name,pad\n" + rows).encode())  # type: ignore[attr-defined]
    assert len(rows) > PREVIEW_MAX_BYTES
    preview = S3Connector().preview(CONFIG, KEYS, Selection(path="big.csv"))
    assert len(preview.rows) == 20 and preview.rows[-1][0] == "19"


def test_a_parquet_preview_and_fetch(s3: object) -> None:
    buffer = io.BytesIO()
    pd.DataFrame({"id": [1, 2], "phone": ["+91 98765 43210", "n/a"]}).to_parquet(buffer, index=False)
    s3.put_object(Bucket=BUCKET, Key="t.parquet", Body=buffer.getvalue())  # type: ignore[attr-defined]
    preview = S3Connector().preview(CONFIG, KEYS, Selection(path="t.parquet"))
    assert preview.rows[0] == ("1", "[REDACTED:phone]")
    sink = io.BytesIO()
    fetched = S3Connector().fetch(
        CONFIG, KEYS, Selection(path="t.parquet"), sink, limit_bytes=10**6, max_rows=10
    )
    assert fetched.file_format == "parquet" and sink.getvalue() == buffer.getvalue()


def test_fetch_streams_the_file_and_refuses_one_over_the_limit(s3: object) -> None:
    sink = io.BytesIO()
    fetched = S3Connector().fetch(
        CONFIG, KEYS, Selection(path="exports/customers.csv"), sink, limit_bytes=10**6, max_rows=10
    )
    assert fetched.file_name == "customers.csv" and fetched.size_bytes == len(CSV)
    assert sink.getvalue().decode() == CSV
    with pytest.raises(ConnectorError) as caught:
        S3Connector().fetch(
            CONFIG, KEYS, Selection(path="exports/customers.csv"), io.BytesIO(), limit_bytes=10, max_rows=10
        )
    assert caught.value.code == "CONNECTION_TOO_LARGE" and caught.value.status == 413


def test_a_file_that_is_not_a_table_cannot_be_picked(s3: object) -> None:
    with pytest.raises(ConnectorError) as caught:
        S3Connector().preview(CONFIG, KEYS, Selection(path="exports/readme.pdf"))
    assert caught.value.code == "CONNECTION_NOT_A_TABLE_FILE"


def test_s3_compatible_needs_an_endpoint_and_keys_and_refuses_link_local() -> None:
    compatible = S3Connector(compatible=True)
    info = compatible.info()
    assert [f.name for f in info.fields if f.required] == [
        "endpoint_url",
        "bucket",
        "access_key_id",
        "secret_access_key",
    ]
    report = compatible.test({"bucket": "b", "endpoint_url": "https://minio.example.com"}, {})
    sign_in = next(s for s in report.steps if s.name is StepName.SIGN_IN)
    assert sign_in.status is StepStatus.FAILED and "HMAC" in (sign_in.fix or "")
    metadata = compatible.test({"bucket": "b", "endpoint_url": "http://169.254.169.254"}, KEYS)
    reach = next(s for s in metadata.steps if s.name is StepName.REACH)
    assert reach.status is StepStatus.FAILED and "belongs to the server itself" in reach.message
    bad = compatible.test({"bucket": "b", "endpoint_url": "ftp://x"}, KEYS)
    assert next(s for s in bad.steps if s.name is StepName.REACH).status is StepStatus.FAILED


def test_the_caller_arn_becomes_the_principal_to_simulate() -> None:
    assert _principal_arn("arn:aws:sts::123456789012:assumed-role/Reader/sess") == (
        "arn:aws:iam::123456789012:role/Reader"
    )
    assert _principal_arn("arn:aws:iam::123456789012:user/ana") == "arn:aws:iam::123456789012:user/ana"
    assert _principal_arn("arn:aws:iam::123456789012:root") is None
    assert _principal_arn("nonsense") is None
