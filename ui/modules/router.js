// The phase-module registry (PARALLEL_WORK_PROTOCOL.md §4).
//
// Phase 1's router is `app.js`: it owns the overview, the use-case screen and the three pages,
// and its routes do not change. A phase branch adds whole screens of its own - onboarding,
// generative - and needs somewhere to claim a URL without editing a line `app.js` already has.
// This is that somewhere.
//
// A module registers once, at import time, and claims hash routes by prefix:
//
//     import { registerModule } from "../router.js";   // from ui/modules/<phase>/index.js
//     registerModule({
//       name: "onboarding",
//       routes: ["clients", "sources", "mappings"],
//       render: async (app, parts) => { ... },   // `parts` is the hash, split on "/"
//     });
//
// `app.js` asks `resolveRoute()` first and falls through to its own routing when nothing claims
// the hash - so with no module registered, which is the state of this branch, the UI behaves
// exactly as it did before this file existed.

// The top bar's store (v1 seams, at the end of this file). Imported first, before the Phase 4b block's
// `boot.js`: ES modules evaluate their imports in source order, so a slot or access provider that
// `boot.js` registers finds `chrome.js` already evaluated. `chrome.js` imports only `dom.js`.
import { addNavSlot, refreshChrome, setAccess, setActiveNav as markActiveNav } from "../chrome.js";
// Screens ask this before drawing a control the person could not use (a Viewer's Setup is read-only).
export { canAccess } from "../chrome.js";

const registered = [];

/**
 * Add a module to the registry. `name` must be unique; `routes` are first hash segments it owns
 * (`#/clients/...` is claimed by `"clients"`); `render(app, parts)` draws into the app element.
 */
export function registerModule(module) {
  const { name, routes, render } = module || {};
  if (!name || typeof render !== "function" || !Array.isArray(routes) || routes.length === 0) {
    throw new Error("registerModule needs { name, routes: [...], render(app, parts) }");
  }
  if (registered.some((m) => m.name === name)) {
    throw new Error(`A module named "${name}" is already registered`);
  }
  const taken = routes.find((route) => registered.some((m) => m.routes.includes(route)));
  if (taken) throw new Error(`Route "${taken}" is already claimed by another module`);
  registered.push({ name, routes: [...routes], render });
}

/** Every registered module, in registration order. Read-only: the array is a copy. */
export function phaseModules() {
  return registered.map((m) => ({ name: m.name, routes: [...m.routes] }));
}

/**
 * The module that owns this hash, or `null` when none does - which is every hash until a phase
 * branch registers one, so `app.js` keeps its own routing unchanged.
 */
export function resolveRoute(parts) {
  if (!Array.isArray(parts) || parts.length === 0) return null;
  return registered.find((m) => m.routes.includes(parts[0])) || null;
}

// ===========================================================================
// Shared file (PARALLEL_WORK_PROTOCOL.md §4): three branches edit it at once.
// Add code only inside your own block, at its end. Never edit above your
// block, never reorder, never reformat the rest of the file.
// Importing your module's entry point is enough - it calls registerModule()
// itself, so nothing above this line has to learn its name.
// ===========================================================================

// ---- PHASE-2 (onboarding) — append only below this line ----
// Two seams a hash route cannot give a module (Plan A M35). Onboarding draws no screen of its own:
// it is a way to fill Step 1 of the Phase 1 Setup form ("Build from raw tables"), and it puts a
// client picker in the header every screen shares. So instead of a route it registers a *setup
// source* and a *header tool*, and the Phase 1 modules that own those places ask for them here -
// which keeps the one-way rule this file exists for: `ui/usecase.js` and `ui/dom.js` import this
// registry, never a phase module, and with nothing registered they render exactly as before.
//
// Registration can land after the first screen is painted - `index.html` loads a phase module after
// `app.js` - so every registration announces itself with `MODULES_CHANGED`, and `app.js` repaints
// the current route when it hears it.
//
// The header tool itself is kept by `ui/dom.js`, which draws the header: `dom.js` must not import
// this file (see `setHeaderTool`), so the edge runs from here to there.

import { MODULES_EVENT, headerToolHtml as drawnHeaderTool, setGlossary, setHeaderTool } from "../dom.js";

export const MODULES_CHANGED = MODULES_EVENT;

const extensions = { setupSource: null, headerTool: null };
let announcing = false;

/** One `MODULES_CHANGED` per task, however many registrations it carried: a module that registers
 * a setup source and a header tool together is one change to the screen, not two repaints. */
export function announceModulesChanged() {
  if (announcing) return;
  announcing = true;
  queueMicrotask(() => {
    announcing = false;
    window.dispatchEvent(new Event(MODULES_CHANGED));
  });
}

const announce = announceModulesChanged;

/**
 * A second way to fill Setup Step 1 (the first is uploading a prepared file). `source` is
 * `{ name, card(uc, mode, context), mount(container, options), context() }`; `ui/usecase.js`
 * documents what it calls each with. One per page: a second registration is a wiring mistake.
 */
export function registerSetupSource(source) {
  const { name, card, mount, context } = source || {};
  if (!name || typeof card !== "function" || typeof mount !== "function" || typeof context !== "function") {
    throw new Error("registerSetupSource needs { name, card(uc, mode, context), mount(container, options), context() }");
  }
  if (extensions.setupSource) throw new Error(`A setup source ("${extensions.setupSource.name}") is already registered`);
  extensions.setupSource = source;
  announce();
}

/** The registered setup source, or `null` - in which case Step 1 is the upload control alone. */
export function setupSource() {
  return extensions.setupSource;
}

/**
 * The top bar's context slot - the client picker - `{ name, html() }`. Since v1 the top bar
 * (`ui/chrome.js`) draws it at its right end; the page header's right side is the logo alone. `html()`
 * may return `""` on a route where the tool means nothing.
 */
export function registerHeaderTool(tool) {
  const { name, html } = tool || {};
  if (!name || typeof html !== "function") throw new Error("registerHeaderTool needs { name, html() }");
  if (extensions.headerTool) throw new Error(`A header tool ("${extensions.headerTool.name}") is already registered`);
  extensions.headerTool = tool;
  setHeaderTool(tool);
  announce();
}

/** The header tool's markup, or `""` when none is registered. */
export function headerToolHtml() {
  return drawnHeaderTool();
}
// ---- END PHASE-2 ----

// ---- PHASE-3A (generative) — append only below this line ----
// ---- END PHASE-3A ----

// ---- PHASE-4A (aws) — append only below this line ----
// ---- END PHASE-4A ----

// ---- PHASE-4B (production) — append only below this line ----
// Side effects only, and deliberately not `index.js`: boot.js must run before app.js's first render
// (the bearer token on the very first call) and must not import this file back (DEC-790). The screens
// register themselves from index.html's Phase 4b block once this file has finished evaluating.
import "./production/boot.js";
// ---- END PHASE-4B ----
// ---- PHASE-3B (uplift) — append only below this line ----
// The uplift module (`modules/uplift/index.js`, routes `uplift` and `campaign`) is loaded by its own
// `<script type="module">` in index.html's PHASE-3B block, not imported here: it imports this file
// for `registerModule`, and an import back from here would run it before `registered` exists.
// ---- END PHASE-3B ----
// ---- PLAN-E (pilot) — append only below this line ----
// The pilot module (`modules/pilot/index.js`, route `pilot`) is loaded by its own `<script>` in
// index.html's PLAN-E block, as the uplift module is, for the same reason: it imports this file.
// ---- END PLAN-E ----

// ---- V1-UI (foundation seams, docs/ui/FOUNDATION.md) ----
// How a module fills the top bar and the shared screens without editing them. `registerNavSlot`,
// `registerAccess` and `registerGlossary` only forward to `chrome.js` / `dom.js`, which are evaluated
// before `production/boot.js` (see the import at the top), so they are safe to call from boot.
// `registerRunAction` keeps its list here: call it from a module's entry point, as `registerModule`.

/**
 * Fill a top-bar slot: `name` is "models", "admin", "demo", "help", "user" or "badge:approvals"
 * (see `chrome.js`); `slot` is `{ html(), bind?(bar) }`. Several registrations under one name are
 * drawn in registration order. The bar redraws on every route change and `MODULES_CHANGED`; call
 * `refreshTopBar()` when only the slot's own state changed.
 */
export function registerNavSlot(name, slot) {
  const { html, bind } = slot || {};
  if (!name || typeof html !== "function" || (bind !== undefined && typeof bind !== "function")) {
    throw new Error("registerNavSlot needs (name, { html(), bind?(bar) })");
  }
  addNavSlot(name, { html, bind });
}

/**
 * Who may see what in the top bar: `{ can(method, path), status?() }` - `can` as the production
 * session's, `status()` returning "signed-out" to draw the wordmark and the user slot only. With no
 * provider every item is drawn (a deployment without sign-in).
 */
export function registerAccess(provider) {
  if (!provider || typeof provider.can !== "function") throw new Error("registerAccess needs { can(method, path) }");
  setAccess(provider);
}

/**
 * The plain-language catalogue (`GET /pilot/help`): `{ code, term, metric, setting }` as lookup
 * functions or maps, or the catalogue itself. `errorBox` then leads with a code's title, and screens
 * read `glossaryCode` / `glossaryTerm` / `glossaryMetric` / `glossarySetting` from `dom.js`.
 */
export function registerGlossary(glossary) {
  if (!glossary || typeof glossary !== "object") {
    throw new Error("registerGlossary needs { code, term, metric, setting }");
  }
  setGlossary(glossary);
}

const runActions = [];

/**
 * An action offered on a run's results (a button in the Output page's header, or a flow block):
 * `{ name, applies(uc, run), html(uc, run) }`. `name` must be unique.
 */
export function registerRunAction(action) {
  const { name, applies, html } = action || {};
  if (!name || typeof applies !== "function" || typeof html !== "function") {
    throw new Error("registerRunAction needs { name, applies(uc, run), html(uc, run) }");
  }
  if (runActions.some((a) => a.name === name)) throw new Error(`A run action named "${name}" is already registered`);
  runActions.push({ name, applies, html });
  announceModulesChanged();
}

/** The markup of every run action that applies to this run, in registration order; `""` for none. */
export function runActionsHtml(uc, run) {
  return runActions
    .map((action) => {
      try {
        return action.applies(uc, run) ? action.html(uc, run) || "" : "";
      } catch {
        return "";
      }
    })
    .join("");
}

/** Mark a top-bar goal active for the current route only ("campaigns" on a scoring run's Output). */
export function setActiveNav(id) {
  markActiveNav(id);
}

/** Redraw the top bar now that a slot's own state changed (a count, the signed-in user). */
export function refreshTopBar() {
  refreshChrome();
}
// ---- END V1-UI ----
