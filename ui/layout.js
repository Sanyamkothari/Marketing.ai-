/* Which shell the UI draws, and where every old address lives in the new one (Plan M M174, DEC-1604).

   The deployment setting `ui_layout` (`MARKETING_AI_UI_LAYOUT`, engine/settings.py) is "classic" - today's
   top bar, byte for byte - or "campaigns" - the five-place sidebar of Plan M §3 (Overview, Campaigns, Data,
   Reports, Settings), the approvals bell, Help and the profile top right. The API hands it to the browser on
   the call the UI already makes at start-up, `GET /auth/me` (field `ui_layout`), so this module listens to
   the production session and changes nothing until that answer says "campaigns". Before the answer, with
   sign-in refused, or against an API without the field, the layout is "classic".

   Pages read it with `uiLayout()` / `isCampaignLayout()`. A page on a branch that cannot import this file
   may read `document.documentElement.dataset.uiLayout` (set to "campaigns" only in that layout) or listen
   for `LAYOUT_EVENT` on window instead.

   ROUTES is the one table of old and new addresses (Plan M §1 M5: every old hash keeps working). Each row
   says, for a hash pattern, either where it now redirects (`to`) or that it is kept as it is (`to: null`),
   and which of the five places - or the bell - it belongs to (`home`), which the sidebar marks. A test
   lists every route a module registers and fails on one this table does not place.

   This file imports `dom.js` (for the event name) and nothing else statically; it never imports the router,
   so `chrome.js` and `app.js` can both import it without a cycle (DEC-790). The production session is
   loaded on demand, as `app.js` loads the uplift charts: a Phase 1 module never depends on a phase module,
   and without that module (a build before Phase 4b) the layout simply stays "classic". */

import { MODULES_EVENT } from "./dom.js";

export const LAYOUTS = Object.freeze(["classic", "campaigns"]);
export const DEFAULT_LAYOUT = "classic";

/** Fired on window when the layout changes; `event.detail` is the new layout. */
export const LAYOUT_EVENT = "marketing-ai:layout-changed";

/** The five places of the sidebar, in order (Plan M §3). `href` is where each one opens. */
export const PLACES = Object.freeze([
  { id: "overview", label: "Overview", href: "#/overview" },
  { id: "campaigns", label: "Campaigns", href: "#/campaigns" },
  { id: "data", label: "Data", href: "#/data" },
  { id: "reports", label: "Reports", href: "#/reports" },
  { id: "settings", label: "Settings", href: "#/settings" },
]);

/** The bell's own "place": the approvals page belongs to no sidebar entry; the bell is marked instead. */
export const BELL = "approvals";

/*
 * A row may say `notBy: "<module name>"`: a module of that name owning the target's first segment does not make
 * the target a page this build has (see layoutRedirect).
 *
 * Pattern syntax: hash segments after "#/", separated by "/". A literal matches itself, `:id` matches any
 * one segment (and is copied into `to`), and a trailing `*` matches zero or more further segments. The
 * first row that matches wins, so a specific row comes before the general one. "" is `#/` itself.
 */
export const ROUTES = Object.freeze([
  // Overview (M175)
  { from: "", to: "#/overview", home: "overview", why: "Home becomes the Overview" },
  { from: "industry/*", to: "#/overview", home: "overview", why: "the industry chooser is off; its pages were Home" },
  { from: "overview/*", to: null, home: "overview", why: "the Overview itself (M175)" },
  // Campaigns (M176, M177)
  // Plan J's `decide` module still claims `campaigns`, and since M176 its render asks M176's campaignsRoute() first,
  // which draws the Campaigns list at the bare `#/campaigns`: the list exists, so Results redirects (no `notBy`).
  { from: "results/*", to: "#/campaigns", home: "campaigns", why: "Results' runs and campaigns are the Campaigns list" },
  { from: "campaigns", to: null, home: "campaigns", why: "the Campaigns list (M176)" },
  { from: "campaigns/new/*", to: null, home: "campaigns", why: "the New campaign wizard (M177)" },
  { from: "campaigns/:id", to: "#/campaigns/:id/overview", home: "campaigns", why: "a campaign opens on its Overview tab" },
  { from: "campaigns/:id/*", to: null, home: "campaigns", why: "a campaign's tabs (M176)" },
  { from: "campaign/*", to: null, home: "campaigns", why: "a scoring run's campaign results; the campaign page links it" },
  { from: "uc/*", to: null, home: "campaigns", why: "a goal's own pages, reached from the wizard and Technical" },
  { from: "generative/connection/*", to: null, home: "settings", why: "the AI service sign-in stays under Settings" },
  { from: "generative/*", to: null, home: "campaigns", why: "the assistant, root-cause notes and copy belong to a goal" },
  // Data (M179)
  { from: "data/*", to: null, home: "data", why: "the Data page (M179)" },
  { from: "connections", to: "#/data", home: "data", why: "connections are the Data page's sources" },
  { from: "connections/ai/*", to: null, home: "settings", why: "the AI service stays under Settings" },
  { from: "connections/*", to: null, home: "data", why: "a connection's own forms keep their route" },
  // The Data page (M179) has Sources and 'Check my data' (#/data/check), which links to Data Doctor's own page.
  { from: "doctor/*", to: null, home: "data", why: "Data Doctor keeps its own page; the Data page's 'Check my data' links it" },
  // Reports (M178)
  { from: "reports/*", to: null, home: "reports", why: "the Reports page (M178)" },
  { from: "pilot", to: "#/reports", home: "reports", why: "the old Reports hub is the Reports page" },
  { from: "pilot/kit/*", to: null, home: "settings", why: "the data request kit is under Settings, Advanced" },
  { from: "pilot/*", to: null, home: "reports", why: "one report or Proof Pack keeps its address (a link in an e-mail)" },
  { from: "audit", to: "#/reports/check", home: "reports", why: "auditing a campaign is 'Check a past campaign'" },
  { from: "audit/*", to: null, home: "reports", why: "the audit form's own steps" },
  // The bell
  { from: "approvals/*", to: null, home: BELL, why: "opened from the bell" },
  // Settings, Advanced and Admin
  { from: "settings/*", to: null, home: "settings", why: "Settings itself" },
  { from: "cost/*", to: null, home: "settings", why: "Costs, under Settings" },
  { from: "monitoring/*", to: null, home: "settings", why: "model health and schedules, under Settings, Advanced" },
  { from: "uplift/*", to: null, home: "settings", why: "the uplift workbench, under Settings, Advanced" },
  { from: "privacy/*", to: null, home: "settings", why: "Privacy and consent, under Settings" },
  { from: "admin/*", to: null, home: "settings", why: "Users and the audit log, under Settings, Admin" },
  // No place: signing in, one's own account, and the Help menu's pages
  { from: "signin/*", to: null, home: null, why: "the sign-in screen has no sidebar place" },
  { from: "help/*", to: null, home: null, why: "the glossary and what's new (M173), opened from Help (?) top right" },
  { from: "account/*", to: null, home: null, why: "changing one's password, from the profile menu" },
]);

// --- the setting -----------------------------------------------------------------------------------

let layout = DEFAULT_LAYOUT;

// The last redirect made, until its target has been drawn, and the two most recent distinct hashes this module
// was asked about. A page that sends the browser straight back to where it was redirected from, while it is still
// being drawn, is a bounce: the old address is then drawn instead of going round again. Once the target has been
// drawn (`layoutDrawn`), the redirect is forgotten, so a person who follows a link from the new page to the old
// address - a "#/" link on the Overview - is redirected as anyone else is.
const bounce = { from: null, to: null };
const seen = { current: null, previous: null };

function forgetBounce() {
  bounce.from = null;
  bounce.to = null;
}

/** "classic" or "campaigns". */
export const uiLayout = () => layout;

/** True in the campaigns layout (the five-place sidebar). */
export const isCampaignLayout = () => layout === "campaigns";

/**
 * Switch the layout. Anything but "campaigns" is "classic". A change marks `<html data-ui-layout>` (only in
 * the campaigns layout, so the classic page is untouched), and announces itself twice: `LAYOUT_EVENT` for a
 * page that cares, and the modules-changed event, on which `app.js` draws the route again (and so applies
 * its redirect) and `chrome.js` repaints the bar.
 */
export function setUiLayout(value) {
  const next = value === "campaigns" ? "campaigns" : DEFAULT_LAYOUT;
  if (next === layout) return;
  layout = next;
  forgetBounce();
  if (typeof document !== "undefined" && document.documentElement) {
    const root = document.documentElement;
    if (next === "campaigns") root.setAttribute("data-ui-layout", "campaigns");
    else if (root.hasAttribute("data-ui-layout")) root.removeAttribute("data-ui-layout");
  }
  if (typeof window !== "undefined" && typeof window.dispatchEvent === "function") {
    // The window's own constructors, so the events are the page's (a test's jsdom window included).
    window.dispatchEvent(new window.CustomEvent(LAYOUT_EVENT, { detail: next }));
    window.dispatchEvent(new window.Event(MODULES_EVENT));
  }
}

// --- the table -------------------------------------------------------------------------------------

/** A hash ("#/a/b", "#a/b", "a/b") as its segments, without empties. */
export function partsOf(hash) {
  return String(hash || "")
    .replace(/^#?\/?/, "")
    .split("?")[0]
    .split("/")
    .filter(Boolean);
}

function matchRow(row, parts) {
  const pattern = row.from ? row.from.split("/") : [];
  const values = {};
  for (let i = 0; i < pattern.length; i += 1) {
    const seg = pattern[i];
    if (seg === "*" && i === pattern.length - 1) return values;
    if (i >= parts.length) return null;
    if (seg.startsWith(":")) values[seg.slice(1)] = parts[i];
    else if (seg !== parts[i]) return null;
  }
  return parts.length === pattern.length ? values : null;
}

/** The table row for a hash and what its `:id` segments matched, or null for an address it does not know. */
export function routeEntry(hash) {
  const parts = Array.isArray(hash) ? hash : partsOf(hash);
  for (const row of ROUTES) {
    const values = matchRow(row, parts);
    if (values) return { row, values };
  }
  return null;
}

/** The place a hash belongs to in the campaigns layout: one of PLACES' ids, BELL, or null. */
export function homeFor(hash) {
  const hit = routeEntry(hash);
  return hit ? hit.row.home : null;
}

/** Where a hash redirects to in the campaigns layout (whatever the current layout), or null when it is kept. */
export function targetFor(hash) {
  const hit = routeEntry(hash);
  if (!hit || !hit.row.to) return null;
  return hit.row.to.replace(/:([a-z]+)/g, (_, name) => hit.values[name] || "");
}

/**
 * The address to send the browser to instead of `hash`, or null to draw `hash` as it is. Always null in the
 * classic layout. `claimed(parts)` says which screen exists for the target (app.js passes the router's
 * `resolveRoute`, which answers the owning module, `{ name }`, or null): a redirect never sends anyone to a page
 * this build does not have - nor to one owned only by the row's `notBy` module - so the switch can be turned on
 * before every Phase 2 page has landed, and old pages keep working until then.
 */
export function layoutRedirect(hash, { claimed = () => true } = {}) {
  if (!isCampaignLayout()) return null;
  const now = `#/${partsOf(hash).join("/")}`;
  if (now !== seen.current) {
    seen.previous = seen.current;
    seen.current = now;
  }
  // Anywhere but the two ends of the last redirect: that redirect is over.
  if (now !== bounce.from && now !== bounce.to) forgetBounce();
  const hit = routeEntry(now);
  const target = targetFor(now);
  if (!hit || !target || target === now) return null;
  let ready = false;
  try {
    const owner = claimed(partsOf(target));
    ready = Boolean(owner) && !(hit.row.notBy && owner && owner.name === hit.row.notBy);
  } catch {
    ready = false;
  }
  if (!ready) return null;
  // Sent straight back by the target before it was drawn: draw the old address, do not go round again.
  if (bounce.from === now && bounce.to === seen.previous) return null;
  bounce.from = now;
  bounce.to = target;
  return target;
}

/**
 * app.js says a route has been drawn and stayed (the hash did not change while it was drawn). When that is the
 * target of the last redirect, the redirect is over: from here on, reaching the old address again - a link, the
 * wordmark, typing it - is a person's choice and is redirected again.
 */
export function layoutDrawn(hash) {
  if (bounce.to !== null && `#/${partsOf(hash).join("/")}` === bounce.to) forgetBounce();
}

/** Test seam: back to the state of a fresh page. */
export function _resetLayoutForTests() {
  layout = DEFAULT_LAYOUT;
  forgetBounce();
  seen.current = null;
  seen.previous = null;
  if (typeof document !== "undefined" && document.documentElement) document.documentElement.removeAttribute("data-ui-layout");
}

/** Follow the session: `GET /auth/me` carries the setting; a signed-out answer (401) carries nothing and keeps
 * what was known. Resolves once listening, or when the session module cannot be loaded (then: classic). */
export const followSession = import("./modules/production/session.js").then(
  (session) =>
    session.onSession((me) => {
      if (me && typeof me.ui_layout === "string") setUiLayout(me.ui_layout);
    }),
  () => null,
);
