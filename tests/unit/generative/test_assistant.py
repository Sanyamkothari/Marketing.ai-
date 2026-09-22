"""`engine.generative.assistant`: five steps whose order is the whole of the contract.

**The floor comes first, and a refusal costs one embedding.** A question the documents do not
answer is refused before a model is asked anything, the sentence the customer reads is the
operator's rather than a model's improvisation, and the answer says so in `called_model`. That is
proved from the fake client's own call log - exactly one `embed` and no `complete` - because
"no model was called" is a claim about something that did not happen, and only the log can settle
it. Nothing else in this file is worth as much.

**The two refusals are different mechanisms, and only the pair proves it.** The floor refuses
without calling; `GroundedFakeMode.REFUSING` refuses after calling, with the extracts retrieved and
counted. Asserting either alone would leave `called_model` looking like a synonym for `refused`.
The same-domain off-topic question - the one that shares the corpus's vocabulary and outscores
several genuine questions under a bag of words - is deliberately not here: a lexical fake cannot
tell it apart, so a test that asserted it refuses would pin noise rather than behaviour (DEC-219).

**A citation is a position, and a position nobody supplied is dropped.** The prompt numbers the
extracts, so `extracts_for` is the citation vocabulary and its order is load-bearing: extract 1 is
the first element and nothing else. A model that cites past the end has said something about a
document that was not in front of it, and what is proved is that the claim survives while the false
citation does not, leaving a `UNKNOWN_CITATION` warning behind for the report to carry.

**Nothing the model wrote is taken as it stands.** The quote is cut to `QUOTE_WORDS` here rather
than trusted from the reply, a code fence is stripped because stripping one changes no content, a
reply the contract cannot read is dropped for the operator's refusal sentence rather than raised or
shown, and only the last `HISTORY_TURNS` turns reach the prompt.

**A quote is words from the chunk or it is nothing.** Everything else a `Citation` carries is the
engine's own - the chunk id, the document, the heading, the similarity - so a quote taken on trust
would be the single invented thing in a row of real provenance, and the reader most likely to
believe it is the one who checked the document name first. Both halves are proved: a quote the cited
chunk really contains survives to the artefact, and one it does not is dropped for an empty string
with an `UNSUPPORTED_QUOTE` warning, while the citation around it stands (DEC-226).

The index is real and is built once for the module: three documents of the synthetic Northwind
corpus, parsed, chunked and embedded by the code a deployment runs, so the retrieval order, the
similarities and the chunk ids asserted below are earned rather than stubbed. Two costs come with
that. The fixture is shared, so no test may write to it. And every similarity is the lexical
fake's, so this module sets its own floor and never claims the shipped 0.25 produces a particular
outcome: that number is calibrated for the Bedrock embedding model and means something else here
(DEC-218).

`_parse` is called directly wherever the fake cannot be made to produce the reply that matters: a
quote past the limit, a fenced object, a citation number nobody supplied sitting beside one that
was. Those calls go through `parsed`, which hands the parser the question and the refusal sentence
the flow would have handed it, so a check read off it is the check a caller would have seen.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from dataclasses import dataclass, replace
from pathlib import Path

import pytest

from engine.config import BudgetConfig, LlmConfig, RagConfig, UseCaseConfig, load_use_case
from engine.generative.assistant import (
    ANSWER_PROMPT,
    HISTORY_TURNS,
    QUOTE_WORDS,
    UNKNOWN_CITATION,
    UNSUPPORTED_QUOTE,
    Turn,
    _parse,
    answer,
    extracts_for,
)
from engine.generative.budget import Meter
from engine.generative.contracts import (
    AssistantAnswer,
    Chunk,
    Citation,
    GenerativePurpose,
    GuardrailCheck,
    GuardrailOutcome,
)
from engine.generative.errors import INDEX_NOT_FOUND, MODEL_OUTPUT_MALFORMED, GenerativeError
from engine.generative.guardrails import BANNED_PHRASES, PII_IN_OUTPUT, Guardrails, load_policy
from engine.generative.index import build_index
from engine.generative.prompts import load_prompt
from engine.generative.retrieval import Retrieved, retrieve
from engine.generative.vectorstore import LocalVectorStore, Match, VectorStore
from engine.llm import GroundedFakeLLMClient, GroundedFakeMode, LLMCall
from engine.storage import LocalStorage
from engine.utils.ids import new_index_id
from tests.fixtures.make_docs import build_knowledge_base

USE_CASE = "ai-onboarding-assistant"
BASE: UseCaseConfig = load_use_case(USE_CASE)

STEMS: tuple[str, ...] = ("faq_activation", "faq_billing", "plans_prepaid")
"""Three documents rather than fourteen: the flow is the same and the fixture costs a tenth of it."""

ANSWERED = "How long does a new SIM take to activate?"
"""A question the corpus answers, whose words are in a heading the chunker kept (DEC-217)."""

DISJOINT = "Who painted the ceiling of the Sistine Chapel?"
"""A question with no word in common with a telecom corpus, which is the only off-topic question a
lexical fake can be trusted to place below a floor (DEC-219)."""

FLOOR = 0.15
"""This module's similarity floor, owned here because the shipped one is a Bedrock number (DEC-218)."""

REFUSAL = "The documents I have do not cover that. Please write to the support desk."
"""A refusal sentence nothing else in the repository uses, so an answer carrying it was configured."""


@dataclass(frozen=True)
class KnowledgeIndex:
    """A built index, the store holding it and the directory it lives in, shared by every test here."""

    index_id: str
    store: VectorStore
    root: Path


def meter_for(client: GroundedFakeLLMClient) -> Meter:
    """A meter over `client` with the cache off, so every call is a call the client records."""
    return Meter(client, job_id="x_20260101_abcdef01", llm=LlmConfig(), budget=BudgetConfig(cache=False))


def rag(**overrides: object) -> RagConfig:
    """RAG settings this module owns: its own floor, and a refusal sentence nobody else ships."""
    settings: dict[str, object] = {
        "top_k": 4,
        "min_similarity": FLOOR,
        "mmr_lambda": 0.7,
        "refusal_message": REFUSAL,
    }
    settings.update(overrides)
    return RagConfig(**settings)


def use_case(**overrides: object) -> UseCaseConfig:
    """The shipped assistant use case, with this module's RAG settings in place of its own."""
    return BASE.model_copy(
        update={"generative": BASE.generative.model_copy(update={"rag": rag(**overrides)})}
    )


def ask(
    index: KnowledgeIndex,
    question: str,
    *,
    mode: GroundedFakeMode = GroundedFakeMode.GROUNDED,
    judged: bool = True,
    history: tuple[Turn, ...] = (),
    config_root: Path | None = None,
    **overrides: object,
) -> tuple[GroundedFakeLLMClient, Meter, AssistantAnswer]:
    """Answer `question` against `index`, handing back the client and the meter that paid for it.

    The client's own call log cannot by itself distinguish a call the flow routed through the
    meter from one it made directly against the client - both land in `client.calls` the same way
    - so a test that needs to prove "every call went through the meter" reads `meter.usage()`
    rather than the log alone.

    `judged=False` builds the guardrails without a meter, which is the documented way to run the
    deterministic rules and skip the judges; it is what a test uses when a judge's verdict would
    otherwise decide the thing being asserted.
    """
    client = GroundedFakeLLMClient(mode=mode)
    meter = meter_for(client)
    return (
        client,
        meter,
        answer(
            question,
            index_id=index.index_id,
            use_case=use_case(**overrides),
            store=index.store,
            meter=meter,
            guardrails=Guardrails(load_policy(), meter=meter if judged else None),
            history=history,
            config_root=config_root,
        ),
    )


def retrieved_for(index: KnowledgeIndex, question: str, **overrides: object) -> Retrieved:
    """What retrieval chooses for `question`, worked out the way the flow itself works it out."""
    (vector,) = meter_for(GroundedFakeLLMClient()).embed([question])
    return retrieve(index.store, index.index_id, vector, config=rag(**overrides))


def completes(client: GroundedFakeLLMClient) -> tuple[LLMCall, ...]:
    """Every completion the client was asked for - what `called_model` has to agree with."""
    return tuple(call for call in client.calls if call.kind == "complete")


def chunk(text: str, *, section: str = "How long does activation take?", ordinal: int = 0) -> Chunk:
    """One hand-built chunk, for the parsing tests that own their extracts rather than retrieve them."""
    return Chunk(
        chunk_id=f"faq_activation-{ordinal:05d}",
        doc_id="faq_activation",
        document="faq_activation.md",
        section=section,
        ordinal=ordinal,
        tokens=len(text.split()),
        text=text,
    )


def match(
    text: str,
    *,
    similarity: float = 0.5,
    ordinal: int = 0,
    section: str = "How long does activation take?",
) -> Match:
    """One scored chunk, as retrieval hands it to the prompt."""
    return Match(chunk=chunk(text, section=section, ordinal=ordinal), similarity=similarity)


def reply(**payload: object) -> str:
    """A model reply in the shape the answering prompt asks for."""
    return json.dumps({"answer": "Within four hours.", "refused": False, **payload})


def parsed(
    raw: str, matches: Sequence[Match] = ()
) -> tuple[str, bool, tuple[Citation, ...], tuple[GuardrailCheck, ...]]:
    """`_parse` as the flow calls it, with the question asked and the sentence configured for a refusal.

    A check read off it then carries the target a caller reading `answer.guardrails` would have seen.
    """
    return _parse(raw, matches, question=ANSWERED, refusal=REFUSAL)


@pytest.fixture(scope="module")
def knowledge_index(tmp_path_factory: pytest.TempPathFactory) -> KnowledgeIndex:
    """Three Northwind documents, parsed, chunked and embedded by the code a deployment runs."""
    root = tmp_path_factory.mktemp("assistant")
    paths = build_knowledge_base(root / "docs", stems=STEMS)
    storage = LocalStorage(root / "data")
    store = LocalVectorStore(storage)
    index_id = new_index_id()
    build_index(
        list(paths),
        index_id=index_id,
        use_case=BASE,
        storage=storage,
        store=store,
        meter=meter_for(GroundedFakeLLMClient()),
    )
    return KnowledgeIndex(index_id=index_id, store=store, root=root / "data")


# ---------------------------------------------------------------------------
# The floor, which comes first and costs nothing
# ---------------------------------------------------------------------------
def test_a_question_the_documents_share_no_words_with_is_refused_without_calling_a_model(
    knowledge_index: KnowledgeIndex,
) -> None:
    """The single most important claim in the module: nothing above the floor, so nothing was asked."""
    client, _, result = ask(knowledge_index, DISJOINT)
    assert result.refused
    assert result.answer == REFUSAL
    assert result.retrieved == 0
    assert result.citations == ()
    assert result.called_model is False
    assert [call.kind for call in client.calls] == ["embed"]
    assert completes(client) == ()


def test_a_floor_nothing_can_reach_refuses_the_question_the_documents_do_answer(
    knowledge_index: KnowledgeIndex,
) -> None:
    """Same state from the other direction: it is the floor that refuses, not the question."""
    client, _, result = ask(knowledge_index, ANSWERED, min_similarity=0.99)
    assert result.refused
    assert result.answer == REFUSAL
    assert (result.retrieved, result.called_model) == (0, False)
    assert completes(client) == ()
    reachable = retrieved_for(knowledge_index, ANSWERED)
    assert reachable.matches, "the same question is answerable at this module's own floor"


def test_a_refusal_the_floor_made_carries_no_guardrail_checks(knowledge_index: KnowledgeIndex) -> None:
    """Nothing was generated, so nothing was checked; a passing check would claim otherwise."""
    _, _, result = ask(knowledge_index, DISJOINT)
    assert result.guardrails == ()


def test_the_question_is_embedded_bare_and_exactly_once(knowledge_index: KnowledgeIndex) -> None:
    """A chunk is embedded with its heading in front of it; the question is not (DEC-217)."""
    client, _, _ = ask(knowledge_index, ANSWERED)
    embeddings = [call for call in client.calls if call.kind == "embed"]
    assert [call.texts for call in embeddings] == [(ANSWERED,)]


# ---------------------------------------------------------------------------
# The grounded answer
# ---------------------------------------------------------------------------
def test_a_question_the_documents_answer_comes_back_answered_and_cited(
    knowledge_index: KnowledgeIndex,
) -> None:
    """The baseline: without it every refusal below could be passing because nothing ever answers."""
    client, meter, result = ask(knowledge_index, ANSWERED)
    found = retrieved_for(knowledge_index, ANSWERED)
    assert not result.refused
    assert result.answer and result.answer != REFUSAL
    assert result.called_model is True
    assert result.retrieved == len(found.matches) > 1
    assert result.citations
    assert len(completes(client)) == 2
    assert {usage.purpose: usage.calls for usage in meter.usage().by_purpose} == {
        GenerativePurpose.EMBEDDING: 1,
        GenerativePurpose.ASSISTANT_ANSWER: 1,
        GenerativePurpose.JUDGE_FAITHFULNESS: 1,
    }


def test_every_citation_points_at_a_chunk_that_was_really_retrieved(
    knowledge_index: KnowledgeIndex,
) -> None:
    """A citation carries the chunk's provenance and the similarity of the match it came from."""
    _, _, result = ask(knowledge_index, ANSWERED)
    retrieved = {found.chunk.chunk_id: found for found in retrieved_for(knowledge_index, ANSWERED).matches}
    assert result.citations
    for citation in result.citations:
        found = retrieved[citation.chunk_id]
        assert citation.document == found.chunk.document
        assert citation.section == found.chunk.section
        assert citation.similarity == pytest.approx(round(found.similarity, 4))
        assert 0 < len(citation.quote.split()) <= QUOTE_WORDS
        assert citation.quote.lower() in " ".join(found.chunk.text.lower().split())


def test_the_extracts_the_model_saw_are_the_chunks_retrieval_chose_in_the_order_it_chose_them(
    knowledge_index: KnowledgeIndex,
) -> None:
    """The prompt numbers by position, so the numbering has to be retrieval's order and nothing else."""
    client, _, result = ask(knowledge_index, ANSWERED)
    extracts = extracts_for(retrieved_for(knowledge_index, ANSWERED).matches)
    prompt = completes(client)[0].prompt
    for number, extract in enumerate(extracts, start=1):
        assert f"[{number}] ({extract['document']}" in prompt
        assert extract["section"] in prompt
        assert extract["text"] in prompt
    assert f"[{len(extracts) + 1}]" not in prompt
    assert result.retrieved == len(extracts)


def test_the_faithfulness_judge_is_given_the_extracts_the_prompt_was_given(
    knowledge_index: KnowledgeIndex,
) -> None:
    """Asking "is every claim supported?" means something only when it is asked of the same text."""
    client, _, _ = ask(knowledge_index, ANSWERED)
    judged = completes(client)[1].prompt
    for found in retrieved_for(knowledge_index, ANSWERED).matches:
        assert found.chunk.text in judged


# ---------------------------------------------------------------------------
# The numbering, which is the citation vocabulary
# ---------------------------------------------------------------------------
def test_extract_one_is_the_first_match_and_nothing_else() -> None:
    """The prompt numbers by position, so this order is what a citation number means."""
    matches = (
        match("A new SIM is usually live within four hours.", ordinal=1),
        match("Restart the handset first.", section="Why does my new SIM show no network?", ordinal=2),
        match("The SIM is handed only to the applicant.", section="Can somebody else collect it?", ordinal=3),
    )
    extracts = extracts_for(matches)
    assert extracts[0] == {
        "document": "faq_activation.md",
        "section": "How long does activation take?",
        "text": "A new SIM is usually live within four hours.",
    }
    assert [extract["text"] for extract in extracts] == [found.chunk.text for found in matches]


def test_an_extract_carries_the_provenance_and_the_passage_and_nothing_a_prompt_cannot_use() -> None:
    """A chunk id in the prompt would invite the model to cite a filename instead of a position."""
    extracts = extracts_for((match("Within four hours."),))
    assert [sorted(extract) for extract in extracts] == [["document", "section", "text"]]


def test_no_matches_are_no_extracts() -> None:
    """The shape the refusal path relies on: nothing retrieved renders nothing, rather than failing."""
    assert extracts_for(()) == ()


# ---------------------------------------------------------------------------
# A citation that points nowhere
# ---------------------------------------------------------------------------
def test_a_citation_that_points_nowhere_is_dropped_and_the_claim_survives(
    knowledge_index: KnowledgeIndex,
) -> None:
    """The model saw the extracts and answered from them; only its reference is wrong."""
    _, _, result = ask(knowledge_index, ANSWERED, mode=GroundedFakeMode.UNGROUNDED)
    assert not result.refused
    assert result.answer and result.answer != REFUSAL
    assert result.citations == ()
    warnings = [check for check in result.guardrails if check.rule == UNKNOWN_CITATION]
    assert [check.outcome for check in warnings] == [GuardrailOutcome.WARNED]
    assert "not supplied" in warnings[0].detail


def test_every_check_on_one_answer_names_the_question_and_never_the_answer(
    knowledge_index: KnowledgeIndex,
) -> None:
    """One list whose `target` meant the question in one row and the answer in the next is unreadable."""
    _, _, result = ask(knowledge_index, ANSWERED, mode=GroundedFakeMode.UNGROUNDED)
    assert result.guardrails
    assert {check.target for check in result.guardrails} <= {ANSWERED[:60], ANSWERED[:80]}
    assert all(check.target for check in result.guardrails)


def test_a_false_citation_is_dropped_while_the_citation_beside_it_survives() -> None:
    """Dropping the answer would cost a reader the claim; dropping the reference costs them nothing."""
    matches = (match("Within four hours.", similarity=0.39), match("Restart the handset.", ordinal=1))
    text, refused, citations, checks = parsed(
        reply(citations=[{"chunk": 2, "quote": "Restart the handset."}, {"chunk": 9, "quote": "invented"}]),
        matches,
    )
    assert (text, refused) == ("Within four hours.", False)
    assert [citation.chunk_id for citation in citations] == [matches[1].chunk.chunk_id]
    assert [check.rule for check in checks] == [UNKNOWN_CITATION]


@pytest.mark.parametrize("cited", [0, -1, 4, "seven", "", None, 1.5])
def test_a_citation_number_that_names_no_extract_is_dropped_however_it_was_written(cited) -> None:
    """Three extracts were supplied, so a fourth, a zeroth and a word are all the same mistake."""
    matches = tuple(match(f"Extract {number}.", ordinal=number) for number in (1, 2, 3))
    _, _, citations, checks = parsed(reply(citations=[{"chunk": cited, "quote": "q"}]), matches)
    assert citations == ()
    assert [(check.rule, check.outcome) for check in checks] == [(UNKNOWN_CITATION, GuardrailOutcome.WARNED)]


@pytest.mark.parametrize("cited", [["extract 3"], [3], [None], [[{"chunk": 1}]]])
def test_a_citation_that_is_not_an_object_is_recorded_like_any_other_that_points_nowhere(cited) -> None:
    """A citation entry with no `chunk` to read is still a claim that pointed nowhere, not a silent drop."""
    _, _, citations, checks = parsed(reply(citations=cited), (match("Within four hours."),))
    assert citations == ()
    assert [(check.rule, check.outcome) for check in checks] == [(UNKNOWN_CITATION, GuardrailOutcome.WARNED)]


@pytest.mark.parametrize("cited", ["2", 2.0])
def test_a_number_the_model_wrote_as_a_string_or_a_whole_float_is_still_a_number(cited: object) -> None:
    """Models write `"2"` and `2.0` as often as `2`, and dropping either would cost a good citation."""
    matches = (match("Within four hours."), match("Restart the handset.", ordinal=1, similarity=0.2))
    _, _, citations, checks = parsed(
        reply(citations=[{"chunk": cited, "quote": "Restart the handset."}]), matches
    )
    assert [citation.chunk_id for citation in citations] == [matches[1].chunk.chunk_id]
    assert checks == ()


def test_a_reply_that_cites_nothing_cites_nothing_rather_than_failing() -> None:
    """A model that answers without citing is a guardrail's problem, not the parser's."""
    matches = (match("Within four hours."),)
    assert parsed(reply(citations=None), matches)[2] == ()
    assert parsed(reply(), matches)[2] == ()


# ---------------------------------------------------------------------------
# What the model wrote, and what is trusted of it
# ---------------------------------------------------------------------------
def test_a_quote_is_cut_to_the_word_limit_here_rather_than_trusted_from_the_model() -> None:
    """The prompt asks for at most 25 words; asking is not enforcing, and the artefact is enforced."""
    passage = " ".join(f"word{number}" for number in range(QUOTE_WORDS * 2))
    matches = (match(passage),)
    _, _, citations, checks = parsed(reply(citations=[{"chunk": 1, "quote": passage}]), matches)
    assert citations[0].quote.split() == passage.split()[:QUOTE_WORDS]
    assert checks == (), "the whole passage is the chunk's own, so trimming it invents nothing"


# ---------------------------------------------------------------------------
# A quote the chunk does not contain (DEC-226)
# ---------------------------------------------------------------------------
def test_a_quote_the_cited_chunk_really_contains_is_carried_through_as_the_model_wrote_it() -> None:
    """Without this the check below could pass by dropping every quote there has ever been."""
    matches = (match("A new SIM is usually live within four hours of the form being accepted."),)
    _, _, citations, checks = parsed(
        reply(citations=[{"chunk": 1, "quote": "usually live within four hours"}]), matches
    )
    assert [citation.quote for citation in citations] == ["usually live within four hours"]
    assert checks == ()


def test_a_quote_the_cited_chunk_does_not_contain_is_dropped_and_the_citation_survives() -> None:
    """The invented words go; the chunk id, document, heading and similarity are the engine's own."""
    matches = (match("A new SIM is usually live within four hours.", similarity=0.42),)
    _, _, citations, checks = parsed(
        reply(citations=[{"chunk": 1, "quote": "activation is instant and always free"}]), matches
    )
    assert [(citation.chunk_id, citation.quote) for citation in citations] == [
        (matches[0].chunk.chunk_id, "")
    ]
    assert [citation.similarity for citation in citations] == [0.42]
    assert [(check.rule, check.outcome) for check in checks] == [(UNSUPPORTED_QUOTE, GuardrailOutcome.WARNED)]
    assert "does not contain" in checks[0].detail


def test_a_quote_that_borrows_a_real_documents_name_for_invented_words_is_still_dropped() -> None:
    """The failure worth naming: invented words under a chunk id, a document and a heading that are real."""
    matches = (match("A new SIM is usually live within four hours."),)
    _, _, citations, _ = parsed(reply(citations=[{"chunk": 1, "quote": "live within four minutes"}]), matches)
    assert citations[0].document == "faq_activation.md"
    assert citations[0].section == "How long does activation take?"
    assert citations[0].quote == ""


@pytest.mark.parametrize(
    "quoted",
    ["Within\n four   hours.", "WITHIN FOUR HOURS.", "  within four hours.  "],
)
def test_a_quote_the_chunk_contains_but_spells_differently_is_still_the_chunks_own_words(
    quoted: str,
) -> None:
    """A passage crosses a template and a JSON string; re-wrapping it changes no word of it."""
    matches = (match("Within four hours."),)
    _, _, citations, checks = parsed(reply(citations=[{"chunk": 1, "quote": quoted}]), matches)
    assert citations[0].quote
    assert checks == ()


@pytest.mark.parametrize("quoted", ["", "   ", None])
def test_a_citation_that_quotes_nothing_is_not_recorded_as_having_invented_a_quote(
    quoted: object,
) -> None:
    """No words were claimed, so none were invented; an empty quote shows a reader nothing at all."""
    matches = (match("Within four hours."),)
    _, _, citations, checks = parsed(reply(citations=[{"chunk": 1, "quote": quoted}]), matches)
    assert [citation.quote for citation in citations] == [""]
    assert checks == ()


def test_a_dropped_quote_is_warned_about_beside_the_good_citation_it_was_found_next_to() -> None:
    """One bad quote costs one quote, not the other citation and not the answer."""
    matches = (match("Within four hours."), match("Restart the handset.", ordinal=1))
    text, _, citations, checks = parsed(
        reply(
            citations=[
                {"chunk": 1, "quote": "within four hours"},
                {"chunk": 2, "quote": "replace the handset"},
            ]
        ),
        matches,
    )
    assert text == "Within four hours."
    assert [citation.quote for citation in citations] == ["within four hours", ""]
    assert [check.rule for check in checks] == [UNSUPPORTED_QUOTE]


def test_a_check_on_a_dropped_quote_names_the_question_like_every_other_check() -> None:
    """One list whose `target` meant two different things could not be read at all (DEC-223)."""
    _, _, _, checks = parsed(
        reply(citations=[{"chunk": 1, "quote": "invented words"}]), (match("Within four hours."),)
    )
    assert [check.target for check in checks] == [ANSWERED[:60]]


def test_a_grounded_answer_from_the_fake_keeps_its_quote(knowledge_index: KnowledgeIndex) -> None:
    """End to end, so the check is proved not to reject the quotes a cooperative model really writes."""
    _, _, result = ask(knowledge_index, ANSWERED)
    assert result.citations
    assert all(citation.quote for citation in result.citations)
    assert UNSUPPORTED_QUOTE not in {check.rule for check in result.guardrails}


def test_the_word_limit_the_prompt_asks_for_is_the_one_the_parser_enforces() -> None:
    """Two numbers in two files that must agree; a reworded prompt is how they stop agreeing."""
    assert f"at most {QUOTE_WORDS} words" in load_prompt(ANSWER_PROMPT).system


@pytest.mark.parametrize("fence", ["```json\n{body}\n```", "```\n{body}\n```", "{body}"])
def test_a_reply_wrapped_in_a_code_fence_is_still_read(fence: str) -> None:
    """A model asked for bare JSON supplies a fence often enough that refusing over it costs answers."""
    matches = (match("Within four hours."),)
    body = reply(citations=[{"chunk": 1, "quote": "Within four hours."}])
    text, refused, citations, checks = parsed(fence.format(body=body), matches)
    assert (text, refused) == ("Within four hours.", False)
    assert [citation.chunk_id for citation in citations] == [matches[0].chunk.chunk_id]
    assert checks == ()


@pytest.mark.parametrize("raw", ["Certainly! Here is your answer, in prose.", "[1, 2]", "", "null"])
def test_a_reply_the_contract_cannot_read_is_dropped_for_the_refusal(raw: str) -> None:
    """Raising would turn a bad answer into a failed request; passing it on would put it on a screen."""
    text, refused, citations, checks = parsed(raw)
    assert (text, refused, citations) == (REFUSAL, True, ())
    assert [(check.rule, check.outcome) for check in checks] == [
        (MODEL_OUTPUT_MALFORMED, GuardrailOutcome.BLOCKED)
    ]


def test_a_malformed_reply_never_reaches_the_caller_as_an_answer(
    knowledge_index: KnowledgeIndex,
) -> None:
    """Whatever else happens to it, an unparsed blob is not what the customer is shown."""
    _, _, result = ask(knowledge_index, ANSWERED, mode=GroundedFakeMode.MALFORMED)
    assert result.refused
    assert result.called_model is True
    assert result.citations == ()
    assert result.answer == REFUSAL
    assert MODEL_OUTPUT_MALFORMED in {check.rule for check in result.guardrails}


def test_a_malformed_reply_is_refused_in_the_operators_own_words(
    knowledge_index: KnowledgeIndex,
) -> None:
    """With the judges off, so it is the parser refusing and not a judge that could not read a verdict."""
    client, _, result = ask(knowledge_index, ANSWERED, mode=GroundedFakeMode.MALFORMED, judged=False)
    assert result.refused
    assert result.answer == REFUSAL
    assert result.citations == ()
    assert MODEL_OUTPUT_MALFORMED in {check.rule for check in result.guardrails}
    assert len(completes(client)) == 1


# ---------------------------------------------------------------------------
# The two refusals, which are not one mechanism
# ---------------------------------------------------------------------------
def test_a_refusal_the_model_made_called_the_model_where_a_refusal_the_floor_made_did_not(
    knowledge_index: KnowledgeIndex,
) -> None:
    """The pair is the proof: without it `called_model` would look like a synonym for `refused` (DEC-219)."""
    prompted_client, _, prompted = ask(knowledge_index, ANSWERED, mode=GroundedFakeMode.REFUSING)
    floored_client, _, floored = ask(knowledge_index, DISJOINT)
    assert prompted.refused and floored.refused
    assert prompted.called_model is True
    assert floored.called_model is False
    assert len(completes(prompted_client)) == 2
    assert completes(floored_client) == ()
    assert prompted.retrieved > 0
    assert floored.retrieved == 0
    assert prompted.citations == floored.citations == ()


def test_a_refusal_the_model_made_still_reports_what_was_put_in_front_of_it(
    knowledge_index: KnowledgeIndex,
) -> None:
    """`retrieved` is what the extracts cost, not what the answer used, so a refusal still carries it."""
    _, _, result = ask(knowledge_index, ANSWERED, mode=GroundedFakeMode.REFUSING)
    assert result.retrieved == len(retrieved_for(knowledge_index, ANSWERED).matches)
    assert result.answer == REFUSAL


# ---------------------------------------------------------------------------
# The guardrails
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    ("mode", "rule"),
    [(GroundedFakeMode.BANNED, BANNED_PHRASES), (GroundedFakeMode.PII, PII_IN_OUTPUT)],
)
def test_an_answer_a_rule_blocked_is_replaced_by_the_refusal_and_keeps_its_check(
    knowledge_index: KnowledgeIndex, mode: GroundedFakeMode, rule: str
) -> None:
    """The blocked text is not shown, not cited and not forgotten: the check is what says it happened."""
    _, _, result = ask(knowledge_index, ANSWERED, mode=mode)
    assert result.answer == REFUSAL
    assert result.refused
    assert result.citations == ()
    assert result.called_model is True
    assert result.retrieved > 0
    blocked = [check for check in result.guardrails if check.outcome is GuardrailOutcome.BLOCKED]
    assert [check.rule for check in blocked] == [rule]


def test_a_blocked_answer_reports_the_checks_that_passed_beside_the_one_that_did_not(
    knowledge_index: KnowledgeIndex,
) -> None:
    """A reviewer asking what was looked at gets the whole sweep, not only the rule that fired."""
    _, _, result = ask(knowledge_index, ANSWERED, mode=GroundedFakeMode.BANNED)
    assert len({check.rule for check in result.guardrails}) > 1
    assert any(check.outcome is GuardrailOutcome.PASSED for check in result.guardrails)


def test_an_answer_nothing_objected_to_is_returned_with_its_checks(
    knowledge_index: KnowledgeIndex,
) -> None:
    """A clean answer still carries the sweep, so "checked and fine" is a thing the artefact can say."""
    _, _, result = ask(knowledge_index, ANSWERED)
    assert not result.refused
    assert result.guardrails
    assert all(check.outcome is GuardrailOutcome.PASSED for check in result.guardrails)


# ---------------------------------------------------------------------------
# The conversation, and what the artefact records
# ---------------------------------------------------------------------------
def test_only_the_last_turns_of_a_conversation_reach_the_prompt(
    knowledge_index: KnowledgeIndex,
) -> None:
    """A longer window would cost tokens on every question to serve the rare one that needs it."""
    history = tuple(Turn(role="user", text=f"turn number {number}") for number in range(HISTORY_TURNS + 3))
    client, _, _ = ask(knowledge_index, ANSWERED, history=history)
    prompt = completes(client)[0].prompt
    assert [turn["text"] for turn in history if turn["text"] in prompt] == [
        turn["text"] for turn in history[-HISTORY_TURNS:]
    ]
    assert "turn number 0" not in prompt


def test_a_conversation_shorter_than_the_window_is_carried_whole(
    knowledge_index: KnowledgeIndex,
) -> None:
    """Truncation is a ceiling, not a quota: two turns are two turns."""
    history = (Turn(role="user", text="is the sim live"), Turn(role="assistant", text="not yet"))
    client, _, _ = ask(knowledge_index, ANSWERED, history=history)
    prompt = completes(client)[0].prompt
    assert all(turn["text"] in prompt for turn in history)


def test_a_question_asked_with_no_history_carries_none(knowledge_index: KnowledgeIndex) -> None:
    """The client sends the conversation it has; nothing is stored, so nothing is remembered."""
    client, _, _ = ask(knowledge_index, ANSWERED)
    assert "Earlier in this conversation" not in completes(client)[0].prompt


@pytest.mark.parametrize("question", [ANSWERED, DISJOINT])
def test_the_prompt_version_is_the_version_of_the_prompt_that_was_loaded(
    knowledge_index: KnowledgeIndex, config_root: Path, tmp_path: Path, question: str
) -> None:
    """Recorded on the refusal too, which is the path that never renders the prompt it names."""
    prompts = tmp_path / "configs" / "prompts"
    prompts.mkdir(parents=True)
    shipped = (config_root / "prompts" / f"{ANSWER_PROMPT}.v1.md").read_text(encoding="utf-8")
    renamed = shipped.replace("version: 1", "version: 7")
    (prompts / f"{ANSWER_PROMPT}.v7.md").write_text(renamed, encoding="utf-8")
    _, _, result = ask(knowledge_index, question, config_root=tmp_path / "configs")
    assert result.prompt_version == 7
    _, _, shipped_result = ask(knowledge_index, question)
    assert shipped_result.prompt_version == load_prompt(ANSWER_PROMPT).version


@pytest.mark.parametrize("question", [ANSWERED, DISJOINT])
def test_the_latency_is_measured_from_a_monotonic_clock_on_every_path(
    monkeypatch: pytest.MonkeyPatch, knowledge_index: KnowledgeIndex, question: str
) -> None:
    """`_elapsed` subtracts two clock readings; stubbing the clock is what proves the subtraction runs,
    on the refusal path as much as the answered one, rather than a literal `0` that would pass either
    way."""
    ticks = iter([100.0, 100.25])
    monkeypatch.setattr("engine.generative.assistant.time.monotonic", lambda: next(ticks))
    _, _, result = ask(knowledge_index, question)
    assert result.latency_ms == 250


def test_the_question_comes_back_on_the_answer_exactly_as_it_was_asked(
    knowledge_index: KnowledgeIndex,
) -> None:
    """A row of `rag_eval.json` is read beside the reference set, so the key has to survive the trip."""
    _, _, result = ask(knowledge_index, ANSWERED)
    assert result.question == ANSWERED


def test_answering_a_question_writes_nothing(knowledge_index: KnowledgeIndex) -> None:
    """An evaluation asks a hundred questions; a hundred artefacts is what taking storage out avoids."""
    before = sorted(knowledge_index.root.rglob("*"))
    ask(knowledge_index, ANSWERED)
    ask(knowledge_index, DISJOINT)
    assert sorted(knowledge_index.root.rglob("*")) == before


# ---------------------------------------------------------------------------
# The failure that is not a refusal
# ---------------------------------------------------------------------------
def test_asking_an_index_that_does_not_exist_is_a_coded_error_and_not_a_refusal(
    knowledge_index: KnowledgeIndex,
) -> None:
    """Saying the documents do not answer it would be a lie about documents nobody could read."""
    with pytest.raises(GenerativeError) as error:
        ask(replace(knowledge_index, index_id="x_20260101_deadbeef"), ANSWERED)
    assert error.value.code == INDEX_NOT_FOUND
    assert "x_20260101_deadbeef" in error.value.message
