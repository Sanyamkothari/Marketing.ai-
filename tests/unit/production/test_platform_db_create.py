"""`create_tables` on an empty data folder: two first requests at once never fail (UI_AUDIT §8.4)."""

from __future__ import annotations

import threading
from pathlib import Path

from sqlalchemy import inspect

from engine.platform_db import create_tables, sqlite_engine
from engine.scheduling.schedules import SCHEDULING_TABLES


def test_first_requests_at_once_create_each_table_without_an_error(tmp_path: Path) -> None:
    engines = [sqlite_engine(tmp_path / "platform.db") for _ in range(8)]
    start = threading.Barrier(len(engines))
    errors: list[BaseException] = []

    def first_request(index: int) -> None:
        start.wait()
        try:
            create_tables(engines[index], SCHEDULING_TABLES)
        except BaseException as exc:  # any failure is the defect
            errors.append(exc)

    threads = [threading.Thread(target=first_request, args=(i,)) for i in range(len(engines))]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert errors == []
    assert set(SCHEDULING_TABLES) <= set(inspect(engines[0]).get_table_names())


def test_a_table_created_by_another_process_in_between_is_not_an_error(tmp_path: Path) -> None:
    create_tables(sqlite_engine(tmp_path / "platform.db"), SCHEDULING_TABLES)
    create_tables(sqlite_engine(tmp_path / "platform.db"), SCHEDULING_TABLES)
