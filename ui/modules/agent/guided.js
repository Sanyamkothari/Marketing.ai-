// The Guided setup tab (Plan G §5.1, M75): upload a file, read what the helper suggests, answer its
// questions, approve, then Run - the Setup form's own Run.
//
// Everything on this screen is the session's (`engine/agent/contracts.py` `AgentSession`): every
// title, reason, question, option, summary line, stop reason and chat reply is a string the API
// wrote, escaped here and never reworded, and every number is one the API measured. They are shown
// as plain text only - no markdown, no HTML, no images, no links made from URLs - because a column
// name or a cell value in the person's file can carry text written to look like markup. A helper
// reply shows the tool results it cites ("Source: …"), so a person can see where each number came
// from; a reply that cites none shows no such line.
//
// The only thing this module keeps of its own is which boxes are ticked. A box starts ticked when the
// helper is `sure` and unticked when it says `check`, and nothing is sent until the person previews or
// approves: then every suggestion still pending is decided as its box says - ticked accepted,
// unticked rejected (DEC-1010: only the person decides). A question is answered the moment an option
// is clicked, because an answer can change what the helper suggests next.
//
// Approve ends where Manual setup ends: `apply` returns the prepared upload, the two columns and the
// overrides, and `host.approved(...)` fills the Setup form's state with them - its paths come from the
// API, never from this file - so the Run button below is `ui/usecase.js`'s own `submit()`.
//
// The Setup form repaints its whole `<main>` on every change, so, as the raw-tables panel does, one
// pair of elements is kept per (use case, mode) and moved into each fresh placeholder: a session, a
// chat turn in flight and the ticks survive the repaint.

import { ApiError, getProfile, postUpload, templateUrl } from "../../api.js";
import {
  EM_DASH,
  announceStatus,
  dash,
  dataTable,
  errorBox,
  esc,
  fmtInt,
  fmtNum,
  glossaryCode,
  present,
} from "../../dom.js";
import { postAnswer, postApply, postDecisions, postMessage, postPreview, startSession } from "./api.js";
import { injectAgentStyles } from "./styles.js";

const entries = new Map();

/** Which use cases offer Guided setup: the ones that train a model on a table (Plan G G10, DEC-1003). */
export function guidedApplies(uc) {
  return Boolean(uc && uc.trainable_in_phase_1 && uc.advanced_settings && (uc.advanced_settings.stages || []).length);
}

/** The groups of the checklist, by the proposal's own `kind`; a kind not listed here is not drawn. */
const GROUPS = [
  { id: "roles", kinds: ["role"], title: "Columns & outcome" },
  { id: "fixes", kinds: ["recipe_step", "acknowledgement"], title: "Data fixes" },
  {
    id: "settings",
    kinds: ["setting"],
    title: "Settings: Auto (recommended)",
    cap: "Every setting not listed here keeps this use case's recommended value.",
    empty: "No change suggested: every setting keeps this use case's recommended value.",
  },
];

function fresh() {
  return {
    phase: "upload", // upload | uploading | starting | session
    fileName: "",
    upload: null,
    uploadError: null,
    session: null,
    chat: null,
    ticks: {},
    busy: "", // "" | answer | preview | approve | chat | fill
    actionError: null,
    report: null,
    preview: null,
    previewError: null,
    applied: null,
    fillError: null,
    draft: "",
    asking: "",
    chatError: null,
  };
}

// --- reading the session ---------------------------------------------------------------------------

/** Whether a suggestion's box is ticked: the person's own tick, else the session's decision, else the
 * helper's default - ticked when it is sure. */
function ticked(g, proposal) {
  if (Object.prototype.hasOwnProperty.call(g.ticks, proposal.proposal_id)) return g.ticks[proposal.proposal_id];
  if (proposal.state === "accepted") return true;
  if (proposal.state === "rejected") return false;
  return proposal.confidence === "sure";
}

/** The decisions that bring the session in line with the boxes: every pending suggestion, and every
 * decided one whose box was changed since. */
function decisionsToSend(g) {
  return (g.session.proposals || [])
    .filter((p) => p.state === "pending" || (p.state === "accepted") !== ticked(g, p))
    .map((p) => ({ proposal_id: p.proposal_id, state: ticked(g, p) ? "accepted" : "rejected" }));
}

const openQuestions = (session) => (session.questions || []).filter((q) => q.blocking && !present(q.answer));

const modeCopy = (uc, mode) => (uc.setup.modes || []).find((m) => m.value === mode) || uc.setup.modes[0] || {};

/** Take the API's answer as the session, keeping a tick only while the session has not moved the
 * proposal it belongs to (an answer can accept a suggestion by itself). */
function adopt(g, response) {
  const before = new Map(((g.session && g.session.proposals) || []).map((p) => [p.proposal_id, p.state]));
  g.session = response.session;
  if (response.chat) g.chat = response.chat;
  const now = new Set();
  for (const proposal of g.session.proposals || []) {
    now.add(proposal.proposal_id);
    if (before.has(proposal.proposal_id) && before.get(proposal.proposal_id) !== proposal.state) {
      delete g.ticks[proposal.proposal_id];
    }
  }
  for (const id of Object.keys(g.ticks)) if (!now.has(id)) delete g.ticks[id];
}

// --- drawing ---------------------------------------------------------------------------------------

const step = (n, label, done, inner) =>
  `<div class="fstep${done ? " done" : ""}"><div class="stepno">${esc(n)}</div><div><div class="flabel">${esc(
    label,
  )}</div>${inner}</div></div>`;

const laterStep = (n, label, hint) =>
  `<div class="fstep todo"><div class="stepno">${esc(n)}</div><div><div class="flabel">${esc(
    label,
  )}</div><div class="fhint">${esc(hint)}</div></div></div>`;

function uploadStep(entry, n) {
  const g = entry.state;
  const uc = entry.host.uc;
  const busy =
    g.phase === "uploading" ? "Reading the file…" : g.phase === "starting" ? "The helper is checking your data…" : "";
  const locked = Boolean(busy || g.applied);
  const hint = `${modeCopy(uc, entry.mode).dataset_hint || ""} The helper reads it and suggests fixes. Nothing changes until you approve, and your file itself is never changed.`;
  return step(
    n,
    "Your data",
    Boolean(g.upload),
    `<div class="fhint">${esc(hint.trim())}</div>
    <div class="orline"><label class="control file${g.upload ? " has" : ""}"><input type="file" id="ag-file" class="sr" accept=".csv,.parquet"${
      locked ? " disabled" : ""
    }><span class="fname">${esc(g.fileName || "Upload CSV or Parquet")}</span><span class="ico" aria-hidden="true">⤒</span></label><span>or <a class="btn quiet sm" href="${esc(
      templateUrl(uc.setup.template_url),
    )}" download>Download template</a></span></div>
    ${busy ? `<div class="loading" role="status">${esc(busy)}</div>` : ""}
    ${g.uploadError ? errorBox(g.uploadError) : ""}`,
  );
}

function itemHtml(g, proposal, disabled) {
  const check = proposal.confidence === "check" ? `<span class="pill warn">Please check</span>` : "";
  return `<label class="ag-item"><input type="checkbox" data-ag-proposal="${esc(proposal.proposal_id)}"${
    ticked(g, proposal) ? " checked" : ""
  }${disabled ? " disabled" : ""}><span><span class="ag-t">${esc(proposal.title)}</span>${check}<span class="ag-r">${esc(
    proposal.reason,
  )}</span></span></label>`;
}

function questionHtml(question, disabled) {
  const open = question.blocking && !present(question.answer);
  const options = (question.options || [])
    .map(
      (option) =>
        `<div class="ag-opt"><button type="button" class="btn secondary sm" data-ag-question="${esc(
          question.question_id,
        )}" data-ag-option="${esc(option.option_id)}" aria-pressed="${question.answer === option.option_id}"${
          disabled ? " disabled" : ""
        }>${esc(option.label)}</button>${option.effect ? `<span class="ag-eff">${esc(option.effect)}</span>` : ""}</div>`,
    )
    .join("");
  return `<div class="ag-q" data-ag-q="${esc(question.question_id)}"><div class="ag-qt">${esc(question.text)}${
    open ? `<span class="pill warn">Needs an answer</span>` : ""
  }</div><div class="ag-opts" role="group" aria-label="${esc(question.text)}">${options}</div></div>`;
}

function checklistHtml(g, closed) {
  const session = g.session;
  const questions = session.questions || [];
  const disabled = closed || Boolean(g.busy);
  const asked = questions.length
    ? `<section class="ag-group" data-ag-group="questions"><h4>Questions</h4>${questions
        .map((q) => questionHtml(q, disabled))
        .join("")}</section>`
    : "";
  const groups = GROUPS.map((group) => {
    const items = (session.proposals || []).filter((p) => group.kinds.includes(p.kind));
    if (!items.length && !group.empty) return "";
    const cap = items.length ? group.cap : group.empty;
    return `<section class="ag-group" data-ag-group="${esc(group.id)}"><h4>${esc(group.title)}</h4>${
      cap ? `<p class="ag-cap">${esc(cap)}</p>` : ""
    }${items.map((p) => itemHtml(g, p, closed || g.busy === "approve")).join("")}</section>`;
  }).join("");
  return `${asked}${groups}`;
}

function stopHtml(entry, n) {
  const session = entry.state.session;
  return step(
    n,
    "This data cannot be used as it is",
    false,
    `<div class="ag-stop" role="status" data-ag-stop><p class="ag-lead">${esc(dash(session.stop_reason))}</p>
    <div class="btn-row"><button type="button" class="btn secondary sm" id="ag-restart">Upload another file</button><button type="button" class="btn quiet sm" data-ag-manual>Use Manual setup instead</button></div></div>`,
  );
}

const listHtml = (title, items) =>
  `<div><h5>${esc(title)}</h5>${
    items && items.length
      ? `<ul>${items.map((item) => `<li>${esc(item)}</li>`).join("")}</ul>`
      : `<span class="ag-none">${EM_DASH}</span>`
  }</div>`;

/** One step's effect on the rows it read, in words, from the receipt's own counts. */
function stepLine(result) {
  if (result.kind === "drop_column") return `${result.column}: hidden from the model`;
  const failed = result.failed ? `, ${fmtInt(result.failed)} could not be read` : "";
  return `${result.column}: ${fmtInt(result.changed)} of ${fmtInt(result.rows)} values changed${failed}`;
}

const stepsList = (receipt) =>
  receipt && (receipt.steps || []).length
    ? `<ul class="ag-steps">${receipt.steps.map((r) => `<li>${esc(stepLine(r))}</li>`).join("")}</ul>`
    : "";

/** The columns the steps read, before and after, on the API's preview rows. */
function previewTables(preview) {
  const receipt = preview.receipt;
  if (!receipt || !(receipt.steps || []).length) {
    return `<p class="ag-note" data-ag-nochange>None of the ticked fixes changes the data, so the file is used as it is.</p>`;
  }
  const touched = [...new Set(receipt.steps.map((r) => r.column))];
  const table = (columns, rows) => {
    const shown = touched.filter((name) => columns.includes(name));
    if (!shown.length) return `<span class="ag-none">${EM_DASH}</span>`;
    const index = shown.map((name) => columns.indexOf(name));
    return dataTable(
      shown.map((label, i) => ({ label, more: i >= 5 })),
      (rows || []).map((row) => index.map((i) => esc(dash(row[i])))),
      { moreLabel: "Show more columns" },
    );
  };
  const hidden = touched.filter((name) => preview.columns_before.includes(name) && !preview.columns_after.includes(name));
  return `<div class="ag-preview" data-ag-preview><h5>Before</h5>${table(preview.columns_before, preview.rows_before)}<h5>After</h5>${table(
    preview.columns_after,
    preview.rows_after,
  )}${hidden.length ? `<p class="ag-note">${esc(`Hidden from the model: ${hidden.join(", ")}`)}</p>` : ""}<h5>On these rows</h5>${stepsList(
    receipt,
  )}</div>`;
}

function summaryHtml(entry, n, closed) {
  const g = entry.state;
  const summary = g.session.summary || {};
  const dirty = !closed && decisionsToSend(g).length > 0;
  // Once approved, the receipt below says what the steps did to every row; the preview would repeat it.
  const preview = closed
    ? ""
    : g.busy === "preview"
      ? `<div class="loading" role="status">Preparing the preview…</div>`
      : g.previewError
        ? errorBox(g.previewError)
        : g.preview
          ? previewTables(g.preview)
          : "";
  return step(
    n,
    "Summary",
    closed,
    `<div class="ag-sum" data-ag-summary>${listHtml("What will change", summary.decisions)}${listHtml(
      "What the helper assumed",
      summary.assumptions,
    )}${listHtml("What will happen when you run", summary.intended_actions)}${listHtml(
      "Columns the model will not see",
      summary.hidden_columns,
    )}</div><p class="ag-note" data-ag-dirty${dirty ? "" : " hidden"}>This summary is brought up to date with your ticks when you preview the changes or approve.</p>
    ${
      closed
        ? ""
        : `<div class="btn-row ag-previewbar"><button type="button" class="btn secondary sm" id="ag-preview"${
            g.busy ? " disabled" : ""
          }>Preview changes</button></div>`
    }${preview}`,
  );
}

/** A `409` from Approve with the Run button's checks: read-only, in the Setup form's own layout. */
function checksHtml(report) {
  const items = (report.checks || [])
    .map((check) => {
      const severity = check.severity === "error" ? "bad" : check.severity === "warning" ? "warn" : "ok";
      const entry = glossaryCode(check.code);
      return `<div class="vitem"><span class="pill ${severity}" data-code="${esc(check.code)}">${esc(
        (entry && entry.title) || check.code,
      )}</span><div><div class="vmsg">${esc(check.message)}</div>${
        check.suggestion ? `<div class="vsug">${esc(check.suggestion)}</div>` : ""
      }</div></div>`;
    })
    .join("");
  const errors = report.error_count || 0;
  const head = `${fmtInt(errors)} ${errors === 1 ? "problem" : "problems"} must be fixed before this data can be used. Change your ticks or answers above and approve again, or open Manual setup.`;
  return `<div class="vlist" role="alert" data-ag-checks><div class="vhead">${esc(head)}</div>${items}</div>`;
}

function appliedHtml(entry) {
  const g = entry.state;
  const applied = g.applied;
  const receipt = applied.receipt;
  const line = receipt
    ? `Your data is prepared in a copy of your file: ${fmtInt(receipt.rows_out)} rows and ${fmtInt(receipt.columns_out)} columns.`
    : "Your file is used as it is: none of the approved changes needed a copy.";
  const holds = entry.host.holds(applied.upload_id);
  const next = holds
    ? "Run it below. Manual setup shows every choice filled in."
    : "The Setup form holds another file now. Use the prepared file to run it.";
  return `<div class="ag-applied" data-ag-applied><p class="ag-done"><span class="ok">✓ Approved.</span> ${esc(line)}</p>${stepsList(
    receipt,
  )}<p class="ag-note">${esc(next)}</p>${g.fillError ? errorBox(g.fillError) : ""}<div class="btn-row">${
    holds
      ? ""
      : `<button type="button" class="btn secondary sm" id="ag-fill"${g.busy ? " disabled" : ""}>Use the prepared file</button>`
  }<button type="button" class="btn quiet sm" data-ag-manual>Open Manual setup</button><button type="button" class="btn quiet sm" id="ag-restart">Start again with another file</button></div></div>`;
}

function actionsHtml(entry) {
  const g = entry.state;
  if (g.applied) return appliedHtml(entry);
  const open = openQuestions(g.session);
  const why = !entry.host.mayRun
    ? "Your role cannot start runs."
    : open.length
      ? "Answer the questions marked “Needs an answer” first."
      : "";
  return `${g.report ? checksHtml(g.report) : ""}${g.actionError ? errorBox(g.actionError) : ""}<div class="actions"><button type="button" class="btn primary" id="ag-approve"${
    why || g.busy ? " disabled" : ""
  }>${esc(g.busy === "approve" ? "Approving…" : "Approve")}</button><span class="reason">${esc(
    why || "Approving prepares a copy of your file with the ticked fixes. Your file itself is not changed.",
  )}</span></div>`;
}

function mainHtml(entry) {
  const g = entry.state;
  const score = entry.mode === "score";
  // Score mode's first step is the Setup form's own model choice, drawn above this panel.
  const first = Number(entry.host.firstStep) || 1;
  const [one, two, three] = [first, first + 1, first + 2];
  const fixes = score ? "Checks" : "Data fixes";
  if (!g.session) {
    return `${uploadStep(entry, one)}${laterStep(
      two,
      fixes,
      score
        ? "After upload: the helper checks the file against what the model was trained on."
        : "After upload: the helper checks your data and suggests fixes, with a reason for each.",
    )}${laterStep(three, "Summary", "What will change, and what happens when you run. Then approve.")}`;
  }
  if (g.session.status === "stopped") return `${uploadStep(entry, one)}${stopHtml(entry, two)}`;
  const closed = Boolean(g.applied) || g.session.status === "applied";
  return `${uploadStep(entry, one)}${step(
    two,
    fixes,
    closed,
    `<p class="ag-lead">Ticked suggestions are applied when you approve. Untick any you do not want; the ones marked “Please check” need your eye.</p>${checklistHtml(
      g,
      closed,
    )}`,
  )}${summaryHtml(entry, three, closed)}${actionsHtml(entry)}`;
}

/** At most this many numbers of one tool result are shown beside a reply: enough to find the one it quotes. */
const SOURCE_NUMBERS = 4;

/**
 * One tool result a reply cites, compactly: the tool, the column (or other text arguments) it read,
 * and the first plain numbers of its result - the ones a reply can quote. Nothing is reworded or
 * rounded beyond the house formats; sample values and lists are left out (they are masked, and long).
 */
function sourceLine(result) {
  const words = (key) => String(key).replace(/_/g, " ");
  const read = Object.values(result.args || {}).filter((v) => typeof v === "string" && v);
  const numbers = Object.entries(result.result || {})
    .filter(([, v]) => typeof v === "number" && Number.isFinite(v))
    .slice(0, SOURCE_NUMBERS)
    .map(([k, v]) => `${words(k)} ${Number.isInteger(v) ? fmtInt(v) : fmtNum(v, 3)}`);
  return [words(result.tool), ...read, ...numbers].join(" · ");
}

/** "Source: …" under a helper reply, one line per cited tool result; nothing when it cites none. */
function sourcesHtml(ids, evidence) {
  const cited = (ids || []).map((id) => evidence.get(id)).filter(Boolean);
  if (!cited.length) return "";
  return `<div class="gmeta ag-src" data-ag-sources>${cited
    .map((r) => `<span>${esc(`Source: ${sourceLine(r)}`)}</span>`)
    .join("")}</div>`;
}

function asideHtml(entry) {
  const g = entry.state;
  const session = g.session;
  if (!session) return "";
  const practice =
    g.chat && g.chat.backend === "fake"
      ? `<p class="ag-practice" data-ag-practice>Practice answers: replies are sample text, not from a real AI service. The suggestions do not depend on them.</p>`
      : "";
  const evidence = new Map((session.tool_results || []).map((r) => [r.evidence_id, r]));
  const messages = (session.transcript || [])
    .map((m) =>
      m.role === "user"
        ? `<div class="gmsg user">${esc(m.text)}</div>`
        : `<div class="gmsg bot">${esc(m.text)}${sourcesHtml(m.evidence_ids, evidence)}</div>`,
    )
    .join("");
  const pending = g.asking
    ? `<div class="gmsg user">${esc(g.asking)}</div><div class="gmsg bot"><span class="loading">Thinking…</span></div>`
    : "";
  const closed = Boolean(g.applied) || session.status === "applied";
  const empty = `<div class="empty">Ask why a fix is suggested, or ask for a change such as “make training faster”. A change the helper suggests appears in the list to tick.</div>`;
  return `<section class="card ag-chat" id="ag-chat"><h3>${esc(session.agent_name)}</h3>${practice}<div class="gchat">${
    messages || pending ? `${messages}${pending}` : empty
  }</div>${g.chatError ? `<div class="card-body">${errorBox(g.chatError)}</div>` : ""}<form id="ag-ask" class="gaskrow"><div class="control"><input type="text" id="ag-question" aria-label="Ask the helper" placeholder="Ask the helper…" value="${esc(
    g.draft,
  )}" autocomplete="off" maxlength="1000"${closed ? " disabled" : ""}></div><button type="submit" class="btn secondary"${
    g.busy || closed ? " disabled" : ""
  }>Ask</button></form></section>`;
}

function draw(entry) {
  entry.main.innerHTML = mainHtml(entry);
  entry.aside.innerHTML = asideHtml(entry);
  entry.aside.hidden = !entry.state.session;
  bind(entry);
}

// --- behaviour -------------------------------------------------------------------------------------

async function uploadFile(entry, file) {
  const g = entry.state;
  Object.assign(g, fresh(), { phase: "uploading", fileName: file.name });
  draw(entry);
  try {
    g.upload = await postUpload(file, entry.host.uc.id, entry.mode);
  } catch (error) {
    g.uploadError = error;
    g.phase = "upload";
    draw(entry);
    return;
  }
  g.phase = "starting";
  draw(entry);
  try {
    adopt(g, await startSession(g.upload.upload_id, entry.host.uc.id));
    g.phase = "session";
    announceStatus(g.session.status === "stopped" ? "The helper stopped: see why" : "The helper's suggestions are ready");
  } catch (error) {
    g.uploadError = error;
    g.phase = "upload";
  }
  draw(entry);
}

/** Send every pending or changed decision; nothing is sent when the session already agrees. */
async function syncDecisions(entry) {
  const g = entry.state;
  const decisions = decisionsToSend(g);
  if (!decisions.length) return;
  adopt(g, await postDecisions(g.upload.upload_id, decisions));
}

async function answerQuestion(entry, questionId, optionId) {
  const g = entry.state;
  g.busy = "answer";
  g.actionError = null;
  draw(entry);
  try {
    adopt(g, await postAnswer(g.upload.upload_id, questionId, optionId));
    g.preview = null;
    g.report = null;
  } catch (error) {
    g.actionError = error;
  }
  g.busy = "";
  draw(entry);
}

async function preview(entry) {
  const g = entry.state;
  g.busy = "preview";
  g.previewError = null;
  draw(entry);
  try {
    await syncDecisions(entry);
    g.preview = await postPreview(g.upload.upload_id);
  } catch (error) {
    g.previewError = error;
  }
  g.busy = "";
  draw(entry);
}

/** Fill the Setup form with what Approve returned; the prepared upload's profile is read first, so
 * Manual setup shows the prepared file exactly as it shows one uploaded there. */
async function fill(entry) {
  const g = entry.state;
  const applied = g.applied;
  g.busy = "fill";
  g.fillError = null;
  draw(entry);
  let profile;
  try {
    profile = await getProfile(applied.upload_id);
  } catch (error) {
    g.fillError = error;
    g.busy = "";
    draw(entry);
    return;
  }
  g.busy = "";
  draw(entry);
  entry.host.approved({
    mode: applied.mode,
    upload: { upload_id: applied.upload_id, profile },
    primaryKey: applied.primary_key,
    target: applied.target,
    overrides: applied.overrides || {},
  });
}

async function approve(entry) {
  const g = entry.state;
  g.busy = "approve";
  g.actionError = null;
  g.report = null;
  draw(entry);
  try {
    await syncDecisions(entry);
    g.applied = await postApply(g.upload.upload_id);
  } catch (error) {
    g.busy = "";
    if (error instanceof ApiError && error.status === 409 && error.body && error.body.validation) {
      g.report = error.body.validation;
    } else {
      g.actionError = error;
    }
    draw(entry);
    return;
  }
  g.session = { ...g.session, status: "applied" };
  announceStatus("Approved: ready to run");
  await fill(entry);
}

async function ask(entry) {
  const g = entry.state;
  const text = g.draft.trim();
  if (!text || g.busy) return;
  g.busy = "chat";
  g.asking = text;
  g.chatError = null;
  draw(entry);
  try {
    adopt(g, await postMessage(g.upload.upload_id, text));
    g.draft = "";
  } catch (error) {
    g.chatError = error;
  }
  g.busy = "";
  g.asking = "";
  draw(entry);
  const input = entry.aside.querySelector("#ag-question");
  if (input && typeof input.focus === "function") input.focus();
}

function restart(entry) {
  entry.state = fresh();
  draw(entry);
}

function bind(entry) {
  const main = entry.main;
  const g = entry.state;
  const file = main.querySelector("#ag-file");
  if (file) {
    file.addEventListener("change", (event) => {
      const chosen = event.target.files && event.target.files[0];
      if (chosen) uploadFile(entry, chosen);
    });
  }
  main.querySelectorAll("[data-ag-proposal]").forEach((box) =>
    box.addEventListener("change", () => {
      g.ticks[box.dataset.agProposal] = box.checked;
      g.report = null;
      const note = main.querySelector("[data-ag-dirty]");
      if (note) note.hidden = decisionsToSend(g).length === 0;
    }),
  );
  main.querySelectorAll("[data-ag-option]").forEach((button) =>
    button.addEventListener("click", () => answerQuestion(entry, button.dataset.agQuestion, button.dataset.agOption)),
  );
  const on = (root, id, fn) => {
    const element = root.querySelector(`#${id}`);
    if (element) element.addEventListener("click", fn);
  };
  on(main, "ag-preview", () => preview(entry));
  on(main, "ag-approve", () => approve(entry));
  on(main, "ag-fill", () => fill(entry));
  on(main, "ag-restart", () => restart(entry));
  main.querySelectorAll("[data-ag-manual]").forEach((button) => button.addEventListener("click", () => entry.host.manual()));
  const input = entry.aside.querySelector("#ag-question");
  if (input) input.addEventListener("input", () => (g.draft = input.value));
  const form = entry.aside.querySelector("#ag-ask");
  if (form) {
    form.addEventListener("submit", (event) => {
      event.preventDefault();
      if (input) g.draft = input.value;
      ask(entry);
    });
  }
}

/**
 * Mount the Guided setup tab into the two elements the Setup form just painted (`registerSetupMode`,
 * `modules/router.js`). `host` is the Setup form's door back: `{ uc, mode, mayRun, holds(uploadId),
 * approved(fill), manual() }`.
 */
export function mountGuided(slots, host) {
  injectAgentStyles();
  const key = `${host.uc.id}|${host.mode}`;
  let entry = entries.get(key);
  if (!entry) {
    entry = { mode: host.mode, state: fresh(), main: document.createElement("div"), aside: document.createElement("div") };
    entry.main.className = "ag";
    entry.aside.className = "ag-aside";
    entries.set(key, entry);
  }
  entry.host = host;
  draw(entry);
  slots.main.replaceChildren(entry.main);
  if (slots.aside) slots.aside.replaceChildren(entry.aside);
}
