// Plan J's screens (M94; PARALLEL_WORK_PROTOCOL.md §4, the PLAN-J block of `ui/index.html` imports it).
//
// It registers the campaigns list Results draws beside its runs (`registerResultsList`, the router's
// PLAN-J seam) and one screen, a campaign's page (`#/campaigns/<id>`): the measured result - or the
// early look, labelled, with no verdict - and the "Plan the test" card (`plan.js`) with its slider over
// the server's computed points (`GET /campaigns/{id}/plan-preview`, DEC-1204): moving it repaints the
// readout only, and a rate typed into the form asks the server again. The two actions
// on the page, registering the plan and measuring now, are offered only to a person whose role may
// do them, and every event is delegated from `document`, so a repaint needs no re-binding.

import { canAccess, registerModule, registerPagePanel, registerResultsList, setActiveNav } from "../router.js";
import { errorBox, skeleton } from "../../dom.js";
import { getIndustries, postUpload, treatListUrl } from "../../api.js";
import {
  deleteDriftEvent,
  getArbitrationConflicts,
  getCampaign,
  getCampaigns,
  getDriftEvents,
  getPlan,
  getPlanPreview,
  getTreatListSummary,
  postAudit,
  postDriftEvent,
  postMeasure,
  postPlan,
  postProgramme,
} from "./api.js";
import { driftEventsCardHtml } from "./drift_events.js";
import { AUDIT_ROUTE, auditBody, auditPageHtml, fileOf, programmeBody } from "./audit.js";
import { planBody, previewHtml, previewReadoutHtml } from "./plan.js";
import { campaignPageHtml, campaignsListHtml, conflictsCardHtml, injectStyles, treatListCardHtml } from "./views.js";

export { conflictsCardHtml, treatListCardHtml };
export const ROUTES = ["campaigns"];

const hashParts = () => window.location.hash.replace(/^#\/?/, "").split("/").filter(Boolean);

const screen = {
  id: null,
  view: null,
  plans: null,
  preview: null,
  previewIndex: null,
  busy: false,
  measureError: null,
  planError: null,
};

function draw(app) {
  if (!screen.view || hashParts()[0] !== "campaigns" || decodeURIComponent(hashParts()[1] || "") !== screen.id) return;
  app.innerHTML = campaignPageHtml({
    view: screen.view,
    plans: screen.plans,
    can: canAccess,
    busy: screen.busy,
    measureError: screen.measureError,
    planError: screen.planError,
    preview: screen.preview,
    previewIndex: screen.previewIndex,
  });
  document.title = `${screen.view.campaign.name} · Marketing AI`;
  setActiveNav("results"); // a campaign is one of the Results
}

async function load(id) {
  const [view, plans, preview] = await Promise.all([
    getCampaign(id),
    getPlan(id).catch(() => null),
    getPlanPreview(id).catch(() => null), // a preview that fails leaves the card as it was
  ]);
  screen.view = view;
  screen.plans = plans;
  setPreview(preview);
}

function setPreview(preview) {
  screen.preview = preview;
  screen.previewIndex = preview && preview.current_index !== null && preview.current_index !== undefined ? preview.current_index : 0;
}

async function renderCampaign(app, parts) {
  injectStyles();
  const id = decodeURIComponent(parts[1] || "");
  if (!id) {
    window.location.hash = "#/results"; // the list of campaigns is on Results
    return;
  }
  if (screen.id !== id) {
    Object.assign(screen, {
      id,
      view: null,
      plans: null,
      preview: null,
      previewIndex: null,
      busy: false,
      measureError: null,
      planError: null,
    });
  }
  if (!screen.view) app.innerHTML = skeleton("page", { title: "Campaign" });
  try {
    await load(id);
  } catch (error) {
    app.innerHTML = `<main class="screen dc" data-module="decide">${errorBox(error, { retry: true })}</main>`;
    return;
  }
  draw(app);
}

registerModule({ name: "decide", routes: ROUTES, render: renderCampaign });

registerResultsList({ name: "campaigns", load: getCampaigns, html: campaignsListHtml });
registerResultsList({ name: "arbitration_conflicts", load: getArbitrationConflicts, html: conflictsCardHtml });

// --- the page's two actions, delegated -----------------------------------------------------------

document.addEventListener("submit", async (event) => {
  const form = event.target && event.target.closest ? event.target.closest("[data-plan-form]") : null;
  if (!form || !screen.id) return;
  event.preventDefault();
  const app = document.getElementById("app");
  try {
    await postPlan(screen.id, planBody(form));
    screen.planError = null;
    await load(screen.id);
  } catch (error) {
    screen.planError = error;
  }
  draw(app);
});

document.addEventListener("click", async (event) => {
  const button = event.target && event.target.closest ? event.target.closest("[data-measure]") : null;
  if (!button || !screen.id || screen.busy) return;
  const app = document.getElementById("app");
  screen.busy = true;
  draw(app);
  try {
    await postMeasure(screen.id);
    screen.measureError = null;
    await load(screen.id);
  } catch (error) {
    // `CAMPAIGN_NOT_MATURED` says the day to come back, and carries no number to show.
    screen.measureError = error;
  }
  screen.busy = false;
  draw(app);
});

// --- the preview slider: computed points only (DEC-1204) -------------------------------------------

document.addEventListener("input", (event) => {
  const slider = event.target && event.target.closest ? event.target.closest("[data-preview-slider]") : null;
  if (!slider || !screen.preview) return;
  screen.previewIndex = Number(slider.value);
  const readout = document.querySelector("[data-preview-readout]");
  if (readout) readout.innerHTML = previewReadoutHtml(screen.preview, screen.previewIndex); // the readout only
});

document.addEventListener("change", async (event) => {
  const field = event.target && event.target.closest ? event.target.closest("[data-plan-form] input[name=base_rate_pct]") : null;
  if (!field || !screen.id) return;
  const id = screen.id;
  const rate = field.value === "" ? null : Number(field.value) / 100;
  let preview;
  try {
    preview = await getPlanPreview(id, rate !== null && rate >= 0 && rate <= 1 ? rate : null);
  } catch {
    return; // the card keeps the points it has; registering the plan is unaffected
  }
  if (screen.id !== id) return;
  setPreview(preview);
  const section = document.querySelector("[data-plan-preview]");
  if (section) section.outerHTML = previewHtml(screen.preview, screen.previewIndex);
});

// --- Treat list page panel (Plan J M98, DEC-1308) ------------------------------------------------
const treatStates = new Map();

async function loadTreatSummary(runId) {
  let st = treatStates.get(runId);
  if (!st) {
    st = { runId, summary: null, loading: true, error: null };
    treatStates.set(runId, st);
    try {
      st.summary = await getTreatListSummary(runId);
    } catch (err) {
      st.error = err;
    }
    st.loading = false;
    repaintTreatPanel(runId);
  }
  return st;
}

function repaintTreatPanel(runId) {
  if (typeof document === "undefined") return;
  const el = document.querySelector(`[data-treat-panel="${encodeURIComponent(runId)}"]`);
  const st = treatStates.get(runId);
  if (!el || !st || st.loading) return;
  el.innerHTML = treatListCardHtml(st.summary, { treatListHref: treatListUrl(runId), error: st.error });
}

function treatListPanelHtml(kind, uc, run) {
  if (!run || !run.run_id) return "";
  const runId = run.run_id;
  injectStyles();
  loadTreatSummary(runId);
  const st = treatStates.get(runId);
  const inner =
    st && !st.loading
      ? treatListCardHtml(st.summary, { treatListHref: treatListUrl(runId), error: st.error })
      : "";
  // A failure is shown once; the next visit to the page asks the server again.
  if (st && st.error) treatStates.delete(runId);
  return `<div data-treat-panel="${encodeURIComponent(runId)}">${inner}</div>`;
}

registerPagePanel({
  name: "treat_list",
  applies: (kind, uc, run) => kind === "output" && !!run && run.mode === "score",
  html: treatListPanelHtml,
});

// --- Events behind the change: a scoring run's Output page (Plan J M109, DEC-1319) ----------------
// The server reads the notes beside the run's drift report (`GET /runs/{id}/drift-events`); the card draws
// that answer and offers the add and remove actions only to a person whose role may do them.
const driftStates = new Map();

const driftCanEdit = (runId) => canAccess("POST", `/runs/${encodeURIComponent(runId)}/drift-events`);

function driftCard(runId) {
  const st = driftStates.get(runId);
  if (!st || st.loading) return "";
  return driftEventsCardHtml(st.view ? { ...st.view, can_edit: driftCanEdit(runId) } : null, { error: st.error });
}

function repaintDriftPanel(runId) {
  if (typeof document === "undefined") return;
  const el = document.querySelector(`[data-drift-panel="${encodeURIComponent(runId)}"]`);
  if (el) el.innerHTML = driftCard(runId);
}

async function loadDriftEvents(runId) {
  if (driftStates.has(runId)) return;
  const st = { view: null, loading: true, error: null };
  driftStates.set(runId, st);
  try {
    st.view = await getDriftEvents(runId);
  } catch {
    st.view = null; // no card is better than a card that says nothing; the notice above still stands
  }
  st.loading = false;
  repaintDriftPanel(runId);
}

function driftPanelHtml(kind, uc, run) {
  if (!run || !run.run_id) return "";
  injectStyles();
  loadDriftEvents(run.run_id);
  return `<div data-drift-panel="${encodeURIComponent(run.run_id)}">${driftCard(run.run_id)}</div>`;
}

registerPagePanel({
  name: "drift_events",
  applies: (kind, uc, run) => kind === "output" && !!run && run.mode === "score" && run.problem_type !== "uplift",
  html: driftPanelHtml,
});

async function driftAct(panel, call) {
  const runId = decodeURIComponent(panel.getAttribute("data-drift-panel"));
  const st = driftStates.get(runId);
  if (!st) return;
  try {
    st.view = await call(runId);
    st.error = null;
  } catch (error) {
    st.error = error;
  }
  repaintDriftPanel(runId);
}

document.addEventListener("submit", (event) => {
  const form = event.target && event.target.closest ? event.target.closest("[data-drift-event-form]") : null;
  const panel = form && form.closest("[data-drift-panel]");
  if (!panel) return;
  event.preventDefault();
  const data = new FormData(form);
  driftAct(panel, (runId) =>
    postDriftEvent(runId, {
      event_date: data.get("event_date"),
      kind: data.get("kind"),
      note: data.get("note"),
      measures: data.getAll("measures"),
    }),
  );
});

document.addEventListener("click", (event) => {
  const button = event.target && event.target.closest ? event.target.closest("[data-drift-event-remove]") : null;
  const panel = button && button.closest("[data-drift-panel]");
  if (!panel) return;
  driftAct(panel, (runId) => deleteDriftEvent(runId, button.getAttribute("data-drift-event-remove")));
});

// --- Audit a campaign, and the programme readout (Plan J M103, DEC-1313) ------------------------------
// One page, `#/audit`: the form for a campaign another tool ran and the one for the whole programme. A file
// goes through the ordinary upload (`POST /uploads`), the form then offers that file's own columns, and the
// answer is the campaign's page, where the server's label says what its numbers can claim.

const audit = {
  files: { assignment: null, outcomes: null, contact: null, programme: null },
  busy: false,
  error: null,
  programmeBusy: false,
  programmeError: null,
  useCase: null,
};

function drawAudit(app) {
  if (hashParts()[0] !== AUDIT_ROUTE) return;
  // The values typed so far survive a repaint (choosing a file draws the page again).
  const kept = {};
  for (const form of document.querySelectorAll("[data-audit-form], [data-programme-form]")) {
    for (const [key, value] of new FormData(form).entries()) {
      if (typeof value === "string") kept[key] = value;
    }
  }
  app.innerHTML = auditPageHtml({ can: canAccess, state: audit });
  for (const [key, value] of Object.entries(kept)) {
    const input = app.querySelector(`[name="${key}"]`);
    if (!input) continue;
    if (input.type === "radio") {
      const chosen = app.querySelector(`[name="${key}"][value="${value}"]`);
      if (chosen) chosen.checked = true;
    } else {
      input.value = value;
    }
  }
  document.title = "Audit a campaign · Marketing AI";
  setActiveNav("results");
}

registerModule({
  name: "audit",
  routes: [AUDIT_ROUTE],
  render: async (app) => {
    injectStyles();
    drawAudit(app);
  },
});

/** The use case an uploaded file is filed under: the first one open today (the file is not about it). */
async function uploadUseCase() {
  if (audit.useCase) return audit.useCase;
  const payload = await getIndustries();
  for (const industry of (payload && payload.industries) || []) {
    for (const stage of industry.stages || []) {
      const found = (stage.use_cases || []).find((u) => !u.status || u.status === "available");
      if (found) {
        audit.useCase = found.id;
        return found.id;
      }
    }
  }
  throw new Error("No use case is set up yet, so a file cannot be uploaded.");
}

document.addEventListener("change", async (event) => {
  const input = event.target && event.target.closest ? event.target.closest("[data-audit-file]") : null;
  if (!input || !input.files || !input.files[0]) return;
  const kind = input.dataset.auditFile;
  const failed = kind === "programme" ? "programmeError" : "error";
  const app = document.getElementById("app");
  try {
    audit.files[kind] = fileOf(await postUpload(input.files[0], await uploadUseCase(), "score"));
    audit[failed] = null;
  } catch (error) {
    audit[failed] = error;
  }
  drawAudit(app);
});

document.addEventListener("submit", async (event) => {
  const auditForm = event.target && event.target.closest ? event.target.closest("[data-audit-form]") : null;
  const programmeForm = event.target && event.target.closest ? event.target.closest("[data-programme-form]") : null;
  const form = auditForm || programmeForm;
  if (!form) return;
  event.preventDefault();
  const app = document.getElementById("app");
  const values = Object.fromEntries(new FormData(form).entries());
  const busy = auditForm ? "busy" : "programmeBusy";
  const failed = auditForm ? "error" : "programmeError";
  audit[busy] = true;
  audit[failed] = null;
  drawAudit(app);
  try {
    const view = auditForm
      ? await postAudit(auditBody(values, audit.files))
      : await postProgramme(programmeBody(values, audit.files));
    audit.files = { assignment: null, outcomes: null, contact: null, programme: null };
    audit[busy] = false;
    window.location.hash = `#/campaigns/${encodeURIComponent(view.campaign.campaign_id)}`;
    return;
  } catch (error) {
    // `CAMPAIGN_NOT_MATURED` says the day to come back; the files stay chosen so the person can try again.
    audit[failed] = error;
  }
  audit[busy] = false;
  drawAudit(app);
});
