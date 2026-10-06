"""`engine.generative.vectorstore`: the arithmetic under a search, and the contract over it.

Almost every vector here is chosen by hand rather than embedded from the synthetic corpus, and that
is deliberate. A test that embeds real documents can only assert that one chunk beat another; a test
over `[1, 0]` and `[0, 1]` can assert the number itself, which is the thing `retrieval` then applies
a floor to. The cost accepted is that nothing in this module says whether the shipped
`min_similarity` is a sensible floor - that is a question about an embedding model rather than about
a dot product, and DEC-218 puts it where it belongs.

Four claims the module makes about itself, and what each is worth proving:

**A search returns similarities, not ranks.** Every score a search hands back is checked against
`cosine` of the same two vectors as they were written, before any normalisation. A store that
returned raw dot products would pass a rank-shaped test and quietly make the longest vector the best
match, so the scale-free property is asserted with vectors of deliberately different magnitudes.

**A zero vector matches nothing.** A chunk with no content word in it embeds to zero, and the
honest cosine for "nothing to match on" is 0.0 - not `NaN`, not a division by zero, and not a
position at the top of an arbitrary ordering. Both directions are proved: a zero chunk sitting among
real ones sorts below all of them, and a zero question scores every chunk at exactly 0.0 rather than
failing.

**A chunk survives Parquet unchanged.** Including `page`, which Parquet has no nullable integer for:
a `None` goes in, a `NaN` is stored, and a `None` has to come back out. The round trip is asserted
over a mixture of paged and unpaged chunks written out of id order, because restoring the absence
and preserving the order are two different things and only one of them is obvious.

**Half an index is not an index.** The chunks and the vectors are two files, which is the whole
reason a citation does not pay for a float array, and the consequence is that either can be present
without the other. `exists` is therefore true only when both are, and an index missing one of them
raises rather than searching whatever survived.

The error paths are split the same way the engine splits them everywhere else, and that split is
asserted rather than assumed: a caller's own mistake - lengths that are not parallel, a `top_k`
below one, a query of the wrong width - is a `ValueError` at the call site, while a fact about the
stored data is a coded `GenerativeError` that a screen can render and an HTTP status can be mapped
from.
"""

from __future__ import annotations

import math
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from engine.generative.contracts import CHUNKS_FILENAME, EMBEDDINGS_FILENAME, Chunk, ChunkEmbedding
from engine.generative.errors import GenerativeError
from engine.generative.vectorstore import (
    BM25_B,
    BM25_K1,
    CHUNK_COLUMNS,
    EMBEDDING_COLUMNS,
    LocalVectorStore,
    Match,
    VectorStore,
    bm25_corpus,
    bm25_scores,
    bm25_tokens,
    chunk_ids,
    cosine,
)
from engine.storage import LocalStorage, index_key
from engine.utils.ids import new_index_id

INDEX = new_index_id()
"""One index id for the whole module; each test gets its own `tmp_path`, so they cannot collide."""


@pytest.fixture
def storage(tmp_path: Path) -> LocalStorage:
    return LocalStorage(tmp_path)


@pytest.fixture
def store(storage: LocalStorage) -> LocalVectorStore:
    return LocalVectorStore(storage)


def chunk(ordinal: int, *, page: int | None = 1, text: str = "Bills are issued monthly.") -> Chunk:
    """One chunk of a billing FAQ, with everything but its position and its page held constant."""
    return Chunk(
        chunk_id=f"c{ordinal}",
        doc_id="faq_billing",
        document="faq_billing.md",
        section="Billing",
        ordinal=ordinal,
        page=page,
        tokens=8,
        text=text,
    )


def similarities(matches: tuple[Match, ...]) -> tuple[float, ...]:
    return tuple(match.similarity for match in matches)


def ids(matches: tuple[Match, ...]) -> tuple[str, ...]:
    return tuple(match.chunk.chunk_id for match in matches)


# ---------------------------------------------------------------------------
# The round trip
# ---------------------------------------------------------------------------
def test_the_chunks_written_come_back_identical_and_in_written_order(store) -> None:
    """Written order is reading order, and reading order is what a citation's ordinal means."""
    written = (chunk(2), chunk(0), chunk(1))
    store.write(INDEX, written, [[1.0, 0.0], [0.0, 1.0], [1.0, 1.0]])
    assert store.chunks(INDEX) == written


def test_a_chunk_with_no_page_comes_back_with_none_rather_than_a_nan(store) -> None:
    """Parquet has no nullable integer; the contract says `int | None` and the store has to mean it."""
    store.write(INDEX, [chunk(0, page=7), chunk(1, page=None)], [[1.0, 0.0], [0.0, 1.0]])
    pages = [read.page for read in store.chunks(INDEX)]
    assert pages == [7, None]
    assert not any(isinstance(page, float) for page in pages)


def test_a_whole_index_of_unpaged_chunks_still_reads_back(store) -> None:
    """A column that is null from top to bottom is the case a `notna` mask is easiest to get wrong."""
    store.write(INDEX, [chunk(0, page=None), chunk(1, page=None)], [[1.0, 0.0], [0.0, 1.0]])
    assert [read.page for read in store.chunks(INDEX)] == [None, None]


def test_a_second_write_replaces_the_index_rather_than_adding_to_it(store) -> None:
    store.write(INDEX, [chunk(0), chunk(1)], [[1.0, 0.0], [0.0, 1.0]])
    store.write(INDEX, [chunk(9)], [[1.0, 1.0]])
    assert chunk_ids(store.chunks(INDEX)) == ("c9",)


# ---------------------------------------------------------------------------
# Two files, and what follows from there being two
# ---------------------------------------------------------------------------
def test_a_write_produces_one_file_of_chunks_and_one_of_vectors(store, storage) -> None:
    """The split is the reason a citation does not pay to load a float array it will not look at."""
    store.write(INDEX, [chunk(0)], [[1.0, 0.0]])
    assert set(storage.list_keys()) == {
        index_key(INDEX, CHUNKS_FILENAME),
        index_key(INDEX, EMBEDDINGS_FILENAME),
    }


def test_an_index_exists_only_when_both_of_its_files_do(store, storage) -> None:
    """Half an index answers no question, so `exists` must not call it one."""
    assert not store.exists(INDEX)
    store.write(INDEX, [chunk(0)], [[1.0, 0.0]])
    assert store.exists(INDEX)
    storage.delete(index_key(INDEX, EMBEDDINGS_FILENAME))
    assert not store.exists(INDEX)


def test_the_chunks_can_be_read_with_the_vectors_gone_and_a_search_then_refuses(store, storage) -> None:
    """Reading a citation touches one file; a search that has lost the other says so rather than guessing."""
    store.write(INDEX, [chunk(0)], [[1.0, 0.0]])
    storage.delete(index_key(INDEX, EMBEDDINGS_FILENAME))
    assert chunk_ids(store.chunks(INDEX)) == ("c0",)
    with pytest.raises(GenerativeError) as error:
        store.search(INDEX, [1.0, 0.0], top_k=1)
    assert error.value.code == "INDEX_NOT_FOUND"


def test_the_columns_of_both_files_are_the_contracts_they_were_taken_from(store, storage) -> None:
    """A field added to `Chunk` has to reach the file, or a rebuilt index would silently drop it."""
    store.write(INDEX, [chunk(0)], [[1.0, 0.0]])
    chunks_frame = pd.read_parquet(storage.local_path(index_key(INDEX, CHUNKS_FILENAME)))
    vectors_frame = pd.read_parquet(storage.local_path(index_key(INDEX, EMBEDDINGS_FILENAME)))
    assert tuple(chunks_frame.columns) == CHUNK_COLUMNS == tuple(Chunk.model_fields)
    assert tuple(vectors_frame.columns) == EMBEDDING_COLUMNS
    assert set(EMBEDDING_COLUMNS) <= set(ChunkEmbedding.model_fields)


def test_a_row_of_either_file_validates_as_the_model_the_registry_names(store, storage) -> None:
    """`GENERATIVE_TABULAR_SCHEMAS` promises these two files validate; a column rename would break it."""
    store.write(INDEX, [chunk(0, page=None)], [[1.5, 0.0]])
    chunks_frame = pd.read_parquet(storage.local_path(index_key(INDEX, CHUNKS_FILENAME)))
    vectors_frame = pd.read_parquet(storage.local_path(index_key(INDEX, EMBEDDINGS_FILENAME)))
    assert Chunk.model_validate(chunks_frame.to_dict(orient="records")[0]).chunk_id == "c0"
    embedding = ChunkEmbedding.model_validate(vectors_frame.to_dict(orient="records")[0])
    assert embedding.chunk_id == "c0"
    assert embedding.embedding == (1.5, 0.0)


# ---------------------------------------------------------------------------
# What a write refuses
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    ("chunks", "vectors"),
    [
        ([chunk(0), chunk(1)], [[1.0, 0.0]]),
        ([chunk(0)], [[1.0, 0.0], [0.0, 1.0]]),
        ([chunk(0)], []),
    ],
)
def test_chunks_and_vectors_that_are_not_parallel_are_refused(store, chunks, vectors) -> None:
    """The two files are joined by position alone, so a length mismatch is a corrupt index."""
    with pytest.raises(ValueError, match="not parallel"):
        store.write(INDEX, chunks, vectors)


def test_a_refused_write_leaves_no_index_behind(store) -> None:
    """The check comes before the first file, so a rejected build cannot half-replace what was there."""
    with pytest.raises(ValueError, match="not parallel"):
        store.write(INDEX, [chunk(0)], [])
    assert not store.exists(INDEX)


# ---------------------------------------------------------------------------
# What a search returns
# ---------------------------------------------------------------------------
def test_a_search_returns_the_cosine_itself_and_not_a_rank(store) -> None:
    """`retrieval` applies a floor to this number, which an ordering could not tell it."""
    vectors = [[1.0, 0.0], [1.0, 1.0], [0.0, 1.0]]
    store.write(INDEX, [chunk(0), chunk(1), chunk(2)], vectors)
    query = [2.0, 1.0]
    matches = store.search(INDEX, query, top_k=3)
    by_id = {match.chunk.chunk_id: match.similarity for match in matches}
    for ordinal, vector in enumerate(vectors):
        assert by_id[f"c{ordinal}"] == pytest.approx(cosine(query, vector))
    assert len(set(by_id.values())) == 3


def test_a_longer_vector_is_not_a_better_match(store) -> None:
    """Both sides are normalised first, so an embedding model's scale cannot buy a chunk a place."""
    store.write(INDEX, [chunk(0), chunk(1)], [[1.0, 0.0], [500.0, 0.0]])
    matches = store.search(INDEX, [3.0, 0.0], top_k=2)
    assert similarities(matches) == pytest.approx((1.0, 1.0))


def test_the_closest_chunk_comes_first_and_the_opposite_one_comes_last(store) -> None:
    store.write(INDEX, [chunk(0), chunk(1), chunk(2)], [[-1.0, 0.0], [0.0, 1.0], [1.0, 0.0]])
    matches = store.search(INDEX, [1.0, 0.0], top_k=3)
    assert ids(matches) == ("c2", "c1", "c0")
    assert similarities(matches) == pytest.approx((1.0, 0.0, -1.0))


def test_a_zero_vector_matches_nothing_and_sorts_below_every_real_match(store) -> None:
    """Zero is the honest score for a chunk with nothing to match on, and it keeps it off the top."""
    store.write(INDEX, [chunk(0), chunk(1), chunk(2)], [[0.0, 0.0], [1.0, 0.0], [1.0, 1.0]])
    matches = store.search(INDEX, [1.0, 1.0], top_k=3)
    assert ids(matches) == ("c2", "c1", "c0")
    assert matches[-1].similarity == 0.0
    assert all(math.isfinite(similarity) for similarity in similarities(matches))


def test_a_zero_question_scores_everything_at_zero_rather_than_failing(store) -> None:
    """A question that embedded to nothing is answered with nothing, not with an arbitrary winner."""
    store.write(INDEX, [chunk(0), chunk(1)], [[1.0, 0.0], [0.0, 1.0]])
    matches = store.search(INDEX, [0.0, 0.0], top_k=2)
    assert similarities(matches) == (0.0, 0.0)
    assert ids(matches) == ("c0", "c1")


def test_ties_keep_the_order_they_were_written_in(store) -> None:
    """Two chunks that score the same are separated by the sort alone, and reading order is the answer.

    The tied pair sits *below* two distinct scores on purpose: that is the arrangement an unstable
    sort actually reorders, where a search over nothing but identical chunks would pass either way.
    """
    vectors = [[0.0, 1.0], [0.0, 1.0], [1.0, 0.0], [1.0, 1.0]]
    store.write(INDEX, [chunk(0), chunk(1), chunk(2), chunk(3)], vectors)
    matches = store.search(INDEX, [1.0, 0.0], top_k=4)
    assert ids(matches) == ("c2", "c3", "c0", "c1")
    assert ids(store.search(INDEX, [1.0, 0.0], top_k=4)) == ids(matches)


def test_a_search_returns_at_most_top_k_and_never_more_than_the_index_holds(store) -> None:
    store.write(INDEX, [chunk(0), chunk(1), chunk(2)], [[1.0, 0.0], [1.0, 1.0], [0.0, 1.0]])
    assert ids(store.search(INDEX, [1.0, 0.0], top_k=1)) == ("c0",)
    assert len(store.search(INDEX, [1.0, 0.0], top_k=50)) == 3


def test_every_similarity_is_a_plain_float_between_minus_one_and_one(store) -> None:
    """A `numpy.float64` leaking out would serialise into an artefact as something else."""
    store.write(INDEX, [chunk(0), chunk(1)], [[3.0, 4.0], [-2.0, 0.5]])
    for match in store.search(INDEX, [1.0, 2.0], top_k=2):
        assert type(match.similarity) is float
        assert -1.0 <= match.similarity <= 1.0


# ---------------------------------------------------------------------------
# What a search refuses
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("top_k", [0, -1])
def test_a_top_k_below_one_is_refused_before_the_index_is_even_opened(store, top_k) -> None:
    """A caller's own mistake is reported as one, rather than as a fact about a missing index."""
    with pytest.raises(ValueError, match="top_k must be at least 1"):
        store.search(INDEX, [1.0, 0.0], top_k=top_k)


def test_a_query_of_the_wrong_width_is_refused_and_the_message_names_both_widths(store) -> None:
    """Two embedding models produce two widths; the numbers are what tells an operator which is which."""
    store.write(INDEX, [chunk(0)], [[1.0, 0.0, 0.0]])
    with pytest.raises(ValueError, match="2 dimensions and the index has 3"):
        store.search(INDEX, [1.0, 0.0], top_k=1)


def test_searching_an_index_that_was_never_built_names_the_index_and_not_a_path(store) -> None:
    """A path is a deployment detail; the id is the thing a reader can check against a listing."""
    with pytest.raises(GenerativeError) as error:
        store.search("x_20200101_00000000", [1.0, 0.0], top_k=1)
    assert error.value.code == "INDEX_NOT_FOUND"
    assert "x_20200101_00000000" in error.value.message
    assert "/" not in error.value.message
    assert error.value.suggestion


def test_reading_the_chunks_or_the_matrix_of_a_missing_index_fails_the_same_way(store) -> None:
    for call in (store.chunks, store.matrix):
        with pytest.raises(GenerativeError) as error:
            call("x_20200101_00000000")
        assert error.value.code == "INDEX_NOT_FOUND"


def test_an_index_that_holds_no_chunks_is_empty_rather_than_missing(store) -> None:
    """The two are different failures with different fixes: nothing was built, or nothing parsed."""
    store.write(INDEX, [], [])
    assert store.exists(INDEX)
    assert store.chunks(INDEX) == ()
    for call in (lambda index: store.search(index, [1.0, 0.0], top_k=1), store.matrix):
        with pytest.raises(GenerativeError) as error:
            call(INDEX)
        assert error.value.code == "INDEX_EMPTY"
        assert INDEX in error.value.message


# ---------------------------------------------------------------------------
# The matrix a search is run against
# ---------------------------------------------------------------------------
def test_the_matrix_is_unit_rows_in_chunk_order_and_a_zero_row_stays_zero(store) -> None:
    """The one place the normalisation is visible: every row of length one, except the one with none."""
    store.write(INDEX, [chunk(0), chunk(1), chunk(2)], [[3.0, 0.0], [0.0, -7.0], [0.0, 0.0]])
    matrix = store.matrix(INDEX)
    assert matrix.shape == (3, 2)
    assert np.allclose(matrix, [[1.0, 0.0], [0.0, -1.0], [0.0, 0.0]])
    assert np.allclose(np.linalg.norm(matrix, axis=1), [1.0, 1.0, 0.0])


# ---------------------------------------------------------------------------
# The arithmetic itself
# ---------------------------------------------------------------------------
def test_cosine_is_one_for_the_same_direction_zero_across_and_minus_one_opposite() -> None:
    assert cosine([1.0, 2.0], [1.0, 2.0]) == pytest.approx(1.0)
    assert cosine([1.0, 0.0], [0.0, 1.0]) == pytest.approx(0.0)
    assert cosine([1.0, 2.0], [-1.0, -2.0]) == pytest.approx(-1.0)


def test_cosine_of_a_vector_with_no_length_is_zero_rather_than_undefined() -> None:
    """The same answer `_normalise` gives a zero row, because the two must not disagree."""
    assert cosine([0.0, 0.0], [1.0, 1.0]) == 0.0
    assert cosine([1.0, 1.0], [0.0, 0.0]) == 0.0
    assert cosine([0.0, 0.0], [0.0, 0.0]) == 0.0
    assert cosine([], []) == 0.0


def test_cosine_does_not_change_when_either_vector_is_scaled() -> None:
    """An angle is what it measures, so magnitude - and therefore an embedding model's scale - is not."""
    left, right = [1.0, 2.0, 3.0], [4.0, 5.0, 6.0]
    expected = cosine(left, right)
    assert cosine([value * 2 for value in left], right) == pytest.approx(expected)
    assert cosine(left, [value * 100 for value in right]) == pytest.approx(expected)
    assert cosine(right, left) == pytest.approx(expected)


def test_cosine_agrees_with_what_a_search_scores(store) -> None:
    """Two implementations of one idea - `retrieval` uses both, and an MMR trade needs them to agree."""
    store.write(INDEX, [chunk(0), chunk(1)], [[0.3, 0.9, 0.1], [0.8, 0.1, 0.6]])
    query = [0.25, 0.7, 0.4]
    matches = store.search(INDEX, query, top_k=2)
    for match in matches:
        vector = [0.3, 0.9, 0.1] if match.chunk.chunk_id == "c0" else [0.8, 0.1, 0.6]
        assert match.similarity == pytest.approx(cosine(query, vector))


def test_cosine_refuses_two_vectors_of_different_widths() -> None:
    """Padding the shorter one would invent a dimension and quietly return a number nobody can act on."""
    with pytest.raises(ValueError, match="mismatch"):
        cosine([1.0, 2.0], [1.0, 2.0, 3.0])


# ---------------------------------------------------------------------------
# The small helpers, and the protocol
# ---------------------------------------------------------------------------
def test_chunk_ids_are_the_ids_in_order_and_an_empty_corpus_gives_an_empty_tuple() -> None:
    """A manifest and a citation both want this shape, and both care that it is not sorted."""
    assert chunk_ids([chunk(2), chunk(0), chunk(1)]) == ("c2", "c0", "c1")
    assert chunk_ids(iter([chunk(0)])) == ("c0",)
    assert chunk_ids([]) == ()


def test_a_local_vector_store_satisfies_the_vector_store_protocol(store) -> None:
    """Phase 4 swaps OpenSearch in behind this; the protocol is the only thing holding the two together."""
    assert isinstance(store, VectorStore)


def test_a_store_missing_one_operation_is_not_a_vector_store() -> None:
    """All four are needed: a store that could not say whether an index exists would rebuild every time."""

    class WriteOnly:
        def write(self, index_id, chunks, vectors) -> None: ...

        def chunks(self, index_id) -> tuple[Chunk, ...]:
            return ()

        def search(self, index_id, query, *, top_k, query_text=None, bm25_weight=0.0) -> tuple[Match, ...]:
            return ()

    assert not isinstance(WriteOnly(), VectorStore)


# ---------------------------------------------------------------------------
# BM25 and hybrid search (DEC-1260 ... DEC-1262)
# ---------------------------------------------------------------------------
def test_bm25_matches_a_hand_computed_score() -> None:
    """Two documents, "a b" and "a c c", asked "c" and then "a c", worked by hand.

    N = 2, average length 2.5. idf(c) = ln(1 + (2 - 1 + 0.5) / (1 + 0.5)) = ln 2 and idf(a) =
    ln(1 + 0.5 / 2.5) = ln 1.2. The length norms are k1 * (1 - b + b * len / 2.5): 1.275 for the
    two-word document and 1.725 for the three-word one. The scale is sum(idf) * (k1 + 1).
    """
    assert (BM25_K1, BM25_B) == (1.5, 0.75)
    corpus = ["a b", "a c c"]
    only_c = bm25_scores(corpus, "c")
    assert only_c[0] == 0.0
    assert only_c[1] == pytest.approx((math.log(2) * 2 * 2.5 / (2 + 1.725)) / (math.log(2) * 2.5))
    assert only_c[1] == pytest.approx(0.5369127517)
    both = bm25_scores(corpus, "a c")
    scale = (math.log(1.2) + math.log(2)) * 2.5
    assert both[0] == pytest.approx((math.log(1.2) * 1 * 2.5 / (1 + 1.275)) / scale)
    assert both[1] == pytest.approx(
        (math.log(1.2) * 1 * 2.5 / (1 + 1.725) + math.log(2) * 2 * 2.5 / (2 + 1.725)) / scale
    )


def test_bm25_counts_a_question_word_once_and_ignores_words_no_chunk_has() -> None:
    corpus = ["a b", "a c c"]
    assert list(bm25_scores(corpus, "c c c")) == list(bm25_scores(corpus, "c"))
    assert list(bm25_scores(corpus, "c zebra")) == list(bm25_scores(corpus, "c"))
    assert list(bm25_scores(corpus, "zebra")) == [0.0, 0.0]
    assert list(bm25_scores(corpus, "")) == [0.0, 0.0]
    assert list(bm25_scores([], "c")) == []


def test_bm25_tokens_are_lower_cased_words_in_any_script() -> None:
    assert bm25_tokens("Late-FEE, 2% of ₹100!") == ["late", "fee", "2", "of", "100"]
    assert bm25_tokens("Café naïve") == ["café", "naïve"]


def test_bm25_statistics_are_computed_once_per_set_of_texts() -> None:
    """The cache is keyed by the texts themselves, so a rebuilt index can never get stale statistics."""
    first = bm25_corpus(["alpha beta", "beta gamma"])
    assert bm25_corpus(["alpha beta", "beta gamma"]) is first
    rebuilt = bm25_corpus(["alpha beta", "beta delta"])
    assert rebuilt is not first
    assert rebuilt.document_frequency["delta"] == 1 and "delta" not in first.document_frequency


def test_the_store_does_not_tokenise_the_index_again_for_each_question(
    store: LocalVectorStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    from engine.generative import vectorstore

    store.write(INDEX, [chunk(0, text="late fee"), chunk(1, text="porting code")], [[1.0, 0.0], [0.0, 1.0]])
    vectorstore._bm25_corpus.cache_clear()
    tokenised: list[str] = []
    real = vectorstore.bm25_tokens
    monkeypatch.setattr(vectorstore, "bm25_tokens", lambda text: tokenised.append(text) or real(text))
    for question in ("late fee", "porting", "fee"):
        store.search(INDEX, [1.0, 0.0], top_k=2, query_text=question, bm25_weight=0.5)
    corpus_texts = [text for text in tokenised if text.startswith("Billing")]
    assert len(corpus_texts) == 2, "each chunk is tokenised once, not once per question"


def test_a_hybrid_search_ranks_by_the_blend_and_still_reports_the_cosine(store: LocalVectorStore) -> None:
    """The dense winner loses the rank to the chunk that has the question's word; neither loses its cosine."""
    store.write(
        INDEX,
        [chunk(0, text="reconnection is automatic"), chunk(1, text="the porting code")],
        [[1.0, 1.0], [0.6, 0.8]],
    )
    dense = store.search(INDEX, [1.0, 0.0], top_k=2)
    assert ids(dense) == ("c0", "c1")
    assert all(m.keyword_score is None and m.hybrid_score is None for m in dense)
    mixed = store.search(INDEX, [1.0, 0.0], top_k=2, query_text="porting code", bm25_weight=0.5)
    assert ids(mixed) == ("c1", "c0")
    by_id = {m.chunk.chunk_id: m for m in mixed}
    assert by_id["c0"].similarity == pytest.approx(math.sqrt(0.5))
    assert by_id["c1"].similarity == pytest.approx(0.6)
    assert by_id["c0"].keyword_score == 0.0
    assert by_id["c1"].hybrid_score == pytest.approx(0.5 * 0.6 + 0.5 * by_id["c1"].keyword_score)


def test_a_weight_of_zero_or_no_question_text_is_the_plain_vector_search(store: LocalVectorStore) -> None:
    store.write(INDEX, [chunk(0, text="alpha"), chunk(1, text="beta")], [[1.0, 0.0], [0.0, 1.0]])
    plain = store.search(INDEX, [1.0, 0.0], top_k=2)
    assert store.search(INDEX, [1.0, 0.0], top_k=2, query_text="beta", bm25_weight=0.0) == plain
    assert store.search(INDEX, [1.0, 0.0], top_k=2, query_text="  ", bm25_weight=0.5) == plain


@pytest.mark.parametrize("weight", [-0.1, 1.5])
def test_a_weight_outside_zero_to_one_is_a_callers_mistake(store: LocalVectorStore, weight: float) -> None:
    store.write(INDEX, [chunk(0)], [[1.0, 0.0]])
    with pytest.raises(ValueError, match="bm25_weight"):
        store.search(INDEX, [1.0, 0.0], top_k=1, query_text="bills", bm25_weight=weight)
