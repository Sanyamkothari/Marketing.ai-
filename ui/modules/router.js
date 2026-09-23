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

export const MODULES_CHANGED = "marketing-ai:modules-changed";

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

/** Something drawn at the right of every page header, beside the logo: `{ name, html() }`. */
export function registerHeaderTool(tool) {
  const { name, html } = tool || {};
  if (!name || typeof html !== "function") throw new Error("registerHeaderTool needs { name, html() }");
  if (extensions.headerTool) throw new Error(`A header tool ("${extensions.headerTool.name}") is already registered`);
  extensions.headerTool = tool;
  announce();
}

/** The header tool's markup, or `""` when none is registered. */
export function headerToolHtml() {
  return extensions.headerTool ? extensions.headerTool.html() : "";
}
// ---- END PHASE-2 ----

// ---- PHASE-3A (generative) — append only below this line ----
// ---- END PHASE-3A ----

// ---- PHASE-4A (aws) — append only below this line ----
// ---- END PHASE-4A ----

// ---- PHASE-4B (production) — append only below this line ----
// ---- END PHASE-4B ----
