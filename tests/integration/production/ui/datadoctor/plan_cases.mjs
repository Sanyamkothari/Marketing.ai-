/* The cases of plan.test.mjs: the plan screen in jsdom, on answers the real visit gave, with the plan ledger's fields
   (which the API sends later) added on the way out, so the parts that need them are drawn and tested now. */
import { test } from "node:test";
import assert from "node:assert/strict";
import { $, $$, installPage, settle, until } from "../harness.mjs";
import { CASE, makeWorld, screen, serverFor } from "./common.mjs";
import { check } from "../a11y/checks.mjs";
import { visibleText } from "../glossary/collect.mjs";
import { violations, describe } from "../glossary/check.mjs";

let world = makeWorld();
installPage((request) => serverFor(world)(request), { hash: "#/data/check" });
const { mountDataDoctor, _resetDataDoctorForTests } = await import("../../../../../ui/modules/datadoctor/screen.js");
const { forgetAiServiceStatus } = await import("../../../../../ui/availability.js");

const app = $("#app");
const posts = (tail) => world.posted.filter((p) => p.path === `/cases/${CASE}/${tail}`);

async function show(at, { patch = null } = {}) {
  world = makeWorld({ at });
  world.patch = patch;
  _resetDataDoctorForTests();
  forgetAiServiceStatus();
  await settle(2);
  app.innerHTML = `<main><h1>Data</h1><div class="dds-layout"><div id="m"></div><aside id="a"></aside></div></main>`;
  mountDataDoctor({ main: $("#m"), aside: $("#a") }, { place: "data", detail: true });
  await until(() => $("[data-dds-verdict]") || $("[data-dds-start]"), 3000, `the ${at} screen`);
  await settle(3);
}

const NOW = "2026-10-12T09:00:00Z";
const sqlItem = (extra = {}) => ({
  item_id: "sql.spend",
  kind: "recipe_step",
  title: "Add each customer's total spend",
  source: "rules",
  confidence: "check",
  state: "pending",
  upload_id: null,
  evidence_ids: [],
  payload: {
    step: { kind: "sql_query", params: { sql: "SELECT c.id, SUM(o.amount) AS spend FROM t_customers c JOIN t_orders o ON o.id = c.id GROUP BY c.id" } },
    detail: "SELECT c.id, SUM(o.amount) AS spend FROM t_customers c JOIN t_orders o ON o.id = c.id GROUP BY c.id",
    rows_in: 1200,
    rows_out: 1100,
  },
  payload_digest: "sql",
  proposed_at: NOW,
  ...extra,
});
const LEDGER = {
  rows_in: 12000,
  rows_out: 11100,
  customers_in: 10000,
  customers_out: 9700,
  positive_rate_in: 14.2,
  positive_rate_out: 12.9,
  steps: [
    {
      item_id: "sql.spend",
      rows_in: 1200,
      rows_out: 1100,
      customers_in: 1200,
      customers_out: 1100,
      customers_lost: 100,
      customers_duplicated: 40,
      totals: [{ column: "spend", before: 100500, after: 98200 }],
      samples: {
        before: [{ customer_id: "C-1", email: "asha@example.com", spend: 120 }],
        after: [{ customer_id: "C-1", email: "asha@example.com", spend: 118 }],
      },
    },
  ],
  findings: [
    { level: "stop", kind: "guard", count: 0, text: "A join made one customer appear on two rows.", item_id: "sql.spend" },
    { level: "flag", kind: "merged_ids", count: 3, samples: [{ before_id: "C-123", after_id: "123", other_id: "S-123" }] },
    { level: "flag", kind: "contact_exclusion", count: 40, samples: [{ customer_id: "C-9", phone: "9876543210" }] },
    { level: "note", kind: "guard", count: 2, text: "columns are almost empty." },
  ],
};
const withLedger = (extra = {}, ledger = LEDGER) => (s) => {
  s.case.items.push(sqlItem(extra));
  s.view.ledger = ledger;
  return s;
};

// --- the funnel -------------------------------------------------------------------------------------------------

test("the funnel: rows and customers in and out, the yes share before and after, and the biggest losses", async () => {
  await show("approve_class", { patch: withLedger() });
  const funnel = $("[data-dds-funnel]");
  const row = (kind) => funnel.querySelector(`[data-dds-funnel-row="${kind}"]`);
  assert.match(row("rows").textContent, /12,000 before, 11,100 after \(900 fewer, 7\.5%\)/);
  assert.match(row("customers").textContent, /10,000 before, 9,700 after \(300 fewer, 3\.0%\)/);
  assert.match(row("rate").textContent, /14\.2% before, 12\.9% after/);
  assert.ok(row("rows").classList.contains("is-flag") && row("rate").classList.contains("is-flag"), "a loss over 5% and a move over 1 point stand out");
  assert.ok(!row("customers").classList.contains("is-flag"));
  const loss = funnel.querySelector("[data-dds-goto]");
  assert.match(loss.textContent, /Add each customer's total spend: 100 rows/);
  loss.click();
  await settle(1);
  assert.ok(document.activeElement.closest('[data-dds-item="sql.spend"]'), "Biggest losses takes you to the step");
});

test("with no ledger the funnel draws only each step's own losses, never a total chained across steps", async () => {
  await show("approve");
  assert.ok(!$("[data-dds-funnel]") && !$("[data-dds-checks]"));
  await show("approve", {
    patch: (s) => {
      s.case.items.push(sqlItem({ confidence: "sure", state: "accepted" }));
      return s;
    },
  });
  assert.ok(!$("[data-dds-funnel-row]"), "only what was counted is drawn");
  assert.match($("[data-dds-funnel] [data-dds-goto]").textContent, /Add each customer's total spend: 100 rows/);
  assert.match($('[data-dds-step-receipt="sql.spend"]').textContent, /1,200 before, 1,100 after/, "the step's own counts still show");
});

// --- three things to check --------------------------------------------------------------------------------------

test("3 things to check: riskiest first, counts and sample rows first, never the AI's words", async () => {
  await show("approve_class", {
    patch: (s) => {
      withLedger()(s);
      s.case.items.push({
        ...sqlItem({ item_id: "ai.step", title: "TRUST ME, THIS IS PERFECTLY SAFE", source: "ai", confidence: "check" }),
        payload: { step: { kind: "normalise_key" }, ids_merged: 9 },
      });
      return s;
    },
  });
  const box = $("[data-dds-checks]");
  assert.equal(box.querySelector("h3, h4, h5").textContent, "3 things to check");
  const top = [...box.querySelector(".dds-check-list").children];
  assert.deepEqual(top.map((li) => li.dataset.ddsLevel), ["stop", "flag", "flag"]);
  assert.match(top[0].textContent, /A join made one customer appear on two rows\./);
  assert.match(top[1].textContent, /12 customer IDs merged into others/, "merged IDs from every step add up");
  assert.match(top[2].textContent, /40 customers kept off the contact list/);
  assert.ok(!box.textContent.includes("TRUST ME"), "an AI step's words are not evidence");
  assert.equal(top[2].querySelectorAll("td.dds-hidden-cell").length, 1, "a phone number in a sample row is hidden");
  assert.ok(!app.textContent.includes("9876543210") && !app.textContent.includes("asha@example.com"));
  assert.match(box.querySelector("[data-dds-more-notes] summary").textContent, /3 more notes/);
});

test("the three alert levels look different: a stop, a flag (check) and a note", async () => {
  await show("approve_class", { patch: withLedger() });
  const chip = (level) => [...$$(`.badge[data-dds-level="${level}"]`)][0];
  const look = (el) => [el.className, el.textContent.trim()];
  assert.deepEqual(look(chip("stop")), ["badge tone-critical", "Stop"]);
  assert.deepEqual(look(chip("flag")), ["badge tone-warning", "Check"]);
  assert.deepEqual(look($(`.badge[data-dds-level="note"]`)), ["badge tone-info", "Note"]);
});

// --- a step's receipt ----------------------------------------------------------------------------------------------

test("a step shows what our code counted: lost and counted-twice apart, totals, rows before and after with personal data hidden", async () => {
  await show("approve_class", { patch: withLedger() });
  const section = $('[data-dds-plan-section="combine"]');
  assert.ok(section, "a query step lands in Combine");
  const receipt = section.querySelector('[data-dds-step-receipt="sql.spend"]');
  assert.match(receipt.textContent, /We counted/);
  assert.match(receipt.querySelector("[data-dds-lost]").textContent, /Customers lost100/);
  assert.match(receipt.querySelector("[data-dds-duplicated]").textContent, /Customers that appear twice40/, "never netted off against each other");
  assert.match(receipt.querySelector("[data-dds-total]").textContent, /100500 before, 98200 after/);
  assert.match(receipt.textContent, /1,200 before, 1,100 after/);
  const [before, after] = receipt.querySelectorAll("[data-dds-sample]");
  assert.equal(before.querySelector("caption").textContent, "Before");
  assert.equal(after.querySelector("caption").textContent, "After");
  assert.equal(before.querySelector("td.dds-hidden-cell").textContent, "hidden", "an email is hidden");
  assert.match(receipt.textContent, /Personal details are hidden\./);
});

test("the query is only behind Show the query", async () => {
  await show("approve_class", { patch: withLedger() });
  const fold = $("details.dds-query");
  assert.equal(fold.querySelector("summary").textContent, "Show the query");
  assert.equal(fold.open, false);
  const holders = $$("*").filter((el) => el.children.length === 0 && el.textContent.includes("SUM(o.amount)"));
  assert.equal(holders.length, 1, "the query is drawn once");
  assert.ok(fold.contains(holders[0]), "and only inside Show the query");
});

// --- the plan, section by section ---------------------------------------------------------------------------------

test("the plan: sections with one sentence and one count, only what needs a yes open, one final yes", async () => {
  await show("approve_class");
  const ids = $$("[data-dds-plan-section]").map((s) => s.dataset.ddsPlanSection);
  assert.deepEqual(ids, ["read", "outcome", "exclusions"]);
  assert.ok(ids.length <= 7);
  const line = $('[data-dds-plan-section="exclusions"] [data-dds-sec-line]').textContent;
  assert.match(line, /Rows and customers left out, and who must not be contacted\. 2 changes\. 2 need your yes\./);
  const read = $('[data-dds-plan-section="read"]');
  assert.ok(!read.querySelector("input[data-dds-sec-ok]"), "a section with nothing to decide asks for no check");
  assert.equal(read.querySelector("details[data-dds-sure] summary").textContent, "2 sure fixes");
  assert.ok(read.querySelectorAll("details[data-dds-sure] input[data-dds-tick]").length === 2, "folded, and still untickable");
  const approve = $("[data-dds-approve-button]");
  assert.ok(approve.disabled);
  assert.match($("[data-dds-approve-hold]").textContent, /Check each section above first: Exclusions\./);
});

test("the top says what to do: sections to check, then Approve, from the counts the screen has", async () => {
  await show("approve", {
    patch: (s) => {
      s.case.items.push(sqlItem());
      return s;
    },
  });
  assert.equal($("[data-dds-todo]").textContent, "To do: check 1 section, then Approve");
  const box = $('input[data-dds-sec-ok="combine"]');
  box.checked = true;
  box.dispatchEvent(new window.Event("change", { bubbles: true }));
  assert.equal($("[data-dds-todo]").textContent, "To do: Approve");
});

test("the To do line also counts changes still waiting for their own yes, and a stop replaces Approve", async () => {
  const { todoHtml } = await import("../../../../../ui/modules/datadoctor/plan_view.js");
  const text = (html) => html.replace(/<[^>]+>/g, "");
  assert.equal(text(todoHtml("needs_approval", { unchecked: 0, unconfirmed: 1 })), "To do: agree to 1 change, then Approve");
  assert.equal(
    text(todoHtml("needs_approval", { asking: 2, unchecked: 1, unconfirmed: 2, stops: 1 })),
    "To do: answer 2 questions, check 1 section, agree to 2 changes, then fix the step that stops the plan",
  );
  assert.equal(todoHtml("ready", { unchecked: 1 }), "");
  assert.equal(todoHtml("not_ready", {}), "");
});

test("a note (a check that changes nothing) is not counted as needing a yes", async () => {
  const note = (id) => ({ ...sqlItem({ item_id: id, kind: "data_check", state: "accepted", title: "customer_id: every value is different" }), payload: { level: "note", code: "ID_UNIQUE" } });
  const fix = { ...sqlItem({ item_id: "fix.one", title: "Read spend as a number" }), payload: { step: { kind: "parse_number" } } };
  await show("approve", {
    patch: (s) => {
      s.case.items.push(fix, note("n1"), note("n2"));
      return s;
    },
  });
  const sec = $('[data-dds-plan-section="fix"]');
  const shown = sec.querySelectorAll("[data-dds-item]").length;
  assert.match(sec.querySelector("[data-dds-sec-line]").textContent, new RegExp(`${shown} changes?\\. 1 needs your yes\\.`), "the two notes are not changes");
  assert.equal(sec.querySelectorAll('[data-dds-item="n1"], [data-dds-item="n2"]').length, 0, "notes are lines, not items to tick");
  assert.equal(sec.querySelector("details[data-dds-sure] summary").textContent, "1 sure fix");
  assert.match($("[data-dds-notes]").textContent, /every value is different/, "the notes are read once, under Good to know");
});

test("Approve waits for each section with something to decide, then one final yes", async () => {
  await show("approve", {
    patch: (s) => {
      s.case.items.push(sqlItem());
      return s;
    },
  });
  const approve = () => $("[data-dds-approve-button]");
  assert.ok(approve().disabled, "the query step still needs a look");
  assert.match($("[data-dds-approve-hold]").textContent, /Combine/);
  const box = $('input[data-dds-sec-ok="combine"]');
  box.checked = true;
  box.dispatchEvent(new window.Event("change", { bubbles: true }));
  assert.equal(approve().disabled, false);
  assert.equal(document.activeElement.dataset.ddsSecOk, "combine", "the focus stays on the box");
  world.next.approval = "approve";
  approve().click();
  await until(() => posts("approval").length === 1, 2000, "the one final yes");
});

test("a stop on a step still ticked holds the final yes, even after its section is checked", async () => {
  await show("approve", {
    patch: (s) => {
      withLedger({ state: "accepted" })(s);
      return s;
    },
  });
  const box = $('input[data-dds-sec-ok="combine"]');
  box.checked = true;
  box.dispatchEvent(new window.Event("change", { bubbles: true }));
  assert.ok($("[data-dds-approve-button]").disabled);
  assert.match($("[data-dds-approve-hold]").textContent, /A step stops the plan/);
});

test("saying yes to a stop leaves the section checks as they were (the plan did not change)", async () => {
  const stop = { ...LEDGER, findings: [{ level: "stop", kind: "guard", key: "k1", count: 0, text: "A join made one customer appear on two rows.", item_id: "sql.spend" }] };
  await show("approve", { patch: withLedger({ state: "accepted" }, stop) });
  const box = () => $('input[data-dds-sec-ok="combine"]');
  box().checked = true;
  box().dispatchEvent(new window.Event("change", { bubbles: true }));
  assert.ok(box().checked);
  world.patch = (s) => {
    withLedger({ state: "accepted" }, { ...stop, findings: [{ ...stop.findings[0], level: "flag" }] })(s);
    return s;
  };
  $("[data-dds-ack]").click();
  await until(() => posts("acknowledgements").length === 1, 2000, "the yes to the stop");
  await settle(3);
  assert.ok(box().checked, "the section is still checked after the stop got its yes");
});

// --- the question queue --------------------------------------------------------------------------------------------

test("at most three blocking questions at once; the rest can wait, each with its default stated", async () => {
  await show("question", {
    patch: (s) => {
      const base = s.case.questions.find((q) => q.code === "KEY_AMBIGUOUS");
      for (let n = 1; n <= 4; n += 1) {
        s.case.questions.push({ ...structuredClone(base), question_id: `q.more.${n}`, code: `MORE_${n}`, text: `Extra question ${n}?` });
      }
      return s;
    },
  });
  const cards = $$("[data-dds-question]");
  const later = $("[data-dds-later]");
  const now = cards.filter((c) => !later.contains(c) && c.classList.contains("is-open") && c.querySelector(".tag"));
  assert.equal(now.length, 3, "three blocking questions, not five");
  assert.match(later.querySelector("summary").textContent, /3 more questions can wait/);
  const waiting = [...later.querySelectorAll("[data-dds-question]")];
  assert.equal(waiting.length, 3);
  for (const card of waiting) {
    assert.match(card.querySelector("[data-dds-default]").textContent, /The safe choice, if you don't know/);
  }
  assert.ok($$("[data-dds-question]").some((c) => later.contains(c) && c.dataset.ddsCode === "UNKNOWN_BOOLEAN"), "a question that is not blocking waits too");
});

// --- I don't know -> who would know -------------------------------------------------------------------------------

test("I don't know on a question asks who would know and gives a message ready to copy", async () => {
  await show("question");
  const card = () => $$("[data-dds-question]").find((c) => c.dataset.ddsCode === "KEY_AMBIGUOUS");
  card().querySelector("[data-dds-dont-know]").click();
  await settle(1);
  const panel = card().querySelector("[data-dds-who]");
  assert.deepEqual(
    [...panel.querySelectorAll("[data-dds-who-pick]")].map((b) => b.textContent.trim()),
    ["CRM admin", "Database admin", "Compliance", "Finance", "Campaign manager"],
  );
  assert.ok(!panel.querySelector("[data-dds-who-message]"), "the message waits for a person to be picked");
  panel.querySelector('[data-dds-who-pick="finance"]').click();
  await settle(1);
  const message = $("[data-dds-who-message]").value;
  assert.match(message, /^Hello \(Finance\),/);
  assert.match(message, /The point: /);
  assert.match(message, /Until I hear back: Data Doctor takes the safe choice/);
  assert.equal(document.activeElement.dataset.ddsWhoPick, "finance", "the focus stays where it was");
  assert.equal(posts("answers").length, 0, "nothing answered by guessing");
  $("[data-dds-copy]").click();
  await settle(2);
  assert.ok($("[data-dds-who-message]"), "the message stays for a person to copy by hand if the browser will not");
  $("[data-dds-who-close]").click();
  await settle(1);
  assert.ok(!$("[data-dds-who]"));
});

test("I don't know on a step parks it for the one who would know; Resume brings it back", async () => {
  await show("approve_class");
  const consent = screen("approve_class").view.approve_class.find((a) => a.role === "approver");
  $(`[data-dds-approval="${consent.item_id}"] [data-dds-item-dk]`).click();
  await settle(1);
  assert.ok($(`[data-dds-who] [data-dds-park]`).disabled, "pick who would know first");
  $('[data-dds-who] [data-dds-who-pick="crm_admin"]').click();
  await settle(1);
  const park = $("[data-dds-park]");
  assert.equal(park.textContent.trim(), "Park this step: waiting for CRM admin");
  world.patch = (s) => {
    const it = s.case.items.find((i) => i.item_id === consent.item_id);
    it.state = "rejected";
    it.parked_for = "crm_admin";
    return s;
  };
  park.click();
  await until(() => posts("decisions").length === 1, 2000, "the park");
  const sent = posts("decisions")[0].body;
  assert.deepEqual([sent.item_id, sent.accept, sent.park_for], [consent.item_id, false, "crm_admin"]);
  await until(() => $(`[data-dds-parked="${consent.item_id}"]`), 2000, "the parked step");
  assert.match($(`[data-dds-parked="${consent.item_id}"]`).textContent, /Waiting for CRM admin\. Left out until then\./);
  world.patch = null;
  $(`[data-dds-resume="${consent.item_id}"]`).click();
  await until(() => posts("decisions").length === 2, 2000, "the resume");
  assert.deepEqual([posts("decisions")[1].body.accept, posts("decisions")[1].body.park_for], [false, undefined]);
});

// --- accessibility and words, on the new shapes ---------------------------------------------------------------------

test("the new shapes pass the structural checks, have 40px targets and use no word kept from a marketer", async () => {
  for (const patch of [withLedger(), withLedger({ state: "accepted" })]) {
    await show("approve_class", { patch });
    $(`[data-dds-item-dk]`).click();
    await settle(1);
    $('[data-dds-who-pick="compliance"]').click();
    await settle(1);
    const found = check(app).violations;
    assert.deepEqual(found, [], JSON.stringify(found));
    for (const b of $$("[data-dds] button")) assert.ok(b.classList.contains("btn"), b.outerHTML.slice(0, 80));
    const v = violations(visibleText(app));
    assert.ok(v.ok, describe("plan screen", v));
  }
  await show("question");
  const found = check(app).violations;
  assert.deepEqual(found, [], JSON.stringify(found));
});

// --- one approval ----------------------------------------------------------------------------------------------------

test("a plan that comes back after an approval says what is new, and folds what was approved under 'Already approved'", async () => {
  let ids = [];
  await show("approve", {
    patch: (s) => {
      ids = s.case.items.filter((i) => i.state === "accepted" && i.kind !== "data_check").map((i) => i.item_id);
      s.view.carried = { approved: ids, gone: [], line: "We joined your files into one table; 1 new thing to check." };
      return s;
    },
  });
  assert.ok(ids.length > 0);
  assert.equal($("[data-dds-new-line]").textContent.trim(), "We joined your files into one table; 1 new thing to check.");
  const fold = $("[data-dds-approved]");
  assert.match(fold.querySelector("summary").textContent, /^Already approved \(\d+\)$/);
  for (const id of ids) assert.ok(!$(`[data-dds-plan-section] [data-dds-item="${id}"]`), "an approved item is not drawn in its section again");
  assert.ok($$("[data-dds-approved] [data-dds-item]").length > 0, "they are still there, and can be unticked");
  await show("approve");
  assert.ok(!$("[data-dds-new-line]") && !$("[data-dds-approved]"), "before any approval nothing is folded");
});

test("steps approved last month are folded as 'Same as last month', and what cannot apply is said", async () => {
  let ids = [];
  await show("approve", {
    patch: (s) => {
      ids = s.case.items.filter((i) => i.state === "accepted" && i.kind !== "data_check").map((i) => i.item_id);
      s.view.carried = { approved: ids, gone: [], last_month: true, line: "Last month's steps on ‘spend’ cannot be used: this file has no such column, so they are asked again." };
      return s;
    },
  });
  assert.match($("[data-dds-approved] summary").textContent, /^Same as last month: \d+ steps?$/);
  assert.match($("[data-dds-new-line]").textContent, /‘spend’ cannot be used/);
});
