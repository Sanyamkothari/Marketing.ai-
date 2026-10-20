// The chat-first Data Doctor screen's markup (pure functions from `GET /cases/{case_id}/screen` to HTML strings; no
// DOM, no fetch). The screen reads like any agent that just does the work for you:
//
//   the conversation   `view.conversation` (`engine/doctor/conversation.py`), one card after another: what was looked
//                      at, each fix as a one-line change with Undo, the questions only the person can answer as
//                      chips, doubts, the goals (or whether the data fits the goal chosen first), the chat, and ONE
//                      "Approve and start" card
//   the composer       a box to type in, with a few suggested prompts; with no AI service it says so in plain words
//                      and every button still works
//   the data panel     a short live checklist and the first rows of each file (`view.preview` when the server sends
//                      it, else the sample rows the plan already holds), with "See details" for today's full plan
//
// Every title, sentence, option and reply is a string the API wrote: escaped here, never reworded; the person's own
// file and column names are drawn inside `data-user-text`. Same data attributes as the plan view where the action is
// the same (`data-dds-answer` + `data-dds-q` answer a question, `data-dds-goal` chooses a goal), so one click handler
// serves both.

import { button, icon, statusChip } from "../../components.js";
import { esc } from "../../dom.js";
import { count } from "../../format.js";
import { goalName, userText } from "../../labels.js";
import { AI_SETTINGS_HREF } from "../../availability.js";
import { minutesText } from "../campaigns/run_rules.js";
import { sentHtml, plain, TOOL_PHRASES } from "../agent/sent.js";
import { samplesHtml } from "./plan_view.js";
import { AI_CONSENT_BUTTON, AI_CONSENT_LINE, AI_SPENT_LINE, VERDICTS, columnNote, namesOf, standingHtml, userTextIn as nameIn } from "./view.js";

/** A server sentence with the person's names in it; a column is said in words (`marketing opt in`), not as the file spells it. */
const userTextIn = (text, names) => nameIn(text, names, { friendly: true });

/** What the chat says at once while the server works (by what was clicked); a chat message says "Thinking…". */
export const PROGRESS = Object.freeze({
  upload: "Reading your files…",
  approve: "Checking everything before we start…",
  continue: "Starting your campaign…",
  chat: "Thinking…",
});
export const progressText = (busy) => PROGRESS[busy] || "Checking that…";

const plural = (n, one, many) => `${count(n)} ${n === 1 ? one : many}`;
const cards = (screen) => (screen && screen.view && screen.view.conversation) || [];

/** The one card of a kind (`first_look`, `ready`, ...), or null. */
export const cardOf = (screen, type) => cards(screen).find((c) => c.type === type) || null;

// --- the checklist ----------------------------------------------------------------------------------------------

/**
 * The short live checklist beside the chat: looked at / fixes / questions left / goal / ready. Every number is one the
 * server counted (`first_look`, the change cards, the open questions).
 */
export function checklistOf(screen) {
  const first = cardOf(screen, "first_look");
  const view = screen.view;
  const changes = cards(screen).filter((c) => c.type === "change" && c.state === "accepted").length;
  const open = cards(screen).filter((c) => c.type === "question" && (c.answer === null || c.answer === undefined)).length;
  const ready = ["ready", "ready_with_warnings"].includes(view.verdict.key);
  return [
    { id: "looked", done: Boolean(first), text: first ? `Looked at ${plural(first.files.length, "file", "files")}` : "Looking at your data" },
    { id: "fixes", done: Boolean(first), text: changes ? `${plural(changes, "fix", "fixes")} made` : "No fixes needed so far" },
    { id: "questions", done: open === 0, text: open ? `${plural(open, "question", "questions")} left` : "No questions left" },
    { id: "goal", done: Boolean(view.goal_id), text: view.goal_id ? `Goal: ${goalName(view.goal_id)}` : "Goal: not chosen yet" },
    { id: "ready", done: ready, text: ready ? "Ready to start" : "Not ready to start yet" },
  ];
}

function checklistHtml(screen) {
  const rows = checklistOf(screen)
    .map(
      (r) =>
        `<li data-dds-check-item="${r.id}" class="${r.done ? "is-done" : ""}">${icon(r.done ? "checkCircle" : "draft", { size: 16 })}<span>${esc(r.text)}</span></li>`,
    )
    .join("");
  return `<section class="card ddc-box" data-dds-checklist-panel aria-labelledby="ddc-progress-t"><h3 class="dds-group-t" id="ddc-progress-t">Progress</h3><ul class="ddc-checklist">${rows}</ul></section>`;
}

// --- the data panel ---------------------------------------------------------------------------------------------

/** The preview's files (`view.preview.files`), or none when the server sent no preview. */
export const previewFiles = (view) => (view && view.preview && Array.isArray(view.preview.files) ? view.preview.files : []);

function previewTable(file, side) {
  const columns = file.columns || [];
  const rows = (side === "before" ? file.before : file.after) || [];
  if (!columns.length || !rows.length) return `<p class="dds-why">No rows to show for this file yet.</p>`;
  const head = columns
    .map(
      (c) =>
        `<th scope="col"${c.changed && side === "after" ? ` class="is-changed"` : ""}>${userText(plain(c.name))}${
          c.changed && side === "after" ? ` <span class="ddc-mark">fixed</span>` : ""
        }</th>`,
    )
    .join("");
  const body = rows
    .map((r) => `<tr>${columns.map((c, i) => `<td${c.personal ? ` class="dds-hidden-cell"` : ""}>${userText(plain(String(r[i] ?? "")))}</td>`).join("")}</tr>`)
    .join("");
  const changed = columns.filter((c) => c.changed).length;
  const caption = `${side === "before" ? "First rows of your file, as they are" : "First rows after the fixes"}${
    file.total_rows ? ` (of ${count(file.total_rows)})` : ""
  }${side === "after" && changed ? `, ${plural(changed, "column", "columns")} fixed` : ""}. Personal details are hidden.`;
  return `<div class="ddc-table-wrap"><table class="ddc-table" data-dds-preview-table="${side}"><caption class="sr">${esc(caption)}</caption><thead><tr>${head}</tr></thead><tbody>${body}</tbody></table></div><p class="dds-why">${esc(caption)}</p>${
    file.note ? `<p class="dds-why">${esc(file.note)}</p>` : ""
  }`;
}

/** Without a preview: the sample rows each step of the plan already holds (`ledger.steps[].samples`), personal ones hidden. */
function samplesPanel(screen) {
  const { case: c, view } = screen;
  const steps = ((view.ledger && view.ledger.steps) || []).filter((s) => s.samples && Array.isArray(s.samples.after) && s.samples.after.length).slice(0, 3);
  if (!steps.length) return `<p class="dds-why" data-dds-no-rows>Rows appear here once Data Doctor has read your data.</p>`;
  const title = (id) => {
    const item = (c.items || []).find((i) => i.item_id === id);
    return plain((item && item.title) || "A fix");
  };
  return steps
    .map((s) => samplesHtml(s.samples.after.slice(0, 3), { personal: s.samples.personal_columns || [], caption: title(s.item_id) }))
    .join("");
}

/** "4,000 customers" (the customer list's rows, else the first file's), or "" when nothing was counted. */
export function dataSummary(screen) {
  const files = (cardOf(screen, "first_look") || { files: [] }).files.filter((f) => Number.isFinite(f.rows));
  const customers = files.find((f) => /customer/i.test(f.kind || ""));
  const pick = customers || files[0];
  return pick ? plural(pick.rows, customers ? "customer" : "row", customers ? "customers" : "rows") : "";
}

/** The right-hand panel: progress, the data (tabs per file, After or Before), earlier answers, and "See details". On a phone it is one line, "Your data · 4,000 customers · Show", until it is opened. */
export function panelHtml(screen, { tab = null, side = "after", intake = "", busy = false, open = false } = {}) {
  const files = previewFiles(screen.view);
  const chosen = files.find((f) => f.upload_id === tab) || files[0] || null;
  const tabs =
    files.length > 1
      ? `<div class="dds-actions" role="group" aria-label="Files">${files
          .map((f) =>
            button({
              label: plain(f.file_name),
              kind: f === chosen ? "primary" : "secondary",
              size: "sm",
              attrs: `data-dds-file-tab="${esc(f.upload_id)}" aria-pressed="${f === chosen}"`,
            }),
          )
          .join("")}</div>`
      : "";
  const sides = chosen
    ? `<div class="dds-actions" role="group" aria-label="Rows shown">${[
        ["after", "After the fixes"],
        ["before", "As in your file"],
      ]
        .map(([id, label]) => button({ label, kind: side === id ? "primary" : "secondary", size: "sm", attrs: `data-dds-side="${id}" aria-pressed="${side === id}"` }))
        .join("")}</div>`
    : "";
  const data = chosen
    ? `${tabs}${sides}${previewTable(chosen, side === "before" ? "before" : "after")}`
    : samplesPanel(screen);
  const names = (screen.case.files || []).map((f) => userText(plain(f.file_name))).join(", ");
  const heading = chosen ? `Your data: ${userText(plain(chosen.file_name))}` : "Your data";
  const summary = dataSummary(screen);
  const toggle = `<button type="button" class="btn secondary ddc-panel-toggle" data-dds-panel-toggle aria-expanded="${open}" aria-controls="ddc-panel-body"><span>${esc(
    `Your data${summary ? ` · ${summary}` : ""} · `,
  )}<span class="ddc-panel-act" data-dds-panel-act>${open ? "Hide" : "Show"}</span></span></button>`;
  return `<aside class="ddc-panel${open ? " is-open" : ""}" aria-label="Your data" data-dds-panel>${toggle}<div class="ddc-panel-body" id="ddc-panel-body">${checklistHtml(screen)}<section class="card ddc-box" data-dds-data aria-labelledby="ddc-data-t"><h3 class="dds-group-t" id="ddc-data-t">${heading}</h3>${
    files.length || !names ? "" : `<p class="dds-files">${icon("data", { size: 16 })}<span>${names}</span></p>`
  }${data}</section>${standingHtml(screen.view, { busy, level: 3 })}${intake ? `<div class="dds-more" data-dds-more>${intake}</div>` : ""}<div class="dds-actions">${button(
    { label: "See details (for experts)", kind: "tertiary", attrs: "data-dds-see-details" },
  )}</div></div></aside>`;
}

// --- the cards --------------------------------------------------------------------------------------------------

const agentRow = (inner, attrs = "") =>
  `<div class="ddc-row ddc-agent" ${attrs}><span class="ddc-avatar" aria-hidden="true">DD</span><div class="ddc-stack">${inner}</div></div>`;
const indented = (inner, attrs = "") => `<div class="ddc-indent" ${attrs}>${inner}</div>`;

/** Plain phrases for what the agent did; a tool without one is left out (it is shown under technical details only). */
const DID = Object.freeze({
  ...TOOL_PHRASES,
  propose_step: "Suggested a change",
  propose_sql: "Prepared a change to the table",
  join_files: "Joined the files",
  stack_files: "Stacked the files",
  ask: "Asked you a question",
  suggest_answer: "Suggested an answer",
  express_doubt: "Said what it is not sure about",
  request_data: "Asked for more data",
  withdraw: "Took back a change",
  revise: "Changed a suggestion",
  preview_working_copy: "Looked at the result",
});

const WHO = Object.freeze({ rules: "Done by the rules", agent: "Done by Data Doctor", you: "Your choice", someone: "Chosen by a teammate" });

function changeHtml(card, names, busy, { clicked: clickedKey = "", column = "" } = {}) {
  const accepted = card.state === "accepted";
  const waiting = card.state === "pending";
  const sub = [
    card.counts ? userTextIn(card.counts, names) : "",
    columnNote([column]),
    accepted && card.by ? esc(WHO[card.by] || "") : "",
    waiting ? esc(card.source === "ai" ? "Suggested by the AI. Waiting for your yes." : "Waiting for your yes.") : "",
    card.state === "rejected" ? esc(card.parked_for ? "Waiting for someone who would know. Left out." : "Undone. Not in the plan.") : "",
  ]
    .filter(Boolean)
    .join(" · ");
  const id = esc(card.item_id);
  const usingIt = clickedKey === `use:${card.item_id}`; // the one that was clicked shows it is working
  const undoing = clickedKey === `undo:${card.item_id}`;
  const actions = accepted
    ? button({ label: "Undo", kind: "secondary", size: "sm", attrs: `data-dds-undo="${id}" aria-label="Undo: ${esc(plain(card.title))}"`, disabled: busy, busy: undoing })
    : `${button({ label: waiting ? "Use this" : "Do it again", kind: waiting ? "primary" : "secondary", size: "sm", attrs: `data-dds-use="${id}" aria-label="${waiting ? "Use" : "Do again"}: ${esc(plain(card.title))}"`, disabled: busy, busy: usingIt })}${
        waiting ? button({ label: "Skip", kind: "tertiary", size: "sm", attrs: `data-dds-undo="${id}" aria-label="Skip: ${esc(plain(card.title))}"`, disabled: busy, busy: undoing }) : ""
      }`;
  return indented(
    `<div class="ddc-change is-${esc(card.state)}" data-dds-change-card="${id}" data-dds-change-state="${esc(card.state)}">${icon(accepted ? "checkCircle" : "draft", { size: 18 })}<div class="ddc-change-body"><span class="dds-item-t">${userTextIn(
      card.title,
      names,
    )}</span>${sub ? `<span class="dds-item-sub">${sub}</span>` : ""}</div><div class="dds-actions">${actions}</div></div>`,
  );
}

function chip(option, q, { chosen = false, busy = false, dk = false, clicked = "" } = {}) {
  return `<li class="dds-opt${chosen ? " is-chosen" : ""}">${button({
    label: option.label,
    kind: chosen ? "primary" : "secondary",
    attrs: `data-dds-answer="${esc(option.id)}" data-dds-q="${esc(q.question_id)}"${dk ? " data-dds-dk-chip" : ""} aria-pressed="${chosen}"`,
    disabled: busy || chosen,
    busy: clicked === `answer:${q.question_id}|${option.id}`,
  })}${option.effect ? `<span class="dds-effect">${userTextIn(option.effect, [])}${option.rows_changed != null ? ` · ${esc(option.rows_changed === 0 ? "Changes no rows" : `Changes ${plural(option.rows_changed, "row", "rows")}`)}` : ""}</span>` : ""}${
    option.recommended ? `<span class="dds-effect">Recommended</span>` : ""
  }</li>`;
}

function questionHtml(q, names, { busy, remembered, clicked = "", column = "" }) {
  const answered = q.answer !== null && q.answer !== undefined;
  const all = [...q.options, ...(q.dont_know ? [q.dont_know] : [])];
  const chose = all.find((o) => o.id === q.answer);
  const chips = [
    ...q.options.map((o) => chip(o, q, { chosen: o.id === q.answer, busy, clicked })),
    ...(q.dont_know ? [chip(q.dont_know, q, { chosen: q.dont_know.id === q.answer && !q.options.some((o) => o.id === q.answer), busy, dk: true, clicked })] : []),
  ].join("");
  const state = q.note
    ? `<p class="dds-state" data-dds-assumed>${icon("info", { size: 14 })}<span>${userTextIn(q.note, names)}</span></p>`
    : answered
      ? `<p class="dds-state" data-dds-answered>${icon("check", { size: 14 })}<span>${esc(`Your answer: ${chose ? chose.label : q.answer}.`)}${
          q.counts ? ` ${userTextIn(q.counts, names)}.` : ""
        } ${esc("Tap another answer to change it.")}</span></p>`
      : "";
  const memory =
    remembered && answered && !remembered.revoked
      ? `<div class="dds-remembered" data-dds-remembered="${esc(remembered.answer_id)}"><p class="dds-state">${icon("clock", { size: 14 })}<span>Remembered for next time.</span></p>${button({
          label: "Revoke",
          kind: "secondary",
          size: "sm",
          attrs: `data-dds-revoke="${esc(remembered.answer_id)}" aria-label="Revoke the remembered answer"`,
          disabled: busy,
        })}</div>`
      : "";
  return indented(
    `<article class="dds-card dds-q${answered ? "" : " is-open"}" data-dds-question="${esc(q.question_id)}" data-dds-code="${esc(q.code || "")}" aria-labelledby="ddcq-${esc(q.question_id)}"><p class="dds-card-t" id="ddcq-${esc(q.question_id)}">${userTextIn(
      q.text,
      names,
    )}</p>${columnNote([column]) ? `<p class="dds-effect" data-dds-column-note>${columnNote([column])}</p>` : ""}${q.blocking && !answered ? `<p class="dds-tag-row"><span class="chip neutral">Needed to go on</span></p>` : ""}${
      q.why ? `<p class="dds-why"><span class="dds-label">Why we ask:</span> ${userTextIn(q.why, names)}</p>` : ""
    }<ul class="dds-opts" aria-label="Answers">${chips}</ul>${state}${memory}</article>`,
  );
}

function doubtHtml(d, names) {
  const part = (label, text) => `<p class="dds-doubt-part"><span class="dds-label">${esc(label)}</span> ${userTextIn(text, names)}</p>`;
  return indented(
    `<article class="dds-card dds-doubt" data-dds-doubt="${esc(d.doubt_id)}"><p class="dds-card-t has-icon">${icon("info", { size: 16 })}<span>Data Doctor is not sure</span></p>${part(
      "I am not sure because",
      d.because,
    )}${part("Here is what I would do:", d.would_do)}${part("Here is what changes if not:", d.if_not)}</article>`,
  );
}

function requestHtml(r, names, { busy, kitHref }) {
  const answered = r.answer !== null && r.answer !== undefined;
  const chose = r.options.find((o) => o.id === r.answer);
  const kit = kitHref ? `<p class="dds-why"><a href="${esc(kitHref)}" data-dds-request-kit download>Download the list to send your data team</a></p>` : "";
  const chips = r.options.length
    ? answered
      ? `<p class="dds-state" data-dds-answered>${icon("check", { size: 14 })}<span>${esc(`Your answer: ${chose ? chose.label : r.answer}.`)}</span></p>`
      : `<div class="dds-actions">${r.options
          .map((o) => button({ label: o.label, kind: o.safe ? "tertiary" : "secondary", attrs: `data-dds-answer="${esc(o.id)}" data-dds-q="${esc(r.question_id)}"`, disabled: busy }))
          .join("")}</div>`
    : "";
  return indented(
    `<article class="dds-card dds-request" data-dds-request="${esc(r.question_id || "")}"><p class="dds-card-t has-icon">${icon("inbox", { size: 16 })}<span>${userTextIn(
      r.text,
      names,
    )}</span></p>${r.why ? `<p class="dds-why">${userTextIn(r.why, names)}</p>` : ""}${kit}${chips}</article>`,
  );
}

function goalsHtml(g, names, busy, clicked = "") {
  if (!g.cards.length) {
    return agentRow(
      `<div class="ddc-bubble" data-dds-goals>Nothing in this data fits one of the goals yet. ${button({
        label: "See details",
        kind: "tertiary",
        size: "sm",
        attrs: "data-dds-see-details",
      })}</div>`,
    );
  }
  const items = g.cards
    .map(
      (c) =>
        `<li class="dds-card ddc-goal" data-dds-goal-row="${esc(c.goal_id)}"><span class="dds-card-t">${esc(c.name)}${
          c.best ? ` <span class="chip neutral" data-dds-goal-best>Best fit</span>` : ""
        }</span>${c.why ? `<span class="dds-why">${userTextIn(c.why, names)}</span>` : ""}${button({
          label: "Use this",
          kind: c.best ? "primary" : "secondary",
          attrs: `data-dds-goal="${esc(c.goal_id)}" aria-label="Choose the goal: ${esc(c.name)}"`,
          disabled: busy,
          busy: clicked === `goal:${c.goal_id}`,
        })}</li>`,
    )
    .join("");
  const more = g.more_count
    ? `<p class="dds-why">${esc(`${plural(g.more_count, "more goal fits", "more goals fit")} less well.`)} ${button({ label: "See details", kind: "tertiary", size: "sm", attrs: "data-dds-see-details" })}</p>`
    : "";
  return `${agentRow(`<div class="ddc-bubble">Here is what your data is good for. Pick one, or tell me what you have in mind.</div>`)}${indented(
    `<ul class="ddc-goals" data-dds-goals>${items}</ul>${more}`,
  )}`;
}

function fitHtml(f, names) {
  return indented(
    `<article class="dds-card ddc-fit${f.ok ? "" : " is-not"}" data-dds-fit="${esc(f.goal_id)}" data-dds-fit-ok="${f.ok}"><p class="dds-card-t has-icon">${icon(f.ok ? "checkCircle" : "alert", {
      size: 16,
    })}<span>${esc(f.ok ? `A good fit for “${f.name}”` : `Not a good fit for “${f.name}” yet`)}</span></p><p class="dds-why">${userTextIn(f.sentence, names)}</p></article>`,
  );
}

function messageHtml(m, names, { busy, openSent, index, clicked = "" }) {
  if (m.role === "user") return `<div class="ddc-row ddc-user"><div class="ddc-bubble" data-dds-msg="user">${esc(plain(m.text))}</div></div>`;
  const did = [...new Set((m.tools || []).map((t) => DID[t]).filter(Boolean))];
  const trace = did.length
    ? `<details class="ddc-trace" data-dds-keep="trace-${index}"><summary>How I did this</summary><ul>${did.map((d) => `<li>${esc(d)}</li>`).join("")}</ul></details>`
    : "";
  const back = (m.withdrawn || []).length
    ? `<p class="dds-why" data-dds-withdrawn>${esc("I took back: ")}${m.withdrawn.map((t) => userTextIn(t, names)).join("; ")}</p>`
    : "";
  const s = m.suggestion;
  const suggestion = s
    ? `<div class="ddc-suggestion" data-dds-suggestion="${esc(s.question_id)}"><p class="dds-why">${esc(`I understood: ${plain(s.label)}.`)}</p>${
        s.used
          ? `<p class="dds-state">${icon("check", { size: 14 })}<span>That question is answered.</span></p>`
          : button({ label: "Use this answer", kind: "primary", size: "sm", attrs: `data-dds-answer="${esc(s.option_id)}" data-dds-q="${esc(s.question_id)}" data-dds-use-answer`, disabled: busy, busy: clicked === `answer:${s.question_id}|${s.option_id}` })
      }</div>`
    : "";
  return agentRow(
    `<div class="ddc-bubble" data-dds-msg="agent">${esc(plain(m.text))}${sentHtml(m.sent, { id: index, open: openSent.has(String(index)) })}</div>${back}${trace}${suggestion}`,
  );
}

function firstLookHtml(f, names) {
  return agentRow(`<div class="ddc-bubble" data-dds-first-look>${userTextIn(f.sentence, names)}</div>`);
}

/** The ready card in the stage it is in. `ctx`: the ticks (`stops`, `confirms` sets), `speed`, `busy`, `startHtml` for the start stage, and for the approve stage `starts` (false: approving does not start a run, the button says "continue") and `approveHtml` (what will happen after the click). */
export function readyHtml(r, ctx) {
  const names = ctx.names || [];
  const notes = (r.notes || []).map((n) => `<li>${userTextIn(n, names)}</li>`).join("");
  const notesHtml = notes ? `<ul class="dds-reasons" data-dds-ready-notes>${notes}</ul>` : "";
  if (r.stage === "blocked") {
    return indented(
      `<section class="ddc-ready is-blocked" data-dds-ready="blocked"><h3 class="dds-group-t">Not ready yet</h3><p class="dds-why">${esc(plain(r.sentence))}</p>${notesHtml}</section>`,
    );
  }
  if (r.stage === "start") {
    const left = (r.left_out || []).length
      ? `<p class="dds-why" data-dds-left-out>${esc("Left out, because nobody agreed:")} ${r.left_out.map((s) => nameIn(s, names)).join("; ")}</p>`
      : "";
    return indented(
      `<section class="ddc-ready" data-dds-ready="start"><h3 class="dds-group-t">Ready to start</h3><p class="dds-why">The data is ready for this goal. Approved, checked and saved with this check.</p>${
        r.sentence ? `<blockquote class="dds-sentence" data-dds-approved-sentence>${userTextIn(r.sentence, names)}</blockquote>` : ""
      }${left}${ctx.alertHtml || ""}${ctx.startHtml || ""}</section>`,
    );
  }
  const stops = (r.stops || [])
    .map(
      (s) =>
        `<div class="ddc-stop" data-dds-stop-block="${esc(s.key)}"><label class="dds-tick ddc-tick" data-dds-stop-row="${esc(s.key)}"><input type="checkbox" data-dds-stop="${esc(s.key)}"${ctx.stops.has(s.key) ? " checked" : ""}${ctx.busy ? " disabled" : ""}><span class="dds-item-body"><span class="dds-item-t">${userTextIn(
          s.text,
          names,
        )}</span><span class="dds-item-sub">A safety check stops this plan until you say yes. Tick to go on anyway.</span></span></label>${
          s.detail
            ? `<details class="ddc-detail" data-dds-keep="stop-${esc(s.key)}"><summary>See details</summary><p class="dds-why" data-dds-stop-detail>${userTextIn(s.detail, names)}</p></details>`
            : ""
        }</div>`,
    )
    .join("");
  const yes = (r.confirmations || [])
    .map((c) => {
      const who = c.role === "approver" ? "an approver on your team" : "an analyst on your team";
      if (!c.can_agree) {
        const ask = c.cannot_agree === "separation" ? `You suggested or answered this, so ask ${who.replace(/^an? /, "another ")} to agree to it.` : `Ask ${who} to agree to it.`;
        return `<div class="dds-state ddc-ask" data-dds-ask-other="${esc(c.cannot_agree || "role")}">${icon("info", { size: 14 })}<span>${nameIn(c.sentence, names)}<br>${esc(ask)} ${esc(
          c.required ? "It is in the plan, so this waits for them." : "Until then it is left out.",
        )}</span></div>`;
      }
      return `<label class="dds-tick ddc-tick" data-dds-confirm-row="${esc(c.item_id)}"><input type="checkbox" data-dds-confirm="${esc(c.item_id)}"${ctx.confirms.has(c.item_id) ? " checked" : ""}${ctx.busy ? " disabled" : ""}><span class="dds-item-body"><span class="dds-item-t">${esc(
        "I agree: ",
      )}${nameIn(c.sentence, names)}</span><span class="dds-item-sub">${esc(`${c.shown ? "Shown above, with Undo. " : ""}${c.required ? "Needed to go on. " : "Left out unless you agree. "}Why it needs your yes: ${plain(c.why)}${c.separate ? ", from someone other than whoever suggested it" : ""}.`)}</span></span></label>`;
    })
    .join("");
  const speeds = (r.speeds || []).length
    ? `<div class="dds-actions" role="group" aria-label="How long to learn" data-dds-speeds>${r.speeds
        .map((sp) =>
          button({
            label: `${sp.name}: about ${minutesText(sp.minutes)}${sp.recommended ? " (recommended)" : ""}`,
            kind: ctx.speed === sp.id ? "primary" : "secondary",
            size: "sm",
            attrs: `data-dds-speed="${esc(sp.id)}" aria-pressed="${ctx.speed === sp.id}"`,
          }),
        )
        .join("")}</div>`
    : "";
  const ok = approveAllowed(r, ctx);
  const label = approveLabel(ctx);
  const hold = r.blocked
    ? `<p class="dds-why" data-dds-approve-hold>${esc("A change in the plan needs someone else's own yes first.")}</p>`
    : r.hold
      ? `<p class="dds-why" data-dds-approve-hold>${esc(r.hold)}</p>`
      : "";
  return indented(
    `<section class="ddc-ready" data-dds-ready="approve" aria-labelledby="ddc-ready-t"><h3 class="dds-group-t" id="ddc-ready-t">Ready to start</h3><blockquote class="dds-sentence" id="dds-approve-sentence" data-dds-approve-sentence>${esc(
      r.sentence,
    )}</blockquote>${notesHtml}${stops}${yes}${hold}${speeds}${ctx.approveHtml || ""}${ctx.alertHtml || ""}<div data-dds-approve-note>${leavesOutHtml(r, ctx)}</div><div class="dds-actions">${button({
      label,
      kind: "primary",
      attrs: `data-dds-approve-start aria-describedby="dds-approve-sentence"`,
      disabled: ctx.busy || !ok,
      busy: ctx.busy === "approve",
    })}<span class="dds-why">The only approval. Your files are not changed.</span></div></section>`,
  );
}

const approveLabel = (ctx) => (ctx.starts === false ? "Approve and continue" : "Approve and start");

/**
 * Next to the button: what the click leaves out (a change that needs its own yes and is not ticked, or cannot be agreed
 * to by this reader). Redrawn in place when a box is ticked.
 */
export function leavesOutHtml(r, ctx) {
  const names = ctx.names || [];
  const out = (r.confirmations || []).filter((c) => !c.required && (!c.can_agree || !ctx.confirms.has(c.item_id)));
  return out.length
    ? `<p class="dds-why" data-dds-approve-leaves-out>${esc(`${approveLabel(ctx)} leaves out:`)} ${out.map((c) => nameIn(c.sentence, names)).join("; ")}</p>`
    : "";
}

/** Whether the one click may go: every stop and every needed own yes is ticked, and nobody else must agree first. */
export function approveAllowed(r, ctx) {
  if (!r || r.stage !== "approve" || r.blocked || r.hold) return false;
  if ((r.stops || []).some((s) => !ctx.stops.has(s.key))) return false;
  return !(r.confirmations || []).some((c) => c.required && c.can_agree && !ctx.confirms.has(c.item_id));
}

// --- the composer -----------------------------------------------------------------------------------------------

/** 2-3 prompts to try: what was done, what to ask next. */
export function promptsOf(screen) {
  const open = cards(screen).some((c) => c.type === "question" && (c.answer === null || c.answer === undefined));
  const fixed = cards(screen).some((c) => c.type === "change");
  return [
    fixed ? "What did you change?" : "What did you find?",
    open ? "Why are you asking this?" : "Is anything risky here?",
    screen.view.goal_id ? "Is this data enough for my goal?" : "What is this data good for?",
  ];
}

/** The composer. With no AI service the box says so in plain words (the buttons above still work). */
export function composerHtml(screen, { ai = null, use = null, draft = "", busy = false, canConnect = true, canConsent = true } = {}) {
  let note = "";
  if (ai === false) {
    // One short line: what is missing and the way to it (the buttons above work without it).
    const way = canConnect
      ? `<a href="${esc(AI_SETTINGS_HREF)}" data-ai-settings-link>Connect it in Settings</a>.`
      : esc("Ask an admin to connect it.");
    note = `<div class="dds-nochat" role="status"><p data-ai-not-connected>${esc("Chat needs the AI service.")} ${way}</p></div>`;
  } else if (use && !use.consent) {
    note = `<div class="dds-nochat" role="status"><p data-ai-no-consent>${esc(AI_CONSENT_LINE)}</p>${
      canConsent
        ? `<div class="dds-actions">${button({ label: AI_CONSENT_BUTTON, kind: "secondary", attrs: "data-dds-ai-consent", disabled: busy })}</div>`
        : `<p class="dds-why">Ask someone who can change this check to allow it.</p>`
    }</div>`;
  } else if (use && use.spent) {
    note = `<div class="dds-nochat" role="status"><p data-ai-spent>${esc(AI_SPENT_LINE)}</p></div>`;
  }
  const off = Boolean(note);
  const prompts = promptsOf(screen)
    .map((p) => button({ label: p, kind: "tertiary", size: "sm", attrs: `data-dds-prompt="${esc(p)}"`, disabled: off || Boolean(busy) }))
    .join("");
  const working = busy && busy !== "chat" ? `<p class="ddc-progress" role="status" data-dds-progress><span class="ddc-spin" aria-hidden="true"></span><span>${esc(progressText(busy))}</span></p>` : "";
  return `<div class="ddc-composer${off ? " is-off" : ""}" data-dds-composer>${working}${note}<div class="ddc-prompts" role="group" aria-label="Things to ask">${prompts}</div><form id="dds-chat-form" class="ddc-form"><label class="sr" for="dds-chat-input">Message Data Doctor</label><input type="text" id="dds-chat-input" class="input" autocomplete="off" maxlength="1000" placeholder="${
    off ? "Chat is not available" : "Ask anything, or tell me what to change…"
  }" value="${esc(draft)}"${off || busy === "chat" ? " disabled" : ""}>${button({ label: "Send", kind: "primary", type: "submit", attrs: "data-dds-send", disabled: off || Boolean(busy) })}</form></div>`;
}

// --- the whole conversation -------------------------------------------------------------------------------------

/** The cards, in the server's order, then the composer. */
export function conversationHtml(screen, ctx) {
  const c = screen.case;
  const names = namesOf(c);
  const remembered = new Map((screen.view.remembered || []).map((r) => [r.question_id, r]));
  const busy = Boolean(ctx.busy);
  const clicked = ctx.pending || ""; // what was clicked, while the server answers: that control shows it is working
  const columnOf = new Map([
    ...(c.items || []).map((i) => [`item:${i.item_id}`, i.payload && (i.payload.column || (i.payload.step && i.payload.step.column))]),
    ...(c.questions || []).map((q) => [`question:${q.question_id}`, q.column]),
  ]);
  let index = 0;
  const body = cards(screen)
    .map((card) => {
      switch (card.type) {
        case "first_look":
          return firstLookHtml(card, names);
        case "change":
          return changeHtml(card, names, busy, { clicked, column: columnOf.get(`item:${card.item_id}`) || "" });
        case "question":
          return questionHtml(card, names, { busy, clicked, column: columnOf.get(`question:${card.question_id}`) || "", remembered: remembered.get(card.question_id) || null });
        case "doubt":
          return doubtHtml(card, names);
        case "request":
          return requestHtml(card, names, { busy, kitHref: ctx.kitHref ? ctx.kitHref(card.goal_ids || []) : "" });
        case "goals":
          return goalsHtml(card, names, busy, clicked);
        case "fit":
          return fitHtml(card, names);
        case "message":
          return messageHtml(card, names, { busy, clicked, openSent: ctx.openSent || new Set(), index: (index += 1) });
        case "ready":
          return readyHtml(card, { ...ctx, names });
        default:
          return ""; // a card this screen does not know yet (the server may send newer ones): left out, never broken
      }
    })
    .join("");
  const thinking = ctx.busy === "chat" ? agentRow(`<div class="ddc-bubble" data-dds-progress role="status"><span class="ddc-spin" aria-hidden="true"></span><span>${esc(progressText("chat"))}</span></div>`) : "";
  return `<section class="ddc-conv" aria-label="Conversation with Data Doctor" data-dds-conversation>${body}${thinking}${composerHtml(screen, ctx.composer || {})}</section>`;
}

/** The slim header: the goal (or "Your data") and the verdict, with the carried-on line when there is one. */
export function headHtml(screen, { level = 2 } = {}) {
  const { view } = screen;
  const v = VERDICTS[view.verdict.key] || VERDICTS.working;
  const title = view.goal_id ? `Your data for "${goalName(view.goal_id)}"` : "Your data";
  const line = view.carried && view.carried.line ? `<p class="dds-why" data-dds-new-line role="status">${esc(view.carried.line)}</p>` : "";
  return `<div class="ddc-head" data-dds-verdict="${esc(view.verdict.key)}"><h${level} class="dds-title">${esc(title)}</h${level}>${statusChip("health", v.status, { word: v.word })}<p class="dds-next" data-dds-next>${esc(
    view.verdict.next,
  )}</p>${line}</div>`;
}
