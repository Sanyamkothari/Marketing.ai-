"""Build an `AgentContext` from a frame, the way the API will from an upload."""

from __future__ import annotations

from pathlib import Path

import pandas as pd

from engine.agent.tools import AgentContext
from engine.config import RunMode, load_use_case
from engine.stages import ingest
from tests.fixtures.make_data import GenerationSpec, generate


def context_for(
    frame: pd.DataFrame,
    use_case_id: str = "targeted-advertisement",
    *,
    config_root: Path | None = None,
    target: str | None = None,
    mode: RunMode = RunMode.TRAIN,
) -> AgentContext:
    config = load_use_case(use_case_id, config_root)
    profile = ingest.profile_dataset(
        frame,
        config,
        upload_id="u-test",
        file_name="test.csv",
        file_format="csv",
        file_size_bytes=1,
        delimiter=",",
        encoding="utf-8",
    )
    return AgentContext(
        use_case_id=use_case_id,
        config=config,
        config_root=config_root,
        upload_id="u-test",
        mode=mode,
        profile=profile,
        frame=frame,
        target=target,
    )


def synthetic(
    variant: str = "clean", rows: int = 2_000, use_case_id: str = "targeted-advertisement"
) -> pd.DataFrame:
    return generate(GenerationSpec(use_case_id=use_case_id, rows=rows, variant=variant))
