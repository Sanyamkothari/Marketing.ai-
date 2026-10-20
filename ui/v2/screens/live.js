/* Live: what the agent is doing right now, and why (plan-v2.md section 9, screen 4).

   One page holds the batches waiting for approval (when the goal is at "approve each batch"), today's
   briefing, the activity feed (every decision with its reason sentence, the "left alone" ones folded
   away with a count), the inbox of alerts (and what paused itself), and the customer health numbers
   next to the limits in the approved brief. Clicking a customer opens a side panel with their state
   and a timeline of events and decisions, each decision with its "why".

   Pausing: inside the app the top bar's Pause button is the one control (ctx.pauseInTopBar), and this
   page only shows the goal's state, following every change through api.onGoalChange. Two buttons that
   did the same thing with different words were confusing. Drawn on its own (no top bar), the page
   keeps its own Pause button so it never loses the one-click stop.

   Rules this file keeps, because the marketer reads these numbers as facts:
     - a value the API did not send is an em dash, never a guess or a zero;
     - money is shown in the goal's currency, never a hard-coded one;
     - each section loads on its own, so one failing endpoint never blanks the page.

   The small helpers at the top (`h`, the formatters, `ensureStyles`, `resolveGoal`) are also used by
   proof.js and operator.js. The basics every screen shares (the em dash, `present`, `num`) come from
   components.js; the formatters kept here are the ones whose wording the feed and the proof page need
   ("$4,300 (range $900 to $7,700)", a date without the year). */

import { EM_DASH, num, present } from "../components.js";

// ---- tiny helpers shared by live.js, proof.js and operator.js ------------------------------------------

export { num, present };
export const DASH = EM_DASH;
const SVG_NS = "http://www.w3.org/2000/svg";

/** Append children, skipping null/false so callers can write `cond && h(...)`. */
function appendKids(node, kids) {
  for (const kid of kids.flat(Infinity)) {
    if (kid === null || kid === undefined || kid === false) continue;
    node.appendChild(typeof kid === "object" ? kid : document.createTextNode(String(kid)));
  }
  return node;
}

function setProps(node, props) {
  for (const [key, value] of Object.entries(props || {})) {
    if (value === null || value === undefined || value === false) continue;
    if (key === "class") node.setAttribute("class", String(value));
    else if (key === "text") node.textContent = String(value);
    else if (key.startsWith("on") && typeof value === "function") node.addEventListener(key.slice(2), value);
    else node.setAttribute(key, value === true ? "" : String(value));
  }
  return node;
}

/** Build an element. Text children become text nodes, so nothing from the API is ever parsed as HTML. */
export function h(tag, props, ...kids) {
  return appendKids(setProps(document.createElement(tag), props), kids);
}

/** Build an SVG element (for the proof chart). */
export function s(tag, props, ...kids) {
  return appendKids(setProps(document.createElementNS(SVG_NS, tag), props), kids);
}

/** Money in the goal's currency. Cents only for small amounts, where they matter. No currency: a plain number. */
export function money(n, currency) {
  if (!present(n)) return DASH;
  const value = Number(n);
  const small = Math.abs(value) < 1000 && !Number.isInteger(value);
  const digits = { minimumFractionDigits: small ? 2 : 0, maximumFractionDigits: small ? 2 : 0 };
  if (currency) {
    try {
      return new Intl.NumberFormat("en-US", { style: "currency", currency, ...digits }).format(value);
    } catch {
      // An unknown currency code: show the code rather than a wrong symbol.
    }
    return `${new Intl.NumberFormat("en-US", digits).format(value)} ${currency}`;
  }
  return new Intl.NumberFormat("en-US", digits).format(value);
}

/** A whole count with grouping ("8,120"). */
export const count = (n) => (present(n) ? Math.round(Number(n)).toLocaleString("en-US") : DASH);

/** A share as a percent. Tiny rates (complaints) keep two decimals so 0.02% does not read as 0%. */
export function pct(fraction) {
  if (!present(fraction)) return DASH;
  const p = Number(fraction) * 100;
  const places = Math.abs(p) < 1 ? 2 : 1;
  return `${p.toLocaleString("en-US", { maximumFractionDigits: places })}%`;
}

/** An estimate always with its range: "$4,300 (range $900 to $7,700)". Never a bare point. */
export function range(est, fmt) {
  if (!est || !present(est.point) || !present(est.low) || !present(est.high)) return DASH;
  return `${fmt(est.point)} (range ${fmt(est.low)} to ${fmt(est.high)})`;
}

/** A timestamp in the viewer's time zone: "20 Oct, 14:00". */
export function when(iso) {
  if (!present(iso)) return DASH;
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return String(iso);
  return d.toLocaleString("en-GB", { day: "numeric", month: "short", hour: "2-digit", minute: "2-digit" });
}

/** A date only: "8 Jul 2026". */
export function day(iso) {
  if (!present(iso)) return DASH;
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return String(iso);
  return d.toLocaleDateString("en-GB", { day: "numeric", month: "short", year: "numeric" });
}

const CHANNELS = {
  email: "Email",
  whatsapp: "WhatsApp",
  sms: "SMS",
  push: "Push",
  web: "Web",
  webhook: "Your tool (webhook)",
};
export const channelLabel = (c) => (present(c) ? CHANNELS[c] || String(c) : DASH);

const GROUPS = { agent: "Agent", baseline: "Simple rule", holdout: "Holdout (never contacted)" };
export const groupLabel = (g) => (present(g) ? GROUPS[g] || String(g) : DASH);

/** The offer's label from the approved brief; the raw id when the brief does not know it. */
export function offerLabel(brief, offerId) {
  if (!present(offerId) || offerId === "none") return "No offer";
  const offers = brief?.envelope?.offers || [];
  const found = offers.find((o) => o.offer_id === offerId);
  return found ? found.label : String(offerId);
}

/** The error's own words, for "Couldn't load ...: <why>". */
export const errText = (e) => (e && e.message ? e.message : String(e));

/** Call an API method by name, failing clearly when api.js does not provide it. */
export function call(api, name, ...args) {
  if (!api || typeof api[name] !== "function") return Promise.reject(new Error(`the API has no ${name}()`));
  try {
    return Promise.resolve(api[name](...args));
  } catch (e) {
    return Promise.reject(e);
  }
}

/** Tell the person something: the app's toast when there is one, else a polite live region. */
export function notify(ctx, message, kind = "info", fallback = null) {
  if (ctx && typeof ctx.toast === "function") ctx.toast(message, kind);
  else if (fallback) fallback.textContent = message;
}

/** Load feed.css once, next to this file, so the screens look right whatever app.js links. */
export function ensureStyles() {
  if (typeof document === "undefined" || !document.head) return;
  if (document.getElementById("v2-feed-css")) return;
  const link = document.createElement("link");
  link.id = "v2-feed-css";
  link.rel = "stylesheet";
  link.href = new URL("./feed.css", import.meta.url).href;
  document.head.appendChild(link);
}

/**
 * The goal this screen is about: ctx.goalId when given, else the first active goal (then any goal).
 * Returns the detail bundle {goal, brief, content, proof}, or null when there is no goal at all.
 */
export async function resolveGoal(ctx) {
  let id = ctx.goalId;
  if (!present(id)) {
    if (typeof ctx.api?.listGoals !== "function") return null;
    const goals = (await ctx.api.listGoals()) || [];
    const pick = goals.find((g) => g.status === "active") || goals.find((g) => g.status === "paused") || goals[0];
    if (!pick) return null;
    id = pick.goal_id;
  }
  const bundle = await call(ctx.api, "getGoal", id);
  return bundle && bundle.goal ? bundle : { goal: bundle, brief: null, content: [], proof: null };
}

/** A card with a heading and a body that starts as "Loading…". */
export function section(title, extraClass = "") {
  const heading = h("h2", { class: "ap-h2" }, title);
  const body = h("div", { class: "ap-card-body" }, h("p", { class: "ap-muted" }, "Loading…"));
  const card = h("section", { class: `ap-card ${extraClass}`.trim() }, heading, body);
  return { card, heading, body };
}

/** True when the API answered "no such thing" (404), which is an empty state, not a failure. */
export const notFound = (e) => Boolean(e) && e.status === 404;

/** The plain-language error inside a section. */
export const failed = (what, e) => h("p", { class: "ap-error", role: "alert" }, `Couldn't load ${what}: ${errText(e)}`);

/** The empty state: one sentence. */
export const empty = (sentence) => h("p", { class: "ap-muted" }, sentence);

/** The page heading with an optional subtitle. */
export function pageHead(title, subtitle) {
  return h("header", { class: "ap-head" }, h("h1", { class: "ap-h1" }, title), subtitle && h("p", { class: "ap-sub" }, subtitle));
}

// ---- pure logic, exported for tests ----------------------------------------------------------------

/**
 * Milliseconds since the epoch for an ISO timestamp, for sorting. Text order breaks as soon as two
 * timestamps carry different offsets ("Z" against "+02:00"), so we compare instants. An unreadable or
 * missing timestamp sorts last.
 */
export function instant(iso) {
  const t = present(iso) ? Date.parse(iso) : Number.NaN;
  return Number.isNaN(t) ? Number.NEGATIVE_INFINITY : t;
}

/** Comparator: newest first by the given timestamp getter. */
export const newestFirst = (at) => (a, b) => instant(at(b)) - instant(at(a));

/** Newest first, split into the decisions that act and the ones that leave a customer alone. */
export function splitDecisions(decisions) {
  const sorted = [...(decisions || [])].sort(newestFirst((d) => d.made_at));
  return {
    acted: sorted.filter((d) => d.kind === "act" && d.action),
    leftAlone: sorted.filter((d) => d.kind !== "act" || !d.action),
  };
}

/** "Email · No offer · follow-up 1": what the decision chose, in the marketer's words. */
export function describeAction(decision, brief) {
  const a = decision?.action;
  if (!a) return "Left alone";
  const parts = [channelLabel(a.channel), offerLabel(brief, a.offer_id)];
  if (a.follow_up_step > 0) parts.push(`follow-up ${a.follow_up_step}`);
  return parts.join(" · ");
}

/** Alerts from /alerts and /health together, once each, for this goal (and account-wide ones), newest first. */
export function mergeAlerts(lists, goalId) {
  const seen = new Map();
  for (const list of lists) for (const a of list || []) if (a && !seen.has(a.alert_id)) seen.set(a.alert_id, a);
  return [...seen.values()]
    .filter((a) => !present(goalId) || !present(a.goal_id) || a.goal_id === goalId)
    .sort(newestFirst((a) => a.at));
}

const EVENT_WORDS = {
  signup: "Signed up",
  visit: "Visited the shop",
  view: "Looked at an item",
  cart: "Added to cart",
  purchase: "Bought",
  refund: "Refunded",
  send: "Sent",
  deliver: "Delivered",
  open: "Opened",
  click: "Clicked",
  reply: "Replied",
  bounce: "Bounced",
  unsubscribe: "Unsubscribed",
  complaint: "Complained (marked as spam)",
  block: "Blocked us",
  fail: "Send failed",
  consent_granted: "Gave consent",
  consent_revoked: "Withdrew consent",
  preference: "Stated a preference",
};

/** One line for an event. Reply text is never shown (the contract says it is never logged). */
export function describeEvent(event, currency) {
  const parts = [EVENT_WORDS[event.type] || String(event.type)];
  if (present(event.channel)) parts.push(channelLabel(event.channel));
  if (present(event.value)) parts.push(money(event.value, currency));
  if (present(event.category)) parts.push(String(event.category));
  if (event.type === "preference" && event.attrs && present(event.attrs.key)) {
    parts.push(`${event.attrs.key} = ${event.attrs.value ?? DASH}`);
  }
  return parts.join(" · ");
}

/** Events and decisions on one time line, newest first. */
export function timeline(bundle) {
  const items = [
    ...(bundle?.events || []).map((e) => ({ at: e.ts, kind: "event", item: e })),
    ...(bundle?.decisions || []).map((d) => ({ at: d.made_at, kind: "decision", item: d })),
  ];
  return items.sort(newestFirst((x) => x.at));
}

// ---- the screen -------------------------------------------------------------------------------------

/** Batches waiting for a person, newest first, then the decided ones, most recently decided first. */
export function splitBatches(batches) {
  const rows = [...(batches || [])];
  return {
    pending: rows.filter((b) => b.status === "pending").sort(newestFirst((b) => b.created_at)),
    decided: rows.filter((b) => b.status !== "pending").sort(newestFirst((b) => b.decided_at || b.created_at)),
  };
}

const BATCH_WORDS = { pending: "Waiting for you", approved: "Approved", rejected: "Rejected", sent: "Sent" };

/** "3 contacts · expected net profit $6.34 · expected spend $0.10 · prepared 20 Oct, 13:00" */
export function batchFacts(batch, currency) {
  const n = batch.n_contacts;
  return [
    `${count(n)} ${Number(n) === 1 ? "contact" : "contacts"}`,
    `expected net profit ${money(batch.expected_net, currency)}`,
    `expected spend ${money(batch.expected_spend, currency)}`,
    `prepared ${when(batch.created_at)}`,
  ].join(" · ");
}

const FEED_PAGE = 20;
const STATUS_WORDS = { active: "Running", paused: "Paused", draft: "Not started", done: "Finished" };
const AUTONOMY_WORDS = { shadow: "Shadow (sends nothing)", approve: "Approve each batch", autopilot: "Autopilot" };
const SEVERITY = {
  critical: { icon: "✕", word: "Critical" },
  warning: { icon: "!", word: "Warning" },
  info: { icon: "i", word: "Info" },
};

export async function render(root, ctx) {
  ensureStyles();
  const api = ctx.api;
  const announcer = h("p", { class: "ap-sr", role: "status", "aria-live": "polite" });
  const page = h("div", { class: "ap-page ap-live" });
  root.replaceChildren(page);

  let bundle;
  try {
    bundle = await resolveGoal(ctx);
  } catch (e) {
    if (notFound(e)) {
      page.append(
        pageHead("Live", `There is no goal ${ctx.goalId}.`),
        h("div", { class: "ap-card" }, empty("It may have been removed, or the link is mistyped. Pick a goal from the switcher at the top."),
          typeof ctx.navigate === "function" && h("button", { class: "ap-btn", type: "button", onclick: () => ctx.navigate("goal") }, "Go to Goal")),
        announcer,
      );
    } else page.append(pageHead("Live"), failed("the goal", e), announcer);
    return;
  }
  if (!bundle || !bundle.goal) {
    page.append(
      pageHead("Live", "Nothing is running yet."),
      h("div", { class: "ap-card" }, empty("Set a goal and approve its plan, and the agent's work shows up here."),
        typeof ctx.navigate === "function" && h("button", { class: "ap-btn", type: "button", onclick: () => ctx.navigate("goal") }, "Set a goal")),
      announcer,
    );
    return;
  }

  let goal = bundle.goal;
  const brief = bundle.brief || null;
  const goalId = goal.goal_id;
  const currency = goal.currency;
  const state = { panelClose: null, batches: undefined, batchesFor: null, batchLoad: null };
  const topbarPause = Boolean(ctx.pauseInTopBar);

  // Header: what goal, its status, and (only when there is no top bar) the one-click pause.
  const statusChip = h("span", { class: "ap-chip" });
  const autonomyChip = h("span", { class: "ap-chip ap-chip-quiet" });
  const pauseBtn = h("button", { type: "button", class: "ap-btn" });
  const pauseHint = h("p", { class: "ap-muted ap-small" });
  const paintHeader = () => {
    statusChip.textContent = STATUS_WORDS[goal.status] || String(goal.status || DASH);
    statusChip.setAttribute("class", `ap-chip ap-status-${goal.status || "unknown"}`);
    autonomyChip.textContent = `Autonomy: ${AUTONOMY_WORDS[goal.autonomy] || goal.autonomy || DASH}`;
    const paused = goal.status === "paused";
    const canToggle = goal.status === "active" || paused;
    pauseBtn.textContent = paused ? "Resume" : "Pause everything";
    pauseBtn.setAttribute("class", paused ? "ap-btn ap-btn-primary" : "ap-btn ap-btn-danger");
    pauseBtn.disabled = !canToggle;
    pauseBtn.setAttribute("aria-pressed", paused ? "true" : "false");
    if (topbarPause) {
      pauseHint.textContent = canToggle
        ? paused
          ? "Paused: the agent decides nothing and sends nothing until you press Resume at the top."
          : "Running. Pause, at the top, stops every send for this goal at once."
        : "This goal is not running yet, so there is nothing to pause.";
    } else {
      pauseHint.textContent = canToggle
        ? paused
          ? "Paused: the agent decides nothing and sends nothing until you resume."
          : "Stops every send for this goal at once. You can resume any time."
        : "This goal is not running, so there is nothing to pause.";
    }
    paintBatches();
  };
  // Batches: shown only while the goal is at "approve each batch", loaded once per goal.
  const batchSec = section("Waiting for your approval", "ap-batches");
  const batchSlot = h("div", { class: "ap-batch-slot" });
  function paintBatches() {
    if (goal.autonomy !== "approve") {
      batchSlot.replaceChildren();
      return;
    }
    if (batchSec.card.parentNode !== batchSlot) batchSlot.replaceChildren(batchSec.card);
    if (state.batchesFor === goalId) return;
    state.batchesFor = goalId;
    state.batchLoad = loadBatches();
  }
  async function loadBatches() {
    batchSec.body.replaceChildren(h("p", { class: "ap-muted" }, "Loading…"));
    let rows;
    try {
      // An API client without batches (an older build) is "not listed yet", not an error.
      rows = typeof api?.listBatches === "function" ? await api.listBatches(goalId) : null;
    } catch (e) {
      state.batchesFor = null; // try again on the next change
      batchSec.heading.textContent = "Waiting for your approval";
      batchSec.body.replaceChildren(failed("the batches waiting for you", e));
      return;
    }
    state.batches = rows;
    drawBatches();
  }
  function drawBatches() {
    const rows = state.batches;
    const pending = rows ? splitBatches(rows).pending.length : 0;
    batchSec.heading.textContent = pending ? `Waiting for your approval (${pending})` : "Waiting for your approval";
    batchSec.body.replaceChildren(...batchesView(rows, currency, decide));
  }
  async function decide(batch, approving, buttons) {
    for (const b of buttons) b.disabled = true;
    try {
      const updated = await call(api, approving ? "approveBatch" : "rejectBatch", batch.batch_id);
      const next = updated && updated.batch_id ? updated : { ...batch, status: approving ? "approved" : "rejected" };
      state.batches = (state.batches || []).map((b) => (b.batch_id === next.batch_id ? next : b));
      notify(ctx, approving ? `Approved. ${count(batch.n_contacts)} ${Number(batch.n_contacts) === 1 ? "message goes" : "messages go"} out inside the plan.` : "Rejected. Nothing in that batch will be sent.", approving ? "good" : "info", announcer);
      drawBatches();
    } catch (e) {
      notify(ctx, `Couldn't ${approving ? "approve" : "reject"} the batch: ${errText(e)}`, "critical", announcer);
      for (const b of buttons) b.disabled = false;
    }
  }

  pauseBtn.addEventListener("click", async () => {
    const resuming = goal.status === "paused";
    pauseBtn.disabled = true;
    pauseBtn.setAttribute("aria-busy", "true");
    try {
      const updated = await call(api, resuming ? "resumeGoal" : "pauseGoal", goalId);
      goal = updated && updated.goal_id ? updated : { ...goal, status: resuming ? "active" : "paused" };
      notify(ctx, resuming ? "Resumed. The agent is working again." : "Paused. Nothing more will be sent.", resuming ? "good" : "warning", announcer);
    } catch (e) {
      notify(ctx, `Couldn't ${resuming ? "resume" : "pause"}: ${errText(e)}`, "critical", announcer);
    } finally {
      pauseBtn.removeAttribute("aria-busy");
      paintHeader();
    }
  });
  paintHeader();
  // The top bar can pause or resume this goal too; api.js tells every subscriber, so follow it here.
  // The subscription ends the first time it fires after this screen has left the page.
  if (typeof api?.onGoalChange === "function") {
    const stop = api.onGoalChange((changed) => {
      if (page.isConnected === false) {
        if (typeof stop === "function") stop();
        return;
      }
      if (changed && changed.goal_id === goalId) {
        goal = changed;
        paintHeader();
      }
    });
  }

  const header = h(
    "header",
    { class: "ap-head ap-live-head" },
    h("div", { class: "ap-head-text" }, h("h1", { class: "ap-h1" }, "Live"), h("p", { class: "ap-sub" }, goal.text || goalId),
      h("div", { class: "ap-chips" }, statusChip, autonomyChip)),
    topbarPause ? h("div", { class: "ap-pause" }, pauseHint) : h("div", { class: "ap-pause" }, pauseBtn, pauseHint),
  );

  const briefingSec = section("Today's briefing", "ap-briefing");
  const healthSec = section("Customer health");
  const inboxSec = section("Inbox");
  const feedSec = section("Activity");
  page.append(
    header,
    h("div", { class: "ap-grid" }, h("div", { class: "ap-col-main" }, batchSlot, briefingSec.card, feedSec.card),
      h("div", { class: "ap-col-side" }, inboxSec.card, healthSec.card)),
    announcer,
  );

  const openCustomer = (customerId, opener) => {
    if (state.panelClose) state.panelClose();
    state.panelClose = customerPanel(page, ctx, customerId, { currency, brief }, opener, () => (state.panelClose = null));
  };

  const [briefing, decisions, health, alerts] = await Promise.allSettled([
    call(api, "getBriefing", goalId),
    call(api, "listDecisions", { goal_id: goalId, limit: 100 }),
    call(api, "getHealth"),
    call(api, "listAlerts"),
  ]);

  // Briefing
  if (briefing.status === "rejected") briefingSec.body.replaceChildren(failed("today's briefing", briefing.reason));
  else briefingSec.body.replaceChildren(...briefingView(briefing.value));

  // Activity feed
  if (decisions.status === "rejected") feedSec.body.replaceChildren(failed("the activity feed", decisions.reason));
  else feedSec.body.replaceChildren(...feedView(decisions.value, brief, openCustomer));

  // Inbox: alerts from both endpoints; one failing still shows the other's.
  const lists = [];
  if (alerts.status === "fulfilled") lists.push(alerts.value);
  if (health.status === "fulfilled" && health.value) lists.push(health.value.alerts);
  const merged = mergeAlerts(lists, goalId);
  inboxSec.heading.textContent = merged.length ? `Inbox (${merged.length})` : "Inbox";
  inboxSec.body.replaceChildren(
    ...(alerts.status === "rejected" ? [failed("alerts", alerts.reason)] : []),
    ...inboxView(merged),
  );

  // Health
  if (health.status === "rejected") healthSec.body.replaceChildren(failed("customer health", health.reason));
  else healthSec.body.replaceChildren(...healthView(health.value, brief));

  if (state.batchLoad) await state.batchLoad;
}

function batchesView(batches, currency, decide) {
  if (batches === null) {
    return [empty("Batches to approve will be listed here. This server does not list them yet, so check back after the next update.")];
  }
  const { pending, decided } = splitBatches(batches);
  const out = [];
  if (!pending.length) out.push(empty("Nothing is waiting for you. The agent holds each new batch here until you approve it."));
  else {
    out.push(h("p", { class: "ap-muted ap-small" }, "Nothing in a batch is sent until you approve it. Each message still follows the plan's limits."));
    out.push(h("ul", { class: "ap-feed ap-batches-list", "aria-label": "Batches waiting for approval, newest first" }, ...pending.map((b) => {
      const label = b.summary || `A batch of ${count(b.n_contacts)} contacts`;
      const approve = h("button", { type: "button", class: "ap-btn ap-btn-primary", "aria-label": `Approve batch: ${label}` }, "Approve");
      const reject = h("button", { type: "button", class: "ap-btn", "aria-label": `Reject batch: ${label}` }, "Reject");
      approve.addEventListener("click", () => decide(b, true, [approve, reject]));
      reject.addEventListener("click", () => decide(b, false, [approve, reject]));
      return h("li", { class: "ap-feed-item ap-batch" },
        h("p", { class: "ap-what ap-batch-summary" }, label),
        h("p", { class: "ap-muted ap-small" }, batchFacts(b, currency)),
        h("div", { class: "ap-actions" }, approve, reject));
    })));
  }
  if (decided.length) {
    out.push(h("details", { class: "ap-left-alone" },
      h("summary", null, `Decided (${decided.length})`),
      h("ul", { class: "ap-feed ap-feed-quiet ap-batches-list" }, ...decided.map((b) =>
        h("li", { class: "ap-feed-item ap-batch" },
          h("p", null, h("span", { class: `ap-chip ap-chip-quiet ap-batch-${b.status}` }, BATCH_WORDS[b.status] || String(b.status)), " ", b.summary || `A batch of ${count(b.n_contacts)} contacts`),
          h("p", { class: "ap-muted ap-small" }, [batchFacts(b, currency), present(b.decided_at) ? `decided ${when(b.decided_at)}${present(b.decided_by) ? ` by ${b.decided_by}` : ""}` : null].filter(Boolean).join(" · ")))))));
  }
  return out;
}

function briefingView(briefing) {
  if (!briefing) return [empty("No briefing yet. The first one arrives after the agent's first full day.")];
  return [
    h("p", { class: "ap-headline" }, briefing.headline || DASH),
    h("p", { class: "ap-muted ap-small" }, `${briefing.period === "weekly" ? "Weekly" : "Daily"} briefing · as of ${when(briefing.as_of)}`),
    ...(briefing.sections || []).map((sec) =>
      h("div", { class: "ap-brief-sec" }, h("h3", { class: "ap-h3" }, sec.title || DASH), h("p", null, sec.body || DASH)),
    ),
  ];
}

function feedView(decisions, brief, openCustomer) {
  const { acted, leftAlone } = splitDecisions(decisions);
  if (!acted.length && !leftAlone.length) return [empty("No decisions yet. They appear here as the agent makes them.")];

  const list = h("ol", { class: "ap-feed", "aria-label": "Customers contacted, newest first" });
  let shown = 0;
  const more = h("button", { type: "button", class: "ap-btn ap-btn-quiet" });
  const showMore = () => {
    acted.slice(shown, shown + FEED_PAGE).forEach((d) => list.append(feedItem(d, brief, openCustomer)));
    shown = Math.min(acted.length, shown + FEED_PAGE);
    more.hidden = shown >= acted.length;
    more.textContent = `Show ${Math.min(FEED_PAGE, acted.length - shown)} more`;
  };
  more.addEventListener("click", showMore);
  showMore();

  const aloneList = h("ol", { class: "ap-feed ap-feed-quiet" }, ...leftAlone.map((d) => feedItem(d, brief, openCustomer)));
  const alone = h(
    "details",
    { class: "ap-left-alone" },
    h("summary", null, `Left alone (${leftAlone.length})`),
    h("p", { class: "ap-muted ap-small" }, "Choosing not to contact someone is a decision too. Each one says why."),
    aloneList,
  );

  // replaceChildren() turns `false` into the text "false", so drop the parts that are not shown.
  return [
    acted.length ? list : empty("Nobody contacted in this window."),
    acted.length > FEED_PAGE ? more : null,
    leftAlone.length > 0 ? alone : null,
  ].filter(Boolean);
}

function feedItem(d, brief, openCustomer) {
  const acted = d.kind === "act" && d.action;
  const who = h("button", { type: "button", class: "ap-link", "aria-label": `Open customer ${d.customer_id}` }, d.customer_id);
  who.addEventListener("click", () => openCustomer(d.customer_id, who));
  const meta = [`Decided ${when(d.made_at)}`];
  if (acted) meta.push(`send at ${when(d.action.send_at)}`);
  return h(
    "li",
    { class: `ap-feed-item ${acted ? "" : "ap-feed-nothing"}`.trim() },
    h("div", { class: "ap-feed-top" }, who, h("span", { class: "ap-what" }, describeAction(d, brief)),
      d.group && d.group !== "agent" && h("span", { class: "ap-chip ap-chip-quiet" }, groupLabel(d.group))),
    h("p", { class: "ap-why" }, d.reason || DASH),
    h("p", { class: "ap-muted ap-small" }, meta.join(" · ")),
  );
}

function inboxView(alerts) {
  if (!alerts.length) return [empty("Nothing needs you. Questions and automatic pauses land here.")];
  return [
    h(
      "ul",
      { class: "ap-inbox" },
      ...alerts.map((a) => {
        const sev = SEVERITY[a.severity] || SEVERITY.info;
        return h(
          "li",
          { class: `ap-alert ap-sev-${a.severity || "info"}` },
          h("div", { class: "ap-alert-top" },
            h("span", { class: "ap-sev" }, h("span", { class: "ap-sev-icon", "aria-hidden": "true" }, sev.icon), sev.word),
            a.auto_paused && h("span", { class: "ap-chip ap-chip-paused" }, "Auto-paused"),
            h("span", { class: "ap-muted ap-small" }, when(a.at))),
          h("p", null, a.message || DASH),
          !present(a.goal_id) && h("p", { class: "ap-muted ap-small" }, "Affects every goal"),
        );
      }),
    ),
  ];
}

/** The brief's stop-rule threshold for a metric, if the plan has one. */
function stopAt(brief, metric) {
  const rule = (brief?.envelope?.stop_rules || []).find((r) => r.metric === metric);
  return rule ? rule.threshold : null;
}

function healthView(health, brief) {
  if (!health) return [empty("No health numbers yet.")];
  const cap = brief?.envelope?.max_contacts_per_week;
  const deliveryFloor = stopAt(brief, "delivery_rate_below");
  const complaintCeil = stopAt(brief, "complaint_rate");
  const tiles = [
    { label: "Delivered", value: pct(health.delivery_rate), note: present(deliveryFloor) ? `Pauses the channel below ${pct(deliveryFloor)}` : null,
      bad: present(health.delivery_rate) && present(deliveryFloor) && health.delivery_rate < deliveryFloor },
    { label: "Complaints", value: pct(health.complaint_rate), note: present(complaintCeil) ? `Pauses the goal above ${pct(complaintCeil)}` : null,
      bad: present(health.complaint_rate) && present(complaintCeil) && health.complaint_rate > complaintCeil },
    { label: "Unsubscribes", value: pct(health.unsubscribe_rate), note: null, bad: false },
    { label: "Blocks", value: pct(health.block_rate), note: null, bad: false },
    { label: "Contacts per customer per week", value: num(health.contacts_per_customer_week, 2),
      note: present(cap) ? `Hard cap ${cap} per customer` : null,
      bad: present(health.contacts_per_customer_week) && present(cap) && health.contacts_per_customer_week > cap },
  ];
  return [
    h("dl", { class: "ap-tiles" }, ...tiles.map((t) =>
      h("div", { class: `ap-tile ${t.bad ? "ap-tile-bad" : ""}`.trim() },
        h("dt", null, t.label),
        h("dd", { class: "ap-tile-value" }, t.value, t.bad && h("span", { class: "ap-tile-flag" }, " · over the limit")),
        t.note && h("dd", { class: "ap-muted ap-small" }, t.note)),
    )),
    h("p", { class: "ap-muted ap-small" }, `As of ${when(health.as_of)}`),
  ];
}

// ---- the customer side panel ------------------------------------------------------------------------

/**
 * Open the side panel for one customer; returns its close function. Focus moves into the panel and
 * back to the button that opened it; Escape and the backdrop close it.
 */
function customerPanel(host, ctx, customerId, { currency, brief }, opener, onClosed) {
  const title = h("h2", { class: "ap-h2", id: "ap-panel-title" }, `Customer ${customerId}`);
  const closeBtn = h("button", { type: "button", class: "ap-btn ap-btn-quiet", "aria-label": "Close customer panel" }, "Close");
  const body = h("div", { class: "ap-panel-body" }, h("p", { class: "ap-muted" }, "Loading…"));
  const panel = h("aside", { class: "ap-panel", role: "dialog", "aria-modal": "true", "aria-labelledby": "ap-panel-title", tabindex: "-1" },
    h("div", { class: "ap-panel-head" }, title, closeBtn), body);
  const backdrop = h("div", { class: "ap-backdrop" });
  let open = true;
  const onKey = (e) => {
    if (e.key === "Escape") close();
  };
  function close() {
    if (!open) return;
    open = false;
    document.removeEventListener("keydown", onKey);
    panel.remove();
    backdrop.remove();
    if (opener && typeof opener.focus === "function") opener.focus();
    onClosed();
  }
  closeBtn.addEventListener("click", close);
  backdrop.addEventListener("click", close);
  document.addEventListener("keydown", onKey);
  host.append(backdrop, panel);
  closeBtn.focus();

  call(ctx.api, "getCustomer", customerId).then(
    (bundle) => {
      if (!open) return;
      body.replaceChildren(...customerView(bundle, currency, brief));
    },
    (e) => {
      if (!open) return;
      body.replaceChildren(notFound(e) ? empty(`No details for customer ${customerId} yet.`) : failed(`customer ${customerId}`, e));
    },
  );
  return close;
}

function customerView(bundle, currency, brief) {
  const st = bundle?.state;
  if (!st) return [empty("We have no record of this customer.")];
  const rows = [
    ["Group", groupLabel(st.group)],
    ["Segment", present(st.segment) ? st.segment : DASH],
    ["Where", [st.country, st.timezone].filter(present).join(" · ") || DASH],
    ["Purchases", count(st.purchases)],
    ["Revenue", money(st.revenue, currency)],
    ["Average order", st.purchases ? money(st.revenue / st.purchases, currency) : DASH],
    ["Last purchase", day(st.last_purchase_at)],
    ["Top categories", (st.top_categories || []).join(", ") || DASH],
    ["Contacts (7 / 30 days)", `${count(st.contacts_7d)} / ${count(st.contacts_30d)}`],
    ["Last contact", when(st.last_contact_at)],
    ["Offer sensitivity", present(st.offer_sensitivity) ? `${num(st.offer_sensitivity, 2)} (0 buys at full price, 1 needs an offer)` : DASH],
  ];
  for (const [k, v] of Object.entries(st.scores || {})) rows.push([`Score: ${k}`, num(v, 3)]);
  for (const [k, v] of Object.entries(st.preferences || {})) rows.push([`Preference: ${k}`, String(v)]);
  if (st.open_complaint) rows.push(["Open complaint", "Yes"]);

  const channels = Object.entries(st.channels || {}).map(([ch, cs]) => {
    const reach = cs.unsubscribed ? "unsubscribed" : !cs.address_known ? "no address" : !cs.consent ? "no consent" : "can contact";
    const counts = `${count(cs.sends)} sent · ${count(cs.delivers)} delivered · ${count(cs.clicks)} clicks · ${count(cs.replies)} replies`;
    return h("li", null, h("strong", null, channelLabel(ch)), `: ${reach} · ${counts}`);
  });

  const items = timeline(bundle).map(({ at, kind, item }) => {
    if (kind === "event") {
      return h("li", { class: "ap-tl-item ap-tl-event" },
        h("span", { class: "ap-tl-when" }, when(at)),
        h("span", null, describeEvent(item, currency)),
        present(item.decision_id) && h("span", { class: "ap-muted ap-small" }, ` · from ${item.decision_id}`));
    }
    const facts = [`chance of this pick ${pct(item.probability)}`, `expected net ${money(item.expected_net, currency)}`, groupLabel(item.group)];
    return h("li", { class: "ap-tl-item ap-tl-decision" },
      h("span", { class: "ap-tl-when" }, when(at)),
      h("span", { class: "ap-what" }, item.kind === "act" ? `Decided: ${describeAction(item, brief)}` : "Decided: leave alone"),
      h("p", { class: "ap-why" }, h("span", { class: "ap-why-label" }, "Why: "), item.reason || DASH),
      h("p", { class: "ap-muted ap-small" }, facts.join(" · ")),
      (item.blocked_by || []).length > 0 && h("p", { class: "ap-muted ap-small" }, `Ruled out by: ${item.blocked_by.join(", ")}`),
      (item.bids || []).length > 0 && h("details", { class: "ap-bids" }, h("summary", null, `Options considered (${item.bids.length})`),
        h("ol", null, ...item.bids.map((b) => h("li", null,
          `${describeAction({ action: b.action }, brief)}: expected net ${money(b.expected_net, currency)}, uplift ${pct(b.uplift)}`)))),
    );
  });

  return [
    h("dl", { class: "ap-facts" }, ...rows.map(([k, v]) => h("div", null, h("dt", null, k), h("dd", null, v)))),
    h("h3", { class: "ap-h3" }, "Channels"),
    channels.length ? h("ul", { class: "ap-plain" }, ...channels) : empty("No channels known."),
    h("h3", { class: "ap-h3" }, "Timeline"),
    items.length ? h("ol", { class: "ap-timeline" }, ...items) : empty("Nothing has happened yet."),
  ];
}
