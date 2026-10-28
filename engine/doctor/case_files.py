"""A case's files as M240's format tools read them (Plan O wave 1 integration; DEC-1801, DEC-1802 (t), DEC-1810).

M240's tools (`engine.doctor.tools_formats`) name a file by its id in the case and never see a storage key; the
`FormatToolContext` they need is implemented here over M231's case (`engine.doctor.case`):

* a file id is an upload id the case holds (`Case.files`); any other id is refused (`TOOL_INPUT_INVALID`), so a
  tool cannot reach a file of another case or another client;
* `received_file` is the file as it arrived (`original.*` when intake kept one, else the stored source);
* `derived_key` places a file made from another under that upload's own directory (`uploads/<id>/doctor/<name>`),
  so it is deleted with its upload by retention and erasure (`engine/privacy/layout.py`'s `uploads/` store);
* `readable_key` is the CSV or Parquet the engine reads: the `source.csv` `read_other_format` made, else the
  upload's stored source (intake's CSV for a workbook);
* `add_file` makes a CSV made from a file (a workbook's sheet) an upload of its own through the caller's
  `make_upload` (the API layer's `finish_upload`, so the sheet is profiled and kept like any upload) and adds it
  to the case with the workbook as its `original_upload_id`.

Only the registry's callers build a `CaseFiles`.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Final, Literal

import pandas as pd

from engine.doctor.case import CaseFile, CaseStore
from engine.doctor.registry import DataToolError
from engine.storage import Storage, StorageError, upload_key
from engine.utils.time import utc_now

__all__ = ["DERIVED_DIR", "CaseFiles"]

DERIVED_DIR: Final[str] = "doctor"
"""The folder, inside an upload's directory, for the files Data Doctor makes from it."""

_DERIVED_NAME: Final[re.Pattern[str]] = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")
_RECORD: Final[str] = "upload.json"


@dataclass(frozen=True)
class CaseFiles:
    """`engine.doctor.tools_formats.FormatToolContext` over one M231 case."""

    store: CaseStore
    client_id: str
    case_id: str
    actor: str
    """Who adds a file (a user id); the case's timeline names them."""
    make_upload: Callable[[str, str], str] | None = None
    """`(csv_key, file_name) -> upload_id`: makes the CSV at `csv_key` an upload of its own (the API layer's
    `finish_upload`). Without it `add_file` is refused, and nothing is added to the case."""
    row_cap: int | None = None
    """The most rows `frame` reads; the use case's profile row cap when the caller gives it."""

    @property
    def storage(self) -> Storage:
        return self.store.storage

    # -- the case's files ------------------------------------------------------------------------------
    def _file(self, file_id: str) -> CaseFile:
        case = self.store.get(self.client_id, self.case_id)
        for file in case.files:
            if file.upload_id == file_id:
                return file
        raise DataToolError("TOOL_INPUT_INVALID", "That file is not in this case.", status=422)

    def _record(self, file_id: str) -> dict[str, Any]:
        try:
            record = json.loads(self.storage.read_bytes(upload_key(file_id, _RECORD)))
        except (StorageError, ValueError) as exc:
            raise DataToolError(
                "TOOL_INPUT_INVALID", "That file's upload cannot be read.", status=422
            ) from exc
        if not isinstance(record, dict):
            raise DataToolError("TOOL_INPUT_INVALID", "That file's upload cannot be read.", status=422)
        return record

    def received_file(self, file_id: str) -> tuple[str, str]:
        """`(storage key, file name)` of the file as it arrived."""
        file = self._file(file_id)
        record = self._record(file_id)
        key = record.get("original_key") or record.get("source_key")
        if not isinstance(key, str) or not key.startswith(f"uploads/{file_id}/"):
            raise DataToolError("TOOL_INPUT_INVALID", "That file's upload cannot be read.", status=422)
        name = file.file_name or str(record.get("file_name") or key.rsplit("/", 1)[-1])
        return key, name

    def derived_key(self, file_id: str, name: str) -> str:
        """Where a file made from `file_id` is kept: inside that upload's directory, so it leaves with it."""
        self._file(file_id)
        if not _DERIVED_NAME.fullmatch(name) or ".." in name:
            raise DataToolError(
                "TOOL_INPUT_INVALID", "That is not a file name Data Doctor writes.", status=422
            )
        return upload_key(file_id, f"{DERIVED_DIR}/{name}")

    def readable_key(self, file_id: str) -> str:
        """The CSV or Parquet the engine reads: the converted `source.csv` when there is one, else the source."""
        converted = self.derived_key(file_id, "source.csv")
        if self.storage.exists(converted):
            return converted
        record = self._record(file_id)
        key = record.get("source_key")
        if not isinstance(key, str) or not key.startswith(f"uploads/{file_id}/"):
            raise DataToolError("TOOL_INPUT_INVALID", "That file's upload cannot be read.", status=422)
        return key

    def add_file(self, parent_file_id: str, csv_key: str, label: str) -> str:
        """Make the CSV at `csv_key` (made from `parent_file_id`) an upload and a file of the case; its id."""
        self._file(parent_file_id)
        if not csv_key.startswith(upload_key(parent_file_id, f"{DERIVED_DIR}/")):
            raise DataToolError(
                "TOOL_INPUT_INVALID", "Only a file made from this one can be added.", status=422
            )
        if self.make_upload is None:
            raise DataToolError(
                "TOOL_UNKNOWN", "Adding a file to the case is not available here.", status=404
            )
        file_name = f"{label}.csv"[:200]  # the label names the workbook and the sheet ("crm - Customers")
        upload_id = self.make_upload(csv_key, file_name)
        case = self.store.get(self.client_id, self.case_id)
        self.store.add_file(
            self.client_id,
            self.case_id,
            expected_version=case.version,
            actor=self.actor,
            file=CaseFile(
                upload_id=upload_id,
                file_name=file_name,
                sheet=label,
                original_upload_id=parent_file_id,
                added_at=utc_now(),
                added_by=self.actor,
            ),
        )
        return upload_id

    def frame(self, file_id: str) -> pd.DataFrame:
        """The file's rows as the engine reads them, capped."""
        from engine.stages import ingest

        key = self.readable_key(file_id)
        file_format: Literal["csv", "parquet"] = "parquet" if key.endswith(".parquet") else "csv"
        cap = self.row_cap if self.row_cap is not None else ingest.DEFAULT_PROFILE_ROW_CAP
        try:
            return ingest.read_upload(self.storage, key, file_format=file_format, row_cap=cap).frame
        except ingest.IngestError as exc:
            raise DataToolError(exc.code, exc.message, status=422) from exc
