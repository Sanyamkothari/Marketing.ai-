// Plan E's entry point (M59-M64), loaded by its own `<script type="module">` in index.html's PLAN-E
// block, after `app.js`, the same way the uplift module is.
//
// It adds, without editing any other workstream's screen:
//   * Reports (`#/pilot`), Build data (`#/pilot/kit`) and the report viewers (registered with
//     `modules/router.js`);
//   * in the top bar (through its slots): the "Sample data: Demo Telecom" chip in demo mode, and the
//     Help menu - "Take the tour" when the demo is seeded, and "Send feedback";
//   * the plain-language catalogue as the router's glossary, so every screen's error leads with the
//     catalogue's title for its code;
//   * "What does this mean?" beside every warning code and every advanced setting;
//   * the guided tour, offered once to a first-time visitor of the demo.
//
// `GET /pilot/demo` and `GET /pilot/help` are read once at start (and again on the next route
// change if the first try failed, for instance before signing in). If either fails the rest still
// works: no chip or tour, or no help buttons, rather than a broken page.

import { esc } from "../../dom.js";
import {
  announceModulesChanged,
  refreshTopBar,
  registerGlossary,
  registerModule,
  registerNavSlot,
} from "../router.js";
import { getDemo, getHelp } from "./api.js";
import { feedbackMenuItem, mountFeedback } from "./feedback.js";
import { installHelp } from "./help.js";
import { renderPilot } from "./screen.js";
import { injectPilotStyles } from "./styles.js";
import { maybeStartTour, startTour, tourAvailable } from "./tour.js";

let demo = null;
let demoLoading = false;
let helpLoaded = false;
let helpLoading = false;

function demoChip() {
  if (!demo || !demo.demo_mode) return "";
  const label = demo.seeded ? `Sample data: ${demo.manifest.client_name}` : "Sample data: not loaded";
  return `<span class="chip neutral pe-demo" title="Everything shown belongs to a made-up client">${esc(label)}</span>`;
}

function helpEntries() {
  const tour = tourAvailable(demo) ? `<button type="button" data-pe-tour-start>Take the tour</button>` : "";
  return `${tour}${feedbackMenuItem()}`;
}

function loadDemo() {
  if (demoLoading) return;
  demoLoading = true;
  getDemo()
    .then((payload) => {
      demo = payload;
      refreshTopBar();
      // Reports and Build data show the sample data's own rows: draw them again now it is known.
      if (/^#\/pilot(\/|$)/.test(window.location.hash)) announceModulesChanged();
      if (demo && demo.demo_mode && demo.seeded) maybeStartTour(demo);
    })
    .catch(() => {
      demo = null;
    })
    .finally(() => {
      demoLoading = false;
    });
}

function loadHelp() {
  if (helpLoading || helpLoaded) return;
  helpLoading = true;
  getHelp()
    .then((catalogue) => {
      helpLoaded = true;
      registerGlossary(catalogue);
      installHelp(catalogue);
    })
    .catch(() => {
      // no catalogue, no help buttons: every screen works as it did
    })
    .finally(() => {
      helpLoading = false;
    });
}

registerModule({
  name: "pilot",
  routes: ["pilot"],
  render: (app, parts) => renderPilot(app, parts, demo),
});

registerNavSlot("demo", { html: demoChip });
registerNavSlot("help", { html: helpEntries });

injectPilotStyles();
mountFeedback();

document.addEventListener("click", (event) => {
  const start = event.target && event.target.closest && event.target.closest("[data-pe-tour-start]");
  if (!start) return;
  event.preventDefault();
  // Close the Help menu the way the top bar does (Escape), so focus returns to "Help" after the tour.
  const escape = new KeyboardEvent("keydown", { key: "Escape", bubbles: true });
  escape.peInternal = true;
  document.dispatchEvent(escape);
  startTour(demo);
});

window.addEventListener("hashchange", () => {
  if (!demo) loadDemo();
  if (!helpLoaded) loadHelp();
});

loadDemo();
loadHelp();
