"""Build an `AgentContext` from a frame, the way the API will from an upload."""

from __future__ import annotations

import atexit
import functools
import shutil
import tempfile
from pathlib import Path

import pandas as pd

from engine.agent.tools import AgentContext
from engine.config import RunMode, load_use_case
from engine.config import config_root as default_config_root
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


def with_header_help(source: Path, target: Path) -> Path:
    """A copy of the configuration root `source` at `target` with Plan K's `agentic.enabled` on.

    M142's setup help for new sources (the header reading, the lapse drafts, the mapping questions and the
    second opinion) is part of Plan K and runs only with that switch on (DEC-1409 (f)); everything else in the
    copy is the shipped configuration."""
    shutil.copytree(source, target)
    (target / "agentic" / "agentic.yaml").write_text("agentic:\n  enabled: true\n", encoding="utf-8")
    return target


@functools.cache
def header_help_root() -> Path:
    """One copy per process of the shipped configuration with `agentic.enabled` on (`with_header_help`)."""
    scratch = Path(tempfile.mkdtemp(prefix="header-help-"))
    atexit.register(shutil.rmtree, scratch, ignore_errors=True)
    return with_header_help(default_config_root(None), scratch / "configs")


def header_help_context(
    frame: pd.DataFrame,
    use_case_id: str = "targeted-advertisement",
    *,
    target: str | None = None,
    mode: RunMode = RunMode.TRAIN,
) -> AgentContext:
    """`context_for` under `header_help_root()`: Guided setup with M142's setup help on."""
    return context_for(frame, use_case_id, config_root=header_help_root(), target=target, mode=mode)


def synthetic(
    variant: str = "clean", rows: int = 2_000, use_case_id: str = "targeted-advertisement"
) -> pd.DataFrame:
    return generate(GenerationSpec(use_case_id=use_case_id, rows=rows, variant=variant))
