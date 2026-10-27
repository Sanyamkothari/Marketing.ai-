"""Shared pieces for the waves benchmark tests: one tiny synthetic X5-shaped table, built once."""

from __future__ import annotations

import pandas as pd
import pytest

from autonomy.gym.waves.data import WavesData, build_waves_data, fixture_tables


@pytest.fixture(scope="session")
def tables() -> tuple[pd.DataFrame, pd.DataFrame, pd.Timestamp]:
    return fixture_tables(300, seed=3)


@pytest.fixture(scope="session")
def data(tables: tuple[pd.DataFrame, pd.DataFrame, pd.Timestamp]) -> WavesData:
    receipts, clients, cutoff = tables
    return build_waves_data(receipts, clients, cutoff=cutoff)
