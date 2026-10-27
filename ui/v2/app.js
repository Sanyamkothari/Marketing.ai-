// The Autonomy shell: the top bar (name, goal switcher, the goal's autonomy badge, a "paused itself" badge, inbox,
// the one big Pause button), the five numbered screens, a slim note under the bar (Shadow: "nothing is being
// sent", with the trial day; or "getting started is safe" before any plan is approved), and a hash router that
// loads each screen as an ES module.
//
// Every screen module exports `render(root, ctx)`, where ctx = {api, goalId, navigate, toast} (plus `goal`, the
// current Goal object or null, `refreshGoals()`, for screens that change a goal, `pauseAllInTopBar`, which tells a
// screen that the top bar's Pause stops every goal at once, and `pauseInTopBar`, now false: the top bar no longer
// pauses only the current goal, so a screen keeps its own per-goal Pause). A screen that fails to load shows a
// placeholder instead of breaking the app, so screens can be built and shipped one at a time.
//
// The big Pause button pauses every running goal (POST /v2/pause, or each goal's own pause on an older API).
// With nothing running it offers Resume for the goals it paused (or the current goal, if that one is paused).
//
// When the current goal is at "approve each batch", the top bar also shows how many batches wait for approval;
// it links to the Live screen, where they are approved or rejected.
//
// Getting started (#/start, screens/onboarding.js) is linked from the nav, and from the note under the bar while
// no plan has been approved; with no goal at all it is the first page.

import { createApi, routeMissing } from "./api.js";
import { button, clear, describe, el, emptyState, errorBox, goalStatusWords, humanize, info, int, isSimulatedShop, loading, present, range, toast, when } from "./components.js";
import { TRIAL_DAYS, rangeVerdict, trialDay, trialWords } from "./screens/onboarding.js";

/** The screens, in the order a marketer meets them. `goal: true` means the address carries a goal id. */
export const SCREENS = Object.freeze([
  { id: "connect", label: "Connect", goal: false, load: () => import("./screens/connect.js") },
  { id: "goal", label: "Goal", goal: false, load: () => import("./screens/goal.js") },
  { id: "plan", label: "Plan", goal: true, load: () => import("./screens/plan.js") },
  { id: "live", label: "Live", goal: true, load: () => import("./screens/live.js") },
  { id: "proof", label: "Proof", goal: true, load: () => import("./screens/proof.js") },
]);
export const OPERATOR = Object.freeze({ id: "operator", label: "Operator mode", goal: false, load: () => import("./screens/operator.js") });
/** Getting started: the safe path (Shadow first), outside the five numbered steps. */
export const START = Object.freeze({ id: "start", label: "Getting started", goal: false, load: () => import("./screens/onboarding.js") });
const ALL = [...SCREENS, START, OPERATOR];
const DEFAULT_ROUTE = "#/goal";
const FIRST_RUN_ROUTE = "#/start";
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
  noteSeq: 0,
  inboxSeq: 0,
  /** Goals the big Pause button paused in this page load, so Resume brings back exactly those. */
  pausedByButton: new Set(),
  /** Per goal: {at, since, today, expected} for the Shadow note, kept a minute so routing stays quick. */
  shadowFacts: new Map(),
  /** True when the server's only data is the Gym's simulated shop (the sample files are `api.isMock()` instead). */
  simulated: false,
};

const $ = (id) => document.getElementById(id);

/**
 * The shell's explanations live in descriptions (components.js describe), not on the bar: each top-bar control gets
 * one box, made once right after it and re-worded on every redraw. Without a real DOM (the node tests) the words
 * stay in the control's aria-label only.
 */
function setInfo(target, text) {
  if (!target) return;
  const tipId = `${target.id}-info`;
  let tip = $(tipId);
  if (!tip) {
    if (typeof target.after !== "function") return;
    tip = el("span", { id: tipId });
    target.after(tip);
    describe(target, tip);
  }
  tip.textContent = text;
}

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
  return `${text} (${goalStatusWords(g.status).toLowerCase()})`;
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

const running = () => shell.goals.filter((g) => g.status === "active");
const goalsWord = (n) => `${n} ${n === 1 ? "goal" : "goals"}`;

/** What Resume brings back: the goals the big button paused (still paused), else the current goal if paused. */
function resumeTargets() {
  const mine = shell.goals.filter((g) => g.status === "paused" && shell.pausedByButton.has(g.goal_id));
  if (mine.length) return mine;
  const g = currentGoal();
  return g && g.status === "paused" ? [g] : [];
}

function drawPause() {
  const b = $("pause-button");
  const scope = $("pause-scope");
  const live = running();
  const targets = live.length ? [] : resumeTargets();
  // Near-black with a pause glyph while anything sends; Resume, in the negative colour, only while paused (the
  // stopped state is plain at a glance). screens/shell.css draws the glyphs.
  b.classList.remove("btn-danger", "btn-primary", "is-resume");
  b.removeAttribute("title"); // the words are in its description (#pause-scope), not a browser tooltip
  if (live.length) {
    b.textContent = "Pause";
    b.classList.add("btn-primary");
    b.disabled = false;
    b.setAttribute("aria-label", `Pause all sending: stops ${live.length === 1 ? "the 1 running goal" : `all ${live.length} running goals`} at once`);
    if (scope) scope.textContent = "Stops all sending, for every goal, at once. You can resume any time.";
  } else if (targets.length) {
    b.textContent = "Resume";
    b.classList.add("btn-danger", "is-resume");
    b.disabled = false;
    b.setAttribute("aria-label", targets.length === 1 ? `Resume sending for: ${targets[0].text || targets[0].goal_id}` : `Resume sending for the ${targets.length} goals you paused`);
    if (scope) scope.textContent = "Nothing is being sent. Resume lets the agent act again inside the approved plan.";
  } else {
    b.textContent = "Pause";
    b.classList.add("btn-primary");
    b.disabled = true;
    b.removeAttribute("aria-label");
    if (scope) scope.textContent = shell.goals.length ? "Nothing is sending right now, so there is nothing to pause." : "Nothing is sending: there is no goal yet.";
  }
}

/** The autonomy level in a few words, for the badge next to the goal switcher. */
const AUTONOMY_BADGE = Object.freeze({
  shadow: { text: "Shadow: sends nothing", long: "Shadow: it decides and shows you what it would do, but sends nothing" },
  approve: { text: "You approve each batch", long: "Approve each batch: nothing goes out until you approve it with one click" },
  autopilot: { text: "Autopilot", long: "Autopilot: it acts on its own inside the limits you set" },
});

/** Badge text, tone and the sentence a screen reader hears, for a goal (and its Shadow trial day, when known). */
export function autonomyBadge(goal, day = null) {
  if (!goal) return null;
  if (goal.status === "draft") {
    return { text: "Plan not approved: nothing runs yet", tone: "draft", long: "This goal's plan is not approved yet, so nothing runs. Check and approve it on the Plan page." };
  }
  if (goal.status === "done" || goal.status === "retired") {
    return { text: "Finished", tone: "draft", long: "This goal has finished. Nothing more is decided or sent for it." };
  }
  const base = AUTONOMY_BADGE[goal.autonomy] || { text: humanize(goal.autonomy), long: humanize(goal.autonomy) };
  const trial = goal.autonomy === "shadow" ? trialWords(day) : null;
  const text = `${goal.status === "paused" ? "Paused · " : ""}${base.text}${trial ? ` · ${trial}` : ""}`;
  const long = `Autonomy level: ${base.long}.${trial ? ` Shadow trial: ${trial}.` : ""}${goal.status === "paused" ? " Paused right now." : ""} Change it on the Plan page.`;
  return { text, tone: goal.status === "paused" ? "paused" : goal.autonomy, long };
}

function drawAutonomyBadge() {
  const badge = $("autonomy-badge");
  if (!badge) return;
  const g = currentGoal();
  const facts = g ? shell.shadowFacts.get(g.goal_id) : null;
  const info = autonomyBadge(g, g && g.autonomy === "shadow" && facts ? trialDay(facts.since) : null);
  if (!info) {
    badge.hidden = true;
    return;
  }
  badge.hidden = false;
  badge.textContent = info.text;
  badge.className = `autonomy-badge autonomy-${info.tone}`;
  badge.setAttribute("href", hashFor("plan", g.goal_id));
  // The spoken name starts with the visible words, so voice control ("click Autopilot") finds it.
  badge.setAttribute("aria-label", `${info.text}. ${info.long}`);
  setInfo(badge, info.long);
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
  // Operator mode is for us, not the marketer: small, and on a phone shown only on request (?operator=1).
  op.classList.toggle("is-asked", activeId === OPERATOR.id || operatorAsked());
  const start = $("start-link");
  if (start) {
    start.classList.toggle("is-current", activeId === START.id);
    if (activeId === START.id) start.setAttribute("aria-current", "page");
    else start.removeAttribute("aria-current");
  }
  $("inbox-link").setAttribute("href", hashFor("live", shell.goalId));
}

function operatorAsked() {
  try {
    return new URLSearchParams((typeof location !== "undefined" && location.search) || "").get("operator") === "1";
  } catch {
    return false;
  }
}

function drawBanner() {
  const banner = $("mode-banner");
  if (api.isMock()) {
    clear(
      banner,
      el("strong", {}, "Simulated shop."),
      " Sample data: nothing you do here reaches a customer.",
      info(`${api.reason || ""} Changes last until you reload.`.trim(), { label: "About the sample data" }),
    );
    banner.hidden = false;
    return;
  }
  if (!shell.simulated) {
    banner.hidden = true;
    return;
  }
  // The demo server runs a shop the Gym simulates: every number on every screen is from it, so the bar says so.
  clear(
    banner,
    el("strong", {}, "Simulated shop."),
    " These numbers come from a shop we simulate, not from your customers.",
  );
  banner.hidden = false;
}

/** Ask once (the answer is kept a minute) whether the server's data is a simulated shop, and redraw the banner. */
async function refreshSimulated() {
  let now = false;
  try {
    now = await isSimulatedShop({ api });
  } catch {
    now = false;
  }
  if (now !== shell.simulated) {
    shell.simulated = now;
    drawBanner();
  }
}

function drawTopbar(activeId) {
  drawSwitcher();
  drawAutonomyBadge();
  drawPause();
  drawNav(activeId);
  drawBanner();
  drawGoalNote(activeId);
}

// ---- the note under the bar ---------------------------------------------------------------------------------
/**
 * One slim line under the bar, always visible (the bar is sticky):
 * - the current goal is in Shadow: "Nothing is being sent", the trial day, and what it would have done;
 * - the current goal is paused: nothing is being sent until Resume;
 * - no plan approved yet (or no goal): getting started is safe, with a link to the steps.
 */
function drawGoalNote(activeId = parseHash(location.hash).screen) {
  const note = $("goal-note");
  if (!note) return;
  const g = currentGoal();
  const approvedAny = shell.goals.some((x) => x.status !== "draft");
  // A paused goal neither decides nor sends (only running goals are run), so "deciding" is only said while it runs.
  if (g && g.autonomy === "shadow" && g.status === "active") {
    const facts = shell.shadowFacts.get(g.goal_id) || null;
    const trial = facts ? trialWords(trialDay(facts.since)) : null;
    note.className = "goal-note goal-note-shadow";
    clear(
      note,
      el("strong", {}, "Shadow mode."),
      " ",
      el("span", {}, "Autonomy is deciding, but nothing is being sent."),
      trial ? el("span", { class: "goal-note-trial" }, ` Shadow trial: ${trial}.`) : null,
      // What it would have done and its own forecast: one hover (or tap) away, not on the bar.
      info(shadowDetail(g, facts), { label: "What it would have done so far" }),
      activeId === START.id ? null : el("a", { class: "goal-note-link", href: "#/start" }, trial && trialDay(facts.since) > TRIAL_DAYS ? "What's next" : "About the trial"),
    );
    note.hidden = false;
    return;
  }
  if (g && g.status === "paused") {
    note.className = "goal-note goal-note-paused";
    clear(
      note,
      el("strong", {}, "Paused: nothing is being sent for this goal."),
      " ",
      g.autonomy === "shadow" ? info("It is not deciding either, so the Shadow trial is on hold.", { label: "About the Shadow trial" }) : null,
      " ",
      // The big button says Resume only while nothing else runs; otherwise it pauses the others, so point to Live.
      !running().length
        ? "Press Resume at the top when you want it to act again."
        : activeId === "live"
          ? "Press Resume on this page when you want it to act again."
          : el("a", { class: "goal-note-link", href: hashFor("live", g.goal_id) }, "Resume it on the Live page"),
    );
    note.hidden = false;
    return;
  }
  if (shell.goalsLoaded && !approvedAny && activeId !== START.id) {
    note.className = "goal-note goal-note-start";
    clear(
      note,
      el("strong", {}, "New here? Getting started is safe."),
      info(
        "Shadow first (it decides but sends nothing), then Approve with a small budget, then Autopilot when Proof shows real extra profit. It has not yet run with a real business, so start in Shadow.",
        { label: "How getting started works" },
      ),
      el("a", { class: "goal-note-link", href: "#/start" }, "See the steps"),
    );
    note.hidden = false;
    return;
  }
  note.hidden = true;
  clear(note);
}

/** "On 19 Oct it would have contacted 412 and left 1,688 alone. Its own forecast: $a – $b (not proof)." The words of
 * the Shadow note's description, or null when nothing is known yet. */
function shadowDetail(goal, facts) {
  if (!facts) return null;
  const parts = [];
  const t = facts.today;
  if (t && Number(t.customers_considered) === 0) {
    parts.push(` It hasn't made its first decisions yet${present(t.next_run_at) ? `; the next daily run is ${when(t.next_run_at)}` : ""}.`);
  } else if (t && present(t.contacted)) {
    parts.push(` On ${when(t.date, { time: false })} it would have contacted ${int(t.contacted)} ${Number(t.contacted) === 1 ? "customer" : "customers"} and left ${int(t.left_alone)} alone.`);
  }
  const e = facts.expected;
  if (e && e.expected_net) {
    const unsure = rangeVerdict(e.expected_net) === "unsure";
    parts.push(` Its own forecast so far: ${range(e.expected_net, e.currency || goal.currency)} extra profit${unsure ? ", so not sure yet" : ""} (a forecast, not proof).`);
  }
  return parts.length ? parts.join("").trim() : null;
}

/** Fetch what the Shadow note needs for the current goal (once a minute per goal), then redraw the note. */
async function loadShadowFacts() {
  const g = currentGoal();
  const seq = ++shell.noteSeq;
  if (!g || g.autonomy !== "shadow" || !(g.status === "active" || g.status === "paused")) return;
  const cached = shell.shadowFacts.get(g.goal_id);
  if (cached && Date.now() - cached.at < 60000) return;
  const optional = (name, ...args) => (typeof api[name] === "function" ? api[name](...args) : Promise.resolve(null));
  const [todayR, expectedR] = await Promise.allSettled([optional("getToday", g.goal_id), optional("getExpected", g.goal_id)]);
  const today = todayR.status === "fulfilled" ? todayR.value : null;
  const expected = expectedR.status === "fulfilled" ? expectedR.value : null;
  let since = today && today.autonomy_since;
  if (!since) {
    // The approval date is when Shadow started: the brief's approved_at, when the day count has none.
    try {
      const detail = await api.getGoal(g.goal_id);
      since = (detail && detail.brief && detail.brief.approved_at) || null;
    } catch {
      since = null;
    }
  }
  if (seq !== shell.noteSeq) return;
  shell.shadowFacts.set(g.goal_id, { at: Date.now(), since, today, expected });
  drawAutonomyBadge();
  drawGoalNote();
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
  refreshSimulated();
}

/**
 * The alerts Live's Inbox lists for this goal: /alerts and /health's alerts together, once each, this goal's and
 * the account-wide ones (every severity, info too), newest first. The same set as Live, so the counts agree.
 */
export function inboxAlerts(lists, goalId) {
  const seen = new Map();
  for (const list of lists) for (const a of Array.isArray(list) ? list : []) if (a && a.alert_id && !seen.has(a.alert_id)) seen.set(a.alert_id, a);
  return [...seen.values()]
    .filter((a) => !goalId || !a.goal_id || a.goal_id === goalId)
    .sort((a, b) => String(b.at || "").localeCompare(String(a.at || "")));
}

/** Inbox kinds that mean harm or a stopped business (autonomy/inbox/items.py); the rest are decisions to take. */
const URGENT_KINDS = new Set(["safety_stop", "losing", "reconnect", "data"]);

/** The exceptions-only inbox's items Live lists for this goal: this goal's and the account-wide ones. */
export function inboxItems(view, goalId) {
  const items = view && Array.isArray(view.items) ? view.items : [];
  return items.filter((i) => i && (!goalId || !i.goal_id || i.goal_id === goalId));
}

async function loadInbox() {
  const seq = ++shell.inboxSeq;
  const [alertsR, healthR, boxR] = await Promise.allSettled([
    api.listAlerts(),
    api.getHealth(),
    typeof api.listInbox === "function" ? api.listInbox() : Promise.resolve(null),
  ]);
  if (seq !== shell.inboxSeq) return;
  const box = boxR.status === "fulfilled" ? boxR.value : null;
  if (alertsR.status === "rejected" && healthR.status === "rejected" && !box) return; // a hint; Live shows the inbox itself
  const lists = [alertsR.status === "fulfilled" ? alertsR.value : [], healthR.status === "fulfilled" && healthR.value ? healthR.value.alerts : []];
  // With the exceptions-only inbox (GET /v2/inbox) the count is its items, the same set Live's Inbox lists; an older
  // API counts the alerts, as Live then lists them.
  const all = box ? inboxItems(box, shell.goalId) : inboxAlerts(lists, shell.goalId);
  const urgent = box ? all.filter((i) => URGENT_KINDS.has(i.kind)).length : all.filter((a) => a.severity === "critical" || a.severity === "warning").length;
  const count = $("inbox-count");
  count.hidden = all.length === 0;
  count.textContent = String(all.length);
  count.classList.toggle("count-quiet", urgent === 0);
  $("inbox-link").setAttribute(
    "aria-label",
    all.length ? `Inbox, ${all.length} ${all.length === 1 ? "message" : "messages"}${urgent ? `, ${urgent} need a look` : ", nothing urgent"}` : "Inbox, empty",
  );
  drawSelfPause(inboxAlerts(lists, shell.goalId).filter((a) => a.auto_paused));
}

/** The red "Paused itself" badge: Autonomy stopped a goal or a channel on its own (a stop rule fired). */
function drawSelfPause(paused) {
  const badge = $("selfpause-badge");
  if (!badge) return;
  if (!paused.length) {
    badge.hidden = true;
    return;
  }
  const latest = paused[0];
  badge.hidden = false;
  const text = paused.length > 1 ? `Paused itself (${paused.length})` : "Paused itself";
  badge.textContent = text;
  badge.setAttribute("href", hashFor("live", shell.goalId));
  const words = `Autonomy paused sending on its own ${paused.length === 1 ? "once" : `${paused.length} times`}. Latest, ${when(latest.at)}: ${latest.message || humanize(latest.code)} Open the Inbox on the Live page.`;
  badge.setAttribute("aria-label", `${text}. ${words}`);
  setInfo(badge, words);
}

/** The "to approve" link in the top bar: index.html has it; made here, next to the inbox, if a page lacks it. */
function batchLink() {
  const existing = $("batch-link");
  if (existing) return existing;
  const inbox = $("inbox-link");
  if (!inbox || typeof inbox.before !== "function") return null;
  const link = el(
    "a",
    { id: "batch-link", class: "inbox-link batch-link", href: "#/live" },
    // `topbar-label` is hidden visually on a phone (styles.css), so there the link is only its count and the
    // goal switcher keeps its room; the link's aria-label and title still say what it is.
    el("span", { class: "topbar-label", "aria-hidden": "true" }, "To approve"),
    // The accent, not the inbox's red: a batch waiting is a task, not an alarm.
    el("span", { id: "batch-count", class: "count count-accent" }),
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
  setInfo(link, "Approve or reject them on the Live screen. Nothing in a batch is sent until you approve it.");
}

/** Reload the goal list (after a screen created, approved, paused or resumed one) and redraw the bar. */
async function refreshGoals() {
  await loadGoals();
  // A first goal made on a screen (the setup on Getting started) becomes the current one, so the bar shows it.
  if (!shell.goalId) shell.goalId = defaultGoalId();
  const route = parseHash(location.hash);
  drawTopbar(route.screen);
  return shell.goals;
}

/** Pause every running goal at once: POST /v2/pause, or each goal's own pause on an API without that route. */
async function pauseEverything(live) {
  let out = null;
  if (typeof api.pauseAll === "function") {
    try {
      out = await api.pauseAll();
    } catch (err) {
      if (!routeMissing(err)) throw err;
      out = null;
    }
  }
  if (out && Array.isArray(out.paused)) return out.paused;
  const paused = [];
  for (const g of live) paused.push(await api.pauseGoal(g.goal_id));
  return paused;
}

async function togglePause() {
  const live = running();
  const targets = live.length ? [] : resumeTargets();
  if (!live.length && !targets.length) return;
  const b = $("pause-button");
  b.disabled = true;
  try {
    let changed;
    if (live.length) {
      changed = await pauseEverything(live);
      for (const g of changed) if (g && g.goal_id) shell.pausedByButton.add(g.goal_id);
      const n = changed.length;
      toast(
        n ? `Paused all sending: ${goalsWord(n)} stopped. Nothing more goes out until you press Resume.` : "Nothing was sending, so nothing needed pausing.",
        "warning",
      );
    } else {
      changed = [];
      for (const g of targets) changed.push(await api.resumeGoal(g.goal_id));
      for (const g of targets) shell.pausedByButton.delete(g.goal_id);
      toast(`Resumed: ${goalsWord(changed.length)} acting inside the plan again.`, "good");
    }
    const full = changed.every((g) => g && g.goal_id); // full Goals update the bar through onGoalChange
    if (!full) await loadGoals();
    drawSwitcher();
    drawAutonomyBadge();
    drawPause();
    drawGoalNote();
    // Live follows the change itself (api.onGoalChange), keeping its feed and any open customer panel. Other
    // screens are redrawn, leaving keyboard focus on the button just pressed.
    if (parseHash(location.hash).screen !== "live" || !full) route({ focus: false });
  } catch (err) {
    toast(`Could not change sending. ${err.message || ""}`.trim(), "critical");
    await loadGoals();
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
  const bare = !location.hash || location.hash === "#" || location.hash === "#/";
  if (bare) {
    // First run (no goal at all): start with the safe path. Otherwise the Goal screen.
    if (!shell.goalsLoaded) await loadGoals();
    if (seq !== shell.seq) return;
    history.replaceState(null, "", shell.goals.length ? DEFAULT_ROUTE : FIRST_RUN_ROUTE);
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
    // The top bar's Pause stops every goal now, so screens keep their own per-goal Pause (pauseInTopBar false).
    const ctx = { api, goalId: shell.goalId, goal: currentGoal(), navigate, toast, refreshGoals, pauseInTopBar: false, pauseAllInTopBar: Boolean($("pause-button")) };
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

/** ?theme=light or ?theme=dark forces a theme (styles.css reads [data-theme]); otherwise the system's setting. */
function applyTheme() {
  try {
    const theme = new URLSearchParams((typeof location !== "undefined" && location.search) || "").get("theme");
    const root = typeof document !== "undefined" ? document.documentElement : null;
    if (root && root.setAttribute && (theme === "light" || theme === "dark")) root.setAttribute("data-theme", theme);
  } catch {
    /* the system's setting stands */
  }
}

function start() {
  applyTheme();
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
      loadShadowFacts();
    }
  });
  $("pause-button").addEventListener("click", togglePause);
  // "Stops all sending" is the Pause button's description (hover or keyboard focus), not words on the bar.
  if ($("pause-scope")) describe($("pause-button"), $("pause-scope"));
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
    if (goal.status === "active") shell.pausedByButton.delete(goal.goal_id);
    drawSwitcher();
    drawAutonomyBadge();
    drawPause();
    drawGoalNote();
    loadBatchBadge(); // the autonomy may have moved to or from "approve each batch"
    shell.shadowFacts.delete(goal.goal_id); // approved in Shadow just now, or moved on: count the trial again
    loadShadowFacts();
  });
  if (typeof api.onBatchChange === "function") api.onBatchChange(() => loadBatchBadge());
  const afterRoute = () => {
    loadInbox();
    loadBatchBadge();
    loadShadowFacts();
  };
  window.addEventListener("hashchange", () => route().then(afterRoute));
  route().then(afterRoute);
}

if (typeof document !== "undefined" && document.getElementById("main")) start();
