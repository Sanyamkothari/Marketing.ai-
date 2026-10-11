// The top bar: one role-aware bar above every screen (v1, docs/ui/FOUNDATION.md).
//
//   [Marketing AI]  Home  Connections  Results (2)  Settings          [chip] [user] [Minfy logo]
//
// Plan H M82 (DEC-1113): exactly four places. Home is the journey, Connections the cloud services
// and the AI service (M80), Results every run (with the approvals count as its badge), Settings
// everything else - privacy, schedules, admin, the advanced tools. The screens that used to be top-level items
// (Build data, Reports, Campaigns, Model health, Schedules, Privacy, Admin, Uplift, Waiting for
// approval) keep their routes; they are opened from Results, from a run, or from Settings. The
// "admin" and "models" slots are no longer drawn in the bar: the Settings page reads them
// (`navSlotHtml`), so the role checks their owners make still decide what is offered. The place marked
// is `navFor`'s for the route, except a run opened from Results, which stays under Results (`placeFor`).
//
// It is `<header id="pb-bar" class="topbar">` - the id the Phase 4b user bar had, so every test that
// reads `#pb-bar a[href=...]` keeps reading the same place - drawn *before* `#app`, outside the area
// each screen repaints. Phase modules never edit it: they fill its slots through `modules/router.js`
// (`registerNavSlot`, `registerAccess`, `registerHeaderTool`, `setActiveNav`), which forwards here.
//
// This file imports `dom.js` only. `modules/router.js` imports it (before `production/boot.js`, so a
// slot or access provider registered from boot finds this module evaluated) and `app.js` mounts it;
// nothing here imports the router back, so the graph stays acyclic (DEC-790).
//
// Slots (each `{ html(), bind?(bar) }`; several registrations under one name are drawn in order):
//   "models"   extra model tools (`<a>` elements); drawn on the Settings page, not in the bar
//   "admin"    the admin entries (Users, Audit log, ...); drawn on the Settings page, not in the bar
//   "demo"     the sample-data chip, right side
//   "help"     the Help (?) menu's entries, drawn top right beside the user menu (Plan M M173); each slot
//              may carry an `order` number (default 0) that sorts its entries, lowest first
//   "user"     the user menu (or "Sign in", or the sign-in-off chip), far right
//   "badge:approvals"  a count shown on Results ("" for none)
// The context slot is the registered header tool, `headerToolHtml()` (Plan H: it draws nothing, as
// there is one company and no client to choose).
//
// Until a module fills "user", an older strip mounted before `#app` (Phase 4b's `#pb-bar` user bar,
// Plan E's `#pe-bar`) is adopted into the bar's second row, so nothing it offered is lost.

import { LOGO, MODULES_EVENT, esc, headerToolHtml } from "./dom.js";
// <M174>
// Plan M M174 (DEC-1604): with the deployment's `ui_layout` set to "campaigns" the bar is the new shell
// (`shell.js`: the five-place sidebar, the bell, Help and the profile); in "classic" nothing below changes.
// Both files are leaves as far as this one is concerned: neither imports the router or this file. The lines
// between the M174 marks are the whole of M174 in this file: the classic layout's check removes them to draw
// the bar as it was before (tests/integration/test_shell_ui.py).
import { isCampaignLayout } from "./layout.js";
import { injectShellStyles, shellBarHtml } from "./shell.js";
// </M174>

const slots = new Map();
let access = null;
let active = { id: null, hash: null };
const ui = { menu: null, panel: false };
let bar = null;
let sub = null;
let lastHtml = "";
let scheduled = false;

/** The four places (Plan H M82). `need` is the `[method, path]` a role must be allowed (via
 * `registerAccess`); `badge` names the slot whose count is drawn beside the label. */
export const NAV_ITEMS = [
  { id: "home", label: "Home", href: "#/" },
  { id: "connections", label: "Connections", href: "#/connections" },
  { id: "results", label: "Results", href: "#/results", badge: "badge:approvals" },
  { id: "settings", label: "Settings", href: "#/settings" },
];

/** The goals screens still name with `setActiveNav` from before Plan H, and where they live now. */
const OLD_GOALS = { campaigns: "results", reports: "results", models: "home", build: "settings", admin: "settings" };

// --- what the router forwards ---------------------------------------------------------------------

/** `registerNavSlot`'s store. */
export function addNavSlot(name, slot) {
  if (!slots.has(name)) slots.set(name, []);
  slots.get(name).push(slot);
  refreshChrome();
}

/** `registerAccess`'s store: `{ can(method, path), status?() }`; `null` shows everything. */
export function setAccess(provider) {
  access = provider || null;
  refreshChrome();
}

/** Mark a goal active for the current route only (a scoring run's Output page is a Result). */
export function setActiveNav(id) {
  active = { id, hash: currentHash() };
  refreshChrome();
}

/** Redraw the bar at the end of this task (many changes in one task are one redraw). */
export function refreshChrome() {
  if (scheduled) return;
  scheduled = true;
  queueMicrotask(() => {
    scheduled = false;
    paintChrome();
  });
}

// --- routes ------------------------------------------------------------------------------------------

const currentHash = () => (typeof window === "undefined" ? "#/" : window.location.hash || "#/");

/** The place a route belongs to (Plan H M82), or `null` (sign-in, account). */
export function navFor(hash) {
  const parts = String(hash || "").replace(/^#\/?/, "").split("/").filter(Boolean);
  const [a, b] = parts;
  if (!a || a === "industry" || a === "uc") return "home";
  if (a === "connections") return "connections";
  if (a === "results" || a === "campaign" || a === "approvals") return "results";
  if (a === "pilot") {
    if (b === "kit" || (b === "view" && parts[2] === "readiness")) return "settings";
    return "results";
  }
  if (a === "monitoring") return b === "runs" ? "results" : "settings";
  if (a === "generative") return b === "copy" ? "results" : b === "connection" ? "connections" : "home";
  if (a === "settings" || a === "admin" || a === "privacy" || a === "uplift") return "settings";
  return null;
}

// --- a run opened from Results (docs/UI_AUDIT.md §8.4 item 2) --------------------------------------
//
// A run's own screens are use-case routes, which `navFor` puts under Home. Opened from Results (a row,
// the campaign page, Waiting for approval), the bar would jump to Home. So the place a run was opened
// from is remembered, per run, while the person stays on it (its Data / Model / Output pages, its
// root-cause notes): Results stays marked. Opening the same run from Home (its use case's runs) marks
// Home again. The memory is this tab's `sessionStorage`, so a reload keeps the mark; without storage
// (a private window) it lasts until the reload.

const RUN_PAGES = new Set(["run", "data", "model", "output"]);
const ORIGIN_KEY = "marketing-ai:run-opened-from";
const ORIGINS_KEPT = 50;
const origins = new Map(); // "<use case>/<run>" -> "results"
let originsLoaded = false;

/** The run a route shows, as "<use case>/<run>", or null: `#/uc/<uc>/{run,data,model,output}/<run>`
 * and the run's root-cause notes, `#/generative/rca/<uc>/<run>`. */
export function runOf(hash) {
  const parts = String(hash || "").replace(/^#\/?/, "").split("/").filter(Boolean);
  if (parts[0] === "uc" && RUN_PAGES.has(parts[2]) && parts[1] && parts[3]) return `${parts[1]}/${parts[3]}`;
  if (parts[0] === "generative" && parts[1] === "rca" && parts[2] && parts[3]) return `${parts[2]}/${parts[3]}`;
  return null;
}

function storage() {
  try {
    return typeof window !== "undefined" && window.sessionStorage ? window.sessionStorage : null;
  } catch {
    return null;
  }
}

function loadOrigins() {
  if (originsLoaded) return;
  originsLoaded = true;
  try {
    const saved = JSON.parse((storage() && storage().getItem(ORIGIN_KEY)) || "[]");
    for (const run of Array.isArray(saved) ? saved : []) if (typeof run === "string") origins.set(run, "results");
  } catch {
    // unreadable storage: nothing remembered, the bar follows the route
  }
}

function saveOrigins() {
  while (origins.size > ORIGINS_KEPT) origins.delete(origins.keys().next().value);
  try {
    const store = storage();
    if (store) store.setItem(ORIGIN_KEY, JSON.stringify([...origins.keys()]));
  } catch {
    // storage refused (quota, private mode): the mark lasts for this page only
  }
}

/** The place the bar marks for a route: `navFor`, except a run opened from Results is a Result. */
export function placeFor(hash) {
  const place = navFor(hash);
  if (place !== "home") return place;
  loadOrigins();
  const run = runOf(hash);
  return run && origins.get(run) === "results" ? "results" : place;
}

/** A route change from `from` to `to`: remember where a run was opened from (moving between one run's
 * own pages keeps what was remembered for it). */
export function noteRoute(from, to) {
  const run = runOf(to);
  if (!run || runOf(from) === run) return;
  loadOrigins();
  origins.delete(run);
  if (placeFor(from) === "results") origins.set(run, "results");
  saveOrigins();
}

let lastHash = null;

function activeId() {
  const hash = currentHash();
  const id = active.hash === hash && active.id ? active.id : placeFor(hash);
  return OLD_GOALS[id] || id;
}

// --- drawing -----------------------------------------------------------------------------------------

/** Whether the signed-in person may call `method path`, from the same provider the bar hides items by.
 * With no provider (sign-in off, or not resolved yet) everyone may, as the bar shows everything. */
export function canAccess(method, path) {
  return allowed([method, path]);
}

function allowed(need) {
  if (!need || !access || typeof access.can !== "function") return true;
  try {
    return access.can(need[0], need[1]) !== false;
  } catch {
    return true;
  }
}

/** The access provider's `status()` ("off", "signed-in", "signed-out", ...), or null without one. */
export function accessStatus() {
  return sessionState();
}

function sessionState() {
  try {
    return access && typeof access.status === "function" ? access.status() : null;
  } catch {
    return null;
  }
}

function slotHtml(name) {
  // Stable sort: slots with no `order` (or equal ones) keep their registration order.
  return [...(slots.get(name) || [])]
    .sort((a, b) => (Number(a.order) || 0) - (Number(b.order) || 0))
    .map((slot) => {
      try {
        return slot.html() || "";
      } catch {
        return "";
      }
    })
    .join("");
}

/** A slot's markup, for a screen that draws it instead of the bar (the Settings page, Plan H). */
export function navSlotHtml(name) {
  return slotHtml(name);
}

const chev = `<span class="chev" aria-hidden="true"></span>`;

function badge(name) {
  const count = String(slotHtml(name) || "").trim();
  return count && count !== "0" ? ` <span class="count" aria-label="${esc(count)} waiting">${esc(count)}</span>` : "";
}

function menuItem(item, on) {
  const menuId = `tn-${item.id}`;
  const entries = item.menu
    .filter((entry) => allowed(entry.need))
    .map(
      (entry) =>
        `<a href="${esc(entry.href)}"${currentHash() === entry.href && entry.href !== "#/" ? ` class="on" aria-current="page"` : ""}>${esc(
          entry.label,
        )}${entry.badge ? badge(entry.badge) : ""}</a>`,
    )
    .join("");
  const extra = item.slot ? slotHtml(item.slot) : "";
  if (!entries && !extra) return "";
  const open = ui.menu === menuId;
  const count = item.badge ? badge(item.badge) : "";
  return `<li class="menu"><button type="button" class="tn-item${on ? " on" : ""}" data-menu="${menuId}" aria-expanded="${open}" aria-controls="${menuId}"${
    on ? ` aria-current="true"` : ""
  }>${esc(item.label)}${count}${chev}</button><div class="menu-pop" id="${menuId}"${open ? "" : " hidden"}>${entries}${extra}</div></li>`;
}

/**
 * The Help (?) menu (Plan M M173): the `help` slot's entries under one button, top right. Drawn only
 * when some module has put an entry in the slot. The button is a plain `button` with
 * `aria-expanded`; Enter or Space opens it, Arrow keys move through the entries, Escape closes it and
 * puts focus back on the button.
 */
function helpMenuHtml() {
  const entries = slotHtml("help");
  if (!entries) return "";
  const open = ui.menu === "tn-help";
  return `<div class="menu tb-help"><button type="button" class="tb-help-btn" data-menu="tn-help" aria-expanded="${open}" aria-controls="tn-help" aria-haspopup="true" aria-label="Help" title="Help"><span aria-hidden="true">?</span></button><div class="menu-pop right" id="tn-help"${
    open ? "" : " hidden"
  }>${entries}</div></div>`;
}

/** The bar's markup for the current route, slots and access (exported for tests). */
export function topBarHtml() {
  // <M174>
  if (isCampaignLayout()) return campaignShellHtml();
  // </M174>
  const state = sessionState();
  const signedOut = state === "signed-out";
  const on = activeId();
  const items = signedOut
    ? ""
    : NAV_ITEMS.map((item) => {
        if (!allowed(item.need)) return "";
        if (item.menu) return menuItem(item, on === item.id);
        const here = on === item.id;
        return `<li><a class="tn-item${here ? " on" : ""}" href="${esc(item.href)}"${here ? ` aria-current="page"` : ""}>${esc(
          item.label,
        )}${item.badge ? badge(item.badge) : ""}</a></li>`;
      }).join("");
  const nav = items ? `<nav class="topnav tb-c" aria-label="Main"><ul>${items}</ul></nav>` : "";
  const context = signedOut ? "" : headerToolHtml();
  const utils = `${signedOut ? "" : slotHtml("demo")}${signedOut ? "" : helpMenuHtml()}${slotHtml("user")}`;
  return `<div class="tb-row"><a class="wordmark" href="#/">Marketing AI</a>${nav}<div class="tb-context">${context}</div>${
    utils ? `<div class="tb-utils tb-c">${utils}</div>` : ""
  }<a class="tb-logo" href="#/" aria-label="Minfy, home">${LOGO}</a><button type="button" class="btn secondary sm tb-menu-btn" data-tb-menu aria-expanded="${ui.panel}" aria-controls="pb-bar">Menu</button></div>`;
}

// <M174>
/** The campaigns layout's bar (Plan M M174): the same slots and state, drawn by `shell.js`. */
function campaignShellHtml() {
  const status = sessionState();
  return shellBarHtml({
    hash: currentHash(),
    status,
    slot: slotHtml,
    context: status === "signed-out" ? "" : headerToolHtml(),
    menu: ui.menu,
    panel: ui.panel,
  });
}

/** The campaigns layout's styles and the bar's two marks (the shell is on; it has a sidebar), or none. */
function markShell() {
  if (isCampaignLayout()) {
    injectShellStyles(bar.ownerDocument);
    bar.classList.add("sh-on");
    bar.classList.toggle("sh-side-on", Boolean(bar.querySelector(".sh-side")));
  } else if (bar.classList.contains("sh-on")) {
    bar.classList.remove("sh-on", "sh-side-on");
  }
}

// </M174>
/**
 * Where keyboard focus is inside the bar's row, as something that survives the row being drawn again:
 * a menu's button (`{ menu }`), or the n-th entry of a menu (`{ menu, index }`). A late registration
 * redraws the bar while someone is arrowing through the Help menu; they must not lose their place.
 */
function focusInRow(row) {
  const doc = row && row.ownerDocument;
  const active = doc && doc.activeElement;
  if (!active || !row.contains(active)) return null;
  const trigger = active.closest("[data-menu]");
  if (trigger) return { menu: trigger.getAttribute("data-menu") };
  const pop = active.closest(".menu-pop");
  if (!pop) return null;
  return { menu: pop.id, index: popEntries(pop).indexOf(active) };
}

function restoreFocus(row, focus) {
  if (!row || !focus) return;
  const target =
    focus.index === undefined
      ? row.querySelector(`[data-menu="${focus.menu}"]`)
      : popEntries(row.querySelector(`#${focus.menu}`) || document.createElement("div"))[focus.index];
  if (target) target.focus();
}

function paintChrome() {
  if (!bar) return;
  const html = topBarHtml();
  if (html !== lastHtml) {
    lastHtml = html;
    // The second row (adopted strips) is kept as it is: its owners hold references to its elements.
    const row = bar.querySelector(".tb-row");
    const holder = bar.ownerDocument.createElement("div");
    holder.innerHTML = html;
    const focus = focusInRow(row);
    if (row) row.replaceWith(holder.firstElementChild);
    else bar.insertBefore(holder.firstElementChild, bar.firstChild);
    restoreFocus(bar.querySelector(".tb-row"), focus);
  }
  bar.classList.toggle("open", ui.panel);
  // <M174>
  markShell();
  // </M174>
  for (const [, list] of slots) {
    for (const slot of list) {
      if (typeof slot.bind === "function") {
        try {
          slot.bind(bar);
        } catch {
          // a slot that fails to wire leaves its markup; the rest of the bar still works
        }
      }
    }
  }
}

// --- behaviour: menus, the mobile panel, toggletips, copy buttons, the skip link -------------------

function closeMenus(focusTrigger = false) {
  if (!ui.menu) return;
  const id = ui.menu;
  ui.menu = null;
  const doc = bar && bar.ownerDocument;
  if (doc) {
    const pop = doc.getElementById(id);
    if (pop) pop.hidden = true;
    const trigger = bar.querySelector(`[data-menu="${id}"]`);
    if (trigger) {
      trigger.setAttribute("aria-expanded", "false");
      if (focusTrigger) trigger.focus();
    }
  }
  lastHtml = "";
}

function closeToggletips(doc, except = null) {
  for (const btn of doc.querySelectorAll("[data-toggletip][aria-expanded='true']")) {
    if (btn === except) continue;
    btn.setAttribute("aria-expanded", "false");
    const pop = btn.nextElementSibling;
    if (pop) pop.hidden = true;
  }
}

function onClick(event) {
  const doc = event.currentTarget;
  const target = event.target;
  if (!target || typeof target.closest !== "function") return;
  const skip = target.closest("[data-skip]");
  if (skip) {
    // `#app` as a hash would be read as a route; move focus instead.
    event.preventDefault();
    const app = doc.getElementById("app");
    if (app) app.focus();
    return;
  }
  const menuBtn = target.closest("[data-menu]");
  if (menuBtn && bar && bar.contains(menuBtn)) {
    const id = menuBtn.getAttribute("data-menu");
    const opening = ui.menu !== id;
    closeMenus();
    if (opening) {
      ui.menu = id;
      menuBtn.setAttribute("aria-expanded", "true");
      const pop = doc.getElementById(id);
      if (pop) pop.hidden = false;
    }
    lastHtml = "";
    return;
  }
  const panel = target.closest("[data-tb-menu]");
  if (panel) {
    ui.panel = !ui.panel;
    panel.setAttribute("aria-expanded", String(ui.panel));
    bar.classList.toggle("open", ui.panel);
    lastHtml = "";
    return;
  }
  const tip = target.closest("[data-toggletip]");
  if (tip) {
    const open = tip.getAttribute("aria-expanded") !== "true";
    closeToggletips(doc, tip);
    tip.setAttribute("aria-expanded", String(open));
    const pop = tip.nextElementSibling;
    if (pop) pop.hidden = !open;
    return;
  }
  const copy = target.closest("[data-copy]");
  if (copy) {
    const text = copy.getAttribute("data-copy");
    const nav = doc.defaultView && doc.defaultView.navigator;
    if (nav && nav.clipboard && typeof nav.clipboard.writeText === "function") {
      nav.clipboard.writeText(text).then(
        () => {
          copy.textContent = "Copied";
        },
        () => {},
      );
    }
    return;
  }
  // <M174>
  // The campaigns layout's folded sidebar closes when one of its places is chosen, even the one on screen
  // (no route change follows that one, so the Menu button is told here).
  if (ui.panel && bar && target.closest(".sh-side a")) {
    ui.panel = false;
    bar.classList.remove("open");
    const menuBtn = bar.querySelector("[data-tb-menu]");
    if (menuBtn) menuBtn.setAttribute("aria-expanded", "false");
    lastHtml = "";
  }
  // </M174>
  // A click anywhere else, or on a link inside a menu, closes what was open.
  if (ui.menu && !(target.closest(".menu-pop") && !target.closest("a"))) closeMenus();
  if (!target.closest(".toggletip")) closeToggletips(doc);
}

/** The entries of an open menu, in order: its links and buttons that are not disabled. */
const popEntries = (pop) => [...pop.querySelectorAll("a[href], button")].filter((el) => !el.disabled && !el.hidden);

/**
 * Arrow keys in a menu (Plan M M173): from its button, Down opens it and lands on the first entry;
 * inside it, Down and Up move (wrapping), Home and End jump. Returns whether the key was used.
 */
function menuNavigation(event) {
  const target = event.target;
  if (!bar || !target || typeof target.closest !== "function") return false;
  if (!["ArrowDown", "ArrowUp", "Home", "End"].includes(event.key)) return false;
  const menu = target.closest(".menu");
  if (!menu || !bar.contains(menu)) return false;
  const pop = menu.querySelector(".menu-pop");
  if (!pop) return false;
  const trigger = target.closest("[data-menu]");
  if (trigger && menu.contains(trigger)) {
    if (event.key === "ArrowUp" || event.key === "End") return false;
    if (pop.hidden) trigger.click();
    const first = popEntries(pop)[0];
    if (first) first.focus();
    event.preventDefault();
    return true;
  }
  if (!pop.contains(target)) return false;
  const entries = popEntries(pop);
  if (!entries.length) return false;
  const at = entries.indexOf(target.closest("a[href], button"));
  let next;
  if (event.key === "ArrowDown") next = (at + 1) % entries.length;
  else if (event.key === "ArrowUp") next = (at - 1 + entries.length) % entries.length;
  else if (event.key === "Home") next = 0;
  else next = entries.length - 1;
  entries[next].focus();
  event.preventDefault();
  return true;
}

function onKey(event) {
  if (event.key !== "Escape") {
    menuNavigation(event);
    return;
  }
  const doc = event.currentTarget;
  if (ui.menu) {
    closeMenus(true);
    return;
  }
  const tip = doc.querySelector("[data-toggletip][aria-expanded='true']");
  if (tip) {
    closeToggletips(doc);
    tip.focus();
    return;
  }
  if (ui.panel && bar) {
    ui.panel = false;
    bar.classList.remove("open");
    const btn = bar.querySelector("[data-tb-menu]");
    if (btn) {
      btn.setAttribute("aria-expanded", "false");
      btn.focus();
    }
    lastHtml = "";
  }
}

/** The Help button's look. Injected once, tokens only, so both themes apply. */
const BAR_CSS = `
.tb-help-btn{display:inline-grid;place-items:center;width:32px;height:32px;padding:0;border:1px solid var(--line2);border-radius:50%;background:var(--surface);color:var(--ink2);font:inherit;font-size:14px;font-weight:600;cursor:pointer}
.tb-help-btn:hover,.tb-help-btn[aria-expanded="true"]{background:var(--soft);color:var(--ink)}
.tb-help .menu-pop{min-width:200px}
.tb-help .menu-pop a:focus-visible,.tb-help .menu-pop button:focus-visible,.tb-help-btn:focus-visible{outline:2px solid var(--brand-blue);outline-offset:-2px}
@media (max-width:700px){.topbar.open .tb-help{width:100%}.topbar.open .tb-help-btn{width:100%;border-radius:8px;justify-content:flex-start;padding:0 12px}}
`;

function injectBarStyles(doc) {
  if (!doc.head || doc.getElementById("tb-bar-styles")) return;
  const style = doc.createElement("style");
  style.id = "tb-bar-styles";
  style.textContent = BAR_CSS;
  doc.head.appendChild(style);
}

/** Legacy strips drawn before `#app` by modules that predate this bar. */
const LEGACY = ["pb-bar", "pe-bar"];

function adopt(el) {
  if (!el || el === bar || !sub || sub.contains(el)) return;
  if (el.id === "pb-bar") el.id = "pb-userbar";
  sub.appendChild(el);
}

/**
 * Put the bar before `#app` (once) and keep it current. An older `#pb-bar` strip already there - the
 * Phase 4b user bar, mounted by `production/boot.js` before `app.js` runs - is renamed and adopted
 * into the bar's second row, so its links still sit inside `#pb-bar`. Returns the bar.
 */
export function mountChrome(doc = document) {
  const app = doc.getElementById("app");
  if (!app || !app.parentNode) return null;
  const existing = doc.getElementById("pb-bar");
  if (existing && existing.classList.contains("topbar")) return existing;
  bar = doc.createElement("header");
  bar.className = "topbar";
  bar.setAttribute("role", "banner");
  sub = doc.createElement("div");
  sub.className = "tb-sub";
  bar.appendChild(sub);
  if (existing) adopt(existing);
  bar.id = "pb-bar";
  app.parentNode.insertBefore(bar, app);
  for (const id of LEGACY) adopt(doc.getElementById(id));
  // A strip mounted later (the pilot module loads after app.js) is adopted as it arrives.
  const Observer = doc.defaultView && doc.defaultView.MutationObserver;
  if (Observer) {
    new Observer(() => {
      for (const id of LEGACY) {
        const el = doc.getElementById(id);
        if (el && el !== bar && el.parentNode === app.parentNode) adopt(el);
      }
    }).observe(app.parentNode, { childList: true });
  }
  doc.addEventListener("click", onClick);
  doc.addEventListener("keydown", onKey);
  // Tabbing out of an open menu closes it, so a menu never stays open behind the page.
  doc.addEventListener("focusin", (event) => {
    const target = event.target;
    if (ui.menu && target && typeof target.closest === "function" && !target.closest(".menu")) closeMenus();
  });
  injectBarStyles(doc);
  const win = doc.defaultView;
  if (win) {
    lastHash = currentHash();
    win.addEventListener("hashchange", () => {
      const now = currentHash();
      noteRoute(lastHash, now);
      lastHash = now;
      ui.menu = null;
      ui.panel = false;
      refreshChrome();
    });
    win.addEventListener(MODULES_EVENT, refreshChrome);
  }
  lastHtml = "";
  paintChrome();
  return bar;
}
