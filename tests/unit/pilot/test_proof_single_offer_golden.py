"""A Value Proof Pack of one offer is byte for byte what it was before the several-offer fix (DEC-1314 (r)-(w)).

`tests/fixtures/pilot/proof_single_offer/<case>/` holds, for five campaigns of one offer measured through the API
on `main` at 0f59866 (before the fix), the aggregate artefacts their Pack read (`data/`, exactly the files the
Pack listed in `artefacts`) and the Pack itself (`pack.json`, `ProofView.model_dump_json(indent=1)` built at a
fixed moment). The cases are the ones M104's own tests build: a scored propensity campaign with a harmed band
and no value inputs, a scored uplift campaign priced with its run's value inputs, a verified-random audit and a
stated-random audit priced with their own, and an amount campaign valued from its adjusted interval
(`tests/integration/pilot/test_proof_pack.py`, `test_proof_pack_amounts.py`). Rebuilding each Pack from the same
artefacts with today's code must give the same bytes, and its provenance must still verify.
"""

from __future__ import annotations

import shutil
from datetime import UTC, datetime
from pathlib import Path

import pytest

from engine.pilot.proof import build_proof, verify_provenance
from engine.storage import LocalStorage

GOLDEN = Path(__file__).resolve().parents[2] / "fixtures" / "pilot" / "proof_single_offer"
BUILT = datetime(2026, 10, 10, 12, 0, tzinfo=UTC)
CASES = ("scored_banded", "scored_uplift", "audit_verified", "audit_stated", "amount_adjusted")


def test_every_golden_case_is_present() -> None:
    assert sorted(path.name for path in GOLDEN.iterdir() if path.is_dir()) == sorted(CASES)


@pytest.mark.parametrize("case", CASES)
def test_a_single_offer_pack_is_byte_identical_to_the_stored_one(
    case: str, config_root: Path, tmp_path: Path
) -> None:
    data = tmp_path / "data"
    shutil.copytree(GOLDEN / case / "data", data)
    (campaign_id,) = [path.name for path in (data / "campaigns").iterdir()]
    storage = LocalStorage(data)
    view = build_proof(storage, campaign_id, root=config_root, now=BUILT)
    expected = (GOLDEN / case / "pack.json").read_text(encoding="utf-8")
    assert view.model_dump_json(indent=1) + "\n" == expected
    verify_provenance(view, storage)
