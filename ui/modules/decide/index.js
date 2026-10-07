// Plan J's screens (M94; PARALLEL_WORK_PROTOCOL.md §4, the PLAN-J block of `ui/index.html` imports it).
//
// It registers the campaigns list Results draws beside its runs (`registerResultsList`, the router's
// PLAN-J seam) and one screen, a campaign's page (`#/campaigns/<id>`): the measured result - or the
// early look, labelled, with no verdict - and the "Plan the test" card (`plan.js`) with its slider over
// the server's computed points (`GET /campaigns/{id}/plan-preview`, DEC-1204): moving it repaints the
// readout only, and a rate typed into the form asks the server again. The two actions
// on the page, registering the plan and measuring now, are offered only to a person whose role may
// do them, and every event is delegated from `document`, so a repaint needs no re-binding.

import { canAccess, registerModule, registerResultsList, setActiveNav } from "../router.js";
import { errorBox, skeleton } from "../../dom.js";
import { getCampaign, getCampaigns, getPlan, getPlanPreview, postMeasure, postPlan } from "./api.js";
import { planBody, previewHtml, previewReadoutHtml } from "./plan.js";
import { campaignPageHtml, campaignsListHtml, injectStyles } from "./views.js";

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
