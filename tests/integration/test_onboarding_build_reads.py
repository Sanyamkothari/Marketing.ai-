"""M37: a build reads each of its sources once (docs/PERFORMANCE.md, DEC-096).

Before M37 a build read every source twice - once to aggregate it, and again at the write stage to
profile it for the manifest - and each read parsed and fingerprinted the whole file. At the M14
benchmark size the second pass was a third of the build. Nothing about the dataset showed it: the
two passes agreed, so every other test stayed green while the build did its reading twice. This
test is the one that fails if a later edit brings the second pass back.
"""

from __future__ import annotations

from collections import Counter
from pathlib import Path
from typing import Any

import pytest

from engine.onboarding.sources import FileSourceReader, ProfiledRead
from engine.onboarding.specs import SourceSpec
from tests.integration.test_onboarding_flow import ENTITY_COLUMNS, _run

pytestmark = pytest.mark.integration


def test_the_build_reads_each_source_once_and_profiles_it_from_that_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    reads: Counter[str] = Counter()
    real = FileSourceReader.read_profiled

    def counting(self: FileSourceReader, source: SourceSpec) -> ProfiledRead:
        reads[source.source_id] += 1
        return real(self, source)

    def refused(self: FileSourceReader, source: SourceSpec, **_: Any) -> Any:
        raise AssertionError(f"the build read {source.source_id} a second time")

    monkeypatch.setattr(FileSourceReader, "read_profiled", counting)
    monkeypatch.setattr(FileSourceReader, "read", refused)
    monkeypatch.setattr(FileSourceReader, "profile", refused)

    flow = _run(tmp_path, entity_columns=ENTITY_COLUMNS)

    assert flow.report.passed
    assert reads == Counter({source.source_id: 1 for source in flow.sources})
    manifest = flow.manifest
    for source in flow.sources:
        assert manifest.source_fingerprints[source.source_id].hash == source.fingerprint.hash
