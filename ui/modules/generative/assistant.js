// The AI Onboarding Assistant screen (prototypes 09-11): build a knowledge index, grade it against
// reference questions, then ask it things.
//
// An index is deliberately not a run - `engine/generative/__init__.py` says so and `DEC-211` gives
// it its own status document - so this screen does not reuse `usecase.js`'s controller even though
// the shapes rhyme (Setup, a running list, a finished summary). What it must not rhyme on is trust:
// every number here either came out of `DocIndexManifest`/`RagEval`/`LlmUsageReport` or it is an em
// dash, and every answer in "Try it" shows the citations it was actually drawn from, because an
// answer whose sources are hidden is indistinguishable from a guess (rule 1 of the package). The
// backend badge at the top is not decoration: with `generative.llm.backend: fake`, nothing below it
// reached a real model, and plan §13.3 asks that this be obvious rather than discovered.

import { ApiError } from "../../api.js";
import { EM_DASH, dash, errorBox, esc, fmtInt, fmtNum, fmtSize, fmtStamp, pageHead, stageChip, typeChip } from "../../dom.js";
import { backendBadge, citationCard, guardrailList, usageLine } from "./gdom.js";
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

// --- Setup: documents, reference questions, model -----------------------------------------------

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
    <div class="flabel">Documents</div><div class="fhint">Everything the assistant is allowed to answer from. PDF, DOCX, Markdown or plain text.</div>
    <div class="orline"><label class="control file ${
      s.documents.length ? "has" : ""
    }"><input type="file" id="g-docs" class="sr" multiple accept=".pdf,.docx,.md,.txt"><span class="fname">${esc(
      chosen,
    )}</span><span class="ico" aria-hidden="true">⤒</span></label><span>or <button type="button" class="linkbtn" id="g-sample-docs">use the sample documents</button></span></div>
    ${rows ? `<div class="runs-list" style="margin-top:10px;border:1px solid var(--line);border-radius:8px">${rows}</div>` : ""}
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
    <div class="flabel">Reference questions <span style="font-weight:400;color:var(--muted)">· optional</span></div>
    <div class="fhint">Questions paired with the answer you would accept. Without them the assistant is built but never scored.</div>
    <div class="orline"><label class="control file ${
      s.referenceFile ? "has" : ""
    }"><input type="file" id="g-refset" class="sr" accept=".csv"><span class="fname">${esc(
      chosen,
    )}</span><span class="ico" aria-hidden="true">⤒</span></label><span>or <button type="button" class="linkbtn" id="g-sample-questions">use the sample questions</button></span></div>
    ${s.referenceUploading ? `<div class="loading">Reading the file…</div>` : ""}
    ${s.referenceError ? errorBox(s.referenceError) : ""}
    ${
      columns.length
        ? `<div class="frow" style="margin-top:12px"><div class="field"><span class="sub">Primary key</span><div class="control sel"><select id="g-pk">${columns
            .map((c) => `<option value="${esc(c)}"${c === s.primaryKey ? " selected" : ""}>${esc(c)}</option>`)
            .join("")}</select></div></div><div class="field"><span class="sub">Reference answer column</span><div class="control sel"><select id="g-refcol">${columns
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
    <div class="flabel">Model</div><div class="fhint">AutoML tries every selected LLM and keeps the best. Pick one only if you need to.</div>
    <div class="frow"><div class="field"><div class="control sel"><select id="g-model" aria-label="Model">${choices
      .map(
        (c) =>
          `<option value="${esc(c.value)}"${c.value === s.modelChoice ? " selected" : ""}${
            c.enabled === false ? " disabled" : ""
          }>${esc(c.label)}</option>`,
      )
      .join("")}</select></div></div></div></div></div>`;
}

const OVERRIDE_FIELDS = [
  ["chunk_tokens", "Chunk size (tokens)", "generative.rag.chunk_tokens"],
  ["chunk_overlap", "Chunk overlap (0-0.5)", "generative.rag.chunk_overlap"],
  ["top_k", "Chunks retrieved (top_k)", "generative.rag.top_k"],
  ["min_similarity", "Similarity floor (0-1)", "generative.rag.min_similarity"],
  ["temperature", "Temperature (0-1)", "generative.llm.temperature"],
];

function advancedHtml(s) {
  return `<details class="adv-wrap" id="g-adv"${s.advOpen ? " open" : ""}><summary>Advanced settings<span class="n">Each pipeline stage can be configured · defaults work for most data</span></summary>
    <div class="frow" style="padding-top:14px">${OVERRIDE_FIELDS.map(
      ([key, label]) =>
        `<div class="field sm"><span class="sub">${esc(label)}</span><div class="control"><input type="text" inputmode="decimal" data-override="${esc(
          key,
        )}" value="${esc(s.overrides[key])}" placeholder="default"></div></div>`,
    ).join("")}</div></details>`;
}

function indexesCard(s) {
  const rows = s.indexes.map((ix) => {
    const modelLabel = ix.llm && ix.llm.backend === "fake" ? "Fake backend" : dash(ix.llm && ix.llm.generation_model_id);
    return `<a class="runrow" href="#/generative/assistant/${esc(s.__ucId)}/${esc(ix.index_id)}">
      <div><div class="r1">${esc(modelLabel)}${ix.champion ? '<span class="champ">Champion</span>' : ""}</div>
      <div class="r2">${esc(ix.source_label)} · ${esc(fmtStamp(ix.created_at))}</div></div>
      <div class="r3"><b>${dash(ix.faithfulness, (v) => `Faithfulness ${fmtNum(v, 2)}`)}</b><span style="font-size:12px;color:var(--muted)">${esc(
        ix.kind,
      )}</span></div></a>`;
  });
  return `<section class="card"><h3>Previous runs</h3><div class="runs-list">${
    rows.length ? rows.join("") : `<div class="empty">No index has been built yet.</div>`
  }</div></section>`;
}

function buildTabHtml(uc, s) {
  const blocker = !s.documents.length && !s.useSampleDocuments ? "Add at least one document" : "";
  return `<form id="g-build" novalidate style="margin-top:18px">
    ${documentsStep(s)}${referenceStep(s)}${modelStep(uc, s)}${advancedHtml(s)}
    ${s.submitError ? errorBox(s.submitError) : ""}
    <div class="actions"><button type="submit" class="run" id="g-submit"${
      blocker || s.submitting ? " disabled" : ""
    }>${esc(s.submitting ? "Starting…" : "Build assistant")}</button><span class="reason">${esc(blocker)}</span></div>
  </form>`;
}

function evaluateTabHtml(s) {
  const champion = s.indexes.find((ix) => ix.champion) || s.indexes[0];
  if (!champion) {
    return `<p class="desc" style="margin-top:18px">Build an assistant first - there is no index yet to grade.</p>`;
  }
  const columns = (s.referenceSet && s.referenceSet.columns) || [];
  const blocker = !s.referenceSet && !s.useSampleQuestions ? "Upload a reference-question file" : "";
  return `<form id="g-evaluate" novalidate style="margin-top:18px">
    <p class="desc">Grading <b>${esc(champion.llm && champion.llm.generation_model_id ? champion.llm.generation_model_id : champion.index_id)}</b> against a reference set. This does not rebuild the index.</p>
    ${referenceStep(s)}
    ${columns.length ? "" : ""}
    ${s.submitError ? errorBox(s.submitError) : ""}
    <div class="actions"><button type="submit" class="run" id="g-eval-submit"${
      blocker || s.submitting ? " disabled" : ""
    }>${esc(s.submitting ? "Starting…" : "Evaluate assistant")}</button><span class="reason">${esc(blocker)}</span></div>
  </form>`;
}

function setupHtml(uc, s) {
  s.__ucId = uc.id;
  return `<div class="setup-grid">
    <section class="card"><div class="form-body">
      ${backendBadge(uc.config && uc.config.generative && uc.config.generative.llm)}
      <div class="seg" role="group" aria-label="Assistant tab">
        <button type="button" data-tab="build" class="${s.tab === "build" ? "on" : ""}">Build assistant</button>
        <button type="button" data-tab="evaluate" class="${s.tab === "evaluate" ? "on" : ""}">Evaluate assistant</button>
      </div>
      <p class="seg-help">${
        s.tab === "build"
          ? "Upload documents and question-answer pairs. The assistant is built and checked against the reference answers."
          : "Grade an already-built assistant against a new or updated reference set."
      }</p>
      ${s.tab === "build" ? buildTabHtml(uc, s) : evaluateTabHtml(s)}
    </div></section>
    ${indexesCard(s)}
  </div>
  <p class="next">After the build, this page shows the pipeline: Documents → Model → Answers.</p>`;
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
  return `<div class="setup-grid"><section class="card"><h3>Running…</h3><ol class="progress">${rows}</ol></section>${indexesCard(
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
      "Data",
      manifest ? `${manifest.documents.length} document${manifest.documents.length === 1 ? "" : "s"}` : EM_DASH,
      manifest
        ? `${dash(manifest.total_chunks, fmtInt)} chunks · ${dash(
            manifest.documents.reduce((n, d) => n + (d.pages || 0), 0),
            fmtInt,
          )} pages`
        : "",
    ],
    [
      "#g-eval",
      "Model",
      s.detail.llm && s.detail.llm.backend === "fake" ? "Fake backend" : dash(s.detail.llm && s.detail.llm.generation_model_id),
      evalAgg ? `Pass rate ${fmtNum(evalAgg.pass_rate * 100, 0)}%` : "Not graded yet",
    ],
    [
      "#g-chat",
      "Answers",
      uc.pages && uc.pages.output ? uc.pages.output : "Try it",
      "Ask a question below",
    ],
  ];
  return items
    .map(
      ([href, label, value, meta], i) =>
        `${i ? '<div class="arrow" aria-hidden="true">→</div>' : ""}<a class="block" href="${href}"><div><div class="lab"><span>${String(
          i + 1,
        ).padStart(2, "0")}&nbsp;&nbsp;${label}</span><span class="bstate">✓ Done</span></div><div class="val">${esc(
          value,
        )}</div><div class="meta">${esc(meta)}</div></div><div class="go"><span>View details</span><span aria-hidden="true">›</span></div></a>`,
    )
    .join("");
}

function evaluationCard(s) {
  const rag = s.detail.rag_eval;
  if (!rag) {
    return `<section class="card" id="g-eval"><h3>Evaluation</h3><div class="empty">No reference set was supplied, so this index was built but never scored.</div></section>`;
  }
  const agg = rag.aggregates;
  const worst = (rag.questions || []).slice(0, 10);
  return `<section class="card" id="g-eval"><h3>Evaluation</h3>
    <div style="padding:18px 20px 4px"><div style="font-size:28px;font-weight:700">${fmtNum(
      agg.pass_rate * 100,
      0,
    )}% passed</div>
    <svg class="track" height="8" role="img" style="margin-top:10px" aria-label="pass rate"><rect x="0" y="0" width="100%" height="8" rx="4" fill="var(--track)"/><rect x="0" y="0" width="${Math.max(
      0,
      Math.min(100, agg.pass_rate * 100),
    ).toFixed(2)}%" height="8" rx="4" fill="var(--ok)"/></svg>
    <div class="gseg-row"><span>${fmtInt(agg.questions)} reference questions</span><span>Threshold ${fmtNum(
      agg.pass_threshold * 100,
      0,
    )}%</span></div></div>
    ${
      worst.length
        ? `<div class="tbl-wrap" style="margin-top:12px"><table><thead><tr><th>question</th><th>score</th><th>what went wrong</th></tr></thead><tbody>${worst
            .map(
              (q) =>
                `<tr><td>${esc(q.question)}</td><td>${dash(
                  q.faithfulness ?? q.correctness,
                  (v) => fmtNum(v, 2),
                )}</td><td>${esc(q.failure || (q.passed ? "" : EM_DASH))}</td></tr>`,
            )
            .join("")}</tbody></table></div><p class="caption">The ten weakest answers. Everything else scored above the rest.</p>`
        : ""
    }</section>`;
}

function indexCard(s) {
  const manifest = s.detail.manifest;
  return `<section class="card" id="g-index"><h3>Index</h3>${
    manifest
      ? `<div class="kv"><span class="k">Documents</span><span class="v">${fmtInt(
          manifest.documents.length,
        )}</span></div><div class="kv"><span class="k">Pages</span><span class="v">${fmtInt(
          manifest.documents.reduce((n, d) => n + (d.pages || 0), 0),
        )}</span></div><div class="kv"><span class="k">Text chunks</span><span class="v">${fmtInt(
          manifest.total_chunks,
        )}</span></div><div class="kv"><span class="k">Embedding model</span><span class="v">${esc(
          manifest.chunk_config.embedding_model_id,
        )}</span></div><div class="kv"><span class="k">Chunk size</span><span class="v">${fmtInt(
          manifest.chunk_config.chunk_tokens,
        )} tokens · ${fmtNum(manifest.chunk_config.chunk_overlap * 100, 0)}% overlap</span></div>${
          manifest.warnings.length
            ? `<div class="kv"><span class="k">Warnings</span><span class="v" style="color:var(--warn)">${esc(
                manifest.warnings.join(", "),
              )}</span></div>`
            : ""
        }`
      : `<div class="empty">This build has not produced ${esc("doc_index_manifest.json")} yet.</div>`
  }<h4>Cost</h4><div style="padding:13px 20px">${usageLine(s.detail.llm_usage)}</div></section>`;
}

function messageHtml(entry) {
  const q = `<div class="gmsg user">${esc(entry.question)}</div>`;
  const a = entry.answer;
  if (!a) return `${q}<div class="gmsg bot"><span class="loading">Thinking…</span></div>`;
  const called = a.called_model
    ? ""
    : `<div class="gmeta"><span>Similarity floor refused the question - one embedding call, no generation.</span></div>`;
  const cites = (a.citations || []).map(citationCard).join("");
  const guardrails = guardrailList(a.guardrails);
  return `${q}<div class="gmsg bot${a.refused ? " refused" : ""}">${esc(a.answer)}${cites}${called}${guardrails}
    <div class="gmeta"><span>${fmtInt(a.retrieved)} chunk${a.retrieved === 1 ? "" : "s"} retrieved</span><span>${
      a.latency_ms
    } ms</span><span>prompt v${a.prompt_version}</span>${
      a.refused ? '<span class="gwarn">Refused</span>' : ""
    }</div></div>`;
}

function chatCard(s) {
  return `<section class="card" id="g-chat"><h3>Try it</h3>
    <div class="gchat">${
      s.conversation.length
        ? s.conversation.map(messageHtml).join("")
        : `<div class="empty">Ask a question below. Citations and refusals show exactly as the assistant produced them.</div>`
    }</div>
    ${s.askError ? errorBox(s.askError) : ""}
    <form id="g-ask" class="gaskrow"><input type="text" id="g-question" placeholder="Ask a question…" value="${esc(
      s.question,
    )}" autocomplete="off"><button type="submit"${s.asking ? " disabled" : ""}>${
      s.asking ? "Asking…" : "Ask"
    }</button></form></section>`;
}

function resultsHtml(uc, s) {
  if (!s.detail) return `<div class="loading">Loading the index…</div>`;
  const status = s.detail.status;
  const failed = status && status.state === "failed";
  const headline = failed
    ? `<span class="bad">✕ Build failed</span>`
    : `<span class="ok">✓ Assistant built</span>`;
  return `<div class="results">
    ${backendBadge(s.detail.llm)}
    <div class="summary">${headline}${
      failed && status.error_message ? `<span>${esc(status.error_message)}</span>` : ""
    }<span class="muted">${esc(s.detail.index_id)} · ${esc(fmtStamp(status && status.updated_at))}</span></div>
    <span class="cap">AI pipeline</span><div class="flow">${flowBlocks(uc, s)}</div>
    <div class="row" style="margin-top:24px">${evaluationCard(s)}${indexCard(s)}</div>
    ${chatCard(s)}
    <div class="runs-below">${indexesCard(s)}</div>
  </div>`;
}

// --- shell -------------------------------------------------------------------------------------

export function assistantHtml(uc, s) {
  const body = s.view === "running" ? runningHtml(uc, s) : s.view === "results" ? resultsHtml(uc, s) : setupHtml(uc, s);
  return `<main class="screen t-${esc(uc.marker)}">
    ${pageHead(
      `<a class="back" href="#/">‹&nbsp; Customer Lifecycle</a><h1 class="h1">${esc(
        uc.name,
      )}</h1><p class="desc">${esc(uc.description)}</p><div class="chips">${stageChip(
        uc.lifecycle_stage,
      )}${typeChip(uc)}</div>`,
    )}
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
