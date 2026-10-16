/* "Show technical details" (Plan M §1 M3, M180, DEC-1610): one per-person switch that shows or hides everything
   technical - the Data, Model and Output pages of a list (their links), the model reports, Setup's Advanced
   settings, and every technical section - for analysts who want them. It changes what a person SEES, never what
   they may DO: roles and the API still decide that, and a technical page still opens when its address is visited
   (the CSS rule below that starts `html.hide-tech:has` shows the body of a technical tab opened by its address).

   What is technical is marked in the markup, the same marks the visible-text check reads (Plan M M170):
     - class "tech" (every "Technical details" section `dom.js` techDetails() draws, and older ones);
     - [data-technical] (a section, a tab or a link to a technical page);
     - details[data-technical-details] (techDetails() adds it; this file also adds it to any other <details>
       whose summary reads exactly "Technical details", as a page draws one).
   With the switch off, <html> carries the class "hide-tech" and one CSS rule hides all three. With it on, nothing
   is added to the page: it shows as it did before this file existed.

   Where the choice is kept:
     - signed in: on the server, per person (`GET /auth/me` -> `preferences.show_technical`, saved with
       `PUT /auth/me/preferences`), so it follows the person to another browser;
     - sign-in off (or no answer yet): this browser's localStorage (key STORAGE_KEY), or memory when storage is
       refused (a private window).
   Until a person chooses, the default depends on the layout (DEC-1610 (c)): OFF in the campaigns layout (M174's
   `<html data-ui-layout="campaigns">`), ON in the classic layout, so the classic app stays exactly as it was
   until somebody turns the switch off. M181 makes the campaigns layout the default.

   Imports: `api.js` (the API's address) and `dom.js` (escaping), two leaf modules; the production session is loaded on demand, as `layout.js` does,
   so a build without it simply keeps the choice in the browser. */

import { API_BASE } from "./api.js";
import { esc } from "./dom.js";

export const STORAGE_KEY = "marketing-ai.show-technical";
/** Fired on window whenever the effective value changes; `event.detail` is the new boolean. */
export const TECHNICAL_EVENT = "marketing-ai:technical-changed";
/** The class <html> carries while technical details are hidden. */
export const HIDE_CLASS = "hide-tech";
export const STYLE_ID = "tech-switch-styles";
export const SWITCH_LABEL = "Show technical details";
/** The layout M174 marks on <html>, and the event it fires when that changes (ui/layout.js). */
const LAYOUT_ATTR = "data-ui-layout";
const LAYOUT_EVENT = "marketing-ai:layout-changed";
const TECH_SUMMARY = /^\s*technical details\s*$/i;

export const CSS = `
html.${HIDE_CLASS} .tech,html.${HIDE_CLASS} [data-technical],html.${HIDE_CLASS} details[data-technical-details]{display:none !important}
html.${HIDE_CLASS} .flow>[data-technical]+.arrow{display:none !important}
html.${HIDE_CLASS}:has([data-ds-tabs] [data-technical][aria-selected="true"]) :is(section,details,div)[data-technical]:not([data-ds-tabs] *),html.${HIDE_CLASS}:has([data-ds-tabs] [data-technical][aria-selected="true"]) :is(section,details).tech:not([data-ds-tabs] *){display:block !important}
.tech-switch{display:flex;align-items:flex-start;gap:10px;cursor:pointer;font-size:14px;color:var(--ink)}
.tech-switch input{margin:2px 0 0;width:16px;height:16px;flex:none;accent-color:var(--brand-blue)}
.tech-switch .ts-text{display:flex;flex-direction:column;gap:2px}
.tech-switch .ts-label{font-weight:600}
.tech-switch .ts-hint{font-size:13px;color:var(--muted);line-height:1.45;font-weight:400}
.menu-pop .tech-switch{padding:8px 12px;font-size:13px}
`;

const state = {
  local: null, // this browser's saved choice: true, false or null (none)
  memory: null, // the fallback when storage is refused
  signedIn: false,
  server: null, // the signed-in person's saved choice: true, false or null (none)
  value: null, // the effective value last applied
};

// --- storage: localStorage, or memory when the browser refuses it --------------------------------------------------

function readLocal() {
  try {
    const raw = window.localStorage.getItem(STORAGE_KEY);
    if (raw === "on") return true;
    if (raw === "off") return false;
    return null;
  } catch {
    return state.memory;
  }
}

function writeLocal(on) {
  state.memory = on;
  try {
    window.localStorage.setItem(STORAGE_KEY, on ? "on" : "off");
  } catch {
    // private window or blocked storage: memory keeps it until the page is reloaded
  }
}

// --- the value -------------------------------------------------------------------------------------------------------

/** True in M174's campaigns layout; false in the classic layout, or before M174's file has marked it. */
export function campaignLayout() {
  try {
    return document.documentElement.getAttribute(LAYOUT_ATTR) === "campaigns";
  } catch {
    return false;
  }
}

/** The value until a person chooses: off in the campaigns layout, on in the classic one (DEC-1610 (c)). */
export const defaultShowTechnical = () => !campaignLayout();

/** Whether technical details are shown for this person, now. */
export function showTechnical() {
  const chosen = state.signedIn ? state.server : state.local;
  return typeof chosen === "boolean" ? chosen : defaultShowTechnical();
}

/** Put the effective value on the page (the <html> class and every switch) and announce a change. */
function apply() {
  const on = showTechnical();
  if (!started) return on;
  const root = document.documentElement;
  if (root) {
    if (on) root.classList.remove(HIDE_CLASS);
    else root.classList.add(HIDE_CLASS);
    if (root.getAttribute("class") === "") root.removeAttribute("class");
  }
  syncSwitches(on);
  const changed = state.value !== null && state.value !== on;
  state.value = on;
  if (changed && typeof window.dispatchEvent === "function") {
    window.dispatchEvent(new window.CustomEvent(TECHNICAL_EVENT, { detail: on }));
  }
  return on;
}

/**
 * Turn technical details on or off for this person. Signed in, the choice is saved on the server (and put back
 * if the server refuses); otherwise in this browser. Resolves to the value in effect afterwards.
 */
export async function setShowTechnical(on) {
  const next = Boolean(on);
  if (!state.signedIn) {
    writeLocal(next);
    state.local = next;
    return apply();
  }
  const before = state.server;
  state.server = next;
  apply();
  try {
    const response = await window.fetch(`${API_BASE}/auth/me/preferences`, {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ show_technical: next }),
    });
    if (!response.ok) throw new Error(`HTTP ${response.status}`);
  } catch {
    state.server = before;
  }
  return apply();
}

/** Call `listener(on)` after every change. Returns the unsubscribe function. */
export function onTechnicalChange(listener) {
  const handler = (event) => listener(Boolean(event.detail));
  window.addEventListener(TECHNICAL_EVENT, handler);
  return () => window.removeEventListener(TECHNICAL_EVENT, handler);
}

// --- the switch ------------------------------------------------------------------------------------------------------

/** A real checkbox with its label (keyboard: Tab to it, Space to change it). Any number may be on a page. */
export function technicalSwitchHtml({ hint = "", id = "" } = {}) {
  const on = showTechnical();
  const attrs = id ? ` id="${esc(id)}"` : "";
  const text = hint ? `<span class="ts-hint">${esc(hint)}</span>` : "";
  return `<label class="tech-switch"><input type="checkbox" role="switch" data-tech-switch${attrs}${on ? " checked" : ""}><span class="ts-text"><span class="ts-label">${SWITCH_LABEL}</span>${text}</span></label>`;
}

function syncSwitches(on) {
  for (const input of document.querySelectorAll("input[data-tech-switch]")) {
    if (input.checked !== on) input.checked = on;
  }
}

// --- "Technical details" sections a page draws by hand ---------------------------------------------------------------

/** Mark every <details> under `root` whose summary reads exactly "Technical details", as techDetails() does. */
export function markTechnicalDetails(root = document) {
  if (!root || typeof root.querySelectorAll !== "function") return;
  const list = [...root.querySelectorAll("details:not([data-technical-details])")];
  if (root.tagName && root.tagName.toUpperCase() === "DETAILS") list.push(root);
  for (const details of list) {
    if (details.hasAttribute("data-technical-details")) continue;
    const summary = [...details.children].find((c) => c.tagName.toUpperCase() === "SUMMARY");
    if (summary && TECH_SUMMARY.test(summary.textContent)) details.setAttribute("data-technical-details", "");
  }
}

// --- start-up --------------------------------------------------------------------------------------------------------

function injectStyles(doc = document) {
  if (!doc || !doc.head || doc.getElementById(STYLE_ID)) return;
  const style = doc.createElement("style");
  style.id = STYLE_ID;
  style.textContent = CSS;
  doc.head.appendChild(style);
}

function onChange(event) {
  const input = event.target;
  if (!input || typeof input.matches !== "function" || !input.matches("input[data-tech-switch]")) return;
  setShowTechnical(input.checked);
}

let started = false;

/** Once per page: styles, the saved choice, the change handler, and the session and layout followers. */
export function startTechnical() {
  // A unit test's stand-in window (no document, no events) gets the functions and nothing else.
  if (started || typeof document === "undefined" || typeof document.addEventListener !== "function") return;
  if (typeof document.querySelectorAll !== "function" || !document.documentElement) return;
  if (typeof window === "undefined" || typeof window.addEventListener !== "function") return;
  started = true;
  injectStyles();
  state.local = readLocal();
  document.addEventListener("change", onChange);
  window.addEventListener(LAYOUT_EVENT, () => apply());
  const Observer = window.MutationObserver;
  if (Observer && document.body) {
    markTechnicalDetails(document.body);
    new Observer((records) => {
      for (const record of records) {
        for (const node of record.addedNodes) if (node.nodeType === 1) markTechnicalDetails(node);
        // a summary whose text was drawn after its <details>
        const details = record.target.nodeType === 1 ? record.target.closest("details") : null;
        if (details) markTechnicalDetails(details);
      }
    }).observe(document.body, { childList: true, subtree: true });
  }
  // A new switch drawn by a page (Settings) shows the current value; one drawn before a session answer is
  // corrected by apply().
  apply();
}

/** Follow the production session: who is signed in, and their saved choice. Without that module: the browser. */
export const followSession = import("./modules/production/session.js").then(
  (session) =>
    session.onSession((me, status) => {
      const signedIn = status === "signed-in" && Boolean(me) && Boolean(me.preferences);
      state.signedIn = signedIn;
      state.server = signedIn && typeof me.preferences.show_technical === "boolean" ? me.preferences.show_technical : null;
      if (started) apply();
    }),
  () => null,
);

/** Test seam: back to the state of a fresh page (keeps the listeners). */
export function _resetTechnicalForTests() {
  state.local = readLocal();
  state.signedIn = false;
  state.server = null;
  state.value = null;
  apply();
}

/** Test seam: what this module believes. */
export const _technicalState = () => ({ ...state });

startTechnical();
