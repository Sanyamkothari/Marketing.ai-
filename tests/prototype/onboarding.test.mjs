/* Phase 2 §10 B — Setup step 1 becomes a choice, and the four-step
   raw-table onboarding panel behind "Build from raw tables".
   Phase 2 §10 F — the lineage block on the Data page. */
import { test } from "node:test";
import assert from "node:assert/strict";
import { load, go, ev, $, $$, body, click, set, wait } from "./harness.mjs";

/** Open a use case, choose "Build from raw tables", load the sample tables. */
function opened(id = "rca") {
  const dom = load(`#/uc/${id}`);
  click(dom, '.pickcard[data-src="raw"]');
  click(dom, "#ob-sample");
  return dom;
}
const openStep = (dom, n) => click(dom, `[data-ob-open="${n}"]`);

test("step 1 offers two ways in, and the file card is the default", () => {
  const dom = load("#/uc/rca");
  const cards = $$(dom, ".pickcard");
  assert.equal(cards.length, 2);
  assert.equal(cards[0].querySelector(".pt1").textContent, "Upload a prepared file");
  assert.equal(cards[1].querySelector(".pt1").textContent, "Build from raw tables");
  assert.ok(cards[0].classList.contains("on"));
  assert.ok($(dom, "#f-file"), "the prepared-file path is untouched");
  assert.equal($(dom, ".ob"), null, "no panel until the second card is chosen");
});

test("choosing raw tables reveals four collapsible steps below step 1", () => {
  const dom = load("#/uc/rca");
  click(dom, '.pickcard[data-src="raw"]');
  assert.ok($(dom, ".ob"));
  assert.deepEqual($$(dom, ".obstep .ot").map((e) => e.textContent),
    ["Sources", "Mapping", "Features & label", "Build & review"]);
  assert.equal($$(dom, ".obstep.done").length, 0, "nothing is ticked before any work");
  assert.ok($(dom, "#ob-files"), "the drop zone takes real files");
});

test("sources: one row per file, with role, key, time and a coverage badge", () => {
  const dom = opened();
  const rows = $$(dom, ".srcrow");
  assert.equal(rows.length, 6);
  assert.deepEqual(rows.map((r) => r.querySelector(".sname").textContent),
    ["customers.csv", "bills.csv", "payments.csv", "complaints.csv", "usage_daily.csv", "activity_log.csv"]);
  assert.equal(rows[0].querySelector(".srows").textContent, "20,000 rows");
  assert.deepEqual($$(dom, "[data-ob-role]").map((s) => s.value),
    ["Customers", "Bills", "Payments", "Complaints", "Usage", "Activity"]);
  // every event table carries the same coverage number; the spine table does not
  const badges = $$(dom, ".srcrow .cov").map((e) => e.textContent);
  assert.equal(badges[0], "spine table");
  assert.deepEqual(badges.slice(1), Array(5).fill("96% of rows match a customer"));
  // one hint line per role
  assert.equal($$(dom, ".srchint").length, 6);
  assert.match($$(dom, ".srchint")[0].textContent, /spine of the dataset/);
  assert.ok($(dom, ".obstep.done"), "the sources step ticks once the roles are set");
  // every role in the dropdown
  assert.deepEqual([...$(dom, "[data-ob-role]").options].map((o) => o.textContent),
    ["Customers", "Bills", "Payments", "Complaints", "Usage", "Campaign events",
     "Orders", "Activity", "Other event"]);
});

test("mapping: one table per source, with type, samples and a confidence pill", () => {
  const dom = opened();
  openStep(dom, 2);
  assert.equal($$(dom, ".mapcard").length, 6);
  const first = $$(dom, ".mapcard")[0];
  const row = first.querySelector(".maprow");
  assert.equal(row.querySelector(".mcol").textContent, "CUST_ID");
  assert.equal(row.querySelector("[data-ob-map]").value, "customer_id");
  assert.equal(row.querySelector(".mtype").textContent, "ID");
  assert.equal(row.querySelector(".msamp").textContent.split(" · ").length, 3);
  assert.equal(row.querySelector(".conf").textContent, "0.99");
  // "not used" is always available
  assert.equal([...row.querySelector("[data-ob-map]").options][0].textContent, "not used");
  // confidence bands: green >= 0.85, amber below, grey for nothing
  const pills = [...first.querySelectorAll(".conf")];
  const cls = (v) => pills.find((p) => p.textContent === v).className;
  assert.equal(cls("0.94"), "conf");
  assert.equal(cls("0.71"), "conf amber");
  assert.equal(cls("—"), "conf grey");
});

test("mapping: a required-but-unmapped column is highlighted with a plain message", () => {
  const dom = opened();
  openStep(dom, 2);
  const need = $(dom, ".maprow.need");
  assert.ok(need, "complaints.csv has no event date yet");
  assert.equal(need.querySelector(".msamp").textContent, "event_date");
  assert.equal(need.querySelector(".needmsg").textContent,
    "Every event table needs the date the event happened. Without it we cannot tell which rows came before a snapshot.");
  click(dom, "#ob-accept");
  assert.ok($(dom, ".maprow.need"), "accepting suggestions does not invent the missing one");
  assert.equal($$(dom, ".obstep")[1].classList.contains("done"), false);
  set(dom, "[data-ob-need]", "TICKET_TS");
  assert.equal($(dom, ".maprow.need"), null, "mapping it clears the message");
  assert.ok($$(dom, ".obstep")[1].classList.contains("done"), "the mapping step now ticks");
});

test("mapping: the boolean column opens a value map that flips the values", () => {
  const dom = opened();
  openStep(dom, 2);
  assert.equal($(dom, ".vmap"), null);
  click(dom, "#ob-vmap");
  const vmap = $(dom, ".vmap");
  assert.equal(vmap.querySelector(".vt").textContent, "DND_FLAG → marketing_opt_in");
  assert.deepEqual([...vmap.querySelectorAll(".vrow")].map((r) =>
    [...r.querySelectorAll("code")].map((c) => c.textContent)),
    [["Y", "false"], ["N", "true"], ["(empty)", "false"]]);
  assert.equal(vmap.querySelector("#ob-invert").checked, true);
  assert.match(vmap.textContent, /values are flipped/);
});

test("features: a checklist grouped by role, an add form, a label sentence and snapshots", () => {
  const dom = opened();
  openStep(dom, 3);
  assert.deepEqual($$(dom, ".featgrp .fg").map((e) => e.textContent.split(" · ")[0]),
    ["Customers", "Bills", "Payments", "Complaints", "Usage", "Activity"]);
  const feats = $$(dom, "[data-ob-feat]");
  assert.equal(feats.length, 16);
  assert.ok(feats.every((f) => f.checked), "every suggestion starts ticked");
  const row = feats[0].closest(".featrow");
  assert.equal(row.querySelector(".fn").textContent, "tenure_months");
  assert.equal(row.querySelector(".fd").textContent, "Months since activation, at the snapshot.");
  assert.equal(row.querySelector(".fw").textContent, "At snapshot");
  // add-feature form: role, function, column, window, filter
  for (const id of ["#ob-add-role", "#ob-add-fn", "#ob-add-col", "#ob-add-win", "#ob-add-filter"]) {
    assert.ok($(dom, id), `missing ${id}`);
  }
  set(dom, "#ob-add-fn", "Count");
  set(dom, "#ob-add-col", "complaint_type");
  set(dom, "#ob-add-win", "30 days");
  click(dom, "#ob-add");
  assert.equal($$(dom, "[data-ob-feat]").length, 17);
  assert.ok(body(dom).includes("complaint_type_count_30d"));
  // the label is an editable sentence
  assert.match($(dom, ".sentence").textContent, /A customer counts as/);
  assert.equal($(dom, "#ob-lname").value, "churned");
  assert.equal($(dom, "#ob-lwhen").value, "no activity");
  assert.equal($(dom, "#ob-ldays").value, "60");
  assert.ok(body(dom).includes("churned_60d"));
  set(dom, "#ob-ldays", "90");
  assert.ok(body(dom).includes("churned_90d"), "editing the sentence renames the target");
  set(dom, "#ob-ldays", "60");
  // snapshots: single or periodic monthly, capped at 12
  assert.deepEqual([...$(dom, "#ob-snap").options].map((o) => o.textContent),
    ["Single", "Periodic (monthly)"]);
  assert.equal($(dom, "#ob-snapmax").max, "12");
  assert.equal($(dom, "#ob-snapmax").value, "6");
});

test("features: Preview shows five rows, null-rate bars and the positive rate per snapshot", () => {
  const dom = opened();
  openStep(dom, 3);
  assert.equal($(dom, ".nullbars"), null);
  click(dom, "#ob-preview");
  assert.equal($$(dom, ".obbody tbody tr").length, 5);
  assert.equal($$(dom, ".nullbars .nrow").length, 7);
  assert.equal($$(dom, ".posrate .pcol").length, 6);
  assert.deepEqual($$(dom, ".posrate .pv").map((e) => e.textContent),
    ["10.9%", "11.4%", "11.6%", "12.0%", "12.3%", "12.6%"]);
  assert.match(body(dom), /11\.8% of rows are positive across all 6 snapshots/);
});

test("build: a progress list per role, then a report, then Use this dataset", async () => {
  const dom = opened();
  openStep(dom, 4);
  assert.ok($(dom, "#ob-build"));
  click(dom, "#ob-build");
  const steps = $$(dom, ".buildprog li").map((li) => li.textContent.replace(/^\d+/, "").trim());
  assert.deepEqual(steps, [
    "Apply mappings", "Build snapshots",
    "Features: Customers", "Features: Bills", "Features: Payments",
    "Features: Complaints", "Features: Usage", "Features: Activity",
    "Build labels", "Assemble dataset", "Validate", "Write dataset",
  ]);
  await wait(1200);
  assert.equal($$(dom, ".buildprog li.done").length, 12);
  const reports = $$(dom, ".report .rh").map((e) => e.textContent);
  assert.deepEqual(reports, ["Sources", "Snapshots · 11.8% positive overall",
    "Features dropped (2 of 16)", "Checks"]);
  // snapshots carry an inline-SVG mini bar each
  assert.equal($$(dom, ".report svg.mini").length, 6);
  // dropped features name their reason
  assert.match(body(dom), /Empty for 71% of rows\./);
  assert.match(body(dom), /correlation 0\.97/);
  assert.equal($$(dom, ".checks li").length, 6);
  assert.match($$(dom, ".checks li")[0].textContent, /One row per customer per snapshot/);
  assert.ok($(dom, "#ob-use"));
});

test("Use this dataset fills step 2 and collapses the panel", async () => {
  const dom = opened();
  openStep(dom, 4);
  click(dom, "#ob-build");
  await wait(1200);
  click(dom, "#ob-use");
  assert.equal($$(dom, ".obbody").length, 0, "the panel is collapsed");
  assert.equal($(dom, "#f-pk").value, "customer_id + snapshot_date");
  assert.equal($(dom, "#f-target").value, "churned_60d");
  assert.match($(dom, ".ptype .pill").textContent, /^Classification/);
  assert.equal(ev(dom, "STATE['rca'].adv.timeCol"), "snapshot_date");
  assert.equal(ev(dom, "STATE['rca'].adv.split"), "Time-based");
  assert.equal($(dom, "#f-run").disabled, false);
  // the built dataset is 20,000 customers over 6 snapshots, 14 features
  assert.match($(dom, ".preview .pv-head").textContent, /120K rows/);
  assert.match($(dom, ".preview .pv-head").textContent, /Built from 6 tables · 14 features/);
});

test("score mode reads the saved recipe and pre-labels the tables", () => {
  const dom = load("#/uc/rca");
  click(dom, '.seg button[data-mode="score"]');
  const raw = $$(dom, ".pickcard")[1];
  assert.equal(raw.querySelector(".pt1").textContent, "Upload this month's tables");
  assert.match(raw.querySelector(".pt2").textContent, /saved recipe Telecom churn recipe v3/);
  raw.click();
  click(dom, "#ob-sample");
  assert.match(body(dom), /The roles come from the saved recipe/);
  assert.deepEqual($$(dom, "[data-ob-role]").map((s) => s.value),
    ["Customers", "Bills", "Payments", "Complaints", "Usage", "Activity"]);
});

test("Phase 2 §10 F — a run built from raw tables gets a lineage block", async () => {
  const dom = opened();
  go(dom, "#/uc/rca/data");
  assert.equal($(dom, ".lineage"), null, "no lineage before a raw-built run");
  go(dom, "#/uc/rca");
  openStep(dom, 4);
  click(dom, "#ob-build");
  await wait(1200);
  click(dom, "#ob-use");
  $(dom, "#f-setup").dispatchEvent(new dom.window.Event("submit", { cancelable: true }));
  await wait(1200);
  go(dom, "#/uc/rca/data");
  const nodes = $$(dom, ".lnode");
  assert.deepEqual(nodes.map((n) => n.querySelector(".l1").textContent),
    ["Sources", "Mapping", "Recipe", "Dataset", "Run"]);
  assert.deepEqual(nodes.map((n) => n.querySelector(".l2").textContent),
    ["src_8f31c2…", "map_2d90ab…", "rcp_71e4f0…", "ds_c40a19…", "run_5ae803…"]);
  assert.match(body(dom), /sha256 9b1f…c7e2/);
  assert.equal($$(dom, ".larrow").length, 4);
});
