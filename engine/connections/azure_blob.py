"""Azure Blob Storage, an optional add-on: `pip install 'marketing-ai[azure]'` (Plan H M80).

Signs in with a SAS token (preferred: it can be made read-and-list only) or the storage account's
key. The read-only check needs no call at all: a SAS token lists its own permissions (`sp=`), and an
account key can always write, so the test says so.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any, BinaryIO, Final
from urllib.parse import parse_qs

from engine.connections.base import (
    CONNECT_TIMEOUT_S,
    MAX_BROWSE_ITEMS,
    PREVIEW_MAX_BYTES,
    PREVIEW_MAX_PARQUET_BYTES,
    READ_TIMEOUT_S,
    BrowseItem,
    BrowseResult,
    ConfigValue,
    ConnectorError,
    FetchResult,
    FieldSpec,
    FileFormat,
    KindInfo,
    LimitedWriter,
    Preview,
    Selection,
    StepName,
    StepStatus,
    TestReport,
    TestStep,
    addon_missing,
    load_sdk,
    remaining,
    report,
    skipped_after,
    step,
    text_value,
    too_large,
)
from engine.connections.files import (
    file_format_of,
    importable,
    parent_of,
    parquet_too_big_to_preview,
    preview_bytes,
)
from engine.utils.time import utc_now

__all__ = ["AzureBlobConnector", "sas_permissions"]

_ACCOUNT: Final[re.Pattern[str]] = re.compile(r"^[a-z0-9]{3,24}$")
_WRITE_PERMISSIONS: Final[frozenset[str]] = frozenset("wdacuxtmi")
"""SAS permission letters that change something: write, delete, add, create, update, delete-version,
tag, move, set-immutability. Only `r` (read) and `l` (list) are needed."""


def sas_permissions(token: str) -> str | None:
    """The `sp=` permissions of a SAS token, or None when the credential is not a SAS token."""
    query = token.lstrip("?")
    if "sig=" not in query:
        return None
    values = parse_qs(query).get("sp")
    return values[0] if values else ""


class AzureBlobConnector:
    kind = "azure_blob"
    label = "Azure Blob Storage"
    extra = "azure"

    def info(self) -> KindInfo:
        return KindInfo(
            kind=self.kind,
            label=self.label,
            description="CSV and Parquet files in an Azure Blob Storage container. Needs the Azure add-on.",
            group="store",
            tier="optional",
            available=self.available(),
            addon=self.extra,
            fields=(
                FieldSpec(
                    name="account", label="Storage account name", required=True, placeholder="mycompanydata"
                ),
                FieldSpec(name="container", label="Container", required=True, placeholder="exports"),
                FieldSpec(
                    name="credential",
                    label="SAS token or account key",
                    type="password",
                    secret=True,
                    required=True,
                    help="A SAS token with only Read and List permissions is safest.",
                ),
                FieldSpec(name="prefix", label="Start in folder", advanced=True, placeholder="exports/"),
            ),
        )

    def available(self) -> bool:
        return load_sdk("azure.storage.blob") is not None

    def _container(self, config: Mapping[str, ConfigValue], secrets: Mapping[str, str]) -> Any:
        sdk = load_sdk("azure.storage.blob")
        if sdk is None:
            raise addon_missing(self.label, self.extra)
        account = text_value(config, "account").lower()
        if not _ACCOUNT.match(account):
            raise ConnectorError(
                "CONNECTION_ACCOUNT_INVALID",
                "The storage account name is not valid.",
                "It is 3 to 24 lower-case letters and digits, the first part of <account>.blob.core.windows.net.",
            )
        credential = secrets.get("credential", "").strip()
        service = sdk.BlobServiceClient(
            account_url=f"https://{account}.blob.core.windows.net",
            credential=credential.lstrip("?") if sas_permissions(credential) is not None else credential,
            connection_timeout=CONNECT_TIMEOUT_S,
            read_timeout=READ_TIMEOUT_S,
        )
        return service.get_container_client(text_value(config, "container"))

    def test(self, config: Mapping[str, ConfigValue], secrets: Mapping[str, str]) -> TestReport:
        steps: list[TestStep] = []
        container_name = text_value(config, "container")
        prefix = text_value(config, "prefix")
        try:
            container = self._container(config, secrets)
            container.get_container_properties()
        except ConnectorError as exc:
            steps.append(step(StepName.REACH, StepStatus.SKIPPED, "Not tried: the settings are incomplete."))
            steps.append(step(StepName.SIGN_IN, StepStatus.FAILED, exc.message, exc.fix))
            return report([*steps, *skipped_after(remaining(StepName.SIGN_IN))], utc_now())
        except Exception as exc:
            name = type(exc).__name__
            if name in {"ServiceRequestError", "ServiceResponseError"}:
                steps.append(
                    step(
                        StepName.REACH,
                        StepStatus.FAILED,
                        "Azure did not answer for this storage account.",
                        "Check the storage account name and this computer's internet connection.",
                    )
                )
                return report([*steps, *skipped_after(remaining(StepName.REACH))], utc_now())
            steps.append(step(StepName.REACH, StepStatus.OK, "Azure answered."))
            if name == "ResourceNotFoundError":
                steps.append(step(StepName.SIGN_IN, StepStatus.OK, "Signed in."))
                steps.append(
                    step(
                        StepName.LIST,
                        StepStatus.FAILED,
                        f"There is no container called {container_name}.",
                        "Check the container name's spelling.",
                    )
                )
                return report([*steps, *skipped_after(remaining(StepName.LIST))], utc_now())
            steps.append(
                step(
                    StepName.SIGN_IN,
                    StepStatus.FAILED,
                    "The SAS token or account key was refused.",
                    "Copy the token or key again. A SAS token must not have expired and needs Read and List.",
                )
            )
            return report([*steps, *skipped_after(remaining(StepName.SIGN_IN))], utc_now())
        steps.append(step(StepName.REACH, StepStatus.OK, "Azure answered."))
        steps.append(
            step(StepName.SIGN_IN, StepStatus.OK, f"Signed in and opened the container {container_name}.")
        )
        try:
            keys: list[str] = []
            for blob in container.list_blobs(name_starts_with=prefix or None):
                if importable(str(blob.name)):
                    keys.append(str(blob.name))
                if len(keys) >= MAX_BROWSE_ITEMS:
                    break
        except Exception:
            steps.append(
                step(
                    StepName.LIST,
                    StepStatus.FAILED,
                    "Signed in, but the files could not be listed.",
                    "Give the SAS token the List permission.",
                )
            )
            return report([*steps, *skipped_after(remaining(StepName.LIST))], utc_now())
        steps.append(step(StepName.LIST, StepStatus.OK, f"Found {len(keys)} CSV or Parquet files."))
        if keys:
            try:
                container.download_blob(keys[0], offset=0, length=1024).readall()
                steps.append(step(StepName.READ_SAMPLE, StepStatus.OK, f"Read the start of {keys[0]}."))
            except Exception:
                steps.append(
                    step(
                        StepName.READ_SAMPLE,
                        StepStatus.FAILED,
                        "The files can be listed but not read.",
                        "Give the SAS token the Read permission.",
                    )
                )
        else:
            steps.append(step(StepName.READ_SAMPLE, StepStatus.SKIPPED, "Nothing to read yet."))
        steps.append(_read_only_step(secrets.get("credential", "")))
        return report(steps, utc_now())

    def browse(
        self, config: Mapping[str, ConfigValue], secrets: Mapping[str, str], path: str
    ) -> BrowseResult:
        root = text_value(config, "prefix")
        prefix = path or root
        _inside_root(prefix, root)
        container = self._container(config, secrets)
        items: list[BrowseItem] = []
        try:
            for entry in container.walk_blobs(name_starts_with=prefix or None, delimiter="/"):
                name = str(entry.name)
                if name.endswith("/"):
                    items.append(
                        BrowseItem(
                            name=name[len(prefix) :].rstrip("/"), kind="folder", path=name, importable=False
                        )
                    )
                else:
                    size = getattr(entry, "size", None)
                    items.append(
                        BrowseItem(
                            name=name[len(prefix) :],
                            kind="file",
                            path=name,
                            size_bytes=None if size is None else int(size),
                            importable=importable(name),
                        )
                    )
                if len(items) > MAX_BROWSE_ITEMS:
                    break
        except Exception as exc:
            raise _read_error(exc) from None
        parent = parent_of(prefix) if prefix and prefix != root else None
        return BrowseResult(
            path=prefix,
            parent=parent,
            items=tuple(items[:MAX_BROWSE_ITEMS]),
            truncated=len(items) > MAX_BROWSE_ITEMS,
        )

    def file_format(self, selection: Selection) -> FileFormat:
        return file_format_of(_key(selection))

    def preview(
        self, config: Mapping[str, ConfigValue], secrets: Mapping[str, str], selection: Selection
    ) -> Preview:
        key = _key(selection)
        _inside_root(key, text_value(config, "prefix"))
        file_format = file_format_of(key)
        container = self._container(config, secrets)
        try:
            size = int(container.get_blob_client(key).get_blob_properties().size)
            if file_format == "csv":
                length = min(size, PREVIEW_MAX_BYTES)
                data = container.download_blob(key, offset=0, length=length).readall() if length else b""
                return preview_bytes(data, "csv", truncated=size > PREVIEW_MAX_BYTES)
            if size > PREVIEW_MAX_PARQUET_BYTES:
                raise parquet_too_big_to_preview()
            data = container.download_blob(key).readall()
        except ConnectorError:
            raise
        except Exception as exc:
            raise _read_error(exc) from None
        return preview_bytes(data, "parquet", truncated=False)

    def fetch(
        self,
        config: Mapping[str, ConfigValue],
        secrets: Mapping[str, str],
        selection: Selection,
        sink: BinaryIO,
        *,
        limit_bytes: int,
        max_rows: int,  # noqa: ARG002 - a file is imported whole; ingest applies the row cap
    ) -> FetchResult:
        key = _key(selection)
        _inside_root(key, text_value(config, "prefix"))
        file_format = file_format_of(key)
        container = self._container(config, secrets)
        name = key.rsplit("/", 1)[-1]
        writer = LimitedWriter(sink, limit_bytes, name)
        try:
            size = int(container.get_blob_client(key).get_blob_properties().size)
            if size > limit_bytes:
                raise too_large(name, limit_bytes)
            for chunk in container.download_blob(key).chunks():
                writer.write(chunk)
        except ConnectorError:
            raise
        except Exception as exc:
            raise _read_error(exc) from None
        return FetchResult(file_format=file_format, file_name=name, size_bytes=writer.written)


def _read_only_step(credential: str) -> TestStep:
    permissions = sas_permissions(credential.strip())
    if permissions is None:
        return step(
            StepName.READ_ONLY_CHECK,
            StepStatus.WARNING,
            "An account key can change everything in the storage account. Marketing AI only reads.",
            "For safety, create a SAS token with only Read and List permissions and use that instead.",
        )
    if set(permissions) & _WRITE_PERMISSIONS:
        return step(
            StepName.READ_ONLY_CHECK,
            StepStatus.WARNING,
            "This SAS token can also change files. Marketing AI only reads.",
            "For safety, create a SAS token with only Read and List permissions.",
        )
    return step(StepName.READ_ONLY_CHECK, StepStatus.OK, "This SAS token can only read and list.")


def _key(selection: Selection) -> str:
    key = (selection.path or "").strip()
    if not key or key.endswith("/"):
        raise ConnectorError(
            "CONNECTION_PICK_A_FILE", "Pick a file first.", "Choose a CSV or Parquet file from the list."
        )
    return key


def _inside_root(key: str, root: str) -> None:
    if root and not key.startswith(root):
        raise ConnectorError(
            "CONNECTION_OUTSIDE_FOLDER",
            "That file is outside the folder this connection is limited to.",
            "Pick a file from the list, or change “Start in folder” on the connection.",
        )


def _read_error(exc: BaseException) -> ConnectorError:
    name = type(exc).__name__
    if name == "ResourceNotFoundError":
        return ConnectorError(
            "CONNECTION_OBJECT_NOT_FOUND",
            "The container or file was not found.",
            "Open the list again and pick a file that is there.",
            status=404,
        )
    if name in {"ClientAuthenticationError", "HttpResponseError"}:
        return ConnectorError(
            "CONNECTION_ACCESS_DENIED",
            "The SAS token or account key was refused.",
            "Test the connection to see what is wrong.",
            status=409,
        )
    return ConnectorError(
        "CONNECTION_FAILED",
        "Azure Blob Storage could not be read.",
        "Test the connection to see what is wrong.",
        status=502,
    )
