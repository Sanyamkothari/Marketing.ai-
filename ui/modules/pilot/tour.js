// The guided tour of the sample data (Plan E M63; v1: offered, never forced).
//
// Six steps, one per place in the top bar a first-time user needs: Home → Models (Setup, then a
// training run's results) → Campaigns (who to contact, then the campaign's results) → Reports. Each
// step opens the demo's own screen (its use case, its training run, its scoring run), read from
// `GET /pilot/demo`, outlines the part of the screen it talks about and puts its card away from it
// (a bottom sheet on a phone). The tour exists only when demo mode is on *and* the demo is seeded.
//
// It never takes a shared link away from the person who opened it: it starts by itself only on
// Home (`#/`); on any other screen a first visit gets a one-line offer instead. "Skip", Escape and
// finishing all record that the tour was seen - in browser storage only, a convenience, never a record.
// Every storage access is guarded: a browser that refuses storage (or a sandboxed frame) just sees the
// offer again next time.

import { esc } from "../../dom.js";

const SEEN_KEY = "marketing-ai:pilot-tour-seen";

/** Where the top bar's active item is drawn: the outline's last resort for any step. */
const ACTIVE_NAV = "#pb-bar .topnav .tn-item.on";

export function tourSteps(demo) {
  const m = demo && demo.manifest;
  const uc = m ? encodeURIComponent(m.use_case_id) : null;
  return [
    {
      title: "Home",
      text: "Every use case, laid out by customer lifecycle stage. The bar at the top takes you anywhere: Home, Build data, Models, Campaigns, Reports and Admin.",
      hash: "#/",
      targets: ["#pb-bar .topnav", ".timeline-wrap", ".uc-list"],
    },
    {
      title: "Models: set up a model",
      text: "Choose the data: upload a prepared file, or build it from raw tables (Build data has the request to send your client). Keep the recommended settings and click Run training.",
      hash: m ? `#/uc/${uc}` : null,
      targets: ["#f-run", ".setup-grid", ".pick"],
    },
    {
      title: "Models: training results",
      text: "Each run reads Data, Model, Output: what was checked, which model won and how good it is, and what to do next.",
      hash: m ? `#/uc/${uc}/run/${encodeURIComponent(m.train_run_id)}` : null,
      targets: [".flow", ".tabs"],
    },
    {
      title: "Campaigns: who to contact",
      text: "Next month's customers, scored: a band, an action and a reason for every customer, and a control group held back to measure the effect. Every scoring run is listed under Campaigns.",
      hash: m ? `#/uc/${uc}/output/${encodeURIComponent(m.score_run_id)}` : null,
      targets: [".head-actions .btn.primary", ".kpis", "main.screen .card"],
    },
    {
      title: "Campaigns: results",
      text: "After the outcome period, the customers you contacted are compared with the control group: whether the campaign made a measurable difference, with a range.",
      hash: m ? `#/campaign/${uc}/${encodeURIComponent(m.score_run_id)}` : null,
      targets: ["[data-verdict]", ".kpis", "main.screen .card"],
    },
    {
      title: "Reports",
      text: "Model results and each campaign's value in rupees, ready to share as a page or a PDF. Data readiness reports are under Build data.",
      hash: "#/pilot",
      targets: ["[data-pe-reports]"],
    },
  ];
}

// --- remembered, when the browser allows it ------------------------------------------------------

function storage() {
  try {
    return window.localStorage || null;
  } catch {
    return null; // a sandboxed document, or storage switched off
  }
}

function seen() {
  try {
    const store = storage();
    return Boolean(store) && store.getItem(SEEN_KEY) === "1";
  } catch {
    return false;
  }
}

function markSeen() {
  try {
    const store = storage();
    if (store) store.setItem(SEEN_KEY, "1");
  } catch {
    // storage refused: the tour simply offers itself again next time
  }
}

// --- the card ------------------------------------------------------------------------------------------

let card = null;
let offer = null;
let current = { steps: [], index: 0 };
let outlined = null;
let finder = null;
let returnFocus = null;

const narrow = () => typeof window.matchMedia === "function" && window.matchMedia("(max-width: 700px)").matches;

function visible(el) {
  if (!el || !el.isConnected || el.closest("[data-skeleton]")) return false;
  const box = el.getBoundingClientRect();
  return box.width > 0 && box.height > 0;
}

function unoutline() {
  if (outlined) outlined.classList.remove("pe-target");
  outlined = null;
}

/** Outline the step's part of the screen, bring it into view and keep the card out of its way. */
function outline(step) {
  if (outlined && outlined.isConnected) return;
  unoutline();
  const found = [...step.targets, ACTIVE_NAV]
    .map((selector) => document.querySelector(selector))
    .find(visible);
  if (!found || !card) return;
  outlined = found;
  found.classList.add("pe-target");
  const box = found.getBoundingClientRect();
  // On a phone the card is a bottom sheet: bring the target up under the bar, above the sheet.
  if (box.top < 64 || (narrow() ? box.top > 88 : box.bottom > window.innerHeight)) {
    found.scrollIntoView({ block: narrow() ? "start" : "center" });
    if (narrow()) window.scrollBy(0, -72); // clear the sticky top bar
  }
  const after = found.getBoundingClientRect();
  const centre = Math.min(Math.max(after.top, 0), window.innerHeight) + Math.min(after.height, window.innerHeight) / 2;
  card.classList.toggle("at-top", !narrow() && centre > window.innerHeight / 2);
}

/** Screens paint after their data arrives, and repaint: keep looking while the step is shown. */
function watchTarget(step) {
  clearInterval(finder);
  finder = setInterval(() => outline(step), 300);
  outline(step);
}

function close() {
  clearInterval(finder);
  finder = null;
  unoutline();
  if (card) card.remove();
  card = null;
  markSeen();
  if (returnFocus && returnFocus.isConnected) returnFocus.focus();
  returnFocus = null;
}

function onKey(event) {
  if (event.key === "Escape" && card && !event.peInternal) close();
}

function show(steps, index) {
  const step = steps[index];
  current = { steps, index };
  if (!card) {
    card = document.createElement("section");
    card.id = "pe-tour";
    card.className = "pe-tour dialog";
    card.setAttribute("role", "dialog");
    card.setAttribute("aria-labelledby", "pe-tour-title");
    document.body.appendChild(card);
    card.addEventListener("click", (event) => {
      const control = event.target.closest && event.target.closest("[data-pe-tour]");
      if (!control) return;
      const action = control.dataset.peTour;
      const at = current.index;
      const last = at === current.steps.length - 1;
      if (action === "skip" || (action === "next" && last)) {
        close();
        return;
      }
      show(current.steps, action === "back" ? at - 1 : at + 1);
    });
  }
  const last = index === steps.length - 1;
  const dots = steps.map((_, i) => `<li${i === index ? ` class="on"` : ""}></li>`).join("");
  card.innerHTML = `<div class="pe-step">Step ${index + 1} of ${steps.length}</div>
    <h2 id="pe-tour-title">${esc(step.title)}</h2><p>${esc(step.text)}</p>
    <div class="pe-row">
      <ol class="pe-dots" aria-hidden="true">${dots}</ol>
      <div class="btn-row">
        <button type="button" class="btn quiet sm" data-pe-tour="skip">Skip tour</button>
        ${index > 0 ? `<button type="button" class="btn secondary sm" data-pe-tour="back">Back</button>` : ""}
        <button type="button" class="btn primary sm" data-pe-tour="next">${last ? "Finish" : "Next"}</button>
      </div>
    </div>`;
  unoutline();
  card.classList.remove("at-top");
  if (step.hash && window.location.hash !== step.hash) window.location.hash = step.hash;
  watchTarget(step);
  card.querySelector("[data-pe-tour='next']").focus();
}

function closeOffer(remember) {
  if (offer) offer.remove();
  offer = null;
  if (remember) markSeen();
}

/** Start at step 1 (from the Help menu, or the offer). Starting is the person's choice: it navigates. */
export function startTour(demo) {
  if (!tourAvailable(demo)) return;
  closeOffer(false);
  if (!card) returnFocus = document.activeElement;
  show(tourSteps(demo), 0);
}

/** The tour is only for the sample data: demo mode on and the demo seeded. */
export function tourAvailable(demo) {
  return Boolean(demo && demo.demo_mode && demo.seeded && demo.manifest);
}

function onHome() {
  const hash = window.location.hash || "#/";
  return hash === "#/" || hash === "#";
}

/**
 * A first visit to the demo: on Home the tour starts by itself; anywhere else - a shared link, the
 * sign-in screen excepted - a one-line offer appears instead, and the screen stays where it is.
 */
export function maybeStartTour(demo) {
  if (!tourAvailable(demo) || seen() || card || offer) return;
  if (onHome()) {
    startTour(demo);
    return;
  }
  if (/^#\/?(signin|account)\b/.test(window.location.hash)) return;
  offer = document.createElement("section");
  offer.id = "pe-tour-offer";
  offer.className = "pe-offer dialog";
  offer.setAttribute("role", "region");
  offer.setAttribute("aria-label", "Guided tour");
  offer.innerHTML = `<p>New here? Take a 2-minute tour of the sample data.</p>
    <div class="btn-row"><button type="button" class="btn secondary sm" data-pe-offer="start">Start the tour</button>
    <button type="button" class="btn quiet sm" data-pe-offer="no">No thanks</button></div>`;
  offer.addEventListener("click", (event) => {
    const control = event.target.closest && event.target.closest("[data-pe-offer]");
    if (!control) return;
    if (control.dataset.peOffer === "start") startTour(demo);
    else closeOffer(true);
  });
  document.body.appendChild(offer);
}

if (typeof document !== "undefined") document.addEventListener("keydown", onKey);
