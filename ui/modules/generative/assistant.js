// The AI Onboarding Assistant screen (prototypes 09-11): build a knowledge index, grade it against
// reference questions, then ask it things.
//
// An index is deliberately not a run - `engine/generative/__init__.py` says so and `DEC-211` gives
// it its own status document - so this screen does not reuse `usecase.js`'s controller even though
// the shapes rhyme (Setup, a running list, a finished summary). What it must not rhyme on is trust:
// every number here either came out of `DocIndexManifest`/`RagEval`/`LlmUsageReport` or it is an em
// dash, and every answer in "Try it" shows the citations it was actually drawn from, because an
// answer whose sources are hidden is indistinguishable from a guess (rule 1 of the package). The
// backend line at the top is not decoration: with `generative.llm.backend: fake`, nothing below it
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
  techDetails,
} from "../../dom.js";
import { backendBadge, citationCard, gTypeChip, guardrailList, usageLine } from "./gdom.js";
import { getIndex, getIndexes, postAsk, postEvaluate, postIndexBuild, postReferenceSet } from "./api.js";

const AUTOML = "__automl__";
const POLL_MS = 2000;
const STATE = new Map();

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
  };
}

function stateFor(uc) {
  if (!STATE.has(uc.id)) STATE.set(uc.id, freshState(uc));
  return STATE.get(uc.id);
}

/** What a build's AI model is called on screen: "Practice mode" for the fake backend, else its id. */
const modelName = (llm) =>
  llm && llm.backend === "fake" ? "Practice mode" : dash(llm && llm.generation_model_id);

// --- Setup: documents, test questions, AI model ---------------------------------------------------

function documentsStep(s) {
  const chosen = s.documents.length
    ? `${s.documents.length} document${s.documents.length === 1 ? "" : "s"}`
    : s.useSampleDocuments
      ? "Sample documents"
      : "Upload PDF, DOCX, MD or TXT";
  const rows = s.documents
    .map((f) => `<div class="runrow"><div class="r1">${esc(f.name)}</div><div class="r3">${esc(fmtSize(f.size))}</div></div>`)
    .join("");
  return `<div class="fstep ${s.documents.length || s.useSampleDocuments ? "done" : ""}"><div class="stepno">1</div><div>
    <div class="flabel">Documents</div><div class="fhint">Everything the assistant is allowed to answer from. PDF, Word, Markdown or plain text.</div>
    <div class="orline"><label class="control file ${
      s.documents.length ? "has" : ""
    }"><input type="file" id="g-docs" class="sr" multiple accept=".pdf,.docx,.md,.txt"><span class="fname">${esc(
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
};

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
    <div class="gseg-row"><span class="tnum">${fmtInt(agg.questions)} test questions</span><span class="tnum">Pass mark ${esc(
      fmtPct(agg.pass_threshold, 0),
    )}</span></div></div>
    ${
      worst.length
        ? `<div class="tbl-wrap gevaltbl"><table class="tbl tbl-stack"><thead><tr><th>Question</th><th class="num">Score</th><th>What went wrong</th></tr></thead><tbody>${worst
            .map(
              (q) =>
                `<tr><td data-label="Question">${esc(q.question)}</td><td class="num" data-label="Score">${dash(
                  q.faithfulness ?? q.correctness,
                  (v) => fmtNum(v, 2),
                )}</td><td data-label="What went wrong">${esc(
                  q.failure ? FAILURE_LABEL[q.failure] || q.failure : q.passed ? "" : EM_DASH,
                )}</td></tr>`,
            )
            .join("")}</tbody></table></div><p class="caption">The ten weakest answers, weakest first.</p>`
        : ""
    }</section>`;
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
        )}</span></div>${
          manifest.warnings.length
            ? `<div class="kv"><span class="k">Warnings</span><span class="v gwarn">${esc(manifest.warnings.join(", "))}</span></div>`
            : ""
        }`
      : `<div class="empty">The document list is not ready yet.</div>`
  }<div class="card-body"><details class="tech"><summary>Cost</summary><div class="gdetails-body">${usageLine(
    s.detail.llm_usage,
  )}</div></details>${tech}</div></section>`;
}

function messageHtml(entry) {
  const q = `<div class="gmsg user">${esc(entry.question)}</div>`;
  const a = entry.answer;
  if (!a) return `${q}<div class="gmsg bot"><span class="loading">Thinking…</span></div>`;
  const called = a.called_model
    ? ""
    : `<div class="gmeta"><span>No passage in the documents was close enough to this question, so no answer was written.</span></div>`;
  const cites = (a.citations || []).map(citationCard).join("");
  const guardrails = guardrailList(a.guardrails);
  return `${q}<div class="gmsg bot${a.refused ? " refused" : ""}">${esc(a.answer)}${cites}${called}${guardrails}
    <div class="gmeta" title="${esc(`${a.latency_ms} ms · prompt v${a.prompt_version}`)}"><span>${fmtInt(a.retrieved)} passage${
      a.retrieved === 1 ? "" : "s"
    } used</span>${a.refused ? '<span class="gwarn">Refused</span>' : ""}</div></div>`;
}

function chatCard(s) {
  return `<section class="card" id="g-chat"><h3>Try it</h3>
    <div class="gchat">${
      s.conversation.length
        ? s.conversation.map(messageHtml).join("")
        : `<div class="empty">Ask a question below. Each answer shows the passages it came from, or says why it was refused.</div>`
    }</div>
    ${s.askError ? `<div class="gbody">${errorBox(s.askError)}</div>` : ""}
    <form id="g-ask" class="gaskrow"><div class="control"><input type="text" id="g-question" aria-label="Your question" placeholder="Ask a question…" value="${esc(
      s.question,
    )}" autocomplete="off"></div><button type="submit" class="btn primary"${s.asking ? " disabled" : ""}>${
      s.asking ? "Asking…" : "Ask"
    }</button></form></section>`;
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
    <div class="runs-below">${indexesCard(s)}</div>
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
    s.indexId = indexId;
    s.detail = await getIndex(indexId);
    const state = s.detail.status && s.detail.status.state;
    s.view = state === "pending" || state === "running" ? "running" : "results";
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

  async function ask() {
    const question = s.question.trim();
    if (!question || s.asking) return;
    s.asking = true;
    s.askError = null;
    s.question = "";
    const entry = { question, answer: null };
    s.conversation = [...s.conversation, entry];
    rerender();
    try {
      entry.answer = await postAsk(s.indexId, question);
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
