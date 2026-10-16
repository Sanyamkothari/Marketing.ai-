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
  campaignRuns: {}, // campaign run id -> the view GET /campaign-runs/{id} answers
  nextCampaignRun: [], // the views POST /campaign-runs (and a restart) answer, in turn
  campaignRunAnswer: null, // a refusal for POST /campaign-runs, when a case needs one
  posted: [],
  approveAnswer: null,
  runAnswer: null,
  list, // the list's summary (treat_list_summary.json); a case may answer another
  lift: {
    mode: "classification",
    base_rate_pct: 12,
    bins: [
      { rows: 50, actual_rate_pct: 36, lift: 3 },
      { rows: 50, actual_rate_pct: 18, lift: 1.5 },
      { rows: 50, actual_rate_pct: 6, lift: 0.5 },
    ],
  },
  importance: { items: [{ feature: "tenure_months", share_pct: 60 }, { feature: "monthly_spend", share_pct: 40 }] },
  preview, // the planner's preview; a case may answer another
  policy: null, // the list's policy_recommendation.json, when a case has one (else 404, as a non-uplift run)
  modelsAnswer: null, // an answer for GET /models other than `models` (a failure), when a case needs one
  me: null, // GET /auth/me other than the recorded one (sign-in on), when a case needs one
};

const ok = (body, status = 200) => ({ status, body });
const notFound = { status: 404, body: { detail: { code: "NOT_FOUND", message: "Not in this fake." } } };

const server = (request) => {
  const { method, path: p, query, body } = request;
  if (method !== "GET") world.posted.push({ method, path: p, body });
  if (method === "GET" && p === "/auth/me") return ok(world.me || fx("me"));
  if (method === "GET" && p === "/industries") return ok(fx("industries"));
  if (method === "GET" && p === `/use-cases/${UC}`) return ok(fx("use_case"));
  if (method === "GET" && p === "/use-cases/order-fulfillment") return ok(fx("use_case_ops"));
  if (method === "GET" && p === `/use-cases/${UC}/cost-estimate`) return ok(fx("cost_score"));
  if (method === "GET" && p === "/models") return world.modelsAnswer || ok(world.models);
  if (method === "POST" && p === "/runs") {
    if (world.runAnswer) {
      const answer = world.runAnswer;
      world.runAnswer = null;
      return answer;
    }
    return ok({ run_id: world.nextRun.shift() }, 202);
  }
  if (method === "POST" && p === "/campaign-runs") {
    if (world.campaignRunAnswer) {
      const answer = world.campaignRunAnswer;
      world.campaignRunAnswer = null;
      return answer;
    }
    const view = world.nextCampaignRun.shift();
    world.campaignRuns[view.record.campaign_run_id] = view;
    return ok(view, 202);
  }
  if (method === "POST" && p.startsWith("/campaign-runs/") && p.endsWith("/restart")) {
    const view = world.nextCampaignRun.shift();
    world.campaignRuns[view.record.campaign_run_id] = view;
    return ok(view, 202);
  }
  if (method === "GET" && p.startsWith("/campaign-runs/")) {
    world.runReads = (world.runReads || 0) + 1;
    const view = world.campaignRuns[p.split("/")[2]];
    return view ? ok(view) : notFound;
  }
  if (method === "POST" && p === "/uploads") return ok({ upload_id: "u_wizard_list", profile: null }, 201);
  if (method === "GET" && p.startsWith("/runs/")) {
    const [, , id, kind, name] = p.split("/");
    if (!kind) return world.runs[id] ? ok(world.runs[id]) : notFound;
    if (kind === "artefacts" && name === "scoring_summary.json") return ok(scoring);
    if (kind === "artefacts" && name === "treat_list_summary.json") return ok(world.list);
    // The training run's own files the plain summary reads (the shape the engine writes): how its first customers did, what it relied on.
    if (kind === "artefacts" && name === "decile_lift.json") return ok(world.lift);
    if (kind === "artefacts" && name === "feature_importance.json") return ok(world.importance);
    if (kind === "uplift" && name === "policy_recommendation.json") return world.policy ? ok(world.policy) : notFound;
    // The served contact list, for the campaign's own page (its Audience tab reads it): two customers.
    if (kind === "treat_list.csv") return ok(`${world.runs[id].run.primary_key || "customer_id"},treat\n1,1\n2,1\n`);
    return notFound;
  }
  if (method === "POST" && p.startsWith("/models/") && p.endsWith("/approve")) {
    // A case's answer; a function answers and may change the registry as the server would have meanwhile.
    if (world.approveAnswer) return typeof world.approveAnswer === "function" ? world.approveAnswer(request) : world.approveAnswer;
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
    if (world.planAnswer) return world.planAnswer;
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
// The wizard's money is ui/format.js's (M172; Plan M final integration): lakh and crore, the paise below Rs 100.
const { rupees } = await import("../../../../../../ui/format.js");
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

// The downloads the page starts (M181 part C): the address of each, and how many calls the page had made to the
// server by then, so a case can say the plan was registered BEFORE the file was asked for. jsdom does not save
// files, so the click is recorded and its default (a navigation jsdom does not implement) is prevented.
const downloads = [];
w.addEventListener(
  "click",
  (event) => {
    const link = event.target && event.target.closest && event.target.closest("a[download]");
    if (!link) return;
    downloads.push({ href: link.href, auto: link.hasAttribute("data-cw-autodownload"), after: world.posted.map((x) => x.path) });
    event.preventDefault();
  },
  true,
);

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

test("M187: the chosen goal is drawn chosen on coming back, and forgotten on a fresh visit or once a campaign exists", async () => {
  const chosen = () => $$("[data-cw-goal].is-selected").map((e) => e.getAttribute("data-cw-goal"));
  const toGoal = async () => {
    w.location.hash = "#/campaigns/new";
    await settle(8);
    await until(() => $("[data-cw-goal-card]"), 3000, "the goal cards");
  };
  await open("#/campaigns/new");
  await until(() => $("[data-cw-goal-card]"), 3000, "the goal cards");
  assert.deepEqual(chosen(), [], "nothing is chosen on a first visit");
  $(`[data-cw-goal="${UC}"]`).click();
  await settle(4);
  assert.equal(w.location.hash, `#/campaigns/new/customers/${UC}`);
  // Back to step 1 inside the wizard: the card the person chose is still drawn chosen.
  await toGoal();
  assert.deepEqual(chosen(), [UC]);
  // Leaving the wizard and opening New campaign again is a fresh visit.
  w.location.hash = "#/campaigns";
  await settle(8);
  await toGoal();
  assert.deepEqual(chosen(), [], "a fresh visit starts with no goal chosen");
  // Chosen, then on to the campaign made from it (Review or Send): a New campaign from there starts clean too.
  $(`[data-cw-goal="${UC}"]`).click();
  await settle(4);
  world.view = fx("campaign_draft");
  w.location.hash = `#/campaigns/new/review/${CID}`;
  await settle(8);
  await until(() => $("[data-cw-review]"), 3000, "the review");
  await toGoal();
  assert.deepEqual(chosen(), [], "once a campaign exists the choice is spent");
});

test("a goal that contacts nobody opens its own classic page instead", async () => {
  await open("#/campaigns/new/customers/order-fulfillment");
  await until(() => w.location.hash === "#/uc/order-fulfillment", 3000, "the classic page");
});

/** A campaign run as `GET /campaign-runs/{id}` answers it. */
const STEP_TITLES = [
  ["train", "Learning from your customers"],
  ["score", "Scoring your customers"],
  ["list", "Building your contact list"],
  ["campaign", "Preparing your campaign"],
];
const crView = (id, state, { at = 0, error = null, campaignId = null, eta = 600, extra = {} } = {}) => ({
  record: {
    campaign_run_id: id,
    state,
    use_case_id: UC,
    created_at: "2026-10-15T10:00:00Z",
    updated_at: "2026-10-15T10:00:00Z",
    steps: STEP_TITLES.map(([key, title], i) => ({
      key,
      title,
      state: state === "done" || i < at ? "done" : i === at ? (state === "running" ? "running" : "failed") : "pending",
    })),
    campaign_id: campaignId,
    score_run_id: campaignId ? RUN : null,
    error,
    ...extra,
  },
  pct: state === "done" ? 100 : 40,
  eta_seconds: state === "running" ? eta : null,
  summary: error ? error.message : state === "done" ? "Your campaign is ready." : "Learning from your customers: about 10 min left.",
});
const lastGet = () => world.runReads || 0;

test("Customers, first campaign: Approve starts the whole campaign on the server with the chosen time, and goes to its page", async () => {
  world.models = fx("models_empty");
  guided.host = null;
  await open(`#/campaigns/new/customers/${UC}`);
  await until(() => guided.host, 3000, "Guided setup mounted");
  assert.equal($("[data-cw-customers]").getAttribute("data-cw-customers"), "first");
  assert.match(text(), /first campaign for this goal/);
  assert.equal(guided.host.mode, "train");
  assert.equal(guided.host.modelVersionId, null);
  assert.equal(guided.host.uc.id, UC);
  world.nextCampaignRun = [crView("cr_1", "running")];
  world.campaignRuns.cr_1 = world.nextCampaignRun[0];
  guided.host.approved({ ...fill("train"), speed: "thorough" });
  await until(() => w.location.hash === "#/campaigns/run/cr_1", 3000, "the progress page");
  assert.deepEqual(lastPost("/campaign-runs").body, {
    use_case: UC,
    upload_id: "u_prepared",
    primary_key: "customer_id",
    speed: "thorough",
    overrides: { "evaluation.split": "random" },
    target: "reactivated_90d",
  });
  assert.equal(lastPost("/runs"), undefined, "the browser starts no run of its own");
  assert.equal(guided.host.holds("u_prepared"), true, "Guided setup knows its approval was taken");
});

test("Customers, later campaign: the current model is used - nothing learns again", async () => {
  world.models = { versions: [version("m_wb_1", "champion", "r_train")] };
  guided.host = null;
  await open(`#/campaigns/new/customers/${UC}`);
  await until(() => guided.host, 3000, "Guided setup mounted");
  assert.equal($("[data-cw-customers]").getAttribute("data-cw-customers"), "again");
  assert.equal(guided.host.mode, "score");
  assert.equal(guided.host.modelVersionId, "m_wb_1");
  world.nextCampaignRun = [crView("cr_2", "running", { at: 1 })];
  guided.host.approved(fill("score"));
  await until(() => w.location.hash === "#/campaigns/run/cr_2", 3000, "the progress page");
  assert.deepEqual(lastPost("/campaign-runs").body, {
    use_case: UC,
    upload_id: "u_prepared",
    primary_key: "customer_id",
    speed: "balanced",
    overrides: { "evaluation.split": "random" },
    model_version_id: "m_wb_1",
  });
});

test("Customers: a start the server refuses is shown, and the step stays", async () => {
  world.models = fx("models_empty");
  guided.host = null;
  await open(`#/campaigns/new/customers/${UC}`);
  await until(() => guided.host, 3000, "Guided setup mounted");
  world.campaignRunAnswer = {
    status: 409,
    body: { detail: { code: "CAMPAIGN_RUN_ACTIVE", message: "A campaign is already being prepared from this data (cr_0)." } },
  };
  guided.host.approved(fill("train"));
  await until(() => text().includes("already being prepared from this data"), 3000, "the refusal");
  assert.equal(w.location.hash, `#/campaigns/new/customers/${UC}`);
  assert.equal(guided.host.holds("u_prepared"), false, "Guided setup offers the start again");
});

test("an address from when the browser chained the steps opens the Customers step afresh", async () => {
  await open(`#/campaigns/new/customers/${UC}/r_old`);
  await until(() => w.location.hash === `#/campaigns/new/customers/${UC}`, 3000, "the Customers step");
});

test("the progress page: steps in plain words, the time left, and that the page can be closed", async () => {
  world.campaignRuns.cr_3 = crView("cr_3", "running", { at: 0, eta: 600 });
  await open("#/campaigns/run/cr_3");
  await until(() => $("[data-run-steps]"), 3000, "the steps");
  assert.deepEqual(
    $$("[data-run-step]").map((e) => e.getAttribute("data-state")),
    ["running", "pending", "pending", "pending"],
  );
  assert.match(text(), /Learning from your customers/);
  assert.match(text(), /About 10 minutes left/);
  assert.match(text(), /You can close this page/);
  assert.ok($("[role=progressbar]"));
  assert.equal(primaries().length, 0);
});

test("the progress page: when the run is done it leads to the campaign's review, where the person approves and downloads", async () => {
  world.campaignRuns.cr_4 = crView("cr_4", "running", { at: 3, eta: 20 });
  await open("#/campaigns/run/cr_4");
  await until(() => $("[data-run-steps]"), 3000, "the steps");
  world.campaignRuns.cr_4 = crView("cr_4", "done", { campaignId: CID });
  await until(() => $("[data-run-ready]"), 6000, "the finished page");
  assert.equal($("h1").textContent, "Your campaign is ready");
  assert.equal($("[data-run-ready]").getAttribute("href"), `#/campaigns/new/review/${CID}`);
  assert.equal($("[data-run-campaign]").getAttribute("href"), `#/campaigns/${CID}/overview`);
  assert.equal($("[data-run-restart]"), null);
});

test("the progress page: a run the server restarted under says so and Restart starts it again as a new run", async () => {
  const error = { code: "RUN_INTERRUPTED", message: "Learning from your customers stopped because the server restarted. Start the campaign again.", step: "train" };
  world.campaignRuns.cr_5 = crView("cr_5", "stopped", { at: 0, error });
  await open("#/campaigns/run/cr_5");
  await until(() => $("[data-run-restart]"), 3000, "the Restart button");
  assert.match(text(), /stopped because the server restarted/);
  assert.equal($("[data-run-restart]").textContent.trim(), "Restart");
  const before = lastGet();
  world.nextCampaignRun = [crView("cr_6", "running")];
  $("[data-run-restart]").click();
  await until(() => w.location.hash === "#/campaigns/run/cr_6", 3000, "the new run's page");
  assert.deepEqual(lastPost("/campaign-runs/cr_5/restart").body, { confirm_cost: false });
  await until(() => lastGet() > before, 3000, "the new run is read");
});

test("the progress page: a start that needs a cost confirmed offers 'Start it anyway', which confirms it", async () => {
  const error = { code: "RUN_COST_NEEDS_CONFIRMATION", message: "This could cost more than the cap.", step: "train" };
  world.campaignRuns.cr_7 = crView("cr_7", "failed", { at: 0, error });
  await open("#/campaigns/run/cr_7");
  await until(() => $("[data-run-restart]"), 3000, "the button");
  assert.equal($("[data-run-restart]").textContent.trim(), "Start it anyway");
  assert.match(text(), /This could cost more than the cap\./);
  world.nextCampaignRun = [crView("cr_8", "running")];
  $("[data-run-restart]").click();
  await until(() => w.location.hash === "#/campaigns/run/cr_8", 3000, "the new run's page");
  assert.deepEqual(lastPost("/campaign-runs/cr_7/restart").body, { confirm_cost: true });
});

test("leaving the progress page stops its poll", async () => {
  world.campaignRuns.cr_9 = crView("cr_9", "running");
  await open("#/campaigns/run/cr_9");
  await until(() => $("[data-run-steps]"), 3000, "the steps");
  w.location.hash = "#/approvals";
  await settle(4);
  const reads = lastGet();
  await new Promise((resolve) => setTimeout(resolve, 3600));
  assert.equal(lastGet(), reads, "no more reads once the person left");
  assert.equal($("[data-module=campaign-run]"), null);
});

test("Audience: who is contacted by priority, the comparison group and the test size, all from the API, on the Review screen", async () => {
  world.view = fx("campaign_draft");
  world.previewQueries = [];
  // M181 part C: Audience's old address opens the one screen that shows Audience and Review (no "Continue").
  await open(`#/campaigns/new/audience/${CID}`);
  await until(() => w.location.hash === `#/campaigns/new/review/${CID}`, 3000, "the Review screen");
  await until(() => $("[data-cw-audience]") && $("[data-cw-review]"), 3000, "the audience and the review");
  // The five named steps stay in the indicator; Audience and Review are both on screen, Review is the current one.
  assert.deepEqual(
    $$("[data-cw-stepper]").map((li) => [li.getAttribute("data-cw-stepper"), li.querySelector(".st-label").firstChild.textContent.trim()]),
    [
      ["goal", "Goal"],
      ["customers", "Customers"],
      ["audience", "Audience"],
      ["review", "Review"],
      ["send", "Send"],
    ],
  );
  assert.deepEqual(
    // M187: the design system's stepper (done, current, upcoming); both steps on screen are drawn current.
    $$("[data-cw-stepper]").map((li) => li.className.replace("st-step ", "")),
    ["done", "done", "current", "current", "upcoming"],
  );
  assert.equal($("[data-cw-stepper=review]").getAttribute("aria-current"), "step");
  assert.equal($$("[aria-current=step]").length, 1);
  assert.equal($("[data-cw-next]"), null, "no Continue: Audience and Review share the screen");
  assert.equal($("[data-cw-contacted]").textContent, `${list.treat_rows.toLocaleString("en-US")} customers are on the contact list.`);
  // M187: what happens to a priority is a second line under its name, not a column of prose.
  const rows = $$("[data-cw-priority] tbody tr").map((tr) => [
    tr.querySelector(".cw-pri-name").textContent,
    tr.querySelector("td.num").textContent,
    tr.querySelector(".cw-pri-action").textContent,
  ]);
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
  assert.equal(primaries().length, 1, "one primary button on the shared screen");
  assert.equal($("[data-cw-again]").getAttribute("href"), `#/campaigns/new/customers/${UC}`);
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
    await open(`#/campaigns/new/review/${CID}`);
    await until(() => $("[data-cw-group]") && $("[data-cw-review]"), 3000, "the comparison group and the review");
    assert.match(
      $("[data-cw-group]").textContent,
      new RegExp(`^${(point.n_control + 3).toLocaleString("en-US")} customers \\(12\\.3%\\) are not contacted`),
    );
    assert.equal($("[data-cw-tile=comparison] .stat-value").textContent, (point.n_control + 3).toLocaleString("en-US"));
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
    assert.equal($("[data-cw-tile=value] .stat-value").textContent, `${rupees(5314.5)} to ${rupees(306932.49)}`);
    assert.match($("[data-cw-review-value]").textContent, new RegExp(`between ${esc(rupees(5314.5))} and ${esc(rupees(306932.49))}`));
    world.policy = { computed_on: "scored", net_value_low: null, net_value_high: null };
    await open(`#/campaigns/new/review/${CID}`);
    await until(() => $("[data-cw-review-value]"), 3000, "the expected value");
    assert.equal($("[data-cw-tile=value] .stat-value").textContent, rupees(2518304.28));
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

const treatHref = new RegExp(`/runs/${RUN}/treat_list\\.csv$`);
const planPath = `/campaigns/${CID}/plan`;
/** The calls the page made after the `n`th one, by path. */
const postedSince = (n) => world.posted.slice(n).map((x) => x.path);

test("Review: the figures, then 'Approve and download' registers the plan, and only then downloads the list and shows Send", async () => {
  world.view = fx("campaign_draft");
  downloads.length = 0;
  await open(`#/campaigns/new/review/${CID}`);
  await until(() => $("[data-cw-review]"), 3000, "the review");
  assert.equal($("[data-cw-tile=contacted] .stat-value").textContent, list.treat_rows.toLocaleString("en-US"));
  assert.equal($("[data-cw-tile=comparison] .stat-value").textContent, preview.n_holdout.toLocaleString("en-US"));
  assert.ok($("[data-cw-review-cost]"), "the cost line");
  assert.equal($("[data-cw-review-compliance]"), null, "the compliance switch is off: no compliance line");
  assert.equal(primaries().length, 1);
  assert.equal($("[data-cw-approve]").textContent, "Approve and download");
  assert.equal($("[data-cw-approve]").getAttribute("data-cw-approve"), "plan");
  assert.equal(lastPost(planPath), undefined, "nothing registered before Approve");
  assert.equal(downloads.length, 0, "nothing downloaded before Approve");
  const before = world.posted.length;
  $("[data-cw-approve]").click();
  await until(() => w.location.hash === `#/campaigns/new/send/${CID}`, 3000, "Send");
  // Exactly the body the real API accepted and froze (test_campaign_wizard_ui.py registered it).
  assert.deepEqual(lastPost(planPath).body, fx("plan_body"));
  assert.deepEqual(postedSince(before), [planPath], "the plan only: no model to approve");
  // One download, of the list's served file, started after the server had accepted the plan.
  assert.equal(downloads.length, 1, JSON.stringify(downloads));
  assert.ok(downloads[0].auto);
  assert.match(downloads[0].href, treatHref);
  assert.ok(downloads[0].after.includes(planPath), "the plan was registered before the download");
  // Send: the campaign is Live, the list is on its way, "Download again" is a secondary link to the same file.
  await until(() => $("[data-cw-send]"), 3000, "Send drawn");
  assert.equal($("[data-cw-status]").textContent, "Live");
  assert.match($("[data-cw-live]").textContent, /Your contact list is downloading\./);
  const again = $("[data-cw-download]");
  assert.equal(again.getAttribute("data-cw-download"), "again");
  assert.equal(again.textContent, "Download again");
  assert.ok(!again.classList.contains("primary") && again.hasAttribute("download"));
  assert.match(again.getAttribute("href"), treatHref);
  assert.equal(new URL(again.href).href, downloads[0].href, "the same file as the download");
  assert.equal(primaries().length, 1);
  assert.equal($("#app .btn.primary").getAttribute("data-cw-open"), "", "the next step is the campaign's page");
});

test("Review: when the plan is refused nothing is downloaded, the error is shown, and the screen stays", async () => {
  world.view = fx("campaign_draft");
  world.planAnswer = { status: 409, body: { detail: { code: "PLAN_ALREADY_REGISTERED", message: "This campaign already has a plan." } } };
  downloads.length = 0;
  try {
    await open(`#/campaigns/new/review/${CID}`);
    await until(() => $("[data-cw-approve]"), 3000, "the review");
    $("[data-cw-approve]").click();
    await until(() => $("[data-cw-review] .apierr"), 3000, "the error");
    assert.match($("[data-cw-review]").textContent, /This campaign already has a plan\./);
    assert.equal(downloads.length, 0, "no download without the plan");
    assert.equal(w.location.hash, `#/campaigns/new/review/${CID}`);
    assert.equal($("[data-cw-approve]").disabled, false, "the person can try again");
  } finally {
    world.planAnswer = null;
  }
});

test("Review, a goal's first campaign: an Approver approves the new model, the plan and the download in one click, in that order", async () => {
  const LIST_MODEL = scoreRun.run.model_version_id;
  assert.ok(LIST_MODEL, "the list's run names the model it was built with");
  world.view = fx("campaign_draft");
  world.models = { versions: [version(LIST_MODEL, "pending_approval", "r_train")] };
  downloads.length = 0;
  await open(`#/campaigns/new/review/${CID}`);
  await until(() => $("[data-cw-approve]"), 3000, "the review");
  const button = $("[data-cw-approve]");
  assert.equal(button.textContent, "Approve the new model and this plan, then download");
  assert.equal(button.getAttribute("data-cw-approve"), "model-and-plan");
  assert.match($("[data-cw-new-model]").textContent, /a new model that still needs an Approver's approval\. You have that role\./);
  assert.match($("[data-cw-approve-note]").textContent, /approves the new model, then fixes how this campaign will be judged, then downloads/);
  assert.equal($("[data-cw-approvals]").getAttribute("href"), "#/approvals", "the details stay one link away");
  assert.equal(primaries().length, 1);
  assert.equal(lastPost(`/models/${LIST_MODEL}/approve`), undefined, "nothing approved before the click");
  const before = world.posted.length;
  button.click();
  await until(() => w.location.hash === `#/campaigns/new/send/${CID}`, 3000, "Send");
  // The approvals page's own call, then the plan; then the download.
  assert.deepEqual(postedSince(before), [`/models/${LIST_MODEL}/approve`, planPath]);
  assert.deepEqual(lastPost(`/models/${LIST_MODEL}/approve`).body, { approved_by: "local operator" });
  assert.deepEqual(lastPost(planPath).body, fx("plan_body"));
  assert.equal(downloads.length, 1);
  assert.match(downloads[0].href, treatHref);
  assert.deepEqual(downloads[0].after.slice(before), [`/models/${LIST_MODEL}/approve`, planPath]);
});

test("Review, first campaign: a refused model approval stops everything - no plan, no download - and the error is shown", async () => {
  const LIST_MODEL = scoreRun.run.model_version_id;
  world.view = fx("campaign_draft");
  world.models = { versions: [version(LIST_MODEL, "pending_approval", "r_train")] };
  world.approveAnswer = { status: 409, body: { detail: { code: "CHAMPION_CHANGED", message: "Another model became current meanwhile." } } };
  downloads.length = 0;
  try {
    await open(`#/campaigns/new/review/${CID}`);
    await until(() => $("[data-cw-approve=model-and-plan]"), 3000, "the review");
    const before = world.posted.length;
    $("[data-cw-approve]").click();
    await until(() => /Another model became current meanwhile\./.test($("[data-cw-review]").textContent), 3000, "the error");
    assert.deepEqual(postedSince(before), [`/models/${LIST_MODEL}/approve`], "no plan after a refused approval");
    assert.equal(downloads.length, 0);
    assert.equal(w.location.hash, `#/campaigns/new/review/${CID}`);
  } finally {
    world.approveAnswer = null;
  }
});

test("Review, first campaign: the model approved but the plan refused - trying again only registers the plan", async () => {
  const LIST_MODEL = scoreRun.run.model_version_id;
  world.view = fx("campaign_draft");
  world.models = { versions: [version(LIST_MODEL, "pending_approval", "r_train")] };
  world.planAnswer = { status: 503, body: { detail: { code: "STORE_BUSY", message: "Try again in a moment." } } };
  downloads.length = 0;
  try {
    await open(`#/campaigns/new/review/${CID}`);
    await until(() => $("[data-cw-approve=model-and-plan]"), 3000, "the review");
    $("[data-cw-approve]").click();
    // Drawn again from the server: the model is approved now, so the button is the plain one, with the error.
    await until(() => $("[data-cw-approve=plan]") && /Try again in a moment\./.test($("[data-cw-review]").textContent), 3000, "the error");
    assert.equal(downloads.length, 0, "no download without the plan");
    world.planAnswer = null;
    const before = world.posted.length;
    $("[data-cw-approve]").click();
    await until(() => w.location.hash === `#/campaigns/new/send/${CID}`, 3000, "Send");
    assert.deepEqual(postedSince(before), [planPath], "the model is not approved twice");
    assert.equal(downloads.length, 1);
  } finally {
    world.planAnswer = null;
  }
});

test("Review, first campaign: the server refuses the trainer's own approval; the screen says another Approver must do it", async () => {
  const LIST_MODEL = scoreRun.run.model_version_id;
  world.view = fx("campaign_draft");
  world.models = { versions: [version(LIST_MODEL, "pending_approval", "r_train")] };
  world.approveAnswer = { status: 403, body: { detail: { code: "SEPARATION_OF_DUTIES", message: "You trained it." } } };
  downloads.length = 0;
  try {
    await open(`#/campaigns/new/review/${CID}`);
    await until(() => $("[data-cw-approve=model-and-plan]"), 3000, "the review");
    const before = world.posted.length;
    $("[data-cw-approve]").click();
    await until(() => $("[data-cw-waiting=review]"), 3000, "the waiting notice");
    assert.match(text(), /You started this learning, so another person with the Approver role must approve the new model/);
    assert.equal($("[data-cw-approve]"), null, "no Approve while it waits");
    assert.equal($("#app .btn.primary").getAttribute("href"), "#/approvals");
    assert.equal(primaries().length, 1);
    assert.deepEqual(postedSince(before), [`/models/${LIST_MODEL}/approve`], "no plan");
    assert.equal(downloads.length, 0, "no download");
  } finally {
    world.approveAnswer = null;
  }
});

test("Review, first campaign: someone who may not approve models is held, with the way to Waiting for approval", async () => {
  const LIST_MODEL = scoreRun.run.model_version_id;
  world.view = fx("campaign_draft");
  world.models = { versions: [version(LIST_MODEL, "pending_approval", "r_train")] };
  denied.add("POST /models/{model_id}/approve");
  try {
    await open(`#/campaigns/new/review/${CID}`);
    await until(() => $("[data-cw-waiting=review]"), 3000, "the waiting notice");
    assert.equal($("[data-cw-approve]"), null);
    assert.equal($("#app .btn.primary").getAttribute("href"), "#/approvals");
    assert.match(text(), /someone with the Approver role must approve first/);
  } finally {
    denied.clear();
  }
});

test("Review: a list built with a model an Approver turned down cannot be sent; the screen offers to start again", async () => {
  const LIST_MODEL = scoreRun.run.model_version_id;
  world.view = fx("campaign_draft");
  world.models = { versions: [version(LIST_MODEL, "archived", "r_train")] };
  world.models.versions[0].version = { ...world.models.versions[0].version, approved_at: null, promoted_at: null };
  try {
    await open(`#/campaigns/new/review/${CID}`);
    await until(() => $("[data-cw-model-refused]"), 3000, "the refusal");
    assert.equal($("[data-cw-approve]"), null);
    assert.equal($("[data-cw-restart]").getAttribute("href"), `#/campaigns/new/customers/${UC}`);
    assert.equal(primaries().length, 1);
  } finally {
    world.models = fx("models_empty");
  }
});

// --- M181 part C review: the approval hold fails closed, and follows the model wherever its list can be sent ----

const reviewHash = `#/campaigns/new/review/${CID}`;
const registryDown = { status: 503, body: { detail: { code: "STORE_BUSY", message: "The registry is busy." } } };
/** A version waiting for an approval whose decision was measured against `against` (the registry's own field). */
const waitingAgainst = (modelId, against, runId = "r_train") => {
  const row = version(modelId, "pending_approval", runId);
  return { ...row, version: { ...row.version, measured_against_champion_id: against, approved_at: null, promoted_at: null } };
};

test("Review, first campaign: when the registry cannot be read nothing is approved, planned or downloaded, for anyone (fails closed)", async () => {
  const LIST_MODEL = scoreRun.run.model_version_id;
  for (const approver of [false, true]) {
    world.view = fx("campaign_draft");
    world.models = { versions: [version(LIST_MODEL, "pending_approval", "r_train")] };
    world.modelsAnswer = registryDown;
    if (!approver) denied.add("POST /models/{model_id}/approve");
    downloads.length = 0;
    const before = world.posted.length;
    try {
      await open(reviewHash);
      await until(() => $("[data-cw-model-unknown]"), 3000, "the notice");
      assert.equal($("[data-cw-approve]"), null, "no Approve on a guess");
      assert.equal(primaries().length, 0);
      assert.match(text(), /Whether the model behind this list is approved could not be checked/);
      assert.ok($("[data-cw-model-unknown] .apierr"), "the registry's error is shown, with Try again");
      assert.deepEqual(postedSince(before), [], "no approval, no plan");
      assert.equal(downloads.length, 0, "no download");
    } finally {
      world.modelsAnswer = null;
      denied.clear();
    }
  }
  // Once the registry answers, the screen says where the model stands (an Approver: the one button).
  await open(reviewHash);
  await until(() => $("[data-cw-approve=model-and-plan]"), 3000, "the Approver's button once the registry answers");
  world.models = fx("models_empty");
});

test("Review, first campaign: the model approved elsewhere meanwhile - the refused approval redraws from the server and the next click only plans", async () => {
  const LIST_MODEL = scoreRun.run.model_version_id;
  world.view = fx("campaign_draft");
  world.models = { versions: [version(LIST_MODEL, "pending_approval", "r_train")] };
  // Another Approver approved it from the bell after this screen was drawn: the server now refuses this approval.
  world.approveAnswer = () => {
    world.models = { versions: [version(LIST_MODEL, "champion", "r_train")] };
    return {
      status: 409,
      body: { detail: { code: "INVALID_TRANSITION", message: "Only a model waiting for approval can be approved." } },
    };
  };
  downloads.length = 0;
  try {
    await open(reviewHash);
    await until(() => $("[data-cw-approve=model-and-plan]"), 3000, "the review");
    const first = world.posted.length;
    $("[data-cw-approve]").click();
    await until(
      () => $("[data-cw-approve=plan]") && /Only a model waiting for approval can be approved\./.test($("[data-cw-review]").textContent),
      3000,
      "the plain button, with the error",
    );
    assert.equal(downloads.length, 0, "nothing downloaded on the refused click");
    assert.deepEqual(postedSince(first), [`/models/${LIST_MODEL}/approve`], "no plan on the refused click");
    world.approveAnswer = null;
    const before = world.posted.length;
    $("[data-cw-approve]").click();
    await until(() => w.location.hash === `#/campaigns/new/send/${CID}`, 3000, "Send");
    assert.deepEqual(postedSince(before), [planPath], "the plan only: the approval is not posted again");
    assert.equal(downloads.length, 1, "one download");
  } finally {
    world.approveAnswer = null;
    world.models = fx("models_empty");
  }
});

test("Review, first campaign: another model became current while it waited - the screen offers to start again, not the same approval", async () => {
  const LIST_MODEL = scoreRun.run.model_version_id;
  world.view = fx("campaign_draft");
  world.models = { versions: [waitingAgainst(LIST_MODEL, rules.NO_CHAMPION_AT_DECISION)] };
  world.approveAnswer = () => {
    world.models = { versions: [waitingAgainst(LIST_MODEL, rules.NO_CHAMPION_AT_DECISION), version("m_other", "champion", "r_other")] };
    return { status: 409, body: { detail: { code: "CHAMPION_CHANGED", message: "Another model became current meanwhile." } } };
  };
  downloads.length = 0;
  try {
    await open(reviewHash);
    await until(() => $("[data-cw-approve=model-and-plan]"), 3000, "the review");
    const before = world.posted.length;
    $("[data-cw-approve]").click();
    await until(() => $("[data-cw-model-superseded]"), 3000, "the superseded notice");
    assert.match($("[data-cw-review]").textContent, /Another model became current meanwhile\./, "the server's refusal stays on screen");
    assert.equal($("[data-cw-approve]"), null, "the same approval is not offered again");
    assert.equal(primaries().length, 1);
    assert.equal($("#app .btn.primary").getAttribute("data-cw-restart"), "");
    assert.equal($("#app .btn.primary").getAttribute("href"), `#/campaigns/new/customers/${UC}`);
    assert.deepEqual(postedSince(before), [`/models/${LIST_MODEL}/approve`], "no plan");
    assert.equal(downloads.length, 0);
    // Drawn afresh (a refresh, or a person coming back), it says the same without anyone clicking.
    world.approveAnswer = null;
    await open(reviewHash);
    await until(() => $("[data-cw-model-superseded]"), 3000, "superseded on a fresh visit");
    assert.equal($("[data-cw-approve]"), null);
  } finally {
    world.approveAnswer = null;
    world.models = fx("models_empty");
  }
});

test("Send: a plan registered elsewhere for a list whose model still waits does not make it downloadable", async () => {
  const LIST_MODEL = scoreRun.run.model_version_id;
  world.view = fx("campaign_planned");
  downloads.length = 0;
  try {
    world.models = { versions: [version(LIST_MODEL, "pending_approval", "r_train")] };
    await open(`#/campaigns/new/send/${CID}`);
    await until(() => $("[data-cw-send-held=pending]"), 3000, "Send held");
    assert.equal($("[data-cw-download]"), null, "no download link");
    assert.equal($("#app .btn.primary").getAttribute("href"), "#/approvals");
    assert.equal(primaries().length, 1);
    // The registry cannot be read: held too.
    world.modelsAnswer = registryDown;
    await open(`#/campaigns/new/send/${CID}`);
    await until(() => $("[data-cw-send-held=unknown]"), 3000, "Send held while unknown");
    assert.equal($("[data-cw-download]"), null);
    world.modelsAnswer = null;
    // Approved: the download, as before.
    world.models = { versions: [version(LIST_MODEL, "champion", "r_train")] };
    await open(`#/campaigns/new/send/${CID}`);
    await until(() => $("[data-cw-download]"), 3000, "the download once approved");
    assert.equal(downloads.length, 0, "Send itself starts nothing");
  } finally {
    world.modelsAnswer = null;
    world.models = fx("models_empty");
  }
});

test("the campaign's own page: a Draft whose list's model still waits offers no download and no 'Plan the test'", async () => {
  const LIST_MODEL = scoreRun.run.model_version_id;
  // A Draft as the campaign page counts it: no plan, and its results date still ahead (the recorded one is past).
  const draft = fx("campaign_draft");
  world.view = { ...draft, campaign: { ...draft.campaign, treatment_start: "2099-01-01T00:00:00Z" } };
  assert.equal(world.view.campaign.kind, "scored", "a list made here");
  try {
    world.models = { versions: [version(LIST_MODEL, "pending_approval", "r_train")] };
    await open(`#/campaigns/${CID}/overview`);
    await until(() => $("[data-list-held=pending]"), 3000, "the overview's hold");
    assert.equal($("[data-primary=plan]"), null, "no 'Plan the test'");
    assert.equal($("[data-download]"), null, "no download");
    assert.equal($("[data-primary=approvals]").getAttribute("href"), "#/approvals");
    assert.equal(primaries().length, 1);
    await open(`#/campaigns/${CID}/audience`);
    await until(() => $("[data-audience] [data-list-held=pending]"), 3000, "the audience tab's hold");
    assert.equal($("[data-download]"), null, "no download on the Audience tab");
    // Turned down: no download either, and a new campaign is the way on.
    world.models = { versions: [waitingAgainst(LIST_MODEL, null)] };
    world.models.versions[0].version.status = "archived";
    await open(`#/campaigns/${CID}/overview`);
    await until(() => $("[data-list-held=refused]"), 3000, "refused");
    assert.equal($("[data-download]"), null);
    assert.equal($("[data-primary=restart]").getAttribute("href"), `#/campaigns/new/customers/${UC}`);
    // The registry cannot be read: the download is held (fails closed).
    world.modelsAnswer = registryDown;
    await open(`#/campaigns/${CID}/audience`);
    await until(() => $("[data-audience] [data-list-held=unknown]"), 3000, "unknown");
    assert.equal($("[data-download]"), null);
    world.modelsAnswer = null;
    // Approved: 'Plan the test' and the download, as before.
    world.models = { versions: [version(LIST_MODEL, "champion", "r_train")] };
    await open(`#/campaigns/${CID}/overview`);
    await until(() => $("[data-primary=plan]"), 3000, "'Plan the test' once approved");
    assert.equal($("[data-list-held]"), null);
    await open(`#/campaigns/${CID}/audience`);
    await until(() => $("[data-download=contact-list]"), 3000, "the download once approved");
  } finally {
    world.modelsAnswer = null;
    world.models = fx("models_empty");
  }
});

test("the model behind a list, read from the registry's answer: unknown when it did not answer, superseded when another became current", () => {
  const M = "m_list";
  const answer = (...rows) => ({ versions: rows });
  assert.equal(rules.listModelState(null, M), "unknown", "no answer: unknown, never approved");
  assert.equal(rules.listModelState(undefined, M), "unknown");
  assert.equal(rules.listModelState({}, M), "unknown", "an answer without versions is not a registry answer");
  assert.equal(rules.listModelState(answer(), M), "approved", "a version the registry does not know, as before");
  assert.equal(rules.listModelState(answer(version(M, "champion", "r")), M), "approved");
  assert.equal(rules.listModelState(answer(version(M, "pending_approval", "r")), M), "pending", "no comparison recorded");
  assert.equal(rules.listModelState(answer(waitingAgainst(M, rules.NO_CHAMPION_AT_DECISION)), M), "pending", "still no current model");
  assert.equal(
    rules.listModelState(answer(waitingAgainst(M, rules.NO_CHAMPION_AT_DECISION), version("m_new", "champion", "r2")), M),
    "superseded",
    "a current model appeared meanwhile",
  );
  assert.equal(rules.listModelState(answer(waitingAgainst(M, "m_old"), version("m_old", "champion", "r0")), M), "pending");
  assert.equal(
    rules.listModelState(answer(waitingAgainst(M, "m_old"), version("m_new", "champion", "r2")), M),
    "superseded",
    "another model replaced the one it was compared with",
  );
  const refused = waitingAgainst(M, null);
  refused.version.status = "archived";
  assert.equal(rules.listModelState(answer(refused), M), "refused");
  assert.equal(rules.listHeld("approved"), false);
  for (const state of ["pending", "refused", "superseded", "unknown"]) assert.equal(rules.listHeld(state), true, state);
});

test("the model behind a list, as the Review screen reads it", () => {
  assert.equal(rules.modelState(null), "approved", "nothing known: nothing held");
  assert.equal(rules.modelState({ status: "champion" }), "approved");
  assert.equal(rules.modelState({ status: "pending_approval" }), "pending");
  assert.equal(rules.modelState({ status: "archived", approved_at: null, promoted_at: null }), "refused");
  assert.equal(rules.modelState({ status: "archived", approved_at: "2026-10-01T00:00:00Z" }), "approved", "a former current model");
  assert.equal(rules.modelState({ status: "archived", promoted_at: "2026-10-01T00:00:00Z" }), "approved");
  assert.equal(rules.modelState({ status: "candidate" }), "approved", "a version scored by name on the classic pages, as before");
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

test("someone who may not start a campaign sees why, and no goal to start", async () => {
  denied.add("POST /campaign-runs");
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
