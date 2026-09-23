"""Access requests (M48 item 4): everything held about one data principal, as one file to hand over.

The export reads exactly what erasure would remove - the same `find_principal` over the same whole
store, so "complete" means the same thing for both, and `tests/unit/production/test_access_export.py`
checks it against the same planted sentinel as the erasure test - plus the principal's consent history
and any earlier erasure requests.

**One zip of CSV and JSON** (DEC-745), because the rows come from files of different shapes: a scores
row, a dataset row and a raw bill line do not share a header, and forcing them into one JSON document
would turn a customer's own spreadsheet rows into something they cannot open. Inside the archive:

* `manifest.json` - an `AccessExportManifest`: when, for which request, every source location, the
  model versions trained on data that included the person, and what could not be extracted;
* `records/<store>/<key>.csv` - the principal's rows of each tabular file, with that file's header;
* `records/<store>/<key>.json` - the JSON values that are, or directly carry, the principal, each with
  its JSONPath;
* `records/<store>/<key>.txt` - the lines of a text file that mention them;
* `consent_history.json` and `erasure_history.json`.

The bytes are **returned, never written to the store**: a stored copy would be a new place the
person's data lives, which the next erasure would then have to find inside a compressed archive.
Model files are listed under `not_extractable` - their parameters are not records about anyone - and so
is a file that needs review because a numeric id cannot be told apart from a count in it (DEC-737).
"""

from __future__ import annotations

import csv
import io
import json
import uuid
import zipfile
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Final

from sqlalchemy.engine import Engine

from engine.audit.events import principal_hash
from engine.privacy.consent import ConsentLedger, principal_key
from engine.privacy.contracts import AccessExportManifest
from engine.privacy.erasure import erasure_requests, find_principal
from engine.privacy.errors import require_principal_id
from engine.privacy.layout import StoreIndex
from engine.privacy.rewrite import Extract, FileKind, Matcher, extract_bytes
from engine.storage import Storage
from engine.utils.logging import get_logger
from engine.utils.time import utc_now

__all__ = ["ACCESS_EXPORT_MEDIA_TYPE", "AccessExport", "export_principal"]

_LOGGER = get_logger(__name__)

ACCESS_EXPORT_MEDIA_TYPE: Final[str] = "application/zip"

_README: Final[str] = (
    "This archive holds every record this system keeps about one person, found by their customer id.\n"
    "manifest.json lists where each file came from. records/ holds the rows and values themselves,\n"
    "consent_history.json the consent records, erasure_history.json any earlier erasure requests.\n"
    "Model versions listed in the manifest were trained on data that included this person; a model\n"
    "stores learned parameters, not records, so there is nothing further to extract from it.\n"
)


@dataclass(frozen=True)
class AccessExport:
    """The finished export: a zip to stream to whoever made the request, and its manifest."""

    request_id: str
    filename: str
    media_type: str
    content: bytes
    manifest: AccessExportManifest


def export_principal(
    storage: Storage,
    principal_id: str,
    *,
    salt: str,
    engine: Engine | None = None,
    client_id: str | None = None,
    now: datetime | None = None,
    request_id: str | None = None,
    history_all_clients: bool = False,
) -> AccessExport:
    """Everything held about `principal_id`, as a zip of CSV and JSON (see the module docstring).

    `engine` is the platform database for the consent and erasure history; without one the archive
    carries the stores only and says so with empty histories. `history_all_clients` exports the
    consent history under every client, not only `client_id`'s (DEC-738).
    """
    cleaned = require_principal_id(principal_id)
    moment = now or utc_now()
    request = request_id or f"ar_{uuid.uuid4().hex[:20]}"
    matcher = Matcher.for_id(cleaned)
    layout = StoreIndex(storage)
    findings = find_principal(storage, cleaned, index=layout)
    hashed = principal_hash(principal_key(cleaned), salt=salt)
    files: dict[str, bytes] = {}
    not_extractable: list[str] = []
    for location in findings.locations:
        kind = FileKind(location.file_kind)
        if kind is FileKind.BINARY or location.needs_review:
            not_extractable.append(location.key)
            continue
        extract = extract_bytes(kind, storage.read_bytes(location.key), matcher, location.key_columns)
        name, payload = _render(location.store, location.key, extract)
        if payload:
            files[name] = payload
    consent: list[dict[str, Any]] = []
    erasures: list[dict[str, Any]] = []
    if engine is not None:
        ledger = ConsentLedger(engine, salt=salt)
        consent = [
            record.model_dump(mode="json")
            for record in ledger.history(cleaned, client_id=None if history_all_clients else client_id)
        ]
        erasures = [
            record.model_dump(mode="json") for record in erasure_requests(engine, principal_hash=hashed)
        ]
    files["consent_history.json"] = _json_bytes(consent)
    files["erasure_history.json"] = _json_bytes(erasures)
    files["README.txt"] = _README.encode("utf-8")
    manifest = AccessExportManifest(
        request_id=request,
        principal_hash=hashed,
        client_id=client_id,
        generated_at=moment,
        files=("manifest.json", *sorted(files)),
        locations=findings.locations,
        models=findings.models,
        consent_records=len(consent),
        erasure_requests=len(erasures),
        not_extractable=tuple(not_extractable),
    )
    files["manifest.json"] = (manifest.model_dump_json(indent=2) + "\n").encode("utf-8")
    content = _zip(files, moment)
    _LOGGER.info(
        "privacy.access_export request=%s locations=%d files=%d bytes=%d",
        request,
        len(findings.locations),
        len(files),
        len(content),
    )
    return AccessExport(
        request_id=request,
        filename=f"access_export_{request}.zip",
        media_type=ACCESS_EXPORT_MEDIA_TYPE,
        content=content,
        manifest=manifest,
    )


def _render(store: str, key: str, extract: Extract) -> tuple[str, bytes]:
    """The archive member for one location: CSV rows, JSON fragments or text lines."""
    stem = f"records/{store}/{key.replace('/', '__')}"
    if extract.kind in (FileKind.CSV, FileKind.PARQUET):
        out = io.StringIO()
        writer = csv.writer(out, lineterminator="\n")
        writer.writerow(extract.header)
        writer.writerows(extract.rows)
        return f"{stem}.csv", out.getvalue().encode("utf-8") if extract.rows else b""
    if extract.fragments:
        return f"{stem}.json", _json_bytes(
            [{"path": path, "value": value} for path, value in extract.fragments]
        )
    if extract.lines:
        return f"{stem}.txt", ("\n".join(extract.lines) + "\n").encode("utf-8")
    return stem, b""


def _json_bytes(payload: Any) -> bytes:
    return (json.dumps(payload, indent=2, ensure_ascii=False, default=str) + "\n").encode("utf-8")


def _zip(files: dict[str, bytes], moment: datetime) -> bytes:
    """A deflated zip with every member stamped with the export time, in name order after the manifest."""
    buffer = io.BytesIO()
    stamp = (moment.year, moment.month, moment.day, moment.hour, moment.minute, moment.second)
    order = ["manifest.json", *sorted(name for name in files if name != "manifest.json")]
    with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for name in order:
            info = zipfile.ZipInfo(name, date_time=stamp)
            info.compress_type = zipfile.ZIP_DEFLATED
            archive.writestr(info, files[name])
    return buffer.getvalue()
