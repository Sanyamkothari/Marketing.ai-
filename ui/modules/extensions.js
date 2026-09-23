// Plan A M35's two seams - a second way to fill Setup Step 1, and a tool in every page header -
// kept in a module of their own that imports nothing. They used to live in `router.js`'s PHASE-2
// block, which `router.js` still re-exports, so every module that imported them from there keeps
// working. `dom.js` and `usecase.js` import them from here instead: `router.js` also loads Phase
// 4b's `boot.js` for its side effects, and `boot.js` imports `dom.js`, so `dom.js` importing
// `router.js` made a cycle in which `boot.js` ran before `dom.js` had defined anything (DEC-801).
//
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
