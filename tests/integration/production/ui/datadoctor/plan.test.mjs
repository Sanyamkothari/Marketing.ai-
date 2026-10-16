/* The evidence-first plan screen (Data Doctor): the parts that need no page - sections, the ranking of what to check,
   the interruption budget, the question queue, hiding personal data, the "Who would know?" message - then, with the
   recorded answers ($DDS_FIXTURES), the screen itself in jsdom (plan_cases.mjs). */
import { test } from "node:test";
import assert from "node:assert/strict";
import {
  BLOCKING_AT_ONCE,
  countsAsLoss,
  INTERRUPTION_BUDGET,
  SECTIONS,
  alertOf,
  findingsOf,
  leadOf,
  maskCell,
  notesOf,
  planOf,
  queueOf,
  readyToApprove,
  receiptOf,
  sectionOf,
  signatureOf,
  withinBudget,
} from "../../../../../ui/modules/datadoctor/plan.js";
import { WHO, messageFor } from "../../../../../ui/modules/datadoctor/who.js";

const item = (id, kind, extra = {}) => ({
  item_id: id,
  kind,
  title: id,
  source: "rules",
  confidence: "sure",
  state: "accepted",
  payload: {},
  ...extra,
});
const step = (id, stepKind, extra = {}) => item(id, "recipe_step", { payload: { step: { kind: stepKind }, ...(extra.payload || {}) }, ...extra });

test("the plan has six sections and every kind of change lands in one of them", () => {
  assert.deepEqual(
    SECTIONS.map((s) => s.title),
    ["Read the file", "Fix values", "Combine", "Outcome", "Exclusions", "Privacy"],
  );
  const where = (i) => sectionOf(i);
  assert.equal(where(item("a", "key")), "read");
  assert.equal(where(step("b", "map_boolean")), "fix");
  assert.equal(where(step("c", "sql_query")), "combine");
  assert.equal(where(item("d", "join_files")), "combine");
  assert.equal(where(item("e", "outcome_column")), "outcome");
  assert.equal(where(step("f", "remove_rows")), "exclusions");
  assert.equal(where(item("g", "consent_column")), "exclusions");
  assert.equal(where(item("h", "pii_keep")), "privacy");
  assert.equal(where(item("i", "something_new")), "fix", "an unknown kind is a fix to a value, never lost");
  assert.equal(where(item("j", "key", { payload: { section: "privacy" } })), "privacy", "the case may say where it belongs");
});

test("only what needs a yes is open: sure fixes fold, an AI suggestion and a step with a flag open, parked steps wait", () => {
  const sure = step("sure", "parse_date");
  const check = step("check", "parse_number", { confidence: "check" });
  const ai = step("ai", "map_values", { source: "ai" });
  const parked = step("parked", "map_values", { parked_for: "finance", state: "rejected" });
  const flagged = step("flagged", "sql_query", { payload: { step: { kind: "sql_query" } } });
  const ledger = { steps: [{ item_id: "flagged", customers_lost: 5 }] };
  const by = Object.fromEntries(planOf([sure, check, ai, parked, flagged], new Set(), ledger).map((s) => [s.id, s]));
  const { fix, combine } = by;
  assert.deepEqual(fix.sure.map((i) => i.item_id), ["sure"]);
  assert.deepEqual(fix.open.map((i) => i.item_id), ["check", "ai"]);
  assert.deepEqual(fix.waiting.map((i) => i.item_id), ["parked"]);
  assert.deepEqual(combine.open.map((i) => i.item_id), ["flagged"]);
  const own = planOf([step("own", "remove_rows")], new Set(["own"]), null, new Set(["own"]))[0];
  assert.deepEqual([own.open.length, own.pending.length], [1, 0], "an own yes already given asks for nothing more");
});

test("what to check is ranked by risk and led by counts, from fields our code set", () => {
  const items = [
    item("leak", "recipe_step", { payload: { step: { kind: "drop_column" }, column: "cancel_reason", leak_suspect: true } }),
    item("contact", "suppression", { payload: { contact_excluded: 40 } }),
    item("merge", "recipe_step", { payload: { step: { kind: "normalise_key" }, ids_merged: 3, merged_samples: [{ before: "C-1", after: "1" }] } }),
  ];
  const ledger = { findings: [{ level: "flag", kind: "guard", count: 2, text: "customers have two ages", item_id: "merge" }, { level: "stop", kind: "guard", text: "The query made a customer appear twice." }] };
  const found = findingsOf(items, ledger);
  assert.deepEqual(
    found.map((f) => [f.level, f.kind]),
    [["stop", "guard"], ["flag", "merged_ids"], ["flag", "dropped_leak"], ["flag", "contact_exclusion"], ["flag", "guard"]],
  );
  assert.equal(leadOf(found[1]), "3 customer IDs merged into others");
  assert.equal(leadOf(found[2]), "1 column left out: only known after the outcome");
  assert.equal(leadOf(found[3]), "40 customers kept off the contact list");
  assert.equal(leadOf(found[4]), "2 customers have two ages");
  assert.deepEqual(found[1].samples, [{ before: "C-1", after: "1" }]);
});

test("a step that leaves nothing is a stop; lost and counted-twice customers are told apart", () => {
  const items = [step("none", "sql_query"), step("dup", "sql_query")];
  const ledger = { steps: [{ item_id: "none", rows_in: 10, rows_out: 0 }, { item_id: "dup", rows_in: 10, rows_out: 10, customers_lost: 4, customers_duplicated: 4 }] };
  const found = findingsOf(items, ledger);
  assert.equal(found[0].level, "stop");
  assert.deepEqual(found.slice(1).map((f) => f.kind).sort(), ["customers_duplicated", "customers_lost"], "4 lost and 4 doubled never cancel out");
  const plan = planOf(items, new Set(), ledger);
  const gate = readyToApprove(plan, new Map(), found, items);
  assert.equal(gate.ok, false);
  assert.equal(gate.stops.length, 1, "a stop on a step still ticked holds the final yes");
  const lowered = { steps: [{ item_id: "none", rows_in: 10, rows_out: 0, level: "note" }] };
  assert.equal(alertOf(step("none", "sql_query", { payload: { alert_level: "note" } }), lowered), "stop", "nothing can lower a stop");
});

test("a stop that names no step holds the final yes; a check given to a section lapses when the section changes", () => {
  const items = [step("a", "parse_date", { confidence: "check" })];
  const plan = planOf(items, new Set(), null);
  const checked = new Map([["fix", signatureOf(plan[0])]]);
  assert.equal(readyToApprove(plan, checked, [], items).ok, true);
  const global = [{ level: "stop", kind: "guard", count: 0, items: [] }];
  assert.equal(readyToApprove(plan, checked, global, items).ok, false, "a stop on the whole plan is never skipped");
  const grown = [...items, step("b", "parse_number")];
  const later = planOf(grown, new Set(), null);
  assert.deepEqual(readyToApprove(later, checked, [], grown).unchecked.map((s) => s.id), ["fix"], "a new change asks for a new look");
});

test("a join that folds an orders log into one row per customer is not a loss; a change left unticked is told at the final yes", () => {
  const join = item("join", "build_dataset", { confidence: "check", state: "pending" });
  const note = item("note", "data_check", { confidence: "check", state: "pending" });
  const items = [join, note, step("a", "parse_date", { confidence: "check", state: "accepted" })];
  assert.equal(countsAsLoss(join), false, "6,029 order rows in, 4,000 customers out: no one is lost");
  assert.equal(countsAsLoss(join, { lost: 0 }), false);
  assert.equal(countsAsLoss(join, { lost: 12 }), true, "a join whose receipt says customers are lost is still a loss");
  assert.equal(countsAsLoss(step("q", "sql_query")), true);
  const plan = planOf(items, new Set(), null);
  const checked = new Map(plan.map((s) => [s.id, signatureOf(s)]));
  const gate = readyToApprove(plan, checked, [], items);
  assert.equal(gate.ok, true, "the section check alone still lets the final yes through");
  assert.deepEqual(gate.skipped.map((i) => i.item_id), ["join"], "but the unticked join is named: approving leaves it out");
  const ticked = [item("join", "build_dataset", { confidence: "check", state: "accepted" })];
  assert.deepEqual(readyToApprove(planOf(ticked, new Set(), null), new Map(), [], ticked).skipped, []);
});

test("at most five things interrupt: stops always, flags while the budget lasts, the rest are notes", () => {
  const flags = Array.from({ length: 6 }, (_, n) => ({ level: "flag", kind: "guard", count: n, items: [] }));
  const stops = [{ level: "stop", kind: "guard", count: 0, items: [] }, { level: "stop", kind: "guard", count: 0, items: [] }];
  const { asking, folded } = withinBudget([...stops, ...flags], 1);
  assert.equal(asking.length + 1, INTERRUPTION_BUDGET, "with one question already asking, two stops and two flags fill the five");
  assert.equal(folded.length, 4);
  assert.equal(withinBudget([...stops, ...stops, ...stops, ...flags], 3).asking.length, 6, "a stop is never folded away");
  assert.equal(withinBudget([{ level: "note", kind: "guard", items: [] }], 0).asking.length, 0, "a note never interrupts");
});

test("at most three blocking questions at once; the rest wait", () => {
  const q = (id, blocking, answer = null) => ({ question_id: id, blocking, answer });
  const queue = queueOf([q("a", true), q("b", true), q("c", true), q("d", true), q("e", false), q("f", true, "x")]);
  assert.equal(BLOCKING_AT_ONCE, 3);
  assert.deepEqual(queue.now.map((x) => x.question_id), ["a", "b", "c"]);
  assert.deepEqual(queue.later.map((x) => x.question_id), ["d", "e"]);
  assert.deepEqual(queue.done.map((x) => x.question_id), ["f"]);
});

test("personal details are hidden in sample rows, by column name, by the ledger's list and by what a cell looks like", () => {
  assert.equal(maskCell("email", "a@b.com"), "hidden");
  assert.equal(maskCell("customer_name", "Asha"), "hidden");
  assert.equal(maskCell("note", "call 98765 43210 now"), "hidden");
  assert.equal(maskCell("city", "Pune", ["city"]), "hidden");
  assert.equal(maskCell("segment", "Gold"), "Gold");
  assert.equal(maskCell("spend", 120.5), "120.5");
  assert.equal(maskCell("signup", "2024-01-31"), "2024-01-31", "a date is not a phone number");
  assert.equal(maskCell("signup", "2024-01-31 10:15:00"), "2024-01-31 10:15:00");
  assert.equal(maskCell("ref", "+91 98765 43210"), "hidden");
});

test("a receipt falls back to the row counts the case holds, and says nothing it was not given", () => {
  const bare = receiptOf(item("x", "join_files", { payload: { rows_in: 100, rows_out: 90 } }), null);
  assert.deepEqual([bare.rowsIn, bare.rowsOut, bare.lost, bare.duplicated, bare.counted], [100, 90, null, null, false]);
  assert.equal(receiptOf(item("y", "key"), null), null);
});

test("the message for the one who would know holds the point, why it matters and what happens meanwhile", () => {
  assert.deepEqual(WHO.map((w) => w.label), ["CRM admin", "Database admin", "Compliance", "Finance", "Campaign manager"]);
  const text = messageFor("compliance", { what: "Does Y mean the customer agreed?", why: "It decides who may be contacted.", meanwhile: "this step is left out." });
  assert.match(text, /^Hello \(Compliance\),/);
  assert.match(text, /The point: Does Y mean the customer agreed\?/);
  assert.match(text, /Why it matters: It decides who may be contacted\./);
  assert.match(text, /Until I hear back: this step is left out\./);
});

if (!process.env.DDS_FIXTURES) {
  test("the plan screen's jsdom suite (run it through tests/integration/test_data_doctor_ui.py)", {
    skip: "DDS_FIXTURES is not set",
  }, () => {});
} else {
  await import("./plan_cases.mjs");
}

test("one fact, one line: a step the guards already wrote about is not counted again from its receipt", () => {
  const items = [step("cut", "remove_rows")];
  const ledger = {
    steps: [{ item_id: "cut", rows_in: 1000, rows_out: 970, customers_in: 1000, customers_out: 970, customers_lost: 30 }],
    findings: [{ level: "flag", kind: "guard", code: "STEP_LOSES_CUSTOMERS", text: "Step 3 loses 30 of 1,000 customers", item_id: "cut" }],
  };
  const found = findingsOf(items, ledger, new Set(["cut"]));
  assert.deepEqual(found.map((f) => f.kind), ["guard"], "not also '30 customers lost' and '30 rows left out'");
  const bare = findingsOf(items, { steps: ledger.steps }, new Set(["cut"]));
  assert.deepEqual(bare.map((f) => f.kind), ["customers_lost"], "without the guards' sentence, customers lost, not rows too");
});

test("a guard that says something else about a step does not hide the receipt's counts", () => {
  const items = [step("cut", "remove_rows")];
  const steps = [{ item_id: "cut", rows_in: 1000, rows_out: 970, customers_in: 1000, customers_out: 970, customers_lost: 30 }];
  const other = { steps, findings: [{ level: "flag", kind: "guard", code: "EXPECTATION_CHANGED_AFTER_RUN", text: "changed after it ran", item_id: "cut" }] };
  assert.deepEqual(findingsOf(items, other, new Set(["cut"])).map((f) => f.kind).sort(), ["customers_lost", "guard"]);
  const zero = [{ item_id: "cut", rows_in: 1000, rows_out: 0, customers_in: 1000, customers_out: 0, customers_lost: 1000 }];
  const said = { steps: zero, findings: [{ level: "stop", kind: "guard", code: "STEP_ZERO_ROWS", text: "Step 3 left no rows at all", item_id: "cut" }] };
  assert.deepEqual(findingsOf(items, said, new Set()).map((f) => f.kind), ["guard", "customers_lost"], "no second 'rows left out' stop");
  const unsaid = { steps: zero, findings: [] };
  assert.ok(findingsOf(items, unsaid, new Set()).some((f) => f.kind === "rows_left_out" && f.level === "stop"), "without the guard, the stop stays");
});

test("a live note is shown even when an earlier copy of the same sentence was rejected", () => {
  const note = (id, state) => item(id, "data_check", { title: "same", state, confidence: "check", payload: { level: "note" } });
  assert.deepEqual(notesOf([note("a", "rejected"), note("b", "pending")]).map((i) => i.item_id), ["b"]);
});

test("a note is a line to read: it is no change, needs no yes and is shown once", () => {
  const note = (id, title) => item(id, "data_check", { title, state: "pending", confidence: "check", payload: { level: "note", code: "KEY_REPORT" } });
  const items = [note("n1", "'customer_id' is different on every row"), note("n2", "'customer_id' is different on every row"), note("n3", "other")];
  assert.deepEqual(planOf(items, new Set(), null), [], "no section, no tick, no 'needs your yes'");
  assert.deepEqual(notesOf(items).map((i) => i.item_id), ["n1", "n3"]);
  const flag = item("f1", "data_check", { state: "pending", confidence: "check", payload: { level: "flag" } });
  assert.equal(planOf([flag], new Set(), null)[0].pending.length, 1, "a flag still asks");
});
