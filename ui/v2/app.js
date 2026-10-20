// The Autonomy shell: the top bar (name, goal switcher, inbox, one-click pause), the nav for the five screens
// plus Operator mode, and a hash router that loads each screen as an ES module.
//
// Every screen module exports `render(root, ctx)`, where ctx = {api, goalId, navigate, toast} (plus `goal`, the
// current Goal object or null, `refreshGoals()`, for screens that change a goal, and `pauseInTopBar`, which tells
// a screen that the top bar's Pause button is the one pause control). A screen that fails to load shows a
// placeholder instead of breaking the app, so screens can be built and shipped one at a time.
//
// When the current goal is at "approve each batch", the top bar also shows how many batches wait for approval;
// it links to the Live screen, where they are approved or rejected.

import { createApi } from "./api.js";
import { button, clear, el, emptyState, errorBox, humanize, loading, toast } from "./components.js";

/** The screens, in the order a marketer meets them. `goal: true` means the address carries a goal id. */
export const SCREENS = Object.freeze([
  { id: "connect", label: "Connect", goal: false, load: () => import("./screens/connect.js") },
  { id: "goal", label: "Goal", goal: false, load: () => import("./screens/goal.js") },
  { id: "plan", label: "Plan", goal: true, load: () => import("./screens/plan.js") },
  { id: "live", label: "Live", goal: true, load: () => import("./screens/live.js") },
  { id: "proof", label: "Proof", goal: true, load: () => import("./screens/proof.js") },
]);
export const OPERATOR = Object.freeze({ id: "operator", label: "Operator mode", goal: false, load: () => import("./screens/operator.js") });
const ALL = [...SCREENS, OPERATOR];
const DEFAULT_ROUTE = "#/goal";
const REMEMBER_KEY = "autonomy.v2.goal";

/** "#/plan/goal_1" -> {screen: "plan", goalId: "goal_1"}. Unknown screens keep their name so we can say so. */
export function parseHash(hash) {
  const clean = String(hash || "").replace(/^#\/?/, "").split("?")[0];
  const [screen = "", ...rest] = clean.split("/");
  let goalId = rest.join("/") || null;
  if (goalId) {
    try {
      goalId = decodeURIComponent(goalId);
    } catch {
      /* keep it as typed */
    }
  }
  return { screen: screen || null, goalId };
}

/** The address of a screen, with the goal id when that screen takes one. */
export function hashFor(screenId, goalId) {
  const screen = ALL.find((s) => s.id === screenId);
  if (screen && (screen.goal || screen === OPERATOR) && goalId) return `#/${screenId}/${encodeURIComponent(goalId)}`;
  return `#/${screenId}`;
}

const api = createApi();
const shell = {
  goals: [],
  goalsLoaded: false,
  goalId: null,
  seq: 0,
  modules: new Map(),
  firstRender: true,
  badgeSeq: 0,
};

const $ = (id) => document.getElementById(id);

function remember(goalId) {
  try {
    if (goalId) localStorage.setItem(REMEMBER_KEY, goalId);
  } catch {
    /* private mode: nothing to remember */
  }
}

function remembered() {
  try {
    return localStorage.getItem(REMEMBER_KEY);
  } catch {
    return null;
  }
}

const currentGoal = () => shell.goals.find((g) => g.goal_id === shell.goalId) || null;

/** The goal to show when the address names none: the last one used, else the first running one, else the first. */
function defaultGoalId() {
  const last = remembered();
  if (last && shell.goals.some((g) => g.goal_id === last)) return last;
  const running = shell.goals.find((g) => g.status === "active") || shell.goals.find((g) => g.status === "paused");
  return (running || shell.goals[0] || {}).goal_id || null;
}

/** Go to a screen. Takes "#/plan/x", "/plan/x", "plan/x" or ("plan", goalId). */
export function navigate(target, goalId) {
  let hash;
  if (goalId !== undefined || ALL.some((s) => s.id === target)) hash = hashFor(target, goalId ?? shell.goalId);
  else hash = `#/${String(target).replace(/^#?\/?/, "")}`;
  if (location.hash === hash) route();
  else location.hash = hash;
}

// ---- the top bar ------------------------------------------------------------------------------------------
function goalLabel(g) {
  const text = g.text && g.text.length > 48 ? `${g.text.slice(0, 47)}…` : g.text || g.goal_id;
  return `${text} (${humanize(g.status).toLowerCase()})`;
}

function drawSwitcher() {
  const select = $("goal-select");
  if (!shell.goalsLoaded) return;
  if (!shell.goals.length) {
    clear(select, el("option", { value: "" }, "No goals yet"));
    select.disabled = true;
    return;
  }
  const known = shell.goals.some((g) => g.goal_id === shell.goalId);
  clear(
    select,
    shell.goalId ? null : el("option", { value: "" }, "Choose a goal"),
    shell.goalId && !known ? el("option", { value: shell.goalId }, `${shell.goalId} (not in your list)`) : null,
    shell.goals.map((g) => el("option", { value: g.goal_id, selected: g.goal_id === shell.goalId }, goalLabel(g))),
  );
  select.value = shell.goalId || "";
  select.disabled = false;
}

function drawPause() {
  const b = $("pause-button");
  const g = currentGoal();
  b.classList.remove("btn-danger", "btn-primary");
  if (g && g.status === "active") {
    b.textContent = "Pause";
    b.classList.add("btn-danger");
    b.disabled = false;
    b.setAttribute("aria-label", "Pause this goal: stop all sends now");
    b.title = "Stops every send for this goal at once. You can resume any time.";
  } else if (g && g.status === "paused") {
    b.textContent = "Resume";
    b.classList.add("btn-primary");
    b.disabled = false;
    b.setAttribute("aria-label", "Resume this goal");
    b.title = "Lets the agent act again inside the approved plan.";
  } else {
    b.textContent = "Pause";
    b.classList.add("btn-danger");
    b.disabled = true;
    b.removeAttribute("aria-label");
    b.title = g ? "Nothing is running for this goal yet." : "Choose a goal first.";
  }
}

function drawNav(activeId) {
  clear(
    $("nav-list"),
    SCREENS.map((s, i) => {
      const current = s.id === activeId;
      return el(
        "li",
        {},
        el(
          "a",
          { href: hashFor(s.id, shell.goalId), class: ["nav-link", current && "is-current"], "aria-current": current ? "page" : null },
          el("span", { class: "nav-step", "aria-hidden": "true" }, String(i + 1)),
          s.label,
        ),
      );
    }),
  );
  const op = $("operator-link");
  op.setAttribute("href", hashFor(OPERATOR.id, shell.goalId));
  op.classList.toggle("is-current", activeId === OPERATOR.id);
  if (activeId === OPERATOR.id) op.setAttribute("aria-current", "page");
  else op.removeAttribute("aria-current");
  $("inbox-link").setAttribute("href", hashFor("live", shell.goalId));
}

function drawBanner() {
  const banner = $("mode-banner");
  if (!api.isMock()) {
    banner.hidden = true;
    return;
  }
  clear(
    banner,
    el("strong", {}, "Sample data. "),
    `${api.reason} Nothing you do here reaches a customer, and changes last until you reload.`,
  );
  banner.hidden = false;
}

function drawTopbar(activeId) {
  drawSwitcher();
  drawPause();
  drawNav(activeId);
  drawBanner();
}

async function loadGoals() {
  try {
    const goals = await api.listGoals();
    shell.goals = Array.isArray(goals) ? goals : [];
  } catch (err) {
    shell.goals = [];
    toast(`Could not load your goals. ${err.message || ""}`.trim(), "critical");
  }
  shell.goalsLoaded = true;
}

async function loadInbox() {
  try {
    const alerts = await api.listAlerts();
    // Count what the Live inbox shows for this goal: its own alerts and the account-wide ones, not other goals'.
    const open = (Array.isArray(alerts) ? alerts : []).filter(
      (a) => a.severity !== "info" && (!a.goal_id || !shell.goalId || a.goal_id === shell.goalId),
    );
    const count = $("inbox-count");
    count.hidden = open.length === 0;
    count.textContent = String(open.length);
    $("inbox-link").setAttribute("aria-label", open.length ? `Alerts inbox, ${open.length} need a look` : "Alerts inbox, nothing urgent");
  } catch {
    /* the inbox count is a hint; the Live screen shows the inbox itself */
  }
}

/** The "to approve" link in the top bar, made once next to the inbox (index.html stays as it is). */
function batchLink() {
  const existing = $("batch-link");
  if (existing) return existing;
  const inbox = $("inbox-link");
  if (!inbox || typeof inbox.before !== "function") return null;
  const link = el(
    "a",
    { id: "batch-link", class: "inbox-link batch-link", href: "#/live" },
    // `wordmark-text` is the class styles.css hides on a phone, so there the badge is only its count and the
    // goal switcher keeps its room; the link's aria-label and title still say what it is.
    el("span", { class: "wordmark-text", "aria-hidden": "true" }, "To approve"),
    // The brand colour, not the inbox's red: a batch waiting is a task, not an alarm.
    el("span", { id: "batch-count", class: "count", style: { background: "var(--brand)", color: "var(--brand-ink)" } }),
  );
  showBadge(link, false);
  inbox.before(link);
  return link;
}

/** `.inbox-link` sets `display`, which beats the `hidden` attribute, so set both. */
function showBadge(link, on) {
  link.hidden = !on;
  link.style.display = on ? "" : "none";
}

/**
 * Show how many batches wait for approval, for the current goal at "approve each batch". Hidden otherwise, and
 * hidden too when the API has no batch route yet (listBatches answers null) or the call fails: it is a hint, and
 * the Live screen says what is going on.
 */
async function loadBatchBadge() {
  const link = batchLink();
  if (!link) return;
  const seq = ++shell.badgeSeq;
  const g = currentGoal();
  if (!g || g.autonomy !== "approve" || typeof api.listBatches !== "function") {
    showBadge(link, false);
    return;
  }
  let rows = null;
  try {
    rows = await api.listBatches(g.goal_id);
  } catch {
    rows = null;
  }
  if (seq !== shell.badgeSeq) return;
  const pending = (Array.isArray(rows) ? rows : []).filter((b) => b.status === "pending").length;
  showBadge(link, pending > 0);
  link.setAttribute("href", hashFor("live", g.goal_id));
  $("batch-count").textContent = String(pending);
  link.setAttribute("aria-label", `${pending} ${pending === 1 ? "batch" : "batches"} waiting for your approval`);
  link.title = "Approve or reject them on the Live screen. Nothing in a batch is sent until you approve it.";
}

/** Reload the goal list (after a screen created, approved, paused or resumed one) and redraw the bar. */
async function refreshGoals() {
  await loadGoals();
  const route = parseHash(location.hash);
  drawTopbar(route.screen);
  return shell.goals;
}

async function togglePause() {
  const g = currentGoal();
  if (!g) return;
  const b = $("pause-button");
  b.disabled = true;
  try {
    const pausing = g.status === "active";
    const next = pausing ? await api.pauseGoal(g.goal_id) : await api.resumeGoal(g.goal_id);
    if (!(next && next.goal_id)) await loadGoals(); // a full Goal updates the bar through onGoalChange
    toast(pausing ? "Paused. Nothing more will be sent for this goal until you resume." : "Resumed. The agent is acting inside the plan again.", pausing ? "warning" : "good");
    // Live follows the change itself (api.onGoalChange), keeping its feed and any open customer panel. Other
    // screens are redrawn, leaving keyboard focus on the button just pressed.
    if (parseHash(location.hash).screen !== "live" || !(next && next.goal_id)) route({ focus: false });
  } catch (err) {
    toast(`Could not change the goal. ${err.message || ""}`.trim(), "critical");
    drawPause();
  }
}

// ---- the router -------------------------------------------------------------------------------------------
async function loadScreen(screen) {
  if (!shell.modules.has(screen.id)) {
    shell.modules.set(
      screen.id,
      screen.load().catch((err) => {
        shell.modules.delete(screen.id); // try again next time
        throw err;
      }),
    );
  }
  return shell.modules.get(screen.id);
}

function placeholder(screen) {
  return emptyState(
    `${screen.label} is not ready yet`,
    "This screen is still being built. The rest of the app works without it.",
    button("Back to Goal", () => navigate("goal"), { kind: "secondary" }),
  );
}

/** Draw the screen the address names. `focus: false` redraws without moving keyboard focus to the content. */
export async function route({ focus = true } = {}) {
  const seq = ++shell.seq;
  const main = $("main");
  if (!location.hash || location.hash === "#" || location.hash === "#/") {
    history.replaceState(null, "", DEFAULT_ROUTE);
  }
  const { screen: screenId, goalId } = parseHash(location.hash);
  const screen = ALL.find((s) => s.id === screenId);

  // A goal the list has not seen (one just created, or a shared link) means the list is stale: load it again.
  if (!shell.goalsLoaded || (goalId && !shell.goals.some((g) => g.goal_id === goalId))) await loadGoals();
  if (seq !== shell.seq) return;

  if (goalId) shell.goalId = goalId;
  else if (!shell.goalId) shell.goalId = defaultGoalId();
  if (shell.goalId) remember(shell.goalId);

  drawTopbar(screen ? screen.id : null);
  document.title = screen ? `${screen.label} · Autonomy` : "Not found · Autonomy";

  const root = el("div", { class: "screen", dataset: { screen: screen ? screen.id : "unknown" } });
  clear(main, root);

  if (!screen) {
    clear(root, emptyState("There is no such page", "The address may be mistyped.", button("Go to Goal", () => navigate("goal"), { kind: "primary" })));
  } else if (screen.goal && !shell.goalId) {
    clear(
      root,
      el("h1", {}, screen.label),
      emptyState("Choose a goal first", "Tell Autonomy what you want to achieve, or pick a goal from the switcher at the top.", button("Set a goal", () => navigate("goal"), { kind: "primary" })),
    );
  } else if (screen.goal && !goalId) {
    // The address names no goal: put the current one in it, so the page can be bookmarked and shared.
    history.replaceState(null, "", hashFor(screen.id, shell.goalId));
    route();
    return;
  } else {
    clear(root, loading());
    let mod;
    try {
      mod = await loadScreen(screen);
    } catch {
      if (seq === shell.seq) clear(root, placeholder(screen));
      return;
    }
    if (seq !== shell.seq) return;
    if (!mod || typeof mod.render !== "function") {
      clear(root, placeholder(screen));
      return;
    }
    clear(root);
    const ctx = { api, goalId: shell.goalId, goal: currentGoal(), navigate, toast, refreshGoals, pauseInTopBar: Boolean($("pause-button")) };
    try {
      await mod.render(root, ctx);
    } catch (err) {
      if (seq === shell.seq) clear(root, errorBox(`The ${screen.label} screen hit a problem.`, err));
    }
  }
  if (seq !== shell.seq) return;
  if (!shell.firstRender && focus) main.focus({ preventScroll: false });
  shell.firstRender = false;
}

function start() {
  $("goal-select").addEventListener("change", (e) => {
    const id = e.target.value;
    if (!id) return;
    shell.goalId = id;
    remember(id);
    const { screen } = parseHash(location.hash);
    const current = ALL.find((s) => s.id === screen);
    if (current && (current.goal || current === OPERATOR)) navigate(current.id, id);
    else {
      drawTopbar(screen);
      loadInbox();
      loadBatchBadge();
    }
  });
  $("pause-button").addEventListener("click", togglePause);
  // The skip link's "#main" would change the hash and the router would call it an unknown page: move focus
  // to the content instead, leaving the address alone.
  const skip = $("skip-link");
  if (skip) {
    skip.addEventListener("click", (e) => {
      e.preventDefault();
      $("main").focus();
    });
  }
  api.onModeChange(drawBanner);
  // A screen paused, resumed or approved a goal: keep the switcher and the Pause button in step with it.
  api.onGoalChange((goal) => {
    const known = shell.goals.some((g) => g.goal_id === goal.goal_id);
    shell.goals = known ? shell.goals.map((g) => (g.goal_id === goal.goal_id ? goal : g)) : [...shell.goals, goal];
    drawSwitcher();
    drawPause();
    loadBatchBadge(); // the autonomy may have moved to or from "approve each batch"
  });
  if (typeof api.onBatchChange === "function") api.onBatchChange(() => loadBatchBadge());
  const afterRoute = () => {
    loadInbox();
    loadBatchBadge();
  };
  window.addEventListener("hashchange", () => route().then(afterRoute));
  route().then(afterRoute);
}

if (typeof document !== "undefined" && document.getElementById("main")) start();
