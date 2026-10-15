"""Keeps the table an approved join or stack makes as a new upload of the case (`VisitDeps.combine`).

Not a route: the case visit builds it when the person approves (see `engine.doctor.combine_run`). The whole files are
read, the table is written as a new upload read like any other file, and the files it was made from stay as they are.
"""

from __future__ import annotations

from pathlib import Path

import pandas as pd

from api.routes.uploads import finish_upload, load_upload
from engine.config import load_use_case
from engine.doctor.case import Case, CaseFile, CaseItem
from engine.doctor.combine_run import combine_item, upload_ids_of
from engine.doctor.contracts import Diagnosis
from engine.doctor.visit import CombinePort
from engine.intake.config import load_intake_config
from engine.stages import ingest
from engine.storage import Storage, upload_key
from engine.utils.ids import new_upload_id
from engine.utils.time import utc_now

__all__ = ["case_combiner"]


def case_combiner(storage: Storage, root: Path) -> CombinePort:
    """The visit's `CombinePort` over the uploads in `storage`."""

    def combine(case: Case, item: CaseItem, actor: str) -> CaseFile:
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
