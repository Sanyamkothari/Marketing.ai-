"""`S3Storage`: everything that is true of a bucket and of nothing else.

The backend-agnostic half of this store's behaviour is in `tests/unit/test_storage_contract.py`,
run against both implementations. What is left here is the part that has no local counterpart: how
a storage key becomes an object key, what a write is tagged and encrypted with, what a pre-signed
URL carries, and - the reason this file is as long as it is - exactly which `StorageError` each AWS
failure becomes.

The error table is built from **constructed** `botocore.exceptions.ClientError`s rather than from
errors provoked out of moto. That is a deliberate choice with a measurement behind it: moto 5.2.3
raises a bare `KeyError` for `complete_multipart_upload` with an unknown upload id, so "provoke the
failure and see what happens" would be testing moto's fidelity rather than this module's mapping.
A constructed error is the wire shape botocore itself produces, which is the thing being mapped.
"""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any

import pytest

from engine.aws.s3_storage import DIGEST_METADATA_KEY, MAX_S3_KEY_BYTES, S3Storage
from engine.storage import Storage, StorageError, SupportsPresignedDownload, presigned_download_url

BUCKET = "marketing-ai-unit"
REGION = "us-east-1"


@pytest.fixture
def aws_credentials(monkeypatch: pytest.MonkeyPatch) -> None:
    """Credentials moto accepts and a real account would refuse."""
    for name, value in (
        ("AWS_ACCESS_KEY_ID", "testing"),
        ("AWS_SECRET_ACCESS_KEY", "testing"),
        ("AWS_SECURITY_TOKEN", "testing"),
        ("AWS_SESSION_TOKEN", "testing"),
        ("AWS_DEFAULT_REGION", REGION),
    ):
        monkeypatch.setenv(name, value)


@pytest.fixture
def s3(aws_credentials: None) -> Any:
    """A moto-backed client with the bucket already created."""
    moto = pytest.importorskip("moto", reason="S3Storage's happy paths are tested against moto")
    import boto3

    with moto.mock_aws():
        client = boto3.client("s3", region_name=REGION)
        client.create_bucket(Bucket=BUCKET)
        yield client


def store(client: Any, tmp_path: Path, **kwargs: Any) -> S3Storage:
    """An `S3Storage` over `client`, with a mirror of its own."""
    kwargs.setdefault("workspace", tmp_path / "mirror")
    return S3Storage(BUCKET, client=client, **kwargs)


# ---------------------------------------------------------------------------
# Keys: what the bucket actually holds
# ---------------------------------------------------------------------------
def test_a_key_becomes_prefix_slash_key_and_comes_back_relative(s3: Any, tmp_path: Path) -> None:
    subject = store(s3, tmp_path, prefix="artefacts")
    subject.write_text("runs/r1/run.json", "{}")

    assert [item["Key"] for item in s3.list_objects_v2(Bucket=BUCKET)["Contents"]] == [
        "artefacts/runs/r1/run.json"
    ]
    assert subject.list_keys() == ("runs/r1/run.json",)
    assert subject.read_text("runs/r1/run.json") == "{}"


def test_an_empty_prefix_puts_keys_at_the_bucket_root(s3: Any, tmp_path: Path) -> None:
    """What `api/deps.py` passes for a bucket-per-environment deployment."""
    subject = store(s3, tmp_path, prefix="")
    subject.write_text("runs/r1/run.json", "{}")

    assert [item["Key"] for item in s3.list_objects_v2(Bucket=BUCKET)["Contents"]] == ["runs/r1/run.json"]
    assert subject.list_keys() == ("runs/r1/run.json",)


def test_a_neighbouring_prefix_is_not_this_store(s3: Any, tmp_path: Path) -> None:
    """`artefacts` and `artefacts-old` share seven characters and nothing else."""
    store(s3, tmp_path, prefix="artefacts").write_text("runs/r1/mine.json", "{}")
    S3Storage(BUCKET, prefix="artefacts-old", client=s3, workspace=tmp_path / "other").write_text(
        "runs/r1/theirs.json", "{}"
    )

    assert store(s3, tmp_path, prefix="artefacts").list_keys() == ("runs/r1/mine.json",)


def test_directory_markers_are_not_keys(s3: Any, tmp_path: Path) -> None:
    """A console or a sync tool leaves zero-byte "folders" behind; they are not artefacts."""
    subject = store(s3, tmp_path, prefix="artefacts")
    subject.write_text("runs/r1/run.json", "{}")
    s3.put_object(Bucket=BUCKET, Key="artefacts/runs/r1/", Body=b"")

    assert subject.list_keys() == ("runs/r1/run.json",)


def test_list_keys_pages_through_every_object(s3: Any, tmp_path: Path) -> None:
    subject = store(s3, tmp_path, prefix="artefacts", page_size=2)
    for index in range(7):
        subject.write_text(f"runs/r1/part-{index}.json", "{}")

    assert subject.list_keys() == tuple(f"runs/r1/part-{index}.json" for index in range(7))


def test_a_key_that_is_valid_in_characters_but_too_long_in_bytes_is_refused(s3: Any, tmp_path: Path) -> None:
    """512 characters and 1024 bytes are different limits, and a CJK character is three bytes."""
    key = "runs/r1/" + "\u65e5" * 400
    assert len(key) < 512
    assert len(key.encode()) > MAX_S3_KEY_BYTES

    with pytest.raises(StorageError) as excinfo:
        store(s3, tmp_path, prefix="artefacts").write_text(key, "{}")
    assert excinfo.value.code == "KEY_INVALID"


def test_the_prefix_counts_towards_the_byte_limit(s3: Any, tmp_path: Path) -> None:
    key = "runs/r1/" + "x" * 500
    long_prefix = "p" * 600
    store(s3, tmp_path, prefix="short").write_text(key, "{}")

    with pytest.raises(StorageError) as excinfo:
        S3Storage(BUCKET, prefix=long_prefix, client=s3, workspace=tmp_path / "far").write_text(key, "{}")
    assert excinfo.value.code == "KEY_INVALID"


def test_hydrating_one_run_does_not_drag_down_its_neighbour(s3: Any, tmp_path: Path) -> None:
    """`runs/r1` is a prefix of `runs/r10`; only the directory boundary tells them apart."""
    subject = store(s3, tmp_path, prefix="artefacts")
    subject.write_bytes("runs/r1/a.bin", b"mine")
    subject.write_bytes("runs/r10/b.bin", b"theirs")

    mirrored = S3Storage(BUCKET, prefix="artefacts", client=s3, workspace=tmp_path / "second")
    root = mirrored.local_path("runs/r1")

    assert sorted(path.name for path in root.rglob("*")) == ["a.bin"]


# ---------------------------------------------------------------------------
# Tagging and encryption
# ---------------------------------------------------------------------------
def test_a_run_artefact_is_tagged_with_its_run_id_and_the_client(s3: Any, tmp_path: Path) -> None:
    """The run id is read out of the key, because `Storage` has no parameter to pass it in."""
    subject = store(s3, tmp_path, prefix="artefacts", client_id="acme")
    subject.write_text("runs/r_20260921_abcdef01/run.json", "{}")

    tags = s3.get_object_tagging(Bucket=BUCKET, Key="artefacts/runs/r_20260921_abcdef01/run.json")
    assert {tag["Key"]: tag["Value"] for tag in tags["TagSet"]} == {
        "run_id": "r_20260921_abcdef01",
        "client_id": "acme",
    }


def test_a_key_outside_runs_carries_the_client_tag_only(s3: Any, tmp_path: Path) -> None:
    subject = store(s3, tmp_path, prefix="artefacts", client_id="acme")
    subject.write_text("models/uc/1/model/scorer.json", "{}")

    tags = s3.get_object_tagging(Bucket=BUCKET, Key="artefacts/models/uc/1/model/scorer.json")
    assert {tag["Key"]: tag["Value"] for tag in tags["TagSet"]} == {"client_id": "acme"}


def test_a_deployment_that_serves_nobody_in_particular_tags_nothing(s3: Any, tmp_path: Path) -> None:
    store(s3, tmp_path, prefix="artefacts").write_text("uploads/u1/f.csv", "x")

    assert s3.get_object_tagging(Bucket=BUCKET, Key="artefacts/uploads/u1/f.csv")["TagSet"] == []


def test_sse_kms_is_requested_only_when_a_key_id_is_configured(tmp_path: Path) -> None:
    """Asserted on the request, not on the response: what matters is what this module asks for."""
    plain = _Recorder()
    store(plain, tmp_path, prefix="artefacts").write_text("runs/r1/a.json", "{}")
    ((_, plain_request),) = plain.calls

    encrypted = _Recorder()
    store(
        encrypted, tmp_path / "kms", prefix="artefacts", kms_key_id="arn:aws:kms:eu-west-1:1:key/abc"
    ).write_text("runs/r1/a.json", "{}")
    ((_, encrypted_request),) = encrypted.calls

    assert "ServerSideEncryption" not in plain_request
    assert "SSEKMSKeyId" not in plain_request
    assert encrypted_request["ServerSideEncryption"] == "aws:kms"
    assert encrypted_request["SSEKMSKeyId"] == "arn:aws:kms:eu-west-1:1:key/abc"
    assert encrypted_request["Tagging"] == "run_id=r1"


def test_every_single_put_carries_a_digest_so_a_publish_can_diff_it(s3: Any, tmp_path: Path) -> None:
    subject = store(s3, tmp_path, prefix="artefacts")
    subject.write_bytes("runs/r1/a.bin", b"payload")

    head = s3.head_object(Bucket=BUCKET, Key="artefacts/runs/r1/a.bin")
    assert head["Metadata"][DIGEST_METADATA_KEY] == hashlib.sha256(b"payload").hexdigest()


def test_a_publish_re_uploads_only_what_changed(s3: Any, tmp_path: Path) -> None:
    subject = store(s3, tmp_path, prefix="artefacts")
    root = subject.local_path("models/uc/1/model")
    root.mkdir(parents=True, exist_ok=True)
    (root / "a.bin").write_bytes(b"one")
    (root / "b.bin").write_bytes(b"two")

    assert subject.publish_local_path("models/uc/1/model") == (
        "models/uc/1/model/a.bin",
        "models/uc/1/model/b.bin",
    )
    assert subject.publish_local_path("models/uc/1/model") == ()

    (root / "b.bin").write_bytes(b"three")
    assert subject.publish_local_path("models/uc/1/model") == ("models/uc/1/model/b.bin",)
    assert subject.read_bytes("models/uc/1/model/b.bin") == b"three"


def test_a_publish_diffs_against_the_bucket_and_not_only_against_this_instance(
    s3: Any, tmp_path: Path
) -> None:
    """A fresh process must not re-upload a predictor tree a previous one already published."""
    first = store(s3, tmp_path, prefix="artefacts")
    root = first.local_path("models/uc/1/model")
    root.mkdir(parents=True, exist_ok=True)
    (root / "a.bin").write_bytes(b"one")
    assert first.publish_local_path("models/uc/1/model") == ("models/uc/1/model/a.bin",)

    second = S3Storage(BUCKET, prefix="artefacts", client=s3, workspace=tmp_path / "mirror")
    assert second.publish_local_path("models/uc/1/model") == ()


# ---------------------------------------------------------------------------
# Pre-signed downloads
# ---------------------------------------------------------------------------
def test_a_pre_signed_url_names_the_object_and_the_caller_s_lifetime(s3: Any, tmp_path: Path) -> None:
    """The TTL belongs to `engine/settings.py`; this module deliberately has no default of its own."""
    subject = store(s3, tmp_path, prefix="artefacts")
    subject.write_text("runs/r1/scores.csv", "id\n1\n")

    short = subject.presigned_download_url("runs/r1/scores.csv", expires_in=120)
    long = subject.presigned_download_url("runs/r1/scores.csv", expires_in=3600)

    assert isinstance(subject, SupportsPresignedDownload)
    assert "artefacts/runs/r1/scores.csv" in short
    assert "response-content-disposition" not in short
    # Asserted as a difference rather than against a literal query parameter, because the spelling
    # of the expiry is the signature version's business and this module does not pick one.
    assert short != long, "the caller's lifetime has to reach the signature"


def test_a_filename_becomes_a_content_disposition_the_header_can_hold(s3: Any, tmp_path: Path) -> None:
    subject = store(s3, tmp_path, prefix="artefacts")
    subject.write_text("runs/r1/scores.csv", "id\n1\n")

    url = subject.presigned_download_url(
        "runs/r1/scores.csv", expires_in=900, filename='scores "2026".csv\r\nX-Injected: 1'
    )

    assert "response-content-disposition=attachment" in url
    assert "%0D%0A" not in url and "%0A" not in url, "a newline in a filename must not reach a header"


def test_the_free_dispatcher_finds_the_capability(s3: Any, tmp_path: Path) -> None:
    subject: Storage = store(s3, tmp_path, prefix="artefacts")
    subject.write_text("runs/r1/scores.csv", "id\n1\n")

    url = presigned_download_url(subject, "runs/r1/scores.csv", expires_in=900)

    assert url is not None and "artefacts/runs/r1/scores.csv" in url


def test_a_lifetime_of_zero_is_refused_rather_than_signed(s3: Any, tmp_path: Path) -> None:
    with pytest.raises(StorageError) as excinfo:
        store(s3, tmp_path, prefix="artefacts").presigned_download_url("runs/r1/a.csv", expires_in=0)
    assert excinfo.value.code == "READ_FAILED"


# ---------------------------------------------------------------------------
# The error table
# ---------------------------------------------------------------------------
def client_error(code: str, status: int, operation: str) -> Exception:
    """A `ClientError` in the shape botocore builds one, without provoking it out of moto."""
    from botocore.exceptions import ClientError

    return ClientError(
        {
            "Error": {"Code": code, "Message": "a message naming the key, which must never be logged"},
            "ResponseMetadata": {"HTTPStatusCode": status},
        },
        operation,
    )


@pytest.mark.parametrize(
    ("code", "status", "expected"),
    [
        ("NoSuchKey", 404, "KEY_NOT_FOUND"),
        ("NotFound", 404, "KEY_NOT_FOUND"),
        ("404", 404, "KEY_NOT_FOUND"),
        ("", 404, "KEY_NOT_FOUND"),
        ("NoSuchBucket", 404, "READ_FAILED"),
        ("AccessDenied", 403, "READ_FAILED"),
        ("", 403, "READ_FAILED"),
        ("KMS.AccessDeniedException", 400, "READ_FAILED"),
        ("InvalidAccessKeyId", 403, "READ_FAILED"),
        ("SlowDown", 503, "READ_FAILED"),
    ],
)
def test_a_failed_read_maps_to_the_right_code(tmp_path: Path, code: str, status: int, expected: str) -> None:
    subject = store(_Raiser(client_error(code, status, "GetObject")), tmp_path, prefix="artefacts")

    with pytest.raises(StorageError) as excinfo:
        subject.read_bytes("runs/r1/a.json")
    assert excinfo.value.code == expected
    assert excinfo.value.key == "runs/r1/a.json"


@pytest.mark.parametrize(
    ("code", "status", "expected"),
    [
        ("AccessDenied", 403, "WRITE_FAILED"),
        ("NoSuchBucket", 404, "WRITE_FAILED"),
        ("NoSuchUpload", 404, "WRITE_FAILED"),
        ("KMS.AccessDeniedException", 400, "WRITE_FAILED"),
        ("RequestTimeout", 400, "WRITE_FAILED"),
    ],
)
def test_a_failed_write_maps_to_write_failed(tmp_path: Path, code: str, status: int, expected: str) -> None:
    subject = store(_Raiser(client_error(code, status, "PutObject")), tmp_path, prefix="artefacts")

    with pytest.raises(StorageError) as excinfo:
        subject.write_text("runs/r1/a.json", "{}")
    assert excinfo.value.code == expected


def test_a_missing_bucket_is_never_reported_as_a_missing_artefact(tmp_path: Path) -> None:
    """A 404 that says `NoSuchBucket` is configuration; `KEY_NOT_FOUND` would misdirect the reader."""
    subject = store(_Raiser(client_error("NoSuchBucket", 404, "GetObject")), tmp_path, prefix="artefacts")

    with pytest.raises(StorageError) as excinfo:
        subject.read_bytes("runs/r1/a.json")
    assert excinfo.value.code == "READ_FAILED"
    assert "bucket" in excinfo.value.message


def test_no_mapped_error_quotes_the_aws_message(tmp_path: Path) -> None:
    """Plan section 13.7: the class and the AWS code are diagnostics, the message is data."""
    subject = store(_Raiser(client_error("AccessDenied", 403, "GetObject")), tmp_path, prefix="a")

    with pytest.raises(StorageError) as excinfo:
        subject.read_bytes("runs/r1/a.json")
    assert "which must never be logged" not in excinfo.value.message
    assert "AccessDenied" in excinfo.value.message


@pytest.mark.parametrize(("code", "status"), [("404", 404), ("NoSuchKey", 404), ("", 404)])
def test_exists_is_false_on_a_404(tmp_path: Path, code: str, status: int) -> None:
    subject = store(_Raiser(client_error(code, status, "HeadObject")), tmp_path, prefix="artefacts")

    assert subject.exists("runs/r1/a.json") is False


@pytest.mark.parametrize(("code", "status"), [("AccessDenied", 403), ("403", 403), ("", 403)])
def test_exists_raises_on_a_403(tmp_path: Path, code: str, status: int) -> None:
    """Without `s3:ListBucket` a missing object answers 403.

    Answering `False` there would turn "you may not look" into "there is nothing to see" - a lie
    the caller cannot detect, and a permissions bug that hides until somebody counts the artefacts.
    """
    subject = store(_Raiser(client_error(code, status, "HeadObject")), tmp_path, prefix="artefacts")

    with pytest.raises(StorageError) as excinfo:
        subject.exists("runs/r1/a.json")
    assert excinfo.value.code == "READ_FAILED"


def test_size_bytes_of_a_forbidden_key_raises_rather_than_reporting_absence(tmp_path: Path) -> None:
    subject = store(_Raiser(client_error("AccessDenied", 403, "HeadObject")), tmp_path, prefix="a")

    with pytest.raises(StorageError) as excinfo:
        subject.size_bytes("runs/r1/a.json")
    assert excinfo.value.code == "READ_FAILED"


# ---------------------------------------------------------------------------
# Construction
# ---------------------------------------------------------------------------
def test_build_storage_builds_this_class_from_settings(tmp_path: Path) -> None:
    """`engine/settings.py` names this constructor; the two must agree or nothing deploys."""
    from engine.settings import Settings, StorageBackend, build_storage

    settings = Settings(
        storage_backend=StorageBackend.S3,
        s3_bucket=BUCKET,
        s3_prefix="artefacts/",
        region="eu-west-1",
        client_id="acme",
        local_cache_dir=tmp_path / "cache",
    )
    built = build_storage(settings)

    assert isinstance(built, S3Storage)
    assert isinstance(built, Storage)
    assert built.bucket == BUCKET
    assert built.prefix == "artefacts"


def test_repr_names_the_bucket_and_the_prefix_and_nothing_else(tmp_path: Path) -> None:
    assert repr(S3Storage(BUCKET, prefix="artefacts", workspace=tmp_path)) == (
        f"S3Storage(bucket='{BUCKET}', prefix='artefacts')"
    )


def test_the_module_imports_without_boto3_at_module_scope() -> None:
    """The package rule (DEC-306): a laptop checkout must not pay for boto3 to import this."""
    source = Path("engine/aws/s3_storage.py").read_text(encoding="utf-8")
    module_level = [
        line
        for line in source.splitlines()
        if (line.startswith("import ") or line.startswith("from ")) and "boto" in line
    ]
    assert module_level == []


class _Raiser:
    """An S3 client on which every call raises the same constructed error."""

    def __init__(self, error: Exception) -> None:
        self._error = error

    def __getattr__(self, name: str) -> Any:
        def call(**_kwargs: Any) -> Any:
            raise self._error

        return call


class _Recorder:
    """An S3 client that records the requests it is given and answers plausibly."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def put_object(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(("put_object", kwargs))
        return {"ETag": '"recorded"'}

    def create_multipart_upload(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(("create_multipart_upload", kwargs))
        return {"UploadId": "recorded"}
