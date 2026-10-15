"""A join key that is personal data (G24): the working key is a salted hash, the real value waits in a vault, and an
Aadhaar number is never stored (the person is asked for another key)."""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from engine.agent.tools import AgentToolError
from engine.doctor.case import Case, CaseItem
from engine.doctor.combine_run import combine_item
from engine.doctor.contracts import Diagnosis
from engine.privacy.working_key import (
    WorkingKeyError,
    attach_real_keys,
    key_vault,
    new_salt,
    pii_key_kind,
    refuse_aadhaar,
    working_key,
)
from tests.unit.doctor.test_combining import START, _Day, _one

CUSTOMERS = 120


def _phones() -> list[str]:
    return [f"9{n:09d}"[:10] for n in range(100_000_001, 100_000_001 + CUSTOMERS)]


def _files(spell: str = "plain") -> dict[str, tuple[str, pd.DataFrame]]:
    phones = _phones()
    rng = np.random.default_rng(2)
    customers = pd.DataFrame(
        {
            "phone": phones,
            "region": rng.choice(["North", "South"], CUSTOMERS),
            "tenure": rng.integers(1, 60, CUSTOMERS),
        }
    )
    who = [phones[n % 100] for n in range(600)]
    if spell == "spaced":  # the same people, written the way another system writes them
        who = [f"+91 {p[:5]} {p[5:]}" for p in who]
    orders = pd.DataFrame(
        {
            "order_id": [f"O{n}" for n in range(600)],
            "phone": who,
            "order_date": [
                (START + pd.Timedelta(days=(n * 3) % 200)).strftime("%Y-%m-%d") for n in range(600)
            ],
            "amount": [100 + n % 400 for n in range(600)],
        }
    )
    return {"up-c": ("customers.csv", customers), "up-o": ("orders.csv", orders)}


def _built(
    tmp_path: Path, files: dict[str, tuple[str, pd.DataFrame]], salt: str
) -> tuple[Case, CaseItem, object]:
    day = _Day(tmp_path, files)
    case = day.open()
    item = _one(case, "build_dataset")
    frames = {u: day.world.files[u].frame for u in files}
    names = {u: day.world.files[u].file_name for u in files}
    made = combine_item(item, Diagnosis.model_validate(case.diagnosis), frames, names, key_salt=salt)
    return case, item, made


def test_the_same_number_written_two_ways_gets_one_hash_and_a_salt_changes_it() -> None:
    series = pd.Series(["98765 43210", "+91-9876543210", None, "9876543211"])
    hashed = working_key(series, "salt-a")
    assert hashed[0] == hashed[1] and hashed[0] != hashed[3]
    assert pd.isna(hashed[2])
    assert "9876543210" not in "".join(h for h in hashed.dropna())
    assert working_key(series, "salt-b")[0] != hashed[0]  # another case, another hash
    assert new_salt() != new_salt()


def test_the_vault_gives_the_real_number_back_for_the_export_only() -> None:
    real = pd.Series(["9876543210", "9876543211"])
    hashed = working_key(real, "s")
    frame = pd.DataFrame({"phone": hashed, "treat": [1, 0]})
    out = attach_real_keys(frame, key_vault(real, hashed), "phone")
    assert out["contact_key"].tolist() == ["9876543210", "9876543211"]
    assert "contact_key" not in frame.columns  # the working table never holds it


def test_key_kinds_and_the_aadhaar_refusal() -> None:
    assert pii_key_kind(pd.Series(_phones()), "phone") == "phone"
    assert pii_key_kind(pd.Series([f"C-{n}" for n in range(50)]), "customer_id") is None
    aadhaar = pd.Series([f"{2000_0000_0000 + n * 7_919:012d}" for n in range(60)])
    assert pii_key_kind(aadhaar, "aadhaar_no") == "aadhaar"
    with pytest.raises(WorkingKeyError) as raised:
        refuse_aadhaar("aadhaar")
    assert raised.value.code == "KEY_AADHAAR_NOT_STORED" and "another ID column" in raised.value.message
    refuse_aadhaar("phone")  # a phone number is hashed, not refused


@pytest.mark.parametrize("spell", ["plain", "spaced"])
def test_a_join_on_a_phone_number_carries_hashes_and_keeps_the_real_numbers_in_the_vault(
    tmp_path: Path, spell: str
) -> None:
    _, _, made = _built(tmp_path, _files(spell), "case-salt")
    table, vault = made.frame, made.key_vault  # type: ignore[attr-defined]

    assert not set(table["phone"]) & set(_phones())  # no real number in the table that goes on
    assert table["phone"].str.fullmatch(r"[0-9a-f]{32}").all()
    assert len(table) == CUSTOMERS and table["phone"].is_unique
    counted = table.filter(like="order").sum(numeric_only=True).sum()
    assert counted > 0  # the orders still met their customers, even written another way

    assert vault is not None and set(vault["real_key"]) == set(_phones())
    assert vault["working_key"].tolist() == table["phone"].tolist() or set(vault["working_key"]) == set(
        table["phone"]
    )


def test_a_join_on_an_aadhaar_number_is_refused_and_asks_for_another_key(tmp_path: Path) -> None:
    aadhaar = [f"{2000_0000_0000 + n * 7_919:012d}" for n in range(CUSTOMERS)]
    files = _files()
    files["up-c"][1]["phone"] = aadhaar
    files["up-o"][1]["phone"] = [aadhaar[n % 100] for n in range(600)]
    files["up-c"][1].rename(columns={"phone": "aadhaar_no"}, inplace=True)
    files["up-o"][1].rename(columns={"phone": "aadhaar_no"}, inplace=True)

    day = _Day(tmp_path, files)
    case = day.open()
    assert not [i for i in case.items if i.kind == "build_dataset"]  # nothing is proposed on that key
    asked = [q for q in case.questions if q.code == "KEY_AADHAAR_NOT_STORED"]
    assert asked and "never stored" in asked[0].text


def test_an_approved_join_whose_key_turns_out_to_be_aadhaar_numbers_stops_without_storing_them(
    tmp_path: Path,
) -> None:
    day = _Day(tmp_path, _files())
    case = day.open()
    item = _one(case, "build_dataset")
    aadhaar = [f"{2000_0000_0000 + n * 7_919:012d}" for n in range(CUSTOMERS)]
    frames = {u: day.world.files[u].frame.copy() for u in ("up-c", "up-o")}
    frames["up-c"]["phone"] = aadhaar
    frames["up-o"]["phone"] = [aadhaar[n % 100] for n in range(600)]
    with pytest.raises(AgentToolError) as raised:
        combine_item(item, Diagnosis.model_validate(case.diagnosis), frames, {}, key_salt="s")
    assert raised.value.code == "KEY_AADHAAR_NOT_STORED"
