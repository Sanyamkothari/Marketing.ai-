/* The M49 screens - schedules, one schedule's firing history, missed runs, alerts and a scoring run's
   outcomes - in the whole page as index.html loads it, signed in as an Analyst, then a Viewer, then
   an Admin. Every body the fake API answers with is one the real app answered (PB_FIXTURES, written
   by test_production_ops_ui_js.py); every request the page makes is checked for what it sent. */
import { test } from "node:test";
import assert from "node:assert/strict";
import { $, $$, fixture, settle, until } from "../harness.mjs";
import { chooseFile, created, installOps, ok, refused, setField, submit } from "./fake.mjs";

const ids = fixture("ids");
const tokens = {
  "tok-analyst": fixture("me_analyst"),
  "tok-viewer": fixture("me_viewer"),
  "tok-admin": fixture("me_admin"),
};
let measured = false;
let campaignMeasured = false; // measured on the run's Campaign results page (ui/modules/uplift)
let noSchedules = false;
// Plan M M173: what the value view (`GET /pilot/roi/{run}`) says; `null` is a 404, as before, so the screens ask
// for outcomes and campaign results the old way.
let roi = null;
let runBody = fixture("run");
const firings = fixture("firings_drift").firings;

const { w, calls, forms } = installOps({
  tokens,
  token: "tok-analyst",
  hash: "#/monitoring/schedules",
  table: [
    ["GET", "/industries", () => ok(fixture("industries"))],
    ["GET", "/schedules", () => ok(noSchedules ? fixture("schedules_none") : fixture("schedules"))],
    ["POST", "/schedules", () => created(fixture("schedule_score"))],
    ["POST", "/schedules/retraining/sync", () => ok(fixture("retraining_sync"))],
    [
      "GET",
      "/schedules/{id}",
      (_r, p) => ok(p.id === ids.drift_schedule ? fixture("schedule_drift") : fixture("schedule_score")),
    ],
    ["PATCH", "/schedules/{id}", () => ok(fixture("schedule_edited"))],
    ["DELETE", "/schedules/{id}", () => ({ status: 204, body: null })],
    ["POST", "/schedules/{id}/disable", () => ok(fixture("schedule_paused"))],
    ["POST", "/schedules/{id}/enable", () => ok(fixture("schedule_drift"))],
    ["POST", "/schedules/{id}/fire", () => created(fixture("fired"))],
    [
      "GET",
      "/schedules/{id}/firings",
      (r) => ok({ firings: firings.filter((f) => !r.query.status || f.status === r.query.status) }),
    ],
    ["GET", "/monitoring/missed-firings", () => ok(fixture("missed"))],
    [
      "GET",
      "/monitoring/alerts",
      (r) => ok(r.query.unacknowledged_only === "true" ? fixture("alerts_open") : fixture("alerts_all")),
    ],
    ["POST", "/monitoring/alerts/{id}/acknowledge", () => ok(fixture("alert_acknowledged"))],
    ["GET", "/runs", () => ok(fixture("runs"))],
    ["GET", "/runs/{id}", () => ok(runBody)],
    ["GET", "/pilot/roi/{id}", () => (roi ? ok(roi) : refused(404, { detail: { code: "RUN_NOT_FOUND", message: "No such run." } }))],
    [
      "GET",
      "/runs/{id}/outcomes",
      () => (measured ? ok(fixture("outcome_report")) : refused(404, fixture("outcomes_missing"))),
    ],
    [
      "GET",
      "/runs/{id}/incrementality-input",
      () => (measured ? ok(fixture("incrementality")) : refused(404, fixture("incrementality_missing"))),
    ],
    ["POST", "/runs/{id}/outcomes", () => ((measured = true), created(fixture("outcome_report")))],
    [
      "GET",
      "/runs/{id}/campaign-results",
      () => (campaignMeasured ? ok(fixture("campaign_results")) : refused(404, fixture("campaign_results_missing"))),
    ],
    ["GET", "/privacy/runs/{id}/consent-report", () => refused(404, fixture("consent_report_missing"))],
    ["GET", "/audit/events", () => ok({ events: [], total: 0, offset: 0, limit: 50 })],
  ],
});

await import("../../../../../ui/app.js");
await import("../../../../../ui/modules/production/index.js");
const session = await import("../../../../../ui/modules/production/session.js");
// Plan H M82 (DEC-1113): the links that were in the top bar are on the Settings page; this is what
// that page offers to the person signed in now, drawn by its own pure builder.
const { settingsHtml } = await import("../../../../../ui/modules/simple/pages.js");
const offers = (href) => settingsHtml({ can: (m, p) => session.can(m, p), status: "signed-in" }).includes(`href="${href}"`);

const text = () => ($("#app") || {}).textContent || "";
const last = (method, path) => calls.filter((c) => c.method === method && c.path === path).pop();
const rowOf = (id) => $(`tr[data-schedule="${id}"]`);
const heading = () => ($("main h1") || {}).textContent || "";
const cardTitles = () => $$("main .card h3").map((h) => h.textContent).join(" | ");
/** The breadcrumb as [label, href] pairs, href null for the screen itself (docs/UI_AUDIT.md §8.4 item 12). */
const crumbTrail = () => $$("#app .crumbs a, #app .crumbs .cur").map((e) => [e.textContent, e.getAttribute("href")]);
const SETTINGS = ["Settings", "#/settings"];
const RESULTS = ["Results", "#/results"];

test("the Analyst's schedule list shows every schedule with its cadence, and Settings links here", async () => {
  await until(() => $$("tr[data-schedule]").length === fixture("schedules").schedules.length, 3000, "the schedules");
  assert.ok(offers("#/monitoring/schedules"), "Schedules on the Settings page");
  assert.deepEqual(crumbTrail(), [SETTINGS, ["Schedules", null]], "Settings › Schedules, not Home › Model health › Schedules");
  assert.match(rowOf(ids.score_schedule).textContent, /Monthly · 02:00 Asia\/Kolkata/);
  assert.match(rowOf(ids.drift_schedule).textContent, /0 \* \* \* \* · UTC/);
  assert.equal($("#pb-schedule-create-submit").disabled, false);
  assert.equal($$(".pb-why").length, 0, "an Analyst is refused nothing here");
  assert.equal(last("GET", "/schedules").auth, "Bearer tok-analyst");
});

test("a new schedule sends only what was chosen; the cron line only for a custom cadence", async () => {
  const form = $("#pb-schedule-create");
  const useCase = form.elements.namedItem("use_case_id");
  assert.equal(useCase.tagName, "SELECT", "use cases come from GET /industries");
  const chosen = useCase.options[0].value;
  setField(w, form, "kind", "score");
  setField(w, form, "preset", "monthly");
  setField(w, form, "cron", "5 5 5 * *"); // typed, then abandoned by choosing a preset: never sent
  setField(w, form, "onboarding_spec_id", "sp_sched");
  submit(w, form);
  await until(() => $(".pb-ok"), 2000, "the confirmation");
  assert.deepEqual(last("POST", "/schedules").body, {
    use_case_id: chosen,
    kind: "score",
    cadence: "monthly",
    timezone: "Asia/Kolkata",
    parameters: { onboarding_spec_id: "sp_sched" },
    enabled: true,
  });
  assert.match($(".pb-ok").textContent, /Created Score new data/);

  const again = $("#pb-schedule-create");
  setField(w, again, "kind", "drift_check");
  setField(w, again, "preset", "custom");
  setField(w, again, "cron", "0 6 1 * *");
  setField(w, again, "timezone", "UTC");
  setField(w, again, "client_id", "c_demo_telco_1");
  setField(w, again, "enabled", false);
  submit(w, again);
  await settle();
  const body = last("POST", "/schedules").body;
  assert.equal(body.cadence, "0 6 1 * *");
  assert.equal(body.timezone, "UTC");
  assert.equal(body.client_id, "c_demo_telco_1");
  assert.equal(body.enabled, false);
  assert.deepEqual(body.parameters, {});
});

test("Run now shows the firing as recorded; Pause and Delete (asked twice) call their routes", async () => {
  rowOf(ids.drift_schedule).querySelector("[data-fire]").click();
  await until(() => /Ran now:/.test(text()), 2000, "the firing");
  assert.equal(last("POST", `/schedules/${ids.drift_schedule}/fire`).auth, "Bearer tok-analyst");
  assert.equal($(".pb-ok .pill").dataset.status, fixture("fired").status);
  assert.equal($(".pb-ok [data-code]").dataset.code, fixture("fired").result_code, "the code, in words");
  assert.match(text(), new RegExp(fixture("fired").result_code));

  rowOf(ids.drift_schedule).querySelector("[data-disable]").click();
  await until(() => /Paused: it will not fire/.test(text()), 2000, "the pause");
  assert.ok(last("POST", `/schedules/${ids.drift_schedule}/disable`));

  const deletes = () => calls.filter((c) => c.method === "DELETE").length;
  const del = rowOf(ids.score_schedule).querySelector("[data-delete]");
  assert.deepEqual([...del.classList].sort(), ["btn", "pb-quiet-bad", "quiet", "sm"], "quiet, red text, not filled");
  del.click();
  await settle(2);
  assert.equal(deletes(), 0, "the first click only asks");
  assert.match(rowOf(ids.score_schedule).textContent, /Delete it and its history\?/);
  const yes = rowOf(ids.score_schedule).querySelector("[data-delete-yes]");
  assert.ok(yes.classList.contains("danger") && yes.classList.contains("confirm"), "only the confirm step is filled red");
  rowOf(ids.score_schedule).querySelector("[data-delete-yes]").click();
  await until(() => deletes() === 1, 2000, "the delete");
  assert.equal(last("DELETE", `/schedules/${ids.score_schedule}`).auth, "Bearer tok-analyst");
});

test("Sync retraining schedules reports what the server created, updated and removed", async () => {
  $("#pb-retraining-sync").click();
  await until(() => /recipe\(s\):/.test(text()), 2000, "the sync result");
  const sync = fixture("retraining_sync");
  assert.match(text(), new RegExp(`${sync.created.length} created, ${sync.updated.length} updated, ${sync.removed.length} removed`));
});

test("one schedule: its firing history with the missed slots, filtered by status", async () => {
  w.location.hash = `#/monitoring/schedules/${ids.drift_schedule}`;
  await until(() => $("#pb-firings-filter") && $$("tbody tr").length === firings.length, 3000, "the history");
  assert.deepEqual(crumbTrail().slice(0, 2), [SETTINGS, ["Schedules", "#/monitoring/schedules"]]);
  assert.equal(crumbTrail().length, 3, "then the schedule itself");
  assert.equal($$('.pill.bad[data-status="missed"]').filter((p) => p.textContent === "Missed").length, 5);
  const filter = $("#pb-firings-filter");
  setField(w, filter, "status", "missed", { change: true });
  await until(() => last("GET", `/schedules/${ids.drift_schedule}/firings`).query.status === "missed", 2000, "the filter");
  await until(() => $$("tbody tr").length === 5, 2000, "only the missed firings");
  assert.deepEqual(last("GET", `/schedules/${ids.drift_schedule}/firings`).query, { status: "missed", limit: "200" });
});

test("editing a schedule sends its whole cadence, zone and parameters", async () => {
  w.location.hash = `#/monitoring/schedules/${ids.score_schedule}`;
  await until(() => /^Score new data ·/.test(heading()) && $("#pb-schedule-edit"), 3000, "the edit form");
  const form = $("#pb-schedule-edit");
  assert.equal(form.elements.namedItem("preset").value, "monthly");
  assert.equal(form.elements.namedItem("onboarding_spec_id").value, "sp_sched");
  setField(w, form, "preset", "custom");
  setField(w, form, "cron", "0 6 1 * *");
  submit(w, form);
  await until(() => /Saved\. Next run/.test(text()), 2000, "the save");
  assert.deepEqual(last("PATCH", `/schedules/${ids.score_schedule}`).body, {
    cadence: "0 6 1 * *",
    timezone: "Asia/Kolkata",
    parameters: { onboarding_spec_id: "sp_sched" },
  });
  assert.match(text(), /0 6 1 \* \* · Asia\/Kolkata/, "the answer is drawn");
});

test("missed runs lists every due slot nobody fired", async () => {
  w.location.hash = "#/monitoring/missed";
  await until(() => /^Missed runs ·/.test(cardTitles()), 3000, "the missed slots");
  assert.deepEqual(crumbTrail(), [SETTINGS, ["Model health", "#/monitoring/alerts"], ["Missed runs", null]]);
  assert.equal($$("tbody tr").length, fixture("missed").firings.length);
  assert.match(text(), /SCHEDULE_MISSED/);
  assert.ok($(`a[href="#/monitoring/schedules/${ids.drift_schedule}"]`));
});

test("alerts open on the open ones; acknowledging says who, and the list re-reads", async () => {
  w.location.hash = "#/monitoring/alerts";
  const open = fixture("alerts_open").alerts;
  await until(() => heading() === "Alerts" && $$("tr[data-alert]").length === open.length, 3000, "the open alerts");
  assert.equal(last("GET", "/monitoring/alerts").query.unacknowledged_only, "true");
  const drop = open.find((a) => a.kind === "performance_drop");
  assert.match($(`tr[data-alert="${drop.alert_id}"]`).textContent, /The model did worse on real outcomes/);
  $(`tr[data-alert="${drop.alert_id}"] [data-ack]`).click();
  await until(() => /Marked as being dealt with by you/.test(text()), 2000, "the acknowledgement");
  assert.ok(last("POST", `/monitoring/alerts/${drop.alert_id}/acknowledge`));
  assert.match($(".pb-ok").textContent, new RegExp(fixture("alert_acknowledged").acknowledged_by));

  const filters = $("#pb-alert-filters");
  setField(w, filters, "unacknowledged_only", false, { change: true });
  await until(() => $$("tr[data-alert]").length === fixture("alerts_all").alerts.length, 2000, "every alert");
  assert.equal(last("GET", "/monitoring/alerts").query.unacknowledged_only, undefined, "false is left out");
});

test("the Campaigns list links each run to its Campaign results page and reads Measured once it was measured", async () => {
  const run = fixture("run").run;
  const row = () => $(`tr[data-run="${ids.run_id}"]`);
  const campaignReads = () => calls.filter((c) => c.method === "GET" && /\/campaign-results$/.test(c.path));
  w.location.hash = "#/monitoring/runs";
  await until(() => row() && row().querySelector("[data-status]"), 3000, "the run and its results");
  const link = row().querySelector("a[data-campaign]");
  assert.equal(link.getAttribute("href"), `#/campaign/${run.use_case_id}/${ids.run_id}`);
  assert.equal(link.textContent, run.use_case_name || run.use_case_id);
  const pill = () => row().querySelector("[data-status]");
  assert.equal(pill().dataset.status, "not_added");
  assert.equal(pill().textContent, "Not added yet");
  const action = row().querySelector(`a[href="#/monitoring/runs/${ids.run_id}"]`);
  assert.equal(action.textContent, "Add outcomes");
  assert.ok(campaignReads().some((c) => c.path === `/runs/${ids.run_id}/campaign-results`));
  const done = fixture("runs")
    .runs.filter((r) => r.state === "done")
    .slice(0, 20)
    .map((r) => `/runs/${r.run_id}/campaign-results`);
  assert.ok(campaignReads().every((c) => done.includes(c.path)), "only the 20 newest finished runs are asked");

  campaignMeasured = true; // measured on the Campaign results page, no outcomes added here
  w.location.hash = "#/monitoring/alerts";
  await until(() => heading() === "Alerts", 3000, "another screen");
  w.location.hash = "#/monitoring/runs";
  await until(() => row() && pill() && pill().dataset.status === "measured", 3000, "Measured");
  assert.equal(pill().textContent, "Measured");
  assert.equal(row().querySelector(`a[href="#/monitoring/runs/${ids.run_id}"]`).textContent, "Add outcomes");
  campaignMeasured = false;
});

test("a finished scoring run: no outcomes yet, then an upload measures it against the test score", async () => {
  w.location.hash = "#/monitoring/runs";
  await until(() => heading() === "Campaigns" && /^Campaigns ·/.test(cardTitles()), 3000, "the scored lists");
  assert.deepEqual(crumbTrail(), [RESULTS, ["Campaigns", null]], "under Results, where the bar marks it");
  assert.ok($(`a[href="#/monitoring/runs/${ids.run_id}"]`));
  assert.deepEqual(last("GET", "/runs").query, { mode: "score", limit: "100" });
  w.location.hash = `#/monitoring/runs/${ids.run_id}`;
  await until(() => $("#pb-outcomes"), 3000, "the upload form");
  assert.deepEqual(crumbTrail().slice(0, 2), [RESULTS, ["Campaigns", "#/monitoring/runs"]]);
  assert.match(text(), /No outcomes have been added for this run yet\./);
  assert.equal($$(".apierr").length, 0, "a report not written yet is not an error");

  const form = $("#pb-outcomes");
  chooseFile(form.elements.namedItem("file"), "outcomes.csv", "entity_key,churn_next_60d\nQZUI000000,yes\n");
  setField(w, form, "outcome_column", "churn_next_60d");
  submit(w, form);
  await until(() => /Performance on real outcomes ·/.test(text()), 3000, "the report");
  const sent = forms.pop();
  assert.match(sent.url, new RegExp(`/runs/${ids.run_id}/outcomes$`));
  assert.equal(sent.form.get("file").name, "outcomes.csv");
  assert.equal(sent.form.get("outcome_column"), "churn_next_60d");

  const report = fixture("outcome_report");
  assert.match(text(), new RegExp(`Performance on real outcomes · ${report.metric_label}`));
  assert.match($(".apierr").textContent, /PERFORMANCE_DROP/);
  assert.ok($('.apierr a[href="#/monitoring/alerts"]'));
  assert.match(text(), /Campaign effect \(contacted vs control group\)/);
  assert.match(text(), /Difference vs control group/);
  assert.equal($$("tbody tr").some((tr) => /· control/.test(tr.textContent)), true, "by band");
});

test("a Viewer reads everything but every change is refused in place, and a refused click sends nothing", async () => {
  session.storeToken("tok-viewer", null);
  await session.loadMe();
  w.location.hash = "#/monitoring/schedules";
  await until(() => $$("tr[data-schedule]").length > 0 && $("#pb-schedule-create-submit"), 3000, "the list");
  assert.equal($("#pb-schedule-create-submit").disabled, true);
  const reasons = $$(".pb-why").map((n) => n.textContent);
  assert.ok(reasons.includes(fixture("schedule_refused").detail.message), "the server's own sentence");
  assert.ok(reasons.includes("Only an Analyst can run a schedule now."));
  const before = calls.filter((c) => c.method !== "GET").length;
  rowOf(ids.drift_schedule).querySelector("[data-fire]").click();
  submit(w, $("#pb-schedule-create"));
  await settle(3);
  assert.equal(calls.filter((c) => c.method !== "GET").length, before, "nothing was sent");

  w.location.hash = "#/monitoring/alerts";
  await until(() => $$("tr[data-alert]").length > 0, 3000, "the alerts");
  assert.ok($$(".pb-why").some((n) => n.textContent === "Only an Analyst can acknowledge an alert."));
});

test("an Admin is not an Analyst: the Privacy link is offered, running a schedule is not", async () => {
  session.storeToken("tok-admin", null);
  await session.loadMe();
  assert.ok(offers("#/privacy/consent"), "Privacy on the Settings page");
  w.location.hash = "#/monitoring/runs";
  w.location.hash = "#/monitoring/schedules";
  await until(() => $$("tr[data-schedule]").length > 0 && $$(".pb-why").length > 0, 3000, "the list");
  assert.ok($$(".pb-why").some((n) => n.textContent === "Only an Analyst can run a schedule now."));
});

test("with nothing scheduled: one primary (the open form's Create schedule), no More actions, the sync still reachable", async () => {
  session.storeToken("tok-analyst", null);
  await session.loadMe();
  noSchedules = true;
  w.location.hash = "#/monitoring/alerts";
  await until(() => heading() === "Alerts", 3000, "another screen");
  w.location.hash = "#/monitoring/schedules";
  await until(() => /Nothing runs on its own yet\./.test(text()), 3000, "the empty state");
  const primaries = $$("main .btn.primary");
  assert.equal(primaries.length, 1, "one primary");
  assert.equal(primaries[0].id, "pb-schedule-create-submit");
  const open = $("#pb-schedule-new-open");
  assert.ok(open.classList.contains("secondary"));
  assert.equal(open.getAttribute("aria-expanded"), "true", "the form it controls is open");
  assert.equal($("main .pb-more-actions"), null);
  // Still reachable with nothing scheduled (no capability removed), as a quiet action, not a primary.
  const sync = $("#pb-retraining-sync");
  assert.ok(sync, "Update retraining schedules now stays reachable");
  assert.ok(sync.classList.contains("quiet") && !sync.classList.contains("primary"));
  assert.equal(/More actions/.test(text()), false);
  noSchedules = false;
});

// --- Plan M M173 (DEC-1603): ask only for what can exist --------------------------------------------------------
//
// The browser logs every 404 as a console error however the page handles it. The value view says whether anything
// was recorded for a run, and the run's own record lists the files its flow wrote, so a run nothing was recorded
// for draws its empty page without a string of requests that can only answer 404.

const reads = (re) => calls.filter((c) => c.method === "GET" && re.test(c.path));
const inDays = (n) => new Date(Date.now() + n * 86400000).toISOString().slice(0, 10);
// Not measured, no source, and the outcome window still ahead: ingestion refuses it, so no outcomes can exist.
const NOTHING = { run_id: ids.run_id, status: "not_measured", source: null, campaign_id: null, results_available_on: inDays(30) };
// The same answer the value view gives for a run whose outcomes WERE recorded but have no matched control rows.
const NOT_PRICED = { ...NOTHING, results_available_on: inDays(-30) };

test("the Campaigns list asks neither for outcomes nor for campaign results of a run the value view says is still maturing", async () => {
  roi = NOTHING;
  w.location.hash = "#/monitoring/alerts";
  await until(() => heading() === "Alerts", 3000, "another screen");
  calls.length = 0;
  w.location.hash = "#/monitoring/runs";
  const row = () => $(`tr[data-run="${ids.run_id}"]`);
  await until(() => row() && row().querySelector("[data-status]"), 3000, "the run and its results");
  assert.equal(row().querySelector("[data-status]").dataset.status, "not_added");
  assert.equal(row().querySelector(`a[href="#/monitoring/runs/${ids.run_id}"]`).textContent, "Add outcomes");
  assert.ok(reads(new RegExp(`/pilot/roi/${ids.run_id}$`)).length > 0, "the value view was asked");
  assert.equal(reads(/\/runs\/[^/]+\/(outcomes|campaign-results)$/).length, 0, "no request that could only answer 404");
  roi = null;
});

test("a run whose outcomes were added here reads Measured and View results without asking for either file", async () => {
  roi = { run_id: ids.run_id, status: "measured", source: "outcome_ingestion", campaign_id: null };
  w.location.hash = "#/monitoring/alerts";
  await until(() => heading() === "Alerts", 3000, "another screen");
  calls.length = 0;
  w.location.hash = "#/monitoring/runs";
  const row = () => $(`tr[data-run="${ids.run_id}"]`);
  await until(() => row() && row().querySelector("[data-status]"), 3000, "the run and its results");
  assert.equal(row().querySelector("[data-status]").dataset.status, "measured");
  assert.equal(row().querySelector(`a[href="#/monitoring/runs/${ids.run_id}"]`).textContent, "View results");
  assert.equal(reads(/\/runs\/[^/]+\/(outcomes|campaign-results)$/).length, 0);
  roi = null;
});

test("a run measured through one of its campaigns reads Measured without asking for the run's own files", async () => {
  roi = { run_id: ids.run_id, status: "measured", source: "incrementality_report", campaign_id: "c_20261011_abcdef01" };
  w.location.hash = "#/monitoring/alerts";
  await until(() => heading() === "Alerts", 3000, "another screen");
  calls.length = 0;
  w.location.hash = "#/monitoring/runs";
  const row = () => $(`tr[data-run="${ids.run_id}"]`);
  await until(() => row() && row().querySelector("[data-status]"), 3000, "the run and its results");
  assert.equal(row().querySelector("[data-status]").dataset.status, "measured", "the campaign measured it");
  assert.equal(reads(/\/runs\/[^/]+\/(outcomes|campaign-results)$/).length, 0);
  roi = null;
});

test("a run the value view cannot speak for is asked about the old way", async () => {
  roi = null; // 404: the value view is not known
  w.location.hash = "#/monitoring/alerts";
  await until(() => heading() === "Alerts", 3000, "another screen");
  calls.length = 0;
  w.location.hash = "#/monitoring/runs";
  await until(() => $(`tr[data-run="${ids.run_id}"] [data-status]`), 3000, "the run and its results");
  assert.ok(reads(new RegExp(`/runs/${ids.run_id}/outcomes$`)).length > 0);
  assert.ok(reads(new RegExp(`/runs/${ids.run_id}/campaign-results$`)).length > 0);
});

test("one run still maturing: the empty page, and no request for outcomes, the control-group input or a consent report it cannot have", async () => {
  roi = NOTHING;
  const record = fixture("run").run;
  assert.ok(!("consent_report.json" in (record.artefacts || {})), "this run was not gated by a consent ledger");
  w.location.hash = "#/monitoring/alerts";
  await until(() => heading() === "Alerts", 3000, "another screen");
  calls.length = 0;
  w.location.hash = `#/monitoring/runs/${ids.run_id}`;
  await until(() => $("#pb-outcomes"), 3000, "the upload form");
  assert.match(text(), /No outcomes have been added for this run yet\./);
  assert.equal($$(".apierr").length, 0);
  assert.equal(reads(/\/runs\/[^/]+\/(outcomes|incrementality-input)$/).length, 0);
  assert.equal(reads(/\/privacy\/runs\//).length, 0, "no consent report: the run's own record lists none");
  roi = null;
});

test("a run whose record lists a consent report asks for it; one the value view says has outcomes asks for them", async () => {
  roi = { run_id: ids.run_id, status: "measured", source: "outcome_ingestion", campaign_id: null };
  const record = fixture("run");
  runBody = { ...record, run: { ...record.run, artefacts: { ...(record.run.artefacts || {}), "consent_report.json": "k" } } };
  w.location.hash = "#/monitoring/alerts";
  await until(() => heading() === "Alerts", 3000, "another screen");
  calls.length = 0;
  w.location.hash = `#/monitoring/runs/${ids.run_id}`;
  await until(() => reads(/\/privacy\/runs\/[^/]+\/consent-report$/).length > 0, 3000, "the consent report to be asked for");
  await settle(8);
  assert.ok(reads(new RegExp(`/runs/${ids.run_id}/outcomes$`)).length > 0, "outcomes exist: they are asked for");
  runBody = record;
  roi = null;
});

test("not measured with no source but a window already past: recorded outcomes are still asked for and shown (no control group)", async () => {
  // compute_roi answers not_measured/null for a run with a saved outcome report but no matched control rows;
  // that must not read as "nothing recorded".
  roi = NOT_PRICED;
  measured = true; // the fake `GET /runs/{id}/outcomes` now has a report
  w.location.hash = "#/monitoring/alerts";
  await until(() => heading() === "Alerts", 3000, "another screen");
  calls.length = 0;
  w.location.hash = "#/monitoring/runs";
  const row = () => $(`tr[data-run="${ids.run_id}"]`);
  await until(() => row() && row().querySelector("[data-status]"), 3000, "the run and its results");
  assert.equal(row().querySelector("[data-status]").dataset.status, "measured");
  assert.equal(row().querySelector(`a[href="#/monitoring/runs/${ids.run_id}"]`).textContent, "View results");
  assert.ok(reads(new RegExp(`/runs/${ids.run_id}/outcomes$`)).length > 0, "the report was asked for");
  calls.length = 0;
  w.location.hash = `#/monitoring/runs/${ids.run_id}`;
  await until(() => reads(new RegExp(`/runs/${ids.run_id}/outcomes$`)).length > 0, 3000, "the outcomes to be asked for");
  await settle(8);
  assert.doesNotMatch(text(), /No outcomes have been added for this run yet\./);
  measured = false;
  roi = null;
});

test("not measured with no source and a window already past, nothing stored: the plain empty page", async () => {
  roi = NOT_PRICED;
  measured = false;
  w.location.hash = "#/monitoring/alerts";
  await until(() => heading() === "Alerts", 3000, "another screen");
  calls.length = 0;
  w.location.hash = `#/monitoring/runs/${ids.run_id}`;
  await until(() => $("#pb-outcomes"), 3000, "the upload form");
  assert.match(text(), /No outcomes have been added for this run yet\./);
  assert.equal(reads(/\/runs\/[^/]+\/incrementality-input$/).length, 0, "no report, so no control-group input is asked for");
  roi = null;
});

test("not measured with no source and no outcome window at all (the use case defines no horizon): nothing can exist, nothing is asked for", async () => {
  // Ingestion refuses a run whose window is unknown (OUTCOME_WINDOW_UNKNOWN), so there is no report to hide.
  roi = { ...NOTHING, results_available_on: null };
  w.location.hash = "#/monitoring/alerts";
  await until(() => heading() === "Alerts", 3000, "another screen");
  calls.length = 0;
  w.location.hash = "#/monitoring/runs";
  const row = () => $(`tr[data-run="${ids.run_id}"]`);
  await until(() => row() && row().querySelector("[data-status]"), 3000, "the run and its results");
  assert.equal(row().querySelector("[data-status]").dataset.status, "not_added");
  assert.equal(reads(/\/runs\/[^/]+\/(outcomes|campaign-results)$/).length, 0);
  roi = null;
});
