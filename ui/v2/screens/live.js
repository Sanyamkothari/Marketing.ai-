/* Live: what the agent is doing right now, and why (PITCH.md screen 4, "A daily briefing").

   From the top, the page answers a business owner's questions in their order:
     - Is it running, and how much is it allowed to do? (status, the autonomy level in one sentence, Pause, and a
       red banner when it paused itself, naming why);
     - What did it do yesterday? The pitch's own sentence, "Yesterday I contacted N customers, left M alone, and
       here's why", with the left-alone reasons counted (GET /v2/today), when it last checked and checks next,
       messages, purchases, the extra profit so far against people we never contact (with its range, or "not sure
       yet"), and the spend against the budget;
     - What is waiting for me? (batches to approve, worded by channel and offer);
     - Who did it contact, with what, and what really happened? (the feed: name, Would send / Waiting for your
       approval / Scheduled / Sent / Delivered / Failed, channel, offer, message, image, send time, how many
       messages this week, and the reason; "left alone" is a decision with its own reason, folded under a count);
     - What did it learn, and are customers OK? (learning notes, the inbox, health against each safety stop).
   Tapping a customer opens a panel with what it knows about them in words (reading hours, offer habit, refill
   due, what they might like next), every decision with its reason, the options it weighed and why each lost.

   Pausing: the top bar's Pause stops every goal (ctx.pauseAllInTopBar), so the page keeps a per-goal "Pause this
   goal". With ctx.pauseInTopBar the page only shows the goal's state, following api.onGoalChange.

   Rules this file keeps, because the marketer reads these numbers as facts:
     - every number comes from the API (or the mock files that mirror it); a value it did not send is an em dash,
       never a guess or a zero, and nothing here adds numbers up into new ones;
     - money is in the goal's currency; a measured result is shown with its range, and "not sure yet" when the
       range includes zero; a forecast is called a forecast;
     - each section loads on its own, so one failing (or missing) endpoint never blanks the page; an API build
       without the newer endpoints (/today, /activity, /customers/{id}/insight, /health/limits, batch
       breakdowns) still gets the older view of each section.

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
  // One precision for the three: when any of them is $100 or more, all are shown in whole units, so a range
  // never reads "$823.54 to $3,254".
  const whole = [est.point, est.low, est.high].some((v) => Math.abs(Number(v)) >= 100);
  const f = (v) => fmt(whole ? Math.round(Number(v)) : v);
  return `${f(est.point)} (range ${f(est.low)} to ${f(est.high)})`;
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

/**
 * One line for an event. Reply text is never shown (the contract says it is never logged). `items` (item_id ->
 * name, from the customer insight) names the product when the data has its name.
 */
export function describeEvent(event, currency, items = null) {
  const parts = [EVENT_WORDS[event.type] || String(event.type)];
  if (present(event.channel)) parts.push(channelLabel(event.channel));
  if (present(event.value)) parts.push(money(event.value, currency));
  const itemName = present(event.item_id) && items ? items[event.item_id] : null;
  if (present(itemName)) parts.push(String(itemName));
  else if (present(event.category)) parts.push(humanize(event.category));
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

// ---- plain words for the Live screen (pure, exported for tests) -------------------------------------------

const FEED_PAGE = 20;
const FEED_LIMIT = 100;
const STATUS_WORDS = { active: "Running", paused: "Paused", draft: "Not started", done: "Finished" };
const AUTONOMY_WORDS = { shadow: "Shadow (sends nothing)", approve: "Approve each batch", autopilot: "Autopilot" };
const AUTONOMY_SENTENCE = {
  shadow: "Shadow: it decides and shows you what it would do, but sends nothing.",
  approve: "Approve: nothing goes out until you approve each batch, with one click.",
  autopilot: "Autopilot: it acts on its own, inside the limits you set in the plan.",
};
const SEVERITY = {
  critical: { icon: "✕", word: "Critical" },
  warning: { icon: "!", word: "Warning" },
  info: { icon: "i", word: "Info" },
};
/** The groups in a business owner's words (operator.js keeps the short `groupLabel`). */
const GROUP_WORDS = {
  agent: "Looked after by Autonomy",
  baseline: "Simple rule (the group we compare against)",
  holdout: "People we never contact (to measure the difference)",
};
const GROUP_CHIP = { baseline: "Simple rule group", holdout: "People we never contact" };

/** What really happened to a decision to send, as a short label and a tone for its chip. */
const DELIVERY = {
  shadow: { word: "Would send (Shadow)", tone: "quiet" },
  held: { word: "Waiting for your approval", tone: "warn" },
  approved: { word: "Approved, goes out on the next run", tone: "info" },
  rejected: { word: "Not sent: batch turned down", tone: "quiet" },
  queued: { word: "Scheduled", tone: "info" },
  sent: { word: "Sent", tone: "good" },
  delivered: { word: "Delivered", tone: "good" },
  failed: { word: "Failed to deliver", tone: "bad" },
  not_sent: { word: "Not sent", tone: "quiet" },
};

/**
 * The chip for an ACT decision: the API's delivery state when it sent one; with none, "Would send (Shadow)" only
 * while the goal is in Shadow (which sends nothing), else no chip, because we do not know.
 */
export function deliveryLabel(delivery, autonomy) {
  if (delivery && DELIVERY[delivery.state]) return { ...DELIVERY[delivery.state], state: delivery.state, words: delivery.words || null };
  if (autonomy === "shadow") return { ...DELIVERY.shadow, state: "shadow", words: null };
  return null;
}

/** The rule codes on `blocked_by`, in words. Channel-specific ones read "WhatsApp: has not agreed to messages". */
const RULE_WORDS = {
  no_consent: "has not agreed to messages",
  unsubscribed: "unsubscribed",
  no_address: "no address or number for them",
  channel_paused: "paused right now",
  channel_not_allowed: "not allowed by your plan",
  weekly_cap: "already contacted as often as the weekly limit allows",
  max_contacts_per_week: "already contacted as often as the weekly limit allows",
  too_soon: "messaged only hours ago",
  quiet_hours: "quiet hours",
  open_complaint: "has an open complaint, so no promotions",
  snoozed: "asked us to pause messages for now",
  no_budget: "the budget is used up",
  pacing: "slowing down to stay under the safety limits",
  holdout: "in the group we never contact",
  offer_needs_approval: "that offer needs your approval first",
  offer_not_allowed: "that offer is not in your plan",
  no_brief: "no approved plan covers them",
  no_discounts: "asked for no discounts",
};

const humanize = (code) => {
  const s = String(code).replace(/[_-]+/g, " ").trim();
  return s ? s.charAt(0).toUpperCase() + s.slice(1) : s;
};
const capital = (s) => (s ? s.charAt(0).toUpperCase() + s.slice(1) : s);

/** "no_consent:whatsapp" -> "WhatsApp: has not agreed to messages"; "preference:no_discounts" -> "Asked for no discounts". */
export function ruleWords(raw) {
  const [code, rest] = String(raw).split(/:(.*)/s);
  if (code === "preference") return rest === "no_discounts" ? "Asked for no discounts" : `Asked for: ${humanize(rest || "")}`;
  const words = RULE_WORDS[code];
  if (!words) return humanize(raw);
  if (rest && CHANNEL_CODES.has(rest)) return `${channelLabel(rest)}: ${words}`;
  return capital(words);
}
const CHANNEL_CODES = new Set(["email", "whatsapp", "sms", "push", "web", "webhook"]);

/** A day label from the counted date ("2026-10-19") and the API's as_of: Yesterday, Today so far, or On 19 Oct. */
export function dayName(date, asOf) {
  const d = present(date) ? Date.parse(`${String(date).slice(0, 10)}T00:00:00Z`) : Number.NaN;
  const a = present(asOf) ? Date.parse(`${String(asOf).slice(0, 10)}T00:00:00Z`) : Number.NaN;
  const gap = Number.isNaN(d) || Number.isNaN(a) ? null : Math.round((a - d) / 86400000);
  if (gap === 1) return { title: "Yesterday", lead: "Yesterday", in: "yesterday" };
  if (gap === 0) return { title: "Today so far", lead: "So far today", in: "today" };
  const on = Number.isNaN(d) ? "that day" : new Date(d).toLocaleDateString("en-GB", { day: "numeric", month: "short", timeZone: "UTC" });
  return { title: `On ${on}`, lead: `On ${on}`, in: `on ${on}` };
}

const people = (n) => `${count(n)} ${Number(n) === 1 ? "customer" : "customers"}`;

/**
 * The pitch's sentence, true to the autonomy level: "Yesterday I contacted 412 customers, left 1,688 alone, and
 * here's why." Shadow says it would have (it sent nothing); Approve says it picked them (each batch waits).
 */
export function briefingSentence(today) {
  if (!today) return DASH;
  const d = dayName(today.date, today.as_of);
  const alone = `left ${count(today.left_alone)} alone, and here's why.`;
  if (today.sends_nothing || today.autonomy === "shadow") {
    return `${d.lead} I would have contacted ${people(today.contacted)} (Shadow mode, so nothing was sent), ${alone}`;
  }
  if (today.autonomy === "approve") {
    return `${d.lead} I picked ${people(today.contacted)} to contact (each batch waits for your approval), ${alone}`;
  }
  return `${d.lead} I contacted ${people(today.contacted)}, ${alone}`;
}

/** "Made a decision for each of 2,100 customers yesterday. Last check: 19 Oct, 09:00. Next check: 21 Oct, 09:00." */
export function checkedLine(today) {
  if (!today) return DASH;
  const d = dayName(today.date, today.as_of);
  const parts = [`Made a decision for each of ${people(today.customers_considered)} ${d.in}, one by one.`];
  if (present(today.last_run_at)) parts.push(`Last check: ${when(today.last_run_at)}.`);
  if (today.run_overdue && present(today.next_run_at)) parts.push(`The next check was due ${when(today.next_run_at)} and has not run yet.`);
  else if (present(today.next_run_at)) parts.push(`Next check: ${when(today.next_run_at)}.`);
  else parts.push("This server has no daily schedule, so the time of the next check is not known.");
  return parts.join(" ");
}

/**
 * The extra profit so far against people we never contact, from GET /v2/proof. Never a bare number: with its
 * range, and "Not sure yet" when there is no estimate or its range includes zero.
 */
export function profitWords(proof, currency) {
  const est = proof && proof.incremental_profit;
  if (!est || !present(est.point) || !present(est.low) || !present(est.high)) {
    return { sure: false, value: "Not sure yet", note: "Too few customers measured so far to compare with the people we never contact." };
  }
  const fmt = (v) => money(v, currency);
  const r = range(est, fmt);
  if (Number(est.low) <= 0 && Number(est.high) >= 0) {
    return { sure: false, value: "Not sure yet", note: `Best estimate ${r}. The range includes zero, so it may not have made extra profit yet.` };
  }
  if (Number(est.high) < 0) return { sure: true, value: r, note: "Less profit than leaving these customers alone, so far. See Proof." };
  return { sure: true, value: r, note: "More than the people we never contact made, so far. The range is how sure it is." };
}

/** Uplift (a change in the chance of buying, -1..1) as "raises the chance they buy by about 5.2 in 100". */
export function upliftWords(uplift) {
  if (!present(uplift)) return null;
  const pts = Number(uplift) * 100;
  const size = Math.abs(pts) < 10 ? num(Math.abs(pts), 1) : String(Math.round(Math.abs(pts)));
  if (Math.abs(pts) < 0.05) return "would not change the chance they buy";
  return pts > 0 ? `raises the chance they buy by about ${size} in 100` : `lowers the chance they buy by about ${size} in 100`;
}

/** Money down to the cent, and "under $0.01" for a cost that would read $0.00 (a $0.001 email). */
export function fineMoney(n, currency) {
  if (!present(n)) return DASH;
  const v = Number(n);
  if (v !== 0 && Math.abs(v) < 0.005) return `${v < 0 ? "-" : ""}under ${money(0.01, currency)}`;
  return money(Math.round(v * 100) / 100, currency);
}

/** An expected profit in words: "adds $0.72 in profit after costs", or "loses $0.22 after costs" (never "adds -$0.22"). */
export function netWords(n, currency) {
  if (!present(n)) return DASH;
  const v = Number(n);
  return v < 0 ? `loses ${fineMoney(-v, currency)} after costs` : `adds ${fineMoney(v, currency)} in profit after costs`;
}

const sameAction = (a, b) =>
  Boolean(a && b) && a.channel === b.channel && a.offer_id === b.offer_id && a.content_id === b.content_id && a.send_at === b.send_at;

/** Why one option it weighed won or lost, from the decision's own numbers (no new arithmetic beyond comparing). */
export function bidVerdict(bid, decision) {
  if (decision && decision.kind === "act" && sameAction(bid.action, decision.action)) return "Chosen";
  if (Number(bid.expected_net) <= 0) return "Not worth it: it would lose money after its costs";
  if (present(decision?.attention_price) && Number(bid.expected_net) <= Number(decision.attention_price)) {
    return "Not worth it: less than the cost of bothering them again";
  }
  if (decision && decision.kind === "act" && Number(bid.expected_net) > Number(decision.expected_net)) {
    return "Not chosen this time (the reason above says why)";
  }
  return decision && decision.kind === "act" ? "Not chosen: the chosen one was expected to add more" : "Not used";
}

/** "Due for a refill of House blend coffee, 1 kg in about 84 days" (or "now", or "overdue by 3 days"). */
export function refillWords(refill) {
  if (!refill) return null;
  const what = present(refill.item_name) ? refill.item_name : "something they buy again and again";
  const days = Number(refill.days_until_due);
  if (!Number.isFinite(days)) return `Due for a refill of ${what}`;
  if (days < -0.5) return `Overdue for a refill of ${what}, by about ${count(Math.abs(days))} ${Math.round(Math.abs(days)) === 1 ? "day" : "days"}`;
  if (refill.window_open || days <= 0.5) return `Due for a refill of ${what} now`;
  return `Due for a refill of ${what} in about ${count(days)} ${Math.round(days) === 1 ? "day" : "days"}`;
}

/** Offer habit from the state's offer_sensitivity (0..1) when the insight endpoint is not there. Words, with the number. */
export function offerHabitFromSensitivity(s) {
  if (!present(s)) return null;
  const v = Number(s);
  const words = v < 0.2 ? "Rarely needs an offer to buy" : v < 0.6 ? "Sometimes buys because of an offer" : "Usually needs an offer to buy";
  return `${words} (offer sensitivity ${num(v, 2)}, where 0 always pays full price and 1 needs an offer)`;
}

/** How a fill-in reads when the message is shown before it is filled for one person. */
const FILL_WORDS = { first_name: "first name", top_category: "their usual category", offer_label: "the offer" };

/**
 * A message subject (or text) with its fill-ins worded: "We miss you, {{first_name}}" -> "We miss you, [first
 * name]". `offer` is the offer's own label from the plan, used for {{offer_label}} when it is known.
 */
export const subjectWords = (subject, offer = null) =>
  present(subject)
    ? String(subject).replace(/\{\{\s*([\w.]+)\s*\}\}/g, (_m, k) =>
      k === "offer_label" && present(offer) ? String(offer) : `[${FILL_WORDS[k] || String(k).replace(/[_.]+/g, " ")}]`)
    : null;

/** How a content theme reads ("a how-to message"), the same nouns the decision reasons use. */
const THEME_WORDS = {
  savings: "a savings message", quality: "a quality message", urgency: "a last-chance message",
  social_proof: "a social-proof message", education: "a how-to message", convenience: "a convenience message",
  novelty: "a what's-new message", loyalty: "a loyalty message", reminder: "a reminder", personal: "a personal note",
};

/** The start of a message's text, cut at a word, for channels without a subject (WhatsApp, SMS). */
function snippet(text, max = 90) {
  const t = String(text).replace(/\s+/g, " ").trim();
  if (t.length <= max) return t;
  const cut = t.slice(0, max);
  return `${cut.slice(0, Math.max(cut.lastIndexOf(" "), 40)).replace(/[\s,.;:]+$/, "")}…`;
}

/** The message in words: its subject, else the start of its text, else its theme. Never an internal template id. */
export function messageWords(content, offer = null) {
  if (!content) return null;
  const subject = subjectWords(content.subject, offer);
  if (present(subject)) return `“${subject}”`;
  const body = subjectWords(present(content.body) ? snippet(content.body) : null, offer);
  if (present(body)) return `“${body}”`;
  if (present(content.theme)) return THEME_WORDS[content.theme] || `a ${String(content.theme).replace(/_/g, " ")} message`;
  const tpl = content.template_name;
  if (present(tpl) && !/goal_|_v\d/i.test(String(tpl))) return `the “${humanize(tpl)}” template`;
  return null;
}

/** Only a web address or a path can be an image source (never `javascript:` or `data:` from the API). */
const safeSrc = (url) => (present(url) && /^(https?:\/\/|\/|\.\/)/i.test(String(url)) ? String(url) : null);

/** A feed row: {d: Decision, x: ActivityRow | null}. Newest first, split into contacted and left alone. */
export function splitRows(rows) {
  const sorted = [...(rows || [])].filter((r) => r && r.d).sort(newestFirst((r) => r.d.made_at));
  return {
    acted: sorted.filter((r) => r.d.kind === "act" && r.d.action),
    leftAlone: sorted.filter((r) => r.d.kind !== "act" || !r.d.action),
  };
}

/** Is an alert about a stop rule that pauses the whole goal (not only one channel)? From the plan's stop rules. */
function pausesGoal(alert, brief, limits) {
  const rules = [...(limits?.rules || []), ...(brief?.envelope?.stop_rules || [])];
  const rule = rules.find((r) => r.metric === alert.code);
  return rule ? rule.action === "pause_goal" : false;
}

/** Look up an API method that may not exist on older builds: null when it is missing, never a throw. */
function maybe(api, name, ...args) {
  if (!api || typeof api[name] !== "function") return Promise.resolve(null);
  try {
    return Promise.resolve(api[name](...args));
  } catch (e) {
    return Promise.reject(e);
  }
}

// ---- the screen -------------------------------------------------------------------------------------

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
  const contentIndex = new Map((bundle.content || []).map((c) => [c.content_id, c]));
  const state = { panelClose: null, batches: undefined, batchesFor: null, batchLoad: null, breakdowns: new Map(), alerts: [], limits: null, since: null };
  const topbarPause = Boolean(ctx.pauseInTopBar);
  const pauseWord = ctx.pauseAllInTopBar ? "Pause this goal" : "Pause everything";

  // Header: what goal, its status and autonomy level in one sentence, and (only when there is no top bar) Pause.
  const statusChip = h("span", { class: "ap-chip" });
  const autonomyChip = h("span", { class: "ap-chip ap-chip-quiet lv-autonomy" });
  const autonomyLine = h("p", { class: "ap-muted ap-small lv-autonomy-line" });
  const pauseBtn = h("button", { type: "button", class: "ap-btn" });
  const pauseHint = h("p", { class: "ap-muted ap-small" });
  const selfPause = h("div", { class: "lv-selfpause-slot" });
  const paintHeader = () => {
    statusChip.textContent = STATUS_WORDS[goal.status] || String(goal.status || DASH);
    statusChip.setAttribute("class", `ap-chip ap-status-${goal.status || "unknown"}`);
    autonomyChip.textContent = `Autonomy: ${AUTONOMY_WORDS[goal.autonomy] || goal.autonomy || DASH}`;
    autonomyChip.setAttribute("class", `ap-chip ap-chip-quiet lv-autonomy lv-autonomy-${goal.autonomy || "unknown"}`);
    autonomyLine.textContent = [AUTONOMY_SENTENCE[goal.autonomy], present(state.since) ? `At this level since ${day(state.since)}.` : null].filter(Boolean).join(" ");
    const paused = goal.status === "paused";
    const canToggle = goal.status === "active" || paused;
    pauseBtn.textContent = paused ? "Resume" : pauseWord;
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
    paintSelfPause();
    paintBatches();
  };
  // The red banner when Autonomy paused itself (a stop rule fired), naming why. Drawn from the inbox's alerts.
  function paintSelfPause() {
    const auto = state.alerts.filter((a) => a.auto_paused);
    if (!auto.length) {
      selfPause.replaceChildren();
      return;
    }
    const latestGoalStop = auto.find((a) => a.goal_id === goalId && pausesGoal(a, brief, state.limits));
    const goalStopped = goal.status === "paused" && latestGoalStop;
    const shown = auto.slice(0, 3);
    selfPause.replaceChildren(
      h("section", { class: `lv-selfpause ${goalStopped ? "lv-selfpause-goal" : ""}`.trim(), role: goalStopped ? "alert" : null, "aria-labelledby": "lv-selfpause-title" },
        h("h2", { class: "ap-h2", id: "lv-selfpause-title" },
          h("span", { class: "lv-selfpause-icon", "aria-hidden": "true" }, "❚❚"),
          goalStopped ? "This goal paused itself" : `Autonomy paused some sending on its own (${auto.length})`),
        goalStopped && h("p", { class: "lv-selfpause-why" }, latestGoalStop.message || humanize(latestGoalStop.code)),
        h("ul", { class: "ap-plain lv-selfpause-list" }, ...shown.filter((a) => !goalStopped || a !== latestGoalStop).map((a) =>
          h("li", null, h("span", { class: "ap-muted ap-small" }, `${when(a.at)} · `), a.message || humanize(a.code),
            !present(a.goal_id) && h("span", { class: "ap-muted ap-small" }, " (every goal)")))),
        h("p", { class: "ap-muted ap-small" },
          "It does this by itself when complaints, unsubscribes or delivery problems pass the limits in your plan. ",
          goalStopped ? "Nothing more goes out for this goal until you resume it." : "The rest keeps running. Every pause is in the Inbox.")));
  }

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
    // The batch in plain words ("21 customers: 20 by email, 1 by WhatsApp; 20 with no offer, 1 with 10% off").
    const pending = rows ? splitBatches(rows).pending : [];
    const got = await Promise.allSettled(pending.map((b) => maybe(api, "getBatchBreakdown", b.batch_id)));
    let any = false;
    got.forEach((r, i) => {
      if (r.status === "fulfilled" && r.value && present(r.value.words)) {
        state.breakdowns.set(pending[i].batch_id, r.value);
        any = true;
      }
    });
    if (any) drawBatches();
  }
  function drawBatches() {
    const rows = state.batches;
    const pending = rows ? splitBatches(rows).pending.length : 0;
    batchSec.heading.textContent = pending ? `Waiting for your approval (${pending})` : "Waiting for your approval";
    batchSec.body.replaceChildren(...batchesView(rows, currency, decide, state.breakdowns));
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
      h("div", { class: "ap-chips" }, statusChip, autonomyChip), autonomyLine),
    topbarPause ? h("div", { class: "ap-pause" }, pauseHint) : h("div", { class: "ap-pause" }, pauseBtn, pauseHint),
  );

  const todaySec = section("Yesterday", "lv-today");
  todaySec.heading.setAttribute("id", "lv-today-title");
  const briefingSec = section("The full briefing", "ap-briefing");
  const learnedSec = section("What it learned");
  const healthSec = section("Customer health and safety stops");
  const inboxSec = section("Inbox");
  const feedSec = section("Every decision, with its reason");
  page.append(
    header,
    selfPause,
    h("div", { class: "ap-grid" },
      h("div", { class: "ap-col-main" }, todaySec.card, batchSlot, feedSec.card, briefingSec.card),
      h("div", { class: "ap-col-side" }, inboxSec.card, healthSec.card, learnedSec.card)),
    announcer,
  );

  const openCustomer = (customerId, opener, name = null) => {
    if (state.panelClose) state.panelClose();
    state.panelClose = customerPanel(page, ctx, customerId, { currency, brief, contentIndex, name, autonomy: goal.autonomy }, opener, () => (state.panelClose = null));
  };

  const feedLoad = (async () => {
    // The newer feed (delivery state, names, message, image) when the API has it; else the plain decisions.
    const rows = await maybe(api, "listActivity", { goal_id: goalId, limit: FEED_LIMIT });
    if (Array.isArray(rows)) return { rich: true, rows: rows.filter((r) => r && r.decision).map((r) => ({ d: r.decision, x: r })) };
    const decisions = await call(api, "listDecisions", { goal_id: goalId, limit: FEED_LIMIT });
    return { rich: false, rows: (decisions || []).map((d) => ({ d, x: null })) };
  })();

  const [briefing, feed, health, alerts, today, proof, limits] = await Promise.allSettled([
    call(api, "getBriefing", goalId),
    feedLoad,
    call(api, "getHealth"),
    call(api, "listAlerts"),
    maybe(api, "getToday", goalId),
    maybe(api, "getProof", goalId),
    maybe(api, "getHealthLimits", goalId),
  ]);
  if (limits.status === "fulfilled") state.limits = limits.value;
  const todayValue = today.status === "fulfilled" ? today.value : null;
  if (todayValue && present(todayValue.autonomy_since)) state.since = todayValue.autonomy_since;

  // Yesterday: the pitch's daily briefing for this goal.
  todaySec.heading.textContent = todayValue ? dayName(todayValue.date, todayValue.as_of).title : "Day by day";
  if (today.status === "rejected") todaySec.body.replaceChildren(failed("yesterday's numbers", today.reason));
  else {
    todaySec.body.replaceChildren(...todayView(todayValue, {
      goal, brief, currency, proof: proof.status === "fulfilled" ? proof.value : null, proofError: proof.status === "rejected" ? proof.reason : null,
      navigate: ctx.navigate,
    }));
  }

  // The learning notes: from /today, else the briefing's learning sections.
  const briefingValue = briefing.status === "fulfilled" ? briefing.value : null;
  const learned = (todayValue && todayValue.learned && todayValue.learned.length ? todayValue.learned : null) ||
    (briefingValue?.sections || []).filter((s) => /learn/i.test(String(s.title || ""))).map((s) => s.body).filter(present);
  learnedSec.body.replaceChildren(...learnedView(learned));

  // The full (often weekly) briefing.
  if (briefing.status === "rejected") briefingSec.body.replaceChildren(failed("today's briefing", briefing.reason));
  else briefingSec.body.replaceChildren(...briefingView(briefingValue));

  // Activity feed
  if (feed.status === "rejected") feedSec.body.replaceChildren(failed("the activity feed", feed.reason));
  else feedSec.body.replaceChildren(...feedView(feed.value, { brief, contentIndex, autonomy: goal.autonomy, today: todayValue, openCustomer }));

  // Inbox: alerts from both endpoints; one failing still shows the other's.
  const lists = [];
  if (alerts.status === "fulfilled") lists.push(alerts.value);
  if (health.status === "fulfilled" && health.value) lists.push(health.value.alerts);
  const merged = mergeAlerts(lists, goalId);
  state.alerts = merged;
  inboxSec.heading.textContent = merged.length ? `Inbox (${merged.length})` : "Inbox";
  inboxSec.body.replaceChildren(
    ...(alerts.status === "rejected" ? [failed("alerts", alerts.reason)] : []),
    ...inboxView(merged),
  );
  paintHeader();

  // Health
  if (health.status === "rejected") healthSec.body.replaceChildren(failed("customer health", health.reason));
  else healthSec.body.replaceChildren(...healthView(health.value, brief, state.limits));

  if (state.batchLoad) await state.batchLoad;
}

// ---- the sections -----------------------------------------------------------------------------------

function batchesView(batches, currency, decide, breakdowns = new Map()) {
  if (batches === null) {
    return [empty("Batches to approve will be listed here. This server does not list them yet, so check back after the next update.")];
  }
  const { pending, decided } = splitBatches(batches);
  const out = [];
  if (!pending.length) out.push(empty("Nothing is waiting for you. The agent holds each new batch here until you approve it."));
  else {
    out.push(h("p", { class: "ap-muted ap-small" }, "Nothing in a batch is sent until you approve it. Each message still follows the plan's limits. One click approves the whole batch."));
    out.push(h("ul", { class: "ap-feed ap-batches-list", "aria-label": "Batches waiting for approval, newest first" }, ...pending.map((b) => {
      const words = breakdowns.get(b.batch_id)?.words;
      const label = words || b.summary || `A batch of ${count(b.n_contacts)} contacts`;
      const approve = h("button", { type: "button", class: "ap-btn ap-btn-primary", "aria-label": `Approve batch: ${label}` }, "Approve");
      const reject = h("button", { type: "button", class: "ap-btn", "aria-label": `Reject batch: ${label}` }, "Reject");
      approve.addEventListener("click", () => decide(b, true, [approve, reject]));
      reject.addEventListener("click", () => decide(b, false, [approve, reject]));
      return h("li", { class: "ap-feed-item ap-batch" },
        h("p", { class: "ap-what ap-batch-summary" }, label),
        h("p", { class: "ap-muted ap-small" }, batchFacts(b, currency)),
        h("p", { class: "ap-muted ap-small" }, "Expected numbers are its own forecast before sending, not a result."),
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

/** One big number with its label and a short note. */
const bigNumber = (label, value, note, extra = "") =>
  h("div", { class: `lv-big-item ${extra}`.trim() }, h("dt", null, label), h("dd", { class: "lv-big-value" }, value), note && h("dd", { class: "lv-big-note" }, note));

/** A smaller fact tile in the "what came of it" row. */
const stat = (label, value, note, extra = "") =>
  h("div", { class: `lv-stat ${extra}`.trim() }, h("dt", null, label), h("dd", { class: "lv-stat-value" }, value), note && h("dd", { class: "lv-stat-note" }, note));

function todayView(today, { goal, brief, currency, proof, proofError, navigate }) {
  if (!today) {
    return [empty("The day-by-day count is not available from this server yet. The full briefing below has the overview.")];
  }
  if (!today.customers_considered) {
    return [
      h("p", { class: "lv-say" }, "Nothing was decided for this goal yet."),
      h("p", { class: "ap-muted ap-small" }, checkedLine(today)),
      ...(today.notes || []).filter((n) => !/^Purchases are/.test(n)).map((n) => h("p", { class: "ap-muted ap-small" }, n)),
    ];
  }
  const cur = today.currency || currency;
  const d = dayName(today.date, today.as_of);
  const byGroup = today.contacted_by_group || {};
  const groupNote = present(byGroup.baseline) && byGroup.baseline > 0
    ? `${count(byGroup.agent || 0)} chosen by Autonomy, ${count(byGroup.baseline)} by the simple rule we compare it with.`
    : today.sends_nothing ? "Shadow mode: nobody was actually messaged." : null;
  const shadow = today.sends_nothing || today.autonomy === "shadow";
  const reasons = today.reasons || [];
  const maxN = Math.max(1, ...reasons.map((r) => Number(r.n) || 0));
  const cap = brief?.envelope?.max_contacts_per_week;

  const reasonList = reasons.length
    ? h("ul", { class: "lv-reasons", "aria-label": `Why ${count(today.left_alone)} customers were left alone` }, ...reasons.map((r) =>
      h("li", { class: `lv-reason lv-reason-${r.code}` },
        h("span", { class: "lv-reason-n" }, count(r.n)),
        h("span", { class: "lv-reason-words" }, r.words || humanize(r.code),
          r.code === "weekly_cap" && present(cap) && h("span", { class: "ap-muted" }, ` (your limit: ${cap} a week)`),
          r.code === "no_effect" && h("span", { class: "lv-reason-hint" }, "This is where people who would buy anyway end up: a message, or a discount, would only cost you."),
          r.code === "holdout" && h("span", { class: "lv-reason-hint" }, "A small random group, so the Proof page can show the real difference.")),
        h("span", { class: "lv-reason-bar", "aria-hidden": "true" }, h("span", { style: `width: ${Math.max(2, Math.round((100 * (Number(r.n) || 0)) / maxN))}%` })))))
    : empty("Nobody was left alone that day.");

  const profit = profitWords(proof, cur);
  // The runtime's ledger (what the budget limit counts) prices the messages sent; the discounts customers used
  // are counted on Proof, so the two spend figures differ and the tile says which one it is.
  const spentNote = [
    present(today.budget_left) ? `${money(today.budget_left, cur)} left.` : null,
    "The cost of the messages sent, as the budget limit counts it. Proof's spend also counts the discounts customers used.",
  ].filter(Boolean).join(" ");
  const spentShare = present(today.spend_to_date) && present(today.budget) && Number(today.budget) > 0
    ? Math.min(100, Math.max(0, (100 * Number(today.spend_to_date)) / Number(today.budget))) : null;

  return [
    h("p", { class: "lv-say" }, briefingSentence(today)),
    h("p", { class: "ap-muted ap-small lv-checked" }, checkedLine(today)),
    h("dl", { class: "lv-big" },
      bigNumber(shadow ? "Would have contacted" : today.autonomy === "approve" ? "Picked to contact" : "Contacted", count(today.contacted), groupNote),
      bigNumber("Left alone", count(today.left_alone), "Doing nothing is a decision too. Each one has a reason.", "lv-big-quiet")),
    h("h3", { class: "ap-h3" }, `Why it left ${count(today.left_alone)} alone`),
    reasonList,
    h("p", { class: "ap-muted ap-small" }, "It looks for people whose behaviour a message can change, and weighs the cost of bothering someone every time, so leaving people alone often wins."),
    h("h3", { class: "ap-h3" }, `What came of it ${d.in}`),
    h("dl", { class: "lv-stats" },
      stat("Messages sent", count(today.messages_sent), shadow ? "Shadow mode sends nothing." : today.autonomy === "approve" && !Number(today.messages_sent) ? "None yet: a batch goes out only after you approve it." : present(today.delivered) ? `${count(today.delivered)} delivered` : null),
      stat("Purchases", count(today.purchases), present(today.revenue) ? `${money(today.revenue, cur)} in sales, from every purchase by these customers, not only extra ones` : null),
      stat("Extra profit so far", profit.value, proofError ? `Couldn't load the proof: ${errText(proofError)}` : profit.note, profit.sure ? "lv-stat-sure" : "lv-stat-unsure"),
      stat("Spent on messages", present(today.spend_to_date) ? `${money(today.spend_to_date, cur)} of ${money(today.budget, cur)}` : DASH,
        present(today.spend_to_date) ? spentNote : "Spend is not recorded yet.")),
    spentShare !== null && h("div", { class: "lv-budget", role: "img", "aria-label": `Spent ${money(today.spend_to_date, cur)} of the ${money(today.budget, cur)} budget` },
      h("span", { class: "lv-budget-fill", style: `width: ${spentShare.toFixed(1)}%` })),
    h("p", { class: "ap-muted ap-small" }, "Extra profit is measured against a small random group of customers it never contacts, since the goal started. ",
      typeof navigate === "function" && present(goal?.goal_id)
        ? h("a", { class: "ap-link", href: `#/proof/${encodeURIComponent(goal.goal_id)}` }, "See how it is measured on Proof")
        : null),
    ...(today.notes || []).filter((n) => !/^Purchases are every purchase/.test(n)).map((n) => h("p", { class: "ap-muted ap-small" }, n)),
  ].filter(Boolean);
}

function learnedView(learned) {
  if (!learned || !learned.length) {
    return [empty("Nothing new yet. As it watches what happens after each message, it notes here what it changed.")];
  }
  return [h("ul", { class: "ap-plain lv-learned" }, ...learned.map((t) => h("li", null, t)))];
}

function briefingView(briefing) {
  if (!briefing) return [empty("No briefing yet. The first one arrives after the agent's first full day.")];
  return [
    h("p", { class: "ap-headline" }, briefing.headline || DASH),
    h("p", { class: "ap-muted ap-small" }, `${briefing.period === "weekly" ? "Weekly" : "Daily"} briefing · as of ${when(briefing.as_of)}`),
    ...(briefing.sections || []).map((sec) =>
      h("div", { class: "ap-brief-sec" }, h("h3", { class: "ap-h3" }, sec.title || DASH), h("p", null, sec.body || DASH)),
    ),
    /holdout/i.test([briefing.headline, ...(briefing.sections || []).map((x) => `${x.title} ${x.body}`)].join(" ")) &&
      h("p", { class: "ap-muted ap-small" }, "“The holdout” means the small random group of people we never contact, so the difference can be measured."),
  ].filter(Boolean);
}

function feedView(feed, opts) {
  const { acted, leftAlone } = splitRows(feed?.rows);
  if (!acted.length && !leftAlone.length) return [empty("No decisions yet. They appear here as the agent makes them.")];

  const find = findCustomerForm(opts.openCustomer);
  const intro = h("p", { class: "ap-muted ap-small" },
    `The latest ${count(acted.length + leftAlone.length)} decisions. Tap a customer to see everything it decided for them, and why.`,
    opts.today ? " The full counts for the day are in the card at the top." : "");
  const list = h("ol", { class: "ap-feed", "aria-label": "Customers contacted, newest first" });
  let shown = 0;
  const more = h("button", { type: "button", class: "ap-btn ap-btn-quiet" });
  const showMore = () => {
    acted.slice(shown, shown + FEED_PAGE).forEach((r) => list.append(feedItem(r, opts)));
    shown = Math.min(acted.length, shown + FEED_PAGE);
    more.hidden = shown >= acted.length;
    more.textContent = `Show ${Math.min(FEED_PAGE, acted.length - shown)} more`;
  };
  more.addEventListener("click", showMore);
  showMore();

  const aloneList = h("ol", { class: "ap-feed ap-feed-quiet" }, ...leftAlone.map((r) => feedItem(r, opts)));
  const alone = h(
    "details",
    { class: "ap-left-alone" },
    h("summary", null, `Left alone (${leftAlone.length})`),
    h("p", { class: "ap-muted ap-small" }, "Choosing not to contact someone is a decision too. Each one says why."),
    aloneList,
  );

  // replaceChildren() turns `false` into the text "false", so drop the parts that are not shown.
  return [
    intro,
    find,
    h("h3", { class: "ap-h3" }, `Contacted, in these decisions (${acted.length})`),
    acted.length ? list : empty("Nobody contacted in this window."),
    acted.length > FEED_PAGE ? more : null,
    leftAlone.length > 0 ? alone : null,
  ].filter(Boolean);
}

/** "Open a customer": any customer, by id, not only the ones in the latest decisions. */
function findCustomerForm(openCustomer) {
  const input = h("input", { class: "ap-input", id: "lv-find", name: "customer", type: "search", autocomplete: "off", placeholder: "e.g. cust_1042" });
  const btn = h("button", { type: "submit", class: "ap-btn" }, "Open");
  const form = h("form", { class: "lv-find", role: "search" },
    h("label", { for: "lv-find", class: "ap-field-label" }, "Look up any customer by their id"),
    h("div", { class: "lv-find-row" }, input, btn));
  form.addEventListener("submit", (e) => {
    if (e && typeof e.preventDefault === "function") e.preventDefault();
    const id = String(input.value || "").trim();
    if (id) openCustomer(id, input);
  });
  return form;
}

/** The customer's name when the data has one, with the id small; else the id alone. */
function whoButton(customerId, name, openCustomer) {
  const btn = present(name)
    ? h("button", { type: "button", class: "ap-link lv-who", "aria-label": `Open ${name} (customer ${customerId})` }, name, h("span", { class: "lv-id" }, customerId))
    : h("button", { type: "button", class: "ap-link", "aria-label": `Open customer ${customerId}` }, customerId);
  btn.addEventListener("click", () => openCustomer(customerId, btn, present(name) ? name : null));
  return btn;
}

/** The chosen combination beyond channel and offer: message, send time, product mentioned, messages this week. */
function comboParts(d, x, contentIndex, brief = null) {
  const a = d.action;
  if (!a) return [];
  const content = x?.content || contentIndex.get(a.content_id) || null;
  const parts = [];
  const offer = present(a.offer_id) && a.offer_id !== "none" ? offerLabel(brief, a.offer_id) : null;
  const message = messageWords(content, offer);
  if (present(message)) parts.push(`Message: ${message}`);
  if (present(a.send_at)) parts.push(`send time ${when(a.send_at)}`);
  if (x?.item) parts.push(present(x.item.name) ? `mentions ${x.item.name}` : `mentions product ${x.item.item_id}`);
  if (present(x?.contacts_7d)) parts.push(`${count(x.contacts_7d)} ${Number(x.contacts_7d) === 1 ? "message" : "messages"} to them in the last 7 days`);
  return parts;
}

function chip(label, tone) {
  return h("span", { class: `ap-chip lv-state lv-state-${tone}` }, label);
}

function feedItem(row, opts) {
  const { d, x } = row;
  const acted = d.kind === "act" && d.action;
  const who = whoButton(d.customer_id, x?.display_name, opts.openCustomer);
  const status = acted ? deliveryLabel(x?.delivery, opts.autonomy) : { word: "Left alone", tone: "quiet" };
  const content = acted ? x?.content || opts.contentIndex.get(d.action.content_id) : null;
  const img = safeSrc(content?.asset_url);
  const combo = comboParts(d, x, opts.contentIndex, opts.brief);
  const meta = [`Decided ${when(d.made_at)}`];
  if (acted && !combo.some((p) => p.startsWith("send time"))) meta.push(`send at ${when(d.action.send_at)}`);
  return h(
    "li",
    { class: `ap-feed-item ${acted ? "" : "ap-feed-nothing"}`.trim() },
    h("div", { class: "ap-feed-top" }, who,
      status && chip(status.word, status.tone),
      h("span", { class: "ap-what" }, describeAction(d, opts.brief)),
      d.group && d.group !== "agent" && h("span", { class: "ap-chip ap-chip-quiet" }, GROUP_CHIP[d.group] || groupLabel(d.group))),
    !acted && x?.left_alone && h("p", { class: "lv-alone-why" }, x.left_alone.words),
    combo.length > 0 && h("p", { class: "lv-combo" }, combo.join(" · ")),
    img && h("img", { class: "lv-thumb", src: img, alt: "The image in this message", loading: "lazy", width: "96", height: "96" }),
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

/** The unsubscribe tile's note: "Pauses at 3× your usual (usual 0.11%), that is 0.33%", or why the check is off. */
export function unsubscribeNote(rule) {
  if (!rule) return null;
  if (rule.judged && present(rule.baseline) && present(rule.pause_at)) {
    return `Pauses the goal at ${num(rule.threshold, 0)}× your usual rate (usual ${pct(rule.baseline)}), that is ${pct(rule.pause_at)}`;
  }
  return rule.words || null;
}

function healthView(health, brief, limits) {
  if (!health && !limits) return [empty("No health numbers yet.")];
  const rules = new Map((limits?.rules || []).map((r) => [r.metric, r]));
  const cap = brief?.envelope?.max_contacts_per_week;
  const delivery = rules.get("delivery_rate_below");
  const complaint = rules.get("complaint_rate");
  const unsub = rules.get("unsubscribe_multiple");
  const deliveryFloor = present(delivery?.pause_at) ? delivery.pause_at : stopAt(brief, "delivery_rate_below");
  const complaintCeil = present(complaint?.pause_at) ? complaint.pause_at : stopAt(brief, "complaint_rate");
  const deliveryNow = present(health?.delivery_rate) ? health.delivery_rate : delivery?.current;
  const complaintNow = present(health?.complaint_rate) ? health.complaint_rate : complaint?.current;
  const unsubNow = present(health?.unsubscribe_rate) ? health.unsubscribe_rate : unsub?.current;
  const tiles = [
    { label: "Delivered", value: pct(deliveryNow), note: present(deliveryFloor) ? `Pauses the channel below ${pct(deliveryFloor)}` : null,
      bad: present(deliveryNow) && present(deliveryFloor) && deliveryNow < deliveryFloor },
    { label: "Complaints", value: pct(complaintNow), note: present(complaintCeil) ? `Pauses the goal above ${pct(complaintCeil)}` : null,
      bad: present(complaintNow) && present(complaintCeil) && complaintNow > complaintCeil },
    { label: "Unsubscribes", value: pct(unsubNow), note: unsubscribeNote(unsub),
      bad: Boolean(unsub?.judged) && present(unsubNow) && present(unsub?.pause_at) && unsubNow >= unsub.pause_at },
    { label: "Blocks", value: pct(health?.block_rate), note: null, bad: false },
    { label: "Contacts per customer per week", value: num(health?.contacts_per_customer_week, 2),
      note: present(cap) ? `Hard cap ${cap} per customer` : null,
      bad: present(health?.contacts_per_customer_week) && present(cap) && health.contacts_per_customer_week > cap },
  ];
  const others = (limits?.rules || []).filter((r) => !["delivery_rate_below", "complaint_rate", "unsubscribe_multiple"].includes(r.metric));
  return [
    h("dl", { class: "ap-tiles" }, ...tiles.map((t) =>
      h("div", { class: `ap-tile ${t.bad ? "ap-tile-bad" : ""}`.trim() },
        h("dt", null, t.label),
        h("dd", { class: "ap-tile-value" }, t.value, t.bad && h("span", { class: "ap-tile-flag" }, " · over the limit")),
        t.note && h("dd", { class: "ap-muted ap-small" }, t.note)),
    )),
    others.length > 0 && h("h3", { class: "ap-h3" }, "Other safety stops"),
    others.length > 0 && h("ul", { class: "ap-plain lv-stops" }, ...others.map((r) =>
      h("li", null, r.words || humanize(r.metric),
        r.metric === "spend_pace" && present(r.current) && h("span", { class: "ap-muted" }, ` Now: spending at ${pct(r.current)} of the planned pace.`),
        r.judged === false && h("span", { class: "ap-muted" }, " (not running yet)")))),
    limits && h("p", { class: "ap-muted ap-small" }, "Each stop pauses sending by itself, without waiting for you. You can always press Pause yourself."),
    h("p", { class: "ap-muted ap-small" }, `As of ${when(health?.as_of || limits?.as_of)}`),
  ].filter(Boolean);
}

// ---- the customer side panel ------------------------------------------------------------------------

/**
 * Open the side panel for one customer; returns its close function. Focus moves into the panel, stays inside it
 * while it is open (Tab wraps), and goes back to the button that opened it; Escape and the backdrop close it.
 */
function customerPanel(host, ctx, customerId, opts, opener, onClosed) {
  const title = h("h2", { class: "ap-h2", id: "ap-panel-title" }, ...panelTitle(customerId, opts.name));
  const closeBtn = h("button", { type: "button", class: "ap-btn ap-btn-quiet", "aria-label": "Close customer panel" }, "Close");
  const body = h("div", { class: "ap-panel-body" }, h("p", { class: "ap-muted" }, "Loading…"));
  const panel = h("aside", { class: "ap-panel", role: "dialog", "aria-modal": "true", "aria-labelledby": "ap-panel-title", tabindex: "-1" },
    h("div", { class: "ap-panel-head" }, title, closeBtn), body);
  const backdrop = h("div", { class: "ap-backdrop" });
  let open = true;
  const onKey = (e) => {
    if (e.key === "Escape") close();
    else if (e.key === "Tab" && typeof panel.querySelectorAll === "function") {
      const stops = [...panel.querySelectorAll("button, a[href], input, summary, [tabindex]:not([tabindex='-1'])")].filter((n) => !n.disabled);
      if (!stops.length) return;
      const first = stops[0];
      const last = stops[stops.length - 1];
      const active = document.activeElement;
      if (e.shiftKey && (active === first || active === panel)) {
        e.preventDefault();
        last.focus();
      } else if (!e.shiftKey && active === last) {
        e.preventDefault();
        first.focus();
      } else if (typeof panel.contains === "function" && !panel.contains(active)) {
        e.preventDefault();
        first.focus();
      }
    }
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

  // The insight (reading hours, offer habit, refill, delivery states) is extra: without it the panel still shows.
  const insightLoad = maybe(ctx.api, "getCustomerInsight", customerId).catch(() => null);
  Promise.all([call(ctx.api, "getCustomer", customerId), insightLoad]).then(
    ([bundle, insight]) => {
      if (!open) return;
      const name = insight?.display_name || opts.name;
      if (present(name)) title.replaceChildren(...panelTitle(customerId, name));
      body.replaceChildren(...customerView(bundle, insight || null, opts));
    },
    (e) => {
      if (!open) return;
      body.replaceChildren(notFound(e) ? empty(`No details for customer ${customerId} yet.`) : failed(`customer ${customerId}`, e));
    },
  );
  return close;
}

function panelTitle(customerId, name) {
  return present(name) ? [String(name), " ", h("span", { class: "lv-id" }, customerId)] : [`Customer ${customerId}`];
}

const PREFERENCE_WORDS = { no_discounts: "No discounts" };

function knowList(st, insight, opts) {
  const items = [];
  const add = (label, text, note = null) => {
    if (present(text)) items.push(h("li", null, h("strong", null, `${label}: `), text, note && h("span", { class: "ap-muted ap-small lv-basis" }, ` ${note}`)));
  };
  add("Reading hours", insight?.best_hour_words || null, present(insight?.reads_counted) && present(insight?.best_hour) ? `(from ${count(insight.reads_counted)} opens, clicks and replies)` : null);
  add("Offers", insight?.offer_habit_words || offerHabitFromSensitivity(st.offer_sensitivity));
  add("Refill", refillWords(insight?.refill), insight?.refill?.basis ? `(${insight.refill.basis})` : null);
  for (const n of insight?.next_items || []) add("Might like next", present(n.name) ? n.name : `product ${n.item_id}`, present(n.reason) ? `(${n.reason})` : null);
  const c7 = present(insight?.contacts_7d) ? insight.contacts_7d : st.contacts_7d;
  const cap = opts.brief?.envelope?.max_contacts_per_week;
  if (present(c7)) add("This week", `${count(c7)} ${Number(c7) === 1 ? "message" : "messages"} from us in the last 7 days`, present(cap) ? `(your plan allows ${cap} a week)` : null);
  if (st.open_complaint) add("Complaint", "Has an open complaint, so it sends no promotions until it is resolved");
  return items;
}

function customerView(bundle, insight, opts) {
  const { currency, brief } = opts;
  const st = bundle?.state;
  if (!st) return [empty("We have no record of this customer.")];
  const rows = [
    ["Group", GROUP_WORDS[st.group] || groupLabel(st.group)],
    ["Where", [st.country, st.timezone].filter(present).join(" · ") || DASH],
    ["Purchases", count(st.purchases)],
    ["Spent with you", money(st.revenue, currency)],
    ["Average order", st.purchases ? money(st.revenue / st.purchases, currency) : DASH],
    ["Last purchase", day(st.last_purchase_at)],
    ["Usually buys", (st.top_categories || []).map(humanize).join(", ") || DASH],
    ["Messages (last 7 / 30 days)", `${count(st.contacts_7d)} / ${count(st.contacts_30d)}`],
    ["Last message", when(st.last_contact_at)],
  ];
  if (present(st.segment)) rows.splice(1, 0, ["Type of customer", humanize(st.segment)]);
  for (const [k, v] of Object.entries(st.preferences || {})) {
    rows.push(["Asked for", PREFERENCE_WORDS[k] && String(v) === "true" ? PREFERENCE_WORDS[k] : `${humanize(k)}: ${v}`]);
  }

  const channels = Object.entries(st.channels || {}).map(([ch, cs]) => {
    const reach = cs.unsubscribed ? "unsubscribed" : cs.address_known === false ? "no address" : cs.consent === false ? "has not agreed to messages" : "can contact";
    const counts = present(cs.sends) ? ` · ${count(cs.sends)} sent · ${count(cs.delivers)} delivered · ${count(cs.clicks)} ${Number(cs.clicks) === 1 ? "click" : "clicks"} · ${count(cs.replies)} ${Number(cs.replies) === 1 ? "reply" : "replies"}` : "";
    return h("li", null, h("strong", null, channelLabel(ch)), `: ${reach}${counts}`);
  });

  const items = insight?.items || null;
  const entries = timeline(bundle).map(({ at, kind, item }) => {
    if (kind === "event") {
      return h("li", { class: "ap-tl-item ap-tl-event" },
        h("span", { class: "ap-tl-when" }, when(at)),
        h("span", null, describeEvent(item, currency, items)));
    }
    return decisionEntry(at, item, insight, opts);
  });
  const know = knowList(st, insight, opts);

  return [
    h("h3", { class: "ap-h3" }, "What it knows about them"),
    know.length ? h("ul", { class: "ap-plain lv-know" }, ...know) : empty("Nothing worked out yet beyond the facts below."),
    h("dl", { class: "ap-facts" }, ...rows.map(([k, v]) => h("div", null, h("dt", null, k), h("dd", null, v)))),
    h("h3", { class: "ap-h3" }, "Channels"),
    channels.length ? h("ul", { class: "ap-plain" }, ...channels) : empty("No channels known."),
    h("h3", { class: "ap-h3" }, "Every decision and what happened"),
    entries.length ? h("ol", { class: "ap-timeline" }, ...entries) : empty("Nothing has happened yet."),
    brief && h("p", { class: "ap-muted ap-small" }, "Forecasts are what it expected before acting, not results. Results are on Proof."),
  ].filter(Boolean);
}

function decisionEntry(at, d, insight, opts) {
  const { currency, brief, contentIndex } = opts;
  const acted = d.kind === "act" && d.action;
  const status = acted ? deliveryLabel(insight?.deliveries?.[d.decision_id], opts.autonomy) : null;
  const alone = !acted ? insight?.left_alone?.[d.decision_id] : null;
  const itemName = acted && present(d.action.item_id) ? insight?.items?.[d.action.item_id] : null;
  const combo = acted ? comboParts(d, { item: present(d.action.item_id) ? { item_id: d.action.item_id, name: itemName || null } : null }, contentIndex, brief) : [];
  const holdout = d.group === "holdout";
  const bar = !holdout && present(d.attention_price) && (Number(d.attention_price) > 0 || (d.bids || []).length > 0)
    ? `The bar a message had to clear: ${fineMoney(d.attention_price, currency)} (the cost of bothering them again).` : null;
  const forecast = acted && present(d.expected_net) ? `Its forecast before sending: ${netWords(d.expected_net, currency)}.` : null;
  const ruled = (d.blocked_by || []).map(ruleWords);
  return h("li", { class: "ap-tl-item ap-tl-decision" },
    h("span", { class: "ap-tl-when" }, when(at)),
    h("span", { class: "ap-what" }, acted ? `Decided: ${describeAction(d, brief)}` : "Decided: leave them alone"),
    status && h("span", null, " ", chip(status.word, status.tone)),
    alone && h("p", { class: "lv-alone-why" }, alone.words),
    combo.length > 0 && h("p", { class: "lv-combo" }, combo.join(" · ")),
    h("p", { class: "ap-why" }, h("span", { class: "ap-why-label" }, "Why: "), d.reason || DASH),
    (forecast || bar) && h("p", { class: "ap-muted ap-small" }, [forecast, bar].filter(Boolean).join(" ")),
    holdout && h("p", { class: "ap-muted ap-small" }, "In the small random group it never contacts, so Proof can measure the difference."),
    ruled.length > 0 && h("p", { class: "ap-muted ap-small" }, `Ruled out: ${ruled.join("; ")}.`),
    (d.bids || []).length > 0 && h("details", { class: "ap-bids" }, h("summary", null, `Options it weighed (${d.bids.length})`),
      h("p", { class: "ap-muted ap-small" }, "Each line is its own estimate before sending, not a result."),
      h("ol", null, ...bidLabels(d.bids, opts).map((label, i) => bidItem(d.bids[i], d, label, opts)))),
  );
}

/** Each option's name: "WhatsApp · No offer (a reminder)"; two that would still read the same get their send time. */
function bidLabels(bids, { brief, contentIndex }) {
  const labels = bids.map((b) => {
    const a = b.action || {};
    const offer = present(a.offer_id) && a.offer_id !== "none" ? offerLabel(brief, a.offer_id) : null;
    const message = messageWords(contentIndex.get(a.content_id), offer);
    return `${describeAction({ action: a }, brief)}${present(message) ? ` (${message})` : ""}`;
  });
  return labels.map((label, i) =>
    labels.filter((l) => l === label).length > 1 && present(bids[i].action?.send_at) ? `${label}, sent ${when(bids[i].action.send_at)}` : label);
}

function bidItem(b, d, label, { currency }) {
  const verdict = bidVerdict(b, d);
  const costs = [];
  if (Number(b.offer_cost) > 0) costs.push(`the offer's cost ${fineMoney(b.offer_cost, currency)}`);
  if (Number(b.channel_cost) > 0) costs.push(`the message cost ${fineMoney(b.channel_cost, currency)}`);
  const lift = upliftWords(b.uplift);
  const v = Number(b.expected_net);
  const net = Number.isFinite(v) && v < 0 ? `would lose ${fineMoney(-v, currency)}` : `would add ${fineMoney(b.expected_net, currency)}`;
  return h("li", { class: verdict === "Chosen" ? "lv-bid-chosen" : null },
    h("strong", null, label),
    `: ${lift ? `${capital(lift)}; ` : ""}${net} after ${costs.length ? costs.join(" and ") : "its costs"}. `,
    h("span", { class: verdict === "Chosen" ? "lv-verdict lv-verdict-chosen" : "lv-verdict" }, verdict));
}
