// The AI Onboarding Assistant screen (prototypes 09-11): build a knowledge index, grade it against
// reference questions, then ask it things.
//
// An index is deliberately not a run - `engine/generative/__init__.py` says so and `DEC-211` gives
// it its own status document - so this screen does not reuse `usecase.js`'s controller even though
// the shapes rhyme (Setup, a running list, a finished summary). What it must not rhyme on is trust:
// every number here either came out of `DocIndexManifest`/`RagEval`/`LlmUsageReport` or it is an em
// dash, and every answer in "Try it" shows the citations it was actually drawn from, because an
// answer whose sources are hidden is indistinguishable from a guess (rule 1 of the package). The
// backend line at the top is not decoration: when no AI service wrote a build, nothing below it
// reached a real model, and plan §13.3 asks that this be obvious rather than discovered.
//
// v1 UI: plain words ("AI model", "passages", "Built assistants"); the index id, the embedding
// model, passage sizes and cost under Technical details; one primary action per view.

import { ApiError } from "../../api.js";
import {
  EM_DASH,
  backLink,
  dash,
  emptyState,
  errorBox,
  esc,
  fmtInt,
  fmtNum,
  fmtPct,
  fmtSize,
  fmtStamp,
  pageHead,
  present,
  techDetails,
} from "../../dom.js";
import { reasonFor } from "../production/session.js";
import {
  backendBadge,
  citationCard,
  confidencePill,
  gTypeChip,
  guardrailList,
  highlightQuote,
  usageLine,
} from "./gdom.js";
import {
  deleteIndex,
  getChunk,
  getComparison,
  getFeedback,
  getIndex,
  getIndexes,
  getTestQuestionsCsv,
  postAsk,
  postEvaluate,
  postFeedback,
  postIndexBuild,
  postIndexUpdate,
  postReferenceSet,
} from "./api.js";

const AUTOML = "__automl__";
/** What the knowledge base reads (`generative.knowledge_base.accepted_types`), as a file picker filter. */
const ACCEPT = ".pdf,.docx,.md,.txt,.html,.htm";
const POLL_MS = 2000;
const STATE = new Map();
/** Earlier exchanges sent with a question: the three the server's prompt reads (`HISTORY_TURNS` = 6 turns). */
const HISTORY_SENT = 3;

function freshState(uc) {
  return {
    tab: "build",
    documents: [],
    useSampleDocuments: false,
    referenceFile: null,
    useSampleQuestions: false,
    referenceSet: null, // { reference_set_id, columns, row_count }
    referenceUploading: false,
    referenceError: null,
    primaryKey: "",
    referenceColumn: "",
    modelChoice: (uc.setup.model_choices[0] || {}).value || AUTOML,
    overrides: { chunk_tokens: "", chunk_overlap: "", top_k: "", min_similarity: "", temperature: "" },
    advOpen: false,
    submitting: false,
    submitError: null,
    indexes: [],
    view: "setup", // setup | running | results
    indexId: null,
    detail: null, // GET /indexes/{id}
    question: "",
    asking: false,
    askError: null,
    conversation: [],
    passage: null, // { conv, cite, quote, chunkId, data, loading, error } - the side panel (DEC-1271)
    feedback: null, // GET /indexes/{id}/feedback
    exportError: null,
    updateFiles: [],
    updateRemove: [],
    updating: false,
    updateError: null,
    deleting: false,
    deleteError: null,
    compareWith: "",
    comparing: false,
    comparison: null,
    compareError: null,
  };
}

function stateFor(uc) {
  if (!STATE.has(uc.id)) STATE.set(uc.id, freshState(uc));
  return STATE.get(uc.id);
}

/** What a build's AI model is called on screen: "No AI service" for a build no service wrote, else its id. */
const modelName = (llm) =>
  llm && llm.backend === "fake" ? "No AI service" : dash(llm && llm.generation_model_id);

// --- Setup: documents, test questions, AI model ---------------------------------------------------

function documentsStep(s) {
  const chosen = s.documents.length
    ? `${s.documents.length} document${s.documents.length === 1 ? "" : "s"}`
    : s.useSampleDocuments
      ? "Sample documents"
      : "Upload PDF, DOCX, MD, TXT or HTML";
  const rows = s.documents
    .map((f) => `<div class="runrow"><div class="r1">${esc(f.name)}</div><div class="r3">${esc(fmtSize(f.size))}</div></div>`)
    .join("");
  return `<div class="fstep ${s.documents.length || s.useSampleDocuments ? "done" : ""}"><div class="stepno">1</div><div>
    <div class="flabel">Documents</div><div class="fhint">Everything the assistant is allowed to answer from. PDF, Word, Markdown, web pages (HTML) or plain text.</div>
    <div class="orline"><label class="control file ${
      s.documents.length ? "has" : ""
    }"><input type="file" id="g-docs" class="sr" multiple accept="${ACCEPT}"><span class="fname">${esc(
      chosen,
    )}</span><span class="ico" aria-hidden="true">⤒</span></label><span>or <button type="button" class="btn quiet" id="g-sample-docs">use the sample documents</button></span></div>
    ${rows ? `<div class="runs-list gdocs">${rows}</div>` : ""}
    </div></div>`;
}

function referenceStep(s) {
  const done = s.referenceSet || s.useSampleQuestions;
  const chosen = s.referenceFile
    ? s.referenceFile.name
    : s.useSampleQuestions
      ? "Sample questions"
      : "Upload CSV";
  const columns = (s.referenceSet && s.referenceSet.columns) || [];
  return `<div class="fstep ${done ? "done" : ""}"><div class="stepno">2</div><div>
    <div class="flabel">Test questions <span class="gopt">· optional</span></div>
    <div class="fhint">Questions paired with the answer you would accept. Without them the assistant is built but never graded.</div>
    <div class="orline"><label class="control file ${
      s.referenceFile ? "has" : ""
    }"><input type="file" id="g-refset" class="sr" accept=".csv"><span class="fname">${esc(
      chosen,
    )}</span><span class="ico" aria-hidden="true">⤒</span></label><span>or <button type="button" class="btn quiet" id="g-sample-questions">use the sample questions</button></span></div>
    ${s.referenceUploading ? `<div class="loading">Reading the file…</div>` : ""}
    ${s.referenceError ? errorBox(s.referenceError) : ""}
    ${
      columns.length
        ? `<div class="frow gcols"><div class="field"><span class="sub">Question ID column</span><div class="control sel"><select id="g-pk" aria-label="Question ID column">${columns
            .map((c) => `<option value="${esc(c)}"${c === s.primaryKey ? " selected" : ""}>${esc(c)}</option>`)
            .join("")}</select></div></div><div class="field"><span class="sub">Accepted answer column</span><div class="control sel"><select id="g-refcol" aria-label="Accepted answer column">${columns
            .map(
              (c) =>
                `<option value="${esc(c)}"${c === s.referenceColumn ? " selected" : ""}>${esc(c)}</option>`,
            )
            .join("")}</select></div></div></div>`
        : ""
    }
    </div></div>`;
}

function modelStep(uc, s) {
  const choices = uc.setup.model_choices.length ? uc.setup.model_choices : [{ value: AUTOML, label: "AutoML (recommended)", enabled: true }];
  return `<div class="fstep done"><div class="stepno">3</div><div>
    <div class="flabel">AI model</div><div class="fhint">The recommended choice tries every AI model on offer and keeps the best. Pick one only if you need to.</div>
    <div class="frow"><div class="field"><div class="control sel"><select id="g-model" aria-label="AI model">${choices
      .map(
        (c) =>
          `<option value="${esc(c.value)}"${c.value === s.modelChoice ? " selected" : ""}${
            c.enabled === false ? " disabled" : ""
          }>${esc(c.label)}</option>`,
      )
      .join("")}</select></div></div></div></div></div>`;
}

const OVERRIDE_FIELDS = [
  ["chunk_tokens", "Passage size (tokens)", "generative.rag.chunk_tokens"],
  ["chunk_overlap", "Passage overlap (0 to 0.5)", "generative.rag.chunk_overlap"],
  ["top_k", "Passages read per question", "generative.rag.top_k"],
  ["min_similarity", "Closest-match floor (0 to 1)", "generative.rag.min_similarity"],
  ["temperature", "Creativity (0 to 1)", "generative.llm.temperature"],
];

function advancedHtml(s) {
  return `<details class="adv-wrap" id="g-adv"${s.advOpen ? " open" : ""}><summary>Advanced settings<span class="n">The defaults work for most documents</span></summary>
    <div class="frow">${OVERRIDE_FIELDS.map(
      ([key, label]) =>
        `<div class="field sm"><label class="sub" for="g-ov-${esc(key)}">${esc(label)}</label><div class="control"><input type="text" inputmode="decimal" id="g-ov-${esc(
          key,
        )}" data-override="${esc(key)}" value="${esc(s.overrides[key])}" placeholder="default"></div></div>`,
    ).join("")}</div>
    <p class="fhint">Documents are split into passages; for each question the closest passages are found by meaning and the AI model answers only from them. A question with no passage above the floor is refused rather than guessed.</p></details>`;
}

function indexesCard(s) {
  const rows = s.indexes.map((ix) => {
    const grade = dash(ix.faithfulness, (v) => `${fmtPct(v, 0)} grounded`);
    return `<a class="runrow" href="#/generative/assistant/${esc(s.__ucId)}/${esc(ix.index_id)}">
      <div><div class="r1">${esc(modelName(ix.llm))}${ix.champion ? '<span class="champ">Best</span>' : ""}</div>
      <div class="r2">${esc(ix.source_label)} · ${esc(fmtStamp(ix.created_at))}</div></div>
      <div class="r3"><b class="tnum">${esc(grade)}</b><span class="r2">${ix.kind === "evaluate" ? "Graded again" : "Built"}</span></div></a>`;
  });
  return `<section class="card"><h3>Built assistants</h3><div class="runs-list">${
    rows.length ? rows.join("") : `<div class="empty">No assistant built yet.</div>`
  }</div></section>`;
}

function buildTabHtml(uc, s) {
  const blocker = !s.documents.length && !s.useSampleDocuments ? "Add at least one document" : "";
  return `<form id="g-build" class="gform" novalidate>
    ${documentsStep(s)}${referenceStep(s)}${modelStep(uc, s)}${advancedHtml(s)}
    ${s.submitError ? errorBox(s.submitError) : ""}
    <div class="actions"><button type="submit" class="btn primary run" id="g-submit"${
      blocker || s.submitting ? " disabled" : ""
    }>${esc(s.submitting ? "Starting…" : "Build assistant")}</button><span class="reason">${esc(blocker)}</span></div>
  </form>`;
}

function evaluateTabHtml(s) {
  const champion = s.indexes.find((ix) => ix.champion) || s.indexes[0];
  if (!champion) {
    return emptyState({
      title: "No assistant built yet.",
      text: "Build an assistant first; it can then be graded again against new test questions.",
      action: { label: "Build assistant", attrs: 'data-tab="build"', kind: "secondary" },
    });
  }
  const blocker = !s.referenceSet && !s.useSampleQuestions ? "Upload a test-question file" : "";
  return `<form id="g-evaluate" class="gform" novalidate>
    <p class="desc">Grades the best assistant built so far (${esc(modelName(champion.llm))}, ${esc(
      fmtStamp(champion.created_at),
    )}) against test questions. This does not rebuild it.</p>
    ${referenceStep(s)}
    ${s.submitError ? errorBox(s.submitError) : ""}
    <div class="actions"><button type="submit" class="btn primary run" id="g-eval-submit"${
      blocker || s.submitting ? " disabled" : ""
    }>${esc(s.submitting ? "Starting…" : "Evaluate assistant")}</button><span class="reason">${esc(blocker)}</span></div>
  </form>`;
}

function setupHtml(uc, s) {
  s.__ucId = uc.id;
  const built = s.indexes.length > 0;
  // Until something is built, Build is the only real path; Evaluate stays offered, and says why it waits.
  return `<div class="setup-grid">
    <section class="card"><div class="form-body">
      <div class="seg gtabs" role="group" aria-label="Build or evaluate">
        <button type="button" data-tab="build" class="${s.tab === "build" ? "on" : ""}" aria-pressed="${s.tab === "build"}">Build assistant</button>
        <button type="button" data-tab="evaluate" class="${s.tab === "evaluate" ? "on" : ""}" aria-pressed="${s.tab === "evaluate"}">Evaluate assistant${
          built ? "" : ` <span class="gopt">· after a build</span>`
        }</button>
      </div>
      <p class="seg-help">${
        s.tab === "build"
          ? "Upload the documents it may answer from, and optionally test questions to grade it."
          : "Grade an assistant you already built against new or updated test questions."
      }</p>
      ${s.tab === "build" ? buildTabHtml(uc, s) : evaluateTabHtml(s)}
    </div></section>
    ${indexesCard(s)}
  </div>
  <p class="next">After the build, this page shows the documents, the grade and a place to try questions.</p>`;
}

// --- Running ---------------------------------------------------------------------------------

function runningHtml(uc, s) {
  const status = s.detail && s.detail.status;
  const stages = (status && status.stages) || [];
  const cls = { running: "active", done: "done", failed: "failed", cancelled: "cancelled", pending: "" };
  const rows = stages.length
    ? stages
        .map(
          (st, i) =>
            `<li class="${cls[st.state] || ""}"><span class="dot">${i + 1}</span><div><div class="pt">${esc(
              st.title,
            )}</div><div class="pd">${esc(st.detail)}</div></div></li>`,
        )
        .join("")
    : `<li><span class="dot">1</span><div><div class="pt">Starting…</div><div class="pd"></div></div></li>`;
  return `<div class="setup-grid"><section class="card"><h3>Building the assistant…</h3><ol class="progress">${rows}</ol></section>${indexesCard(
    s,
  )}</div>`;
}

// --- Results ---------------------------------------------------------------------------------

function flowBlocks(uc, s) {
  const manifest = s.detail.manifest;
  const evalAgg = s.detail.rag_eval && s.detail.rag_eval.aggregates;
  const items = [
    [
      "#g-index",
      "Documents",
      manifest ? `${manifest.documents.length} document${manifest.documents.length === 1 ? "" : "s"}` : EM_DASH,
      manifest
        ? `${dash(manifest.documents.reduce((n, d) => n + (d.pages || 0), 0), fmtInt)} pages · ${dash(
            manifest.total_chunks,
            fmtInt,
          )} passages`
        : "",
    ],
    ["#g-eval", "AI model", modelName(s.detail.llm), evalAgg ? `${fmtPct(evalAgg.pass_rate, 0)} of test questions passed` : "Not graded yet"],
    ["#g-chat", "Answers", uc.pages && uc.pages.output ? uc.pages.output : "Try it", "Ask a question below"],
  ];
  return items
    .map(
      ([href, label, value, meta], i) =>
        `${i ? '<div class="arrow" aria-hidden="true">→</div>' : ""}<a class="block" href="${href}"><div><div class="lab"><span>${esc(
          label,
        )}</span><span class="bstate">✓ Done</span></div><div class="val">${esc(value)}</div><div class="meta">${esc(
          meta,
        )}</div></div><div class="go"><span>View details</span><span aria-hidden="true">›</span></div></a>`,
    )
    .join("");
}

/** `RagEval` failure codes (`engine/generative/evaluation.py`), in words. */
const FAILURE_LABEL = {
  refusal_mismatch: "Refused when it should have answered, or answered when it should have refused",
  retrieval_miss: "Did not find the passage that holds the answer",
  unfaithful: "The answer strays from the documents",
  provider_error: "Not graded: the AI service failed on this question",
};

/** A rate as a whole percentage, or an em dash when nothing was measured. */
const pctOrDash = (v) => dash(v, (x) => fmtPct(x, 0));

/** The grade card's figures, all read off `IndexDetailResponse.grade` (server-computed, DEC-1276). */
function gradeMetrics(grade) {
  const items = [
    ["Found the right document", pctOrDash(grade.retrieval_hit_rate), "Share of answerable questions whose passages came from the document that holds the answer."],
    ["Stayed with the documents", pctOrDash(grade.mean_faithfulness), "Mean faithfulness: how far each answer is supported by the passages it was given."],
    ["Agreed with the accepted answer", pctOrDash(grade.mean_correctness), "Mean correctness against the accepted answers in the test questions."],
    [
      "Refused or answered rightly",
      `${fmtInt(grade.refusal_correct)} of ${fmtInt(grade.questions)}`,
      `Of the ${fmtInt(grade.should_refuse)} question${grade.should_refuse === 1 ? "" : "s"} it should refuse, it refused ${fmtInt(grade.refused_correctly)}.`,
    ],
  ];
  return `<div class="gmetrics">${items
    .map(
      ([k, v, title]) =>
        `<div class="gmetric" title="${esc(title)}"><div class="k">${esc(k)}</div><div class="v tnum">${esc(v)}</div></div>`,
    )
    .join("")}</div>`;
}

function compareBlock(s) {
  const others = s.indexes.filter((ix) => ix.index_id !== s.indexId && present(ix.pass_rate));
  if (!others.length) return "";
  const options = others
    .map(
      (ix) =>
        `<option value="${esc(ix.index_id)}"${ix.index_id === s.compareWith ? " selected" : ""}>${esc(
          `${fmtStamp(ix.created_at)} · ${ix.source_label} · ${fmtPct(ix.pass_rate, 0)} passed`,
        )}</option>`,
    )
    .join("");
  return `<div class="gcompare" id="g-compare"><div class="frow"><div class="field"><label class="sub" for="g-compare-with">Compare with another version</label><div class="control sel"><select id="g-compare-with"><option value="">Choose a version…</option>${options}</select></div></div>
    <button type="button" class="btn secondary" id="g-compare-go"${s.compareWith && !s.comparing ? "" : " disabled"}>${
      s.comparing ? "Comparing…" : "Compare"
    }</button></div>
    ${s.compareError ? errorBox(s.compareError) : ""}${s.comparison ? comparisonHtml(s.comparison) : ""}</div>`;
}

function comparisonHtml(c) {
  const rows = [
    ["Passed", (g) => `${fmtPct(g.pass_rate, 0)} (${fmtInt(g.passed)} of ${fmtInt(g.questions)})`],
    ["Found the right document", (g) => pctOrDash(g.retrieval_hit_rate)],
    ["Stayed with the documents", (g) => pctOrDash(g.mean_faithfulness)],
    ["Agreed with the accepted answer", (g) => pctOrDash(g.mean_correctness)],
    ["Refused or answered rightly", (g) => `${fmtInt(g.refusal_correct)} of ${fmtInt(g.questions)}`],
    ["Documents", (_g, side) => dash(side.documents, fmtInt)],
  ];
  const head = `<thead><tr><th></th><th>This version</th><th>${esc(fmtStamp(c.right.built_at))}</th></tr></thead>`;
  const body = rows
    .map(
      ([label, fmt]) =>
        `<tr><td>${esc(label)}</td><td class="num">${esc(fmt(c.left.grade, c.left))}</td><td class="num">${esc(
          fmt(c.right.grade, c.right),
        )}</td></tr>`,
    )
    .join("");
  const note = c.same_reference_set
    ? `Both were graded on the same test questions (${fmtInt(c.common_questions)} in common).`
    : `These were graded on different test questions; ${fmtInt(c.common_questions)} question${
        c.common_questions === 1 ? " is" : "s are"
      } in both.`;
  const verdict = (passed) => (passed ? "Passed" : "Failed");
  const changed = c.changed.length
    ? `<div class="tbl-wrap"><table class="tbl tbl-stack" id="g-compare-changed"><thead><tr><th>Question that changed</th><th>This version</th><th>Other version</th></tr></thead><tbody>${c.changed
        .map(
          (q) =>
            `<tr><td data-label="Question">${esc(q.question)}</td><td data-label="This version">${esc(
              verdict(q.left_passed),
            )}</td><td data-label="Other version">${esc(verdict(q.right_passed))}</td></tr>`,
        )
        .join("")}</tbody></table></div>`
    : `<p class="caption">No question changed its verdict.</p>`;
  return `<div class="tbl-wrap"><table class="tbl" id="g-compare-table">${head}<tbody>${body}</tbody></table></div><p class="caption">${esc(
    note,
  )}</p>${changed}`;
}

/** What went wrong with one graded question, in words; a provider error carries its own plain message. */
function failureText(q) {
  if (!q.failure) return q.passed ? "" : EM_DASH;
  const label = FAILURE_LABEL[q.failure] || q.failure;
  return q.failure === "provider_error" && q.error_message ? `${label} (${q.error_message})` : label;
}

function evaluationCard(s) {
  const rag = s.detail.rag_eval;
  if (!rag) {
    return `<section class="card" id="g-eval"><h3>Grade</h3><div class="empty">No test questions were supplied, so this assistant was built but never graded.</div></section>`;
  }
  const agg = rag.aggregates;
  const worst = (rag.questions || []).slice(0, 10);
  const pct = Math.max(0, Math.min(100, agg.pass_rate * 100)).toFixed(2);
  return `<section class="card" id="g-eval"><h3>Grade</h3>
    <div class="gevalhead"><div class="gpass tnum">${esc(fmtPct(agg.pass_rate, 0))} passed</div>
    <svg class="track gpass-track" height="8" role="img" aria-label="${esc(fmtPct(agg.pass_rate, 0))} of test questions passed"><rect x="0" y="0" width="100%" height="8" rx="4" fill="var(--track)"/><rect x="0" y="0" width="${pct}%" height="8" rx="4" fill="var(--ok)"/></svg>
    <div class="gseg-row"><span class="tnum">${fmtInt(agg.questions)} test questions</span>${
      agg.errored ? `<span class="tnum" data-g-errored>${fmtInt(agg.errored)} not graded (the AI service failed)</span>` : ""
    }<span class="tnum">Pass mark ${esc(
      fmtPct(agg.pass_threshold, 0),
    )}</span></div></div>
    ${s.detail.grade ? gradeMetrics(s.detail.grade) : ""}
    ${
      worst.length
        ? `<div class="tbl-wrap gevaltbl"><table class="tbl tbl-stack"><thead><tr><th>Question</th><th class="num">Score</th><th>What went wrong</th></tr></thead><tbody>${worst
            .map(
              (q) =>
                `<tr><td data-label="Question">${esc(q.question)}</td><td class="num" data-label="Score">${dash(
                  q.faithfulness ?? q.correctness,
                  (v) => fmtNum(v, 2),
                )}</td><td data-label="What went wrong">${esc(failureText(q))}</td></tr>`,
            )
            .join("")}</tbody></table></div><p class="caption">The ten weakest answers, weakest first.</p>`
        : ""
    }${compareBlock(s)}</section>`;
}

/** Where this version came from: "Updated from …", what was reused for free and what was read again. */
function updateSummary(s) {
  const u = s.detail.update;
  if (!u) return "";
  const parts = [
    u.added.length ? `added ${u.added.join(", ")}` : "",
    u.replaced.length ? `replaced ${u.replaced.join(", ")}` : "",
    u.removed.length ? `removed ${u.removed.join(", ")}` : "",
  ].filter(Boolean);
  return `<div class="kv"><span class="k">Updated from</span><span class="v"><a href="#/generative/assistant/${esc(
    s.__ucId,
  )}/${esc(u.previous_index_id)}">the previous version</a>${parts.length ? ` · ${esc(parts.join("; "))}` : ""}</span></div>
    <div class="kv" id="g-reuse"><span class="k">Reused</span><span class="v tnum">${fmtInt(u.reused.length)} document${
      u.reused.length === 1 ? "" : "s"
    } (${fmtInt(u.chunks_reused)} passages) without reading them again · ${fmtInt(u.chunks_embedded)} passage${
      u.chunks_embedded === 1 ? "" : "s"
    } read and paid for</span></div>`;
}

function indexCard(s) {
  const manifest = s.detail.manifest;
  const pages = manifest ? manifest.documents.reduce((n, d) => n + (d.pages || 0), 0) : 0;
  const tech = techDetails([
    ["Assistant id", s.detail.index_id],
    ["Embedding model", manifest && manifest.chunk_config.embedding_model_id],
    [
      "Passage size",
      manifest
        ? `${fmtInt(manifest.chunk_config.chunk_tokens)} tokens, ${fmtNum(manifest.chunk_config.chunk_overlap * 100, 0)}% overlap`
        : null,
    ],
  ]);
  return `<section class="card" id="g-index"><h3>Documents</h3>${
    manifest
      ? `<div class="kv"><span class="k">Documents</span><span class="v tnum">${fmtInt(
          manifest.documents.length,
        )}</span></div><div class="kv"><span class="k">Pages</span><span class="v tnum">${fmtInt(
          pages,
        )}</span></div><div class="kv"><span class="k">Passages</span><span class="v tnum">${fmtInt(
          manifest.total_chunks,
        )}</span></div>${updateSummary(s)}${
          manifest.warnings.length
            ? `<div class="kv"><span class="k">Warnings</span><span class="v gwarn">${esc(manifest.warnings.join(", "))}</span></div>`
            : ""
        }`
      : `<div class="empty">The document list is not ready yet.</div>`
  }<div class="card-body"><details class="tech"><summary>Cost</summary><div class="gdetails-body">${usageLine(
    s.detail.llm_usage,
  )}</div></details>${tech}</div></section>`;
}

const UPDATE_PATH = "/indexes/{index_id}/update";
const DELETE_PATH = "/indexes/{index_id}";
const EXPORT_PATH = "/indexes/{index_id}/feedback/test-questions.csv";

/** "Update documents": a new version from this one, and deleting this one. Never edits in place. */
function manageCard(s) {
  const manifest = s.detail.manifest;
  if (!manifest) return "";
  const updateBlocked = reasonFor("POST", UPDATE_PATH);
  const removing = new Set(s.updateRemove);
  const docs = manifest.documents
    .map(
      (d) =>
        `<label><input type="checkbox" data-remove-doc="${esc(d.name)}"${removing.has(d.name) ? " checked" : ""}${
          updateBlocked ? " disabled" : ""
        }> Remove <b>${esc(d.name)}</b> <span class="gopt">· ${fmtInt(d.chunks)} passages</span></label>`,
    )
    .join("");
  const chosen = s.updateFiles.length
    ? `${s.updateFiles.length} file${s.updateFiles.length === 1 ? "" : "s"}: ${s.updateFiles.map((f) => f.name).join(", ")}`
    : "Add or replace documents";
  const nothing = !s.updateFiles.length && !s.updateRemove.length;
  const reason = updateBlocked || (nothing ? "Add a file or pick a document to remove" : "");
  const row = s.indexes.find((ix) => ix.index_id === s.indexId);
  const deleteBlocked =
    reasonFor("DELETE", DELETE_PATH) || (row && row.champion ? "This is the best assistant, so it is kept." : "");
  return `<section class="card" id="g-manage"><h3>Update documents</h3><div class="gmanage">
    <p class="fhint">Makes a new version: unchanged documents keep their passages, so only added or changed files are read and paid for. A file with the same name as a document replaces it. This version stays as it is.</p>
    <form id="g-update" novalidate>
    <label class="control file ${s.updateFiles.length ? "has" : ""}"><input type="file" id="g-update-docs" class="sr" multiple accept="${ACCEPT}"${
      updateBlocked ? " disabled" : ""
    }><span class="fname">${esc(chosen)}</span><span class="ico" aria-hidden="true">⤒</span></label>
    <div class="gdocrows">${docs}</div>
    ${s.updateError ? errorBox(s.updateError) : ""}
    <div class="actions"><button type="submit" class="btn primary" id="g-update-go"${
      reason || s.updating ? " disabled" : ""
    }>${s.updating ? "Starting…" : "Build new version"}</button><span class="reason">${esc(reason)}</span></div>
    </form>
    <div class="glink-row"><button type="button" class="btn quiet" id="g-delete"${deleteBlocked || s.deleting ? " disabled" : ""}>${
      s.deleting ? "Deleting…" : "Delete this version"
    }</button><span class="reason">${esc(deleteBlocked)}</span></div>
    ${s.deleteError ? errorBox(s.deleteError) : ""}
  </div></section>`;
}

/** What people said about the answers, and the one action that turns it into test questions. */
function feedbackCard(s) {
  const fb = s.feedback;
  if (!fb) return "";
  const downs = fb.entries.filter((e) => e.rating === "down");
  const blocked = reasonFor("GET", EXPORT_PATH);
  const list = downs.length
    ? `<ul class="gfeedback-list">${downs
        .map((e) => `<li>${esc(e.question)}${e.comment ? ` <span class="gopt">· ${esc(e.comment)}</span>` : ""}</li>`)
        .join("")}</ul>`
    : `<div class="empty">No answer has been marked as not helpful.</div>`;
  return `<section class="card" id="g-feedback"><h3>Feedback</h3><div class="gmanage">
    <div class="gcounts"><span class="pill ok">${fmtInt(fb.up)} helpful</span><span class="pill ${
      fb.down ? "warn" : "ok"
    }">${fmtInt(fb.down)} not helpful</span></div>
    ${list}
    <p class="fhint">"Add to test questions" downloads the questions marked not helpful as a test-question file. Write the accepted answer for each one yourself, then upload it under Evaluate assistant.</p>
    <div class="glink-row"><button type="button" class="btn secondary" id="g-export"${
      !downs.length || blocked ? " disabled" : ""
    }>Add to test questions</button><span class="reason">${esc(blocked || "")}</span></div>
    ${s.exportError ? errorBox(s.exportError) : ""}
  </div></section>`;
}

function feedbackRow(entry, i) {
  const fb = entry.fb || {};
  if (fb.saved) {
    return `<div class="gfb" data-fb-done="${i}"><span class="gfb-row">Thanks, your feedback was saved.${
      fb.redacted && fb.redacted.length ? " Contact details in it were masked." : ""
    }</span></div>`;
  }
  const pressed = (r) => (fb.rating === r ? "true" : "false");
  return `<div class="gfb"><div class="gfb-row"><span>Was this helpful?</span><button type="button" class="btn quiet sm" data-fb="${i}:up" aria-pressed="${pressed(
    "up",
  )}">Helpful</button><button type="button" class="btn quiet sm" data-fb="${i}:down" aria-pressed="${pressed(
    "down",
  )}">Not helpful</button></div>${
    fb.rating
      ? `<label class="sub" for="g-fb-comment-${i}">Anything to add? <span class="gopt">· optional; contact details are masked</span></label><textarea id="g-fb-comment-${i}" data-fb-comment="${i}" maxlength="1000">${esc(
          fb.comment || "",
        )}</textarea><div class="gfb-row"><button type="button" class="btn secondary sm" data-fb-send="${i}"${
          fb.sending ? " disabled" : ""
        }>${fb.sending ? "Sending…" : "Send feedback"}</button></div>${fb.error ? errorBox(fb.error) : ""}`
      : ""
  }</div>`;
}

function messageHtml(entry, i) {
  const q = `<div class="gmsg user">${esc(entry.question)}</div>`;
  const a = entry.answer;
  if (!a) return `${q}<div class="gmsg bot"><span class="loading">Thinking…</span></div>`;
  const called = a.called_model
    ? ""
    : `<div class="gmeta"><span>No passage in the documents was close enough to this question, so no answer was written.</span></div>`;
  const cites = (a.citations || []).map((c, j) => citationCard(c, { open: `${i}:${j}` })).join("");
  const guardrails = guardrailList(a.guardrails);
  const conf = a.confidence
    ? `<span class="gconf" title="${esc((a.confidence.reasons || []).join(" "))}">${confidencePill(a.confidence.level)}</span>`
    : "";
  const retries = Math.max(0, (a.attempts || 0) - 1);
  const searched = a.searched_for ? `<span class="gsearched">Searched for: ${esc(a.searched_for)}</span>` : "";
  const condenseFailed = a.condense_error
    ? `<span class="gwarn">Searched with your words as typed: the follow-up could not be rewritten.</span>`
    : "";
  const rewritten = retries
    ? `<span class="gretried">Written again ${retries === 1 ? "once" : `${retries} times`} after a check against the documents failed</span>`
    : "";
  return `${q}<div class="gmsg bot${a.refused ? " refused" : ""}">${esc(a.answer)}${cites}${called}${guardrails}
    <div class="gmeta" title="${esc(`${a.latency_ms} ms · prompt v${a.prompt_version}`)}">${conf}<span>${fmtInt(a.retrieved)} passage${
      a.retrieved === 1 ? "" : "s"
    } used</span>${a.refused ? '<span class="gwarn">Refused</span>' : ""}${searched}${condenseFailed}${rewritten}</div>${feedbackRow(
      entry,
      i,
    )}</div>`;
}

/** Starter questions for an empty chat: the index's own graded questions that passed (DEC-1286). */
function suggestionsHtml(s) {
  const suggested = (s.detail && s.detail.suggested_questions) || [];
  if (!suggested.length) return "";
  return `<div class="gsuggest" aria-label="Questions to try">${suggested
    .map((text, i) => `<button type="button" class="btn secondary sm" data-suggest="${i}">${esc(text)}</button>`)
    .join("")}</div>`;
}

function chatCard(s) {
  const reset = s.conversation.length
    ? `<button type="button" class="btn quiet sm" id="g-new-chat"${s.asking ? " disabled" : ""}>New conversation</button>`
    : "";
  return `<section class="card" id="g-chat"><div class="gchat-h"><h3>Try it</h3>${reset}</div>
    <div class="gchat">${
      s.conversation.length
        ? s.conversation.map(messageHtml).join("")
        : `<div class="empty">Ask a question below. Each answer shows the passages it came from, or says why it was refused. Follow-up questions are understood in the context of the conversation.${suggestionsHtml(
            s,
          )}</div>`
    }</div>
    ${s.askError ? `<div class="gbody">${errorBox(s.askError)}</div>` : ""}
    <form id="g-ask" class="gaskrow"><div class="control"><input type="text" id="g-question" aria-label="Your question" placeholder="Ask a question…" value="${esc(
      s.question,
    )}" autocomplete="off"></div><button type="submit" class="btn primary"${s.asking ? " disabled" : ""}>${
      s.asking ? "Asking…" : "Ask"
    }</button></form></section>`;
}

/** The passage a citation came from, its quote marked, with the passages around it a click away. */
function passagePanel(s) {
  const p = s.passage;
  if (!p) return "";
  const d = p.data;
  const title = d ? `${d.document} · ${d.section}` : "Passage";
  const sub = d ? [present(d.page) ? `Page ${d.page}` : "", `Passage ${fmtInt(d.ordinal + 1)}`].filter(Boolean).join(" · ") : "";
  const body = p.loading
    ? `<div class="loading">Loading the passage…</div>`
    : p.error
      ? errorBox(p.error)
      : `<div class="gpassage-text">${highlightQuote(d.text, d.chunk_id === p.chunkId ? p.quote : "")}</div>`;
  return `<aside class="gpassage" id="g-passage" role="dialog" aria-modal="false" aria-labelledby="g-passage-title">
    <div class="gpassage-h"><div><h3 id="g-passage-title">${esc(title)}</h3><div class="sub">${esc(sub)}</div></div>
    <button type="button" class="btn quiet sm" id="g-passage-close" aria-label="Close the passage">Close</button></div>
    <div class="gpassage-body">${body}</div>
    <div class="gpassage-nav"><button type="button" class="btn quiet sm" data-passage-go="previous"${
      d && d.previous_chunk_id && !p.loading ? "" : " disabled"
    }>‹ Previous passage</button><button type="button" class="btn quiet sm" data-passage-go="next"${
      d && d.next_chunk_id && !p.loading ? "" : " disabled"
    }>Next passage ›</button></div></aside>`;
}

function resultsHtml(uc, s) {
  if (!s.detail) return `<div class="loading">Loading the assistant…</div>`;
  const status = s.detail.status;
  const failed = status && status.state === "failed";
  const headline = failed ? `<span class="bad">✕ The build did not finish</span>` : `<span class="ok">✓ Assistant built</span>`;
  return `<div class="results">
    ${backendBadge(s.detail.llm)}
    <div class="summary">${headline}${
      failed && status.error_message ? `<span>${esc(status.error_message)}</span>` : ""
    }<span class="muted">${esc(fmtStamp(status && status.updated_at))}</span></div>
    <span class="cap">How it was built</span><div class="flow">${flowBlocks(uc, s)}</div>
    <div class="row grow">${evaluationCard(s)}${indexCard(s)}</div>
    ${chatCard(s)}
    <div class="row grow">${feedbackCard(s)}${manageCard(s)}</div>
    <div class="runs-below">${indexesCard(s)}</div>
    ${passagePanel(s)}
  </div>`;
}

// --- shell -------------------------------------------------------------------------------------

export function assistantHtml(uc, s) {
  const body = s.view === "running" ? runningHtml(uc, s) : s.view === "results" ? resultsHtml(uc, s) : setupHtml(uc, s);
  const llm = uc.config && uc.config.generative && uc.config.generative.llm;
  return `<main class="screen gscreen t-${esc(uc.marker)}">
    ${pageHead(
      `${backLink(uc)}<h1 class="h1">${esc(uc.name)}</h1><p class="desc">${esc(uc.description)}</p><div class="chips">${gTypeChip(
        uc,
      )}</div>`,
    )}
    ${s.view === "results" ? "" : backendBadge(llm)}
    ${body}
  </main>`;
}

// --- controller ----------------------------------------------------------------------------------

export function createAssistantController(uc, rerender) {
  const s = stateFor(uc);
  let timer = null;
  const stop = () => {
    if (timer) clearInterval(timer);
    timer = null;
  };

  async function refreshIndexes() {
    try {
      const { indexes } = await getIndexes(uc.id);
      s.indexes = indexes || [];
    } catch (error) {
      if (!(error instanceof ApiError)) throw error;
    }
  }

  async function loadIndex(indexId) {
    if (s.indexId !== indexId) {
      Object.assign(s, {
        passage: null,
        feedback: null,
        exportError: null,
        updateFiles: [],
        updateRemove: [],
        updateError: null,
        deleteError: null,
        compareWith: "",
        comparison: null,
        compareError: null,
      });
    }
    s.indexId = indexId;
    s.detail = await getIndex(indexId);
    const state = s.detail.status && s.detail.status.state;
    s.view = state === "pending" || state === "running" ? "running" : "results";
    if (s.view === "results") await loadFeedback();
  }

  /** The feedback card's list; a role that may not read it simply sees no card. */
  async function loadFeedback() {
    try {
      s.feedback = await getFeedback(s.indexId);
    } catch (error) {
      if (!(error instanceof ApiError)) throw error;
      s.feedback = null;
    }
  }

  // --- the passage panel (DEC-1271) ---------------------------------------------------------------
  async function showChunk(indexId, chunkId) {
    s.passage = { ...s.passage, loading: true, error: null };
    rerender();
    try {
      s.passage = { ...s.passage, data: await getChunk(indexId, chunkId), loading: false };
    } catch (error) {
      s.passage = { ...s.passage, error, loading: false };
    }
    rerender();
    const close = document.getElementById("g-passage-close");
    if (close) close.focus();
  }

  function openPassage(conv, cite) {
    const entry = s.conversation[conv];
    const citation = entry && entry.answer && entry.answer.citations[cite];
    if (!citation) return;
    s.passage = { indexId: entry.indexId || s.indexId, chunkId: citation.chunk_id, quote: citation.quote, data: null };
    showChunk(s.passage.indexId, citation.chunk_id);
  }

  function stepPassage(direction) {
    const d = s.passage && s.passage.data;
    const target = d && (direction === "next" ? d.next_chunk_id : d.previous_chunk_id);
    if (target) showChunk(s.passage.indexId, target);
  }

  // --- feedback on answers (DEC-1272) -------------------------------------------------------------
  async function sendFeedback(i) {
    const entry = s.conversation[i];
    if (!entry || !entry.answer || !entry.fb || !entry.fb.rating || entry.fb.sending) return;
    entry.fb = { ...entry.fb, sending: true, error: null };
    rerender();
    try {
      const saved = await postFeedback(entry.indexId || s.indexId, {
        rating: entry.fb.rating,
        question: entry.question,
        answer: entry.answer.answer,
        refused: entry.answer.refused,
        cited_chunk_ids: (entry.answer.citations || []).map((c) => c.chunk_id),
        comment: (entry.fb.comment || "").trim(),
      });
      entry.fb = { ...entry.fb, sending: false, saved: true, redacted: saved.redacted };
      if ((entry.indexId || s.indexId) === s.indexId) await loadFeedback();
    } catch (error) {
      entry.fb = { ...entry.fb, sending: false, error };
    }
    rerender();
  }

  /** "Add to test questions": the server's CSV, saved as a file; nothing in it is written here. */
  async function exportTestQuestions() {
    s.exportError = null;
    try {
      const text = await getTestQuestionsCsv(s.indexId);
      const href = URL.createObjectURL(new Blob([text], { type: "text/csv" }));
      const link = document.createElement("a");
      link.href = href;
      link.download = `test_questions_${s.indexId}.csv`;
      document.body.appendChild(link);
      link.click();
      link.remove();
      URL.revokeObjectURL(href);
    } catch (error) {
      s.exportError = error;
    }
    rerender();
  }

  // --- versions: update, delete, compare (DEC-1274, DEC-1277, DEC-1279) --------------------------
  async function submitUpdate() {
    if (s.updating || (!s.updateFiles.length && !s.updateRemove.length)) return;
    s.updating = true;
    s.updateError = null;
    rerender();
    try {
      const created = await postIndexUpdate(s.indexId, { documents: s.updateFiles, remove: s.updateRemove });
      s.updating = false;
      s.view = "running";
      await loadIndex(created.index_id);
      await refreshIndexes();
      rerender();
      poll();
    } catch (error) {
      s.updating = false;
      s.updateError = error;
      rerender();
    }
  }

  async function removeVersion() {
    if (s.deleting) return;
    const sure = window.confirm(
      "Delete this version of the assistant? Its documents, passages, grade and feedback are removed. Other versions are not affected.",
    );
    if (!sure) return;
    s.deleting = true;
    s.deleteError = null;
    rerender();
    try {
      await deleteIndex(s.indexId);
      s.deleting = false;
      s.indexId = null;
      s.detail = null;
      s.view = "setup";
      await refreshIndexes();
      window.location.hash = `#/generative/assistant/${encodeURIComponent(uc.id)}`;
      rerender();
    } catch (error) {
      s.deleting = false;
      s.deleteError = error;
      rerender();
    }
  }

  async function compare() {
    if (!s.compareWith || s.comparing) return;
    s.comparing = true;
    s.compareError = null;
    s.comparison = null;
    rerender();
    try {
      s.comparison = await getComparison(s.indexId, s.compareWith);
    } catch (error) {
      s.compareError = error;
    }
    s.comparing = false;
    rerender();
  }

  function poll() {
    stop();
    timer = setInterval(async () => {
      if (!s.indexId) return stop();
      try {
        s.detail = await getIndex(s.indexId);
      } catch {
        return stop();
      }
      const state = s.detail.status && s.detail.status.state;
      if (state === "pending" || state === "running") return rerender();
      stop();
      s.view = "results";
      await refreshIndexes();
      rerender();
    }, POLL_MS);
  }

  async function uploadReference(file) {
    s.referenceFile = file;
    s.referenceUploading = true;
    s.referenceError = null;
    s.useSampleQuestions = false;
    rerender();
    try {
      const result = await postReferenceSet(uc.id, file);
      s.referenceSet = result;
      s.primaryKey = (result.columns || [])[0] || "";
      s.referenceColumn = (result.columns || [])[1] || (result.columns || [])[0] || "";
    } catch (error) {
      s.referenceError = error;
      s.referenceSet = null;
    }
    s.referenceUploading = false;
    rerender();
  }

  async function submitBuild() {
    s.submitting = true;
    s.submitError = null;
    rerender();
    try {
      const created = await postIndexBuild(uc.id, {
        documents: s.documents,
        useSampleDocuments: s.useSampleDocuments,
        referenceSetId: s.referenceSet && s.referenceSet.reference_set_id,
        useSampleQuestions: s.useSampleQuestions,
        primaryKey: s.primaryKey,
        referenceColumn: s.referenceColumn,
        modelChoice: s.modelChoice,
        overrides: overridesFromInputs(s),
      });
      s.submitting = false;
      s.view = "running";
      await loadIndex(created.index_id);
      await refreshIndexes();
      rerender();
      poll();
    } catch (error) {
      s.submitting = false;
      s.submitError = error;
      rerender();
    }
  }

  async function submitEvaluate() {
    const champion = s.indexes.find((ix) => ix.champion) || s.indexes[0];
    if (!champion) return;
    s.submitting = true;
    s.submitError = null;
    rerender();
    try {
      await postEvaluate(champion.index_id, {
        referenceSetId: s.referenceSet && s.referenceSet.reference_set_id,
        useSampleQuestions: s.useSampleQuestions,
        primaryKey: s.primaryKey,
        referenceColumn: s.referenceColumn,
      });
      s.submitting = false;
      s.view = "running";
      await loadIndex(champion.index_id);
      rerender();
      poll();
    } catch (error) {
      s.submitting = false;
      s.submitError = error;
      rerender();
    }
  }

  /** The conversation so far with this index, as the server reads it: the last few answered turns. */
  function historyFor(indexId) {
    return s.conversation
      .filter((e) => e.indexId === indexId && e.answer)
      .slice(-HISTORY_SENT)
      .map((e) => ({ question: e.question, answer: e.answer.answer }));
  }

  async function ask(text) {
    const question = (text === undefined ? s.question : text).trim();
    if (!question || s.asking) return;
    s.asking = true;
    s.askError = null;
    s.question = "";
    const history = historyFor(s.indexId);
    const entry = { question, answer: null, indexId: s.indexId, fb: null };
    s.conversation = [...s.conversation, entry];
    rerender();
    try {
      entry.answer = await postAsk(s.indexId, question, history);
    } catch (error) {
      s.conversation = s.conversation.filter((e) => e !== entry);
      s.askError = error;
    }
    s.asking = false;
    rerender();
  }

  function bind(root) {
    const $ = (id) => root.querySelector(`#${id}`);
    const on = (id, event, fn) => {
      const el = $(id);
      if (el) el.addEventListener(event, fn);
    };

    root.querySelectorAll('[data-tab]').forEach((button) =>
      button.addEventListener("click", () => {
        s.tab = button.dataset.tab;
        s.submitError = null;
        rerender();
      }),
    );
    on("g-docs", "change", (event) => {
      s.documents = Array.from(event.target.files || []);
      s.useSampleDocuments = false;
      rerender();
    });
    on("g-sample-docs", "click", () => {
      s.useSampleDocuments = true;
      s.documents = [];
      rerender();
    });
    on("g-refset", "change", (event) => {
      const file = event.target.files[0];
      if (file) uploadReference(file);
    });
    on("g-sample-questions", "click", () => {
      s.useSampleQuestions = true;
      s.referenceFile = null;
      s.referenceSet = null;
      rerender();
    });
    on("g-pk", "change", (event) => {
      s.primaryKey = event.target.value;
    });
    on("g-refcol", "change", (event) => {
      s.referenceColumn = event.target.value;
    });
    on("g-model", "change", (event) => {
      s.modelChoice = event.target.value;
    });
    on("g-adv", "toggle", (event) => {
      s.advOpen = event.target.open;
    });
    root.querySelectorAll("[data-override]").forEach((input) =>
      input.addEventListener("change", () => {
        s.overrides[input.dataset.override] = input.value;
      }),
    );
    on("g-build", "submit", (event) => {
      event.preventDefault();
      if (s.submitting) return;
      submitBuild();
    });
    on("g-evaluate", "submit", (event) => {
      event.preventDefault();
      if (s.submitting) return;
      submitEvaluate();
    });
    on("g-question", "input", (event) => {
      s.question = event.target.value;
    });
    on("g-ask", "submit", (event) => {
      event.preventDefault();
      ask();
    });
    on("g-new-chat", "click", () => {
      if (s.asking) return;
      Object.assign(s, { conversation: [], passage: null, askError: null, question: "" });
      rerender();
    });
    root.querySelectorAll("[data-suggest]").forEach((button) =>
      button.addEventListener("click", () => {
        const suggested = (s.detail && s.detail.suggested_questions) || [];
        const text = suggested[Number(button.dataset.suggest)];
        if (text) ask(text);
      }),
    );

    // Plan I: citations open their passage; answers take feedback; versions are managed.
    root.querySelectorAll("[data-cite]").forEach((card) => {
      const [conv, cite] = card.dataset.cite.split(":").map(Number);
      card.addEventListener("click", () => openPassage(conv, cite));
      card.addEventListener("keydown", (event) => {
        if (event.key !== "Enter" && event.key !== " ") return;
        event.preventDefault();
        openPassage(conv, cite);
      });
    });
    on("g-passage-close", "click", () => {
      s.passage = null;
      rerender();
    });
    on("g-passage", "keydown", (event) => {
      if (event.key !== "Escape") return;
      s.passage = null;
      rerender();
    });
    root.querySelectorAll("[data-passage-go]").forEach((button) =>
      button.addEventListener("click", () => stepPassage(button.dataset.passageGo)),
    );
    root.querySelectorAll("[data-fb]").forEach((button) =>
      button.addEventListener("click", () => {
        const [i, rating] = button.dataset.fb.split(":");
        const entry = s.conversation[Number(i)];
        if (!entry) return;
        entry.fb = { ...(entry.fb || {}), rating, error: null };
        rerender();
      }),
    );
    root.querySelectorAll("[data-fb-comment]").forEach((area) =>
      area.addEventListener("input", () => {
        const entry = s.conversation[Number(area.dataset.fbComment)];
        if (entry && entry.fb) entry.fb.comment = area.value;
      }),
    );
    root.querySelectorAll("[data-fb-send]").forEach((button) =>
      button.addEventListener("click", () => sendFeedback(Number(button.dataset.fbSend))),
    );
    on("g-export", "click", () => exportTestQuestions());
    on("g-update-docs", "change", (event) => {
      s.updateFiles = Array.from(event.target.files || []);
      rerender();
    });
    root.querySelectorAll("[data-remove-doc]").forEach((box) =>
      box.addEventListener("change", () => {
        const name = box.dataset.removeDoc;
        s.updateRemove = box.checked
          ? [...new Set([...s.updateRemove, name])]
          : s.updateRemove.filter((n) => n !== name);
        rerender();
      }),
    );
    on("g-update", "submit", (event) => {
      event.preventDefault();
      submitUpdate();
    });
    on("g-delete", "click", () => removeVersion());
    on("g-compare-with", "change", (event) => {
      s.compareWith = event.target.value;
      s.comparison = null;
      s.compareError = null;
      rerender();
    });
    on("g-compare-go", "click", () => compare());
  }

  return { state: s, bind, refreshIndexes, loadIndex, poll, stop };
}

function overridesFromInputs(s) {
  const overrides = {};
  for (const [key, , path] of OVERRIDE_FIELDS) {
    const raw = s.overrides[key];
    if (raw === "" || raw === undefined || raw === null) continue;
    overrides[path] = Number(raw);
  }
  return overrides;
}
