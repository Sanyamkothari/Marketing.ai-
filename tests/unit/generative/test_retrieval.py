"""`engine.generative.retrieval`: the two things an answer is not allowed to see.

Retrieval makes no model call, and both decisions it makes are decisions to leave something out, so
most of what is proved here is an absence: which chunks did not reach the prompt, and whether the
caller can tell why they did not.

**The floor is proved at its boundary and by its consequence.** A match sitting exactly on
`min_similarity` is evidence and one just below it is not, and when nothing clears it `Retrieved.empty`
is true - which is the refusal that costs nothing, because no model has been asked anything by the
time it is known. Every floor here is set by the test that needs it and never read off the shipped
configuration: 0.25 is calibrated for the Bedrock embedding model and means something else under any
other one (DEC-218), so a test that asserted the shipped number produced a particular outcome would be
pinning a number it does not own.

**`considered` and `above_floor` are proved to be two different numbers.** "Nothing was indexed" and
"nothing was close enough" are different failures with different fixes, and the case that separates
them - `considered` above zero while `above_floor` is zero - is asserted directly rather than inferred
from an empty list.

**The oversample is proved by what the store was asked for, not by what came back.** A recording store
keeps the `top_k` it was handed, and a passage sitting at rank five is then shown reaching a two-chunk
answer, which is the entire reason `OVERSAMPLE` is more than one.

**MMR is proved to keep its plain promise before its clever one.** The first pick is the best raw match
whatever `lambda_` is set to, because a reader who checks the top citation has to find the passage they
expected; only then is the trade itself proved, and with prose whose overlap is real - one policy worded
twice, against a paragraph on another subject - because the repetition term is a Jaccard over words and
invented text would make that arithmetic prove nothing.

Matches and chunks are hand-built and the store is a fake. Retrieval is arithmetic over similarities a
caller supplies; reaching it through a real index would put a chunker, an embedding model and a Parquet
round trip between the test and the thing under test, and every number here would then be a number
nobody chose. The cost is that the fake store has to stay honest, so it is asserted against the
`VectorStore` protocol rather than trusted.
"""

from __future__ import annotations

from collections.abc import Sequence

import pytest

from engine.config import RagConfig
from engine.generative.contracts import Chunk
from engine.generative.retrieval import OVERSAMPLE, mmr, retrieve, similarity_to
from engine.generative.vectorstore import Match, VectorStore

INDEX = "idx_20260101_000000_northwind"
QUESTION: tuple[float, ...] = (0.21, 0.94, 0.07, 0.33)
"""A question vector. Its values never matter: every similarity in this module is handed to the store."""

LATE_FEE = (
    "A late payment fee of 100 rupees or two percent of the outstanding amount, whichever is higher, "
    "is added seven days after the due date."
)
LATE_FEE_REWORDED = (
    "A late payment fee of 100 rupees or two percent of the outstanding amount, whichever is higher, "
    "is charged seven days after the bill due date."
)
LATE_FEE_IN_THE_FAQ = (
    "The late payment fee is 100 rupees or two percent of the outstanding amount, whichever is higher, "
    "and it is added seven days after the due date."
)
LATE_FEE_IN_THE_TABLE = (
    "Late payment adds a fee of 100 rupees or two percent of the outstanding amount, whichever is "
    "higher, seven days after the due date."
)
RECONNECTION = (
    "Reconnection after a suspension is automatic once the balance clears, and the line is usually live "
    "again within four hours."
)
PORTING = (
    "To port a number to another operator, send PORT to 1900 and quote the code that comes back at the "
    "new operator's store."
)


def chunk(text: str, *, document: str = "faq_billing.md", ordinal: int = 0) -> Chunk:
    """One hand-built chunk. Only its text, its document and its id are read by anything below."""
    return Chunk(
        chunk_id=f"{document}#{ordinal}",
        doc_id=document.removesuffix(".md"),
        document=document,
        section="Late payment and reconnection",
        ordinal=ordinal,
        tokens=len(text.split()),
        text=text,
    )


def match(similarity: float, text: str, *, document: str = "faq_billing.md", ordinal: int = 0) -> Match:
    """One scored chunk, as a store hands it back."""
    return Match(chunk=chunk(text, document=document, ordinal=ordinal), similarity=similarity)


def ranked(rows: Sequence[tuple[float, str]]) -> tuple[Match, ...]:
    """Candidates as a search returns them: most similar first, one chunk id per rank."""
    return tuple(match(similarity, text, ordinal=index) for index, (similarity, text) in enumerate(rows))


def rag(**overrides: object) -> RagConfig:
    """A configuration whose floor and trade this module owns rather than borrows (DEC-218).

    `mmr_lambda` defaults to 1.0 so that a test about the floor is not also a test about the trade.
    """
    settings: dict[str, object] = {"top_k": 2, "min_similarity": 0.5, "mmr_lambda": 1.0}
    settings.update(overrides)
    return RagConfig(**settings)


class RecordingStore:
    """A `VectorStore` that answers with a scripted ranking and remembers what it was asked for.

    `search` truncates to the `top_k` it is given, exactly as a real one does, so a test can watch a
    chunk fall out of the window instead of being told it did.
    """

    def __init__(self, matches: Sequence[Match]) -> None:
        self._matches = tuple(matches)
        self.asked_for: list[int] = []
        self.queries: list[tuple[float, ...]] = []

    def write(self, index_id: str, chunks: Sequence[Chunk], vectors: Sequence[Sequence[float]]) -> None:
        raise NotImplementedError("retrieval never writes")

    def chunks(self, index_id: str) -> tuple[Chunk, ...]:
        return tuple(hit.chunk for hit in self._matches)

    def exists(self, index_id: str) -> bool:
        return True

    def search(self, index_id: str, query: Sequence[float], *, top_k: int) -> tuple[Match, ...]:
        self.asked_for.append(top_k)
        self.queries.append(tuple(query))
        return self._matches[:top_k]


def test_the_store_these_tests_use_is_the_protocol_the_engine_ships() -> None:
    """A spy that had drifted from `VectorStore` would prove nothing about the real one."""
    assert isinstance(RecordingStore(()), VectorStore)


# ---------------------------------------------------------------------------
# The similarity floor
# ---------------------------------------------------------------------------
def test_a_match_below_the_floor_is_dropped_and_one_exactly_on_it_is_kept() -> None:
    """The comparison is `>=`, so the chunk that scores the floor itself is evidence and not noise."""
    store = RecordingStore(ranked([(0.90, LATE_FEE), (0.50, RECONNECTION), (0.49, PORTING)]))
    result = retrieve(store, INDEX, QUESTION, config=rag(top_k=3, min_similarity=0.5))
    assert [found.similarity for found in result.matches] == [0.90, 0.50]
    assert result.above_floor == 2
    assert not result.empty
    assert PORTING not in [passage.text for passage in result.chunks]


def test_when_nothing_is_close_enough_there_is_nothing_to_answer_from() -> None:
    """The refusal this module exists to make: an empty result, reached without calling a model."""
    store = RecordingStore(ranked([(0.20, LATE_FEE), (0.11, RECONNECTION)]))
    result = retrieve(store, INDEX, QUESTION, config=rag(min_similarity=0.5))
    assert result.empty
    assert result.matches == ()
    assert result.chunks == ()
    assert (result.considered, result.above_floor) == (2, 0)


def test_nothing_indexed_and_nothing_close_enough_are_told_apart_by_the_two_counts() -> None:
    """A caller that saw only an empty list could not tell the two failures apart, or what to fix."""
    nothing = retrieve(RecordingStore(()), INDEX, QUESTION, config=rag())
    distant = retrieve(RecordingStore(ranked([(0.10, LATE_FEE)])), INDEX, QUESTION, config=rag())
    assert nothing.empty
    assert distant.empty
    assert (nothing.considered, nothing.above_floor) == (0, 0)
    assert (distant.considered, distant.above_floor) == (1, 0)


def test_the_floor_that_was_applied_is_carried_whether_or_not_it_dropped_anything() -> None:
    """An answer records the floor it was actually subject to, not the one a reader assumes."""
    store = RecordingStore(ranked([(0.90, LATE_FEE)]))
    assert retrieve(store, INDEX, QUESTION, config=rag(min_similarity=0.5)).floor == 0.5
    assert retrieve(store, INDEX, QUESTION, config=rag(min_similarity=0.8)).floor == 0.8


def test_the_floor_comes_before_the_trade_so_a_distant_passage_is_never_bought_back_by_being_different() -> (
    None
):
    """Same candidates, same trade, only the floor moved - and the second chunk changes with it."""
    store = RecordingStore(ranked([(0.90, LATE_FEE), (0.88, LATE_FEE_REWORDED), (0.30, PORTING)]))
    high = retrieve(store, INDEX, QUESTION, config=rag(top_k=2, min_similarity=0.5, mmr_lambda=0.5))
    low = retrieve(store, INDEX, QUESTION, config=rag(top_k=2, min_similarity=0.25, mmr_lambda=0.5))
    assert [passage.text for passage in high.chunks] == [LATE_FEE, LATE_FEE_REWORDED]
    assert [passage.text for passage in low.chunks] == [LATE_FEE, PORTING]
    assert (high.above_floor, low.above_floor) == (2, 3)


# ---------------------------------------------------------------------------
# The oversample
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("top_k", [1, 3, 6])
def test_the_store_is_asked_for_oversample_times_the_top_k(top_k: int) -> None:
    """MMR can only choose among what it is given, so the window is wider than the answer."""
    store = RecordingStore(ranked([(0.90, LATE_FEE)]))
    retrieve(store, INDEX, QUESTION, config=rag(top_k=top_k))
    assert store.asked_for == [top_k * OVERSAMPLE]
    assert OVERSAMPLE > 1


def test_the_question_reaches_the_store_exactly_as_it_was_embedded() -> None:
    """The question is embedded bare and searched bare; retrieval puts nothing in front of it (DEC-217)."""
    store = RecordingStore(ranked([(0.90, LATE_FEE)]))
    retrieve(store, INDEX, QUESTION, config=rag())
    assert store.queries == [QUESTION]


def test_the_oversample_is_what_lets_a_lower_ranked_passage_into_a_short_answer() -> None:
    """Rank five cannot be chosen by a search that was only asked for two, which is the whole point."""
    store = RecordingStore(
        ranked(
            [
                (0.90, LATE_FEE),
                (0.88, LATE_FEE_REWORDED),
                (0.86, LATE_FEE_IN_THE_FAQ),
                (0.84, LATE_FEE_IN_THE_TABLE),
                (0.60, RECONNECTION),
            ]
        )
    )
    result = retrieve(store, INDEX, QUESTION, config=rag(top_k=2, min_similarity=0.5, mmr_lambda=0.5))
    assert store.asked_for == [2 * OVERSAMPLE]
    assert [passage.text for passage in result.chunks] == [LATE_FEE, RECONNECTION]


def test_considered_counts_what_the_search_returned_and_not_what_the_index_holds() -> None:
    """`considered` is a statement about the window, so a bigger corpus does not make it bigger."""
    store = RecordingStore(ranked([(0.90 - index / 100, LATE_FEE) for index in range(20)]))
    result = retrieve(store, INDEX, QUESTION, config=rag(top_k=2, min_similarity=0.0))
    assert result.considered == 2 * OVERSAMPLE
    assert result.above_floor == 2 * OVERSAMPLE
    assert len(result.matches) == 2


# ---------------------------------------------------------------------------
# The documents filter
# ---------------------------------------------------------------------------
def test_the_documents_filter_narrows_the_answer_to_the_named_files() -> None:
    """One knowledge base per product line is one index and a filter, not one index per line."""
    store = RecordingStore(
        (
            match(0.90, LATE_FEE, document="faq_billing.md", ordinal=0),
            match(0.80, RECONNECTION, document="plans_prepaid.md", ordinal=0),
            match(0.70, PORTING, document="faq_billing.md", ordinal=1),
        )
    )
    result = retrieve(
        store, INDEX, QUESTION, config=rag(top_k=3, min_similarity=0.5), documents=["plans_prepaid.md"]
    )
    assert [passage.document for passage in result.chunks] == ["plans_prepaid.md"]
    assert result.above_floor == 1


def test_the_filter_is_applied_after_the_search_which_is_what_a_store_with_no_query_language_costs() -> None:
    """The store is asked the same question either way, so a named document can be squeezed out.

    `considered` therefore counts the whole window while `above_floor` counts only what the filter
    left, which is the asymmetry a caller reading the two numbers has to know about.
    """
    crowd = ranked([(0.90 - index / 100, LATE_FEE) for index in range(2 * OVERSAMPLE)])
    wanted = (match(0.40, RECONNECTION, document="plans_prepaid.md"),)
    store = RecordingStore(crowd + wanted)
    result = retrieve(
        store, INDEX, QUESTION, config=rag(top_k=2, min_similarity=0.0), documents=["plans_prepaid.md"]
    )
    assert store.asked_for == [2 * OVERSAMPLE]
    assert "plans_prepaid.md" in {passage.document for passage in store.chunks(INDEX)}
    assert result.empty
    assert (result.considered, result.above_floor) == (2 * OVERSAMPLE, 0)


def test_an_empty_documents_list_allows_nothing_where_none_allows_everything() -> None:
    """`None` is the sentinel for "search everything"; an empty list is a filter that names no file."""
    store = RecordingStore(ranked([(0.90, LATE_FEE)]))
    assert retrieve(store, INDEX, QUESTION, config=rag(), documents=None).above_floor == 1
    assert retrieve(store, INDEX, QUESTION, config=rag(), documents=()).above_floor == 0


# ---------------------------------------------------------------------------
# What reaches the prompt
# ---------------------------------------------------------------------------
def test_the_chunks_come_back_in_the_order_the_prompt_will_number_them() -> None:
    """Citation 1 is the first match MMR chose, which is not the store's order once it has traded."""
    store = RecordingStore(ranked([(0.90, LATE_FEE), (0.88, LATE_FEE_REWORDED), (0.60, RECONNECTION)]))
    result = retrieve(store, INDEX, QUESTION, config=rag(top_k=3, min_similarity=0.5, mmr_lambda=0.5))
    assert result.chunks == tuple(found.chunk for found in result.matches)
    assert [passage.text for passage in result.chunks] == [LATE_FEE, RECONNECTION, LATE_FEE_REWORDED]
    assert [found.similarity for found in result.matches] == [0.90, 0.60, 0.88]


def test_no_more_than_top_k_chunks_ever_reach_the_prompt() -> None:
    """The window is four times the answer, and the prompt is given the answer."""
    store = RecordingStore(ranked([(0.90 - index / 100, LATE_FEE) for index in range(12)]))
    result = retrieve(store, INDEX, QUESTION, config=rag(top_k=3, min_similarity=0.0, mmr_lambda=0.7))
    assert len(result.matches) == 3
    assert result.above_floor == 12


# ---------------------------------------------------------------------------
# The trade itself
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("lambda_", [0.0, 0.3, 0.7, 1.0])
def test_the_first_pick_is_always_the_best_raw_match(lambda_: float) -> None:
    """A reader who checks the top citation must find the passage they expected, whatever the trade."""
    candidates = ranked([(0.90, LATE_FEE), (0.40, RECONNECTION), (0.35, PORTING)])
    assert mmr(candidates, QUESTION, top_k=3, lambda_=lambda_)[0] == candidates[0]


@pytest.mark.parametrize("lambda_", [1.0, 1.5])
def test_a_lambda_of_one_or_more_is_pure_relevance_and_does_no_trading_at_all(lambda_: float) -> None:
    """At lambda 1 the repetition term is worth nothing, so the answer is the store's order, truncated."""
    candidates = ranked([(0.90, LATE_FEE), (0.88, LATE_FEE_REWORDED), (0.60, RECONNECTION)])
    assert mmr(candidates, QUESTION, top_k=2, lambda_=lambda_) == candidates[:2]


def test_a_lower_lambda_prefers_another_subject_to_a_second_wording_of_the_first() -> None:
    """Same candidates in the same order: only `lambda_` decides what the second chunk is."""
    candidates = ranked([(0.90, LATE_FEE), (0.88, LATE_FEE_REWORDED), (0.60, RECONNECTION)])
    relevance = mmr(candidates, QUESTION, top_k=2, lambda_=1.0)
    diversity = mmr(candidates, QUESTION, top_k=2, lambda_=0.5)
    assert relevance[1].chunk.text == LATE_FEE_REWORDED
    assert diversity[1].chunk.text == RECONNECTION


def test_an_empty_candidate_list_is_an_empty_answer() -> None:
    assert mmr((), QUESTION, top_k=5, lambda_=0.7) == ()


@pytest.mark.parametrize("top_k", [0, -1])
def test_asking_for_no_chunks_returns_no_chunks(top_k: int) -> None:
    """A caller that computed its way to zero slots gets nothing back rather than a first pick."""
    assert mmr(ranked([(0.90, LATE_FEE)]), QUESTION, top_k=top_k, lambda_=0.7) == ()


@pytest.mark.parametrize("lambda_", [0.0, 0.5, 0.7, 1.0])
@pytest.mark.parametrize("top_k", [1, 2, 4, 9])
def test_the_trade_returns_at_most_top_k_candidates_and_never_one_of_them_twice(
    top_k: int, lambda_: float
) -> None:
    """A chunk quoted twice in a prompt is a slot the second fact needed, at every setting."""
    candidates = ranked(
        [
            (0.90, LATE_FEE),
            (0.88, LATE_FEE_REWORDED),
            (0.86, LATE_FEE_IN_THE_FAQ),
            (0.60, RECONNECTION),
            (0.55, PORTING),
        ]
    )
    chosen = mmr(candidates, QUESTION, top_k=top_k, lambda_=lambda_)
    assert len(chosen) == min(top_k, len(candidates))
    assert len({found.chunk.chunk_id for found in chosen}) == len(chosen)
    assert all(found in candidates for found in chosen)


def test_a_second_wording_is_still_returned_once_the_answer_has_room_for_it() -> None:
    """MMR re-ranks and does not de-duplicate: three slots and three wordings of one policy fill three."""
    candidates = ranked([(0.90, LATE_FEE), (0.88, LATE_FEE_REWORDED), (0.86, LATE_FEE_IN_THE_FAQ)])
    assert len(mmr(candidates, QUESTION, top_k=3, lambda_=0.2)) == 3


def test_a_passage_with_no_words_in_it_repeats_nothing() -> None:
    """A Jaccard over an empty word set is 0, so a blank chunk looks maximally diverse.

    Which is worth knowing rather than worth fixing here: what keeps a blank chunk out of an answer is
    the floor, because a chunk with no content word embeds to zero and matches nothing.
    """
    candidates = ranked([(0.90, LATE_FEE), (0.88, LATE_FEE_REWORDED), (0.30, "")])
    assert mmr(candidates, QUESTION, top_k=2, lambda_=0.5)[1].chunk.text == ""


def test_a_tie_is_broken_by_the_stores_ranking_so_two_answers_to_one_question_agree() -> None:
    """One policy quoted in two documents scores the same twice; the higher-ranked copy wins, always."""
    candidates = (
        match(0.90, LATE_FEE, document="faq_billing.md", ordinal=0),
        match(0.50, RECONNECTION, document="faq_billing.md", ordinal=1),
        match(0.50, RECONNECTION, document="plans_prepaid.md", ordinal=0),
    )
    chosen = mmr(candidates, QUESTION, top_k=2, lambda_=0.5)
    assert chosen[1].chunk.document == "faq_billing.md"
    assert mmr(candidates, QUESTION, top_k=2, lambda_=0.5) == chosen


def test_similarity_to_is_the_stores_own_arithmetic_and_a_zero_vector_matches_nothing() -> None:
    """Re-exported so a caller need not know which module owns it, including the zero-length case."""
    assert similarity_to((1.0, 0.0), (1.0, 0.0)) == pytest.approx(1.0)
    assert similarity_to((1.0, 0.0), (0.0, 1.0)) == pytest.approx(0.0)
    assert similarity_to((0.0, 0.0), (1.0, 0.0)) == 0.0
