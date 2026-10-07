# Generative and hybrid features (Phase 3a)

Phase 1 and Phase 2 never call a model that was not fit on this deployment's own data. Phase 3a adds
three use cases that do: an assistant that answers from a client's own documents, a note that
explains a finished churn run in plain language, and copy written for a finished win-back run's
bands. All three call a large language model, and a large language model is the one component in
this codebase that can be wrong in a way no unit test catches - it can invent a fact, invent a
citation, or invent a number that looks exactly like a measured one. Every design choice in this
package exists to make that failure either impossible or visible, never silent, which is why this
document leads with reasoning rather than with an endpoint list: the facts below are downstream of
three rules, and the rules are the part worth understanding first.

This document is written from the code as it stands, and one rule governs it: a claim about
behaviour names the module and the function that makes it true, and a claim about something not yet
built says so in the same breath. A document that describes an intention in the present tense is
worse than no document, because the reader who trusted it stops checking. `evaluation.py`,
`root_cause.py` and `win_back.py` have all landed since the first draft of this file, and what has
not is the generative half of the API (`api/routes/generative.py`) and the job runner that writes a
generative job's status document, its guardrail report and its usage record; section 7 says which
artefacts are written by code today and which are so far only contracts.
`engine/generative/contracts.py` is complete and authoritative - it is the source of truth for what
a screen renders and what every artefact means - and is cited throughout.

---

## 1. Three capabilities, and what "hybrid" means

Phase 3a ships three use cases, and they are not three variations on one idea:

- **`ai-onboarding-assistant`** is purely **generative** (`ai_type: generative`,
  `generative.kind: rag_assistant`). Nothing is trained. Uploading is uploading documents, not rows;
  the "model" is retrieval over those documents, and the use case's target column
  (`reference_answer`) exists only so a reference set can grade the assistant's answers against a
  human's own words. There is no predictive half.
- **`rca`** is **hybrid** (`ai_type: hybrid`, `generative.kind: root_cause_summary`). Its predictive
  half is an ordinary Phase 1 binary classifier - churn probability, bands, SHAP reasons, all of it
  built and scored exactly as `targeted-advertisement`'s is. Its generative half reads a **finished**
  run of that classifier and writes a plain-language note explaining what the numbers mean, segment
  by segment.
- **`win-back-campaign`** is also **hybrid**. Its predictive half ranks churned customers by
  reactivation propensity, band by band, exactly as any Phase 1 classifier does. Its generative half
  reads a **finished** scoring run and writes win-back message templates, one set per band per
  channel, ready for a person to approve.

"Hybrid" therefore has one precise meaning in this codebase: **the predictive engine produces the
numbers, and the generative engine explains or acts on them, and the dependency between the two runs
in one direction only.** `engine/generative` reads a finished run's own artefacts -
`feature_importance.json`, `row_explanations.parquet`, `scores.csv`, a run's bands - the way any
other reader of a run directory would, through `Storage`, after the run has already reached a final
state. Nothing under `engine/generative` is imported by `engine.pipeline` or by any module under
`engine.stages`, a root-cause or copy job is not a pipeline stage, and neither job appears in
`StageKey` (`engine/generative/__init__.py`; DEC-210). A test proves the one-way boundary rather than
leaving it to convention.

That direction is not an implementation convenience; it is what makes the rest of this package
possible to reason about. The predictive engine was built, tested and shipped in Phase 1 with no
knowledge that an LLM would ever exist, and it can go on being tested, deployed and reasoned about
exactly that way in Phase 4 whether or not a client turns any generative use case on. A hybrid use
case's champion model is retrained, promoted and scored by the same rules as any predictive one
(DEC-012 keeps `rca` and `win-back-campaign` purely predictive through Phase 1, with no
`generative:` block at all until this phase adds one). And because a generative job runs over a
**finished** run rather than inside one, it cannot rewrite that run's own record of what happened: a
root-cause or copy job keeps its own status document - `root_cause_status.json`, `copy_status.json`
- rather than appending to the run's `status.json`, which stays exactly what it was the moment the
run finished (DEC-211). A run is evidence. A generative job reads that evidence; it never edits it.

---

## 2. Three rules, each enforced by code

The package docstring (`engine/generative/__init__.py`) states three rules that hold everywhere in
this package, and each one names the module that enforces it rather than leaving it as something a
prompt asks a model to do nicely. That distinction is the point: a prompt is a request, and a request
can be ignored, misread or jailbroken. What follows are checks a model's output has to pass before
anything downstream of it can trust it, and each is ordinary Python that runs whether or not the
model cooperated.

**Grounded or nothing.** The assistant answers from retrieved chunks or refuses; a root-cause
summary's every claim carries a reference to something in the evidence pack it was given; campaign
copy may use only whitelisted fields. Three different mechanisms enforce this, one per flow, because
"grounded" means something different in each:

- For the assistant, `engine/generative/assistant.py`'s `_parse` checks both halves of a citation
  against what was actually supplied. The chunk *number* must be among the extracts that were put in
  front of the model, or the citation is dropped and an `UNKNOWN_CITATION` guardrail check records
  it; the *quote* must appear in the cited chunk's own text, flattened for case and whitespace, or
  the quote alone is dropped for an empty string and an `UNSUPPORTED_QUOTE` check records that
  (DEC-226). The second check exists because everything else a `Citation` carries - chunk id,
  document, heading, similarity - is copied from the match the engine itself retrieved, so an
  unchecked quote would be the one invented thing in a row of real provenance, read by the person who
  wanted to verify the claim. The faithfulness judge is then given exactly those extracts as its only
  source of truth - a claim the model made up has nothing to be checked against but the real
  documents.
- For root-cause summaries, the work is split between the data model and one caller, and a flow
  author needs to know which half is which. `RootCause._has_evidence`
  (`engine/generative/contracts.py`) is a `model_validator`, so the **data model** refuses one thing
  and one thing only: a cause whose `evidence_refs` is empty cannot be constructed by any code path,
  now or later. Membership is **not** the data model's to check - a `RootCause` has never seen the
  pack it was written from - and is enforced by the caller: `grounded_causes`
  (`engine/generative/root_cause.py`) drops any parsed cause whose refs are not a subset of
  `EvidencePack.reference_ids` before anything is stored, and a segment that grounds nothing is
  retried and then failed with `UNGROUNDED_CLAIM`. A future flow that builds a `RootCause` from
  somewhere else therefore inherits the empty-refs guarantee for free, and has to make the
  membership check itself.
- For campaign copy, `CopyTemplate.fields_used` is compared against
  `generative.campaign_copy.allowed_fields` by the `allowed_fields_only` guardrail rule
  (`engine/generative/guardrails.py`), which names any `{{placeholder}}` the model invented that has
  no data behind it.

**Everything generated passes guardrails before it is stored.** `Guardrails.check`
(`engine/generative/guardrails.py`) is the single gate every generated text passes through, and it
runs in a fixed order for a reason: the deterministic rules in `RULES` - empty output, length,
the one-way-sender "Reply STOP" check, required lines, allowed fields, banned phrases, PII, URL
whitelist, language - are ordinary regular
expressions and dictionary lookups, so they cost nothing and cannot be talked out of a verdict by
clever prompting. Only text that survives every one of them is handed to `_judge`, which asks an LLM
judge about the things a regular expression cannot decide - is every claim supported, does this read
as pressure, would this offend the person who receives it. A text that fails a deterministic rule is
never sent to a judge, which is not only thrift: a judge asked to score a text that was already going
to be refused would spend money to produce a verdict nobody will read.

**Cost is an artefact.** `Meter` (`engine/generative/budget.py`) is the only way any flow talks to a
model: every `complete`, `judge` and `embed` call goes through it, and `_check_budget` runs *before*
the call is made, not after. A job that has already used `max_calls_per_run` calls, or whose spending
has already reached `max_cost_usd_per_run`, is refused with `BUDGET_EXCEEDED` before the next call
happens, so it makes **not one call more** than it was allowed. The call ceiling is exact, because a
call is a call before it is made; the cost ceiling is not, because what a call will cost is known
only once it comes back, so the run can stop at or slightly above its ceiling rather than below it
(DEC-227, and section 8 for what that means when a price is missing). This is enforced structurally
rather than by convention because there is no second way to reach an `LLMClient` from inside a
generative flow: `assistant.answer`, `root_cause.build_root_cause_summary` and `win_back`'s copy
flow hold a `Meter` and nothing else that can make a call.

---

## 3. The RAG pipeline, end to end

The assistant's whole job is: read a question, decide honestly whether the uploaded documents answer
it, and if they do, answer with citations a person can check. The pipeline that makes that possible
has seven steps, and the interesting part of each one is what it deliberately throws away.

**1. Parse.** `engine/generative/parsers.py` reads an uploaded PDF, DOCX, Markdown, web page (`.html`
or `.htm`) or plain-text file into ordered, headed `Section`s. Five formats share one goal that is
harder than it sounds: recovering *structure* rather than inventing it. DOCX, Markdown and HTML carry
real heading markers, so those three readers are told the answer directly (a web page's scripts,
styles, navigation and form controls are dropped first - section 11). PDF and plain text have been flattened to lines, so the parser
infers a heading from shape - a short, capitalised line that finishes no sentence, sitting after one
that did - deliberately loosened to sentence case rather than Title Case, because "How a refund is
paid" is a heading in every policy document ever written and is not Title Case. A table's cells are
joined the same way by all four readers, so the same tariff table reads as the same sentence whichever
format it arrived in. A document that fails to parse does not fail the build: `build_index`
(`engine/generative/index.py`) records the failure against that one document and carries on, because
one corrupt PDF in a two-hundred-document knowledge base should cost that PDF, not the index.

**2. Heading-aware chunk.** `engine/generative/chunking.py` cuts each section into the passages that
get embedded, retrieved and cited, under two rules that both cost something on purpose. A chunk never
crosses a heading, so `Chunk.section` is a fact a citation can state rather than a guess - the cost is
that a short section becomes a short chunk instead of being packed efficiently with the next one. A
chunk never splits a sentence, so what is embedded and quoted is always something somebody actually
wrote - the cost is that `chunk_tokens` is a target, not a ceiling, and a single long sentence is
emitted as an oversized chunk of its own rather than being cut. Nothing here reads the clock or
depends on dict order: `chunk_id` is derived from `(doc_id, ordinal)` alone, which is what lets a
rebuild of an unchanged document reproduce byte-identical chunks and be skipped.

**3. Embed.** Each chunk is embedded not as its bare text but as `"<doc id> <section>\n\n<text>"`
(`chunking.embedding_text`) - its provenance in front of its words. This is DEC-217, and it is worth
explaining why it is worth a whole decision entry: the words a question uses are very often in the
heading and nowhere in the body. "What is the late payment fee?" shares its entire vocabulary with a
heading reading "Late payment and reconnection"; the paragraph underneath says "a fee of Rs 100 or 2%
of the outstanding amount, whichever is higher" and never repeats the heading's words at all.
Embedding the passage alone throws that match away on exactly the questions a knowledge base exists
to answer. Measured over the 45 answerable questions of the reference set, this is the difference
between retrieving the right document 28 times and retrieving it 40 times - and the margin between a
real question and an off-topic one *widens* rather than narrows, because a heading is specific where
a body paragraph is discursive. The question itself is still embedded bare: the prefix is context the
passage lacks, not a format both sides of a comparison must share. Only what is new is embedded on a
rebuild, and what counts as "not new" is a document's fingerprint **and** its `doc_id` together -
never either alone (DEC-220, narrowed by DEC-221). The fingerprint is needed because an edited
document's chunks keep the same ids as the ones they replaced, so matching on id alone would hand a
rewritten passage the vector of the passage it no longer says. The id is needed because the
fingerprint answers only "are these the same bytes?": a renamed file matches on fingerprint and would
be handed the old manifest entry, so every citation would go on naming a file that no longer exists,
and two uploads of identical bytes would both match one entry and write one document's chunks into
the index twice under the same `chunk_id`. `_reusable_chunks` is therefore keyed on
`(fingerprint, doc_id)`, and a renamed file is re-embedded - which is the right trade, because
`chunk_id` is built from `doc_id` and its chunks really are different chunks.

**4. Index.** `engine/generative/vectorstore.py`'s `LocalVectorStore` writes `chunks.parquet` and
`embeddings.parquet` as two files rather than one, because a citation reads a chunk's text and a
question reads every vector, and keeping them apart means neither operation pays for data it will not
touch. Two files also means two writes, each atomic on its own and neither atomic with the other, so
`search` compares the two lengths before it zips them and raises `INDEX_CORRUPT` naming both counts
when they disagree (DEC-224). That is the difference between refusing to search an index a crash left
half-rebuilt and pairing every passage with another passage's vector, which produces confidently
wrong citations that look exactly like right ones. The `VectorStore` protocol is four operations -
`write`, `chunks`, `search` and `exists` - deliberately small enough that Phase 4's OpenSearch
implementation can stand behind the same four without either side knowing the other exists.

**5. Retrieve, with a similarity floor and MMR.** `engine/generative/retrieval.py`'s `retrieve` makes
two decisions, and both are about what to leave out. The **similarity floor** drops anything below
`min_similarity`: a vector search always returns its `top_k`, however badly they match, so asking a
telecom knowledge base about a share price would otherwise hand back the five least-unrelated
paragraphs it owns. **Maximal marginal relevance** then thins what survives the floor, because the top
matches by raw similarity are routinely near-duplicates - the same policy line repeated in an FAQ, a
table and the prose around it - and handing a model five wordings of one fact wastes the context a
second, different fact needed. `mmr` sorts its candidates itself rather than trusting that they
arrived sorted (DEC-225), so its promise that the first pick is always the best raw match is a
property of the function rather than of the callers it happens to have: a reader who checks the top
citation finds the passage they expected. Neither decision is a model's to make and neither costs
a call; what reaches the prompt is `retrieve`'s output and nothing else, which is what makes "answer
only from the extracts" a rule the engine enforces rather than a request the prompt makes.

**Hybrid ranking, dense floor.** The search itself is *hybrid* by default (DEC-1260): given the
question's words as well as its vector, `LocalVectorStore.search` ranks every chunk by
`(1 - w) * cosine + w * keyword`, where `keyword` is the chunk's Okapi BM25 score for the question
(`k1 = 1.5`, `b = 0.75`, over the chunk's heading and text) scaled to 0..1 by the score a chunk
containing every question word would approach, and `w` is `generative.rag.bm25_weight` (default
`0.25`, `0` for a pure vector search). Embeddings place "the late payment fee" near "charges for paying
after the due date", which keywords cannot; keywords find the plan name, product code or clause
number a question quotes exactly, which an embedding can place a little too far away. Ranking is all
BM25 is allowed to do: every `Match` still carries the plain cosine as `similarity`, and **the floor is
applied to that cosine, never to the blend** (DEC-1261). The refusal rule therefore keeps the meaning
DEC-218 calibrated - a strong semantic match is never pushed under the floor by a low keyword score,
and a passage that merely shares a word with the question is never let over it - while MMR trades on
the blended score (`Match.rank`), and a citation shows the cosine. The BM25 statistics of an index
(word counts, document frequencies, lengths) are computed once per distinct set of chunk texts and
memoised (`vectorstore.bm25_corpus`, DEC-1262), so a question costs one pass over its own words, not a
re-tokenisation of the knowledge base; the cache key is the texts themselves, so an index rebuilt under
the same id can never be scored with stale statistics. The same setting applies everywhere a question
is retrieved - an answer, and the evaluation's own retrieval - whichever client the meter calls.

**6. Answer or refuse.** `engine/generative/assistant.py`'s `answer` embeds the question, retrieves,
and only then decides whether to call a model at all - see the refusal discussion below. When it does
call, the rendered prompt numbers the extracts, and that numbering *is* the citation vocabulary: the
model cites a position, never a filename it might misremember.

**7. Cite.** Every citation in an `AssistantAnswer` carries the chunk id, the document, the heading
and the cosine similarity that retrieved it - all four copied from the match the engine itself chose,
so a reader sees not just what was cited but how confidently - plus a quote of at most 25 words. The
quote is the one field the model supplies, and it is checked twice before it is kept: trimmed to
`QUOTE_WORDS` by the engine rather than trusted from the model's own count, and then looked for in
the cited chunk's own text with case and whitespace flattened on both sides. A quote that is not
found there is dropped for an empty string and an `UNSUPPORTED_QUOTE` guardrail check is recorded
(DEC-226) - so a citation may show no quote, but it can never show words the chunk does not
contain. A screen renders an empty quote as no quote.

### Four refusal mechanisms, and why they are different

A refusal is not one thing in this pipeline. Four different things end with `refused=True` and the
same sentence on the screen, and conflating them would hide the one number that matters most about a
deployment: how much of its refusal behaviour is free. Exactly one of the four is free; the other
three have already paid for a call by the time they refuse, which is what
`AssistantAnswer.called_model` is for.

All four say the same words, and they are the *operator's own*: `rag.refusal_message` is
configuration, so what a customer reads when the documents cannot help them was written by the
deployment rather than improvised by a model under pressure to say something.

**1. The similarity floor - the free one.** In `assistant.answer`, before a model is ever called:
when `retrieve` finds nothing above `min_similarity`, `_refusal` returns immediately with
`called_model=False`, `retrieved=0` and no guardrail checks at all, because nothing was generated and
a passing check would claim otherwise. This is the single most important line in the module, and for
a concrete reason - it is both the honest answer ("the documents have nothing close to this") and a
free one.

**2. The model's own refusal - paid.** The extracts passed the floor, but the model decided none of
them actually answers the question asked - "the extracts answer a neighbouring question but not this
one," in the prompt's own words (`configs/prompts/assistant_answer.v1.md`) - and returned
`"refused": true` in its structured reply. This costs what any other call costs, because reaching the
decision required reading the extracts.

**3. A guardrail block - paid, and the answer is discarded.** The model answered, and a deterministic
rule or a judge refused what it wrote. `answer` then returns the refusal sentence with
`called_model=True`, `retrieved` unchanged and `citations` emptied, and the whole sweep of checks -
the one that blocked and the ones that passed beside it - travels on the answer. The generated text
is not stored, not cited and not shown; the check is what records that it happened.

**4. An unreadable reply - paid, and the reply is dropped rather than relabelled (DEC-222).** When
the reply is not a JSON object the contract can read, `_parse` discards it and returns the refusal
sentence in its place, with a `MODEL_OUTPUT_MALFORMED` check recording that the parse is what failed.
Returning the blob under `refused=True` would satisfy the flag and still put an unparsed model
completion in front of a customer, which is the outcome this mechanism exists to prevent. The reply
is not lost: it is what the model was metered for.

The three paid mechanisms all leave guardrail checks behind, and so do the citation and quote checks
on an answer nothing refused; only the floor leaves none, because nothing was generated to check.
Every check on an answer, wherever in the flow it was raised, is targeted at the *question* and never
at the answer, so `answer.guardrails` reads as one column rather than two meanings interleaved
(DEC-223).

Why the floor cannot substitute for the model, and why the model's refusal is not free - that is,
why mechanisms 1 and 2 are not one mechanism seen twice: retrieval and refusal by relevance are
different signals. DEC-219 states this precisely by giving two concrete
questions against the reference set's lexical fake. "Can you recommend a competitor with a cheaper
plan?" shares almost no vocabulary with the corpus, scores 0.154 against every chunk, and the floor
refuses it correctly with no call at all. "How many employees does Northwind Telecom have?" scores
0.289 - *above* several genuine questions - because the brand name and the question words appear in
every document a lexical model has no way to tell apart on meaning; a real embedding model separates
the two by what they mean, not by which words they share. That is why the reference set's floor test
uses a question whose vocabulary is genuinely disjoint (proving the floor really did catch nothing,
for the right reason), why the prompt-level refusal is tested separately with
`FakeLLMMode.REFUSING`, and why the same-domain off-topic case - where only meaning, not
vocabulary, tells the two questions apart - is a Bedrock assertion rather than one the fake can stand
in for.

One more decision belongs here because it is about the same number - the similarity floor - seen
from another angle: **DEC-218, the floor is calibrated per embedding model, not a universal
constant.** `min_similarity: 0.25` decides whether the assistant refuses for free, which makes it the
single most consequential number in the RAG configuration, and it does not travel between embedding
models. A real embedding model places every vector in a narrow cone, where a genuinely relevant pair
scores 0.3 to 0.9; the reference set's lexical fake hashes words into near-orthogonal buckets, where a
relevant pair scores 0.2 to 0.4. The shipped `0.25` is calibrated for Bedrock and is deliberately not
moved to suit the fake - a quality claim about the floor is a Bedrock claim, made under `-m bedrock`,
and `ChunkConfig.embedding_model_id` is recorded on every index precisely so "is this floor right for
this index" stays an answerable question when the embedding model changes.

---

## 4. Prompts are config

A prompt in this package is never a string embedded in Python. Every prompt lives at
`configs/prompts/<name>.v<N>.md` - YAML front matter naming the prompt, its version, its purpose and
the variables it needs, then a `# System` section and a `# User` section - loaded and rendered by
`engine/generative/prompts.py` (DEC-202). Two properties make that loader worth having rather than an
f-string.

A render either has everything or fails loudly. The rendering environment is jinja2's
`SandboxedEnvironment` with `StrictUndefined`, and the declared `variables` list is checked against
what the caller supplied *before* anything is rendered, so a missing variable is a
`PROMPT_VARIABLE_MISSING` error at render time rather than a prompt with a silent hole in it,
discovered only once a completion comes back confidently wrong. The sandbox matters because a prompt
is a file an operator edits: `SandboxedEnvironment` refuses attribute access that would reach into an
object beyond what the template was handed, however the template is written.

A prompt's hash goes where the wording can be checked against it. `Prompt.content_hash` is a digest
of the file exactly as it sits on disk, computed once per load. A reworded prompt gets a different
hash whether or not its version number changed, and `RootCauseSummary.prompt_hashes` and
`CopyBatch.prompt_hashes` carry that hash alongside the version for exactly the artefacts whose text
is meant to be read - and approved - well after it was generated: a reviewer looking at a template
written last month can check the exact wording behind it without having to trust that the file at
that version number still says what it said then. Every other generative artefact still records
`prompt_versions`
(name to version), which is the provenance a rebuildable index or a freshly graded reference set
needs, and is enough as long as a shipped `.v<N>.md` file is never edited in place - the versioning
scheme's whole purpose is to make "a different wording" and "a different version file" the same
event.

---

## 5. Guardrails: deterministic first, free; judges second, metered

`configs/guardrails.yaml` is read by `Guardrails.check` and says, for each rule, one of `block`,
`warn` or `off` - and `off` means the rule produces no check at all, never a silently passing one, so
a guardrail report can never claim a text was checked for something nobody checked it for. The nine
deterministic rules run first and in a deliberate order (`RULES`): an empty output makes every later
rule meaningless, so it runs first; a length failure is worth reporting before a phrase failure inside
a text that was never going to be used anyway. Every one of the nine is `block` in the shipped
configuration except `language_match`, which is `warn`: telling English from a wrong script needs
only a character-class check, but telling Hindi from Marathi needs a model this rule does not have,
so it says what it can see and nothing more.

Only text that survives every deterministic rule reaches a judge, and a judge is itself a metered LLM
call, configured under `llm_judge` as `{threshold, on_fail}` per judge name - `faithfulness` and
`compliance` at 0.80, `toxicity` at 0.90, all `block` in the shipped file. The fourth judge
`JUDGE_PROMPTS` knows, `correctness`, is not in that file and is not a gate: it is a *grading* judge
that `evaluation` calls itself, so `RagEvalQuestion.correctness` is a score on a screen rather than a
verdict that refuses an answer (nothing in the shipped configuration sets a bar for it).

A judge that does not answer
in the JSON its prompt asked for scores 0 (`guardrails._parse_verdict`) rather than being treated as a
pass, because a verdict nobody can read must not become a silently disabled guardrail. `retries`
(default 2) bounds how many times a caller may regenerate a text before a failure is final - the
guardrails module itself never edits or repairs a generated text; a guardrail that rewrote its input
would turn the stored artefact into a record of something no model actually produced.

---

## 6. Redaction asymmetry: the difference is whose data it is

The same PII detectors run over two very different kinds of text in this package, and they are
handled oppositely on purpose (DEC-216). A **knowledge document** uploaded to build an index is the
client's own published material, and a support email address or an account-number format printed in
it is very often the exact thing a question is about; redacting it would damage the one answer that
address exists to give, and leave the assistant citing `[REDACTED:email]` as the address to write to.
`build_index` therefore indexes such a document exactly as written and only warns - `PII_IN_DOCS:<kinds>`
on `DocIndexManifest.warnings`, naming the kinds found and never a value, so an operator who genuinely
should not have published something is told, in the artefact, and can withdraw it. The
engine does not make that call for them.

**Complaint text**, by contrast, is a customer's own words, was never published, and reaches a prompt
only as an evidence sample for a root-cause summary. `engine/generative/redaction.redact` is applied
to every complaint before it goes into an `EvidencePack`, unconditionally.

Both paths reuse `engine.stages.ingest.PII_DETECTORS` rather than a second pattern set, with one
deliberate exception: the `name` detector is left out of the free-text scanner (DEC-213). Its pattern
- one to four capitalised words - is exactly right over a column, where an open vocabulary
distinguishes a roster of people from a category, and exactly wrong inside a sentence, where it also
matches the first word of every sentence, every product and every place. Running it over prose would
redact a complaint into uselessness rather than protect it, so it is left out and the omission is
written down at the one place `_SCANNERS` is built, rather than left for a later reader to rediscover
by noticing an absence.

---

## 7. The artefacts

Every filename this package can write is one of two maps in `engine/generative/contracts.py`:
`GENERATIVE_ARTEFACTS` (a JSON document, one pydantic model) or `GENERATIVE_TABULAR_SCHEMAS` (a
table, one model per row). They are parallel to the predictive engine's `ARTEFACT_REGISTRY` and
`TABULAR_SCHEMAS` rather than inside them, because `tests/unit/test_artefact_registry.py` pins those
two name for name and a generative artefact is not a predictive one (DEC-210).

**What is true today, and what is intended, are different sentences, and this paragraph keeps them
apart.** Today `api/routes/runs.py:read_artefact` whitelists `ARTEFACT_REGISTRY` and
`TABULAR_SCHEMAS` and nothing else, so `GET /runs/{id}/artefacts/{name}` answers
`404 ARTEFACT_UNKNOWN` for every generative filename; `GENERATIVE_ARTEFACTS`,
`GENERATIVE_TABULAR_SCHEMAS` and the `generative_artefact_model` lookup written for that route have
no caller anywhere in the tree yet. The four maps' names do not in fact collide - that can be
checked in a REPL - but no test asserts it, so "proved disjoint" is not something this document may
say. (`contracts.py`'s own module docstring states the union and the disjointness in the present
tense for the same reason this document used to: both describe the design DEC-210 settled on, and
neither describes what the artefact route does today.) Index artefacts are further out of reach
still: an index lives at `indexes/{index_id}/` (`engine.storage.index_key`), beside runs rather than
inside one, so no run route could serve `doc_index_manifest.json` or `chunks.parquet` whatever its
whitelist said. **The intended design** is
that the run route's whitelist becomes the union of the predictive and generative run maps, with the
disjointness a pinned test rather than a coincidence, and that the index artefacts are served by
`api/routes/generative.py` under `GET /indexes/{index_id}/...` - which is the same reason
`generative_artefact_model` returns `None` rather than raising for a name it does not own: the caller
that will ask is a route that wants to fall through to the predictive map.

Two groupings matter for knowing when to expect a file. `INDEX_ARTEFACTS` is what a finished index
build writes - the manifest, its own status document and the two Parquet files - joined by
`rag_eval.json` only once a reference set has actually been graded. `RUN_GENERATIVE_ARTEFACTS` is
what a root-cause or campaign-copy job may *add* to a run directory that the predictive engine
already finished writing: it adds files, and it never rewrites the ones already there (DEC-210,
DEC-211).

**Which of these are written by code today.** `index.build_index` writes `doc_index_manifest.json`
and, through `LocalVectorStore`, `chunks.parquet` and `embeddings.parquet`; `evaluation.evaluate`
writes `rag_eval.json`; `win_back` writes `copy_batch.json` and `copy_messages.csv`. The three status
documents, `guardrail_report.json`, `llm_usage.json` and `root_cause_summary.json`, are so far
contracts only: their producer is the job runner that has not landed yet, and the objects exist
without a writer - `root_cause.build_root_cause_summary` returns a `RootCauseSummary`, `Meter.usage`
returns an `LlmUsageReport`, and nothing puts either into storage. The "Written when" column below is
therefore the contract each file is written against, not a promise that something writes it today.

| File | Model | Written when | What a reader uses it for |
|---|---|---|---|
| `doc_index_manifest.json` | `DocIndexManifest` | An index build finishes, whether or not every document parsed. | What is in the index and how it was built - document list, fingerprints, chunk counts, the settings a rebuild is compared against, and any `PII_IN_DOCS` warning. The record a reader asks "which documents, which settings, when?" of. |
| `index_status.json` | `GenerativeStatus` | Continuously while an index build runs. | The Running screen for an index build: one stage per document plus the embedding step, polled the same way a predictive run's `status.json` is. A job is never left `running`: whatever it raises, the status ends `failed` with a code and a plain message - the engine's own for a coded error, the AI service's for a provider error, and `JOB_FAILED`'s fixed sentence (traceback to the log) for anything else. A reference set that cannot be graded fails only its `evaluate` step; the index is still built (DEC-1267). |
| `rag_eval.json` | `RagEval` | A reference set is graded against a finished index. | The Results screen for RAG quality: pass rate against `reference_set.pass_threshold`, retrieval hit rate, mean faithfulness and correctness, and the worst-scoring questions stored first so a reviewer reads the head of the list rather than sorting one. A question the AI service failed on is stored as not graded (`failure: provider_error`, its `error_code` and plain `error_message`, null scores) and counted in `aggregates.errored`, outside every other figure (DEC-1266). |
| `root_cause_summary.json` | `RootCauseSummary` | An RCA job finishes over one finished churn run. | Per-segment headlines, root causes and recommended actions, each summary sitting beside the `EvidencePack` it was written from, so every numeric claim on the screen is read from the pack rather than from the model's prose. |
| `root_cause_status.json` | `GenerativeStatus` | Continuously while an RCA job runs. | The Running screen for an RCA job: one step per segment, so `generate:High` and `generate:Medium` can be watched, retried and reported on independently. |
| `copy_batch.json` | `CopyBatch` | A campaign-copy job finishes over one finished scoring run. | The templates awaiting review, plus the audience they cover and the holdout (control rows, suppressed rows, out-of-band rows) that a later uplift comparison needs to have been recorded honestly. |
| `copy_status.json` | `GenerativeStatus` | Continuously while a campaign-copy job runs. | The Running screen for a copy job: one step per band per channel. |
| `copy_messages.csv` | `CopyMessage` (tabular) | Templates in `copy_batch.json` are rendered per scored row. | One row per entity per channel: the rendered text, which template and variant produced it, and why a rule refused a specific rendering when one did - a placeholder can clear a template's length check and still push a rendering over it once real data fills the field. |
| `guardrail_report.json` | `GuardrailReport` | Any job that generated at least one text, alongside that job's own artefact. | Every check that ran over everything the job generated, including the checks for text that was blocked and therefore never stored anywhere else - "nothing was written for the Medium band" is an answer a reviewer needs, and this file is where it lives. |
| `llm_usage.json` | `LlmUsageReport` | Any job that made at least one LLM call, even a job that failed partway through. | What the job spent: calls, tokens and cost broken down by model and by purpose, cache hits, and the budget ceilings that were in force - the artefact behind the cost line on every generative screen. |
| `chunks.parquet` | `Chunk` (tabular) | An index build writes or reuses each document's chunks. | The passages themselves: document, heading, page, ordinal and text - what a citation quotes and what a rebuild's incremental skip is checked against. |
| `embeddings.parquet` | `ChunkEmbedding` (tabular) | An index build embeds or carries over each chunk's vector. | The vectors a question is searched against, kept in a separate file from the text so a citation never pays to load a float array it will not look at. |

---

## 8. Cost and budget

`Meter` is the choke point every flow's spending passes through, and the artefact it produces,
`LlmUsageReport`, is designed around one asymmetry: **calls, tokens and latency are always real
measurements; cost is sometimes unknowable, and unknowable is not the same as zero.**

`configs/llm_prices.yaml` ships with its shape defined and no rows in it, deliberately (DEC-208).
Prices differ by region and by negotiated contract, so any number shipped here would be a figure shown
to a user that nobody at this deployment actually measured - the exact thing plan section 13.3
forbids of a fabricated accuracy score, applied to cost. Until an operator fills the file in, every
call is still metered in full: `LlmUsageReport.totals` carries the real call count and the real token
counts, and `cost_estimate_usd` is `None` rather than `0.0`, because a zero is a measurement and there
was none. `PRICE_UNKNOWN:<model_id>` is recorded in `warnings` naming exactly which model had no
price, so an operator knows which row to add. `max_calls_per_run` needs no price and still binds even
on an entirely unpriced deployment; `max_cost_usd_per_run` cannot bind on what cannot be priced, and
`Meter.budget_usd` returns `None` in that case rather than pretending a ceiling that cannot be checked
is being enforced.

"That case" is wider than an empty price file, and the widening is DEC-227. `budget_usd` is `None`
whenever *any* call so far could not be priced - the table empty, or a table with a row for the
generating model and none for the judge - because that is exactly the condition under which
`cost_so_far` is `None` and the cost comparison in `_check_budget` cannot be made. A half-filled
price file would otherwise put a real-looking ceiling into `llm_usage.json` while nothing was being
tested against it, which is the same fabrication as a cost of `0.0` wearing different clothes. What
still binds on such a deployment is the call ceiling, and it is the only thing that does.

The cost ceiling's promise is also narrower than "not a cent over", and worth stating exactly.
`_check_budget` runs before a call and compares what has *already* been spent, because what a call
will cost is known only once it comes back - the output tokens are the model's to choose. So a run
stops at the first call after its spending reached the ceiling, and the call that took it there may
have taken it past. The ceiling bounds how much further a job goes, not its final total. Projecting
a call's cost from `max_output_tokens` and refusing on the projection would bound the total, at the
price of refusing real work on the strength of a number nobody measured - which is the trade plan
section 13.3 settles the same way everywhere else in this package (DEC-227).

`TOKENS_ESTIMATED` exists for a narrower reason (DEC-215). The `LLMClient` protocol's `embed` returns
vectors and nothing else, because no provider reports token usage through an embedding call the way
it does through a completion, so `Meter.embed` works the token count out from characters using the
same `APPROX_CHARS_PER_TOKEN` ratio the chunker sizes a chunk with. That is a perfectly good number to
budget against - it is consistent with every other place in the engine that means "a token" - and a
poor one to bill a customer from, which is exactly the distinction the warning exists to preserve: a
number is either measured or it says it is estimated, and nothing in this package lets the two look
the same on a screen.

---

## 9. Backends: the AI service, Bedrock and the test model

**Which model answers is a setting a person makes, not a file they edit** (DEC-1140). Under
Connections, **Deliverable AI** is the language service for everything in this document: the
document assistant, root-cause summaries, campaign copy, and the judge and guardrail checks. (Guided
setup's chat helper is **Product AI**, a separate setting: see `docs/CONNECTIONS.md` and
`docs/AGENTS.md`.) They are separate because the deliverable is the customer's - their documents,
their campaign data, their bill, their choice of provider - while the helper is your team's tool.

Every generative route resolves its client through `engine.ai_service.resolve_client(..., slot="deliverable")`
(`api.routes.generative._with_deliverable_ai`), in this order:

1. the **Deliverable AI** the person saved (Amazon Bedrock, OpenAI, Claude, OpenRouter, Hugging Face or
   any OpenAI-compatible server; `engine/llm_http.py` speaks the two HTTP protocols);
2. else the **Product AI**, if that one is saved (so one connection is enough to start);
3. else the use case's own `generative.llm.backend: bedrock`, exactly as before (below);
4. else the test model, **only** when `MARKETING_AI_ALLOW_FAKE_AI` is on (the test suite and developer
   checks; a real deployment must never set it);
5. else nothing: the route answers `409 AI_NOT_CONNECTED` ("No AI service is connected for Deliverable
   AI.", with the fix "Open Connections → AI service → Deliverable AI and connect one."), *before*
   anything is written, so a refused request leaves no half-made index or status file behind.

The model names of the service that answers - not the use case file's - are what every metered call is
made against, what an index records (`backend: external` for a saved third-party service), and what
`llm_usage.json` prices. `LlmBackend.EXTERNAL` is never written in a use-case file (`LlmConfig` refuses
it). A saved provider without embeddings (Claude), or a service saved without an embedding model,
embeds by **keyword hash** (`engine.llm.keyword_hash_vector`, model id `keyword-hash-v1`): the same
deterministic lexical vector the test model's grounded mode uses, so retrieval is real, matches by shared
words, and involves no model. An index remembers the embedding model it was built with; changing the
embedding model means rebuilding it.

**The local embedding model** (DEC-1263). Instead of the provider's embeddings or the keyword hash, a
saved service may name `BAAI/bge-small-en-v1.5` as its embedding model: an open-source model (384
dimensions) that runs on this server through `sentence-transformers`, so the documents are searched by
meaning even with a provider that has no embeddings (the Connections screen offers it as a tick box for
those, and in the list for the others). It is an optional extra, not a dependency, because it pulls in
PyTorch: `pip install 'marketing-ai[local-embeddings]'`. Its first use downloads the weights from
Hugging Face unless they are already in the server's cache. It never falls back: without the extra the
build or question fails with `LOCAL_EMBEDDINGS_NOT_INSTALLED` and that install command, and a model that
cannot load (no network for the first download) fails with `LOCAL_EMBEDDINGS_UNAVAILABLE` - an index
built from one kind of vector and searched with another would be a dimension error at best and wrong
answers under a manifest naming the wrong model at worst. The model loads once per process, under a
lock. A floor calibrated for one embedding model is not automatically right for another (DEC-218).

The rest of this section describes the two backends a **use-case file** can name and the test model.
A use case's `generative.llm.backend` is now only a *fallback*: `bedrock` (step 3 above) is honoured when
nothing is saved, `fake` (the shipped default) means "no service configured in the file", and neither
answers anybody unless step 3 or 4 applies.

`generative.llm.backend` chooses between `fake` and `bedrock` (`LlmBackend`), and `fake` is the
default for every use case that turns a generative flow on (DEC-203). That default is what lets the
test suite open every generative screen, build an index, generate copy, and watch the guardrails and
the budget actually work - the parts of this package most likely to be wrong - without spending
anything or configuring anything. **It is a test tool, not a product mode**: since DEC-1140 it answers
only when `MARKETING_AI_ALLOW_FAKE_AI` is on, and a product screen never offers it.

Switching a use case file to a real model is a configuration change, never a code change:
`generative.llm.{generation,judge,embedding}_model_id` are the only three places a model id appears
anywhere in this package, they default to blank, and `LlmConfig` refuses a document that sets
`backend: bedrock` while any of the three is still blank - at configuration-load time, naming the
first one missing, rather than three stages into a run that was always going to fail (DEC-204).

The fake backend is `FakeLLMClient`, the one fake the whole engine shares, but not in its default
`DIGEST` mode: `build_client` hands a generative flow `FakeLLMClient(mode=FakeLLMMode.GROUNDED)`, and
DEC-214 explains why that second behaviour was worth writing rather than reusing the first. (It was
written as a second fake class, `GroundedFakeLLMClient`; Plan A ruling D7 folded it into
`FakeLLMClient` as a set of modes, with no change to what either behaviour returns.) In `DIGEST` mode
`FakeLLMClient` derives every answer from a SHA-256 of the request, which is exactly right for the
seam it was built for - visibly machine-made, assertable byte for byte - and exactly wrong for testing
retrieval, because a hash embedding has no semantics: the chunk that answers a question would sit no
closer to it than any other, and a RAG test against it would be asserting that one random number beat
another for no real reason. `GROUNDED` mode instead hashes a text's words into buckets with a log term
frequency, so two texts that genuinely share vocabulary really do sit close together, and its
completions are built out of the prompt it was actually given - the numbered extracts, the evidence pack, the allowed
placeholder list - so a grounded answer under the fake is grounded for the same reason a grounded
answer under Bedrock is, and each of the remaining `FakeLLMMode` values can then break exactly one
guardrail at a time for a targeted test.

**With a fake backend, no path that renders generated text may be reachable** (plan section 13.3, as
DEC-214 restates it for this package specifically). DEC-1140 makes that true by construction for a real
run: without `MARKETING_AI_ALLOW_FAKE_AI` the fake is never built. The rule is still worded strictly - it
means a screen must make which backend produced what it shows unmistakable, because
plan section 13.3's underlying rule - never invent a metric, a sample row, or a placeholder number in
a path that reaches the UI - applies exactly as hard to invented prose as to an invented accuracy
score. A screen that could show `[fake completion 3f2a...]`-shaped text to a person evaluating a real
deployment would be showing them a fabricated value with nothing that says so.


### Whose AWS credentials: the connection screen

(This screen chooses the AWS *identity* Bedrock is called as. Whether Bedrock is used at all - and for
which of Product AI and Deliverable AI - is chosen on the AI service screens of Connections.)

Different people use this product with different AWS credentials, and the screen at
`#/generative/connection` - reached from the backend badge on every generative screen - is where a
person says which. It never asks for a key. It chooses a **source**, and the server resolves the
source from wherever AWS tooling already keeps credentials (`engine/aws_connection.py`):

- **Default credentials** - boto3's own chain: `AWS_PROFILE`, environment variables, an SSO login,
  an instance or task role. This is what every job used before the screen existed, and it is still
  what a job uses when nobody has chosen anything.
- **A named profile** - any profile in `~/.aws/config` or `~/.aws/credentials`, including SSO
  profiles. A person without one creates it where their secrets belong, in a terminal on the machine
  running Marketing AI: `aws configure --profile NAME`, or `aws configure sso` for single sign-on.

**Test connection** answers "who am I, and can I use these models?" without spending anything. It asks
STS who the credentials belong to - a call that needs no permission at all - and asks Bedrock whether
each configured model is enabled for that account in that region, which invokes nothing. A task role
scoped to `bedrock:InvokeModel` is usually refused that second question while being perfectly able to
use the model, so a refusal is reported as *unverified*, never as *unavailable*.

Choosing is possible only on the machine that owns the identities: a `local` deployment, asked by the
page the product itself serves, from loopback. Anywhere else the screen is read-only and the identity
is masked:

| Who is asking | Can choose | Sees |
|---|---|---|
| The person at the laptop, in the product's own page | yes | full identity, profile list |
| A colleague opening the laptop's URL over the network | no | masked account, no profiles |
| Another website open in the same browser | no | masked account, no profiles |
| Anyone, on a `dev`, `staging` or `prod` deployment | no | masked account - the IAM task role |

A deployment is single tenant and unauthenticated (docs/AWS_DEPLOYMENT.md, section 9.2), so there is
no person a choice could belong to there. It calls Bedrock as its task role, which is the arrangement
a laptop's named profile is imitating, and an operator changes the role rather than the page.

If someone pastes a key into a request anyway, it is refused with `CREDENTIALS_NOT_ACCEPTED`, it is
never repeated back, and the message says to rotate it: a key typed into a web page should be treated
as exposed. DEC-228 to DEC-232 record why each of these rules is the one it is.

---

## 10. What Phase 3a deliberately does not do

Every omission below was a choice, not an oversight, and each is grounded in something this package
or the plan already commits to elsewhere.

**No uplift modelling.** `win-back-campaign` trains a plain propensity model, and its generative half
writes copy, not a treatment-effect estimate. `CopyBatch.holdout` (`CopyHoldout`) already records the
control rows, the suppressed rows and the out-of-band rows a copy run left out - Phase 1's own control
group, respected and counted rather than reused - because "why is this customer not in the batch?" has
two different answers, and an uplift comparison needs the honest one. That comparison itself is left
to a later phase, named in the contract's own docstring as the reader of this data
(`Phase 3b reads this`); building it now would mean guessing at a design this phase has no reason to
fix yet.

**No sending.** `CopyBatch`'s own docstring is explicit: "Nothing here is ever sent: `approved`
records that a person said a template is fit to use, and sending is somebody else's system." No SES,
SMS gateway or WhatsApp Business API integration exists anywhere in this package, and none should -
`require_human_review` gates a template on a person's decision, not on a channel's delivery, and
building a send path here would mean this codebase making a compliance-carrying decision (who gets
contacted, and when) that belongs to whichever system in a client's stack already owns consent and
delivery.

**No OpenSearch.** `VectorStore` is a four-method protocol precisely so that `LocalVectorStore` -
Parquet for the chunks, Parquet for the vectors, cosine similarity in NumPy - can be the whole
implementation for now, with zero infrastructure: an index is a directory beside the runs, and nothing
has to be running for a test to search one. Phase 4 swaps in OpenSearch Serverless behind the same
protocol; nothing above `engine/generative/vectorstore.py` will need to change to make that swap,
which is the reason the protocol exists at all rather than a concrete class.

**No fine-tuning.** The only lever this package has over what a model says is the prompt (DEC-202)
and the guardrails that check what comes back; there is no training-data pipeline for an LLM anywhere
in `engine/generative`, and `LLMClient` offers exactly three operations - complete, embed, count
tokens - none of which is a fine-tuning job. That is consistent with "cost is an artefact": a
fine-tuned, custom-hosted endpoint does not meter the way a per-call foundation-model request does,
and pricing it honestly would mean a second cost model this phase has no evidence it needs yet.

**No cross-session chat memory.** `assistant.answer`'s `history` parameter is exactly what the client
sends on each request and nothing more: `HISTORY_TURNS` (six turns, three exchanges) bounds how much
of *that* conversation the prompt carries, and the module's own docstring is direct about the rest -
"nothing is stored server-side: the client sends the conversation it has, and the assistant answers
this question with that context and forgets it again." Storing a conversation server-side would be
customer data this package does not otherwise need to hold, retained for a benefit - "and the other
one?" answered without the client resending it - that a longer request already buys for the
conversations that actually need it.

---

## 11. Managing an assistant: passages, feedback, versions (Plan I)

Sections 1 to 10 describe how an answer is made trustworthy at the moment it is written. This section
is about the weeks after: a reader who wants to check a citation, a person who says an answer was
wrong, a policy document that changes, and a reviewer who wants to know whether the new version is
better than the old one. Every route below is in `api/routes/generative.py`, declares its role with
`register(...)` next to itself, and is in `docs/API.md`; the decisions are DEC-1270 to DEC-1279.

| Route | Role | What it does |
|---|---|---|
| `GET /indexes/{id}/chunks/{chunk_id}` | Viewer | One passage exactly as indexed, with its document, section, page and the ids of the passages before and after it in the same document. |
| `POST /indexes/{id}/feedback` | Viewer | A thumbs up or down on one answer, with an optional comment. |
| `GET /indexes/{id}/feedback` | Viewer | Every rating on this index's answers, oldest first, with the two counts. |
| `GET /indexes/{id}/feedback/test-questions.csv` | Analyst, audited | The thumbs-down questions as rows of a reference-set file. |
| `POST /indexes/{id}/update` | Analyst | A new index version with documents added, replaced or removed. |
| `DELETE /indexes/{id}` | Analyst | Deletes one version; never the use case's best one, never one a job is using. |
| `GET /indexes/{id}/compare/{other_id}` | Viewer | Two graded versions of one use case side by side, and the questions whose verdict changed. |

**A citation opens its passage.** A quote of 25 words is enough to recognise a passage and not
enough to judge it, so a citation card in "Try it" is a button: it opens the whole passage in a side
panel, with the quoted words marked and "Previous passage" / "Next passage" walking the document
around it (DEC-1271). The panel marks only words that are really there: `highlightQuote`
(`ui/modules/generative/gdom.js`) looks for the quote with case and spacing ignored - the same
comparison the engine used to keep the quote (DEC-226) - and marks nothing rather than a guess when
it is not found. `Citation.page` is new and optional; the ask route copies it from the cited chunk
after `assistant.answer` returns, so a card can say "page 3" for a PDF and says nothing for a format
without pages.

**Feedback is stored redacted, one file per entry.** `engine/generative/feedback.py` writes each
rating to `indexes/{id}/feedback/{feedback_id}.json` and never rewrites it, as the pilot's feedback
button does (DEC-911): two people pressing the button at once cannot lose each other's entry. The
question, the rated answer and the comment each pass through `engine.pii.redact_text` before they are
stored, so an e-mail address or phone number is kept as a marker naming its kind; who pressed the
button is left to the audit trail, which already records it against the entry id (DEC-1272).

**"Add to test questions" never writes an answer.** The export is a reference-set file in the use
case's own column names (`question`, `expect_refusal`, `source_doc`, `reference_answer` by default),
each thumbs-down question once, and every column but the question left empty. The rated answer was
judged wrong by a person, so copying it - or asking a model for a better one - would put an unchecked
answer into the file whose job is to check answers. A person fills the three cells in and uploads
the file under Evaluate assistant (DEC-1273).

**Updating documents makes a new version, and pays only for what changed.** "Update documents" sends
added and replacement files and the names of documents to remove. The route copies every kept
document's bytes into the new version's own directory (from the previous version's uploads, or from
the bundled sample corpus for a sample build), keeps the previous version's passage settings - a
reused passage is only valid under the settings that cut it - and builds with
`build_index(previous=...)`. Reuse is the engine's own: keyed on `(fingerprint, doc_id)` (DEC-220,
DEC-221), so an unchanged document keeps its passages and vectors at no embedding cost, and a
replaced one is read and embedded again. The new version's `index_update.json` records what was
added, replaced, removed and reused, how many passages were carried over and how many were embedded;
its `llm_usage.json` is the cost of the update alone. The previous version is never touched: it can
still be asked, graded, compared and deleted on its own (DEC-1274). When no test questions are named,
the new version is graded with the ones the previous version was last graded with, so the two can
be compared at once.

**Deleting a version.** `DELETE /indexes/{id}` removes every file under `indexes/{id}/` - status,
manifest, passages, vectors, uploaded copies, grading, usage and feedback - and answers `204`. It
refuses the use case's best index with `409 INDEX_IS_CHAMPION` (the best is decided by the rule
`GET /use-cases/{id}/indexes` already uses: highest mean faithfulness, else the newest), and an index
that is still building or grading, or whose update is still building, with `409 INDEX_BUSY`
(DEC-1279). A version built from it keeps working, because an update holds its own copies.

**Web pages.** `.html` and `.htm` are `DocumentType`s with one reader, built on the standard
library's `html.parser`: `h1`-`h6` open sections, a table row is read as its cells joined like every
other format's tables, and `<head>`, scripts, styles, `<nav>`, `<svg>`, `<iframe>` and form controls
are dropped with everything inside them. Two documents of the test corpus are now built as saved web
pages, chrome and all, and are compared sentence for sentence with their Markdown sources like every
other format (DEC-1275).

**The grade card and the compare view.** `GET /indexes/{id}` carries `grade`: the pass rate and pass
mark, retrieval hit rate, mean faithfulness and mean correctness (null - an em dash on screen - when
nothing was measured), and three counts read off the graded questions: questions where refusing or
answering matched the reference set, questions that should be refused, and how many of those were
(DEC-1276). The compare view puts two graded versions of one use case side by side with the same
figures and lists the questions asked of both that passed on one and failed on the other, matched by
their wording; it says so when the two were graded on different files, and refuses a version that was
never graded with `409 INDEX_NOT_GRADED` and versions of two use cases with `409
INDEXES_NOT_COMPARABLE` (DEC-1277).

Plain-language refusals on these routes, each with its code: `CHUNK_NOT_FOUND` (404),
`INDEX_UPDATE_EMPTY`, `DOCUMENT_NOT_IN_INDEX`, `INDEX_UPDATE_REMOVES_EVERYTHING` and
`DOCUMENT_NAME_CLASH` (422), and `INDEX_SOURCE_MISSING`, `INDEX_EMBEDDING_CHANGED`,
`INDEX_IS_CHAMPION`, `INDEX_BUSY`, `INDEX_NOT_GRADED` and `INDEXES_NOT_COMPARABLE` (409). Every update
refusal is raised before anything is written.

What is not done: feedback is not shown to the model or used to change an answer, a reference answer
is never suggested, the compare view does not re-grade either version, and an update cannot change
the passage size or the embedding model - that is a new build from the start.

## 12. Copy per segment: one message per main reason, or for the persuadables only (Plan I)

A score band says how likely a customer is to come back; it does not say why, and it does not say
whether a message will change anything. `generative.campaign_copy.segment_by` lets copy be written for
a group that does (DEC-1240):

```yaml
generative:
  campaign_copy:
    segment_by: band          # band (default) | top_reason | uplift_segment
    max_segments: 6           # top_reason only: at most this many groups, the last one "other reasons"
```

The setting is also a per-request override - `POST /runs/{run_id}/campaign-copy` with
`{"overrides": {"segment_by": "top_reason"}}` - and that is how the campaign copy screen sends it: a
"Write one message per: Score band / Main reason / Uplift segment" choice above *Generate campaign
copy*, the last offered only when the run's `problem_type` is `uplift`.

**`band` is unchanged, byte for byte.** It uses the same `copy_email`/`copy_sms`/`copy_whatsapp` prompts
with the same variables, so the recorded prompts, `prompt_hashes`, `copy_batch.json` and
`copy_messages.csv` are what they were. The fields added for segments (`CopyTemplate.segment`,
`CopyMessage.segment`, `CopyBatch.segment_by`, `CopyBatch.segments`, `CopyBatch.uplift_budget_applied`,
`CopyHoldout.not_persuadable_rows`, `CopyHoldout.outside_budget_rows`)
are left out of the file when they are empty (`exclude_if`), and the `segment` column of
`copy_messages.csv` is written only when a message has one (DEC-1242).

**`top_reason`** cuts the rows that would get copy per band anyway - `bands_to_write`, minus suppressed
and control rows - by each row's own strongest SHAP reason (`row_explanations.parquet`, `reasons[0]`).
The grouping is `engine.generative.segments.group_keys`, the one `root_cause.py` now uses too, so the
two screens never cut a run differently: largest group first, ties by name. Copy cannot leave an
eligible row without a message, so where a root-cause summary drops the groups past its cap,
`cap_with_other` folds them - and any row with no measured reason - into one `other_reasons` segment,
listed last (DEC-1241). Segment ids are `reason_<feature>` and `other_reasons`; template ids are
`<segment>-<channel>-<variant>`. A run without per-row reasons is refused with
`RUN_WITHOUT_EXPLANATIONS` before any call.

**`uplift_segment`** reads an uplift scoring run's `segment` and `intended_treatment` columns
(`engine/uplift/segments.py`, `engine/uplift/actions.py`) and writes for the persuadables the policy
chose. Every row is counted once, by fixed precedence: suppressed, control, not a persuadable, over the
contact budget, then written to. Sure things, lost causes and sleeping dogs get no message; the batch
counts them in `holdout.not_persuadable_rows` and lists each as a `CopySegment` with `written: false`
and a `skipped_reason` that is the uplift action (`Never treat (contact makes it worse)`), the same
words `scores.csv` already records per row in its `action` column. Persuadables with
`intended_treatment` false are counted in `holdout.outside_budget_rows` and listed as the skipped
segment `persuadable_over_budget` ("Over the contact budget"). A scores file without
`intended_treatment` writes to every persuadable and records `uplift_budget_applied: false` in the
batch. `bands_to_write`
does not apply: an uplift run's segments are its targeting. On a run that is not an uplift scoring run
(`run.json` `problem_type` is not `uplift`, or `scores.csv` has no `segment` column) the request is
refused with `409 COPY_NEEDS_UPLIFT_RUN` before a job is queued, and `generate_campaign_copy` refuses
the same way before any call (DEC-1243).

**What a segment's prompt sees.** Per segment and channel there is one call to a `copy_segment_*`
prompt (DEC-1244), so cost is O(segments x channels) as before. Its inputs are a stored `CopySegment`
and nothing else: the segment's label, one sentence saying how it was formed, the engine's own
aggregate figures (row count, share, rows per band, the mean score or mean predicted uplift) and its
strongest aggregated reasons (`root_cause.aggregate_reasons`, the reason it was formed on first). No
primary key, no field value and no single row's reason is ever passed in; `_plan` reads the rows and
hands the prompt builder counts and means. `tests/unit/generative/test_win_back.py` proves it the
honest way for both new modes - every recorded prompt is searched for every customer id, snapshot date
and spend value in the source file (DEC-1245).

**Rendering and review are unchanged.** Each row is rendered, without a model, from the template of
its own segment; `{{band}}` is the row's own band, since a segment can span bands. Control and
suppressed rows never get a message. Every template is `pending_review` until a person approves it,
whatever it was written for. Regenerating a segment's template re-asks exactly what the first call
asked, from the `CopySegment` the batch stored and with the batch's own `segment_by`, not the use
case's default - the regenerate route takes no override (DEC-1246).

**On the screen** each card names the group it was written for and its size ("Main reason: tenure ·
124 customers", "High band · 31 customers"), groups are drawn largest first, and on an uplift batch
the segments written for nobody are listed with their size and the reason (DEC-1247).

Out of scope, deliberately: no persona reviewer, no multi-agent framework, and no per-row reason
column in `copy_messages.csv` - a row with no message is not a message.

## 13. The assistant as a conversation: follow-ups, retries, reranking, confidence (Plan I)

Section 3 describes how one question is answered. This section is about a person asking several in a
row, in "Try it" or through `POST /indexes/{id}/ask`. The decisions are DEC-1280 to DEC-1289; the code
is `engine/generative/assistant.py` (`answer`, `condense`, `confidence_for`, `suggested_questions`)
and `engine/generative/rerank.py`.

**The conversation is sent, not stored.** The ask body may carry `history`, the earlier exchanges
oldest first, each `{"question": ..., "answer": ...}`: at most 20, a question of up to 2,000
characters, an answer of up to 8,000, nothing else (`422` otherwise, before any call). The server
reads the last three exchanges for this one question and forgets them. "Try it" sends the last three
answered exchanges with the same index version; "New conversation" clears the chat.

**A follow-up is rewritten before it is searched.** "And how much does it cost?" has nothing in it to
search for. When - and only when - there is history, one call with `assistant_condense.v1.md`
(purpose `assistant_condense`, metered and budgeted like every call) rewrites it as a question that
stands alone; that is what is embedded and searched, by vector, BM25 and reranker alike, and the
answer reports it as `searched_for` ("Searched for: …" under the answer). The answer prompt itself
still gets the question as typed and the conversation. If the rewrite fails - the AI service is
unreachable, or the reply is not `{"question": ...}` - the question is searched as typed and
`condense_error` says why; the answer goes ahead. A first question, and every question a grading run
asks, has no history and makes no rewrite call, so its cost is what it was.

**An unfaithful answer is written again before it is refused.** When the faithfulness judge blocks an
answer, the answer prompt (`assistant_answer.v2.md`) is rendered again with a stricter instruction
and the attempt number, up to `retries` times from `configs/guardrails.yaml` (2 shipped). The first
answer that passes is returned; if none does, the operator's refusal sentence is. A block by a
deterministic rule (personal data, a banned phrase, the length) is not retried: a stricter grounding
instruction does not change it. The answer records `attempts` and `retried_after` (the faithfulness
check that refused each earlier attempt), and the chat says "Written again once after a check
against the documents failed".

**Reranking is optional.** `generative.rag.rerank: local` re-orders the passages that passed the
similarity floor with the cross-encoder `BAAI/bge-reranker-base` before MMR chooses from them. It
needs the `local-embeddings` extra (`pip install 'marketing-ai[local-embeddings]'`), downloads the
model on first use, and loads it once per process. When it is chosen and cannot run, asking fails
with `RERANKER_NOT_INSTALLED` or `RERANKER_UNAVAILABLE` (`503`) - it never quietly ranks some other
way. It never adds a passage the floor dropped, a citation still shows the cosine, and it costs no
money, so it is logged (time, passage count) rather than metered. Default: `none`.

**Confidence is measured, not asked for.** Every answer that was not a refusal carries
`confidence`: a level and the signals it was worked out from.

| Level | Rule |
|---|---|
| Low | any of: the answer needed a faithfulness retry; no citation carries a quote found in its passage; the closest passage cleared the similarity floor by less than 0.05 |
| High | none of the above, **and** the faithfulness check ran and scored at least 0.90, **and** the closest passage cleared the floor by at least 0.15 |
| Medium | everything else, including any answer the faithfulness check did not run on |

The similarity is read against the floor, not as a bare number, because the floor is calibrated per
embedding model (DEC-218). A refusal has no confidence. The chat shows the level as a pill; its
tooltip lists the reasons.

**Starter questions are the index's own.** `GET /indexes/{id}` carries `suggested_questions`: up to
four questions from the reference set this version was graded on that passed, were meant to be
answered and were answered, best-supported first. An empty chat shows them; a click asks one. An
index that was never graded shows none, and nothing is ever generated to fill the space.

**Errors on the ask route.** An AI-service failure is a coded, plain error: `LLM_REFUSED` `409`,
`LLM_TOO_LONG` / `LLM_INVALID_REQUEST` `422`, anything else (unreachable, rejected key, throttled,
the local embedder or reranker missing) `503`; the budget is `409 BUDGET_EXCEEDED` as before, and a
generative error code the route has no status for is `503`. No provider text and no traceback reach
the response.

What is not done: no streaming, no server-side memory of conversations, no agent framework, and no
retry of an answer blocked by anything other than the faithfulness check.

## 14. One-way SMS sender IDs: the opt-out link (M91)

"Reply STOP to opt out" cannot work when the SMS sender ID cannot receive replies. That is the case for
India's DLT headers and for any other one-way sender ID: the customer has nothing to reply to, so the
default opt-out line promises an opt-out that does not exist. `generative.campaign_copy.sms_sender` says
which kind of sender the campaign uses:

```yaml
generative:
  campaign_copy:
    sms_sender: two_way       # two_way (default) | one_way
```

**`two_way` is today's behaviour, unchanged.** The SMS required line is `required_lines.sms`
("Reply STOP to opt out") and every prompt, template and message is what it was. The only new thing a
default run records is one more guardrail check, `sms_reply_stop_one_way`, which passes ("nothing
found") because the rule has nothing to say about a two-way sender.

**`one_way` swaps the SMS line for a merge field.** The required line of an SMS template becomes
`{{opt_out_link}}` (`win_back.required_line_for`), and the SMS prompt is given that line like any other,
so the model writes a template ending in it. The field is `OPT_OUT_LINK_FIELD` in
`engine/generative/win_back.py`, beside `UNSUBSCRIBE_FIELD`:

- It is in `_RESERVED_FIELDS`, so it is never read off the uploaded data.
- `allowed_placeholder_fields` returns it for SMS, and for SMS only, when `sms_sender` is `one_way`; a
  template on any other channel, or on SMS under the default, that uses it is blocked by
  `allowed_fields_only` like any placeholder with no data behind it.
- It is filled the way `{{unsubscribe_link}}` is: a rendered message carries the field's own name
  (`opt_out_link`) for the sending system to replace per recipient, because this engine builds no URL
  and `allowed_url_domains` ships empty. The rendered-message check looks for that name rather than the
  braces.
- Under `one_way` the SMS line *is* `{{opt_out_link}}`, whatever `required_lines.sms` says ("Reply STOP
  ...", the shipped default, or any wording of the operator's, such as "To opt out SMS STOP to 1909").
  The one line kept is an operator's own that already contains the placeholder. `{{ opt_out_link }}`
  with spaces counts: the required-line check reads the template with its placeholders normalised.
- The SMS prompts list `opt_out_link` on their "Placeholders you may use" line for `one_way` only
  (`win_back._prompt_allowed_fields`), so the allowed list does not contradict the required line. The
  default prompt text, and its cache keys, are byte-identical.

**The `sms_reply_stop_one_way` guardrail rule** (`configs/guardrails.yaml`, `block`) refuses an SMS
template or rendering that asks the customer to reply STOP ("Reply STOP", "Text 'STOP'", "Send STOP to
56767", "SMS STOP to 1909", "Reply with the word STOP", "typing STOP"). It runs only when the context says the sender is one-way (`CheckContext.one_way_sender`, set by
`win_back` for SMS under `sms_sender: one_way`), and sits before `required_lines` so a "Reply STOP"
template is reported as that rather than as a missing link. It also covers a customer's own field value
that carries the wording, since a rendering is checked again. Like every rule it names the problem and
never quotes the text. Set it to `off` in `guardrails.yaml` and no check is recorded for it.

**WhatsApp is not affected.** A WhatsApp business number has a reply channel of its own, so
`required_lines.whatsapp` and the WhatsApp prompt stay as they are; `sms_sender` is about SMS sender IDs
only. The v1 prompt files are unchanged: they already carry `required_line` as a variable, which is
how a one-way run reaches the model.

Tests: `tests/unit/generative/test_sms_one_way.py`.
