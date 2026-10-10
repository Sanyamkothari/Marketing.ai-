"""Reading abbreviated and romanised-Hindi headers (Plan K M142, DEC-1409): the rules alone.

Through the public entry points: `rank_roles` on a profile built the way an upload's is, with the
shipped `configs/agent/header_lexicon.yaml`. The scoring rule is the one in the lexicon's header and
`engine/agent/headers.py`; the numbers below are that rule worked by hand.
"""

from __future__ import annotations

from pathlib import Path

import pandas as pd
import pytest
import yaml

from engine.agent.headers import (
    ROLES,
    TOP_N,
    VALUE_FLOOR,
    HeaderCandidate,
    HeaderLexiconError,
    activity_dates,
    load_lexicon,
    meanings_of,
    rank_roles,
    reorder_within,
    tokenize,
)
from tests.unit.agent.helpers import context_for

LEXICON = Path(__file__).resolve().parents[3] / "configs" / "agent" / "header_lexicon.yaml"


def _frame(rows: int = 400) -> pd.DataFrame:
    return pd.DataFrame(
        {
            "grahak_id": [f"G{i}" for i in range(rows)],
            "cust_mob_no": [9_000_000_000 + 7 * i for i in range(rows)],
            "kharida_30d": [i % 2 for i in range(rows)],
            "tareekh": [("2026-09-01", "2026-09-08", "2026-09-15")[i % 3] for i in range(rows)],
            "sahmati": [i % 2 for i in range(rows)],
            "dnd_flag": [i % 2 for i in range(rows)],
            "pichhla_sampark": [f"2026-08-{1 + i % 28:02d}" for i in range(rows)],
            "last_rchg_dt": [f"2026-07-{1 + i % 28:02d}" for i in range(rows)],
            "kul_kharch": [float(i) for i in range(rows)],
            "repeat_id": [i % 5 for i in range(rows)],
        }
    )


def _ranked(
    frame: pd.DataFrame | None = None, extra_anchors: dict[str, list[str]] | None = None
) -> dict[str, tuple[HeaderCandidate, ...]]:
    ctx = context_for(_frame() if frame is None else frame)
    return rank_roles(ctx.profile.columns, rows=ctx.profile.row_count, extra_anchors=extra_anchors)


def _top(role: str, frame: pd.DataFrame | None = None) -> list[tuple[str, float]]:
    return [(c.column, c.score) for c in _ranked(frame)[role]]


@pytest.mark.parametrize(
    ("header", "words"),
    [
        ("GrahakID", ("grahak", "id")),
        ("kharida_30d", ("kharida", "d")),
        ("Last-Contact Date", ("last", "contact", "date")),
        ("lastContactedAt", ("last", "contacted", "at")),
        ("CUST_MOB_NO", ("cust", "mob", "no")),
        ("avg_mthly_spnd", ("avg", "mthly", "spnd")),
        ("ग्राहक_आईडी", ("ग्राहक", "आईडी")),
        ("mkt_optin2", ("mkt", "optin")),
    ],
)
def test_a_header_is_cut_into_words(header: str, words: tuple[str, ...]) -> None:
    assert tokenize(header) == words


@pytest.mark.parametrize(
    ("header", "meanings"),
    [
        ("cust_mob_no", ("customer", "mobile", "number")),
        ("last_rchg_dt", ("last", "recharge", "date")),
        ("pehla_order", ("first", "order")),
        ("kul_kharch", ("total", "spend")),
        ("grahak_id", ("customer", "id")),
        ("avg_mthly_spnd", ("avg", "month", "spend")),
        ("marketing_opt_in", ("marketing", "optin")),
        ("do_not_contact", ("optout",)),
        ("contact_no", ("mobile",)),
        ("ग्राहक_आईडी", ("customer", "id")),
    ],
)
def test_the_lexicon_gives_the_documented_meanings(header: str, meanings: tuple[str, ...]) -> None:
    """The examples in the milestone: mob -> mobile, rchg -> recharge, dt -> date, pehla -> first, kul -> total."""
    assert meanings_of(header) == meanings


@pytest.mark.parametrize(
    ("header", "meanings"),
    [
        ("custmobno", ("customer", "mobile", "number")),
        ("lastcontactdate", ("last", "contact", "date")),
        ("custid", ("customer", "id")),
    ],
)
def test_a_run_on_header_is_cut_into_known_words(header: str, meanings: tuple[str, ...]) -> None:
    assert meanings_of(header) == meanings


def test_an_unknown_word_is_kept_as_itself_and_never_guessed() -> None:
    assert meanings_of("xyzzy_qq") == ("word:xyzzy", "word:qq")
    assert meanings_of("addressline") == ("word:addressline",)


def test_scores_follow_the_documented_rule() -> None:
    """name score = strongest anchor + boosters (at most +0.4) - blockers; values then scale it."""
    assert _top("id") == [("grahak_id", 1.0), ("cust_mob_no", 0.85), ("repeat_id", 0.28)]
    # grahak_id: id 0.7 + customer 0.3. cust_mob_no: mobile 0.45 + customer 0.3 + number 0.15 -> capped boost 0.4.
    # repeat_id: id 0.7 on a column that repeats: 0.7 x (0.4 + 0.6 x 0) = 0.28.
    assert _top("outcome") == [("kharida_30d", 0.8)]  # purchase 0.7 + days 0.1
    assert _top("date") == [
        ("tareekh", 0.6)
    ]  # date 0.6; the last-recharge date is blocked by last and recharge
    assert _top("consent") == [("sahmati", 0.8)]
    assert _top("opt_out") == [("dnd_flag", 0.9)]  # optout 0.8 + flag 0.1
    assert _top("last_contact") == [("pichhla_sampark", 0.85)]  # contact 0.55 + last 0.3


def test_values_lower_a_score_but_never_create_one() -> None:
    frame = _frame().assign(consent_but_many=[i % 7 for i in range(400)], plain=[i % 2 for i in range(400)])
    by_name = {c.column: c for c in _ranked(frame)["consent"]}
    assert by_name["consent_but_many"].score == round(0.8 * VALUE_FLOOR, 2)  # seven values: only the floor
    assert "plain" not in by_name  # no word of consent in the header: two values are not enough
    assert by_name["sahmati"].score == 0.8


def test_the_short_list_is_best_first_with_ties_in_file_order() -> None:
    frame = pd.DataFrame(
        {"b_flag": [0, 1] * 50, "a_flag": [0, 1] * 50, "opt_out_x": [0, 1] * 50, "optout": [0, 1] * 50}
    )
    found = [c.column for c in _ranked(frame)["opt_out"]]
    assert found == ["opt_out_x", "optout"]  # both 0.8: the one earlier in the file first
    assert len(found) <= TOP_N


def test_a_role_no_column_fits_is_empty_never_a_guess() -> None:
    frame = pd.DataFrame({"city": ["a", "b"] * 50, "spend": range(100)})
    assert all(found == () for found in _ranked(frame).values())


def test_every_role_is_always_answered() -> None:
    assert set(_ranked()) == set(ROLES)


def test_headers_in_devanagari_are_read_too() -> None:
    """They are not benchmark files (the egress gate aliases a header that is not a plain label), but the
    rules read them: ग्राहक = customer, आईडी = id, तारीख = date, सहमति = consent."""
    frame = pd.DataFrame(
        {
            "ग्राहक_आईडी": [f"G{i}" for i in range(100)],
            "तारीख": [("2026-09-01", "2026-09-08", "2026-09-15")[i % 3] for i in range(100)],
            "सहमति": [i % 2 for i in range(100)],
            "खर्च": [float(i) for i in range(100)],
        }
    )
    found = _ranked(frame)
    assert [c.column for c in found["id"]] == ["ग्राहक_आईडी"]
    assert [c.column for c in found["date"]] == ["तारीख"]
    assert [c.column for c in found["consent"]] == ["सहमति"]
    assert found["outcome"] == ()


def test_the_use_cases_own_outcome_words_count_as_anchors_but_a_letter_does_not() -> None:
    frame = pd.DataFrame({"ordered_again": [0, 1] * 50, "visits_7d": [0, 1] * 50, "other": [0, 1] * 50})
    plain = {c.column for c in _ranked(frame)["outcome"]}
    assert plain == set()
    with_words = {
        c.column
        for c in _ranked(frame, extra_anchors={"outcome": ["ordered_again", "converted_30d"]})["outcome"]
    }
    assert with_words == {"ordered_again"}  # `d` from `converted_30d` is too short to anchor `visits_7d`


def test_a_blocker_counts_against_a_header_but_never_the_anchor_it_chose() -> None:
    frame = pd.DataFrame(
        {"contact_email": ["a@x.test", "b@x.test"] * 50, "last_email_dt": ["2026-08-01", "2026-08-02"] * 50}
    )
    found = {c.column: c.score for c in _ranked(frame)["last_contact"]}
    assert "contact_email" not in found  # contact 0.55 against email 0.5: 0.05, not listed
    assert (
        found["last_email_dt"] == 0.9
    )  # 0.5 + boosters capped at 0.4; email is the anchor, so it is not also a blocker


def test_activity_dates_name_the_activity_and_put_last_first() -> None:
    dates = _frame()["tareekh"]
    ctx = context_for(_frame().assign(pehla_order=dates, order_dt=dates, plain_dt=dates, expiry_dt=dates))
    found = activity_dates(ctx.profile.columns)
    # The last recharge comes first; a first order (pehla_order) and an expiry date are not "last active".
    assert found == (("last_rchg_dt", "recharge"), ("order_dt", "order"))


def test_reorder_within_never_adds_a_column() -> None:
    candidates = tuple(HeaderCandidate(name, "id", 0.9 - i / 10, "high", ()) for i, name in enumerate("abc"))
    assert [c.column for c in reorder_within(candidates, ["c", "z", "a", "c"])] == ["c", "a", "b"]
    assert [c.column for c in reorder_within(candidates, ["zzz"])] == ["a", "b", "c"]
    assert [c.column for c in reorder_within(candidates, [])] == ["a", "b", "c"]
    assert {c.column for c in reorder_within(candidates, ["q", "r", "s"])} <= {"a", "b", "c"}


# ---------------------------------------------------------------------------
# The lexicon file
# ---------------------------------------------------------------------------
def _write(tmp_path: Path, change: dict[str, object]) -> Path:
    document = yaml.safe_load(LEXICON.read_text(encoding="utf-8"))
    document.update(change)
    target = tmp_path / "agent"
    target.mkdir()
    (target / "header_lexicon.yaml").write_text(
        yaml.safe_dump(document, allow_unicode=True), encoding="utf-8"
    )
    return tmp_path


def test_the_shipped_lexicon_validates() -> None:
    lexicon = load_lexicon()
    assert set(lexicon.roles) == set(ROLES)
    assert lexicon.label_drafts.windows_days == (30, 60, 90)
    assert lexicon.label_drafts.grace_days == 7


def test_a_root_can_carry_its_own_lexicon(tmp_path: Path) -> None:
    document = yaml.safe_load(LEXICON.read_text(encoding="utf-8"))
    document["meanings"]["mobile"] = [w for w in document["meanings"]["mobile"] if w != "mob"]
    root = _write(tmp_path, {"meanings": document["meanings"]})
    assert meanings_of("cust_mob_no") == ("customer", "mobile", "number")
    assert meanings_of("cust_mob_no", root) == ("customer", "word:mob", "number")


def test_a_root_without_a_lexicon_uses_the_shipped_one(tmp_path: Path) -> None:
    assert meanings_of("grahak_id", tmp_path) == ("customer", "id")


def test_a_word_listed_under_two_meanings_is_refused(tmp_path: Path) -> None:
    document = yaml.safe_load(LEXICON.read_text(encoding="utf-8"))
    document["meanings"]["date"] = [*document["meanings"]["date"], "mob"]
    root = _write(tmp_path, {"meanings": document["meanings"]})
    with pytest.raises(HeaderLexiconError, match="'mob' is listed under both"):
        load_lexicon(root)


def test_a_role_that_names_a_meaning_without_words_is_refused(tmp_path: Path) -> None:
    document = yaml.safe_load(LEXICON.read_text(encoding="utf-8"))
    document["roles"]["id"]["anchors"] = {"nothing": 0.5}
    root = _write(tmp_path, {"roles": document["roles"]})
    with pytest.raises(HeaderLexiconError, match="no words"):
        load_lexicon(root)


def test_a_missing_role_is_refused(tmp_path: Path) -> None:
    document = yaml.safe_load(LEXICON.read_text(encoding="utf-8"))
    del document["roles"]["opt_out"]
    root = _write(tmp_path, {"roles": document["roles"]})
    with pytest.raises(HeaderLexiconError, match="roles must be exactly"):
        load_lexicon(root)


def test_an_upper_case_word_is_refused(tmp_path: Path) -> None:
    document = yaml.safe_load(LEXICON.read_text(encoding="utf-8"))
    document["meanings"]["id"] = [*document["meanings"]["id"], "NewID"]
    root = _write(tmp_path, {"meanings": document["meanings"]})
    with pytest.raises(HeaderLexiconError, match="lower case"):
        load_lexicon(root)
