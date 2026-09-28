"""Google BigQuery, an optional add-on: `pip install 'marketing-ai[bigquery]'` (Plan H M80).

No SQL at all: tables are listed with the BigQuery API and rows are read with `list_rows` (the
table-data API), so there is no query to build and nothing to inject into. Signing in uses a
service-account key (JSON), stored encrypted like every other secret; ask for a key whose role is
*BigQuery Data Viewer* plus *BigQuery Read Session User* - it can read and nothing else.
"""

from __future__ import annotations

import io
import json
from collections.abc import Mapping
from typing import Any, BinaryIO

import pandas as pd

from engine.connections.base import (
    FETCH_BATCH_ROWS,
    MAX_BROWSE_ITEMS,
    PREVIEW_ROWS,
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
    frame_preview,
    load_sdk,
    remaining,
    report,
    skipped_after,
    step,
    text_value,
)
from engine.utils.time import utc_now

__all__ = ["BigQueryConnector"]


class BigQueryConnector:
    kind = "bigquery"
    label = "Google BigQuery"
    extra = "bigquery"

    def info(self) -> KindInfo:
        return KindInfo(
            kind=self.kind,
            label=self.label,
            description="Tables in Google BigQuery. Needs the BigQuery add-on.",
            group="database",
            tier="optional",
            available=self.available(),
            addon=self.extra,
            fields=(
                FieldSpec(name="project", label="Project ID", required=True, placeholder="my-project-123"),
                FieldSpec(
                    name="credentials_json",
                    label="Service account key (JSON)",
                    type="textarea",
                    secret=True,
                    required=True,
                    help="Paste the whole key file. Give it the BigQuery Data Viewer role only.",
                ),
                FieldSpec(name="dataset", label="Only this dataset", advanced=True),
            ),
        )

    def available(self) -> bool:
        return (
            load_sdk("google.cloud.bigquery") is not None
            and load_sdk("google.oauth2.service_account") is not None
        )

    def _client(self, config: Mapping[str, ConfigValue], secrets: Mapping[str, str]) -> Any:
        bigquery = load_sdk("google.cloud.bigquery")
        service_account = load_sdk("google.oauth2.service_account")
        if bigquery is None or service_account is None:
            raise addon_missing(self.label, self.extra)
        try:
            info = json.loads(secrets.get("credentials_json", ""))
            if not isinstance(info, dict):
                raise ValueError
        except ValueError:
            raise ConnectorError(
                "CONNECTION_KEY_INVALID",
                "The service account key is not a valid key file.",
                "Paste the whole JSON file you downloaded from Google Cloud, from { to }.",
            ) from None
        try:
            credentials = service_account.Credentials.from_service_account_info(info)
            return bigquery.Client(project=text_value(config, "project"), credentials=credentials)
        except Exception:
            raise ConnectorError(
                "CONNECTION_KEY_INVALID",
                "The service account key could not be used.",
                "Download a new key for the service account and paste it again.",
            ) from None

    def test(self, config: Mapping[str, ConfigValue], secrets: Mapping[str, str]) -> TestReport:
        steps: list[TestStep] = []
        try:
            client = self._client(config, secrets)
            datasets = self._datasets(client, config)
        except ConnectorError as exc:
            if exc.code == "CONNECTION_UNREACHABLE":
                steps.append(step(StepName.REACH, StepStatus.FAILED, exc.message, exc.fix))
                return report([*steps, *skipped_after(remaining(StepName.REACH))], utc_now())
            steps.append(step(StepName.REACH, StepStatus.OK, "BigQuery answered."))
            steps.append(step(StepName.SIGN_IN, StepStatus.FAILED, exc.message, exc.fix))
            return report([*steps, *skipped_after(remaining(StepName.SIGN_IN))], utc_now())
        steps.append(step(StepName.REACH, StepStatus.OK, "BigQuery answered."))
        steps.append(
            step(
                StepName.SIGN_IN, StepStatus.OK, f"Signed in to the project {text_value(config, 'project')}."
            )
        )
        first: tuple[str, str] | None = None
        count = 0
        try:
            for dataset in datasets:
                tables = self._tables(client, config, dataset)
                count += len(tables)
                if first is None and tables:
                    first = (dataset, tables[0])
                if count >= MAX_BROWSE_ITEMS:
                    break
        except ConnectorError as exc:
            steps.append(step(StepName.LIST, StepStatus.FAILED, exc.message, exc.fix))
            return report([*steps, *skipped_after(remaining(StepName.LIST))], utc_now())
        if first is None:
            steps.append(
                step(
                    StepName.LIST,
                    StepStatus.OK,
                    "Signed in, but this key cannot see any tables.",
                    "Give the service account the BigQuery Data Viewer role on the datasets you need.",
                )
            )
            steps.append(step(StepName.READ_SAMPLE, StepStatus.SKIPPED, "Nothing to read yet."))
        else:
            steps.append(step(StepName.LIST, StepStatus.OK, f"Found {count} tables."))
            try:
                self._read(client, config, first[0], first[1], 1)
                steps.append(
                    step(StepName.READ_SAMPLE, StepStatus.OK, f"Read a row of {first[0]}.{first[1]}.")
                )
            except ConnectorError:
                steps.append(
                    step(
                        StepName.READ_SAMPLE,
                        StepStatus.FAILED,
                        f"The table {first[0]}.{first[1]} is listed but could not be read.",
                        "Also give the service account the BigQuery Read Session User role.",
                    )
                )
        steps.append(
            step(
                StepName.READ_ONLY_CHECK,
                StepStatus.SKIPPED,
                "BigQuery cannot say what else this key may do. Marketing AI only reads rows.",
                "For safety, give the service account the BigQuery Data Viewer role only.",
            )
        )
        return report(steps, utc_now())

    def _datasets(self, client: Any, config: Mapping[str, ConfigValue]) -> list[str]:
        only = text_value(config, "dataset")
        try:
            names = [
                str(d.dataset_id)
                for d in client.list_datasets(max_results=MAX_BROWSE_ITEMS + 1, timeout=READ_TIMEOUT_S)
            ]
        except Exception as exc:
            raise _api_error(exc) from None
        if only:
            if only not in names:
                raise _not_found("dataset", only)
            return [only]
        return names

    def _tables(self, client: Any, config: Mapping[str, ConfigValue], dataset: str) -> list[str]:
        try:
            ref = f"{text_value(config, 'project')}.{dataset}"
            return [
                str(t.table_id)
                for t in client.list_tables(ref, max_results=MAX_BROWSE_ITEMS + 1, timeout=READ_TIMEOUT_S)
            ]
        except Exception as exc:
            raise _api_error(exc) from None

    def _checked(
        self, client: Any, config: Mapping[str, ConfigValue], selection: Selection
    ) -> tuple[str, str]:
        dataset, table = selection.schema_name or "", selection.table or ""
        if not dataset or not table:
            raise ConnectorError(
                "CONNECTION_PICK_A_TABLE", "Pick a table first.", "Choose a table from the list."
            )
        if dataset not in self._datasets(client, config):
            raise _not_found("dataset", dataset)
        if table not in self._tables(client, config, dataset):
            raise _not_found("table", f"{dataset}.{table}")
        return dataset, table

    def _rows(
        self, client: Any, config: Mapping[str, ConfigValue], dataset: str, table: str, limit: int
    ) -> Any:
        ref = f"{text_value(config, 'project')}.{dataset}.{table}"
        try:
            return client.list_rows(
                ref, max_results=limit, page_size=FETCH_BATCH_ROWS, timeout=READ_TIMEOUT_S
            )
        except Exception as exc:
            raise _api_error(exc) from None

    def _read(
        self, client: Any, config: Mapping[str, ConfigValue], dataset: str, table: str, limit: int
    ) -> pd.DataFrame:
        rows = self._rows(client, config, dataset, table, limit)
        try:
            records = [tuple(row.values()) for row in rows]
            columns = [str(field.name) for field in rows.schema]
        except Exception as exc:
            raise _api_error(exc) from None
        return pd.DataFrame(records, columns=columns)

    def browse(
        self, config: Mapping[str, ConfigValue], secrets: Mapping[str, str], path: str
    ) -> BrowseResult:
        client = self._client(config, secrets)
        datasets = self._datasets(client, config)
        if not path and len(datasets) != 1:
            items = [
                BrowseItem(name=d, kind="schema", path=d, importable=False)
                for d in datasets[:MAX_BROWSE_ITEMS]
            ]
            return BrowseResult(
                path="", parent=None, items=tuple(items), truncated=len(datasets) > MAX_BROWSE_ITEMS
            )
        dataset = path or datasets[0]
        if dataset not in datasets:
            raise _not_found("dataset", dataset)
        tables = self._tables(client, config, dataset)
        items = [
            BrowseItem(name=t, kind="table", path=dataset, schema_name=dataset, table=t, importable=True)
            for t in tables[:MAX_BROWSE_ITEMS]
        ]
        return BrowseResult(
            path=dataset,
            parent="" if len(datasets) > 1 else None,
            items=tuple(items),
            truncated=len(tables) > MAX_BROWSE_ITEMS,
        )

    def file_format(self, selection: Selection) -> FileFormat:  # noqa: ARG002
        return "csv"

    def preview(
        self, config: Mapping[str, ConfigValue], secrets: Mapping[str, str], selection: Selection
    ) -> Preview:
        client = self._client(config, secrets)
        dataset, table = self._checked(client, config, selection)
        return frame_preview(self._read(client, config, dataset, table, PREVIEW_ROWS))

    def fetch(
        self,
        config: Mapping[str, ConfigValue],
        secrets: Mapping[str, str],
        selection: Selection,
        sink: BinaryIO,
        *,
        limit_bytes: int,
        max_rows: int,
    ) -> FetchResult:
        client = self._client(config, secrets)
        dataset, table = self._checked(client, config, selection)
        writer = LimitedWriter(sink, limit_bytes, f"The table {dataset}.{table}")
        rows = self._rows(client, config, dataset, table, max_rows)
        count = 0
        try:
            columns = [str(field.name) for field in rows.schema]
            header = True
            for page in rows.pages:
                batch = [tuple(row.values()) for row in page]
                buffer = io.StringIO()
                pd.DataFrame(batch, columns=columns).to_csv(buffer, index=False, header=header)
                writer.write(buffer.getvalue().encode("utf-8"))
                header = False
                count += len(batch)
            if header:
                writer.write((",".join(columns) + "\n").encode("utf-8"))
        except ConnectorError:
            raise
        except Exception as exc:
            raise _api_error(exc) from None
        return FetchResult(
            file_format="csv", file_name=f"{dataset}.{table}.csv", size_bytes=writer.written, rows=count
        )


def _api_error(exc: BaseException) -> ConnectorError:
    code = getattr(exc, "code", None)
    name = type(exc).__name__
    if code in {401, 403} or name in {"Forbidden", "Unauthorized", "RefreshError"}:
        return ConnectorError(
            "CONNECTION_ACCESS_DENIED",
            "The service account key was refused.",
            "Give the service account the BigQuery Data Viewer and BigQuery Read Session User roles.",
            status=409,
        )
    if code == 404 or name == "NotFound":
        return ConnectorError(
            "CONNECTION_OBJECT_NOT_FOUND",
            "The project, dataset or table was not found.",
            "Check the project ID, then open the list again.",
            status=404,
        )
    if name in {"TransportError", "ConnectionError", "Timeout", "ServiceUnavailable"}:
        return ConnectorError(
            "CONNECTION_UNREACHABLE",
            "BigQuery did not answer.",
            "Check this computer's internet connection and try again.",
            status=502,
        )
    return ConnectorError(
        "CONNECTION_FAILED",
        "BigQuery could not be read.",
        "Test the connection to see what is wrong.",
        status=502,
    )


def _not_found(what: str, name: str) -> ConnectorError:
    return ConnectorError(
        "CONNECTION_OBJECT_NOT_FOUND",
        f"There is no {what} called {name} that this key can read.",
        "Open the list again and pick one that is there.",
        status=404,
    )
