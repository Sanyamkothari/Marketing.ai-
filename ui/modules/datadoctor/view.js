// The Data Doctor screen's markup (Plan O M247, DEC-1817; the design §13): pure functions from the API's answer
// (`GET /cases/{case_id}/screen`: `{ case, timeline, view }`) to HTML strings. No DOM, no fetch, so the jsdom tests
// can draw every question, approval and doubt shape from a real answer.
//
// Every title, question, option, sentence, doubt and chat reply is a string the API wrote: escaped here and never
// reworded, and every number is one the API measured. A column or file name is the person's own text and is drawn
// inside `data-user-text` (`userText`). Technical names - codes, ids, states - are drawn only inside
// "Technical details" (`techDetails`), which "Show technical details" hides. An engine sentence that would use a
// word the glossary keeps from a marketer goes there too (`plainOrTechnical`).
//
// The parts, in the order the screen shows them (the design §13.2):
//   header      the goal (or "Your data"), the verdict chip, the files, the one next action
//   questions   the question, why it is asked, each answer with what it changes, "I don't know"
//   approvals   the exact sentence, why it needs its own yes, who must agree, "I agree to this sentence"
//   doubts      I am not sure because / Here is what I would do / Here is what changes if not
//   requests    what to ask the data team for, the file wanted, and the list to send them
//   earlier answers   (the plan itself, in sections, is `plan_view.js`)
//   approve     the sentence approved, word for word, and Approve
//   goals       with no goal yet: what this data can be used for, and "Choose this goal"
//   chat        the chat, "What the AI looked at" under each reply (`../agent/sent.js`, as built), or the way
//               to Settings › AI service when none is connected
//   timeline    what happened, newest last, with the receipt of each approval

import { button, icon, statusChip, tagChip } from "../../components.js";
import { esc, fmtStamp, techDetails } from "../../dom.js";
import { count, day } from "../../format.js";
import { findNeverShown, goalName, userText } from "../../labels.js";
import { AI_SETTINGS_HREF, AI_NOT_CONNECTED_SENTENCE } from "../../availability.js";
import { sentHtml, plain } from "../agent/sent.js";

/** The option every question offers, mapped to its safe choice (hide, leave as text, stop), never to a guess. */
export const DONT_KNOW = "I don't know";
/** The button of an APPROVE-class item (the design §13.2). */
export const AGREE = "I agree to this sentence";
/** The deliberate step before it: unticked, it keeps "I agree to this sentence" off. */
export const READ_IT = "I have read this sentence";
/** The control that offers an answered question's answers again. */
export const CHANGE_ANSWER = "Change answer";
export const REQUEST_CODES = Object.freeze(["DATA_REQUESTED", "DIFFERENT_FILE_REQUESTED"]);

/** The verdict in plain words, with the chip's tone (the status mapping of `components.js`, kind "health"). */
export const VERDICTS = Object.freeze({
  needs_files: { status: "needs_addon", word: "Waiting for your file" },
  working: { status: "not_tested", word: "Checking" },
  needs_answers: { status: "needs_attention", word: "Needs your answer" },
  needs_approval: { status: "needs_attention", word: "Needs your approval" },
  not_ready: { status: "failed", word: "Not ready yet" },
  ready: { status: "read", word: "Ready" },
  ready_with_warnings: { status: "read", word: "Ready, with notes" },
  choose_goal: { status: "needs_addon", word: "Choose a goal" },
});

/** What a timeline event says. Events not listed (the visit's own steps, the evidence it stored) are technical. */
export const EVENT_WORDS = Object.freeze({
  "case.created": "Check started",
  "file.added": "File added",
  "file.forgotten": "A file was removed",
  "item.proposed": "Change suggested",
  "item.decided": "Change ticked or unticked",
  "question.asked": "Question asked",
  "question.answered": "Question answered",
  "question.assumed": "Small choice decided for you",
  approval: "Approved",
  preview: "Changes tried on a sample",
  hand_off: "Handed on",
  replay: "Next file prepared the same way",
  "privacy.erasure": "Personal details removed",
  "goal.set": "Goal chosen",
  "chat.message": "Chat message",
  "prescription.saved": "Approved changes saved",
  "readiness.checked": "Data checked for the goal",
  "standing_answer.remembered": "Answer remembered",
  "standing_answer.applied": "Earlier answer used",
  "standing_answer.revoked": "Earlier answer revoked",
  "standing_answer.unfit": "Earlier answer no longer fits",
  "doubt.raised": "Said it is not sure",
  "data.requested": "More data asked for",
  "approval.confirmed": "Sentence agreed to",
});

const ROLE_WORDS = Object.freeze({ analyst: "an analyst on your team", approver: "an approver on your team" });

/** An id safe in an `id` attribute (and in `aria-describedby`'s list), from a question or item id. */
const domId = (prefix, id) => `${prefix}-${String(id).replace(/[^A-Za-z0-9_-]/g, "_")}`;

// --- small pieces ---------------------------------------------------------------------------------------------

const plural = (n, one, many) => `${count(n)} ${n === 1 ? one : many}`;

/** A server sentence, drawn as text; one that would use a word kept from a marketer goes to technical details. */
export function plainOrTechnical(text) {
  const shown = plain(text);
  return Object.keys(findNeverShown(shown)).length ? null : shown;
}

/** The active section: the shared base and the active goal's items and questions (as `active_section` reads it). */
export function activeSection(c) {
  const goal = (c.goals || []).find((g) => g.active);
  const inSection = (goalId) => !goalId || (goal && goal.goal_id === goalId);
  return {
    goal: goal ? goal.goal_id : null,
    items: (c.items || []).filter((i) => inSection(i.goal_id)),
    questions: (c.questions || []).filter((q) => inSection(q.goal_id)),
  };
}

/** The questions to show: open blocking first, then open, then answered; data requests are drawn on their own. */
export function questionsOf(c) {
  const { questions } = activeSection(c);
  const asked = questions.filter((q) => !REQUEST_CODES.includes(q.code));
  const rank = (q) => (q.answer === null || q.answer === undefined ? (q.blocking ? 0 : 1) : 2);
  return asked.slice().sort((a, b) => rank(a) - rank(b));
}

export const requestsOf = (c) => activeSection(c).questions.filter((q) => REQUEST_CODES.includes(q.code));

const isOpen = (q) => q.answer === null || q.answer === undefined;

/** The option "I don't know" stands for: the question's own "I don't know", else its safe choice; null for none. */
export function dontKnowOf(q) {
  const own = (q.options || []).find((o) => o.label === DONT_KNOW);
  if (own) return { option: own, own: true };
  const safe = (q.options || []).find((o) => o.safe);
  return safe ? { option: safe, own: false } : null;
}

function measured(option) {
  const parts = [];
  if (Number.isFinite(option.rows_changed)) {
    parts.push(option.rows_changed === 0 ? "Changes no rows" : `Changes ${plural(option.rows_changed, "row", "rows")}`);
  }
  if (Number.isFinite(option.label_rate)) parts.push(`The yes share becomes ${option.label_rate.toFixed(1)}%`);
  return parts.join(" · ");
}

export const userTextIn = (text, names) => {
  // A server sentence with the person's column and file names in it: the names are marked as their own text.
  let html = esc(plain(text));
  for (const name of names.filter(Boolean).sort((a, b) => b.length - a.length)) {
    const safe = esc(plain(name));
    if (safe && html.includes(safe)) html = html.split(safe).join(userText(plain(name)));
  }
  return html;
};

export const namesOf = (c) => [
  ...(c.files || []).map((f) => f.file_name),
  ...(c.questions || []).map((q) => q.column),
  ...(c.items || []).map((i) => i.payload && i.payload.column),
];

// --- the header -----------------------------------------------------------------------------------------------

export function headerHtml(screen, { level = 2, title = "" } = {}) {
  const { case: c, view } = screen;
  const v = VERDICTS[view.verdict.key] || VERDICTS.working;
  const heading = title || (view.goal_id ? `Your data for "${goalName(view.goal_id)}"` : "Your data");
  const files = (c.files || []).length
    ? `<p class="dds-files">${icon("data", { size: 16 })}<span>${plural(c.files.length, "file", "files")}: ${(c.files || [])
        .map((f) => userText(plain(f.file_name) + (f.sheet ? ` (${plain(f.sheet)})` : "")))
        .join(", ")}</span></p>`
    : "";
  const reasons = (view.verdict.reasons || [])
    .map(plainOrTechnical)
    .filter(Boolean)
    .map((r) => `<li>${esc(r)}</li>`)
    .join("");
  return `<div class="dds-head" data-dds-verdict="${esc(view.verdict.key)}"><div class="dds-head-main"><h${level} class="dds-title">${esc(
    heading,
  )}</h${level}>${files}</div><div class="dds-head-side">${statusChip("health", v.status, { word: v.word })}</div><p class="dds-next" data-dds-next>${esc(
    view.verdict.next,
  )}</p>${reasons ? `<ul class="dds-reasons" data-dds-reasons>${reasons}</ul>` : ""}${
    // "Ready, with notes" always shows one: the server's plain note when no warning's own sentence is plain.
    !reasons && view.verdict.note ? `<p class="dds-why" data-dds-note>${esc(view.verdict.note)}</p>` : ""
  }${READY.includes(view.verdict.key) ? leftOutHtml(screen) : ""}</div>`;
}

/**
 * The changes that need their own yes and are left out of what is used until someone agrees (pending and not
 * agreed), in the server's sentences: shown beside the verdict, Approve and continuing, so leaving one out is never
 * silent.
 */
const READY = Object.freeze(["ready", "ready_with_warnings"]);

export function leftOutOf(screen) {
  const { case: c, view } = screen;
  return (view.approve_class || []).filter((a) => !a.confirmed && !itemAccepted(c, a.item_id));
}

export function leftOutHtml(screen, { where = "head" } = {}) {
  const left = leftOutOf(screen);
  if (!left.length) return "";
  const names = namesOf(screen.case);
  return `<div class="dds-leftout" data-dds-left-out="${esc(where)}"><p class="dds-why"><span class="dds-label">Left out until someone agrees:</span></p><ul class="dds-reasons">${left
    .map((a) => `<li data-dds-left-out-item="${esc(a.item_id)}">${userTextIn(a.sentence, names)}</li>`)
    .join("")}</ul></div>`;
}

// --- questions ------------------------------------------------------------------------------------------------

export function questionHtml(
  q,
  c,
  { busy = false, standing = [], changing = false, remembered = null, deferred = false, extra = "" } = {},
) {
  const names = namesOf(c);
  const open = isOpen(q);
  const id = esc(q.question_id);
  const why = q.why ? `<p class="dds-why"><span class="dds-label">Why we ask:</span> ${userTextIn(q.why, names)}</p>` : "";
  const tag = q.blocking && open ? tagChip("Needed to go on") : "";
  const dk = dontKnowOf(q);
  const options = (q.options || [])
    .filter((o) => !(dk && dk.own && o.option_id === dk.option.option_id))
    .map((o) => {
      const chosen = q.answer === o.option_id;
      const effect = measured(o);
      return `<li class="dds-opt${chosen ? " is-chosen" : ""}">${button({
        label: o.label,
        kind: chosen ? "primary" : "secondary",
        attrs: `data-dds-answer="${esc(o.option_id)}" data-dds-q="${id}" aria-pressed="${chosen}"`,
        disabled: busy || chosen,
      })}${effect ? `<span class="dds-effect">${esc(effect)}</span>` : ""}${o.safe ? `<span class="dds-effect">The safe choice</span>` : ""}</li>`;
    })
    .join("");
  const dontKnow = dk
    ? `<li class="dds-opt">${button({
        label: DONT_KNOW,
        kind: "tertiary",
        attrs: `data-dds-dont-know data-dds-answer="${esc(dk.option.option_id)}" data-dds-q="${id}" aria-pressed="${q.answer === dk.option.option_id && dk.own}"`,
        disabled: busy || (dk.own && q.answer === dk.option.option_id),
      })}<span class="dds-effect">${esc(dk.own ? "Data Doctor takes the safe choice." : `Data Doctor takes the safe choice: ${dk.option.label}.`)}</span></li>`
    : "";
  let state = "";
  if (q.standing_answer_id) {
    const used = standing.find((s) => s.answer_id === q.standing_answer_id);
    state = `<p class="dds-state" data-dds-from-standing>${icon("clock", { size: 14 })}<span>${esc(
      used ? `Using your answer from ${day(used.answered_at)}.` : "Using your earlier answer.",
    )}</span></p>`;
  } else if (q.assumed) {
    const chose = (q.options || []).find((o) => o.option_id === q.answer);
    state = `<p class="dds-state" data-dds-assumed>${icon("info", { size: 14 })}<span>${esc(
      `Decided for you: ${chose ? chose.label : "the safe choice"}. The choice changes too little to ask. Pick another answer to change it.`,
    )}</span></p>`;
  } else if (!open) {
    const chose = (q.options || []).find((o) => o.option_id === q.answer);
    state = `<p class="dds-state" data-dds-answered>${icon("check", { size: 14 })}<span>${esc(
      `Your answer: ${chose ? chose.label : q.answer}${q.answered_at ? `, ${day(q.answered_at)}` : ""}.`,
    )}</span></p>`;
  }
  // An answer given here and remembered for the next files: said where it was given, with the same Revoke.
  let memory = "";
  if (remembered && !open) {
    memory = remembered.revoked
      ? `<p class="dds-state" data-dds-remembered="revoked">${icon("info", { size: 14 })}<span>Not remembered for next time. Data Doctor asks again next time.</span></p>`
      : `<div class="dds-remembered" data-dds-remembered="${esc(remembered.answer_id)}"><p class="dds-state">${icon("clock", { size: 14 })}<span>Remembered for next time, <span data-dds-remembered-date>${esc(
          day(remembered.answered_at),
        )}</span>.</span></p>${button({
          label: "Revoke",
          kind: "secondary",
          size: "sm",
          attrs: `data-dds-revoke="${esc(remembered.answer_id)}" aria-label="Revoke your answer from ${esc(day(remembered.answered_at))} for next time"`,
          disabled: busy,
        })}</div>`;
  }
  const optsId = domId("ddsopts", q.question_id);
  // An answered question can be changed: "Change answer" shows its answers again (the server replaces the answer).
  const answered = !open && !q.assumed;
  const change = answered
    ? `<div class="dds-actions">${button({
        label: CHANGE_ANSWER,
        kind: "tertiary",
        attrs: `data-dds-change="${id}" aria-expanded="${changing}"${changing ? ` aria-controls="${esc(optsId)}"` : ""}`,
        disabled: busy,
      })}</div>`
    : "";
  const showOptions = open || q.assumed || changing;
  // A question that can wait says what happens if it is left: its own safe choice, never a guess.
  const fallback = dk ? dk.option.label : "";
  const later =
    deferred && open
      ? `<p class="dds-state" data-dds-default>${icon("clock", { size: 14 })}<span>${esc(
          q.blocking ? "Asked once the ones above are answered." : "This can wait.",
        )} ${esc(fallback && fallback !== DONT_KNOW ? `The safe choice, if you don't know: ${fallback}.` : "The safe choice, if you don't know, is among its answers.")}</span></p>`
      : "";
  return `<article class="dds-card dds-q${open ? " is-open" : ""}" data-dds-question="${id}" data-dds-code="${esc(q.code)}" aria-labelledby="ddsq-${id}"><p class="dds-card-t" id="ddsq-${id}">${userTextIn(q.text, names)}</p>${tag ? `<p class="dds-tag-row">${tag}</p>` : ""}${why}${later}${state}${memory}${change}${
    showOptions ? `<ul class="dds-opts" id="${esc(optsId)}" aria-label="Answers">${options}${dontKnow}</ul>` : ""
  }${extra}</article>`;
}

// --- approvals (APPROVE-class) ----------------------------------------------------------------------------------

export function approvalHtml(entry, item, c, { busy = false, read = false, extra = "" } = {}) {
  const names = namesOf(c);
  const who = ROLE_WORDS[entry.role] || ROLE_WORDS.analyst;
  const whoText = entry.separate ? `${who}, other than whoever suggested or answered it` : who;
  const done = entry.confirmed;
  const pending = item && item.state !== "accepted";
  const sentenceId = domId("dds-sentence", entry.item_id);
  const readId = domId("dds-read", entry.item_id);
  let action;
  if (done) {
    action = `<p class="dds-state" data-dds-agreed>${icon("checkCircle", { size: 14 })}<span>Agreed. It is part of what you approve.</span></p>`;
  } else if (entry.can_agree === false) {
    // The server would refuse this reader: no button that can only fail, but who to ask.
    const ask =
      entry.cannot_agree === "separation"
        ? `You suggested or answered this, so ask ${who.replace(/^an? /, "another ")} to agree to this.`
        : `Ask ${who} to agree to this.`;
    action = `<p class="dds-state" data-dds-ask-other="${esc(entry.cannot_agree || "role")}">${icon("info", { size: 14 })}<span>${esc(ask)}</span></p>`;
  } else {
    // A deliberate step first: "I have read this sentence" turns "I agree to this sentence" on.
    action = `<label class="dds-tick dds-read"><input type="checkbox" id="${esc(readId)}" data-dds-read="${esc(entry.item_id)}"${read ? " checked" : ""}${
      busy ? " disabled" : ""
    }><span class="dds-item-body"><span class="dds-item-t">${esc(READ_IT)}</span></span></label><div class="dds-actions">${button({
      label: AGREE,
      kind: "primary",
      attrs: `data-dds-agree="${esc(entry.item_id)}" aria-describedby="${esc(sentenceId)}"`,
      disabled: busy || !read,
    })}</div>`;
  }
  return `<article class="dds-card dds-approval" data-dds-approval="${esc(entry.item_id)}" data-dds-role="${esc(entry.role)}"><p class="dds-card-t has-icon">${icon("alert", { size: 16 })}<span>Needs your yes on its own</span></p><blockquote class="dds-sentence" id="${esc(sentenceId)}" data-dds-sentence>${userTextIn(
    entry.sentence,
    names,
  )}</blockquote><p class="dds-why"><span class="dds-label">Why:</span> ${esc(entry.why.charAt(0).toUpperCase() + entry.why.slice(1))}.</p><p class="dds-why"><span class="dds-label">Needs a yes from</span> ${esc(
    whoText,
  )}.</p>${pending && !done ? `<p class="dds-why">Until someone agrees, this change is left out.</p>` : ""}${extra}${action}</article>`;
}

// --- doubts ---------------------------------------------------------------------------------------------------

export function doubtHtml(d, c) {
  const names = namesOf(c);
  const part = (key, label, text) =>
    `<p class="dds-doubt-part" data-dds-doubt-part="${key}"><span class="dds-label">${esc(label)}</span> ${userTextIn(
      text,
      names,
    )}</p>`;
  return `<article class="dds-card dds-doubt" data-dds-doubt="${esc(d.doubt_id)}"><p class="dds-card-t has-icon">${icon("info", { size: 16 })}<span>Data Doctor is not sure</span></p>${part("because", "I am not sure because", d.because)}${part(
    "would_do",
    "Here is what I would do:",
    d.would_do,
  )}${part("if_not", "Here is what changes if not:", d.if_not)}</article>`;
}

// --- requests for more data -----------------------------------------------------------------------------------

export function requestHtml(q, c, { kitHref = "", busy = false } = {}) {
  const names = namesOf(c);
  const id = esc(q.question_id);
  const chose = (q.options || []).find((o) => o.option_id === q.answer);
  const options = isOpen(q)
    ? `<div class="dds-actions">${(q.options || [])
        .map((o) =>
          button({
            label: o.label,
            kind: o.safe ? "tertiary" : "secondary",
            attrs: `data-dds-answer="${esc(o.option_id)}" data-dds-q="${id}"`,
            disabled: busy,
          }),
        )
        .join("")}</div>`
    : `<p class="dds-state" data-dds-answered>${icon("check", { size: 14 })}<span>${esc(`Your answer: ${chose ? chose.label : q.answer}.`)}</span></p>`;
  const kit = kitHref
    ? `<p class="dds-why"><a href="${esc(kitHref)}" data-dds-request-kit download>Download the list to send your data team</a></p>`
    : "";
  return `<article class="dds-card dds-request" data-dds-request="${id}"><p class="dds-card-t has-icon">${icon("inbox", { size: 16 })}<span>${userTextIn(q.text, names)}</span></p>${q.why ? `<p class="dds-why">${userTextIn(q.why, names)}</p>` : ""}${kit}${options}</article>`;
}

/** With no goal yet: what is missing for the goals this data cannot power yet, and the list to send. */
export function missingHtml(view, kitHref, c = {}) {
  const goals = view.missing_for || [];
  if (!goals.length) return "";
  const known = new Map(((c.diagnosis && c.diagnosis.use_cases) || []).map((u) => [u.use_case_id, u.name]));
  const names = goals.map((g) => goalName(g, known.get(g) || ""));
  return `<article class="dds-card dds-request" data-dds-missing><p class="dds-card-t has-icon">${icon("inbox", { size: 16 })}<span>${esc(
    `Some goals need more data: ${names.join(", ")}.`,
  )}</span></p><p class="dds-why"><a href="${esc(kitHref)}" data-dds-request-kit download>Download the list to send your data team</a></p></article>`;
}

// --- remembered answers ---------------------------------------------------------------------------------------

/** The answers remembered from before, used here: each with its date and Revoke (the design §7.2). */
export function standingHtml(view, { busy = false, level = 3 } = {}) {
  const list = view.standing || [];
  if (!list.length) return "";
  const rows = list
    .map((s) => {
      const when = `Your answer from <span data-dds-standing-date>${esc(day(s.answered_at))}</span>`;
      const what = `${esc(plain(s.option_label))}${s.column ? ` (${userText(plain(s.column))})` : ""}`;
      const action = s.revoked
        ? `<span class="dds-item-sub" data-dds-revoked>Revoked. Data Doctor asks again.</span>`
        : button({
            label: "Revoke",
            kind: "secondary",
            size: "sm",
            attrs: `data-dds-revoke="${esc(s.answer_id)}" aria-label="Revoke your answer from ${esc(day(s.answered_at))}"`,
            disabled: busy,
          });
      return `<li class="dds-standing" data-dds-standing="${esc(s.answer_id)}"><span class="dds-item-body"><span class="dds-item-t">${when}: ${what}</span></span>${action}</li>`;
    })
    .join("");
  return `<section class="dds-group" data-dds-group="standing" aria-labelledby="dds-g-standing"><h${level} id="dds-g-standing" class="dds-group-t">Using your earlier answers <span class="ds-count">${count(
    list.length,
  )}</span></h${level}><p class="dds-why">Each one is shown and can be revoked. A revoked answer is asked again.</p><ul class="dds-items">${rows}</ul></section>`;
}

// --- Approve --------------------------------------------------------------------------------------------------

export function approveHtml(screen, { busy = false, level = 3, gate = null } = {}) {
  const { view } = screen;
  if (!view.approval_sentence) return "";
  const waiting = (view.approve_class || []).filter((a) => !a.confirmed && itemAccepted(screen.case, a.item_id)).length;
  const left = leftOutOf(screen).length;
  // The section-by-section check (`plan.js` `readyToApprove`): one final yes, once each open section was looked at.
  const unchecked = gate ? gate.unchecked : [];
  const stops = gate ? gate.stops : [];
  const hold = [
    unchecked.length
      ? `Check each section above first: ${unchecked.map((s) => s.title).join(", ")}.`
      : "",
    stops.some((f) => f.items.length) ? "A step stops the plan. Untick it, park it or fix it first." : "",
    stops.some((f) => !f.items.length) ? "A safety check stops the whole plan. It cannot be approved as it is." : "",
  ].filter(Boolean);
  return `<section class="dds-approve card" data-dds-approve aria-labelledby="dds-approve-t"><h${level} id="dds-approve-t" class="dds-group-t">Approve the changes</h${level}><p class="dds-why">One final yes, after you have checked each section. You approve this sentence, word for word. Your files are not changed.</p><blockquote class="dds-sentence" id="dds-approve-sentence" data-dds-approve-sentence>${esc(
    view.approval_sentence,
  )}</blockquote>${waiting ? `<p class="dds-why">${esc(`First give your own yes to ${plural(waiting, "change", "changes")} above.`)}</p>` : ""}${hold
    .map((h) => `<p class="dds-why" data-dds-approve-hold>${esc(h)}</p>`)
    .join("")}${leftOutHtml(screen, { where: "approve" })}${
    left ? `<p class="dds-why" data-dds-left-out-choice>${esc(`After you approve, you choose whether to continue without ${left === 1 ? "it" : "them"}.`)}</p>` : ""
  }<div class="dds-actions">${button({
    label: "Approve",
    kind: "primary",
    attrs: `data-dds-approve-button aria-describedby="dds-approve-sentence"`,
    disabled: busy || waiting > 0 || hold.length > 0,
    busy: busy === "approve",
  })}</div></section>`;
}

const itemAccepted = (c, itemId) => (c.items || []).some((i) => i.item_id === itemId && i.state === "accepted");

// --- goals ----------------------------------------------------------------------------------------------------

/** With no goal yet: the goals, best fit first, and "Choose this goal". The only verdict is the readiness check's, once a goal is chosen. */
export function goalsHtml(screen, { busy = false, level = 3 } = {}) {
  const { case: c, view } = screen;
  if (view.mode !== "generic") return "";
  const found = ((c.diagnosis && c.diagnosis.use_cases) || []).filter((u) => u && u.use_case_id);
  if (!found.length) return "";
  const order = { ready: 0, check: 1, too_small: 2, missing: 3 };
  const rows = found
    .slice()
    .sort((a, b) => (order[a.status] ?? 9) - (order[b.status] ?? 9))
    .map((u, index) => {
      const name = goalName(u.use_case_id, u.name);
      // The server's own words: why, else how many customers and how many already did it (left out when not known).
      const counts = [
        Number.isFinite(u.customers) ? `${count(u.customers)} customers` : "",
        Number.isFinite(u.yes_count) ? `${count(u.yes_count)} already did it` : "",
      ].filter(Boolean);
      // The server's why already carries the counts; the separate counts line is only for a goal with no why.
      const notes = u.why ? [esc(plain(u.why))] : counts.length ? [esc(counts.join(", "))] : [];
      // A goal to check is chosen first; its question is asked then, so only a goal with a real gap shows a first fix.
      const best = index === 0 && ["ready", "check"].includes(u.status);
      const fix = u.status !== "ready" && u.status !== "check" && u.first_fix ? `<span class="dds-why" data-dds-goal-fix>To do first: ${esc(plain(u.first_fix))}</span>` : "";
      return `<li class="dds-goal" data-dds-goal-row="${esc(u.use_case_id)}"><span class="dds-item-body"><span class="dds-item-t">${esc(name)}${
        best ? ` <span class="chip neutral" data-dds-goal-best>Best fit</span>` : ""
      }</span>${notes.map((n) => `<span class="dds-why" data-dds-goal-why>${n}</span>`).join("")}${fix}</span>${button({
        label: "Choose this goal",
        kind: best ? "primary" : "secondary",
        attrs: `data-dds-goal="${esc(u.use_case_id)}" aria-label="Choose the goal: ${esc(name)}"`,
        disabled: busy,
      })}</li>`;
    })
    .join("");
  return `<section class="dds-group" data-dds-goals aria-labelledby="dds-g-goals"><h${level} id="dds-g-goals" class="dds-group-t">What this data can be used for</h${level}><p class="dds-why">Best fit first. Choose a goal and Data Doctor checks whether your data is ready for it. Nothing you answered is asked again.</p><ul class="dds-items">${rows}</ul></section>`;
}

// --- adding a file --------------------------------------------------------------------------------------------

export function uploadHtml({ accept = "", busy = "", first = true, quiet = false, pick = false } = {}) {
  const label = first ? "Choose a file" : "Add another file";
  const text = quiet
    ? ""
    : first
    ? "Choose the file of your customers. Data Doctor reads it, asks what it cannot tell, and never changes your file."
    : "Add a file with the data asked for. It joins this check, and nothing you answered is asked again.";
  return `<div class="dds-upload" data-dds-upload>${text ? `<p class="dds-why">${esc(text)}</p>` : ""}<label class="btn ${first ? "primary" : "secondary"} dds-file-label" for="dds-file">${icon(
    "upload",
  )}<span>${esc(label)}</span></label><input type="file" id="dds-file" class="dds-file" accept="${esc(accept)}"${busy ? " disabled" : ""}>${
    pick && !busy
      ? ` ${button({ label: "Pick from a connection", kind: "secondary", attrs: "data-dds-pick-connection" })}`
      : ""
  }${busy ? `<p class="loading" role="status" data-dds-busy>${esc(busy)}</p>` : ""}</div>`;
}

// --- the chat -------------------------------------------------------------------------------------------------

/**
 * The chat. `ai` is the AI service's state for Data Doctor: true (connected), false (none connected: the way to
 * Settings › AI service, never Connections) or null (not known: the input is offered and the server answers).
 */
export const AI_CONSENT_LINE = "The Doctor is working with its rules only. Its AI has not been allowed to read this client's data.";
export const AI_CONSENT_BUTTON = "Let the Doctor's AI read masked summaries of this client's data";
export const AI_SPENT_LINE = "The AI has used up what this check may spend on it. The Doctor goes on with its rules only.";

export function chatHtml(
  screen,
  { ai = null, draft = "", busy = false, canConnect = true, openSent = new Set(), level = 3, use = null, canConsent = true } = {},
) {
  const entries = (screen.case.chat || [])
    .map((m, i) =>
      m.role === "user"
        ? `<div class="gmsg user" data-dds-msg="user">${esc(plain(m.text))}</div>`
        : `<div class="gmsg bot" data-dds-msg="agent">${esc(plain(m.text))}${sentHtml(m.sent, {
            id: i,
            open: openSent.has(String(i)),
          })}</div>`,
    )
    .join("");
  const head = `<h${level} class="dds-group-t" id="dds-chat-t">Ask Data Doctor</h${level}>`;
  if (ai === false) {
    const rest = canConnect ? "Connect one in Settings › AI service." : "Ask an admin to connect one in Settings › AI service.";
    const go = canConnect
      ? `<div class="dds-actions">${button({
          label: "Open Settings › AI service",
          href: AI_SETTINGS_HREF,
          kind: "secondary",
          attrs: "data-ai-settings-link",
        })}</div>`
      : "";
    return `<section class="dds-chat card" data-dds-chat aria-labelledby="dds-chat-t">${head}<div class="gchat">${entries}</div><div class="dds-nochat" role="status"><p data-ai-not-connected>${esc(
      `${AI_NOT_CONNECTED_SENTENCE} ${rest}`,
    )}</p>${go}<p class="dds-why">Everything else on this screen works without it.</p></div></section>`;
  }
  if (use && !use.consent) {
    const give = canConsent
      ? `<div class="dds-actions">${button({ label: AI_CONSENT_BUTTON, kind: "secondary", attrs: "data-dds-ai-consent", disabled: busy })}</div>`
      : `<p class="dds-why">Ask someone who can change this check to allow it.</p>`;
    return `<section class="dds-chat card" data-dds-chat aria-labelledby="dds-chat-t">${head}<div class="gchat">${entries}</div><div class="dds-nochat" role="status"><p data-ai-no-consent>${esc(
      AI_CONSENT_LINE,
    )}</p>${give}</div></section>`;
  }
  if (use && use.spent) {
    return `<section class="dds-chat card" data-dds-chat aria-labelledby="dds-chat-t">${head}<div class="gchat">${entries}</div><div class="dds-nochat" role="status"><p data-ai-spent>${esc(
      AI_SPENT_LINE,
    )}</p></div></section>`;
  }
  const empty = entries
    ? ""
    : `<p class="dds-why">Ask why a change is suggested, or ask for a change. A change the AI suggests appears in the list, never ticked for you.</p>`;
  return `<section class="dds-chat card" data-dds-chat aria-labelledby="dds-chat-t">${head}<div class="gchat">${entries}${
    busy ? `<div class="gmsg bot"><span class="loading">Thinking…</span></div>` : ""
  }</div>${empty}<form id="dds-chat-form" class="dds-chat-form"><label class="sr" for="dds-chat-input">Ask Data Doctor</label><input type="text" id="dds-chat-input" class="input" autocomplete="off" maxlength="1000" placeholder="Ask about your data…" value="${esc(
    draft,
  )}"${busy ? " disabled" : ""}>${button({ label: "Send", kind: "secondary", type: "submit", disabled: busy, attrs: "data-dds-send" })}</form></section>`;
}

// --- the timeline ---------------------------------------------------------------------------------------------

export function timelineHtml(screen, { level = 3 } = {}) {
  const events = (screen.timeline || []).filter((e) => Object.hasOwn(EVENT_WORDS, e.kind));
  if (!events.length) return "";
  const rows = events
    .map((e) => {
      const d = e.details || {};
      let extra = "";
      if (e.kind === "goal.set" && e.item_id) extra = `: ${goalName(e.item_id)}`;
      if (e.kind === "file.added" && Number.isFinite(d.rows)) extra = `: ${plural(d.rows, "row", "rows")}, ${plural(d.columns || 0, "column", "columns")}`;
      if (e.kind === "preview" && Number.isFinite(d.rows_tried)) extra = ` of ${plural(d.rows_tried, "row", "rows")}`;
      if (e.kind === "readiness.checked" && d.verdict) extra = `: ${(VERDICTS[d.verdict] || { word: "checked" }).word.toLowerCase()}`;
      const receipt =
        (e.kind === "approval" || e.kind === "approval.confirmed") && (d.approval_id || e.item_id)
          ? ` <span class="dds-receipt" data-dds-receipt>${esc(`Receipt ${d.approval_id || e.item_id}`)}</span>`
          : "";
      // An approval the visit carried on by itself (the plan came back with nothing new that asks a person).
      const carried = e.kind === "approval" && d.carried === true;
      const said = carried
        ? `Carried on by itself: ${d.new > 0 ? `${plural(d.new, "new sure fix", "new sure fixes")} (the rules found them; nothing to check)` : "nothing new to check"}`
        : EVENT_WORDS[e.kind] + extra;
      return `<li data-dds-event="${esc(e.kind)}"><time datetime="${esc(e.at)}">${esc(fmtStamp(e.at))}</time><span>${esc(
        said,
      )}${receipt}</span></li>`;
    })
    .join("");
  return `<details class="dds-timeline" data-dds-timeline><summary>${esc(`What happened so far (${events.length})`)}</summary><h${level} class="sr">What happened so far</h${level}><ol class="dds-events">${rows}</ol></details>`;
}

// --- technical details ----------------------------------------------------------------------------------------

export function technicalHtml(screen) {
  const { case: c, view } = screen;
  const codes = activeSection(c)
    .questions.map((q) => q.code)
    .filter(Boolean);
  const hidden = (view.verdict.reasons || []).filter((r) => !plainOrTechnical(r));
  return techDetails([
    ["Case", c.case_id],
    ["Version", String(c.version)],
    ["Step", c.state],
    ["Verdict", view.verdict.key],
    ["Reason", view.verdict.technical || ""],
    ["Question codes", [...new Set(codes)].join(", ")],
    ["Also found", hidden.join(" | ")],
  ]);
}
