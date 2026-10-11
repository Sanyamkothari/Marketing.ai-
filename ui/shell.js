/* The new shell (Plan M §3, M174, DEC-1604): drawn by `chrome.js` in place of today's top bar when the
   deployment's `ui_layout` is "campaigns" (`ui/layout.js`). Today's bar is untouched in "classic".

     [Marketing AI] [Menu]                                   [sample data] [bell 3] [?] [profile] [Minfy]
     ┌──────────────┐
     │ Overview     │   (the page, in #app)
     │ Campaigns    │
     │ Data         │
     │ Reports      │
     │ Settings     │
     └──────────────┘

   - The left sidebar is the five places of `layout.js` PLACES, the same for every role (as Plan H's four
     were): what a role may DO is still decided by each page and the API. The place marked is the route's
     `home` in layout.js's table, with `aria-current="page"`.
   - Top right: the sample-data chip (slot "demo"), the approvals bell (slot "badge:approvals" - the count
     the classic bar puts on Results, read by `production/approvals.js` for an Approver only, the same role
     rule), Help (?) (slot "help": the tour and "Send feedback", in the menu the bar's other menus use), and
     the profile (slot "user"). The wordmark and the Minfy logo stay.
   - Below 900 px the sidebar folds behind a Menu button (the bar's own `data-tb-menu` toggle, so Escape and
     a route change close it as they close the classic panel).
   - Signed out, the shell is the wordmark, the user slot and the logo, as the classic bar is.

   Like `chrome.js`, this file imports only leaf modules (`dom.js`, `layout.js`); it is markup and styles,
   and `chrome.js` keeps all of the behaviour. */

import { LOGO, esc } from "./dom.js";
import { BELL, PLACES, homeFor } from "./layout.js";

export const SIDEBAR_ID = "sh-side";
export const HELP_ID = "sh-help";
export const STYLE_ID = "sh-styles";

const svg = (body) =>
  `<svg class="sh-ico" viewBox="0 0 24 24" width="18" height="18" aria-hidden="true" focusable="false" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round">${body}</svg>`;

const ICONS = {
  overview: svg('<path d="M3 11l9-7 9 7"/><path d="M5 10v10h14V10"/><path d="M10 20v-6h4v6"/>'),
  campaigns: svg('<path d="M3 10v4h3l7 4V6L6 10H3z"/><path d="M16 9a4 4 0 0 1 0 6"/><path d="M19 6a8 8 0 0 1 0 12"/>'),
  data: svg('<ellipse cx="12" cy="5.5" rx="7" ry="2.5"/><path d="M5 5.5v13c0 1.4 3.1 2.5 7 2.5s7-1.1 7-2.5v-13"/><path d="M5 12c0 1.4 3.1 2.5 7 2.5s7-1.1 7-2.5"/>'),
  reports: svg('<path d="M4 20V10"/><path d="M10 20V4"/><path d="M16 20v-7"/><path d="M22 20H2"/>'),
  settings: svg(
    '<circle cx="12" cy="12" r="3"/><path d="M19.4 15a1.7 1.7 0 0 0 .3 1.8l.1.1a2 2 0 1 1-2.8 2.8l-.1-.1a1.7 1.7 0 0 0-1.8-.3 1.7 1.7 0 0 0-1 1.5V21a2 2 0 1 1-4 0v-.1a1.7 1.7 0 0 0-1.1-1.5 1.7 1.7 0 0 0-1.8.3l-.1.1a2 2 0 1 1-2.8-2.8l.1-.1a1.7 1.7 0 0 0 .3-1.8 1.7 1.7 0 0 0-1.5-1H3a2 2 0 1 1 0-4h.1a1.7 1.7 0 0 0 1.5-1.1 1.7 1.7 0 0 0-.3-1.8l-.1-.1a2 2 0 1 1 2.8-2.8l.1.1a1.7 1.7 0 0 0 1.8.3H9a1.7 1.7 0 0 0 1-1.5V3a2 2 0 1 1 4 0v.1a1.7 1.7 0 0 0 1 1.5 1.7 1.7 0 0 0 1.8-.3l.1-.1a2 2 0 1 1 2.8 2.8l-.1.1a1.7 1.7 0 0 0-.3 1.8V9a1.7 1.7 0 0 0 1.5 1H21a2 2 0 1 1 0 4h-.1a1.7 1.7 0 0 0-1.5 1z"/>',
  ),
  bell: svg('<path d="M6 8a6 6 0 1 1 12 0c0 7 3 9 3 9H3s3-2 3-9"/><path d="M10.3 21a1.9 1.9 0 0 0 3.4 0"/>'),
};

/** The count a badge slot holds ("" or "0" for none). */
function countOf(raw) {
  const count = String(raw || "").trim();
  return count && count !== "0" ? count : "";
}

/** The sidebar: the five places, the route's own marked. */
export function sidebarHtml(here) {
  const items = PLACES.map((place) => {
    const on = place.id === here;
    return `<li><a class="sh-item${on ? " on" : ""}" href="${esc(place.href)}" data-place="${esc(place.id)}"${
      on ? ` aria-current="page"` : ""
    }>${ICONS[place.id] || ""}<span>${esc(place.label)}</span></a></li>`;
  }).join("");
  return `<nav class="sh-side" id="${SIDEBAR_ID}" aria-label="Main"><ul>${items}</ul></nav>`;
}

/** The approvals bell: always drawn for a signed-in person (anyone may look), the count only for an Approver. */
export function bellHtml(rawCount, on) {
  const count = countOf(rawCount);
  const label = count ? `Waiting for approval: ${count}` : "Waiting for approval";
  return `<a class="sh-icon sh-bell${on ? " on" : ""}" href="#/approvals" data-bell aria-label="${esc(label)}" title="Waiting for approval"${
    on ? ` aria-current="page"` : ""
  }>${ICONS.bell}${count ? `<span class="count" aria-hidden="true">${esc(count)}</span>` : ""}</a>`;
}

/** Help (?): the registered "help" entries in a menu, or nothing when no module registered any. */
export function helpHtml(entries, open) {
  if (!String(entries || "").trim()) return "";
  return `<div class="sh-help"><button type="button" class="sh-icon" data-menu="${HELP_ID}" aria-expanded="${open}" aria-controls="${HELP_ID}" aria-label="Help" title="Help"><span aria-hidden="true">?</span></button><div class="menu-pop right" id="${HELP_ID}"${
    open ? "" : " hidden"
  }>${entries}</div></div>`;
}

/**
 * The shell's bar, as one element (chrome.js swaps its `.tb-row` for it). `ctx` is what the bar knows:
 * `{ hash, status, slot(name), context, menu, panel }` - the route, the session state ("signed-out" draws
 * no places), a slot's markup, the header tool's markup, the open menu's id and whether the folded sidebar
 * is open.
 */
export function shellBarHtml(ctx) {
  const signedOut = ctx.status === "signed-out";
  const here = homeFor(ctx.hash);
  const user = ctx.slot("user");
  if (signedOut) {
    return `<div class="tb-row sh-row"><a class="wordmark" href="#/">Marketing AI</a><div class="tb-context"></div>${
      user ? `<div class="sh-utils">${user}</div>` : ""
    }<a class="tb-logo" href="#/" aria-label="Minfy, home">${LOGO}</a></div>`;
  }
  const menuBtn = `<button type="button" class="btn secondary sm sh-menu-btn" data-tb-menu aria-expanded="${Boolean(
    ctx.panel,
  )}" aria-controls="${SIDEBAR_ID}">Menu</button>`;
  const utils = `${ctx.slot("demo")}${bellHtml(ctx.slot("badge:approvals"), here === BELL)}${helpHtml(
    ctx.slot("help"),
    ctx.menu === HELP_ID,
  )}${user}`;
  return `<div class="tb-row sh-row"><a class="wordmark" href="#/">Marketing AI</a>${menuBtn}${sidebarHtml(
    here,
  )}<div class="tb-context">${ctx.context || ""}</div><div class="sh-utils">${utils}</div><a class="tb-logo" href="#/" aria-label="Minfy, home">${LOGO}</a></div>`;
}

// Tokens only (index.html's :root), so both themes apply. The sidebar sits under the sticky bar, fixed to
// the left; #app makes room for it. Below 900 px it is a panel the Menu button opens.
export const SHELL_CSS = `
.topbar.sh-on .sh-row{max-width:none;padding:0 16px 0 20px}
.sh-utils{display:flex;align-items:center;gap:6px}
.sh-icon{position:relative;display:inline-grid;place-items:center;width:36px;height:36px;padding:0;border:0;border-radius:8px;background:none;color:var(--ink2);font:inherit;font-size:15px;font-weight:600;cursor:pointer}
.sh-icon:hover,.sh-icon.on{background:var(--soft);color:var(--ink)}
.sh-icon.on{box-shadow:inset 0 -2px 0 var(--brand-blue)}
.sh-bell .count{position:absolute;top:-2px;right:-4px;min-width:18px;height:18px;padding:0 5px;font-size:11px}
.sh-help{position:relative}
.sh-menu-btn{display:none}
.sh-side{position:fixed;top:57px;left:0;bottom:0;z-index:40;width:216px;padding:16px 12px;overflow-y:auto;background:var(--surface);border-right:1px solid var(--line)}
.sh-side>ul{display:flex;flex-direction:column;gap:4px;margin:0;padding:0;list-style:none}
.sh-item{display:flex;align-items:center;gap:12px;height:40px;padding:0 12px;border-radius:8px;color:var(--ink2);font-size:14px;font-weight:500}
.sh-item:hover{background:var(--soft);color:var(--ink)}
.sh-item.on{background:var(--p-t);color:var(--brand-blue);font-weight:600;box-shadow:inset 3px 0 0 var(--brand-blue)}
.sh-item:focus-visible,.sh-icon:focus-visible{outline:2px solid var(--brand-blue);outline-offset:-2px}
.sh-ico{flex:none}
.topbar.sh-side-on~#app{margin-left:216px;padding:0 24px}
@media (max-width:900px){
  .sh-menu-btn{display:inline-flex}
  .sh-side{display:none}
  .topbar.open .sh-side{display:block;top:57px;right:0;bottom:auto;width:auto;max-height:calc(100vh - 57px);border-right:0;border-bottom:1px solid var(--line);box-shadow:0 12px 24px rgba(17,24,39,.12)}
  .topbar.sh-side-on~#app{margin-left:0;padding:0}
}
@media (max-width:600px){
  .topbar.sh-on .sh-row{padding:0 12px;gap:6px}
  .topbar.sh-on .tb-logo{margin-left:0}
  .topbar.sh-on .tb-logo .logo{width:72px;height:20px}
  .sh-utils{gap:2px}
}
`;

/** Put the shell's styles in the page once. Only called in the campaigns layout. */
export function injectShellStyles(doc = document) {
  if (!doc || doc.getElementById(STYLE_ID)) return;
  const style = doc.createElement("style");
  style.id = STYLE_ID;
  style.textContent = SHELL_CSS;
  (doc.head || doc.documentElement).appendChild(style);
}
