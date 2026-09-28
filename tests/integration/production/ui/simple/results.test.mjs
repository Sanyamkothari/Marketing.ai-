/* Plan H M82: the Results page (`#/results`) in the whole page - every run newest first with its use
   case, what it did, its status, its key outcome and a link to the run; "No runs yet" on an empty
   installation; one notice when models wait for approval - and the links a finished run's results
   carry to the screens that left the menu (Model health, the Schedule, the report). */
import { test } from "node:test";
import assert from "node:assert/strict";
import { $, $$, settle, until } from "../harness.mjs";
import { fixture, installWholePage } from "./fake.mjs";

const world = { runs: fixture("runs_empty"), waiting: null };
const { w } = await installWholePage({ hash: "#/results", world });
const router = await import("../../../../../ui/modules/router.js");
const { resultsHtml } = await import("../../../../../ui/modules/simple/pages.js");

const rows = () => $$("[data-results] tbody tr");
const open = async (hash) => {
  w.location.hash = "#/";
  await settle(3);
  w.location.hash = hash;
};

test("an empty installation: Results says there are no runs yet and points to Home", async () => {
  await until(() => $("[data-results-empty]"), 3000, "the empty state");
  assert.equal($("main h1").textContent, "Results");
  assert.match($("[data-results-empty]").textContent, /No runs yet/);
  assert.ok($('[data-results-empty] a[href="#/"]'), "one way to start");
  assert.equal($("[data-approvals]"), null, "nothing waits");
  assert.equal($("#pb-bar .tn-item.on").textContent.trim(), "Results");
});

test("every run, newest first, with its use case, what it did, its status and its outcome", async () => {
  world.runs = fixture("runs");
  await open("#/results");
  await until(() => rows().length === 3, 3000, "three runs");
  const runs = fixture("runs").runs;
  assert.deepEqual(
    rows().map((tr) => tr.querySelector("a[data-run]").getAttribute("data-run")),
    runs.map((r) => r.run_id),
    "in the API's order, which is newest first",
  );
  const cells = rows().map((tr) => [...tr.querySelectorAll("td")].map((td) => td.textContent.trim()));
  assert.equal(cells[0][0], "Telco Customer Churn");
  assert.deepEqual(
    cells.map((c) => c[1]),
    ["Trained a model", "Scored new data", "Trained a model"],
  );
  assert.deepEqual(
    cells.map((c) => c[2]),
    ["Failed", "Finished", "Finished"],
  );
  assert.equal(cells[1][3], "1,200 rows scored");
  assert.match(cells[2][3], /0\.912$/, "a training run's headline metric");
  assert.notEqual(cells[0][3], "", "a failure says it did not finish");
  assert.equal(
    rows()[0].querySelector("a[data-run]").getAttribute("href"),
    `#/uc/${runs[0].use_case_id}/run/${runs[0].run_id}`,
    "each row links to its run",
  );
  assert.match($("[data-results] h3").textContent, /newest first/);
});

test("each row reads as something to open: the name in link colour and a trailing Open ›", () => {
  const runs = fixture("runs").runs;
  rows().forEach((tr, i) => {
    const open = tr.querySelector("a[data-run-open]");
    assert.ok(open, `row ${i} has an Open link`);
    assert.equal(open.textContent, "Open ›");
    assert.equal(open.getAttribute("href"), `#/uc/${runs[i].use_case_id}/run/${runs[i].run_id}`, "a real link to the run");
    assert.match(open.getAttribute("aria-label"), /^Open the run: /, "a screen reader hears which run");
  });
  const css = w.document.getElementById("sp-styles").textContent;
  assert.match(css, /\.sp \.sp-runs a\{color:var\(--brand-blue\)/, "links in the runs table use the link colour");
});

test("models waiting for approval: one notice with one way to them", async () => {
  world.waiting = fixture("approvals");
  await open("#/results");
  await until(() => $("[data-approvals]"), 3000, "the approvals notice");
  assert.match($("[data-approvals]").textContent, /2 models are waiting for approval/);
  assert.ok($('[data-approvals] a[href="#/approvals"]'));
  assert.equal(rows().length, 3, "the runs are still listed below it");
  world.waiting = null;
});

test("a failed list is an error with Try again, not an empty page", () => {
  const html = resultsHtml({ error: { status: 500, code: "HTTP_500", message: "The API answered 500." } });
  const holder = w.document.createElement("div");
  holder.innerHTML = html;
  assert.ok(holder.querySelector(".apierr"));
  assert.ok(holder.querySelector("[data-retry]"));
  assert.doesNotMatch(holder.textContent, /No runs yet/);
});

test("a finished run's results link Model health, the Schedule and its report", () => {
  const uc = fixture("use_case");
  const [, score, train] = fixture("runs").runs;
  const links = (run) => {
    const holder = w.document.createElement("div");
    holder.innerHTML = router.resultLinksHtml(uc, run);
    return [...holder.querySelectorAll("a")].map((a) => [a.textContent.replace("›", "").trim(), a.getAttribute("href")]);
  };
  assert.deepEqual(links(train), [
    ["Model health", "#/monitoring/alerts"],
    ["Schedule", "#/monitoring/schedules"],
    ["Results report", `#/pilot/view/results/${uc.id}`],
  ]);
  assert.deepEqual(links(score).at(-1), ["Campaign value report", `#/pilot/value/${score.run_id}`]);
  assert.deepEqual(links({ ...train, state: "failed" }), [], "nothing for a run that did not finish");
});
