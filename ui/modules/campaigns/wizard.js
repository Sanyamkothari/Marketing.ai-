// The "New campaign" wizard's behaviour (Plan M §3.1, M177, DEC-1607): five steps on one page, driven by the
// URL and the server, so a refresh resumes where the person was.
//
//   #/campaigns/new                              1 Goal
//   #/campaigns/new/customers/<goal>              2 Customers: Guided setup (Plan G/L), unchanged
//   #/campaigns/new/customers/<goal>/<run>        2 ... while the list is being prepared, or waiting
//   #/campaigns/new/audience/<campaign>           3 Audience (the campaign record exists: a Draft)
//   #/campaigns/new/review/<campaign>             4 Review, then Approve (registers the test plan)
//   #/campaigns/new/send/<campaign>               5 Send: the one download; the campaign is Live
//
// Under the hood, existing routes only (Plan M M7): the first campaign of a goal learns from the approved
// file (`POST /runs`, train) and then builds the list from the same customers (`POST /uploads` and
// `POST /runs`, score); a later one builds the list with the current model. A finished list becomes a campaign
// record (`POST /campaigns`), and Approve registers its test plan (`POST /campaigns/{id}/plan`) with the
// planner's own numbers - so the plan is fixed before the download is offered. A new model waiting for an
// approval holds the campaign, and the wizard says so and links to the approvals page.
//
// The step a URL asks for is checked against the server: a campaign with a test plan opens on Send, one
// without on its Draft steps. The only thing kept in the browser is the file chosen for a first campaign,
// so the list can be built from it once the learning is done; after a refresh the person chooses it again.

import { canAccess, setActiveNav, setupModes } from "../router.js";
import { ApiError } from "../../api.js";
import { COST_NEEDS_CONFIRMATION } from "../../cost.js";
import { errorBox } from "../../dom.js";
import { campaignStep } from "../measure/rule.js";
import {
  getCampaign,
  getCampaignsOfRun,
  getComplianceSummary,
  getCostEstimate,
  getIndustries,
  getMe,
  getModels,
  getPlanPreview,
  getRun,
  getScoringSummary,
  getTreatListSummary,
  getUseCase,
  postApprove,
  postCampaign,
  postPlan,
  postRun,
  postUpload,
  treatListUrl,
} from "./wizard_api.js";
import {
  baseRateOf,
  currentModel,
  goalCards,
  parseRoute,
  planBody,
  progressOf,
  resultsDate,
  runState,
  stepForCampaign,
  stepHref,
  versionById,
} from "./wizard_rules.js";
import {
  TITLE,
  audienceHtml,
  customersHtml,
  goalHtml,
  loadingHtml,
  noticeHtml,
  pageHtml,
  progressHtml,
  reviewHtml,
  sendHtml,
  stoppedHtml,
  waitingHtml,
} from "./wizard_views.js";
import { injectWizardStyles } from "./wizard_styles.js";

/** How often a list being prepared is asked about, and a model waiting for an approval. */
export const POLL_MS = 2000;
export const WAIT_POLL_MS = 15000;

const ui = {
  seq: 0,
  app: null,
  parts: null,
  timer: null,
  screen: null, // { step, body() , after?() } - redrawn in place for a local change (busy, error)
  captured: new Map(), // goal id -> the File chosen in Guided setup, for a first campaign's list
  started: null, // the upload id the last start used, so Guided setup knows its approval was taken
  lastStart: null, // { uc, current, fill } of the last start, for "Start it anyway"
  customers: { problem: null, busy: "" },
  review: { busy: false, error: null },
  approval: { busy: false, error: null, own: false }, // the inline approval of a new model (step 2)
  advancing: new Set(), // run ids already being taken to their next step
};

/** Whether the page still shows this run's progress (the person may have gone elsewhere meanwhile). */
const stillOn = (runId) => parseRoute(hashParts()).runId === runId;

const hashParts = () => window.location.hash.replace(/^#\/?/, "").split("/").filter(Boolean);

/** May this person start a campaign: start a list (`POST /runs`) and record the campaign (`POST /campaigns`). */
const canStart = () => canAccess("POST", "/runs") && canAccess("POST", "/campaigns");

function go(hash, { replace = false } = {}) {
  if (replace) window.location.replace(hash);
  else window.location.hash = hash;
}

/** Draw the current screen: the page, its step, then whatever mounts into it (Guided setup). */
function draw() {
  const screen = ui.screen;
  if (!screen || !ui.app) return;
  ui.app.innerHTML = pageHtml(screen.step, screen.body());
  if (screen.after) screen.after(ui.app);
}

function show(token, step, body, after = null) {
  if (token !== ui.seq) return;
  ui.screen = { step, body, after };
  draw();
}

function later(token, ms) {
  clearTimeout(ui.timer);
  ui.timer = setTimeout(() => {
    if (token === ui.seq) renderWizard(ui.app, hashParts());
  }, ms);
}

/**
 * Draw the wizard for this route (`parts` is the hash split on "/": `campaigns`, `new`, ...). Called by the
 * owner of the `campaigns` route for `#/campaigns/new...` (today Plan J's campaign screen delegates to it).
 */
export async function renderWizard(app, parts) {
  injectWizardStyles();
  setActiveNav("results"); // in the classic layout campaigns sit under Results
  document.title = `${TITLE} · Marketing AI`;
  clearTimeout(ui.timer);
  const token = ++ui.seq;
  const route = parseRoute(parts);
  // The same URL drawn again (a poll, a module that registered late) keeps the screen's own state and its
  // content until the new answers are in; another URL starts clean, with a loading line.
  const sameRoute = Boolean(ui.parts) && ui.parts.join("/") === parts.join("/");
  if (!sameRoute) {
    ui.customers = { problem: null, busy: "" };
    ui.review = { busy: false, error: null };
    ui.approval = { busy: false, error: null, own: false };
  }
  ui.app = app;
  ui.parts = [...parts];
  if (!sameRoute || !app.querySelector('[data-module="campaign-wizard"]')) {
    show(token, route.step, () => loadingHtml());
  }
  try {
    if (route.step === "goal") await goalStep(token);
    else if (route.step === "customers") await customersStep(token, route);
    else await campaignStepScreen(token, route);
  } catch (error) {
    show(token, route.step, () => errorBox(error, { retry: true }));
  }
}

// --- 1 Goal ------------------------------------------------------------------------------------------------

async function goalStep(token) {
  if (!canStart()) {
    show(token, "goal", () => goalHtml({ canStart: false }));
    return;
  }
  const industries = await getIndustries();
  const cards = goalCards(industries);
  show(token, "goal", () => goalHtml({ ...cards, canStart: true }));
}

// --- 2 Customers -------------------------------------------------------------------------------------------

/** Guided setup as its module registered it (`registerSetupMode`, Plan G M75), or null when not loaded. */
const guidedMode = (uc) => setupModes(uc).find((mode) => mode.name === "guided") || null;

async function customersStep(token, route) {
  const uc = await getUseCase(route.ucId);
  if (token !== ui.seq) return;
  // A goal that does not contact customers has no campaign: it opens its classic page (Plan M M6).
  if (campaignStep(uc) !== "measure") {
    go(`#/uc/${encodeURIComponent(uc.id)}`, { replace: true });
    return;
  }
  const models = await getModels(uc.id);
  const current = currentModel(models);
  if (!route.runId) {
    showCustomers(token, uc, current);
    return;
  }
  const detail = await getRun(route.runId);
  await onRun(token, uc, models, detail);
}

function showCustomers(token, uc, current) {
  const mode = guidedMode(uc);
  const allowed = canStart();
  show(
    token,
    "customers",
    () =>
      customersHtml({
        uc,
        first: !current,
        canStart: allowed,
        hasGuided: Boolean(mode),
        problem: ui.customers.problem,
        busy: ui.customers.busy,
      }),
    (app) => {
      if (!allowed || !mode) return;
      const main = app.querySelector("[data-cw-guided-main]");
      const aside = app.querySelector("[data-cw-guided-aside]");
      const wrap = app.querySelector("[data-cw-guided]");
      if (!main || !wrap) return;
      // The file chosen in Guided setup is kept for a first campaign: once the model has learned from it, the
      // list is built from the same customers without asking again. Read from the change event as it bubbles;
      // Guided setup itself is not touched.
      wrap.addEventListener(
        "change",
        (event) => {
          const input = event.target;
          if (input && input.type === "file" && input.files && input.files[0]) ui.captured.set(uc.id, input.files[0]);
        },
        true,
      );
      mode.mount(
        { main, aside },
        {
          uc,
          mode: current ? "score" : "train",
          mayRun: canAccess("POST", "/runs"),
          firstStep: 1,
          modelVersionId: current ? current.model_id : null,
          holds: (uploadId) => ui.started === uploadId,
          approved: (fill) => start({ uc, current, fill }),
          manual: () => go(`#/uc/${encodeURIComponent(uc.id)}`),
        },
      );
    },
  );
}

/** Approve in Guided setup: start the list - learning first on a goal's first campaign. */
async function start({ uc, current, fill }, { confirmCost = false } = {}) {
  const here = window.location.hash;
  ui.lastStart = { uc, current, fill };
  ui.started = fill.upload.upload_id;
  ui.customers = { problem: null, busy: current ? "Starting the contact list…" : "Starting…" };
  draw();
  const body = {
    use_case: uc.id,
    mode: current ? "score" : "train",
    upload_id: fill.upload.upload_id,
    primary_key: fill.primaryKey,
    overrides: fill.overrides || {},
  };
  if (current) body.model_version_id = current.model_id;
  else body.target = fill.target;
  if (confirmCost) body.confirm_cost = true;
  try {
    const created = await postRun(body);
    ui.customers = { problem: null, busy: "" };
    if (window.location.hash === here) go(stepHref("customers", uc.id, created.run_id));
  } catch (error) {
    ui.started = null; // Guided setup then offers "Use the prepared file", which starts again
    ui.customers = { problem: refusal(error), busy: "" };
    if (window.location.hash === here) draw();
  }
}

/** A refused start as the screen shows it: the checks' report, a cost to confirm, or the error. */
function refusal(error) {
  if (error instanceof ApiError && error.status === 409 && error.body && error.body.validation) {
    return { validation: error.body.validation };
  }
  if (error instanceof ApiError && error.code === COST_NEEDS_CONFIRMATION) return { confirm: error.message };
  return { error };
}

/** A list being prepared, or prepared: show how far it got, or take it to its next step. */
async function onRun(token, uc, models, detail) {
  const run = detail.run;
  const state = runState(detail);
  const learning = run.mode === "train";
  if (state === "pending" || state === "running") {
    show(token, "customers", () => progressHtml({ learning, share: progressOf(detail) }));
    later(token, POLL_MS);
    return;
  }
  if (state !== "done") {
    const reason =
      state === "cancelled"
        ? "The work was cancelled before the list was ready."
        : "The work stopped before the list was ready.";
    show(token, "customers", () => stoppedHtml({ ucId: uc.id, detail, reason }));
    return;
  }
  if (learning) {
    await afterLearning(token, uc, models, detail);
    return;
  }
  await toCampaign(token, uc, detail);
}

/** The model learned: use it when it is the current one; wait while it waits for an approval. */
async function afterLearning(token, uc, models, detail) {
  const version = versionById(models, detail.run.model_version_id);
  const status = version && version.status;
  const current = currentModel(models);
  if (status === "pending_approval") {
    const canApprove = canAccess("POST", "/models/{model_id}/approve");
    show(
      token,
      "customers",
      () => waitingHtml({ canApprove, own: ui.approval.own, busy: ui.approval.busy, error: ui.approval.error }),
      (app) => {
        const button = app.querySelector("[data-cw-approve-model]");
        if (button) button.addEventListener("click", () => approveModel(version));
      },
    );
    if (!canApprove || ui.approval.own) later(token, WAIT_POLL_MS); // an Approver elsewhere may approve it
    return;
  }
  const model = status === "champion" ? version : current;
  if (!model) {
    show(token, "customers", () =>
      stoppedHtml({ ucId: uc.id, detail, reason: "Nothing was learned that the contact list can use." }),
    );
    return;
  }
  const file = ui.captured.get(uc.id);
  if (!file) {
    // After a refresh, or a file picked from a connection: the customers are chosen once more, for the list.
    go(stepHref("customers", uc.id), { replace: true });
    return;
  }
  const key = detail.run.run_id;
  if (ui.advancing.has(key)) return;
  ui.advancing.add(key);
  show(token, "customers", () => progressHtml({ learning: false, share: null }));
  try {
    const upload = await postUpload(file, uc.id, "score");
    const created = await postRun({
      use_case: uc.id,
      mode: "score",
      upload_id: upload.upload_id,
      primary_key: detail.run.primary_key,
      model_version_id: model.model_id,
    });
    ui.captured.delete(uc.id);
    if (stillOn(detail.run.run_id)) go(stepHref("customers", uc.id, created.run_id), { replace: true });
  } catch (error) {
    ui.advancing.delete(key);
    show(token, "customers", () =>
      noticeHtml({
        title: "The contact list could not be started",
        text: ["Choose the file of customers once more; it is checked against what Marketing AI learned."],
        action: { label: "Choose the customers", href: stepHref("customers", uc.id) },
        attrs: "data-cw-list-refused",
      }) + errorBox(error),
    );
  }
}

/**
 * Approve the new model here (`POST /models/{id}/approve`, the approvals page's own call), for a person with
 * the Approver role. The name sent is the signed-in user's (the server takes it from the session when sign-in
 * is on); no reason is made up for them. The trainer's own approval is refused by the server (separation of
 * duties): the screen then says another Approver must do it.
 */
async function approveModel(version) {
  if (ui.approval.busy) return;
  const here = window.location.hash;
  ui.approval = { busy: true, error: null, own: false };
  draw();
  try {
    const me = await getMe().catch(() => null);
    const who = (me && me.principal && (me.principal.username || me.principal.user_id)) || "local operator";
    await postApprove(version.model_id, { approved_by: who });
    ui.approval = { busy: false, error: null, own: false };
  } catch (error) {
    const own = error instanceof ApiError && error.code === "SEPARATION_OF_DUTIES";
    ui.approval = { busy: false, error: own ? null : error, own };
  }
  if (window.location.hash === here) renderWizard(ui.app, hashParts());
}

/** The list is ready: the campaign record made from it (once, even across a refresh), then Audience. */
async function toCampaign(token, uc, detail) {
  const runId = detail.run.run_id;
  if (ui.advancing.has(runId)) return;
  ui.advancing.add(runId);
  try {
    const existing = await getCampaignsOfRun(runId);
    let campaign = ((existing && existing.campaigns) || [])[0] || null;
    if (!campaign) campaign = (await postCampaign({ run_id: runId })).campaign;
    if (stillOn(runId)) go(stepHref("audience", campaign.campaign_id), { replace: true });
  } finally {
    ui.advancing.delete(runId);
  }
}

// --- 3 Audience, 4 Review, 5 Send ------------------------------------------------------------------------

async function campaignStepScreen(token, route) {
  const view = await getCampaign(route.campaignId);
  if (token !== ui.seq) return;
  const step = stepForCampaign(view, route.step);
  if (step !== route.step) {
    go(stepHref(step, route.campaignId), { replace: true });
    return;
  }
  const campaign = view.campaign;
  const runId = (campaign.run_ids || [])[0];
  if (step === "send") {
    const plan = view.plan;
    show(token, "send", () =>
      sendHtml({ campaign, plan, downloadHref: treatListUrl(runId), resultsOn: (plan && plan.analysis_date) || resultsDate(campaign) }),
    );
    return;
  }
  const [detail, scoring, list] = await Promise.all([getRun(runId), getScoringSummary(runId), getTreatListSummary(runId)]);
  const preview = await getPlanPreview(campaign.campaign_id, baseRateOf(detail.run, scoring)).catch(() => null);
  if (step === "audience") {
    show(token, "audience", () => audienceHtml({ ucId: campaign.use_case_id, scoring, list, preview }), (app) => {
      const next = app.querySelector("[data-cw-next]");
      if (next) next.addEventListener("click", () => go(stepHref("review", campaign.campaign_id)));
    });
    return;
  }
  const [cost, compliance] = await Promise.all([
    getCostEstimate(campaign.use_case_id, "score").catch(() => null),
    list && list.compliance_rule_version ? getComplianceSummary(runId).catch(() => null) : Promise.resolve(null),
  ]);
  const context = { campaign, detail, preview };
  show(
    token,
    "review",
    () =>
      reviewHtml({
        campaign,
        list,
        preview,
        cost,
        compliance,
        resultsOn: resultsDate(campaign),
        canApprove: canAccess("POST", "/campaigns/{campaign_id}/plan"),
        busy: ui.review.busy,
        error: ui.review.error,
      }),
    (app) => {
      const button = app.querySelector("[data-cw-approve]");
      if (button) button.addEventListener("click", () => approve(context));
    },
  );
}

/**
 * The outcome column the plan names: the one the list's model learned to predict (its learning run's
 * `target`), else the goal's own outcome column.
 */
async function outcomeColumnOf(uc, detail) {
  try {
    const models = await getModels(uc.id);
    const version = versionById(models, detail.run.model_version_id);
    if (version && version.run_id) {
      const learned = await getRun(version.run_id);
      if (learned && learned.run && learned.run.target) return learned.run.target;
    }
  } catch {
    // the goal's own column below
  }
  return uc.target && uc.target.column;
}

/** Approve: register the test plan with the planner's numbers, then Send. */
async function approve({ campaign, detail, preview }) {
  if (ui.review.busy) return;
  const here = window.location.hash;
  ui.review = { busy: true, error: null };
  draw();
  try {
    const uc = await getUseCase(campaign.use_case_id);
    const outcomeColumn = await outcomeColumnOf(uc, detail);
    await postPlan(campaign.campaign_id, planBody({ campaign, preview, uc, outcomeColumn }));
    ui.review = { busy: false, error: null };
    if (window.location.hash === here) go(stepHref("send", campaign.campaign_id));
  } catch (error) {
    ui.review = { busy: false, error };
    if (window.location.hash === here) draw();
  }
}

// --- delegated events: the goal cards and "Start it anyway" -------------------------------------------------

document.addEventListener("click", (event) => {
  const target = event.target;
  if (!target || typeof target.closest !== "function") return;
  const goal = target.closest("[data-cw-goal]");
  if (goal) {
    go(stepHref("customers", goal.getAttribute("data-cw-goal")));
    return;
  }
  if (target.closest("[data-cw-confirm-cost]") && ui.lastStart) {
    start(ui.lastStart, { confirmCost: true });
  }
});
