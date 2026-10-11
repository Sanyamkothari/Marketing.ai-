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
import { $, $$, installPage, settle, until } from "../harness.mjs";

const FIXTURES = process.env.WZ_FIXTURES;
if (!FIXTURES) throw new Error("WZ_FIXTURES is not set: run these through tests/integration/test_campaign_wizard_ui.py");
const fx = (name) => JSON.parse(fs.readFileSync(path.join(FIXTURES, `${name}.json`), "utf8"));
const DEMO = JSON.parse(fs.readFileSync(new URL("../../../../fixtures/ui_demo/api.json", import.meta.url), "utf8"));

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
    if (kind === "artefacts" && name === "treat_list_summary.json") return ok(list);
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
    return ok(preview);
  }
  if (method === "POST" && p === `/campaigns/${CID}/plan`) {
    world.view = fx("campaign_planned");
    return ok(fx("plan_registered"), 201);
  }
  return notFound;
};

const { w } = installPage(server, { hash: "#/__start" });
// The page's own routing (app.js), the campaign screens that own `#/campaigns` and hand `new` to the wizard.
const router = await import("../../../../../ui/modules/router.js");
await import("../../../../../ui/app.js");
await import("../../../../../ui/modules/decide/index.js");

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
  world.runs.r_train = runAs("r_train", "train", "done", { model_version_id: "m_wb_1", target: "reactivated_90d" });
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
  });
  await until(() => $("[data-cw-working=list]"), 3000, "the list progress");
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
  assert.match($("[data-cw-group]").textContent, new RegExp(`^${point.n_control.toLocaleString("en-US")} customers \\(`));
  assert.equal(primaries().length, 1);
  $("[data-cw-next]").click();
  await settle(4);
  assert.equal(w.location.hash, `#/campaigns/new/review/${CID}`);
});

test("Review: the figures, then Approve registers the plan with the planner's numbers before Send is offered", async () => {
  world.view = fx("campaign_draft");
  await open(`#/campaigns/new/review/${CID}`);
  await until(() => $("[data-cw-review]"), 3000, "the review");
  assert.equal($("[data-cw-tile=contacted] .v").textContent, list.treat_rows.toLocaleString("en-US"));
  assert.equal($("[data-cw-tile=comparison] .v").textContent, preview.points[preview.current_index].n_control.toLocaleString("en-US"));
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
