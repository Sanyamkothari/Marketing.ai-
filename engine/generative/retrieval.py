"""Choosing which chunks an answer is allowed to see.

Between the store and the prompt sit two decisions, and both are about what to *leave out*.

**The similarity floor.** A vector search always returns its `top_k`, however badly they match: ask
a telecom knowledge base about a share price and it will hand back the five least-unrelated
paragraphs it has, each with a similarity near zero. Answering from those is how a grounded
assistant invents things. So anything below `min_similarity` is dropped, and when that leaves
nothing the caller refuses **without calling a model at all** - which is both the honest answer and
a free one.

**Maximal marginal relevance.** The top `k` by similarity are routinely near-duplicates: a policy
repeated in an FAQ, a table and its surrounding prose, the overlap a chunker deliberately
introduced between neighbours. Handing a model five wordings of one fact wastes the context that a
second fact needed, and makes the answer confidently one-sided. MMR picks greedily, scoring each
candidate on how well it matches the question *minus* how much it repeats what is already chosen,
with `mmr_lambda` setting the trade.

Neither decision is a model's to make, and neither costs a call. What reaches the prompt is
`retrieve`'s output and nothing else, which is what makes "answer only from the extracts" a rule
the engine enforces rather than a request the prompt makes.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Final

from engine.config import RagConfig
from engine.generative.contracts import Chunk
from engine.generative.vectorstore import Match, VectorStore, cosine
from engine.utils.logging import get_logger

__all__ = ["OVERSAMPLE", "Retrieved", "mmr", "retrieve"]

_LOGGER = get_logger(__name__)

OVERSAMPLE: Final[int] = 4
"""How many times `top_k` to ask the store for before MMR narrows it down.

MMR can only choose among what it is given, so asking for exactly `top_k` would leave it nothing to
trade: the diverse chunk it wants is usually the sixth or the tenth by raw similarity, not the
fifth. Four times is enough for that and small enough that the extra rows cost nothing to score.
"""


@dataclass(frozen=True)
class Retrieved:
    """What retrieval decided, and what it decided against.

    `considered` and `above_floor` are carried so the answer artefact can say *why* it refused -
    "nothing was close enough" and "nothing was indexed" are different failures with different
    fixes, and a caller that only saw an empty list could not tell them apart.
    """

    matches: tuple[Match, ...]
    considered: int
    above_floor: int
    floor: float

    @property
    def chunks(self) -> tuple[Chunk, ...]:
        """The chunks themselves, in the order they will be numbered in the prompt."""
        return tuple(match.chunk for match in self.matches)

    @property
    def empty(self) -> bool:
        """True when nothing survived the floor, which is a refusal and not an error."""
        return not self.matches


def retrieve(
    store: VectorStore,
    index_id: str,
    question_vector: Sequence[float],
    *,
    config: RagConfig,
    documents: Sequence[str] | None = None,
) -> Retrieved:
    """The chunks an answer may use: searched, floored, de-duplicated, best first.

    `documents` narrows the search to named files, which is what a use case with one knowledge base
    per product line needs. The filter is applied after the search rather than inside it, because
    the local store has no query language and Phase 4's will; a caller that needs it filtered
    cheaply at scale is a caller on OpenSearch.
    """
    wanted = config.top_k
    candidates = store.search(index_id, question_vector, top_k=wanted * OVERSAMPLE)
    considered = len(candidates)
    if documents is not None:
        allowed = set(documents)
        candidates = tuple(match for match in candidates if match.chunk.document in allowed)
    above = tuple(match for match in candidates if match.similarity >= config.min_similarity)
    chosen = mmr(above, question_vector, top_k=wanted, lambda_=config.mmr_lambda)
    _LOGGER.info(
        "retrieval.done considered=%d above_floor=%d chosen=%d",
        considered,
        len(above),
        len(chosen),
    )
    return Retrieved(
        matches=chosen, considered=considered, above_floor=len(above), floor=config.min_similarity
    )


def mmr(
    candidates: Sequence[Match],
    question_vector: Sequence[float],
    *,
    top_k: int,
    lambda_: float,
) -> tuple[Match, ...]:
    """Greedy maximal marginal relevance over `candidates`, most useful first.

    Each round picks the candidate with the highest `lambda_ * similarity(question) - (1 - lambda_)
    * max similarity(already chosen)`. The first pick is therefore always the best raw match, which
    matters: a reader who checks the top citation should find the passage they expected.

    Similarity between two chunks is computed from their *text* rather than from their vectors,
    because the store hands back chunks and not the vectors behind them. That is a lexical measure
    where the first term is a semantic one, and it is the right way round: what MMR is trying to
    avoid here is two chunks that say the same words, which is exactly what a chunker's overlap and
    a policy quoted twice produce.
    """
    if not candidates or top_k < 1:
        return ()
    if lambda_ >= 1.0:
        return tuple(candidates[:top_k])
    remaining = list(candidates)
    chosen: list[Match] = [remaining.pop(0)]
    while remaining and len(chosen) < top_k:
        best_index, best_score = 0, float("-inf")
        for index, candidate in enumerate(remaining):
            repetition = max(_text_similarity(candidate.chunk, picked.chunk) for picked in chosen)
            score = lambda_ * candidate.similarity - (1.0 - lambda_) * repetition
            if score > best_score:
                best_index, best_score = index, score
        chosen.append(remaining.pop(best_index))
    del question_vector  # the candidates already carry their similarity to it
    return tuple(chosen)


def _text_similarity(left: Chunk, right: Chunk) -> float:
    """How much two chunks repeat each other, as a Jaccard overlap of their words, 0 to 1.

    Cheap, symmetric and needs no vector. Two chunks from one section that share a chunker's
    overlap score high; two chunks about different topics score near zero even when both are
    relevant to the question, which is the case MMR exists to keep.
    """
    a = set(left.text.lower().split())
    b = set(right.text.lower().split())
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def similarity_to(vector: Sequence[float], other: Sequence[float]) -> float:
    """Cosine similarity, re-exported so a caller need not know which module owns the arithmetic."""
    return cosine(vector, other)
