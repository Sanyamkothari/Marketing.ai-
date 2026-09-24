// Phase 4b's screens: sign-in, your own password, the Admin's Users and Audit log (M46/M47), the
// Admin's privacy controls (M48) and monitoring - schedules, alerts, missed runs, outcomes (M49).
//
// The second half of the module. `boot.js` - imported from `ui/modules/router.js`'s Phase 4b block,
// so it runs before `app.js` draws anything - puts the token on every call, draws the user bar and
// gates the other screens' controls. This file registers the routes, and is loaded from the Phase 4b
// `<script>` tag in `ui/index.html`, after `router.js` has finished evaluating; importing it from
// `router.js` instead would call `registerModule` while `router.js` is still mid-evaluation (a
// circular import), before its registry exists.
//
// Five top-level prefixes, each a whole screen family of this module's own:
// * `#/signin[/<return route>]` - the sign-in form (DEC-791);
// * `#/account` - change your own password;
// * `#/admin/users`, `#/admin/audit` - the Admin screens (M46 users, M47 audit viewer, DEC-794);
// * `#/privacy/{consent,erasure,access,retention}` - the DPDP controls, Admin (M48);
// * `#/monitoring/{schedules[/<id>],alerts,missed,runs[/<run_id>]}` - M49, Viewer to read;
// * `#/approvals` - challengers waiting for an Approver, with the head-to-head (Plan D M54).
//
// A data principal's id never appears in any of these routes (DEC-746): the privacy screens take it
// in a form and send it in a request body, so the hash - and so the browser history - never holds it.
//
// Because this file runs after `app.js`'s first `render()`, a page *loaded* on one of these routes
// was first drawn by `app.js` as the overview (no module had claimed the route yet). So once
// registered, and only when the current hash is one of ours, this file asks `app.js` to route again
// by dispatching `hashchange`, and a guard repeats that once if `app.js`'s slower first paint lands
// on top of ours - the overview's `GET /industries` can finish after `GET /auth/me` does.

import { crumbs, errorBox, notFound, pageHead } from "../../dom.js";
import { registerForYou } from "../../overview.js";
import { announceModulesChanged, registerModule, resolveRoute } from "../router.js";
import { getAlerts, getApprovals } from "./api.js";
import { approvalsHtml, bindApprovals, loadApprovals } from "./approvals.js";
import { auditHtml, bindAudit, loadEvents } from "./audit.js";
import { alertsHtml, bindAlerts, loadAlerts, loadMissed, missedHtml } from "./alerts.js";
import { bindRunOutcomes, loadRunOutcomes, loadRuns, runOutcomesHtml, runsHtml } from "./outcomes.js";
import { accessHtml, bindPrivacy, consentHtml, erasureHtml, loadPolicy, loadRegister } from "./privacy.js";
import { bindRetention, loadPlan, retentionHtml } from "./retention.js";
import { bindSchedule, bindSchedules, loadSchedule, loadSchedules, scheduleHtml, schedulesHtml } from "./schedules.js";
import { can, currentMe, ensureMe, goToSignIn, onSession, reasonFor, sessionStatus, SIGNIN_ROUTE } from "./session.js";
import { accountHtml, bindAccount, bindSignIn, ready, signInHtml } from "./signin.js";
import { injectProductionStyles } from "./styles.js";
import { bindUsers, loadUsers, usersHtml } from "./users.js";

injectProductionStyles();

export const ROUTES = [SIGNIN_ROUTE, "account", "admin", "privacy", "monitoring", "approvals"];
const MARK = "data-pb-screen";

function paint(app, html, after) {
  app.innerHTML = html.replace("<main ", `<main ${MARK} `);
  if (after) after();
  window.scrollTo(0, 0);
}

/**
 * A screen of ours that could not be drawn. A route no screen matches (a mistyped link) is one calm
 * card with a way back to Home; anything else leads with a plain sentence and "Try again" (the code
 * and the server's words stay under Details, `errorBox`).
 */
function failure(app, error) {
  if (error && error.code === "NO_SCREEN") {
    paint(
      app,
      `<main class="screen">${pageHead(`${crumbs([{ label: "Not found" }])}<h1 class="h1">Page not found</h1>`)}${notFound(
        "page",
      )}</main>`,
    );
    return;
  }
  paint(
    app,
    `<main class="screen">${pageHead(
      `${crumbs([{ label: "Error" }])}<h1 class="h1">This screen could not be loaded</h1>`,
    )}${errorBox(error, { retry: true })}</main>`,
  );
}

/** The error for a hash under one of our prefixes that names no screen. */
const noScreen = (parts) =>
  Object.assign(new Error(`No screen matches #/${parts.join("/")}`), { code: "NO_SCREEN", status: 404 });

/** A route of ours is still being shown only while the hash still names it: a slow load must not paint over a newer screen. */
const stillOn = (parts) => window.location.hash.replace(/^#\/?/, "").split("/")[0] === parts[0];

async function renderSignIn(app, parts) {
  await ready();
  if (!stillOn(parts)) return;
  const draw = () => paint(app, signInHtml(parts), () => bindSignIn(app, parts, draw));
  draw();
  document.title = "Sign in · Marketing AI";
}

async function renderAccount(app, parts) {
  await ready();
  if (!stillOn(parts)) return;
  const draw = () => paint(app, accountHtml(), () => bindAccount(app, draw));
  draw();
  document.title = "Change password · Marketing AI";
}

async function renderAdmin(app, parts) {
  await ensureMe();
  if (!stillOn(parts)) return;
  if (sessionStatus() === "signed-out") {
    goToSignIn();
    return;
  }
  if (parts[1] === "audit") {
    const draw = () => paint(app, auditHtml(), () => bindAudit(app, draw));
    draw();
    await loadEvents();
    if (stillOn(parts)) draw();
    document.title = "Audit log · Marketing AI";
    return;
  }
  if (!parts[1] || parts[1] === "users") {
    const draw = () => paint(app, usersHtml(), () => bindUsers(app, draw));
    draw();
    await loadUsers();
    if (stillOn(parts)) draw();
    document.title = "Users · Marketing AI";
    return;
  }
  failure(app, noScreen(parts));
}

/**
 * Draw a screen whose content arrives later: paint now (loading), load, paint again - but only while
 * the browser is still on the very hash it was drawn for, so a slow answer never paints over the
 * screen someone has moved on to. Every later repaint (after a click) obeys the same rule.
 */
async function screen(app, html, bind, load, title) {
  const hash = window.location.hash;
  const draw = () => {
    if (window.location.hash === hash) paint(app, html(), () => bind(app, draw));
  };
  draw();
  document.title = `${title} · Marketing AI`;
  await load();
  draw();
}

const PRIVACY_SCREENS = {
  consent: [consentHtml, bindPrivacy, async () => {}, "Consent"],
  erasure: [erasureHtml, bindPrivacy, loadRegister, "Erasure requests"],
  access: [accessHtml, bindPrivacy, async () => {}, "Access requests"],
  retention: [retentionHtml, bindRetention, loadPlan, "Retention"],
};

async function signedInOrAway(parts) {
  await ensureMe();
  if (!stillOn(parts)) return false;
  if (sessionStatus() === "signed-out") {
    goToSignIn();
    return false;
  }
  return true;
}

async function renderPrivacy(app, parts) {
  if (!(await signedInOrAway(parts))) return;
  const entry = PRIVACY_SCREENS[parts[1] || "consent"];
  if (!entry) {
    failure(app, noScreen(parts));
    return;
  }
  const [html, bind, load, title] = entry;
  await screen(app, html, bind, async () => {
    if (reasonFor("GET", "/privacy/purposes")) return; // refused: the screen says why, and asks nothing
    await loadPolicy();
    await load();
  }, title);
}

async function renderApprovals(app, parts) {
  if (!(await signedInOrAway(parts))) return;
  await screen(app, approvalsHtml, bindApprovals, loadApprovals, "Approvals");
}

async function renderMonitoring(app, parts) {
  if (!(await signedInOrAway(parts))) return;
  const [, section, id] = parts;
  if (!section || section === "schedules") {
    if (id) await screen(app, () => scheduleHtml(id), bindSchedule, () => loadSchedule(id), "Schedule");
    else await screen(app, schedulesHtml, bindSchedules, loadSchedules, "Schedules");
  } else if (section === "alerts") {
    await screen(app, alertsHtml, bindAlerts, loadAlerts, "Alerts");
  } else if (section === "missed") {
    await screen(app, missedHtml, () => {}, loadMissed, "Missed runs");
  } else if (section === "runs") {
    if (id) await screen(app, () => runOutcomesHtml(id), bindRunOutcomes, () => loadRunOutcomes(id), "Outcomes");
    else await screen(app, runsHtml, () => {}, loadRuns, "Scoring runs");
  } else {
    failure(app, noScreen(parts));
  }
}

export const productionModule = {
  name: "production",
  routes: ROUTES,
  async render(app, parts) {
    try {
      if (parts[0] === SIGNIN_ROUTE) await renderSignIn(app, parts);
      else if (parts[0] === "account") await renderAccount(app, parts);
      else if (parts[0] === "privacy") await renderPrivacy(app, parts);
      else if (parts[0] === "monitoring") await renderMonitoring(app, parts);
      else if (parts[0] === "approvals") await renderApprovals(app, parts);
      else await renderAdmin(app, parts);
    } catch (error) {
      failure(app, error);
    }
  },
};

registerModule(productionModule);

// --- Home's "For you" line: one next step for the role signed in ---------------------------------
// An Approver reads how many models wait for them, an Analyst how many alerts are open; a Viewer is
// pointed at the campaigns, an Admin who holds no other role at the users. Counted only for a role
// that may act on them, read at most every half minute while Home is shown, and forgotten on a new
// sign-in.

const plural = (n, one, many) => `${n} ${n === 1 ? one : many}`;
const counts = { approvals: null, alerts: null, at: 0, busy: false, who: null };
const COUNT_STALE_MS = 30000;
const mayApprove = () => can("POST", "/models/{model_id}/approve") && can("GET", "/approvals");
const mayAcknowledge = () =>
  can("POST", "/monitoring/alerts/{alert_id}/acknowledge") && can("GET", "/monitoring/alerts");

async function readCounts() {
  const [approvals, alerts] = await Promise.all([
    mayApprove() ? getApprovals().then((body) => (body.items || []).length, () => null) : null,
    mayAcknowledge()
      ? getAlerts({ unacknowledged_only: true }).then((body) => (body.alerts || []).length, () => null)
      : null,
  ]);
  return { approvals, alerts };
}

function refreshCounts() {
  if (counts.busy || Date.now() - counts.at < COUNT_STALE_MS) return;
  counts.busy = true;
  const before = `${counts.approvals}/${counts.alerts}`;
  readCounts()
    .then((next) => Object.assign(counts, next))
    .catch(() => {})
    .finally(() => {
      counts.busy = false;
      counts.at = Date.now();
      if (`${counts.approvals}/${counts.alerts}` !== before) announceModulesChanged();
    });
}

/** The "For you" entries for whoever is signed in (or everyone, with sign-in off). */
export function forYouItems() {
  const status = sessionStatus();
  const me = currentMe();
  if (!me || !["signed-in", "off"].includes(status)) return [];
  refreshCounts();
  const items = [];
  if (mayApprove() && counts.approvals) {
    items.push({ text: `${plural(counts.approvals, "model", "models")} waiting for your approval`, href: "#/approvals" });
  }
  if (mayAcknowledge() && counts.alerts) {
    items.push({ text: `${plural(counts.alerts, "open alert", "open alerts")} on your models`, href: "#/monitoring/alerts" });
  }
  if (items.length || status === "off") return items;
  const roles = me.principal.roles || [];
  if (roles.includes("approver")) return [{ text: "No models are waiting for your approval", href: "#/approvals" }];
  if (roles.includes("analyst")) return [];
  if (roles.includes("admin")) return [{ text: "Manage who can sign in", href: "#/admin/users" }];
  return [{ text: "See the latest campaigns and their results", href: "#/monitoring/runs" }];
}

registerForYou(forYouItems);
onSession((me) => {
  const who = me && me.principal ? me.principal.user_id : null;
  if (who === counts.who) return;
  Object.assign(counts, { approvals: null, alerts: null, at: 0, who });
});

// --- a page loaded directly on one of our routes (see the module comment) -------------------------

const currentParts = () => window.location.hash.replace(/^#\/?/, "").split("/").filter(Boolean);

function ours() {
  const claimed = resolveRoute(currentParts());
  return Boolean(claimed && claimed.name === productionModule.name);
}

export function rerouteIfOurs(win = window) {
  if (!ours()) return false;
  win.dispatchEvent(new win.HashChangeEvent("hashchange"));
  return true;
}

const app = document.getElementById("app");
if (app && ours()) {
  let repairs = 0;
  const guard = new MutationObserver(() => {
    if (!ours() || repairs >= 2) {
      guard.disconnect();
      return;
    }
    if (!app.querySelector(`[${MARK}]`) && app.querySelector("main")) {
      repairs += 1;
      rerouteIfOurs();
    }
  });
  guard.observe(app, { childList: true });
  setTimeout(() => guard.disconnect(), 10000);
  rerouteIfOurs();
}
