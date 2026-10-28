"""Lessons memory: proposed by the AI, decided by the owner, read through `read_guide("lessons")` once approved."""

from __future__ import annotations

from pathlib import Path

import pytest

from engine.doctor.guides import read_guide
from engine.doctor.lessons import (
    MAX_APPROVED,
    MAX_PROPOSED,
    MAX_WORDS,
    LessonError,
    LessonStore,
    approved_lessons,
    check_lesson,
)
from engine.storage import LocalStorage

GOOD = "Ask which date column is the order date before building any window."


def store(tmp_path: Path) -> LessonStore:
    return LessonStore(LocalStorage(tmp_path))


def test_a_proposal_waits_and_is_not_in_the_guide(tmp_path: Path) -> None:
    lessons = store(tmp_path)
    lesson = lessons.propose(GOOD, "case1")
    assert lesson.status == "proposed" and lesson.case_id == "case1"
    assert lessons.propose(GOOD).lesson_id == lesson.lesson_id and len(lessons.all()) == 1
    assert approved_lessons(LocalStorage(tmp_path)) == ()
    assert "Learned" not in "\n".join(
        read_guide("lessons", lessons=approved_lessons(LocalStorage(tmp_path)))[2]
    )


def test_approving_adds_it_to_the_guide_and_rejecting_does_not(tmp_path: Path) -> None:
    lessons = store(tmp_path)
    yes = lessons.propose(GOOD)
    no = lessons.propose("Always drop the first column of every file.")
    assert lessons.decide(yes.lesson_id, True, "owner").status == "approved"
    assert lessons.decide(no.lesson_id, False, "owner").status == "rejected"
    approved = approved_lessons(LocalStorage(tmp_path))
    assert approved == (GOOD,)
    text = "\n".join(read_guide("lessons", lessons=approved)[2])
    assert "## Rules" in text and "## Learned" in text and GOOD in text and "first column" not in text
    assert read_guide("lessons", "learned", lessons=approved)[1] == "Learned"


def test_a_decision_is_final_and_unknown_ids_are_refused(tmp_path: Path) -> None:
    lessons = store(tmp_path)
    lesson = lessons.propose(GOOD)
    lessons.decide(lesson.lesson_id, False, "owner")
    with pytest.raises(LessonError) as decided:
        lessons.decide(lesson.lesson_id, True, "owner")
    assert decided.value.code == "LESSON_DECIDED"
    with pytest.raises(LessonError) as unknown:
        lessons.decide("ls_nope", True, "owner")
    assert unknown.value.code == "LESSON_NOT_FOUND"


def test_the_approved_cap(tmp_path: Path) -> None:
    lessons = store(tmp_path)
    words = ["alpha", "bravo", "charlie", "delta", "echo", "foxtrot", "golf", "hotel", "india"]
    made = [lessons.propose(f"Check the {w} column first.") for w in words]
    for lesson in made[:MAX_APPROVED]:
        lessons.decide(lesson.lesson_id, True, "owner")
    with pytest.raises(LessonError) as over:
        lessons.decide(made[MAX_APPROVED].lesson_id, True, "owner")
    assert over.value.code == "LESSON_CAP"
    lessons.decide(made[MAX_APPROVED].lesson_id, False, "owner")  # a rejection still works


def test_the_proposal_queue_is_capped(tmp_path: Path) -> None:
    lessons = store(tmp_path)
    for i in range(MAX_PROPOSED):
        lessons.propose(f"Rule {'x' * i} stays general.".replace("x" * i, "q" * (i + 1)))
    with pytest.raises(LessonError) as full:
        lessons.propose("One more general rule.")
    assert full.value.code == "LESSON_QUEUE_FULL"


@pytest.mark.parametrize(
    "text",
    [
        "Customer 4521 returned 300 items in 2023.",  # numbers
        'Treat "Acme Corp" rows as test data.',  # a quoted cell or name
        "Email ops@acme.com when the file is late.",  # an address
        "See https://example.com for the rules.",  # a link
        " ".join(["word"] * (MAX_WORDS + 1)),  # too long
        "   ",
        None,
    ],
)
def test_the_filter_refuses_numbers_names_quotes_links_and_long_text(text: object, tmp_path: Path) -> None:
    with pytest.raises(LessonError) as refused:
        check_lesson(text)
    assert refused.value.code == "LESSON_REFUSED"
    with pytest.raises(LessonError):
        store(tmp_path).propose(text)
    assert store(tmp_path).all() == ()


def test_a_rule_id_is_allowed_and_spacing_is_tidied() -> None:
    assert check_lesson("  Cite R4  when you   drop rows. ") == "Cite R4 when you drop rows."


def test_the_guide_with_the_most_lessons_stays_inside_the_guides_limits() -> None:
    lessons = tuple(" ".join(["word"] * MAX_WORDS) for _ in range(MAX_APPROVED))
    lines = read_guide("lessons", lessons=lessons)[2]
    assert len(" ".join(lines).split()) <= 400
    learned = "\n".join(read_guide("lessons", "Learned", rules=False, lessons=lessons)[2])
    assert len(learned.split()) <= MAX_APPROVED * (MAX_WORDS + 1) + 2


def test_an_approved_lesson_can_be_retired_to_make_room(tmp_path: Path) -> None:
    lessons = store(tmp_path)
    words = ["alpha", "bravo", "charlie", "delta", "echo", "foxtrot", "golf", "hotel", "india"]
    made = [lessons.propose(f"Check the {w} column first.") for w in words]
    for lesson in made[:MAX_APPROVED]:
        lessons.decide(lesson.lesson_id, True, "owner")
    with pytest.raises(LessonError) as over:
        lessons.decide(made[MAX_APPROVED].lesson_id, True, "owner")
    assert over.value.code == "LESSON_CAP"
    retired = lessons.retire(made[0].lesson_id, "owner")
    assert retired.status == "rejected" and retired.decided_by == "owner"
    assert made[0].text not in approved_lessons(LocalStorage(tmp_path))
    assert lessons.decide(made[MAX_APPROVED].lesson_id, True, "owner").status == "approved"
    with pytest.raises(LessonError) as again:
        lessons.retire(made[0].lesson_id, "owner")  # only an approved lesson is retired
    assert again.value.code == "LESSON_NOT_APPROVED"
    with pytest.raises(LessonError) as unknown:
        lessons.retire("ls_nope", "owner")
    assert unknown.value.code == "LESSON_NOT_FOUND"


def test_a_bad_read_refuses_the_write_and_leaves_the_file(tmp_path: Path) -> None:
    from engine.doctor.lessons import LESSONS_KEY

    lessons = store(tmp_path)
    kept = lessons.propose(GOOD)
    lessons.decide(kept.lesson_id, True, "owner")
    path = LocalStorage(tmp_path).local_path(LESSONS_KEY)
    path.write_text("{not json", encoding="utf-8")
    with pytest.raises(ValueError):
        lessons.propose("Always ask what a negative amount means before netting it.")
    assert path.read_text(encoding="utf-8") == "{not json"
    assert lessons.all() == ()  # readers fall back; writers never do


def test_two_threads_keep_both_writes(tmp_path: Path) -> None:
    import threading

    lessons = store(tmp_path)
    first = lessons.propose(GOOD)
    second = lessons.propose("Always ask what a negative amount means before netting it.")
    barrier = threading.Barrier(2)

    def approve(lesson_id: str) -> None:
        barrier.wait()
        lessons.decide(lesson_id, True, "owner")

    threads = [threading.Thread(target=approve, args=(item.lesson_id,)) for item in (first, second)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert sorted(item.status for item in lessons.all()) == ["approved", "approved"]


def test_a_lesson_naming_the_case_is_refused() -> None:
    words = ["CustomerID", "invoices_2011.csv", "invoices_2011", "Acme Retail"]
    for text in (
        "Treat Acme Retail rows as test data.",
        "Read customerid as the key column.",
        "Treat invoices_2011 as the order file.",
    ):
        with pytest.raises(LessonError):
            check_lesson(text, words)
    assert check_lesson(GOOD, words) == GOOD


def test_a_lesson_is_capped_and_names_only_known_ids() -> None:
    from engine.doctor.lessons import MAX_LINE_CHARS

    assert check_lesson("Ask about refunds first, as R11 says.")
    with pytest.raises(LessonError):
        check_lesson("Ask about refunds first, as Q45 says.")
    with pytest.raises(LessonError):
        check_lesson(" ".join(["Supercalifragilisticexpialidocious"] * 6))
    assert len(" ".join(["Supercalifragilisticexpialidocious"] * 6)) > MAX_LINE_CHARS


def test_approved_lessons_reach_the_prompt_variables(tmp_path: Path) -> None:
    from engine.doctor.actions import prompt_variables

    lessons = store(tmp_path)
    lessons.decide(lessons.propose(GOOD).lesson_id, True, "owner")
    plain = prompt_variables("review")["guide_index"]
    taught = prompt_variables("review", lessons=approved_lessons(LocalStorage(tmp_path)))["guide_index"]
    assert GOOD not in plain and taught.startswith(plain) and GOOD in taught
