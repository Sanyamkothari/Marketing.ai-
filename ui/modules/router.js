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
// ---- END PHASE-2 ----

// ---- PHASE-3A (generative) — append only below this line ----
// ---- END PHASE-3A ----

// ---- PHASE-4A (aws) — append only below this line ----
// ---- END PHASE-4A ----
