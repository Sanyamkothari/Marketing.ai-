// The "New campaign" wizard's behaviour (Plan M §3.1, M177, DEC-1607): five steps on one page, driven by the
// URL and the server, so a refresh resumes where the person was.
//
//   #/campaigns/new                              1 Goal
//   #/campaigns/new/customers/<goal>              2 Customers: the Data Doctor
//   #/campaigns/new/review/<campaign>             3 Audience and 4 Review on one screen (a Draft campaign),
//                                                   then "Approve and download"
//   #/campaigns/new/audience/<campaign>           (the address of step 3 before M181 part C: opens Review)
//   #/campaigns/new/send/<campaign>               5 Send: the campaign is Live; the download (again)
//
// Under the hood: "Start" asks the server to run the whole campaign (`POST /campaign-runs`: learn from the file, build
// the list from the same customers, record the campaign) and moves to its progress page (`./run_page.js`), so the
// work goes on when the tab closes. Nothing is chained in the browser. A goal that already has a model skips the
// learning. The finished campaign comes back here, to Review: "Approve and download" registers its test plan
// (`POST /campaigns/{id}/plan`) with the planner's own numbers and, only once the server has accepted it, downloads
// the list's served file - so the plan is fixed before any download - then shows Send.
//
// A new model waiting for an approval (M181 part C, DEC-1611): the campaign run builds the list with the model it
// just learned even while it waits. For a person with the Approver role the Review screen's one button says it
// approves the new model too: it calls the approvals page's own route (`POST /models/{id}/approve`) first, then
// the plan, then the download, each only once the one before it worked. The server refuses the trainer's own
// approval while sign-in is on (separation of duties); another Approver then approves it.
// A list built with a model that is not approved (yet) is held wherever it could be sent from: Review, Send and
// the campaign's own page (`listModelState`) offer no plan and no download until it is. They fail closed: when
// the registry cannot be read, nothing is offered and the page asks again.
//
// The step a URL asks for is checked against the server: a campaign with a test plan opens on Send, one
// without on its Draft screen (Review).

import { canAccess, setActiveNav, setupModes } from "../router.js";
import { ApiError } from "../../api.js";
import { errorBox } from "../../dom.js";
import { campaignStep } from "../measure/rule.js";
import {
  getCampaign,
  getComplianceSummary,
  getCostEstimate,
  getIndustries,
  getMe,
  getModels,
  getPlanPreview,
  getModelStory,
  getResultsSummary,
  getRun,
  getScoringSummary,
  getTreatListSummary,
  getUseCase,
  postApprove,
  postCampaignRun,
  postPlan,
  treatListUrl,
} from "./wizard_api.js";
import { getUpliftArtefact } from "../uplift/api.js";
import {
  baseRateOf,
  currentModel,
  expectedValue,
  goalCards,
  isWizardRoute,
  listHeld,
  listModelState,
  parseRoute,
  planBody,
  resultsDate,
  stepForCampaign,
  stepHref,
  versionById,
} from "./wizard_rules.js";
import {
  TITLE,
  customersHtml,
  goalHtml,
  loadingHtml,
  pageHtml,
  reviewHtml,
  sendHtml,
} from "./wizard_views.js";
import { DEFAULT_SPEED, runHref } from "./run_rules.js";
import { injectWizardStyles } from "./wizard_styles.js";
import { mountResultsTabs } from "./results_mount.js";

/** How often a model waiting for an approval is asked about. */
export const WAIT_POLL_MS = 15000;

const ui = {
  seq: 0,
  app: null,
  parts: null,
  timer: null,
  screen: null, // { step, body() , after?() } - redrawn in place for a local change (busy, error)
  started: null, // the upload id the last start used, so the Data Doctor knows its approval was taken
  customers: { problem: null, busy: "" },
  // The Review screen's one button: `busy` is its label while its calls are on their way (else ""), `own` says
  // the server refused this person's approval of the new model because they started the learning.
  review: { busy: "", error: null, own: false },
  justDownloaded: null, // the Send address "Approve and download" went to after starting the download
  chosenGoal: null, // the goal card last chosen: drawn chosen when the person comes back to step 1 (M187)
};

/** Whether work started under `token` is still the page's: no newer draw, and the same route on screen. */
const ours = (token) => token === ui.seq && onRoute();

const hashParts = () => window.location.hash.replace(/^#\/?/, "").split("/").filter(Boolean);

/**
 * Whether the page still shows the route the wizard's current work was started for. The person may have left
 * (a Review waiting for an approval polls): a poll or an answer still on its way then draws nothing over the page they
 * went to. `ui.parts` is the route `renderWizard` last drew; `ui.seq` alone does not know about other modules.
 */
const onRoute = () => {
  const now = hashParts();
  return isWizardRoute(now) && Boolean(ui.parts) && now.join("/") === ui.parts.join("/");
};

/** May this person start a campaign (`POST /campaign-runs`). */
const canStart = () => canAccess("POST", "/campaign-runs");

function go(hash, { replace = false } = {}) {
  if (replace) window.location.replace(hash);
  else window.location.hash = hash;
}

/** Draw the current screen: the page, its step, then whatever mounts into it (the Data Doctor). */
function draw() {
  const screen = ui.screen;
  if (!screen || !ui.app || !onRoute()) return;
  ui.app.innerHTML = pageHtml(screen.step, screen.body());
  if (screen.after) screen.after(ui.app);
}

function show(token, step, body, after = null) {
  if (!ours(token)) return;
  ui.screen = { step, body, after };
  draw();
}

function later(token, ms) {
  clearTimeout(ui.timer);
  ui.timer = setTimeout(() => {
    if (ours(token)) renderWizard(ui.app, hashParts());
  }, ms);
}

// Leaving the wizard stops its poll and retires its work in flight: nothing it started draws again.
window.addEventListener("hashchange", () => {
  if (isWizardRoute(hashParts())) return;
  clearTimeout(ui.timer);
  ui.seq += 1;
  ui.justDownloaded = null;
  ui.chosenGoal = null; // a fresh visit starts with no goal chosen (M187 review)
  // Coming back is a fresh visit: the screen's own state (a refusal, "downloading") is not carried over.
  ui.parts = null;
});

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
    ui.review = { busy: "", error: null, own: false };
  }
  // Send says "downloading" only on the screen the download was started for; any other address forgets it.
  if (ui.justDownloaded !== `#/${parts.join("/")}`) ui.justDownloaded = null;
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
  show(token, "goal", () => goalHtml({ ...cards, canStart: true, selected: ui.chosenGoal }));
}

// --- 2 Customers -------------------------------------------------------------------------------------------

/** The Data Doctor as its module registered it (`registerSetupMode`, Plan G M75), or null when not loaded. */
const doctorMode = (uc) => setupModes(uc).find((mode) => mode.name === "datadoctor") || null;

async function customersStep(token, route) {
  const uc = await getUseCase(route.ucId);
  if (!ours(token)) return;
  // A goal that does not contact customers has no campaign: it opens its classic page (Plan M M6).
  if (campaignStep(uc) !== "measure") {
    go(`#/uc/${encodeURIComponent(uc.id)}`, { replace: true });
    return;
  }
  // An address from when the browser chained the steps (`.../customers/<goal>/<run>`) opens the step afresh.
  if (route.runId) {
    go(stepHref("customers", uc.id), { replace: true });
    return;
  }
  const models = await getModels(uc.id);
  if (!ours(token)) return;
  showCustomers(token, uc, currentModel(models));
}

function showCustomers(token, uc, current) {
  const mode = doctorMode(uc);
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
      if (!main) return;
      mode.mount(
        { main, aside },
        {
          uc,
          mode: current ? "score" : "train",
          mayRun: canAccess("POST", "/campaign-runs"),
          firstStep: 1,
          modelVersionId: current ? current.model_id : null,
          holds: (uploadId) => ui.started === uploadId,
          approved: (fill) => start({ uc, current, fill }),
          manual: () => go(`#/uc/${encodeURIComponent(uc.id)}/manual`),
        },
      );
    },
  );
}

/**
 * Approve in the Data Doctor: ask the server to run the campaign and go to its progress page.
 * The learning time the person chose (`fill.speed`) goes with it; a goal that has a model skips the learning.
 */
async function start({ uc, current, fill }) {
  const here = window.location.hash;
  ui.started = fill.upload.upload_id;
  ui.customers = { problem: null, busy: "Starting…" };
  draw();
  const body = {
    use_case: uc.id,
    upload_id: fill.upload.upload_id,
    primary_key: fill.primaryKey,
    speed: fill.speed || DEFAULT_SPEED,
    overrides: fill.overrides || {},
  };
  if (fill.caseId) body.case_id = fill.caseId; // so the results can link back to "What the Doctor did"
  if (current) body.model_version_id = current.model_id;
  else body.target = fill.target;
  try {
    const created = await postCampaignRun(body);
    ui.customers = { problem: null, busy: "" };
    if (window.location.hash === here) go(runHref(created.record.campaign_run_id));
  } catch (error) {
    ui.started = null; // the Data Doctor then offers "Use the prepared file", which starts again
    ui.customers = { problem: { error }, busy: "" };
    if (window.location.hash === here) draw();
  }
}

// --- 3 Audience and 4 Review (one screen), 5 Send ------------------------------------------------------------

async function campaignStepScreen(token, route) {
  // A campaign exists now: the goal chosen for it is spent, so a New campaign started from here starts clean.
  ui.chosenGoal = null;
  const view = await getCampaign(route.campaignId);
  if (!ours(token)) return;
  // A campaign whose results were added is past the wizard: its own page shows them.
  if (view.report) {
    go(`#/campaigns/${encodeURIComponent(route.campaignId)}`, { replace: true });
    return;
  }
  const step = stepForCampaign(view, route.step);
  if (step !== route.step) {
    go(stepHref(step, route.campaignId), { replace: true });
    return;
  }
  const campaign = view.campaign;
  const runId = (campaign.run_ids || [])[0];
  if (step === "send") {
    const plan = view.plan;
    // A plan can be registered elsewhere (Plan J's page) for a list whose model is not approved: Send then holds
    // the download too. A run or a registry that cannot be read holds it (`unknown`), and the page asks again.
    const [sent, registry] = await Promise.all([getRun(runId).catch(() => null), registryOf(campaign.use_case_id)]);
    if (!ours(token)) return;
    const held = sent && sent.run ? listModelState(registry.models, sent.run.model_version_id) : "unknown";
    show(token, "send", () =>
      sendHtml({
        campaign,
        plan,
        downloadHref: treatListUrl(runId),
        resultsOn: (plan && plan.analysis_date) || resultsDate(campaign),
        downloaded: ui.justDownloaded === `#/${ui.parts.join("/")}`,
        held: listHeld(held) ? held : null,
      }),
    );
    if (held === "unknown" || held === "pending") later(token, WAIT_POLL_MS);
    return;
  }
  const [detail, scoring, list, registry, summary] = await Promise.all([
    getRun(runId),
    getScoringSummary(runId),
    getTreatListSummary(runId),
    registryOf(campaign.use_case_id),
    getResultsSummary(runId, { canSeeNames: canAccess("GET", "/campaign-runs/{campaign_run_id}/top-customers") }),
  ]);
  const preview = await getPlanPreview(campaign.campaign_id, baseRateOf(detail.run, scoring)).catch(() => null);
  const [cost, compliance, policy] = await Promise.all([
    getCostEstimate(campaign.use_case_id, "score").catch(() => null),
    list && list.compliance_rule_version ? getComplianceSummary(runId).catch(() => null) : Promise.resolve(null),
    // The engine's range for a net value, when it gives one (`expectedValue`); asked only for a net value.
    expectedValue(list) && expectedValue(list).kind === "net"
      ? getUpliftArtefact(runId, "policy_recommendation.json").catch(() => null)
      : Promise.resolve(null),
  ]);
  // The model the list was built with, and whether this person may approve it while it waits (M181 part C).
  // A registry that did not answer is `unknown`, never `approved`: nothing is offered on a guess.
  const version = versionById(registry.models, detail.run.model_version_id);
  const model = {
    state: listModelState(registry.models, detail.run.model_version_id),
    canApprove: canAccess("POST", "/models/{model_id}/approve"),
    own: ui.review.own,
    version,
    error: registry.error,
  };
  const story = version && version.run_id ? await getModelStory(version.run_id) : null;
  const context = { campaign, detail, preview, model, runId };
  show(
    token,
    "review",
    () =>
      reviewHtml({
        campaign,
        scoring,
        list,
        policy,
        preview,
        cost,
        compliance,
        resultsOn: resultsDate(campaign),
        canApprove: canAccess("POST", "/campaigns/{campaign_id}/plan"),
        model,
        summary: { ...summary, story },
        busy: ui.review.busy,
        error: ui.review.error,
      }),
    (app) => {
      const button = app.querySelector("[data-cw-approve]");
      if (button) button.addEventListener("click", () => approve(context));
      mountResultsTabs(app, {
        useCaseId: campaign.use_case_id,
        trainRunId: version && version.run_id,
        scoreRunId: runId,
        caseId: summary.caseId,
      });
    },
  );
  // Held for an Approver elsewhere: the screen carries on by itself once the new model is approved. A registry
  // that did not answer is asked again the same way.
  if ((model.state === "pending" && (!model.canApprove || model.own)) || model.state === "unknown") {
    later(token, WAIT_POLL_MS);
  }
}

/** The goal's registry (`GET /models?use_case=`) as `{ models, error }`: a failed call is kept, not swallowed. */
async function registryOf(ucId) {
  try {
    return { models: await getModels(ucId), error: null };
  } catch (error) {
    return { models: null, error };
  }
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

/**
 * Start the download of a served file, as the person's own click on a download link does: the browser saves
 * the file the server sends (the address is the one Send's "Download" link has always pointed at).
 */
export function startDownload(href) {
  const link = document.createElement("a");
  link.href = href;
  link.setAttribute("download", "");
  link.setAttribute("data-cw-autodownload", "");
  link.hidden = true;
  document.body.appendChild(link);
  try {
    link.click();
  } finally {
    link.remove();
  }
}

/**
 * Approve the new model (`POST /models/{id}/approve`, the approvals page's own call), for a person with the
 * Approver role. The name sent is the signed-in user's (the server takes it from the session when sign-in is
 * on); no reason is made up for them.
 */
async function approveModel(version) {
  const me = await getMe().catch(() => null);
  const who = (me && me.principal && (me.principal.username || me.principal.user_id)) || "local operator";
  await postApprove(version.model_id, { approved_by: who });
}

/**
 * The Review screen's one button (M181 part C, DEC-1611), each call only once the one before it worked:
 *   1. when the list's model still waits for an approval, approve it (the trainer's own approval is refused by
 *      the server, separation of duties: the screen then says another Approver must do it, and nothing else
 *      happens);
 *   2. register the test plan with the planner's numbers (`POST /campaigns/{id}/plan`), so it is fixed;
 *   3. only once the server has accepted the plan, download the list's served file, and show Send.
 * A refusal at any point shows its error and nothing after it happens: no plan without the model's approval,
 * no download without the plan. A person who left the page meanwhile gets no download over another page.
 */
async function approve({ campaign, detail, preview, model, runId }) {
  if (ui.review.busy) return;
  // Only a list whose model is approved, or waiting for this person's approval, has the button at all.
  if (!model || (model.state !== "approved" && model.state !== "pending")) return;
  const here = window.location.hash;
  const stay = () => window.location.hash === here;
  let triedModel = false;
  try {
    if (model.state === "pending") {
      ui.review = { busy: "Approving the new model…", error: null, own: false };
      draw();
      try {
        triedModel = true;
        await approveModel(model.version);
      } catch (error) {
        if (error instanceof ApiError && error.code === "SEPARATION_OF_DUTIES") {
          ui.review = { busy: "", error: null, own: true };
          if (stay()) renderWizard(ui.app, hashParts());
          return;
        }
        throw error;
      }
    }
    ui.review = { busy: "Approving…", error: null, own: false };
    draw();
    const uc = await getUseCase(campaign.use_case_id);
    const outcomeColumn = await outcomeColumnOf(uc, detail);
    await postPlan(campaign.campaign_id, planBody({ campaign, preview, uc, outcomeColumn }));
    ui.review = { busy: "", error: null, own: false };
    if (!stay()) return;
    startDownload(treatListUrl(runId));
    ui.justDownloaded = stepHref("send", campaign.campaign_id);
    go(ui.justDownloaded);
  } catch (error) {
    ui.review = { busy: "", error, own: false };
    if (!stay()) return;
    // Once the new model's approval was tried, the screen is drawn again from the server, whatever the answer:
    // approved before the refusal (trying again only fixes the plan), or approved, turned down or passed over
    // elsewhere meanwhile (the button then matches where the model stands, instead of posting the same approval
    // again). The error stays on screen: the route is the same.
    if (triedModel) renderWizard(ui.app, hashParts());
    else draw();
  }
}

// --- delegated events: the goal cards -------------------------------------------------

document.addEventListener("click", (event) => {
  const target = event.target;
  if (!target || typeof target.closest !== "function") return;
  const goal = target.closest("[data-cw-goal]");
  if (goal) {
    // The card shows it was chosen (M187) while the next step opens, and again if the person comes back.
    const chosen = goal.getAttribute("data-cw-goal");
    for (const other of document.querySelectorAll("[data-cw-goal].is-selected")) other.classList.remove("is-selected");
    goal.classList.add("is-selected");
    ui.chosenGoal = chosen;
    go(stepHref("customers", chosen));
  }
});
