# Generative UI endpoint contract

`ui/modules/generative/api.js` calls every endpoint in this file and no others. It is written for the
API these three screens need, not for what happens to exist yet - as of this write-up
`api/routes/generative.py` does not exist and none of these routes are served. That is the point of
writing it down: this is the contract M20's API task is built against, not a description of code
that was read.

Three things shaped every choice below, and are worth stating once rather than per endpoint:

**An index is not a run.** `engine/generative/__init__.py` says so in as many words - nothing under
`engine/generative` is a pipeline stage, and `DEC-211` gives index builds, root-cause jobs and copy
jobs each their own status document rather than writing into a finished run's `status.json`. So the
RAG assistant gets its own resource family, `/use-cases/{id}/indexes` and `/indexes/{id}`, instead of
being squeezed through `POST /runs`; root-cause and campaign-copy jobs, which genuinely do run *over*
a finished run, reuse that run's own artefact route rather than inventing a second one.

**Nothing here is served by a fake backend without saying so.** Every response that carries generated
text also carries (directly, or one hop away through `run_config.json`) the resolved
`generative.llm` block, because plan §13.3 requires a fake backend be obvious on the screen that
renders its output, not just discoverable in a log.

**A filename in `engine/generative/contracts.py` is not renamed for the wire.** Where a response body
*is* an artefact - `AssistantAnswer`, `CopyTemplate`, `RootCauseSummary`, `LlmUsageReport`,
`GuardrailReport`, `GenerativeStatus`, `DocIndexManifest`, `RagEval` - the field names below are the
model's own, not a rephrasing, so a backend implementation can return the pydantic model directly.
Only the thin envelopes around them (`IndexSummary`, `IndexDetailResponse`, and the two job-started
acknowledgements) are new, and they are marked as such.

Conventions carried over unchanged from `docs/API.md`: every error response is
`{"detail": {"code", "message", "path"}}` (`ErrorResponse`), a `404` on a missing artefact is what
`GET /runs/{id}/artefacts/{name}` already returns and the UI already treats as "not produced yet"
rather than a failure, and a job-starting `POST` answers `202` the way `POST /runs` does.

## Reused, unchanged

These already exist and nothing here changes their shape. Listed because the UI modules call them
too, so an implementer does not have to cross-reference `ui/api.js` to see the whole picture.

| Method | Path | Used for |
|---|---|---|
| `GET` | `/use-cases/{use_case_id}` | The assistant, RCA and copy screens all read `config.generative.*` and `setup.model_choices` from here. |
| `GET` | `/runs/{run_id}` | RCA and copy read the scoring run's own record (file name, created_at, state). |
| `GET` | `/runs/{run_id}/artefacts/{name}` | Every generative artefact a root-cause or copy job writes lives in the run's own artefact set (`DEC-210`): `root_cause_status.json`, `root_cause_summary.json`, `copy_status.json`, `copy_batch.json`, `guardrail_report.json`, `llm_usage.json`, alongside the predictive `scoring_summary.json`, `decile_lift.json` and `run_config.json` the RCA and copy screens also read for context. A name this run has not produced answers `404` and the UI already renders that as "not generated yet" (`getArtefact` in `ui/api.js`), so no new artefact-fetch route is needed for RCA or copy. |

## RAG assistant

### `POST /use-cases/{use_case_id}/reference-sets`

Profiles an uploaded reference-question file the way `POST /uploads` profiles a dataset, so the Setup
screen's "Primary key" and "Reference answer column" selects have real column names to offer (mock
09) instead of guessing at `question_id` / `reference_answer`.

Request: `multipart/form-data`, one field `file` (CSV).

Response `200`:

```jsonc
{
  "reference_set_id": "refset_...",   // opaque id, passed back to the two endpoints below
  "columns": ["question_id", "question", "reference_answer", "expect_refusal"],
  "row_count": 300
}
```

### `POST /use-cases/{use_case_id}/indexes`

Starts an index build (`GenerativeJobKind.INDEX_BUILD`): reads the documents, chunks and redacts
them, embeds them, builds the index, and - when a reference set was given - answers and grades it
against that set in the same job, matching the eight running-screen stages of mock 10 (`Reading
documents` … `Grading the answers` … `Saving assistant version`). A reference set is optional
(`ReferenceSetConfig` is inert without one); an index built without one is built but never scored,
exactly as the Setup screen's own copy says.

Request: `multipart/form-data`.

| Field | Type | Required | Meaning |
|---|---|---|---|
| `documents` | file, repeated | one of `documents` / `use_sample_documents` | Knowledge-base files: PDF, DOCX, MD or TXT, checked against `generative.knowledge_base.accepted_types/max_docs/max_mb`. |
| `use_sample_documents` | `"true"` | | Seeds the build from the server's bundled sample corpus (`tests/fixtures/make_docs.py`'s output) instead of an upload - the mock's "use the sample documents" link. |
| `reference_set_id` | string | no | From `POST .../reference-sets`. Omitted when no reference set is used. |
| `use_sample_questions` | `"true"` | | Seeds reference questions from the bundled sample set - "use the sample questions". |
| `primary_key` | string | only with a reference set | Column of the reference file identifying each question. |
| `reference_column` | string | only with a reference set | Column holding the accepted answer. |
| `model_choice` | string | yes | `"__automl__"` (try every candidate generation model in `setup.model_choices` and keep the one with the best faithfulness) or one explicit model id from that list. |
| `overrides` | string (JSON object) | no | Dotted-path overrides into `generative.*`, the same mechanism `RunRequest.overrides` already uses - e.g. `{"generative.rag.top_k": 8, "generative.rag.min_similarity": 0.3}`. The UI's Advanced settings panel is intentionally a short hand-picked list (chunk size, chunk overlap, top_k, similarity floor, temperature) rather than a schema-driven form: `DEC-209` confirms `advanced_settings_schema` returns no stages at all for `ai_type: generative` today, because its eight stages are the predictive pipeline's. A generative-shaped advanced-settings schema is not something this task is scoped to add, so the panel is deliberately small and reads its values back through this same `overrides` dict rather than through `ui/settings.js`'s generic renderer. |

Response `202`:

```jsonc
{ "index_id": "idx_..." }
```

### `GET /use-cases/{use_case_id}/indexes`

The Setup screen's "Previous runs" card (mock 09/11): every index this use case has built, newest
first.

Response `200`:

```jsonc
{
  "indexes": [
    {
      "index_id": "idx_...",
      "kind": "build",                 // "build" | "evaluate" - which job produced this row
      "champion": true,                // this use case's current best index, same meaning as ModelVersionResponse.is_champion
      "state": "done",                 // GenerativeStatus.state
      "llm": { "backend": "bedrock", "generation_model_id": "anthropic.claude-3-5-sonnet-...", "judge_model_id": "...", "embedding_model_id": "amazon.titan-embed-text-v2:0", "region": "ap-south-1" },
      "faithfulness": 0.92,            // RagEval.aggregates.mean_faithfulness; null when never graded
      "source_label": "product_docs (5 files)",  // documents summary, or the reference file's name for an evaluate-only row
      "created_at": "2026-09-22T07:57:00Z"
    }
  ]
}
```

`llm` is new envelope shape, not a contracts.py model - the four `LlmConfig` fields the UI's backend
badge needs, read off the resolved config the build actually ran with (the same values
`run_config.json.config.generative.llm` would carry for a run). `champion` is server-decided the same
way `ModelVersionResponse.is_champion` is: the best-scoring build (by `mean_faithfulness`, falling
back to the newest when nothing has been graded) is the one this use case's assistant answers from.

### `GET /indexes/{index_id}`

One index, in full - the Setup screen's running poll and the Results screen's every card.

Response `200`:

```jsonc
{
  "index_id": "idx_...",
  "llm": { "backend": "...", "generation_model_id": "...", "judge_model_id": "...", "embedding_model_id": "...", "region": "..." },
  "status": { /* GenerativeStatus, i.e. index_status.json verbatim */ },
  "manifest": { /* DocIndexManifest, i.e. doc_index_manifest.json */ } | null,
  "rag_eval": { /* RagEval, i.e. rag_eval.json */ } | null,
  "llm_usage": { /* LlmUsageReport, i.e. llm_usage.json */ } | null,
  "guardrails": { /* GuardrailReport, i.e. guardrail_report.json */ } | null
}
```

`manifest`/`rag_eval`/`llm_usage`/`guardrails` are `null` exactly when the underlying file has not
been written yet (still building, or built with no reference set for `rag_eval`), the same "artefact
this job has not produced" meaning `getArtefact` already carries for a run. The UI polls this route
every two seconds while `status.state` is `pending` or `running`, same cadence as `GET /runs/{id}`.

### `POST /indexes/{index_id}/evaluate`

The Evaluate-assistant tab: re-grades an already-built index against a reference set without
rebuilding it (`GenerativeJobKind.REFERENCE_EVAL`). Overwrites that index's `rag_eval.json` and its
`index_status.json` (the status document belongs to whichever job last touched the index, per
`DEC-211` - there is one status document per index, not per job run over it).

Request: `multipart/form-data`, same `reference_set_id` / `use_sample_questions` / `primary_key` /
`reference_column` fields as the build endpoint.

Response `202`: `{ "index_id": "idx_..." }` (the same id - polled the same way).

### `POST /indexes/{index_id}/ask`

One question, answered live - the "Try it" panel. The response body *is* an `AssistantAnswer`;
`engine/generative/contracts.py`'s own docstring names this route, so the shape is the artefact's,
not this document's to redefine.

Request: `{"question": "My new router has a red light. What do I do?"}`

Response `200`: `AssistantAnswer` -
`{question, answer, refused, citations: [{chunk_id, document, section, quote, similarity}], retrieved, called_model, prompt_version, latency_ms, guardrails: [{target, rule, outcome, detail}]}`.
`called_model: false` is the case the Setup screen's copy promises and the screen must show plainly:
the question scored below `generative.rag.min_similarity` against every chunk, so one embedding call
was made and no generation call was - a refusal that cost a fraction of what an answer costs, and the
UI's job is to say so rather than render it identically to an ordinary refusal.

## Root-cause summaries

### `POST /runs/{run_id}/root-cause`

Starts a root-cause job (`GenerativeJobKind.ROOT_CAUSE`) over a finished scoring run. `run_id` must
name a `done` scoring run whose use case's `generative.kind` is `root_cause_summary`; anything else is
a `409` with the existing `ErrorResponse` shape (the same "this request is well-formed but the state
does not allow it" case `CHAMPION_CHANGED` already models).

Request: `{"overrides": {}}` - dotted-path overrides into `generative.root_cause.*`
(`segment_by`, `max_segments`, `reasons_per_segment`, `complaint_samples_per_segment`, `tone`), same
mechanism as the assistant's `overrides`. The RCA screen's "Generate root causes" button sends `{}`;
a future settings panel can widen this without a shape change.

Response `202`: `{"run_id": "...", "job_id": "..."}`. Progress and the finished summary are read back
through the run's own artefact route (`root_cause_status.json` while running,
`root_cause_summary.json`, `guardrail_report.json` and `llm_usage.json` once done) - there is no
separate polling route for this job, which is why the request above needs no id of its own beyond the
run.

## Campaign copy

### `POST /runs/{run_id}/campaign-copy`

Starts a campaign-copy job (`GenerativeJobKind.CAMPAIGN_COPY`) over a finished scoring run, the same
shape as root-cause above: `run_id` must be a `done` scoring run whose use case's `generative.kind` is
`campaign_copy`, `overrides` reaches into `generative.campaign_copy.*`, the response is
`{"run_id": "...", "job_id": "..."}`, and progress and the finished batch are read back through
`copy_status.json`, `copy_batch.json`, `guardrail_report.json` and `llm_usage.json` on the same run.

### `POST /runs/{run_id}/campaign-copy/templates/{template_id}/approve`

Records that a named person accepted one template - never that anything was sent, the same
unverified-claim pattern `POST /models/{id}/approve` already uses (`DEC-055`): Phase 1 has no
authentication, so `approved_by` is whatever the caller typed, stored so the row is not anonymous and
never read as a verified identity. Refused with `409` when the template's `status` is not
`pending_review` (an already-approved or blocked template is not this route's to touch - blocked
copy is regenerated, not approved over the guardrails' objection).

Request: `{"approved_by": "Asha Rao"}` - `approved_by` non-blank, mirroring
`ModelApproveRequest.approved_by`.

Response `200`: the updated `CopyTemplate` (`status: "approved"`, `approved_by`, `approved_at` now
set).

### `POST /runs/{run_id}/campaign-copy/templates/{template_id}/regenerate`

Re-runs generation for one template in place - one band, one channel, one variant - and replaces it
in `copy_batch.json` once the new attempt has passed (or been blocked by) the same guardrails and
judges as the original batch. Costs one more `by_purpose` entry in `llm_usage.json`, which is why the
UI re-reads that artefact after every regenerate.

Request: no body.

Response `200`: the replacement `CopyTemplate`, with `attempts` incremented and a fresh `template_id`
if the backend chooses to mint one, or the same id with new content if it does not - the UI matches
the returned template back into its grid by whichever `template_id` the response carries, so either
convention works without a UI change.

### `GET /runs/{run_id}/copy_messages.csv`

The rendered messages, one row per scored entity (`CopyMessage`, i.e. `copy_messages.csv` read back
raw) - "Download messages". Same convention as the existing `GET /runs/{run_id}/scores.csv`: a direct
file route beside the JSON artefact route, because a CSV download is not a JSON body. `404` before any
copy has been generated for this run.

## Integration this branch did not make

Two edits are needed outside `ui/modules/generative/` for these screens to be reachable, and neither
touches a file this task was scoped to write, so they are stated here rather than made:

**`ui/index.html`** - one script tag, inside the Phase-3A markers already present:

```html
<!-- ---- PHASE-3A (generative) — append only below this line ---- -->
<script type="module" src="./modules/generative/index.js"></script>
<!-- ---- END PHASE-3A ---- -->
```

**`ui/modules/router.js`** - nothing to add. `ui/modules/generative/index.js` calls
`registerModule({name: "generative", routes: ["generative"], render})` itself at import time (the
same pattern the file's own comment documents for Phase 2), so once the script tag above is present
the module registers on its own and no edit to `router.js`'s shared body is needed.

What *is* still missing is a way to reach these screens by clicking rather than by typing a hash:
`ui/overview.js` links every use case card straight to `#/uc/<id>` regardless of `ai_type`, and
`ui/pages.js`'s Output page has no link out for a hybrid use case's generative half. Wiring those is
two small, precise edits for whichever agent owns those files next:

- A use case whose `ai_type` is `generative` (the assistant) should link to
  `#/generative/assistant/<id>` instead of `#/uc/<id>` - in `overview.js`'s card link and in
  `app.js`'s `showUseCase`/`showPage`, which currently sends every `#/uc/<id>` hash through
  `usecase.js`'s controller unconditionally.
- A finished scoring run whose use case's `generative.kind` is `root_cause_summary` or
  `campaign_copy` should get one more entry in `pages.js`'s Output-page flow (or a plain link beside
  the existing tabs) reading `#/generative/rca/<use_case_id>/<run_id>` or
  `#/generative/copy/<use_case_id>/<run_id>` respectively.

Until those land, every screen this branch built is reachable by URL
(`#/generative/assistant/<use_case_id>`, `#/generative/rca/<use_case_id>/<run_id>`,
`#/generative/copy/<use_case_id>/<run_id>`) and renders correctly; nothing links to them yet.

## Deliberately not built

The prototype's "Deployment & monitoring" card on 12/13/14 (SageMaker Batch, Amazon SES, a weekly
retraining cadence) is labelled "Illustrative sample, AWS reference stack" on the mock itself and
nothing in `engine/generative/contracts.py` backs a single field of it - it is Phase 4a's AWS
reference architecture, not Phase 3a's output. Building it here would mean inventing a response shape
for numbers no run will ever produce, which is exactly what plan §13.3 forbids. The RCA and copy
screens both render the KPI tiles and the churn/win-back decile chart the mocks put above that card
(all of it real: `decile_lift.json`, `scoring_summary.json`, `CopyAudience`, `CopyHoldout`), and stop
there.
