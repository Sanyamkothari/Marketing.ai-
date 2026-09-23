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
const firings = fixture("firings_drift").firings;

const { w, calls, forms } = installOps({
  tokens,
  token: "tok-analyst",
  hash: "#/monitoring/schedules",
  table: [
    ["GET", "/industries", () => ok(fixture("industries"))],
    ["GET", "/schedules", () => ok(fixture("schedules"))],
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
    ["GET", "/runs/{id}", () => ok(fixture("run"))],
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
    ["GET", "/privacy/runs/{id}/consent-report", () => refused(404, fixture("consent_report_missing"))],
    ["GET", "/audit/events", () => ok({ events: [], total: 0, offset: 0, limit: 50 })],
  ],
});

await import("../../../../../ui/app.js");
await import("../../../../../ui/modules/production/index.js");
const session = await import("../../../../../ui/modules/production/session.js");

const text = () => ($("#app") || {}).textContent || "";
const last = (method, path) => calls.filter((c) => c.method === method && c.path === path).pop();
const rowOf = (id) => $(`tr[data-schedule="${id}"]`);
const heading = () => ($("main h1") || {}).textContent || "";
const cardTitles = () => $$("main .card h3").map((h) => h.textContent).join(" | ");

test("the Analyst's schedule list shows every schedule with its cadence, and the bar links here", async () => {
  await until(() => $$("tr[data-schedule]").length === fixture("schedules").schedules.length, 3000, "the schedules");
  assert.ok($('#pb-bar a[href="#/monitoring/schedules"]'), "Monitoring in the user bar");
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
  await until(() => /Fired:/.test(text()), 2000, "the firing");
  assert.equal(last("POST", `/schedules/${ids.drift_schedule}/fire`).auth, "Bearer tok-analyst");
  assert.match(text(), new RegExp(`Fired: ${fixture("fired").status}`));
  assert.match(text(), new RegExp(fixture("fired").result_code));

  rowOf(ids.drift_schedule).querySelector("[data-disable]").click();
  await until(() => /Paused: it will not fire/.test(text()), 2000, "the pause");
  assert.ok(last("POST", `/schedules/${ids.drift_schedule}/disable`));

  const deletes = () => calls.filter((c) => c.method === "DELETE").length;
  rowOf(ids.score_schedule).querySelector("[data-delete]").click();
  await settle(2);
  assert.equal(deletes(), 0, "the first click only asks");
  assert.match(rowOf(ids.score_schedule).textContent, /Delete it and its history\?/);
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
  assert.equal($$(".pill.bad").filter((p) => p.textContent === "missed").length, 5);
  const filter = $("#pb-firings-filter");
  setField(w, filter, "status", "missed", { change: true });
  await until(() => last("GET", `/schedules/${ids.drift_schedule}/firings`).query.status === "missed", 2000, "the filter");
  await until(() => $$("tbody tr").length === 5, 2000, "only the missed firings");
  assert.deepEqual(last("GET", `/schedules/${ids.drift_schedule}/firings`).query, { status: "missed", limit: "200" });
});

test("editing a schedule sends its whole cadence, zone and parameters", async () => {
  w.location.hash = `#/monitoring/schedules/${ids.score_schedule}`;
  await until(() => /Score new data ·/.test(cardTitles()) && $("#pb-schedule-edit"), 3000, "the edit form");
  const form = $("#pb-schedule-edit");
  assert.equal(form.elements.namedItem("preset").value, "monthly");
  assert.equal(form.elements.namedItem("onboarding_spec_id").value, "sp_sched");
  setField(w, form, "preset", "custom");
  setField(w, form, "cron", "0 6 1 * *");
  submit(w, form);
  await until(() => /Saved; next due/.test(text()), 2000, "the save");
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
  assert.match($(`tr[data-alert="${drop.alert_id}"]`).textContent, /Performance drop/);
  $(`tr[data-alert="${drop.alert_id}"] [data-ack]`).click();
  await until(() => /Acknowledged by/.test(text()), 2000, "the acknowledgement");
  assert.ok(last("POST", `/monitoring/alerts/${drop.alert_id}/acknowledge`));
  assert.match($(".pb-ok").textContent, new RegExp(fixture("alert_acknowledged").acknowledged_by));

  const filters = $("#pb-alert-filters");
  setField(w, filters, "unacknowledged_only", false, { change: true });
  await until(() => $$("tr[data-alert]").length === fixture("alerts_all").alerts.length, 2000, "every alert");
  assert.equal(last("GET", "/monitoring/alerts").query.unacknowledged_only, undefined, "false is left out");
});

test("a finished scoring run: no outcomes yet, then an upload measures it against the test score", async () => {
  w.location.hash = "#/monitoring/runs";
  await until(() => /^Scoring runs ·/.test(cardTitles()), 3000, "the scoring runs");
  assert.ok($(`a[href="#/monitoring/runs/${ids.run_id}"]`));
  assert.deepEqual(last("GET", "/runs").query, { mode: "score", limit: "100" });
  w.location.hash = `#/monitoring/runs/${ids.run_id}`;
  await until(() => $("#pb-outcomes"), 3000, "the upload form");
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
  assert.match(text(), /Incrementality input/);
  assert.match(text(), /Treated minus control/);
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
  assert.ok($('#pb-bar a[href="#/privacy/consent"]'));
  w.location.hash = "#/monitoring/runs";
  w.location.hash = "#/monitoring/schedules";
  await until(() => $$("tr[data-schedule]").length > 0 && $$(".pb-why").length > 0, 3000, "the list");
  assert.ok($$(".pb-why").some((n) => n.textContent === "Only an Analyst can run a schedule now."));
});
