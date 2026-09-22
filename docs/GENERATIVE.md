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
required lines, allowed fields, banned phrases, PII, URL whitelist, language - are ordinary regular
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

**1. Parse.** `engine/generative/parsers.py` reads an uploaded PDF, DOCX, Markdown or plain-text file
into ordered, headed `Section`s. Four formats share one goal that is harder than it sounds: recovering
*structure* rather than inventing it. DOCX and Markdown carry real heading markers, so those two
readers are told the answer directly. PDF and plain text have been flattened to lines, so the parser
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
`GroundedFakeMode.REFUSING`, and why the same-domain off-topic case - where only meaning, not
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
a guardrail report can never claim a text was checked for something nobody checked it for. The eight
deterministic rules run first and in a deliberate order (`RULES`): an empty output makes every later
rule meaningless, so it runs first; a length failure is worth reporting before a phrase failure inside
a text that was never going to be used anyway. Every one of the eight is `block` in the shipped
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
| `index_status.json` | `GenerativeStatus` | Continuously while an index build runs. | The Running screen for an index build: one stage per document plus the embedding step, polled the same way a predictive run's `status.json` is. |
| `rag_eval.json` | `RagEval` | A reference set is graded against a finished index. | The Results screen for RAG quality: pass rate against `reference_set.pass_threshold`, retrieval hit rate, mean faithfulness and correctness, and the worst-scoring questions stored first so a reviewer reads the head of the list rather than sorting one. |
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

## 9. Backends: the fake and Bedrock

`generative.llm.backend` chooses between `fake` and `bedrock` (`LlmBackend`), and `fake` is the
default for every use case that turns a generative flow on (DEC-203). That default is not a
convenience; it is what lets a developer with no AWS credentials open every generative screen, build
an index, generate copy, and watch the guardrails and the budget actually work - the parts of this
package most likely to be wrong - without spending anything or configuring anything.

Switching to a real model is a configuration change, never a code change:
`generative.llm.{generation,judge,embedding}_model_id` are the only three places a model id appears
anywhere in this package, they default to blank, and `LlmConfig` refuses a document that sets
`backend: bedrock` while any of the three is still blank - at configuration-load time, naming the
first one missing, rather than three stages into a run that was always going to fail (DEC-204).

The fake backend is not `FakeLLMClient`, the digest-based fake the rest of the engine already shares.
`build_client` hands a generative flow `GroundedFakeLLMClient` instead, and DEC-214 explains why a
second fake was worth writing rather than reusing the first: `FakeLLMClient` derives every answer from
a SHA-256 of the request, which is exactly right for the seam it was built for - visibly machine-made,
assertable byte for byte - and exactly wrong for testing retrieval, because a hash embedding has no
semantics: the chunk that answers a question would sit no closer to it than any other, and a RAG test
against it would be asserting that one random number beat another for no real reason.
`GroundedFakeLLMClient` instead hashes a text's words into buckets with a log term frequency, so two
texts that genuinely share vocabulary really do sit close together, and its completions are built out
of the prompt it was actually given - the numbered extracts, the evidence pack, the allowed
placeholder list - so a grounded answer under the fake is grounded for the same reason a grounded
answer under Bedrock is, and `GroundedFakeMode` can then break exactly one guardrail at a time for a
targeted test.

**With a fake backend, no path that renders generated text may be reachable** (plan section 13.3, as
DEC-214 restates it for this package specifically). This is stricter than "the fake must not be used
in production": it means a screen must make which backend produced what it shows unmistakable, because
plan section 13.3's underlying rule - never invent a metric, a sample row, or a placeholder number in
a path that reaches the UI - applies exactly as hard to invented prose as to an invented accuracy
score. A screen that could show `[fake completion 3f2a...]`-shaped text to a person evaluating a real
deployment would be showing them a fabricated value with nothing that says so.


### Whose AWS credentials: the connection screen

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
