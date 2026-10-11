/* Plan M M177 (DEC-1607): the "New campaign" wizard, step by step, in jsdom.

   Every body the fake API answers with is one the real app answered (`tests/integration/test_campaign_wizard_ui.py`
   writes them to $WZ_FIXTURES): the goal catalogue, the win-back goal and an operational one, a finished
   scoring run, its summaries, the campaign record `POST /campaigns` made from it, the planner's preview at the
   list's own average likelihood, the plan the real API froze from the wizard's body. The model versions are the
   validated demo's recorded answer (tests/fixtures/ui_demo/api.json) with the goal and status a case needs.

   Guided setup (Plan G/L) is not loaded here: a stand-in registers through the same seam (`registerSetupMode`)
   and hands the wizard exactly what Guided setup's Approve hands its host, so each case can approve at will. */
import { test } from "node:test";
import assert from "node:assert/strict";
import fs from "node:fs";
import path from "node:path";
import { $, $$, installPage, settle, until } from "../../harness.mjs";

const FIXTURES = process.env.WZ_FIXTURES;
const fx = (name) => JSON.parse(fs.readFileSync(path.join(FIXTURES, `${name}.json`), "utf8"));
const DEMO = JSON.parse(fs.readFileSync(new URL("../../../../../fixtures/ui_demo/api.json", import.meta.url), "utf8"));

const UC = "win-back-campaign";
const scoreRun = fx("run");
const RUN = scoreRun.run.run_id;
const created = fx("campaign_created");
const CID = created.campaign.campaign_id;
const preview = fx("preview");
const scoring = fx("scoring_summary");
const list = fx("treat_list_summary");

/** A model version as the registry answers it, for this goal, with `status`. */
const demoVersion = DEMO["GET /models?use_case=hillstrom-email"].body.versions.find((v) => v.is_champion).version;
const version = (modelId, status, runId) => ({
  version: { ...demoVersion, model_id: modelId, use_case_id: UC, run_id: runId, status },
  is_champion: status === "champion",
});
/** A run of this goal as `GET /runs/{id}` answers it (the real scoring run's body), in `mode` and `state`. */
const runAs = (runId, mode, state, extra = {}) => ({
  run: { ...scoreRun.run, run_id: runId, mode, state, ...extra },
  status: { ...scoreRun.status, run_id: runId, mode, state },
});

const world = {
  models: fx("models_empty"),
  runs: { [RUN]: scoreRun },
  view: fx("campaign_draft"),
  campaignsOfRun: fx("campaigns_of_run_empty"),
  nextRun: [],
  posted: [],
  approveAnswer: null,
  runAnswer: null,
  list, // the list's summary (treat_list_summary.json); a case may answer another
  preview, // the planner's preview; a case may answer another
  policy: null, // the list's policy_recommendation.json, when a case has one (else 404, as a non-uplift run)
};

const ok = (body, status = 200) => ({ status, body });
const notFound = { status: 404, body: { detail: { code: "NOT_FOUND", message: "Not in this fake." } } };

const server = (request) => {
  const { method, path: p, query, body } = request;
  if (method !== "GET") world.posted.push({ method, path: p, body });
  if (method === "GET" && p === "/auth/me") return ok(fx("me"));
  if (method === "GET" && p === "/industries") return ok(fx("industries"));
  if (method === "GET" && p === `/use-cases/${UC}`) return ok(fx("use_case"));
  if (method === "GET" && p === "/use-cases/order-fulfillment") return ok(fx("use_case_ops"));
  if (method === "GET" && p === `/use-cases/${UC}/cost-estimate`) return ok(fx("cost_score"));
  if (method === "GET" && p === "/models") return ok(world.models);
  if (method === "POST" && p === "/runs") {
    if (world.runAnswer) {
      const answer = world.runAnswer;
      world.runAnswer = null;
      return answer;
    }
    return ok({ run_id: world.nextRun.shift() }, 202);
  }
  if (method === "POST" && p === "/uploads") return ok({ upload_id: "u_wizard_list", profile: null }, 201);
  if (method === "GET" && p.startsWith("/runs/")) {
    const [, , id, kind, name] = p.split("/");
    if (!kind) return world.runs[id] ? ok(world.runs[id]) : notFound;
    if (kind === "artefacts" && name === "scoring_summary.json") return ok(scoring);
    if (kind === "artefacts" && name === "treat_list_summary.json") return ok(world.list);
    if (kind === "uplift" && name === "policy_recommendation.json") return world.policy ? ok(world.policy) : notFound;
    return notFound;
  }
  if (method === "POST" && p.startsWith("/models/") && p.endsWith("/approve")) {
    if (world.approveAnswer) return world.approveAnswer;
    const id = p.split("/")[2];
    world.models = { versions: [version(id, "champion", "r_train")] };
    return ok(world.models.versions[0]);
  }
  if (method === "GET" && p === "/campaigns" && query.run_id === RUN) return ok(world.campaignsOfRun);
  if (method === "POST" && p === "/campaigns") {
    world.campaignsOfRun = fx("campaigns_of_run");
    return ok(created, 201);
  }
  if (method === "GET" && p === `/campaigns/${CID}`) return ok(world.view);
  if (method === "GET" && p === `/campaigns/${CID}/plan-preview`) {
    world.previewQueries = (world.previewQueries || []).concat([query]);
    return ok(world.preview);
  }
  if (method === "POST" && p === `/campaigns/${CID}/plan`) {
    world.view = fx("campaign_planned");
    return ok(fx("plan_registered"), 201);
  }
  return notFound;
};

// A path in `slow` is answered after SLOW_MS, so a case can leave the page while the answer is on its way.
const slow = new Set();
const SLOW_MS = 400;
const delayed = async (request) => {
  if (slow.has(request.path)) await new Promise((resolve) => setTimeout(resolve, SLOW_MS));
  return server(request);
};
const { w } = installPage(delayed, { hash: "#/__start" });
// The page's own routing (app.js), the campaign screens that own `#/campaigns` and hand `new` to the wizard.
const router = await import("../../../../../../ui/modules/router.js");
const wizard = await import("../../../../../../ui/modules/campaigns/wizard.js");
const rules = await import("../../../../../../ui/modules/campaigns/wizard_rules.js");
const rupees = (n) => rules.fmtRupees(n);
const esc = (t) => t.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
await import("../../../../../../ui/app.js");
await import("../../../../../../ui/modules/decide/index.js");

// The stand-in for Guided setup: draws a file control, keeps the host the wizard hands it.
const guided = { host: null, mounts: 0 };
router.registerSetupMode({
  name: "guided",
  label: "Guided setup (recommended)",
  applies: () => true,
  mount: ({ main }, host) => {
    guided.host = host;
    guided.mounts += 1;
    main.innerHTML = `<div data-standin><input type="file" id="ag-file"></div>`;
  },
});

const denied = new Set();
const access = { can: (method, p) => !denied.has(`${method} ${p}`) };
await settle(10);
router.registerAccess(access);

async function open(hash) {
  w.location.hash = "#/__between";
  await settle(4);
  w.location.hash = hash;
  await settle(8);
}

const text = () => $("#app").textContent.replace(/\s+/g, " ");
const primaries = () => $$("#app .btn.primary");
const fill = (mode) => ({
  mode,
  upload: { upload_id: "u_prepared", profile: null },
  primaryKey: "customer_id",
  target: "reactivated_90d",
  overrides: { "evaluation.split": "random" },
});
const lastPost = (p) => [...world.posted].reverse().find((x) => x.path === p);

test("Goal: business names (Plan M §4.2), the stage as a label, operational goals folded, one goal per click", async () => {
  await open("#/campaigns/new");
  await until(() => $("[data-cw-goal-card]"), 3000, "the goal cards");
  assert.equal($("h1").textContent, "New campaign");
  assert.equal($$("h1").length, 1);
  assert.equal($("[data-cw-stepper=goal]").getAttribute("aria-current"), "step");
  const names = $$("[data-cw-goals] [data-cw-goal] .cw-goal-name").map((e) => e.textContent);
  assert.ok(names.includes("Win back lapsed customers") && names.includes("Find customers likely to buy"), names.join());
  assert.ok(!names.includes("Win-back Campaign") && !names.includes("Payment Propensity"), "catalogue names are not shown");
  const more = $$("[data-cw-more] [data-cw-goal]").map((e) => e.getAttribute("data-cw-goal"));
  assert.deepEqual(more.sort(), ["fault-prediction", "order-fulfillment"]);
  assert.equal($("[data-cw-more]").open, false, "More goals starts folded");
  assert.ok(primaries().length <= 1);
  $(`[data-cw-goal="${UC}"]`).click();
  await settle(4);
  assert.equal(w.location.hash, `#/campaigns/new/customers/${UC}`);
});

test("a goal that contacts nobody opens its own classic page instead", async () => {
  await open("#/campaigns/new/customers/order-fulfillment");
  await until(() => w.location.hash === "#/uc/order-fulfillment", 3000, "the classic page");
});

test("Customers, first campaign: Guided setup mounts to learn; its Approve starts the learning on the prepared file", async () => {
  world.models = fx("models_empty");
  guided.host = null;
  await open(`#/campaigns/new/customers/${UC}`);
  await until(() => guided.host, 3000, "Guided setup mounted");
  assert.equal($("[data-cw-customers]").getAttribute("data-cw-customers"), "first");
  assert.match(text(), /first campaign for this goal/);
  assert.equal(guided.host.mode, "train");
  assert.equal(guided.host.modelVersionId, null);
  assert.equal(guided.host.uc.id, UC);
  // The person chooses a file in Guided setup: the wizard keeps it for the list.
  const input = $("#ag-file");
  Object.defineProperty(input, "files", { value: [new w.File(["customer_id\n1\n"], "customers.csv", { type: "text/csv" })] });
  input.dispatchEvent(new w.Event("change", { bubbles: true }));
  world.nextRun = ["r_train"];
  world.runs.r_train = runAs("r_train", "train", "running");
  guided.host.approved(fill("train"));
  await until(() => w.location.hash === `#/campaigns/new/customers/${UC}/r_train`, 3000, "the learning run in the URL");
  assert.deepEqual(lastPost("/runs").body, {
    use_case: UC,
    mode: "train",
    upload_id: "u_prepared",
    primary_key: "customer_id",
    overrides: { "evaluation.split": "random" },
    target: "reactivated_90d",
  });
  assert.equal(guided.host.holds("u_prepared"), true, "Guided setup knows its approval was taken");
  await until(() => $("[data-cw-working=learning]"), 3000, "the learning progress");
  assert.match(text(), /learning from your customers' history/);
  assert.ok($("[role=progressbar]"));
});

test("Customers: a new model waiting for an approval holds the campaign; an Approver approves it here and the list starts", async () => {
  // The settings the person accepted in Guided setup for the learning, as the run record keeps them (expanded):
  // the list is built with the same ones - the consent column above all, so no one who said no is contacted.
  const accepted = { governance: { consent_column: "consent" }, validation: { acknowledged: ["V_SMALL_CLASS"] } };
  world.runs.r_train = runAs("r_train", "train", "done", {
    model_version_id: "m_wb_1",
    target: "reactivated_90d",
    overrides: accepted,
  });
  world.models = { versions: [version("m_wb_1", "pending_approval", "r_train")] };
  world.nextRun = ["r_list"];
  world.runs.r_list = runAs("r_list", "score", "running");
  await open(`#/campaigns/new/customers/${UC}/r_train`);
  await until(() => $("[data-cw-waiting=approve]"), 3000, "the approval card");
  assert.equal(primaries().length, 1);
  assert.equal($("[data-cw-approvals]").getAttribute("href"), "#/approvals");
  $("[data-cw-approve-model]").click();
  await until(() => w.location.hash === `#/campaigns/new/customers/${UC}/r_list`, 3000, "the list being built");
  assert.deepEqual(lastPost("/models/m_wb_1/approve").body, { approved_by: "local operator" });
  const upload = lastPost("/uploads");
  assert.ok(upload, "the chosen file is uploaded again, for the list");
  assert.deepEqual(lastPost("/runs").body, {
    use_case: UC,
    mode: "score",
    upload_id: "u_wizard_list",
    primary_key: scoreRun.run.primary_key,
    model_version_id: "m_wb_1",
    overrides: accepted,
  });
  await until(() => $("[data-cw-working=list]"), 3000, "the list progress");
  // The person goes back to the learning's address: the list already started from it is shown, not a new one.
  const runs = world.posted.filter((x) => x.path === "/runs").length;
  await open(`#/campaigns/new/customers/${UC}/r_train`);
  await until(() => w.location.hash === `#/campaigns/new/customers/${UC}/r_list`, 3000, "the list already started");
  assert.equal(world.posted.filter((x) => x.path === "/runs").length, runs, "no second list");
});

test("leaving the wizard while a list is prepared: the next poll does not draw the wizard over the other page", async () => {
  world.runs.r_poll = runAs("r_poll", "score", "running");
  await open(`#/campaigns/new/customers/${UC}/r_poll`);
  await until(() => $("[data-cw-working=list]"), 3000, "the list progress");
  w.location.hash = "#/approvals";
  await settle(4);
  await new Promise((resolve) => setTimeout(resolve, wizard.POLL_MS + 600));
  await settle(4);
  assert.equal(w.location.hash, "#/approvals");
  assert.equal($("[data-module=campaign-wizard]"), null, "the wizard is not drawn over another page");
});

test("leaving the wizard while an answer is on its way: the late answer draws nothing", async () => {
  world.runs.r_slow = runAs("r_slow", "score", "running");
  slow.add("/runs/r_slow");
  try {
    await open(`#/campaigns/new/customers/${UC}/r_slow`);
    w.location.hash = "#/approvals";
    await settle(4);
    await new Promise((resolve) => setTimeout(resolve, SLOW_MS + 300));
    await settle(4);
    assert.equal(w.location.hash, "#/approvals");
    assert.equal($("[data-module=campaign-wizard]"), null, "the late answer does not draw the wizard");
  } finally {
    slow.clear();
  }
});

test("Customers: someone who may not approve sees the campaign waiting, with the way to the approvals page", async () => {
  world.models = { versions: [version("m_wb_2", "pending_approval", "r_train2")] };
  world.runs.r_train2 = runAs("r_train2", "train", "done", { model_version_id: "m_wb_2" });
  denied.add("POST /models/{model_id}/approve");
  try {
    await open(`#/campaigns/new/customers/${UC}/r_train2`);
    await until(() => $("[data-cw-waiting=wait]"), 3000, "the waiting notice");
    assert.equal($("[data-cw-approve-model]"), null);
    assert.equal($("[data-cw-waiting=wait] a.btn.primary").getAttribute("href"), "#/approvals");
    assert.match(text(), /The campaign waits until then/);
  } finally {
    denied.clear();
  }
});

test("Customers: the server refuses the trainer's own approval, and the wizard says another Approver must do it", async () => {
  world.models = { versions: [version("m_wb_3", "pending_approval", "r_train3")] };
  world.runs.r_train3 = runAs("r_train3", "train", "done", { model_version_id: "m_wb_3" });
  world.approveAnswer = { status: 403, body: { detail: { code: "SEPARATION_OF_DUTIES", message: "You trained it." } } };
  await open(`#/campaigns/new/customers/${UC}/r_train3`);
  await until(() => $("[data-cw-approve-model]"), 3000, "the approval card");
  $("[data-cw-approve-model]").click();
  await until(() => $("[data-cw-waiting=wait]"), 3000, "the waiting notice");
  assert.match(text(), /You started this learning, so another person/);
  world.approveAnswer = null;
});

test("Customers, later campaign: the current model checks the file and builds the list - nothing learns again", async () => {
  world.models = { versions: [version("m_wb_1", "champion", "r_train")] };
  guided.host = null;
  await open(`#/campaigns/new/customers/${UC}`);
  await until(() => guided.host, 3000, "Guided setup mounted");
  assert.equal($("[data-cw-customers]").getAttribute("data-cw-customers"), "again");
  assert.equal(guided.host.mode, "score");
  assert.equal(guided.host.modelVersionId, "m_wb_1");
  world.nextRun = [RUN];
  guided.host.approved(fill("score"));
  await until(() => w.location.hash === `#/campaigns/new/audience/${CID}`, 4000, "the campaign made from the list");
  assert.deepEqual(lastPost("/runs").body, {
    use_case: UC,
    mode: "score",
    upload_id: "u_prepared",
    primary_key: "customer_id",
    overrides: { "evaluation.split": "random" },
    model_version_id: "m_wb_1",
  });
  assert.deepEqual(lastPost("/campaigns").body, { run_id: RUN }, "the campaign record is made from the finished list");
});

test("Customers: a cost the server wants confirmed is shown in its words, and 'Start it anyway' confirms it", async () => {
  world.models = fx("models_empty");
  guided.host = null;
  await open(`#/campaigns/new/customers/${UC}`);
  await until(() => guided.host, 3000, "Guided setup mounted");
  world.runAnswer = {
    status: 409,
    body: { detail: { code: "RUN_COST_NEEDS_CONFIRMATION", message: "This could cost more than the cap." } },
  };
  guided.host.approved(fill("train"));
  await until(() => $("[data-cw-cost-confirm]"), 3000, "the confirmation");
  assert.match($("[data-cw-cost-confirm]").textContent, /This could cost more than the cap\./);
  world.nextRun = ["r_train"];
  world.runs.r_train = runAs("r_train", "train", "running");
  $("[data-cw-confirm-cost]").click();
  await until(() => w.location.hash === `#/campaigns/new/customers/${UC}/r_train`, 3000, "the run started");
  assert.equal(lastPost("/runs").body.confirm_cost, true);
});

test("Customers: work that stopped says so and offers to start again", async () => {
  world.runs.r_failed = runAs("r_failed", "train", "failed");
  await open(`#/campaigns/new/customers/${UC}/r_failed`);
  await until(() => $("[data-cw-stopped]"), 3000, "the stopped notice");
  assert.equal($("[data-cw-restart]").getAttribute("href"), `#/campaigns/new/customers/${UC}`);
});

test("Audience: who is contacted by priority, the comparison group and the test size, all from the API", async () => {
  world.view = fx("campaign_draft");
  world.previewQueries = [];
  await open(`#/campaigns/new/audience/${CID}`);
  await until(() => $("[data-cw-audience]"), 3000, "the audience");
  assert.equal($("[data-cw-stepper=audience]").getAttribute("aria-current"), "step");
  assert.equal($("[data-cw-contacted]").textContent, `${list.treat_rows.toLocaleString("en-US")} customers are on the contact list.`);
  const rows = $$("[data-cw-priority] tbody tr").map((tr) => [...tr.querySelectorAll("td")].map((td) => td.textContent));
  assert.deepEqual(
    rows,
    scoring.bands.map((b) => [b.name, b.rows.toLocaleString("en-US"), b.action]),
  );
  // The planner was asked at the list's own average likelihood, and its point at the campaign's split is drawn.
  assert.deepEqual(world.previewQueries.at(-1), { base_rate: String(scoring.score_mean) });
  const point = preview.points[preview.current_index];
  assert.equal(
    $("[data-cw-test-size]").textContent,
    `Enough to see a ${point.mde_pp.toLocaleString("en-US", { minimumFractionDigits: 1, maximumFractionDigits: 1 })}-point change.`,
  );
  assert.match($("[data-cw-group]").textContent, new RegExp(`^${preview.n_holdout.toLocaleString("en-US")} customers \\(`));
  assert.equal(primaries().length, 1);
  $("[data-cw-next]").click();
  await settle(4);
  assert.equal(w.location.hash, `#/campaigns/new/review/${CID}`);
});

test("Audience: 'Which channel' counts the customers sent on each channel, not the ones it cannot reach", async () => {
  world.view = fx("campaign_draft");
  world.list = { ...list, channel_rows: { email: 300, sms: 120 }, channel_counts: { email: 7, sms: 55 } };
  try {
    await open(`#/campaigns/new/audience/${CID}`);
    await until(() => $("[data-cw-channels]"), 3000, "the channel table");
    const rows = $$("[data-cw-channels] tbody tr").map((tr) => [...tr.querySelectorAll("td")].map((td) => td.textContent));
    assert.deepEqual(rows, [
      ["email", "300"],
      ["sms", "120"],
    ]);
  } finally {
    world.list = list;
  }
});

test("Audience and Review: the comparison group is the one this list holds back, not the planner's estimate", async () => {
  world.view = fx("campaign_draft");
  const point = preview.points[preview.current_index];
  // The realised assignment is stratified, so it can differ from the planner's arm size at the same share.
  world.preview = { ...preview, n_holdout: point.n_control + 3, holdout_fraction: 0.1234 };
  try {
    await open(`#/campaigns/new/audience/${CID}`);
    await until(() => $("[data-cw-group]"), 3000, "the comparison group");
    assert.match(
      $("[data-cw-group]").textContent,
      new RegExp(`^${(point.n_control + 3).toLocaleString("en-US")} customers \\(12\\.3%\\) are not contacted`),
    );
    await open(`#/campaigns/new/review/${CID}`);
    await until(() => $("[data-cw-review]"), 3000, "the review");
    assert.equal($("[data-cw-tile=comparison] .v").textContent, (point.n_control + 3).toLocaleString("en-US"));
  } finally {
    world.preview = preview;
  }
});

test("Review: the expected value is the range the list's recommendation gives, else says no range can be given", async () => {
  world.view = fx("campaign_draft");
  world.list = { ...list, net_value_total: 2518304.28 };
  world.policy = { computed_on: "scored", net_value_low: 5314.5, net_value_high: 306932.49 };
  try {
    await open(`#/campaigns/new/review/${CID}`);
    await until(() => $("[data-cw-review-value]"), 3000, "the expected value");
    assert.equal($("[data-cw-tile=value] .v").textContent, `${rupees(5314.5)} to ${rupees(306932.49)}`);
    assert.match($("[data-cw-review-value]").textContent, new RegExp(`between ${esc(rupees(5314.5))} and ${esc(rupees(306932.49))}`));
    world.policy = { computed_on: "scored", net_value_low: null, net_value_high: null };
    await open(`#/campaigns/new/review/${CID}`);
    await until(() => $("[data-cw-review-value]"), 3000, "the expected value");
    assert.equal($("[data-cw-tile=value] .v").textContent, rupees(2518304.28));
    assert.match($("[data-cw-review-value]").textContent, /A range cannot be given for this list\./);
  } finally {
    world.list = list;
    world.policy = null;
  }
});

test("Review: the cost in rupees once an exchange rate is saved", () => {
  const line = rules.costLine({ estimated_usd: 1.25, inr: { amount: 104.5, inr_per_usd: 83.6, source: "admin", as_of: "2026-10-01" } });
  assert.equal(line.text, `About ${rupees(104.5)} to prepare this list, at list price.`);
  assert.equal(line.known, true);
  assert.match(rules.costLine({ estimated_usd: 1.25, inr: null }).text, /^About \$1\.25 /);
});

test("Review: the figures, then Approve registers the plan with the planner's numbers before Send is offered", async () => {
  world.view = fx("campaign_draft");
  await open(`#/campaigns/new/review/${CID}`);
  await until(() => $("[data-cw-review]"), 3000, "the review");
  assert.equal($("[data-cw-tile=contacted] .v").textContent, list.treat_rows.toLocaleString("en-US"));
  assert.equal($("[data-cw-tile=comparison] .v").textContent, preview.n_holdout.toLocaleString("en-US"));
  assert.ok($("[data-cw-review-cost]"), "the cost line");
  assert.equal($("[data-cw-review-compliance]"), null, "the compliance switch is off: no compliance line");
  assert.equal(primaries().length, 1);
  assert.equal(lastPost(`/campaigns/${CID}/plan`), undefined, "nothing registered before Approve");
  $("[data-cw-approve]").click();
  await until(() => w.location.hash === `#/campaigns/new/send/${CID}`, 3000, "Send");
  // Exactly the body the real API accepted and froze (test_campaign_wizard_ui.py registered it).
  assert.deepEqual(lastPost(`/campaigns/${CID}/plan`).body, fx("plan_body"));
});

test("Send: the one download, offered only with the plan fixed; the campaign is Live with its results date", async () => {
  world.view = fx("campaign_planned");
  await open(`#/campaigns/new/send/${CID}`);
  await until(() => $("[data-cw-send]"), 3000, "Send");
  const download = $("[data-cw-download]");
  assert.ok(download.classList.contains("primary"));
  assert.ok(download.hasAttribute("download"));
  assert.match(download.getAttribute("href"), new RegExp(`/runs/${RUN}/treat_list\\.csv$`));
  assert.equal(download.textContent, "Download contact list");
  assert.equal($("[data-cw-status]").textContent, "Live");
  const date = new Date(`${fx("plan_registered").analysis_date}T00:00:00Z`).toLocaleDateString("en-GB", {
    day: "numeric",
    month: "short",
    year: "numeric",
    timeZone: "UTC",
  });
  assert.match($("[data-cw-live]").textContent, new RegExp(`Add its results after ${date}`));
  assert.equal(primaries().length, 1);
  assert.equal($("[data-cw-open]").getAttribute("href"), `#/campaigns/${CID}`);
});

test("Send: the status is the campaign's own; a campaign with results opens its own page", async () => {
  const planned = fx("campaign_planned");
  world.view = { ...planned, campaign: { ...planned.campaign, status: "measured" } };
  try {
    await open(`#/campaigns/new/send/${CID}`);
    await until(() => $("[data-cw-status]"), 3000, "Send");
    assert.equal($("[data-cw-status]").textContent, "Measured");
    assert.doesNotMatch($("[data-cw-send]").textContent, /live/i);
    world.view = { ...world.view, report: { campaign_id: CID } };
    await open(`#/campaigns/new/send/${CID}`);
    await until(() => w.location.hash === `#/campaigns/${CID}`, 3000, "the campaign's own page");
  } finally {
    world.view = fx("campaign_planned");
  }
});

test("a refresh resumes from the server: a planned campaign opens on Send, a Draft never offers the download", async () => {
  world.view = fx("campaign_planned");
  await open(`#/campaigns/new/audience/${CID}`);
  await until(() => w.location.hash === `#/campaigns/new/send/${CID}`, 3000, "Send");
  world.view = fx("campaign_draft");
  await open(`#/campaigns/new/send/${CID}`);
  await until(() => w.location.hash === `#/campaigns/new/review/${CID}`, 3000, "back to Review");
  await until(() => $("[data-cw-review]"), 3000, "the review");
  assert.equal($("[data-cw-download]"), null);
});

test("a finished list already made into a campaign is not recorded twice", async () => {
  world.campaignsOfRun = fx("campaigns_of_run");
  const before = world.posted.filter((x) => x.path === "/campaigns").length;
  await open(`#/campaigns/new/customers/${UC}/${RUN}`);
  await until(() => w.location.hash === `#/campaigns/new/audience/${CID}`, 3000, "Audience");
  assert.equal(world.posted.filter((x) => x.path === "/campaigns").length, before);
});

test("someone who may not start a campaign sees why, and no goal to start", async () => {
  denied.add("POST /campaigns");
  try {
    await open("#/campaigns/new");
    await until(() => $("[data-cw-no-role]"), 3000, "the role notice");
    assert.equal($("[data-cw-goal]"), null);
    await open(`#/campaigns/new/customers/${UC}`);
    await until(() => $("[data-cw-no-role]"), 3000, "the role notice");
  } finally {
    denied.clear();
  }
});

test("Plan J's campaign page still draws for a campaign id: only `new` is the wizard's", async () => {
  await open(`#/campaigns/${CID}`);
  await until(() => $("[data-campaign-page]"), 3000, "Plan J's campaign page");
  assert.equal($("[data-module=campaign-wizard]"), null);
});
