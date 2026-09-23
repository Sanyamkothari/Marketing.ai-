// The strip above every screen that says who is signed in and what they hold (Phase 4b M46).
//
// It lives in its own element *before* `#app`, not inside it: every screen - Phase 1's, Phase 3a's -
// paints by replacing `#app`'s `innerHTML`, so anything drawn inside would vanish on the next
// navigation, and drawing it into each screen's header would mean editing each screen. One element
// outside the painted area is on every screen, including ones written after this file.
//
// What it says follows `GET /auth/me` (DEC-791):
// * `auth_mode=off` - "Access control is off": everyone acts as the local operator, who holds every
//   role (DEC-702). Said plainly, in the warning colour, because a deployment that forgot to turn
//   sign-in on should look like it, not like a signed-in Admin.
// * signed in - the display name, every role held (Viewer is implied by any, DEC-703), the Users and
//   Audit log links when `/auth/me` says this person may open them - and, the same way, Privacy (M48,
//   Admin) and Monitoring (M49, anyone who may read schedules) - "Change password" and "Sign out".
// * signed out - a "Sign in" link.
// * a production deployment with sign-in off (`503 AUTH_NOT_CONFIGURED`) - the server's sentence, so
//   the reason every screen below is refusing is on the page once, above them all.
// * no `/auth/me` at all (an API from before Phase 4b) or the API unreachable - nothing, so the page
//   looks exactly as it did before this module existed.

import { esc } from "../../dom.js";
import { postLogout } from "./api.js";
import { can, forgetSignIn, onSession, sessionProblem, signInHash } from "./session.js";

const ROLE_ORDER = ["viewer", "analyst", "approver", "admin"];
export const ROLE_LABEL = { viewer: "Viewer", analyst: "Analyst", approver: "Approver", admin: "Admin" };

/** Roles in a fixed order, so two people with the same roles read the same line. */
export function roleChips(roles) {
  return [...(roles || [])]
    .sort((a, b) => ROLE_ORDER.indexOf(a) - ROLE_ORDER.indexOf(b))
    .map((role) => `<span class="pb-role">${esc(ROLE_LABEL[role] || role)}</span>`)
    .join(" ");
}

function adminLinks(me) {
  const links = [];
  if (can("GET", "/users", me)) links.push(`<a href="#/admin/users">Users</a>`);
  if (can("GET", "/audit/events", me)) links.push(`<a href="#/admin/audit">Audit log</a>`);
  if (can("GET", "/privacy/purposes", me)) links.push(`<a href="#/privacy/consent">Privacy</a>`);
  if (can("GET", "/schedules", me)) links.push(`<a href="#/monitoring/schedules">Monitoring</a>`);
  return links.join("");
}

/** The bar's inner HTML for one `/auth/me` answer and status; empty when there is nothing to say. */
export function userBarHtml(me, status) {
  if (status === "off" && me) {
    return `<div class="pb-bar-in"><span class="pb-off-note">Access control is off</span><span>Everyone acts as the local operator, with every role.</span>${adminLinks(
      me,
    )}</div>`;
  }
  if (status === "signed-in" && me) {
    const who = me.principal.display_name || me.principal.username;
    return `<div class="pb-bar-in"><span>Signed in as <b>${esc(who)}</b></span>${roleChips(
      me.principal.roles,
    )}${adminLinks(me)}<a href="#/account">Change password</a><button type="button" id="pb-signout">Sign out</button></div>`;
  }
  if (status === "misconfigured") {
    return `<div class="pb-bar-in"><span class="pb-off-note">Sign-in is not configured</span><span>${esc(
      sessionProblem() || "This deployment refuses every request until sign-in is turned on.",
    )}</span></div>`;
  }
  if (status === "signed-out") {
    return `<div class="pb-bar-in"><span>Not signed in</span><a href="${esc(signInHash())}">Sign in</a></div>`;
  }
  return "";
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

/** Put the bar before `#app` and keep it current. Returns the bar element. */
export function mountUserBar(doc = document) {
  const app = doc.getElementById("app");
  if (!app || doc.getElementById("pb-bar")) return doc.getElementById("pb-bar");
  const bar = doc.createElement("div");
  bar.id = "pb-bar";
  bar.className = "pb-bar";
  bar.setAttribute("role", "navigation");
  bar.setAttribute("aria-label", "Signed-in user");
  app.parentNode.insertBefore(bar, app);
  bar.addEventListener("click", (event) => {
    if (event.target && event.target.id === "pb-signout") signOut();
  });
  onSession((me, status) => {
    bar.innerHTML = userBarHtml(me, status);
  });
  return bar;
}
