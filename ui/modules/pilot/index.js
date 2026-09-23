// Plan E's entry point (M59-M64), loaded by its own `<script type="module">` in index.html's PLAN-E
// block, after `app.js`, the same way the uplift module is.
//
// It adds, without editing any other workstream's screen:
//   * the Pilot screen, `#/pilot/...` (registered with `modules/router.js`);
//   * a thin bar above every screen: a "Demo Telecom" badge when demo mode is on, a link to the Pilot
//     screen and "Take the tour";
//   * the feedback button on every screen;
//   * "What does this mean?" beside every warning code and every advanced setting;
//   * the guided tour, offered once to a first-time visitor of the demo.
//
// `GET /pilot/demo` and `GET /pilot/help` are read once at start. If either fails the rest still
// works: no demo badge, or no help buttons, rather than a broken page.

import { esc } from "../../dom.js";
import { registerModule } from "../router.js";
import { getDemo, getHelp } from "./api.js";
import { mountFeedback } from "./feedback.js";
import { installHelp } from "./help.js";
import { renderPilot } from "./screen.js";
import { injectPilotStyles } from "./styles.js";
import { maybeStartTour, startTour } from "./tour.js";

let demo = null;

function mountBar() {
  if (document.getElementById("pe-bar")) return;
  const app = document.getElementById("app");
  if (!app || !app.parentNode) return;
  const bar = document.createElement("div");
  bar.id = "pe-bar";
  bar.className = "pe-bar";
  app.parentNode.insertBefore(bar, app);
  paintBar();
}

function paintBar() {
  const bar = document.getElementById("pe-bar");
  if (!bar) return;
  const badge =
    demo && demo.demo_mode
      ? `<span class="pe-demo">${esc(demo.seeded ? `Demo: ${demo.manifest.client_name}, synthetic data` : "Demo mode (not seeded)")}</span>`
      : "";
  bar.innerHTML = `<div class="pe-bar-in">${badge}<a href="#/pilot">Pilot</a><button type="button" data-pe-tour-start>Take the tour</button></div>`;
  bar.querySelector("[data-pe-tour-start]").addEventListener("click", () => startTour(demo));
}

registerModule({
  name: "pilot",
  routes: ["pilot"],
  render: (app, parts) => renderPilot(app, parts, demo),
});

injectPilotStyles();
mountBar();
mountFeedback();

getDemo()
  .then((payload) => {
    demo = payload;
    paintBar();
    maybeStartTour(demo);
  })
  .catch(() => {
    demo = null;
  });

getHelp()
  .then((catalogue) => installHelp(catalogue))
  .catch(() => {
    // no catalogue, no help buttons: every screen works as it did
  });
