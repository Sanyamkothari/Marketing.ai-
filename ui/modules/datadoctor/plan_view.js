// The plan, evidence first (markup only; `plan.js` works the numbers out). From the top of the screen down:
//
//   funnel      rows and customers in -> out, the yes share before and after, and the biggest losses
//   checks      "3 things to check", ranked by risk, led by counts and sample rows - never by the AI's words
//   queue       at most three blocking questions at once; the rest wait, each with the default stated
//   plan        five to seven sections, one plain sentence and one count each; only what needs a yes is open,
//               sure fixes are folded away (and can still be unticked); a step shows what our code counted for it
//   who         "I don't know" -> "Who would know?", a message ready to copy, and a step parked until they answer
//
// Every number drawn here was counted by our code. A query is drawn only behind "Show the query".

import { button, icon } from "../../components.js";
import { esc } from "../../dom.js";
import { count } from "../../format.js";
import { userText } from "../../labels.js";
import { plain } from "../agent/sent.js";
import {
  BLOCKING_AT_ONCE,
  HIDDEN,
  alertOf,
  columnsOfRows,
  findingsOf,
  isChecked,
  kindOf,
  leadOf,
  ledgerOf,
  maskCell,
  notesOf,
  planOf,
  queueOf,
  readyToApprove,
  receiptOf,
  signatureOf,
  withinBudget,
} from "./plan.js";
import { WHO, messageFor, whoOf } from "./who.js";
import { activeSection, approvalHtml, dontKnowOf, namesOf, questionHtml, questionsOf, userTextIn } from "./view.js";

const plural = (n, one, many) => `${count(n)} ${n === 1 ? one : many}`;
const domId = (prefix, id) => `${prefix}-${String(id).replace(/[^A-Za-z0-9_-]/g, "_")}`;

// --- alert levels -----------------------------------------------------------------------------------------------

const LEVEL_LOOK = Object.freeze({
  stop: { tone: "critical", icon: "critical", word: "Stop" },
  flag: { tone: "warning", icon: "alert", word: "Check" },
  note: { tone: "info", icon: "info", word: "Note" },
});

/** The three alert levels, each with its own colour, icon and word: a stop, a flag (check) or a note. */
export function levelChip(level) {
  const look = LEVEL_LOOK[level] || LEVEL_LOOK.note;
  return `<span class="badge tone-${look.tone}" data-dds-level="${esc(level)}">${icon(look.icon, { size: 12 })}<span>${look.word}</span></span>`;
}

// --- sample rows, personal data hidden --------------------------------------------------------------------------

/** A few rows as a small table; a personal column or a cell that looks personal is drawn as "hidden". */
export function samplesHtml(rows, { personal = [], caption = "" } = {}) {
  if (!rows.length) return "";
  const cols = columnsOfRows(rows);
  const head = cols.map((c) => `<th scope="col">${userText(c)}</th>`).join("");
  const body = rows
    .map(
      (r) =>
        `<tr>${cols
          .map((c) => {
            const cell = maskCell(c, r[c], personal);
            return cell === HIDDEN ? `<td class="dds-hidden-cell">${HIDDEN}</td>` : `<td>${userText(cell)}</td>`;
          })
          .join("")}</tr>`,
    )
    .join("");
  return `<table class="dds-sample" data-dds-sample>${caption ? `<caption>${esc(caption)}</caption>` : ""}<thead><tr>${head}</tr></thead><tbody>${body}</tbody></table>`;
}

// --- the funnel -------------------------------------------------------------------------------------------------

const finite = (v) => Number.isFinite(v);

/**
 * What the funnel draws: the ledger's totals, and each step's own losses; null for nothing. With no ledger only the
 * steps' losses are drawn: the steps read different files (a query is tried on the first rows), so their counts are
 * never chained into a whole-plan total here.
 */
export function funnelOf(items, ledger) {
  const live = items.filter((i) => i.state !== "rejected");
  const steps = live
    .map((item) => ({ item, r: receiptOf(item, ledger) }))
    .filter(({ r }) => r && finite(r.rowsIn) && finite(r.rowsOut));
  const out = {
    rowsIn: ledger && finite(ledger.rows_in) ? ledger.rows_in : null,
    rowsOut: ledger && finite(ledger.rows_out) ? ledger.rows_out : null,
    customersIn: ledger && finite(ledger.customers_in) ? ledger.customers_in : null,
    customersOut: ledger && finite(ledger.customers_out) ? ledger.customers_out : null,
    sampledFrom: ledger && finite(ledger.sampled_from) ? ledger.sampled_from : null,
    rateIn: ledger && finite(ledger.positive_rate_in) ? ledger.positive_rate_in : null,
    rateOut: ledger && finite(ledger.positive_rate_out) ? ledger.positive_rate_out : null,
    losses: steps
      .filter(({ r }) => r.rowsIn > r.rowsOut)
      .map(({ item, r }) => ({ item, lost: r.rowsIn - r.rowsOut }))
      .sort((a, b) => b.lost - a.lost)
      .slice(0, 3),
  };
  const any = [out.rowsIn, out.rowsOut, out.customersIn, out.customersOut, out.rateIn, out.rateOut].some(finite);
  return any || out.losses.length ? out : null;
}

function barRow(label, inN, outN, kind) {
  if (!finite(inN) || !finite(outN)) return "";
  const share = inN > 0 ? Math.max(0, Math.min(100, (outN / inN) * 100)) : 0;
  const fewer = inN - outN;
  const words = `${count(inN)} in, ${count(outN)} out${
    fewer > 0 ? ` (${count(fewer)} fewer, ${inN > 0 ? ((fewer / inN) * 100).toFixed(1) : "0.0"}%)` : fewer < 0 ? ` (${count(-fewer)} more)` : ""
  }`;
  return `<li class="dds-funnel-row${inN > 0 && fewer / inN > 0.05 ? " is-flag" : ""}" data-dds-funnel-row="${kind}"><span class="dds-funnel-label">${esc(label)}</span><span class="dds-funnel-bar" aria-hidden="true"><span class="dds-funnel-in"></span><span class="dds-funnel-out" style="width:${share.toFixed(1)}%"></span></span><span class="dds-funnel-num">${esc(words)}</span></li>`;
}

export function funnelHtml(items, ledger, { level = 3 } = {}) {
  const f = funnelOf(items, ledger);
  if (!f) return "";
  const rate =
    finite(f.rateIn) && finite(f.rateOut)
      ? `<li class="dds-funnel-row${Math.abs(f.rateOut - f.rateIn) > 1 ? " is-flag" : ""}" data-dds-funnel-row="rate"><span class="dds-funnel-label">Yes outcomes</span><span class="dds-funnel-num">${esc(
          `${f.rateIn.toFixed(1)}% before, ${f.rateOut.toFixed(1)}% after`,
        )}</span></li>`
      : "";
  // The counts are from the trial run on the first rows; say so, so 1,000 next to a 4,000-row file is not a puzzle.
  const sample =
    finite(f.sampledFrom) && finite(f.rowsIn) && f.sampledFrom > f.rowsIn
      ? `<p class="dds-why" data-dds-sampled>Counted on a trial of the first ${esc(count(f.rowsIn))} of ${esc(count(f.sampledFrom))} rows. Approving prepares all of them.</p>`
      : "";
  const losses = f.losses.length
    ? `<div class="dds-funnel-losses" data-dds-losses><span class="dds-label">Biggest losses:</span> ${f.losses
        .map(
          ({ item, lost }) =>
            `<button type="button" class="btn tertiary sm" data-dds-goto="${esc(item.item_id)}"><span>${esc(
              `${item.source === "rules" ? plain(item.title || kindOf(item)) : "A step the AI suggested"}: ${count(lost)} rows`,
            )}</span></button>`,
        )
        .join("")}</div>`
    : "";
  return `<section class="dds-funnel card" data-dds-funnel aria-labelledby="dds-funnel-t"><h${level} id="dds-funnel-t" class="dds-group-t">From your file to the prepared data</h${level}><ul class="dds-funnel-rows">${barRow(
    "Rows",
    f.rowsIn,
    f.rowsOut,
    "rows",
  )}${barRow("Customers", f.customersIn, f.customersOut, "customers")}${rate}</ul>${sample}${losses}</section>`;
}

// --- "3 things to check" ------------------------------------------------------------------------------------------

function findingHtml(f, ledger, items) {
  const cols = f.columns && f.columns.length ? ` <span class="dds-check-cols">${f.columns.map((c) => userText(c)).join(", ")}</span>` : "";
  const personal = [];
  for (const id of f.items || []) {
    const item = items.find((i) => i.item_id === id);
    const r = item && receiptOf(item, ledger);
    if (r) personal.push(...r.personal);
  }
  const goto = f.items && f.items.length ? `<button type="button" class="btn tertiary sm" data-dds-goto="${esc(f.items[0])}"><span>Go to it</span></button>` : "";
  const ack =
    f.level === "stop" && f.key
      ? `<button type="button" class="btn secondary sm" data-dds-ack="${esc(f.key)}"><span>Yes, go on anyway</span></button>`
      : "";
  return `<li class="dds-check" data-dds-check data-dds-level="${esc(f.level)}" data-dds-check-kind="${esc(f.kind)}">${levelChip(f.level)}<span class="dds-check-lead">${esc(
    leadOf(f),
  )}</span>${cols}${samplesHtml(f.samples || [], { personal })}${goto}${ack}</li>`;
}

export function checksHtml(findings, { used = 0, ledger = null, items = [], level = 3 } = {}) {
  if (!findings.length) return "";
  const { asking, folded } = withinBudget(findings, used);
  // Three at the top, but a stop is never folded away under the notes.
  const shown = Math.max(3, asking.filter((f) => f.level === "stop").length);
  const top = asking.slice(0, shown);
  const more = [...asking.slice(shown), ...folded];
  const title = top.length ? `${top.length} ${top.length === 1 ? "thing" : "things"} to check` : "Nothing urgent to check";
  const rest = more.length
    ? `<details class="dds-more-notes" data-dds-more-notes data-dds-keep="more-notes"><summary>${esc(plural(more.length, "more note", "more notes"))}</summary><ul class="dds-check-list">${more
        .map((f) => findingHtml(f, ledger, items))
        .join("")}</ul></details>`
    : "";
  return `<section class="dds-checks card" data-dds-checks aria-labelledby="dds-checks-t"><h${level} id="dds-checks-t" class="dds-group-t">${esc(
    title,
  )}</h${level}><p class="dds-why">Counted by our code, riskiest first.</p><ul class="dds-check-list">${top
    .map((f) => findingHtml(f, ledger, items))
    .join("")}</ul>${rest}</section>`;
}

// --- a step's receipt -------------------------------------------------------------------------------------------

function queryOf(item) {
  const p = item.payload || {};
  const sql = typeof p.detail === "string" ? p.detail : p.step && p.step.params && typeof p.step.params.sql === "string" ? p.step.params.sql : "";
  return kindOf(item) === "sql_query" ? sql : "";
}

const pair = (label, a, b) => (finite(a) && finite(b) ? `<div><dt>${esc(label)}</dt><dd>${esc(`${count(a)} in, ${count(b)} out`)}</dd></div>` : "");

/** What our code counted for a step, the sample rows before and after, and the query only behind "Show the query". */
export function receiptHtml(item, ledger) {
  const r = receiptOf(item, ledger);
  const sql = queryOf(item);
  if (!r && !sql) return "";
  let box = "";
  if (r) {
    const level = alertOf(item, ledger);
    const lines = [
      pair("Rows", r.rowsIn, r.rowsOut),
      pair("Customers", r.customersIn, r.customersOut),
      // Lost and counted-twice are told apart and never netted off against each other.
      finite(r.lost) ? `<div data-dds-lost><dt>Customers lost</dt><dd>${esc(count(r.lost))}</dd></div>` : "",
      finite(r.duplicated) ? `<div data-dds-duplicated><dt>Customers counted twice</dt><dd>${esc(count(r.duplicated))}</dd></div>` : "",
      ...r.totals.map(
        (t) => `<div data-dds-total><dt>${userText(t.column)}</dt><dd>${esc(`${t.before === null || t.before === undefined ? "none" : t.before} before, ${t.after === null || t.after === undefined ? "none" : t.after} after`)}</dd></div>`,
      ),
    ].join("");
    box = `<div class="dds-receipt-box" data-dds-step-receipt="${esc(item.item_id)}"><p class="dds-why"><span class="dds-label">${
      r.counted ? "Counted by our code" : "Rows counted"
    }</span> ${level ? levelChip(level) : ""}</p>${lines ? `<dl class="dds-counts">${lines}</dl>` : ""}${
      r.before.length || r.after.length
        ? `<div class="dds-samples">${samplesHtml(r.before, { personal: r.personal, caption: "Before" })}${samplesHtml(r.after, { personal: r.personal, caption: "After" })}</div><p class="dds-why">Personal details are hidden.</p>`
        : ""
    }</div>`;
  }
  const query = sql
    ? `<details class="dds-query" data-dds-query><summary>Show the query</summary>${userText(sql, "pre")}</details>`
    : "";
  return `${box}${query}`;
}

// --- "Who would know?" ------------------------------------------------------------------------------------------

/**
 * The panel "I don't know" opens. `target`: `{ key, role, what, why, meanwhile, item?: id, safe?: { qid, option, label } }`.
 * A step is parked for the chosen person; a question takes its safe choice for now. The message is ready to copy.
 */
export function whoHtml(target, { busy = false } = {}) {
  const picks = WHO.map(
    (w) =>
      `<li class="dds-opt">${button({
        label: w.label,
        kind: target.role === w.id ? "primary" : "secondary",
        attrs: `data-dds-who-pick="${w.id}" data-dds-who-for="${esc(target.key)}" aria-pressed="${target.role === w.id}"`,
        disabled: busy,
      })}</li>`,
  ).join("");
  const who = whoOf(target.role);
  const messageId = domId("ddswhomsg", target.key);
  const message = who
    ? `<label class="dds-label" for="${esc(messageId)}">A message you can copy and send to ${esc(who.label)}</label><textarea id="${esc(messageId)}" class="input dds-who-message" rows="7" readonly data-dds-who-message>${esc(
        messageFor(target.role, target),
      )}</textarea>`
    : `<p class="dds-why">Pick who would know and a message is ready for you to copy.</p>`;
  const act = target.item
    ? button({
        label: who ? `Park this step: waiting for ${who.label}` : "Park this step",
        kind: "primary",
        attrs: `data-dds-park="${esc(target.item)}" data-dds-park-role="${esc(target.role || "")}"`,
        disabled: busy || !who,
      })
    : button({
        label: `Use the safe choice for now${target.safe.label ? `: ${target.safe.label}` : ""}`,
        kind: "secondary",
        attrs: `data-dds-answer="${esc(target.safe.option)}" data-dds-q="${esc(target.safe.qid)}"`,
        disabled: busy,
      });
  const note = target.item
    ? `Parked, the step is left out until ${who ? who.label : "they"} answer${who ? "s" : ""}. You can resume it at any time.`
    : "Data Doctor takes the safe choice meanwhile. Change the answer when you hear back.";
  return `<div class="dds-who" data-dds-who="${esc(target.key)}"><p class="dds-card-t">Who would know?</p><ul class="dds-opts" aria-label="Who would know">${picks}</ul>${message}<p class="dds-why">${esc(
    note,
  )}</p><div class="dds-actions">${act}${who ? button({ label: "Copy message", kind: "secondary", attrs: `data-dds-copy="${esc(target.key)}"`, disabled: busy }) : ""}${button({
    label: "Close",
    kind: "tertiary",
    attrs: `data-dds-who-close="${esc(target.key)}"`,
  })}</div></div>`;
}

const keyOf = (kind, id) => `${kind}:${id}`;

/** What a question's panel says: its own words, why it is asked, and the safe choice taken meanwhile. */
function questionTarget(q, role) {
  const dk = dontKnowOf(q);
  const label = dk && !dk.own ? dk.option.label : "";
  return {
    key: keyOf("q", q.question_id),
    role,
    what: plain(q.text),
    why: plain(q.why || ""),
    meanwhile: label ? `Data Doctor takes the safe choice: ${label}.` : "Data Doctor takes the safe choice.",
    safe: dk ? { qid: q.question_id, option: dk.option.option_id, label } : null,
  };
}

// --- the question queue -----------------------------------------------------------------------------------------

export function queueHtml(c, view, ctx) {
  const { now, later, done } = queueOf(questionsOf(c));
  const standing = view.standing || [];
  const remembered = new Map((view.remembered || []).map((r) => [r.question_id, r]));
  const draw = (q, extra = {}) => {
    const key = keyOf("q", q.question_id);
    const open = ctx.knowing.has(key);
    const target = open && dontKnowOf(q) ? questionTarget(q, ctx.knowing.get(key)) : null;
    return questionHtml(q, c, {
      busy: ctx.busy,
      standing,
      changing: ctx.changing.has(q.question_id),
      remembered: remembered.get(q.question_id) || null,
      extra: target && target.safe ? whoHtml(target, { busy: ctx.busy }) : "",
      ...extra,
    });
  };
  const waiting = later.length
    ? `<details class="dds-later" data-dds-later data-dds-keep="later"><summary>${esc(
        `${plural(later.length, "more question", "more questions")} can wait`,
      )}</summary>${later.map((q) => draw(q, { deferred: true })).join("")}</details>`
    : "";
  return {
    html: `${now.map((q) => draw(q)).join("")}${waiting}${done.map((q) => draw(q)).join("")}`,
    asking: now.length,
    limit: BLOCKING_AT_ONCE,
  };
}

// --- the plan, section by section ---------------------------------------------------------------------------------

function sourceNote(item) {
  if (item.source === "ai") return "Suggested by the AI. Never ticked for you.";
  if (item.source === "person") return "Your edit.";
  return "";
}

function itemHtml(item, c, ctx, { open }) {
  const names = namesOf(c);
  const id = esc(item.item_id);
  const on = item.state === "accepted";
  const sure = item.confidence === "sure" ? "Sure" : "Please check";
  const assumption = item.assumption
    ? `<span class="dds-item-sub" data-dds-assumption>${esc(`Assumed: ${plain(item.assumption)}`)}</span>`
    : "";
  const note = sourceNote(item);
  const level = alertOf(item, ctx.ledger);
  const key = keyOf("i", item.item_id);
  const dk =
    open && !ctx.busy
      ? button({ label: "I don't know", kind: "tertiary", size: "sm", attrs: `data-dds-item-dk="${id}"`, disabled: ctx.busy })
      : "";
  const panel = ctx.knowing.has(key)
    ? whoHtml(
        {
          key,
          role: ctx.knowing.get(key),
          what: plain(item.title || item.kind),
          why: "",
          meanwhile: "this step is left out.",
          item: item.item_id,
        },
        { busy: ctx.busy },
      )
    : "";
  return `<li class="dds-item" data-dds-item="${id}"${level ? ` data-dds-level="${esc(level)}"` : ""}><label class="dds-tick"><input type="checkbox" data-dds-tick="${id}"${on ? " checked" : ""}${
    ctx.busy ? " disabled" : ""
  }><span class="dds-item-body"><span class="dds-item-t">${userTextIn(item.title || item.kind, names)}</span><span class="dds-item-sub">${esc(
    sure,
  )}${note ? ` · ${esc(note)}` : ""}</span>${assumption}</span>${level ? levelChip(level) : ""}</label>${receiptHtml(item, ctx.ledger)}${dk}${panel}</li>`;
}

function parkedHtml(item, c, ctx) {
  const who = whoOf(item.parked_for);
  const names = namesOf(c);
  return `<li class="dds-item dds-parked" data-dds-parked="${esc(item.item_id)}"><span class="dds-item-body"><span class="dds-item-t">${userTextIn(
    item.title || item.kind,
    names,
  )}</span><span class="dds-item-sub">${esc(`Waiting for ${who ? who.label : "someone who would know"}. Left out until then.`)}</span></span>${button({
    label: "Resume",
    kind: "secondary",
    size: "sm",
    attrs: `data-dds-resume="${esc(item.item_id)}"`,
    disabled: ctx.busy,
  })}</li>`;
}

function sectionHtml(sec, screen, ctx, level) {
  const { case: c, view } = screen;
  const entries = new Map((view.approve_class || []).map((a) => [a.item_id, a]));
  const approvals = sec.open.filter((i) => entries.has(i.item_id));
  const ticks = sec.open.filter((i) => !entries.has(i.item_id));
  const lines = [
    plural(sec.items.length, "change", "changes"),
    sec.pending.length ? `${sec.pending.length} ${sec.pending.length === 1 ? "needs" : "need"} your yes` : "",
    sec.waiting.length ? `${sec.waiting.length} waiting for someone` : "",
  ].filter(Boolean);
  const approvalCards = approvals
    .map((i) => {
      const entry = entries.get(i.item_id);
      const key = keyOf("i", i.item_id);
      const dk =
        !entry.confirmed && !ctx.busy
          ? `<div class="dds-actions">${button({ label: "I don't know", kind: "tertiary", size: "sm", attrs: `data-dds-item-dk="${esc(i.item_id)}"` })}</div>`
          : "";
      const panel = ctx.knowing.has(key)
        ? whoHtml(
            { key, role: ctx.knowing.get(key), what: plain(entry.sentence), why: entry.why, meanwhile: "this change is left out.", item: i.item_id },
            { busy: ctx.busy },
          )
        : "";
      return approvalHtml(entry, i, c, { busy: ctx.busy, read: ctx.read.has(i.item_id), extra: `${receiptHtml(i, ctx.ledger)}${dk}${panel}` });
    })
    .join("");
  const open = ticks.length ? `<ul class="dds-items" data-dds-open-items>${ticks.map((i) => itemHtml(i, c, ctx, { open: true })).join("")}</ul>` : "";
  const waiting = sec.waiting.length ? `<ul class="dds-items" data-dds-waiting>${sec.waiting.map((i) => parkedHtml(i, c, ctx)).join("")}</ul>` : "";
  const sure = sec.sure.length
    ? `<details class="dds-sure" data-dds-sure="${sec.id}" data-dds-keep="sure-${sec.id}"><summary>${esc(
        plural(sec.sure.length, "sure fix", "sure fixes"),
      )}</summary><ul class="dds-items">${sec.sure.map((i) => itemHtml(i, c, ctx, { open: false })).join("")}</ul></details>`
    : "";
  const needsCheck = sec.pending.length || sec.waiting.length;
  const check = needsCheck
    ? `<label class="dds-tick dds-sec-check"><input type="checkbox" data-dds-sec-ok="${sec.id}" data-dds-sec-sig="${esc(signatureOf(sec))}"${isChecked(sec, ctx.checked) ? " checked" : ""}${
        ctx.busy ? " disabled" : ""
      }><span class="dds-item-body"><span class="dds-item-t">I have checked this section</span></span></label>`
    : "";
  return `<section class="dds-card dds-plan-sec" data-dds-plan-section="${sec.id}" data-dds-open-count="${sec.open.length}" aria-labelledby="dds-ps-${sec.id}"><h${level} id="dds-ps-${sec.id}" class="dds-group-t">${esc(
    sec.title,
  )} <span class="ds-count">${count(sec.items.length)}</span></h${level}><p class="dds-why" data-dds-sec-line>${esc(sec.line)} ${esc(
    `${lines.join(". ")}.`,
  )}</p>${approvalCards}${open}${waiting}${sure}${check}</section>`;
}

/** Everything the plan view works out of one answer, so the screen draws it and the final yes reads the same numbers. */
export function planModel(screen, ctx) {
  const { case: c, view } = screen;
  const items = activeSection(c).items;
  const ledger = ledgerOf(view);
  const approveIds = new Set((view.approve_class || []).map((a) => a.item_id));
  const confirmedIds = new Set((view.approve_class || []).filter((a) => a.confirmed).map((a) => a.item_id));
  const plan = planOf(items, approveIds, ledger, confirmedIds);
  const findings = findingsOf(items, ledger, approveIds);
  const unconfirmed = (view.approve_class || []).filter((a) => !a.confirmed).length;
  return {
    items,
    notes: notesOf(items),
    ledger,
    plan,
    findings,
    unconfirmed,
    gate: readyToApprove(plan, ctx.checked, findings, items),
  };
}

function notesHtml(notes, c) {
  if (!notes.length) return "";
  const names = namesOf(c);
  return `<section class="dds-card dds-notes" data-dds-notes><p class="dds-label">Good to know</p><ul class="dds-note-list">${notes
    .map((n) => `<li data-dds-note="${esc(n.item_id)}">${userTextIn(n.title || n.kind, names)}</li>`)
    .join("")}</ul></section>`;
}

export function planHtml(screen, model, ctx, { level = 3 } = {}) {
  const notes = notesHtml(model.notes, screen.case);
  if (!model.plan.length) return notes;
  const full = { ...ctx, ledger: model.ledger };
  return `<section class="dds-plan" data-dds-plan aria-labelledby="dds-plan-t"><h${level} id="dds-plan-t" class="dds-section-t">The plan, section by section</h${level}><p class="dds-why">Only changes that need your yes are open. Sure fixes are folded away and can still be unticked. Check each section, then give one final yes.</p>${model.plan
    .map((s) => sectionHtml(s, screen, full, level + 1))
    .join("")}</section>${notes}`;
}
