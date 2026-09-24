// Who is signed in, in the one top bar (Phase 4b M46; v1 docs/ui/FOUNDATION.md).
//
// Since v1 there is no strip of its own: this file fills three slots of the top bar (`ui/chrome.js`)
// and tells it who may see what.
//
// * "user" - the far right of the bar. What it says follows `GET /auth/me` (DEC-791):
//   - signed in: the display name and one role label (the highest held; Viewer is implied by any role,
//     DEC-703, so it is never shown next to another), opening a menu with "Signed in as ...", "Change
//     password" and, set apart at the bottom, "Sign out" (`#pb-signout`);
//   - `auth_mode=off`: a quiet "Sign-in off" chip. Its toggletip says "Access control is off: everyone
//     has every role." (DEC-702) - neutral, not a warning, because it is how a pilot is meant to run;
//   - a production deployment with sign-in off (`503 AUTH_NOT_CONFIGURED`): "Sign-in is not configured"
//     as the one warning, with the server's sentence behind it;
//   - signed out: "Sign in" (on the sign-in screen itself, which it would link to, "Not signed in");
//   - no `/auth/me` at all (an API from before Phase 4b) or the API unreachable: nothing.
// * "admin" - the Admin menu's entries: Users, Audit log, Privacy, Export feedback. The whole menu is
//   there only for whoever may open the user list (an Admin, or everyone with sign-in off); each entry
//   is also checked on its own, the way the old strip checked its links.
// * "models" - "Schedules", after the Models menu's "Model health", for whoever may read schedules.
//
// The access provider (`setAccess`) is the session's own `can`, so the bar hides what the server
// would refuse (Waiting for approval, Model health, Admin) from the same permission list the screens
// gate their buttons with. This file is reached from `boot.js`, which `modules/router.js` imports while
// it is still evaluating, so it talks to `chrome.js` directly: `chrome.js` imports `dom.js` only, and
// the boot graph never reaches the router (DEC-790).

import { API_BASE } from "../../api.js";
import { addNavSlot, refreshChrome, setAccess } from "../../chrome.js";
import { esc } from "../../dom.js";
import { postLogout } from "./api.js";
import {
  can,
  currentMe,
  currentRoute,
  forgetSignIn,
  onSession,
  sessionProblem,
  sessionStatus,
  signInHash,
  SIGNIN_ROUTE,
} from "./session.js";

const ROLE_ORDER = ["viewer", "analyst", "approver", "admin"];
export const ROLE_LABEL = { viewer: "Viewer", analyst: "Analyst", approver: "Approver", admin: "Admin" };

export const OFF_NOTE = "Access control is off: everyone has every role. An administrator turns sign-in on.";

/** Roles in a fixed order, so two people with the same roles read the same line. */
export function roleChips(roles) {
  return [...(roles || [])]
    .sort((a, b) => ROLE_ORDER.indexOf(a) - ROLE_ORDER.indexOf(b))
    .map((role) => `<span class="pb-role">${esc(ROLE_LABEL[role] || role)}</span>`)
    .join(" ");
}

/** The one role label a person is shown under: the highest they hold (Viewer only when that is all). */
export function mainRole(roles) {
  const held = [...(roles || [])].filter((role) => ROLE_ORDER.includes(role));
  if (!held.length) return (roles || [])[0] ? String(roles[0]) : "";
  const top = held.sort((a, b) => ROLE_ORDER.indexOf(b) - ROLE_ORDER.indexOf(a))[0];
  return ROLE_LABEL[top];
}

const chev = `<span class="chev" aria-hidden="true"></span>`;

/** A chip that opens one short explanation (the chrome's toggletip behaviour, `[data-toggletip]`). */
const chipTip = (cls, label, text) =>
  `<span class="toggletip um-tip"><button type="button" class="${cls}" data-toggletip aria-expanded="false">${esc(
    label,
  )}</button><span class="tt-pop right" role="status" hidden>${esc(text)}</span></span>`;

const onSignIn = () => currentRoute().split("/")[0] === SIGNIN_ROUTE;

/** The user slot's markup for one `/auth/me` answer and status; empty when there is nothing to say. */
export function userBarHtml(me, status) {
  if (status === "off" && me) return chipTip("chip neutral um-chip", "Sign-in off", OFF_NOTE);
  if (status === "signed-in" && me) {
    const who = me.principal.display_name || me.principal.username;
    const role = mainRole(me.principal.roles);
    const account =
      me.principal.kind === "user" || me.principal.kind === undefined
        ? `<a href="#/account">Change password</a>`
        : "";
    return `<div class="menu um"><button type="button" class="tn-item um-btn" data-menu="tn-user" aria-expanded="false" aria-controls="tn-user"><span class="um-name">${esc(
      who,
    )}</span>${role ? `<span class="um-role">${esc(role)}</span>` : ""}${chev}</button><div class="menu-pop right" id="tn-user" hidden><div class="mh">Signed in as <b>${esc(
      who,
    )}</b>${role ? ` · ${esc(role)}` : ""}</div>${account}<div class="msep"></div><button type="button" id="pb-signout">Sign out</button></div></div>`;
  }
  if (status === "misconfigured") {
    return chipTip(
      "pill warn um-chip",
      "Sign-in is not configured",
      sessionProblem() || "This deployment refuses every request until sign-in is turned on.",
    );
  }
  if (status === "signed-out") {
    if (onSignIn()) return '<span class="um-state">Not signed in</span>';
    return `<a class="btn secondary sm" href="${esc(signInHash())}"><span class="sr">Not signed in. </span>Sign in</a>`;
  }
  return "";
}

/** Somebody the permission list describes: signed in, or everyone when sign-in is off. */
const known = () => ["off", "signed-in"].includes(sessionStatus());

/** The Admin menu's entries; none (so no Admin menu) for whoever may not open the user list. */
export function adminMenuHtml() {
  if (!known() || !can("GET", "/users")) return "";
  const links = [`<a href="#/admin/users">Users</a>`];
  if (can("GET", "/audit/events")) links.push(`<a href="#/admin/audit">Audit log</a>`);
  if (can("GET", "/privacy/purposes")) links.push(`<a href="#/privacy/consent">Privacy</a>`);
  if (can("GET", "/pilot/feedback/export")) {
    links.push(`<a href="${esc(`${API_BASE}/pilot/feedback/export`)}" download>Export feedback</a>`);
  }
  return links.join("");
}

/** Extra Models entries: the schedules list, beside "Model health", for whoever may read it. */
export function modelsMenuHtml() {
  return known() && can("GET", "/schedules") ? `<a href="#/monitoring/schedules">Schedules</a>` : "";
}

/** Ask the API to revoke this sign-in, forget it here whatever the answer, and show the sign-in form. */
export async function signOut() {
  try {
    await postLogout();
  } catch {
    // an already-lapsed session answers 401; the browser forgets it either way
  }
  forgetSignIn();
  window.location.hash = "#/signin";
}

let mounted = false;

/**
 * Fill the top bar's user, Admin and Models slots, give it the session's `can`, and redraw it whenever
 * the session changes. Once per page; `doc` is where the delegated "Sign out" click is heard.
 */
export function mountUserBar(doc = document) {
  if (mounted) return;
  mounted = true;
  setAccess({ can: (method, path) => can(method, path), status: () => sessionStatus() });
  injectUserStyles(doc);
  addNavSlot("user", { html: () => userBarHtml(currentMe(), sessionStatus()) });
  addNavSlot("admin", { html: adminMenuHtml });
  addNavSlot("models", { html: modelsMenuHtml });
  doc.addEventListener("click", (event) => {
    const target = event.target;
    if (target && typeof target.closest === "function" && target.closest("#pb-signout")) signOut();
  });
  onSession(() => refreshChrome());
}

// The few rules the user menu, the chips and the sign-in card need beyond the shared components in
// `index.html`: tokens only, so both themes apply with no dark block of their own.
const CSS = `
button.um-chip{font:inherit;font-size:12px;font-weight:500;min-height:24px;cursor:pointer}
button.pill.um-chip{border:0}
.um-btn .um-name{max-width:180px;overflow:hidden;text-overflow:ellipsis}
.um-state{font-size:12px;color:var(--muted)}
.um-btn .um-role{font-size:12px;font-weight:500;color:var(--muted)}
.menu-pop .mh b{color:var(--ink);font-weight:600}
.pb-center{max-width:440px;margin:8px auto 0}
.pb-center .desc{margin-top:8px}
.pb-center .card{margin-top:24px}
.pb-center .pb-form{max-width:none}
.pb-center .control{height:40px}
.pb-center .control input{width:100%;height:100%;border:0;background:transparent;color:inherit;font:inherit;font-size:14px;padding:0 12px}
.pb-center .field-err{margin:0;font-size:12px;color:var(--bad)}
.pb-center .field-ok{margin:0;font-size:12px;color:var(--ok)}
.pb-center .field-help{margin:0;font-size:12px;color:var(--muted)}
.pb-center .actions{border-top:0;padding-top:4px;margin-top:0}
.pb-center .actions .btn{width:100%}
.pb-center details.tech{margin-top:16px}
`;

function injectUserStyles(doc) {
  if (!doc.head || doc.getElementById("pb-user-styles")) return;
  const style = doc.createElement("style");
  style.id = "pb-user-styles";
  style.textContent = CSS;
  doc.head.appendChild(style);
}
