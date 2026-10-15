// The guided tour of the sample data (Plan E M63; v1: offered, never forced).
//
// Six steps a first-time user needs: Home → a use case's Setup, then a training run's results → who
// to contact, then the campaign's results → the reports (Plan H: the bar is Home, Connections,
// Results and Settings; the steps are named for what they show, not for a menu). Each
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

/**
 * The tour outside the sample data (Plan M M173): the real pages, in the order a new person meets
 * them. Every step is `optional`: when none of its elements is on the page (a role that may not open
 * it, a page that has not drawn) the tour moves on after a moment instead of pointing at nothing.
 */
// The pilot Reports page (`#/pilot`) is frozen for v1 (plan stage 0): neither tour visits it.
export function generalSteps() {
  return [
    {
      title: "Home",
      text: "Your goals, each labelled with its customer stage. The bar at the top takes you anywhere: Home, Connections, Results and Settings.",
      hash: "#/",
      targets: [".uc-list"],
      optional: true,
    },
    {
      title: "Connections",
      text: "Where your customer data and the AI services come from. Connect a source once and every goal can use it.",
      hash: "#/connections",
      targets: ["main.screen .card", "main.screen"],
      optional: true,
    },
    {
      title: "Results",
      text: "Every list you have made and every campaign you have measured, newest first. Open one to see who to contact and what it was worth.",
      hash: "#/results",
      targets: ["[data-module='simple'] .card", "[data-module='simple']"],
      optional: true,
    },
    {
      title: "Settings",
      text: "Things you set up once: privacy, schedules, costs and the advanced tools.",
      hash: "#/settings",
      targets: ["[data-settings-main]", "[data-settings-advanced]"],
      optional: true,
    },
    {
      title: "Help",
      text: "The question mark is always here. Take this tour again, read the glossary, send feedback or see what is new.",
      hash: null,
      // On a phone the bar's tools are hidden until the Menu is open: point at the Menu button then.
      targets: ["#pb-bar .tb-help-btn", "[data-tb-menu]"],
      optional: true,
    },
  ];
}

export function tourSteps(demo) {
  if (!tourAvailable(demo)) return generalSteps();
  const m = demo && demo.manifest;
  const uc = m ? encodeURIComponent(m.use_case_id) : null;
  return [
    {
      title: "Home",
      text: "Every goal, labelled with its customer stage. The bar at the top takes you anywhere: Home, Connections, Results and Settings.",
      hash: "#/",
      targets: ["#pb-bar .topnav", ".uc-list"],
    },
    {
      title: "Set up a model",
      text: "Guided setup checks your file and recommends the settings; you approve, then click Build the model.",
      hash: m ? `#/uc/${uc}` : null,
      targets: ["#f-run", ".setup-grid", ".pick"],
    },
    {
      title: "The model's results",
      text: "Each model reads Data, Model, Output: what was checked, how good the model is, and what to do next.",
      hash: m ? `#/uc/${uc}/run/${encodeURIComponent(m.train_run_id)}` : null,
      targets: [".flow", ".tabs"],
    },
    {
      title: "Who to contact",
      text: "Next month's customers, ranked: a priority, an action and a reason for every customer, and a comparison group held back to measure the effect. Every contact list is listed under Results.",
      hash: m ? `#/uc/${uc}/output/${encodeURIComponent(m.score_run_id)}` : null,
      targets: [".head-actions .btn.primary", ".kpis", "main.screen .card"],
    },
    {
      title: "Campaign results",
      text: "After the outcome period, the customers you contacted are compared with the comparison group: whether the campaign made a measurable difference, with a range.",
      hash: m ? `#/campaign/${uc}/${encodeURIComponent(m.score_run_id)}` : null,
      targets: ["[data-verdict]", ".kpis", "main.screen .card"],
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
let current = { steps: [], index: 0, dir: 1 };
let outlined = null;
let finder = null;
let returnFocus = null;
let returnToHelp = false;
const HELP_BUTTON = "#pb-bar .tb-help-btn";

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
  // A sample-data step falls back to the bar's marked place; an optional one points at its own
  // elements or at nothing, so that `watchTarget` can move on when they are not there.
  const found = (step.optional ? step.targets : [...step.targets, ACTIVE_NAV])
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
  let waited = 0;
  let found = false; // an element of this step was outlined at least once: a repaint later does not skip it
  finder = setInterval(() => {
    outline(step);
    if (outlined) found = true;
    waited += TOUR_TIMING.findEveryMs;
    // An optional step whose elements never appeared (a role that may not open the page, a page that
    // is not there) is skipped, in the direction the person was going.
    if (step.optional && !found && waited >= TOUR_TIMING.skipAfterMs) move(current.dir);
  }, TOUR_TIMING.findEveryMs);
  outline(step);
  found = Boolean(outlined);
}

/** How often a step looks for its elements, and how long it waits for them before an optional one is skipped. */
export const TOUR_TIMING = { findEveryMs: 300, skipAfterMs: 3000 };

/**
 * Go `dir` steps from the current one: past the end finishes. Back from the first step (reached when
 * the step before it was skipped as absent) turns round and goes forward, so it never shows the same
 * absent step again and again.
 */
function move(dir) {
  const { steps, index } = current;
  const next = index + dir;
  if (next >= steps.length || (next < 0 && steps.length < 2)) {
    close();
    return;
  }
  if (next < 0) {
    show(steps, index + 1, 1);
    return;
  }
  show(steps, next, dir);
}

function close() {
  clearInterval(finder);
  finder = null;
  unoutline();
  if (card) card.remove();
  card = null;
  markSeen();
  // The top bar is drawn again on every route the tour visits, so the button it started from is a new
  // element by now: find the Help button again rather than lose the person's place.
  const back = returnFocus && returnFocus.isConnected ? returnFocus : returnToHelp ? document.querySelector(HELP_BUTTON) : null;
  if (back) back.focus();
  returnFocus = null;
  returnToHelp = false;
}

function onKey(event) {
  if (event.key === "Escape" && card && !event.peInternal) close();
}

function show(steps, index, dir = 1) {
  const step = steps[index];
  current = { steps, index, dir };
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
      move(action === "back" ? -1 : 1);
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

/**
 * Start at step 1 (from the Help menu, or the offer). Starting is the person's choice: it navigates.
 * With the sample data seeded it is the sample-data tour; anywhere else (Plan M M173) it walks the
 * real pages, skipping a step whose elements are not there.
 */
export function startTour(demo) {
  closeOffer(false);
  if (!card) {
    returnFocus = document.activeElement;
    returnToHelp = Boolean(returnFocus && returnFocus.matches && returnFocus.matches(HELP_BUTTON));
  }
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
