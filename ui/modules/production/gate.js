// Role-aware actions (Phase 4b M46, DEC-792): every control that starts something a role may not do
// is disabled and explained in place - "Only an Approver can approve a champion." - using the
// permission list `GET /auth/me` answers, so the screen and the server read one table.
//
// Why one table here, and not an `if (can(...))` in each screen. The controls live in other
// workstreams' files - the Setup form in `ui/usecase.js` (Phase 1), the assistant, root-cause and
// copy screens and the AWS connection screen in `ui/modules/generative/` (Phase 3a) - which this
// branch may not edit (PARALLEL_WORK_PROTOCOL.md §3). Every one of them already gives its controls a
// stable `id` or data attribute, so `ACTION_CONTROLS` maps a selector to the API route that control
// calls, and `applyGates` runs after every paint of `#app` (a `MutationObserver`; every screen paints
// by replacing `innerHTML`). A screen that adds a control later needs a row here, not a new code path;
// `tests/integration/production/test_production_ui.py` fails when a row names a route with no access
// policy, or an `#id` no screen draws any more.
//
// Keyed by `(method, path template)`, not by audit action name: that pair is what the screen's code
// actually calls and what `api/access_policy.py` is keyed by, so the test can check it against the
// real policy table rather than against a second vocabulary.
//
// Disabled and explained, not hidden: a control that vanishes teaches a Viewer nothing about why the
// screen looks emptier than a colleague's, and the plan asks for the reason in place (M46). The
// sentence is the server's own `reason` (`refusal_message`), never one written here.
//
// Advisory, never the enforcement. The API refuses every one of these calls with `403 ROLE_REQUIRED`
// whatever this file does (DEC-717); this is only so a person is not invited to do something that
// will be refused. When `/auth/me` is unknown, or does not list a route, nothing is gated. A capture
// phase listener also swallows a click or submit on a gated control, because a screen that toggles
// `disabled` itself (after a file is chosen, say) would otherwise re-enable it between paints.

import { esc } from "../../dom.js";
import { currentMe, onSession, reasonFor } from "./session.js";

/**
 * Selector → the route that control calls. `explain: false` gates a control without writing the
 * sentence beside it, for the secondary inputs of a form whose button already carries it.
 */
export const ACTION_CONTROLS = [
  // --- Phase 1: the use-case Setup screen (ui/usecase.js) - upload, build/train/score, cancel -----
  { selector: "#f-file", method: "POST", path: "/uploads" },
  { selector: "#f-run", method: "POST", path: "/runs" },
  { selector: "#f-cancel", method: "POST", path: "/runs/{run_id}/cancel" },
  // --- the champion: no screen draws these yet; one that does marks its button with data-action ---
  { selector: '[data-action="models.approve"]', method: "POST", path: "/models/{model_id}/approve" },
  { selector: '[data-action="models.promote"]', method: "POST", path: "/models/{model_id}/promote" },
  // --- Phase 3a: build and evaluate an assistant (generative/assistant.js) -----------------------
  { selector: "#g-docs", method: "POST", path: "/use-cases/{use_case_id}/indexes" },
  { selector: "#g-sample-docs", method: "POST", path: "/use-cases/{use_case_id}/indexes", explain: false },
  { selector: "#g-sample-questions", method: "POST", path: "/use-cases/{use_case_id}/indexes", explain: false },
  { selector: "#g-refset", method: "POST", path: "/use-cases/{use_case_id}/reference-sets" },
  { selector: "#g-submit", method: "POST", path: "/use-cases/{use_case_id}/indexes" },
  { selector: "#g-eval-submit", method: "POST", path: "/indexes/{index_id}/evaluate" },
  { selector: "#g-question", method: "POST", path: "/indexes/{index_id}/ask", explain: false },
  { selector: "#g-ask button", method: "POST", path: "/indexes/{index_id}/ask" },
  // --- Phase 3a: root causes and campaign copy (generative/rca.js, copy.js) -----------------------
  { selector: "#g-generate-rca", method: "POST", path: "/runs/{run_id}/root-cause" },
  { selector: "#g-generate-copy", method: "POST", path: "/runs/{run_id}/campaign-copy" },
  {
    selector: "#g-approver",
    method: "POST",
    path: "/runs/{run_id}/campaign-copy/templates/{template_id}/approve",
    explain: false,
  },
  { selector: "[data-approve]", method: "POST", path: "/runs/{run_id}/campaign-copy/templates/{template_id}/approve" },
  { selector: "[data-regen]", method: "POST", path: "/runs/{run_id}/campaign-copy/templates/{template_id}/regenerate" },
  // --- settings: the AWS connection (generative/connection.js) ------------------------------------
  { selector: 'input[name="c-source"]', method: "PUT", path: "/connection/aws", explain: false },
  { selector: "#c-profile", method: "PUT", path: "/connection/aws", explain: false },
  { selector: "#c-save", method: "PUT", path: "/connection/aws" },
  { selector: "#c-reset", method: "DELETE", path: "/connection/aws" },
  { selector: "#c-test", method: "POST", path: "/connection/aws/test" },
];

const GATE = "pbGate"; // dataset key: the reason this control is gated
const WAS = "pbWasDisabled"; // dataset key: its own `disabled` before we touched it

/** Where the sentence goes: a file input hides inside its styled `<label>`, so beside the label. */
function anchorOf(el) {
  return el.type === "file" ? el.closest("label") || el : el;
}

function explainBeside(anchor, reason) {
  const parent = anchor.parentElement;
  if (!parent) return;
  const already = [...parent.children].some((c) => c.classList.contains("pb-why") && c.textContent === reason);
  if (!already) anchor.insertAdjacentHTML("afterend", `<span class="pb-why" role="note">${esc(reason)}</span>`);
}

/** Disable one control and say why. Idempotent: gating twice changes nothing the second time. */
export function gateControl(el, reason, explain = true) {
  if (el.dataset[GATE] !== reason) {
    if (!(WAS in el.dataset)) el.dataset[WAS] = el.disabled ? "1" : "0";
    el.dataset[GATE] = reason;
    el.title = reason;
    el.classList.add("pb-off");
    if (el.tagName === "A") {
      el.setAttribute("aria-disabled", "true");
      el.setAttribute("tabindex", "-1");
    }
  }
  if ("disabled" in el && el.tagName !== "A" && !el.disabled) el.disabled = true;
  const anchor = anchorOf(el);
  if (anchor !== el) anchor.classList.add("pb-off");
  if (explain) explainBeside(anchor, reason);
}

/** Undo `gateControl` - after a sign-in as someone who may, without waiting for a repaint. */
export function ungateControl(el) {
  if (!(GATE in el.dataset)) return;
  const reason = el.dataset[GATE];
  delete el.dataset[GATE];
  if ("disabled" in el && el.tagName !== "A") el.disabled = el.dataset[WAS] === "1";
  delete el.dataset[WAS];
  if (el.title === reason) el.removeAttribute("title");
  el.classList.remove("pb-off");
  if (el.tagName === "A") {
    el.removeAttribute("aria-disabled");
    el.removeAttribute("tabindex");
  }
  const anchor = anchorOf(el);
  if (anchor !== el) anchor.classList.remove("pb-off");
  const parent = anchor.parentElement;
  if (parent) {
    for (const note of [...parent.children]) {
      if (note.classList.contains("pb-why") && note.textContent === reason) note.remove();
    }
  }
}

/** Gate every mapped control under `root` the `source` permissions refuse; ungate the rest. */
export function applyGates(root, source = currentMe()) {
  let gated = 0;
  for (const control of ACTION_CONTROLS) {
    const reason = source ? reasonFor(control.method, control.path, source) : null;
    for (const el of root.querySelectorAll(control.selector)) {
      if (reason) {
        gateControl(el, reason, control.explain !== false);
        gated += 1;
      } else {
        ungateControl(el);
      }
    }
  }
  return gated;
}

function blockGated(event) {
  const target = event.target;
  if (!target || !target.closest) return;
  let hit = target.closest("[data-pb-gate]");
  if (!hit && event.type === "submit" && target.querySelector) {
    hit = target.querySelector('[data-pb-gate][type="submit"], button[data-pb-gate]:not([type])');
  }
  if (hit) {
    event.preventDefault();
    event.stopImmediatePropagation();
  }
}

const INSTALLED = Symbol.for("marketing-ai.production.gates");

/**
 * Re-apply after every paint of `root` and after every change of who is signed in; swallow events
 * on gated controls. Mutations this function makes itself are not observed (it disconnects first).
 */
export function installGates(doc = document, root = doc.getElementById("app")) {
  if (!root || doc[INSTALLED]) return null;
  doc[INSTALLED] = true;
  for (const type of ["click", "submit", "change"]) doc.addEventListener(type, blockGated, true);
  const View = doc.defaultView || window;
  const observer = new View.MutationObserver(() => {
    observer.disconnect();
    try {
      applyGates(root);
    } finally {
      observer.observe(root, { childList: true, subtree: true, attributes: true, attributeFilter: ["disabled"] });
    }
  });
  observer.observe(root, { childList: true, subtree: true, attributes: true, attributeFilter: ["disabled"] });
  onSession(() => {
    observer.disconnect();
    try {
      applyGates(root);
    } finally {
      observer.observe(root, { childList: true, subtree: true, attributes: true, attributeFilter: ["disabled"] });
    }
  });
  return observer;
}
