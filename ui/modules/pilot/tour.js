// The guided tour for a first-time user (Plan E M63): six steps, dismissible, remembered.
//
// overview → setup → results → output → campaign results → the pilot's reports. With the demo
// seeded, each step opens the demo's own screen (its use case, its training run, its scoring run),
// read from `GET /pilot/demo`; without one, the steps that need a run explain where to find it and
// stay on the current screen rather than open a made-up address. "Skip" and finishing both record
// that the tour was seen (browser storage only - a convenience, never a record).

import { esc } from "../../dom.js";

const SEEN_KEY = "marketing-ai:pilot-tour-seen";

export function tourSteps(demo) {
  const m = demo && demo.manifest;
  const uc = m ? encodeURIComponent(m.use_case_id) : null;
  return [
    {
      title: "The overview",
      text: "Every use case the platform runs for this industry, laid out by customer lifecycle stage. Click a use case to start.",
      hash: "#/",
    },
    {
      title: "Setup",
      text: "Choose the data: upload a prepared file, or build it from raw tables (customers, bills, complaints, usage). Keep the defaults and click Run.",
      hash: m ? `#/uc/${uc}` : null,
    },
    {
      title: "Results",
      text: "Each run is shown as Data → Model → Output: what was checked, which model won and how good it is, and what to do next.",
      hash: m ? `#/uc/${uc}/run/${encodeURIComponent(m.train_run_id)}` : null,
    },
    {
      title: "Output",
      text: "Next month's customers, scored: a band, an action and a reason for every customer, and a control group held back to measure the effect.",
      hash: m ? `#/uc/${uc}/output/${encodeURIComponent(m.score_run_id)}` : null,
    },
    {
      title: "Campaign results",
      text: "After the outcome period, contacted customers are compared with the control group: how many extra customers the campaign kept, with a range.",
      hash: m ? `#/campaign/${uc}/${encodeURIComponent(m.score_run_id)}` : null,
    },
    {
      title: "Pilot reports",
      text: "The data request, the data readiness report, the one-page results report and the value in rupees, ready to share as a page or a PDF.",
      hash: "#/pilot",
    },
  ];
}

function seen() {
  try {
    return window.localStorage.getItem(SEEN_KEY) === "1";
  } catch {
    return false;
  }
}

function markSeen() {
  try {
    window.localStorage.setItem(SEEN_KEY, "1");
  } catch {
    // storage refused: the tour simply offers itself again next time
  }
}

let card = null;

function close() {
  if (card) card.remove();
  card = null;
  markSeen();
}

function show(steps, index) {
  const step = steps[index];
  if (!card) {
    card = document.createElement("section");
    card.id = "pe-tour";
    card.className = "pe-tour";
    card.setAttribute("role", "dialog");
    card.setAttribute("aria-label", "Guided tour");
    document.body.appendChild(card);
  }
  const last = index === steps.length - 1;
  const where = step.hash ? "" : `<p class="pe-note">Open a use case with a finished run to see this screen.</p>`;
  card.innerHTML = `<div class="pe-step">Step ${index + 1} of ${steps.length}</div>
    <h3>${esc(step.title)}</h3><p>${esc(step.text)}</p>${where}
    <div class="pe-row">
      <button type="button" class="pe-btn" data-pe-tour="skip">Skip tour</button>
      ${index > 0 ? `<button type="button" class="pe-btn" data-pe-tour="back">Back</button>` : ""}
      <button type="button" class="pe-btn primary" data-pe-tour="next">${last ? "Finish" : "Next"}</button>
    </div>`;
  card.querySelectorAll("[data-pe-tour]").forEach((control) =>
    control.addEventListener("click", () => {
      const action = control.dataset.peTour;
      if (action === "skip" || (action === "next" && last)) return close();
      show(steps, action === "back" ? index - 1 : index + 1);
    }),
  );
  if (step.hash && window.location.hash !== step.hash) window.location.hash = step.hash;
  card.querySelector("[data-pe-tour='next']").focus();
}

export function startTour(demo) {
  show(tourSteps(demo), 0);
}

/** Offer the tour once, to a first-time visitor of the demo. */
export function maybeStartTour(demo) {
  if (demo && demo.demo_mode && !seen()) startTour(demo);
}
