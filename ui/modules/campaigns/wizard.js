// The "New campaign" wizard's behaviour (Plan M §3.1, M177, DEC-1607): five steps on one page, driven by the
// URL and the server, so a refresh resumes where the person was.
//
//   #/campaigns/new                              1 Goal
//   #/campaigns/new/customers/<goal>              2 Customers: Guided setup (Plan G/L), unchanged
//   #/campaigns/new/customers/<goal>/<run>        2 ... while the list is being prepared, or waiting
//   #/campaigns/new/review/<campaign>             3 Audience and 4 Review on one screen (a Draft campaign),
//                                                   then "Approve and download"
//   #/campaigns/new/audience/<campaign>           (the address of step 3 before M181 part C: opens Review)
//   #/campaigns/new/send/<campaign>               5 Send: the campaign is Live; the download (again)
//
// Under the hood, existing routes only (Plan M M7): the first campaign of a goal learns from the approved
// file (`POST /runs`, train) and then builds the list from the same customers (`POST /uploads` and
// `POST /runs`, score); a later one builds the list with the current model. A finished list becomes a campaign
// record (`POST /campaigns`). "Approve and download" registers its test plan (`POST /campaigns/{id}/plan`) with
// the planner's own numbers and, only once the server has accepted it, downloads the list's served file - so
// the plan is fixed before any download - then shows Send.
//
// A new model waiting for an approval (M181 part C, DEC-1611): for a person with the Approver role the list is
// built with it straight away, and the Review screen's one button says it approves the new model too: it
// calls the approvals page's own route (`POST /models/{id}/approve`) first, then the plan, then the download,
// each only once the one before it worked. Anyone else is held on step 2, as before, with the way to the
// approvals page, and so is the Approver who started the learning while sign-in is on (the server refuses the
// trainer's own approval, separation of duties); nothing is built for them before another Approver has approved.
// A list built with a model that is not approved (yet) is held wherever it could be sent from: Review, Send and
// the campaign's own page (`listModelState`) offer no plan and no download until it is. They fail closed: when
// the registry cannot be read, nothing is offered and the page asks again.
//
// The step a URL asks for is checked against the server: a campaign with a test plan opens on Send, one
// without on its Draft screen (Review). The only thing kept in the browser is the file chosen for a first campaign,
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
  progressOf,
  resultsDate,
  runState,
  stepForCampaign,
  stepHref,
  versionById,
} from "./wizard_rules.js";
import {
  TITLE,
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
  // The Review screen's one button: `busy` is its label while its calls are on their way (else ""), `own` says
  // the server refused this person's approval of the new model because they started the learning.
  review: { busy: "", error: null, own: false },
  justDownloaded: null, // the Send address "Approve and download" went to after starting the download
  advancing: new Set(), // run ids already being taken to their next step
  listOf: new Map(), // learning run id -> the list run started from it, for a person who left and came back
};

/** Whether work started under `token` is still the page's: no newer draw, and the same route on screen. */
const ours = (token) => token === ui.seq && onRoute();

/** Whether the page still shows this run's progress (the person may have gone elsewhere meanwhile). */
const stillOn = (runId) => parseRoute(hashParts()).runId === runId;

const hashParts = () => window.location.hash.replace(/^#\/?/, "").split("/").filter(Boolean);

/**
 * Whether the page still shows the route the wizard's current work was started for. The person may have left
 * (the progress card says they can): a poll or an answer still on its way then draws nothing over the page they
 * went to. `ui.parts` is the route `renderWizard` last drew; `ui.seq` alone does not know about other modules.
 */
const onRoute = () => {
  const now = hashParts();
  return isWizardRoute(now) && Boolean(ui.parts) && now.join("/") === ui.parts.join("/");
};

/** May this person start a campaign: start a list (`POST /runs`) and record the campaign (`POST /campaigns`). */
const canStart = () => canAccess("POST", "/runs") && canAccess("POST", "/campaigns");

function go(hash, { replace = false } = {}) {
  if (replace) window.location.replace(hash);
  else window.location.hash = hash;
}

/** Draw the current screen: the page, its step, then whatever mounts into it (Guided setup). */
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
  show(token, "goal", () => goalHtml({ ...cards, canStart: true }));
}

// --- 2 Customers -------------------------------------------------------------------------------------------

/** Guided setup as its module registered it (`registerSetupMode`, Plan G M75), or null when not loaded. */
const guidedMode = (uc) => setupModes(uc).find((mode) => mode.name === "guided") || null;

async function customersStep(token, route) {
  const uc = await getUseCase(route.ucId);
  if (!ours(token)) return;
  // A goal that does not contact customers has no campaign: it opens its classic page (Plan M M6).
  if (campaignStep(uc) !== "measure") {
    go(`#/uc/${encodeURIComponent(uc.id)}`, { replace: true });
    return;
  }
  const models = await getModels(uc.id);
  const model = currentModel(models);
  if (!route.runId) {
    showCustomers(token, uc, model);
    return;
  }
  const detail = await getRun(route.runId);
  // Nothing more is started or redrawn for a route the person has left (the next steps post and navigate).
  if (!ours(token)) return;
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

/**
 * Whether the signed-in person started this learning: the server then refuses their own approval of what it
 * learned (separation of duties, DEC-862: only between signed-in people, from the run's `requested_by`).
 * `GET /auth/me` failing is not a guess either way: it reaches the error screen, with "Try again".
 */
async function startedByMe(detail) {
  const me = await getMe();
  const principal = me && me.principal;
  const by = detail && detail.run && detail.run.requested_by;
  return Boolean(principal && principal.kind === "user" && by && by === principal.user_id);
}

/**
 * The model learned: use it when it is the current one. A new model waiting for an approval is used straight
 * away by a person with the Approver role who did not start the learning: they approve it on the Review screen
 * together with the plan (M181 part C). Anyone else waits here, as before, until another Approver has approved
 * it, and nothing is built for them. A new model that can no longer be approved (another one became current
 * while it waited, `superseded`) is passed over for the current one, as a version that was never promoted is.
 */
async function afterLearning(token, uc, models, detail) {
  const version = versionById(models, detail.run.model_version_id);
  const status = version && version.status;
  const current = currentModel(models);
  const waiting = listModelState(models, detail.run.model_version_id) === "pending";
  if (waiting) {
    const mayApprove = canAccess("POST", "/models/{model_id}/approve");
    const own = mayApprove && (await startedByMe(detail));
    if (!ours(token)) return;
    if (!mayApprove || own) {
      show(token, "customers", () => waitingHtml({ own }));
      later(token, WAIT_POLL_MS); // an Approver elsewhere may approve it
      return;
    }
  }
  const model = status === "champion" || waiting ? version : current;
  if (!model) {
    show(token, "customers", () =>
      stoppedHtml({ ucId: uc.id, detail, reason: "Nothing was learned that the contact list can use." }),
    );
    return;
  }
  const made = ui.listOf.get(detail.run.run_id);
  if (made) {
    // The list was already started from this learning (the person left and came back): show that one.
    go(stepHref("customers", uc.id, made), { replace: true });
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
      // The settings the person accepted in Guided setup for the learning (the consent column, the value
      // column, the checks they acknowledged): the list is built with the same ones. The run record keeps
      // them, so this holds after a refresh too.
      overrides: detail.run.overrides || {},
    });
    ui.captured.delete(uc.id);
    ui.listOf.set(key, created.run_id);
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

/** The list is ready: the campaign record made from it (once, even across a refresh), then Review. */
async function toCampaign(token, uc, detail) {
  const runId = detail.run.run_id;
  if (ui.advancing.has(runId)) return;
  ui.advancing.add(runId);
  try {
    const existing = await getCampaignsOfRun(runId);
    let campaign = ((existing && existing.campaigns) || [])[0] || null;
    if (!campaign) campaign = (await postCampaign({ run_id: runId })).campaign;
    if (stillOn(runId)) go(stepHref("review", campaign.campaign_id), { replace: true });
  } finally {
    ui.advancing.delete(runId);
  }
}

// --- 3 Audience and 4 Review (one screen), 5 Send ------------------------------------------------------------

async function campaignStepScreen(token, route) {
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
  const [detail, scoring, list, registry] = await Promise.all([
    getRun(runId),
    getScoringSummary(runId),
    getTreatListSummary(runId),
    registryOf(campaign.use_case_id),
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
        busy: ui.review.busy,
        error: ui.review.error,
      }),
    (app) => {
      const button = app.querySelector("[data-cw-approve]");
      if (button) button.addEventListener("click", () => approve(context));
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
