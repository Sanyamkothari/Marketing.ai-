// What the approval view works out of a case, with no markup (the evidence-first plan screen).
//
// The rule behind everything here: a number that says the plan is safe is one OUR CODE counted, never one the AI
// wrote. So this file only reads counts that arrive as data and orders them; it writes no explanation of its own
// beyond a count-led line ("12 customers kept off the contact list"). An item the AI suggested is never used as
// evidence: its words are not read here, only the counts our code attached to it.
//
// THE LEDGER. The plan ledger (the running totals and per-step receipts the engine counts) is read from
// `screen.view.ledger`, and per step from `item.payload.receipt`. Until the backend writes them the screen draws
// from what the case already holds (`item.payload.rows_in` / `rows_out`) and the parts that need the ledger stay
// out. Every part is data-driven, so it lights up when the fields arrive. The field names expected:
//
//   view.ledger = {
//     rows_in, rows_out, customers_in, customers_out,            // whole plan, in -> out
//     positive_rate_in, positive_rate_out,                       // percent (0-100), before and after
//     steps: [{ item_id, level?: "stop"|"flag"|"note",
//               rows_in, rows_out, customers_in, customers_out,
//               customers_lost, customers_duplicated,            // counted apart, never netted off
//               totals: [{ column, before, after }],             // money and count columns
//               personal_columns?: [name],                       // columns whose cells must be hidden
//               samples?: { before: [row], after: [row] } }],    // a few rows, each row { column: value }
//     findings: [{ level: "stop"|"flag"|"note", kind: "merged_ids"|"dropped_leak"|"contact_exclusion"|"guard"|...,
//                  count?, text?, columns?: [name], samples?: [row], item_id?,
//                  key? }],   // a stop's `key` is what a person's yes is sent with (`acknowledgeStop`); once given it is a flag
//   }
//
// Items may also carry (item.payload): `ids_merged`, `leak_suspect`, `contact_excluded`, `section`, `alert_level`.

/** The sections of the plan, in the order they are read. */
export const SECTIONS = Object.freeze([
  { id: "read", title: "Read the file", line: "How your file is read: the ID, the dates and the columns." },
  { id: "fix", title: "Fix values", line: "Fixes to the values in your columns, such as dates, numbers and yes or no answers." },
  { id: "combine", title: "Combine", line: "How files and tables are joined, stacked or reshaped into one." },
  { id: "outcome", title: "Outcome", line: "What counts as a yes, and which customers it is worked out for." },
  { id: "exclusions", title: "Exclusions", line: "Rows and customers left out, and who must not be contacted." },
  { id: "privacy", title: "Privacy", line: "How personal details are kept away from the AI and from the results." },
]);

const KIND_SECTION = Object.freeze({
  key: "read",
  role: "read",
  setting: "read",
  acknowledgement: "read",
  as_of_column: "read",
  rename_column: "read",
  join_files: "combine",
  stack_files: "combine",
  build_dataset: "combine",
  keep_latest_per_id: "combine",
  sql_query: "combine",
  combine_rows: "combine",
  unpivot: "combine",
  normalise_key: "combine",
  outcome_column: "outcome",
  define_outcome: "outcome",
  map_outcome_values: "outcome",
  outcome_definition: "outcome",
  outcome: "outcome",
  outcome_list: "outcome",
  remove_rows: "exclusions",
  valid_range: "exclusions",
  suppression: "exclusions",
  split_file: "exclusions",
  consent_column: "exclusions",
  pii_keep: "privacy",
  mask_pii: "privacy",
});

const payloadOf = (item) => (item && item.payload) || {};

/** The kind an item is read by: a recipe step by its step kind. */
export function kindOf(item) {
  const p = payloadOf(item);
  if (item.kind === "recipe_step" || item.kind === "step") {
    const step = p.step;
    if (step && typeof step.kind === "string") return step.kind;
    if (typeof p.kind === "string") return p.kind;
  }
  return item.kind;
}

/** The section an item belongs to: the case's own word (`payload.section`), else by its kind, else "Fix values". */
export function sectionOf(item) {
  const own = payloadOf(item).section;
  if (SECTIONS.some((s) => s.id === own)) return own;
  return KIND_SECTION[kindOf(item)] || "fix";
}

const num = (v) => (Number.isFinite(v) ? v : null);
const first = (...values) => values.map(num).find((v) => v !== null) ?? null;
const rowsOf = (v) => (Array.isArray(v) ? v.filter((r) => r && typeof r === "object").slice(0, 3) : []);

// --- receipts ---------------------------------------------------------------------------------------------------

export const ledgerOf = (view) => (view && view.ledger && typeof view.ledger === "object" ? view.ledger : null);

/**
 * What our code counted for one step: the ledger's row for it, else the receipt on the item, else the row counts the
 * case already holds. null when nothing was counted. Every field may be null: only counted numbers are drawn.
 */
export function receiptOf(item, ledger) {
  const p = payloadOf(item);
  const row = ledger && Array.isArray(ledger.steps) ? ledger.steps.find((s) => s.item_id === item.item_id) : null;
  const r = row || (p.receipt && typeof p.receipt === "object" ? p.receipt : null);
  const rowsIn = first(r && r.rows_in, p.rows_in);
  const rowsOut = first(r && r.rows_out, p.rows_out);
  if (!r && rowsIn === null && rowsOut === null) return null;
  const samples = (r && r.samples) || {};
  return {
    rowsIn,
    rowsOut,
    customersIn: first(r && r.customers_in),
    customersOut: first(r && r.customers_out),
    lost: first(r && r.customers_lost),
    duplicated: first(r && r.customers_duplicated),
    totals: ((r && r.totals) || []).filter((t) => t && typeof t.column === "string"),
    personal: ((r && r.personal_columns) || []).filter((c) => typeof c === "string"),
    before: rowsOf(samples.before),
    after: rowsOf(samples.after),
    level: r && ["stop", "flag", "note"].includes(r.level) ? r.level : null,
    counted: Boolean(r),
  };
}

// --- hiding personal data ---------------------------------------------------------------------------------------

const PERSONAL_NAME = /(e-?mail|phone|mobile|msisdn|name|address|dob|birth|aadhaar|aadhar|passport|\bpan\b|card)/i;
const EMAIL = /[^\s@]+@[^\s@]+\.[^\s@]+/;
// A run of at least nine digits (a phone or card number); a date such as 2024-01-31 has eight and stays readable.
const phoneLike = (text) =>
  (text.match(/\+?\d[\d\s\-().]{7,}\d/g) || []).some((m) => m.replace(/\D/g, "").length >= 9 && !/^\d{4}-\d{2}-\d{2}/.test(m));
export const HIDDEN = "hidden";

/** A cell for a sample row: hidden when its column is personal (by its name or the ledger's list) or it looks like one. */
export function maskCell(column, value, personal = []) {
  if (value === null || value === undefined || value === "") return "";
  const text = String(value);
  if (personal.includes(column) || PERSONAL_NAME.test(column) || EMAIL.test(text) || phoneLike(text)) return HIDDEN;
  return text.length > 40 ? `${text.slice(0, 37)}...` : text;
}

/** The columns of a few sample rows (at most `max`), in first-seen order. */
export function columnsOfRows(rows, max = 4) {
  const seen = [];
  for (const row of rows) for (const key of Object.keys(row)) if (!seen.includes(key)) seen.push(key);
  return seen.slice(0, max);
}

// --- findings, the alert levels and the interruption budget -------------------------------------------------------

export const LEVELS = Object.freeze(["stop", "flag", "note"]);
/** At most this many things ask for a person's attention at once (research lesson L7: 3 to 5). */
export const INTERRUPTION_BUDGET = 5;
export const TOP_CHECKS = 3;

const KIND_ORDER = Object.freeze([
  "merged_ids",
  "dropped_leak",
  "contact_exclusion",
  "guard",
  "customers_duplicated",
  "customers_lost",
  "rows_left_out",
]);

const plural = (n, one, many) => `${n.toLocaleString("en-US")} ${n === 1 ? one : many}`;

/** The count-led line of a finding: a count first, then what it counts. Written here, from counts, never by the AI. */
export function leadOf(f) {
  const n = f.count;
  switch (f.kind) {
    case "merged_ids":
      return `${plural(n, "customer ID", "customer IDs")} merged into others`;
    case "dropped_leak":
      return `${plural(n, "column", "columns")} left out: only known after the outcome`;
    case "contact_exclusion":
      return `${plural(n, "customer", "customers")} kept off the contact list`;
    case "customers_lost":
      return `${plural(n, "customer", "customers")} lost in one step`;
    case "customers_duplicated":
      return `${plural(n, "customer", "customers")} counted twice in one step`;
    case "rows_left_out":
      return `${plural(n, "row", "rows")} left out${f.of ? ` of ${f.of.toLocaleString("en-US")}` : ""}`;
    default:
      return f.text ? `${n ? `${n.toLocaleString("en-US")} ` : ""}${f.text}` : "A safety check found something";
  }
}

function add(found, f) {
  const same = found.find((g) => g.kind === f.kind && g.level === f.level && f.kind !== "guard");
  if (!same) {
    found.push({ ...f, items: f.itemId ? [f.itemId] : [] });
    return;
  }
  same.count = (same.count || 0) + (f.count || 0);
  same.columns = [...new Set([...(same.columns || []), ...(f.columns || [])])];
  same.samples = [...(same.samples || []), ...(f.samples || [])].slice(0, 3);
  if (f.itemId && !same.items.includes(f.itemId)) same.items.push(f.itemId);
}

/** The worst alert on one item, from the ledger row, the payload and what its counts say; or null. No source can
 * lower another: a step that leaves nothing is a stop whatever else is said about it. */
export function alertOf(item, ledger) {
  const r = receiptOf(item, ledger);
  const p = payloadOf(item);
  const said = [
    r && r.level,
    LEVELS.includes(p.alert_level) ? p.alert_level : null,
    r && r.rowsIn > 0 && r.rowsOut === 0 ? "stop" : null, // a step that leaves nothing stops the plan
    r && (r.lost > 0 || r.duplicated > 0) ? "flag" : null,
  ].filter(Boolean);
  return LEVELS.find((l) => said.includes(l)) || null;
}

/**
 * Everything that deserves a look, ranked by risk: stops, then flags, then notes; inside a level merged IDs, left-out
 * columns, contact-list exclusions, guard findings, then the rest, bigger counts first. Counts come from the ledger
 * or from fields our code put on an item. `approveIds` are the changes that need their own yes.
 */
export function findingsOf(items, ledger, approveIds = new Set()) {
  const found = [];
  // One fact, one line: a fact the guards already wrote a sentence about (by code) is not counted again from the
  // step's receipt. A guard that says something else about the step does not hide the receipt's counts.
  const told = new Map();
  const toldBy = (id, ...codes) => codes.some((c) => told.get(id)?.has(c));
  for (const f of (ledger && ledger.findings) || []) {
    if (!f || !LEVELS.includes(f.level)) continue;
    if (f.item_id && f.level !== "note" && typeof f.code === "string") {
      if (!told.has(f.item_id)) told.set(f.item_id, new Set());
      told.get(f.item_id).add(f.code);
    }
    add(found, {
      level: f.level,
      kind: f.kind || "guard",
      key: typeof f.key === "string" ? f.key : null,
      count: num(f.count) ?? 0,
      text: typeof f.text === "string" ? f.text : "",
      columns: Array.isArray(f.columns) ? f.columns : [],
      samples: rowsOf(f.samples),
      itemId: f.item_id || null,
    });
  }
  for (const item of items) {
    if (item.state === "rejected") continue;
    const p = payloadOf(item);
    const id = item.item_id;
    const merged = first(p.ids_merged);
    if (merged > 0) add(found, { level: "flag", kind: "merged_ids", count: merged, samples: rowsOf(p.merged_samples), itemId: id });
    if (p.leak_suspect === true && kindOf(item) === "drop_column" && typeof p.column === "string") {
      add(found, { level: "flag", kind: "dropped_leak", count: 1, columns: [p.column], itemId: id });
    }
    const contact = first(p.contact_excluded);
    if (contact > 0) add(found, { level: "flag", kind: "contact_exclusion", count: contact, samples: rowsOf(p.contact_samples), itemId: id });
    const r = receiptOf(item, ledger);
    if (!r) continue;
    const saidDup = toldBy(id, "JOIN_REPEATS_CUSTOMERS", "STEP_REPEATS_CUSTOMERS");
    const saidLost = toldBy(id, "STEP_LOSES_CUSTOMERS");
    if (!saidDup && r.duplicated > 0) add(found, { level: "flag", kind: "customers_duplicated", count: r.duplicated, itemId: id });
    if (!saidLost && r.lost > 0) add(found, { level: "flag", kind: "customers_lost", count: r.lost, itemId: id });
    if (r.rowsIn > 0 && r.rowsOut === 0) {
      if (toldBy(id, "STEP_ZERO_ROWS")) continue; // the guard's stop already says it (and stays a stop)
      add(found, { level: "stop", kind: "rows_left_out", count: r.rowsIn, of: r.rowsIn, itemId: id });
    } else if (approveIds.has(id) && r.rowsIn > r.rowsOut && !saidDup && !saidLost && !(r.lost > 0 || r.duplicated > 0)) {
      // (customers lost or counted twice already say it; rows are the same fact, said less well)
      add(found, { level: "flag", kind: "rows_left_out", count: r.rowsIn - r.rowsOut, of: r.rowsIn, itemId: id });
    }
  }
  const order = (f) => LEVELS.indexOf(f.level) * 100 + (KIND_ORDER.includes(f.kind) ? KIND_ORDER.indexOf(f.kind) : 50);
  return found.sort((a, b) => order(a) - order(b) || (b.count || 0) - (a.count || 0));
}

/**
 * Stops always ask; flags ask while the budget lasts (`used` counts the questions and own yes already asking); the
 * rest are folded as notes. Returns `{ asking, folded }`.
 */
export function withinBudget(findings, used = 0, budget = INTERRUPTION_BUDGET) {
  const asking = [];
  const folded = [];
  let room = Math.max(0, budget - used - findings.filter((f) => f.level === "stop").length);
  for (const f of findings) {
    if (f.level === "stop") asking.push(f);
    else if (f.level === "flag" && room > 0) {
      asking.push(f);
      room -= 1;
    } else folded.push(f);
  }
  return { asking, folded };
}

// --- the whole plan -----------------------------------------------------------------------------------------------

/** A note: a check our rules made that changes nothing and warns of nothing (level "note", e.g. "every ID is different").
 * It is shown as a line, and needs no yes unless a stop or flag points at it. */
export const isNote = (item) => item.kind === "data_check" && item.source === "rules" && payloadOf(item).level === "note";

/** An item needs a yes when it is not a sure fix, was not found by the rules, needs its own yes or carries a stop or flag. */
export function needsYes(item, approveIds, ledger) {
  if (approveIds.has(item.item_id)) return true;
  const level = alertOf(item, ledger);
  if (!isNote(item) && (item.confidence !== "sure" || item.source !== "rules")) return true; // a note asks for a yes only when a check points at it
  return level === "stop" || level === "flag";
}

/** The notes to read, each sentence once. */
export const notesOf = (items) => {
  const live = items.filter((i) => isNote(i) && i.state !== "rejected");
  return live.filter((i, n) => live.findIndex((j) => j.title === i.title) === n);
};

/**
 * The plan in sections. Each section: its items split into `open` (need a yes), `sure` (folded, still untickable) and
 * `waiting` (parked for someone who would know) and `pending` (the open ones still asking for something).
 */
export function planOf(items, approveIds, ledger, confirmedIds = new Set()) {
  return SECTIONS.map((s) => {
    const mine = items.filter((i) => sectionOf(i) === s.id && !isNote(i));
    const waiting = mine.filter((i) => i.parked_for);
    const rest = mine.filter((i) => !i.parked_for);
    const open = rest.filter((i) => needsYes(i, approveIds, ledger));
    const sure = rest.filter((i) => !open.includes(i));
    // An own yes already given is drawn (it says "Agreed") but asks for nothing more.
    const pending = open.filter((i) => !confirmedIds.has(i.item_id));
    return { ...s, items: mine, open, sure, waiting, pending };
  }).filter((s) => s.items.length);
}

/** What a section held when it was checked: any change to its items (a new one, a tick, a park) asks for a new look. */
export const signatureOf = (sec) => sec.items.map((i) => `${i.item_id}:${i.state}:${i.parked_for || ""}`).join("|");

/** Whether a section counts as checked: `checked` maps a section id to the signature it had when it was checked. */
export const isChecked = (sec, checked) => checked.get(sec.id) === signatureOf(sec);

/**
 * Whether the final yes is ready: every section with something to look at has been checked as it is now, and no stop
 * is left. A stop holds when its step is still ticked, or when it names no step at all (a check on the whole plan).
 */
export function readyToApprove(plan, checked, findings, items) {
  const unchecked = plan.filter((s) => (s.pending.length || s.waiting.length) && !isChecked(s, checked));
  const open = new Set(items.filter((i) => i.state === "accepted").map((i) => i.item_id));
  const stops = findings.filter((f) => f.level === "stop" && (!f.items.length || f.items.some((id) => open.has(id))));
  return { unchecked, stops, ok: !unchecked.length && !stops.length };
}

// --- the question queue -------------------------------------------------------------------------------------------

/** At most this many blocking questions are asked at once. */
export const BLOCKING_AT_ONCE = 3;
const isOpen = (q) => q.answer === null || q.answer === undefined;

/** `now`: up to three open blocking questions; `later`: the rest of the open ones, each with its default stated; `done`. */
export function queueOf(questions) {
  const open = questions.filter(isOpen);
  const blocking = open.filter((q) => q.blocking);
  const now = blocking.slice(0, BLOCKING_AT_ONCE);
  return { now, later: open.filter((q) => !now.includes(q)), done: questions.filter((q) => !isOpen(q)) };
}
