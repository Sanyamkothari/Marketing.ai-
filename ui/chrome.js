// The top bar: one role-aware bar above every screen (v1, docs/ui/FOUNDATION.md).
//
//   [Marketing AI]  Home  Build data  Models ▾  Campaigns  Reports  Admin ▾     [Client ▾] [chip] [Help ▾] [user]
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
//   "models"   extra entries (`<a>` elements) at the end of the Models menu
//   "admin"    the Admin menu's entries; the Admin item is drawn only when this slot has any
//   "demo"     the sample-data chip, right side
//   "help"     the Help menu's entries; Help is drawn only when this slot has any
//   "user"     the user menu (or "Sign in", or the sign-in-off chip), far right
//   "badge:approvals"  a count shown on Models and on "Waiting for approval" ("" for none)
// The context slot (the client picker) is the registered header tool, `headerToolHtml()`.
//
// Until a module fills "user", an older strip mounted before `#app` (Phase 4b's `#pb-bar` user bar,
// Plan E's `#pe-bar`) is adopted into the bar's second row, so nothing it offered is lost.

import { MODULES_EVENT, esc, headerToolHtml } from "./dom.js";

const slots = new Map();
let access = null;
let active = { id: null, hash: null };
const ui = { menu: null, panel: false };
let bar = null;
let sub = null;
let lastHtml = "";
let scheduled = false;

/** The six goals. `need` is the `[method, path]` a role must be allowed (via `registerAccess`). */
export const NAV_ITEMS = [
  { id: "home", label: "Home", href: "#/" },
  { id: "build", label: "Build data", href: "#/pilot/kit" },
  {
    id: "models",
    label: "Models",
    menu: [
      { label: "Train or score: choose a use case on Home", href: "#/" },
      { label: "Uplift models", href: "#/uplift" },
      { label: "Waiting for approval", href: "#/approvals", need: ["GET", "/approvals"], badge: "badge:approvals" },
      { label: "Model health", href: "#/monitoring/alerts", need: ["GET", "/schedules"] },
    ],
    slot: "models",
  },
  { id: "campaigns", label: "Campaigns", href: "#/monitoring/runs" },
  { id: "reports", label: "Reports", href: "#/pilot" },
  { id: "admin", label: "Admin", menu: [], slot: "admin" },
];

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

/** Mark a goal active for the current route only (a scoring run's Output page is a Campaign). */
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

/** The goal a route belongs to (the plan's navigation table), or `null` (sign-in, account). */
export function navFor(hash) {
  const parts = String(hash || "").replace(/^#\/?/, "").split("/").filter(Boolean);
  const [a, b] = parts;
  if (!a || a === "industry") return "home";
  if (a === "pilot") {
    if (b === "kit" || (b === "view" && parts[2] === "readiness")) return "build";
    if (b === "value") return "campaigns";
    return "reports";
  }
  if (a === "campaign") return "campaigns";
  if (a === "monitoring") return b === "runs" ? "campaigns" : "models";
  if (a === "uc" || a === "uplift" || a === "approvals") return "models";
  if (a === "generative") return b === "copy" ? "campaigns" : b === "connection" ? "admin" : "models";
  if (a === "admin" || a === "privacy") return "admin";
  return null;
}

function activeId() {
  const hash = currentHash();
  return active.hash === hash && active.id ? active.id : navFor(hash);
}

// --- drawing -----------------------------------------------------------------------------------------

function allowed(need) {
  if (!need || !access || typeof access.can !== "function") return true;
  try {
    return access.can(need[0], need[1]) !== false;
  } catch {
    return true;
  }
}

function sessionState() {
  try {
    return access && typeof access.status === "function" ? access.status() : null;
  } catch {
    return null;
  }
}

function slotHtml(name) {
  return (slots.get(name) || [])
    .map((slot) => {
      try {
        return slot.html() || "";
      } catch {
        return "";
      }
    })
    .join("");
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
  const count = item.id === "models" ? badge("badge:approvals") : "";
  return `<li class="menu"><button type="button" class="tn-item${on ? " on" : ""}" data-menu="${menuId}" aria-expanded="${open}" aria-controls="${menuId}"${
    on ? ` aria-current="true"` : ""
  }>${esc(item.label)}${count}${chev}</button><div class="menu-pop" id="${menuId}"${open ? "" : " hidden"}>${entries}${extra}</div></li>`;
}

/** The bar's markup for the current route, slots and access (exported for tests). */
export function topBarHtml() {
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
        )}</a></li>`;
      }).join("");
  const nav = items ? `<nav class="topnav tb-c" aria-label="Main"><ul>${items}</ul></nav>` : "";
  const context = signedOut ? "" : headerToolHtml();
  const help = signedOut ? "" : slotHtml("help");
  const helpOpen = ui.menu === "tn-help";
  const helpMenu = help
    ? `<div class="menu"><button type="button" class="tn-item" data-menu="tn-help" aria-expanded="${helpOpen}" aria-controls="tn-help">Help${chev}</button><div class="menu-pop right" id="tn-help"${
        helpOpen ? "" : " hidden"
      }>${help}</div></div>`
    : "";
  const utils = `${signedOut ? "" : slotHtml("demo")}${helpMenu}${slotHtml("user")}`;
  return `<div class="tb-row"><a class="wordmark" href="#/">Marketing AI</a>${nav}<div class="tb-context">${context}</div>${
    utils ? `<div class="tb-utils tb-c">${utils}</div>` : ""
  }<button type="button" class="btn secondary sm tb-menu-btn" data-tb-menu aria-expanded="${ui.panel}" aria-controls="pb-bar">Menu</button></div>`;
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
    if (row) row.replaceWith(holder.firstElementChild);
    else bar.insertBefore(holder.firstElementChild, bar.firstChild);
  }
  bar.classList.toggle("open", ui.panel);
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
  // A click anywhere else, or on a link inside a menu, closes what was open.
  if (ui.menu && !(target.closest(".menu-pop") && !target.closest("a"))) closeMenus();
  if (!target.closest(".toggletip")) closeToggletips(doc);
}

function onKey(event) {
  if (event.key !== "Escape") return;
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
  const win = doc.defaultView;
  if (win) {
    win.addEventListener("hashchange", () => {
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
