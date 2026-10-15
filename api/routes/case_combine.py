"""Keeps the table an approved join or stack makes as a new upload of the case (`VisitDeps.combine`).

Not a route: the case visit builds it when the person approves (see `engine.doctor.combine_run`). The whole files are
read, the table is written as a new upload read like any other file, and the files it was made from stay as they are.
"""

from __future__ import annotations

import io
import json
from pathlib import Path

import pandas as pd
from fastapi import HTTPException

from api.routes.uploads import finish_upload, load_upload
from engine.agent.tools import AgentToolError
from engine.config import load_use_case
from engine.doctor.case import Case, CaseFile, CaseItem, case_dir
from engine.doctor.combine_run import combine_item, upload_ids_of
from engine.doctor.contracts import Diagnosis
from engine.doctor.visit import CombinePort
from engine.intake.config import load_intake_config
from engine.privacy.working_key import KEY_VAULT_FILENAME, new_salt
from engine.stages import ingest
from engine.storage import Storage, StorageError, upload_key
from engine.utils.ids import new_upload_id
from engine.utils.time import utc_now

__all__ = ["case_combiner"]


def _case_salt(storage: Storage, case: Case) -> str:
    """The case's salt for hashing a key that is personal data: made once, kept beside the case, the same every time."""
    key = f"{case_dir(case.client_id, case.case_id)}key_salt.json"
    try:
        stored = json.loads(storage.read_bytes(key))
        if isinstance(stored, dict) and isinstance(stored.get("salt"), str):
            return str(stored["salt"])
    except (StorageError, ValueError):
        pass
    salt = new_salt()
    storage.write_bytes(key, json.dumps({"salt": salt}).encode("utf-8"))
    return salt


def case_combiner(storage: Storage, root: Path) -> CombinePort:
    """The visit's `CombinePort` over the uploads in `storage`."""

    def combine(case: Case, item: CaseItem, actor: str) -> CaseFile:
        try:
            return _combine(case, item, actor)
        except (HTTPException, StorageError, ingest.IngestError) as exc:  # the visit stops and says why
            raise AgentToolError(
                "COMBINE_FAILED",
                "The combined table could not be made from the files. Untick the change to go on.",
            ) from exc

    def _combine(case: Case, item: CaseItem, actor: str) -> CaseFile:
        ids = upload_ids_of(item)
        records = {upload_id: load_upload(storage, upload_id) for upload_id in ids}
        frames: dict[str, pd.DataFrame] = {}
        for upload_id, record in records.items():
            read = ingest.read_upload(
                storage, record.source_key, file_format=record.file_format, keep_all_rows=True
            )
            frames[upload_id] = read.all_rows if read.all_rows is not None else read.frame
        made = combine_item(
            item,
            Diagnosis.model_validate(case.diagnosis),
            frames,
            {upload_id: record.file_name for upload_id, record in records.items()},
            key_salt=_case_salt(storage, case),
        )
        first = records[ids[0]]
        data = made.frame.to_csv(index=False).encode("utf-8")
        upload_id = new_upload_id()
        key = upload_key(upload_id, "source.csv")
        storage.write_bytes(key, data)
        finish_upload(
            storage,
            load_use_case(first.use_case_id, root),
            upload_id=upload_id,
            source_key=key,
            file_format="csv",
            file_name=made.file_name,
            file_size_bytes=len(data),
            mode=first.mode,
            intake=load_intake_config(root),
        )
        if made.key_vault is not None:
            # The customers' key is personal data: the table holds hashes, and the real values wait here for the
            # contact-list export (G24).
            buffer = io.BytesIO()
            made.key_vault.to_parquet(buffer, index=False)
            storage.write_bytes(upload_key(upload_id, KEY_VAULT_FILENAME), buffer.getvalue())
        record = load_upload(storage, upload_id)
        return CaseFile(
            upload_id=upload_id,
            file_name=record.file_name,
            intake_key=record.intake_key,
            fingerprint=record.fingerprint_hash,
            original_upload_id=ids[0],
            added_at=utc_now(),
            added_by=actor,
        )

    return combine
