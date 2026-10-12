// Campaigns (Plan M M176, DEC-1606): the list at `#/campaigns` and one campaign's page at
// `#/campaigns/<id>/overview|audience|results|technical` (Plan M §3.2).
//
// ROUTING. `#/campaigns` is claimed by Plan J's module (`ui/modules/decide/index.js`, the router allows one owner
// per first segment), so that module asks `campaignsRoute(app, parts)` first and draws its own page only when this
// answers false. What this module draws:
//
//   #/campaigns                       the list (it used to redirect to Results)
//   #/campaigns/new[/<step>]          M177's wizard (`./wizard_index.js`, M177's entry point), loaded when asked for
//   #/campaigns/<id>/<tab>            the campaign page, one of the four tabs
//   #/campaigns/<id>                  in the campaigns layout (M174's `ui.layout`), a redirect to .../overview;
//                                     in the classic layout it stays Plan J's page, exactly as before
//
// and `#/campaigns/<id>/plan` (anything else after the id) is always Plan J's page, which is how the Technical tab
// and a Draft's "Plan the test" reach it in both layouts.
//
// DATA. Existing routes only (DEC-1606): the campaign (`GET /campaigns/{id}`), its scoring run (`GET /runs/{id}`),
// the contact list's counts and file (`treat_list_summary.json`, `treat_list.csv`), the Value Proof Pack once
// measured (`GET /pilot/proof/{id}`). Adding results is Plan J's own sequence: the file through `POST /uploads`,
// then `POST /campaigns/{id}/outcomes`, then `POST /campaigns/{id}/measure`. "Refresh the list" is Phase 1's
// `POST /runs` in score mode on the same customers, with the current approved model.
//
// A LIST WHOSE MODEL IS NOT APPROVED (M181 part C review, DEC-1611). The wizard builds an Approver's first list
// with a new model that still waits for an approval, which they approve on its Review screen. Until it is
// approved, this page offers neither the download nor "Plan the test" for that list: it reads the goal's registry
// (`GET /models?use_case=`) and the wizard's own rule (`listModelState`). It fails closed: when the registry or
// the list's run cannot be read, the download is held too and the page says so.
//
// Every event is delegated from `document`, so a repaint needs no re-binding.

import { COST_NEEDS_CONFIRMATION } from "../../cost.js";
import { esc, skeleton } from "../../dom.js";
import { USER_TEXT_ATTRIBUTE } from "../../labels.js";
import { canAccess, setActiveNav } from "../router.js";
import {
  getCampaign,
  getCampaigns,
  loadContactList,
  getIndustries,
  getProof,
  getProofs,
  getRun,
  postMeasure,
  postOutcomes,
  postRun,
  postUpload,
  readContactList,
  treatListUrl,
} from "./api.js";
import { getModels } from "../../api.js";
import { resetRunResultsLines } from "./links.js";
import { listHeld, listModelState } from "./wizard_rules.js";
import { outcomesPayload, refreshRequest } from "./requests.js";
import { campaignStatus, goalName, GOAL_NAMES, shortName } from "./status.js";
import {
  TABS,
  audienceTableHtml,
  campaignPageHtml,
  campaignShellHtml,
  campaignsListHtml,
  newCampaignPendingHtml,
} from "./views.js";

export { outcomesPayload, refreshRequest };

export const ROUTE = "campaigns";
export const WIZARD = "new";

const hashParts = () => window.location.hash.replace(/^#\/?/, "").split("/").filter(Boolean);
const enc = encodeURIComponent;

// --- the layout switch (M174), read defensively so this branch merges before or after it --------------------

let layoutModule;

/** True in M174's "campaigns" layout; false in "classic", and whenever `ui/layout.js` is not on this build. */
export async function campaignLayout() {
  if (layoutModule === undefined) {
    try {
      layoutModule = await import("../../layout.js");
    } catch {
      layoutModule = null;
    }
  }
  try {
    return !!(layoutModule && typeof layoutModule.isCampaignLayout === "function" && layoutModule.isCampaignLayout());
  } catch {
    return false;
  }
}

/** Tests: pretend the layout module answered `value` (true, false) or was absent (null). */
export function setLayoutForTests(value) {
  layoutModule = value === null ? null : { isCampaignLayout: () => !!value };
}

// --- the list ------------------------------------------------------------------------------------------------

const list = { error: null, entries: null };

/** At most `limit` requests at a time, answers in input order. */
async function mapLimit(items, limit, call) {
  const out = new Array(items.length);
  let next = 0;
  const worker = async () => {
    while (next < items.length) {
      const i = next;
      next += 1;
      out[i] = await call(items[i], i);
    }
  };
  await Promise.all(Array.from({ length: Math.min(limit, items.length) }, worker));
  return out;
}

/** Goal names the API knows, by id, for goals §4.2 does not name (`GET /industries`, asked only then). */
async function apiGoalNames(campaigns) {
  if (!campaigns.some((c) => c.use_case_id && !GOAL_NAMES[c.use_case_id])) return {};
  try {
    const names = {};
    for (const industry of (await getIndustries()).industries || []) {
      for (const stage of industry.stages || []) {
        for (const uc of stage.use_cases || []) names[uc.id] = uc.name;
      }
    }
    return names;
  } catch {
    return {};
  }
}

/**
 * The ids whose Value Proof Pack can be read: `GET /pilot/proof` lists the newest campaigns' Packs (at most
 * `PROOFS_LISTED`, 20, in api/routes/pilot.py) with a status each; only a `ready` one is asked for in full, so an early
 * look, an unfinished period or a refused Pack never costs a request that answers 409, and the reads are capped.
 */
export function readyProofIds(summaries) {
  return new Set(((summaries && summaries.proofs) || []).filter((p) => p && p.status === "ready").map((p) => p.campaign_id));
}

/**
 * The list's rows: each campaign with its page's answer (status and result date), its Value Proof Pack once measured
 * and ready (the net value or what it changed, with its range: M186 draws the figures, not the Pack's sentence) and
 * its goal. One `GET /pilot/proof` says which Packs are ready; those are read six at a time, like the pages. A row
 * whose Pack is not ready, or cannot be read, shows its verdict's short sentence.
 */
export async function loadListEntries() {
  const campaigns = ((await getCampaigns()) || {}).campaigns || [];
  const measured = campaigns.some((c) => c.status === "measured");
  const [views, summaries, names] = await Promise.all([
    mapLimit(campaigns, 6, (c) => getCampaign(c.campaign_id).catch(() => null)),
    measured ? getProofs().catch(() => null) : null,
    apiGoalNames(campaigns),
  ]);
  const ready = readyProofIds(summaries);
  const proofs = await mapLimit(campaigns, 6, (c) =>
    c.status === "measured" && ready.has(c.campaign_id) ? getProof(c.campaign_id).catch(() => null) : null,
  );
  return campaigns.map((campaign, i) => {
    const view = views[i];
    return {
      campaign,
      view,
      proof: proofs[i] || null,
      goal: goalName(campaign.use_case_id, names[campaign.use_case_id]),
      status: campaignStatus({ campaign, plan: view && view.plan, report: view && view.report }),
    };
  });
}

const onList = () => {
  const p = hashParts();
  return p[0] === ROUTE && p.length === 1;
};

async function renderList(app) {
  setActiveNav("campaigns");
  document.title = "Campaigns · Marketing AI";
  if (!list.entries) app.innerHTML = skeleton("list", { title: "Campaigns" });
  try {
    list.entries = await loadListEntries();
    list.error = null;
  } catch (error) {
    list.error = error;
  }
  if (!onList()) return;
  app.innerHTML = campaignsListHtml({
    entries: list.error ? [] : list.entries || [],
    error: list.error,
    canCreate: canAccess("POST", "/campaigns"),
  });
}

// --- the wizard (M177's file) -------------------------------------------------------------------------------

async function renderNew(app, parts) {
  setActiveNav("campaigns");
  let wizard = null;
  try {
    wizard = await import("./wizard_index.js"); // M177's declared entry point
  } catch {
    wizard = null; // M177 not on this build yet
  }
  const render = wizard && wizard.renderWizard;
  if (typeof render === "function") {
    await render(app, parts);
    return;
  }
  document.title = "New campaign · Marketing AI";
  app.innerHTML = newCampaignPendingHtml();
}

// --- the campaign page ---------------------------------------------------------------------------------------

const page = { id: null, view: null, run: null, summary: null, proof: null, models: undefined };
const results = { busy: null, error: null, notice: null };
const refresh = { asking: false, busy: false, error: null, costMessage: null, started: null, startedHref: null };
const audiences = new Map(); // run id -> { loading, error, list, query, page }

function resetPage(id) {
  Object.assign(page, { id, view: null, run: null, summary: null, proof: null, models: undefined });
  Object.assign(results, { busy: null, error: null, notice: null });
  Object.assign(refresh, {
    asking: false,
    busy: false,
    error: null,
    costMessage: null,
    started: null,
    startedHref: null,
  });
}

const currentTab = () => {
  const p = hashParts();
  return p[0] === ROUTE && decodeURIComponent(p[1] || "") === page.id && TABS.includes(p[2]) ? p[2] : null;
};

const runIdOf = (campaign) => ((campaign && campaign.run_ids) || [])[0] || null;
const hasContactList = (campaign) => !!campaign && campaign.kind === "scored" && !!runIdOf(campaign);

async function loadPage(id) {
  const view = await getCampaign(id);
  const campaign = view.campaign;
  const runId = runIdOf(campaign);
  const [run, summary, proof, models] = await Promise.all([
    runId
      ? getRun(runId).then(
          (b) => (b && b.run) || null,
          () => null,
        )
      : null,
    hasContactList(campaign) ? loadContactList(runId).then((answer) => answer.summary) : null,
    campaign.status === "measured" ? getProof(id).catch(() => null) : null,
    // The registry, for a list made here: whether the model it was built with is approved (null: not answered).
    hasContactList(campaign) ? getModels(campaign.use_case_id).catch(() => null) : undefined,
  ]);
  if (page.id !== id) return;
  Object.assign(page, { view, run, summary, proof, models });
}

/**
 * Where the model behind this campaign's contact list stands, when that holds the list back: `pending`,
 * `refused`, `superseded` or `unknown` (the registry or the list's run could not be read); null when the list may
 * be downloaded (its model is approved) or the campaign has no list made here.
 */
function listHoldOf(campaign, run, models) {
  if (!hasContactList(campaign)) return null;
  const state = run && run.model_version_id ? listModelState(models, run.model_version_id) : "unknown";
  return listHeld(state) ? state : null;
}

/** What the page builders read (`views.js` `campaignPageHtml`). */
function model(tab) {
  const { view, run, summary, proof } = page;
  const campaign = view.campaign;
  const plan = view.plan || null;
  const runId = runIdOf(campaign);
  return {
    campaign,
    view,
    plan,
    report: view.report || null,
    status: campaignStatus({ campaign, plan, report: view.report }),
    goal: goalName(campaign.use_case_id, run && run.use_case_name),
    tab,
    run,
    summary,
    proof,
    audience: runId ? audiences.get(runId) || null : null,
    results,
    refresh,
    contactListHref: hasContactList(campaign) ? treatListUrl(runId) : null,
    listHold: listHoldOf(campaign, run, page.models),
    canAdd:
      canAccess("POST", "/uploads") &&
      canAccess("POST", "/campaigns/{campaign_id}/outcomes") &&
      canAccess("POST", "/campaigns/{campaign_id}/measure"),
    canRefresh:
      !!run && campaign.kind === "scored" && !!(run.upload_id || run.dataset_id) && canAccess("POST", "/runs"),
  };
}

function draw(app = document.getElementById("app")) {
  const tab = currentTab();
  if (!tab || !page.view || !app) return;
  const m = model(tab);
  app.innerHTML = campaignPageHtml(m);
  document.title = `${shortName(m.campaign, m.goal)} · Marketing AI`;
  setActiveNav("campaigns");
}

async function loadAudience(runId, primaryKey) {
  if (!runId || audiences.has(runId)) return;
  const entry = { loading: true, error: null, list: null, query: "", page: 1 };
  audiences.set(runId, entry);
  try {
    entry.list = await readContactList(runId, primaryKey); // one pass, as the file arrives
  } catch (error) {
    entry.error = error;
  }
  entry.loading = false;
  draw();
  if (entry.error) audiences.delete(runId); // the next visit asks again
}

async function renderCampaign(app, id, tab) {
  if (page.id !== id) resetPage(id);
  setActiveNav("campaigns");
  if (page.view)
    draw(app); // another tab of the same campaign: at once, then with fresh answers
  else app.innerHTML = campaignShellHtml();
  try {
    await loadPage(id);
  } catch (error) {
    if (page.id === id && currentTab()) app.innerHTML = campaignShellHtml({ error });
    return;
  }
  // The person may have moved to another campaign while this one loaded: that one draws itself.
  if (page.id !== id || !page.view) return;
  draw(app);
  if (tab === "audience" && hasContactList(page.view.campaign)) {
    await loadAudience(runIdOf(page.view.campaign), page.view.campaign.primary_key);
  }
}

/**
 * True when `parts` is an address this module draws in either layout: the list, the wizard, or a campaign's tab.
 * (A bare campaign id is this module's only in the campaigns layout, where it is redirected to a tab at once.)
 * Plan J's page asks this before it repaints after one of its own requests, so it never draws over these.
 */
export function ownsHash(parts) {
  if (!Array.isArray(parts) || parts[0] !== ROUTE) return false;
  return parts.length === 1 || parts[1] === WIZARD || TABS.includes(parts[2]);
}

/**
 * Draw `parts` when it is one of this module's routes and answer true; answer false for Plan J's page (a bare
 * campaign id in the classic layout, or anything else after the id), which the caller then draws as before.
 */
export async function campaignsRoute(app, parts) {
  if (!Array.isArray(parts) || parts[0] !== ROUTE) return false;
  if (parts.length === 1) {
    await renderList(app);
    return true;
  }
  if (parts[1] === WIZARD) {
    await renderNew(app, parts);
    return true;
  }
  const id = decodeURIComponent(parts[1]);
  if (parts.length === 2) {
    if (!(await campaignLayout())) return false;
    window.location.replace(`#/campaigns/${enc(id)}/overview`);
    return true;
  }
  if (!TABS.includes(parts[2])) return false;
  await renderCampaign(app, id, parts[2]);
  return true;
}

// --- actions, delegated ---------------------------------------------------------------------------------------

const closest = (event, selector) => (event.target && event.target.closest ? event.target.closest(selector) : null);

async function reload() {
  const id = page.id;
  try {
    await loadPage(id);
  } catch (error) {
    results.error = results.error || error;
  }
  resetRunResultsLines();
}

/** `POST /campaigns/{id}/measure`; a campaign whose results are not all in yet answers with its day (a notice). */
async function measureNow(id) {
  try {
    await postMeasure(id);
  } catch (error) {
    if (error && error.code === "CAMPAIGN_NOT_MATURED") {
      results.notice = error.message;
      return;
    }
    throw error;
  }
}

async function addResults(form) {
  const id = page.id;
  const campaign = page.view && page.view.campaign;
  if (!id || !campaign || results.busy) return;
  const input = form.querySelector("[data-results-file-input]");
  const file = input && input.files && input.files[0];
  const dateField = form.querySelector("[data-results-covariate-date]");
  Object.assign(results, { error: null, notice: null });
  if (!file) {
    results.error = { code: "NO_FILE", message: "Choose the results file first." };
    draw();
    return;
  }
  results.busy = "Adding the results…";
  draw();
  try {
    const upload = await postUpload(file, campaign.use_case_id, "score");
    await postOutcomes(id, outcomesPayload(upload.upload_id, page.view.plan, dateField ? dateField.value : ""));
    results.busy = "Reading the result…";
    draw();
    await measureNow(id);
  } catch (error) {
    results.error = error;
  }
  results.busy = null;
  await reload();
  draw();
}

async function readResult() {
  const id = page.id;
  if (!id || results.busy) return;
  Object.assign(results, { busy: "Reading the result…", error: null, notice: null });
  draw();
  try {
    await measureNow(id);
  } catch (error) {
    results.error = error;
  }
  results.busy = null;
  await reload();
  draw();
}

async function startRefresh() {
  const run = page.run;
  if (!run || refresh.busy) return;
  const confirmCost = !!refresh.costMessage;
  Object.assign(refresh, { busy: true, error: null });
  draw();
  try {
    const created = await postRun(refreshRequest(run, { confirmCost }));
    Object.assign(refresh, {
      asking: false,
      costMessage: null,
      started: created.run_id,
      startedHref: `#/uc/${enc(run.use_case_id)}/run/${enc(created.run_id)}`,
    });
  } catch (error) {
    if (error && error.code === COST_NEEDS_CONFIRMATION && !confirmCost) refresh.costMessage = error.message;
    else refresh.error = error;
  }
  refresh.busy = false;
  draw();
}

function audienceOfPage() {
  const runId = page.view ? runIdOf(page.view.campaign) : null;
  return runId ? audiences.get(runId) : null;
}

function repaintAudienceTable() {
  const audience = audienceOfPage();
  const holder = document.querySelector("[data-audience-table]");
  if (audience && audience.list && holder) holder.innerHTML = audienceTableHtml(audience);
}

if (typeof document !== "undefined") {
  document.addEventListener("click", (event) => {
    if (!page.view || !currentTab()) return;
    if (closest(event, "[data-refresh-ask]")) {
      Object.assign(refresh, { asking: true, error: null });
      draw();
    } else if (closest(event, "[data-refresh-no]")) {
      Object.assign(refresh, { asking: false, error: null, costMessage: null });
      draw();
    } else if (closest(event, "[data-refresh-yes]")) {
      startRefresh();
    } else if (closest(event, "[data-results-read]")) {
      readResult();
    } else {
      const pager = closest(event, "[data-audience-table] [data-page]");
      const audience = audienceOfPage();
      if (pager && audience && !pager.disabled) {
        audience.page = Number(pager.getAttribute("data-page")) || 1;
        repaintAudienceTable();
      }
    }
  });

  document.addEventListener("submit", (event) => {
    const form = closest(event, "[data-results-form]");
    if (!form || !page.view) return;
    event.preventDefault();
    addResults(form);
  });

  document.addEventListener("input", (event) => {
    const box = closest(event, "[data-audience-search]");
    const audience = audienceOfPage();
    if (!box || !audience) return;
    audience.query = box.value;
    audience.page = 1;
    repaintAudienceTable(); // the table only: the box keeps its focus and caret
  });

  // The results drop zone (M186, DEC-1616 (f)): the file input's label. Choosing a file names it in the zone; a file
  // dropped on the zone is handed to the same input, so "Add results" sends exactly what a chosen file sends.
  document.addEventListener("change", (event) => {
    const input = closest(event, "[data-results-file-input]");
    if (input) showChosenFile(input);
  });
  for (const type of ["dragenter", "dragover"]) {
    document.addEventListener(type, (event) => {
      const zone = closest(event, "[data-results-drop]");
      if (!zone) return;
      event.preventDefault();
      zone.classList.add("is-over");
    });
  }
  document.addEventListener("dragleave", (event) => {
    const zone = closest(event, "[data-results-drop]");
    if (zone && !zone.contains(event.relatedTarget)) zone.classList.remove("is-over");
  });
  document.addEventListener("drop", (event) => {
    const zone = closest(event, "[data-results-drop]");
    if (!zone) return;
    event.preventDefault();
    zone.classList.remove("is-over");
    const input = zone.querySelector("[data-results-file-input]");
    const files = event.dataTransfer && event.dataTransfer.files;
    if (!input || !files || !files.length) return;
    try {
      input.files = files;
    } catch {
      return; // a browser that will not hand dropped files to an input: the file button still works
    }
    showChosenFile(input);
  });
}

/** Name the chosen file in the drop zone (the person's own file name, marked as theirs). */
function showChosenFile(input) {
  const zone = input.closest("[data-results-drop]");
  const line = zone && zone.querySelector("[data-results-chosen]");
  const file = input.files && input.files[0];
  if (!line) return;
  line.hidden = !file;
  line.innerHTML = file ? `Chosen: <span ${USER_TEXT_ATTRIBUTE}>${esc(file.name)}</span>` : "";
}
