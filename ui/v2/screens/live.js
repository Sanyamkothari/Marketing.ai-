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
     - Has it earned Autopilot? When the goal meets the bar (GET /v2/autopilot/{goal}), one card says what it would
       have made, with its range and its proof, and one click turns Autopilot on (POST .../turn-on).
   The Inbox is exceptions only (GET /v2/inbox, docs/v2/ZERO_TOUCH.md item 4): each item is one sentence, money with
   its range when there is money, and one button. An API without that route gets the older alert list.
   Tapping a customer opens a panel with what it knows about them in words (reading hours, offer habit, refill
   due, what they might like next), every decision with its reason, the options it weighed and why each lost.

   Words (DESIGN.md section 6): each card is its title, its numbers and at most one short sentence. Every
   explanation (how a number is measured, thresholds, the full reason of a decision, its message and send time) is a
   description on the label or number it explains: it opens on hover, keyboard focus or tap (components.js
   withInfo / info / describe), and its words stay in the DOM. A feed row is one line: the person, the action, a
   short reason and what happened.

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

import { EM_DASH, describe, info, isSimulatedShop, num, pageHead as pageHeadBlock, present, simulatedLabel, withInfo } from "../components.js";

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
  const parts = rangeParts(est, fmt);
  return parts ? `${parts.point} ${parts.range}` : DASH;
}

/**
 * The same estimate in two pieces, for a figure set large with its range right under it: {point: "$4,300",
 * range: "(range $900 to $7,700)"}. Null when any of the three is missing. `range()` is built from this, so
 * the two never disagree.
 */
export function rangeParts(est, fmt) {
  if (!est || !present(est.point) || !present(est.low) || !present(est.high)) return null;
  // One precision for the three: when any of them is $100 or more, all are shown in whole units, so a range
  // never reads "$823.54 to $3,254".
  const whole = [est.point, est.low, est.high].some((v) => Math.abs(Number(v)) >= 100);
  const f = (v) => fmt(whole ? Math.round(Number(v)) : v);
  return { point: f(est.point), range: `(range ${f(est.low)} to ${f(est.high)})` };
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
/** Severity in words, with a pale pill tone (the word is always there; colour is never the only signal). */
const SEVERITY = {
  critical: { word: "Critical", tone: "bad" },
  warning: { word: "Warning", tone: "warn" },
  info: { word: "Info", tone: "quiet" },
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

/**
 * A left-alone reason's short label, for the bars and the feed rows. The API's full words (`r.words`) go in the
 * label's description, so nothing is lost. A code not listed here shows the API's own words.
 */
const REASON_SHORT = {
  weekly_cap: "Weekly limit reached",
  max_contacts_per_week: "Weekly limit reached",
  holdout: "People we never contact",
  simple_rule: "Simple rule's group",
  not_worth_it: "Not worth bothering them",
  no_effect: "A message would not help",
  testing_rest: "Kept as a check",
  nothing_to_offer: "No message fits yet",
  too_soon: "Messaged hours ago",
  open_complaint: "Open complaint",
  snoozed: "Asked for a pause",
  no_budget: "Budget used up",
  pacing: "Slowing down",
  quiet_hours: "Quiet hours",
  offer_needs_approval: "Offer needs your approval",
  no_discounts: "Asked for no discounts",
  offer_not_allowed: "Offer not in your plan",
  no_brief: "No approved plan",
  channel_paused: "Channel paused",
  unsubscribed: "Unsubscribed",
  no_consent: "Has not agreed to messages",
  no_address: "No address for them",
};
export const reasonShort = (code, words) => REASON_SHORT[code] || (present(words) ? String(words) : humanize(code));

/**
 * A decision's reason cut to its point, for a one-line feed row: the part after "Sent a … by email:" up to the first
 * clause break, at most about 70 characters. The full reason is the row's description.
 */
export function shortReason(reason) {
  if (!present(reason)) return DASH;
  const text = String(reason).replace(/\s+/g, " ").trim();
  const colon = text.indexOf(": ");
  const why = colon > 0 && colon < 80 ? text.slice(colon + 2) : text;
  const clause = (why.match(/^(.*?)(?:[,;]\s|\.\s|\.$|$)/) || [null, why])[1] || why;
  return capital(snippet(clause, 70));
}

/** The runtime's briefing still says "the holdout"; on screen it is "people we never contact" (DESIGN.md section 6). */
export const plainGroupWords = (text) =>
  present(text)
    ? String(text)
      .replace(/\bin the (?:no-contact )?holdout\b/gi, "among the people we never contact")
      .replace(/\b(?:the )?(?:no-contact )?holdout\b/gi, (_m, at) => (at === 0 ? "People we never contact" : "people we never contact"))
    : text;

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
  const parts = briefingParts(today);
  return parts ? parts.map((p) => (typeof p === "string" ? p : p.text)).join("") : DASH;
}

/**
 * The same sentence in pieces, so the Yesterday card can set its two counts as figures with a description each:
 * plain strings, and {key: "contacted" | "left", text} for the counts. `briefingSentence` joins these.
 */
export function briefingParts(today) {
  if (!today) return null;
  const d = dayName(today.date, today.as_of);
  const who = { key: "contacted", text: people(today.contacted) };
  const alone = [", left ", { key: "left", text: count(today.left_alone) }, " alone, and here's why."];
  if (today.sends_nothing || today.autonomy === "shadow") {
    return [`${d.lead} I would have contacted `, who, " (Shadow mode, so nothing was sent)", ...alone];
  }
  if (today.autonomy === "approve") {
    return [`${d.lead} I picked `, who, " to contact (each batch waits for your approval)", ...alone];
  }
  return [`${d.lead} I contacted `, who, ...alone];
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
// `asset_url` may hold several pictures separated by spaces (a carousel): the row shows the first. A host under the
// reserved ".invalid" name can never load (RFC 6761), so it is not requested at all.
const safeSrc = (url) => {
  const first = present(url) ? String(url).trim().split(/\s+/)[0] : "";
  if (!/^(https?:\/\/|\/|\.\/)/i.test(first)) return null;
  return /^https?:\/\/[^/]*\.invalid(?::\d+)?(?:\/|$)/i.test(first) ? null : first;
};

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

// ---- markup for this screen --------------------------------------------------------------------------
// Built with `h` and the design system's classes (styles.css: .page-head, .card, .card-head, .kpis, .pill, .list,
// .error-box, .skeleton), plus `lv-` classes in live.css for what is Live's own. The older `ap-` names stay on the
// elements tests and other screens look for (ap-panel, ap-left-alone, ap-tile-value, ...).

/** The pale pill for a tone: words first, a small dot when the state is a real outcome. */
// "info" (scheduled, approved) is neutral: the accent is kept for data and focus, never for a status.
const PILL_TONE = { good: "pill-positive", warn: "pill-warning", info: "pill-neutral", bad: "pill-negative", quiet: "pill-neutral" };
function chip(label, tone, extra = "") {
  const dot = tone && tone !== "quiet" ? " pill-dot" : "";
  return h("span", { class: `pill ${PILL_TONE[tone] || "pill-neutral"}${dot} lv-state lv-state-${tone || "quiet"} ${extra}`.trim() }, label);
}

/**
 * A row's status in a long list: a small dot in the tone's colour and the word in grey, with no fill, so twenty
 * rows of "Waiting for your approval" read as a quiet column rather than a stripe of amber pills.
 */
// On a phone a feed row's first line holds the name, the action and this mark, so the longer states get a short
// word there; the full words stay in the row for screen readers (live.css hides them visually only).
const MARK_SHORT = {
  "Waiting for your approval": "To approve",
  "Approved, goes out on the next run": "Approved",
  "Not sent: batch turned down": "Not sent",
  "Would send (Shadow)": "Would send",
  "Failed to deliver": "Failed",
};
function statusMark(label, tone) {
  const short = MARK_SHORT[label];
  return h("span", { class: `lv-row-status lv-mark lv-mark-${tone || "quiet"} lv-state lv-state-${tone || "quiet"}${short ? " lv-mark-has-short" : ""}` },
    short ? [h("span", { class: "lv-mark-long" }, label), h("span", { class: "lv-mark-short", "aria-hidden": "true" }, short)] : label);
}

/** Grey blocks at the size of what is coming, so nothing jumps when it lands. Heights in px. */
function skeletonBlock(heights = [20, 20, 20]) {
  return h("div", { class: "skeleton-stack lv-skeleton", role: "status" },
    h("span", { class: "visually-hidden" }, "Loading…"),
    ...heights.map((px, i) => h("span", { class: "skeleton", "aria-hidden": "true", style: `height: ${px}px; width: ${i === heights.length - 1 && heights.length > 1 ? 64 : 100}%` })));
}

/** A card: its title and a grey meta on one line, and a body that starts as grey blocks. */
// `about(text)` puts a quiet (i) after the title holding the card's explanation; `setMeta(text, more)` fills the grey
// meta and, with `more`, gives it a description (the meta is a fact, its detail opens on hover or tap).
function liveSection(title, { extraClass = "", id = null, skeleton = [20, 20, 20] } = {}) {
  const heading = h("h2", { class: "card-title", id });
  heading.textContent = title;
  const aboutSlot = h("span", { class: "info-host lv-about" });
  const meta = h("span", { class: "card-meta" });
  const metaTip = h("span", { class: "info-tip", hidden: true });
  const body = h("div", { class: "lv-body" }, skeletonBlock(skeleton));
  const card = h("section", { class: `card lv-card ${extraClass}`.trim(), "aria-labelledby": id },
    h("div", { class: "card-head" }, h("div", { class: "lv-title-row" }, heading, aboutSlot), h("span", { class: "info-host" }, meta, metaTip)), body);
  let metaWired = false;
  const about = (text) => aboutSlot.replaceChildren(...[present(text) ? info(text, { label: `About ${title}` }) : null].filter(Boolean));
  const setMeta = (text, more = null) => {
    meta.textContent = present(text) ? String(text) : "";
    if (!present(more)) return;
    if (!metaWired) describe(meta, metaTip);
    metaWired = true;
    metaTip.replaceChildren(...[more].flat().filter(Boolean));
  };
  return { card, heading, meta, body, about, setMeta };
}

/** A label (or any words) with a description that opens on hover, focus or tap. No text: the plain label. */
function explained(label, text, cls = null) {
  const target = typeof label === "object" && label ? label : h("span", { class: cls }, label);
  const words = [text].flat().filter((t) => present(t) && t !== false);
  if (!words.length) return target;
  return withInfo(target, words.length === 1 && typeof words[0] === "string" ? words[0] : tipLines(words));
}

/** Several sentences in one description, each on its own line. */
const tipLines = (lines) => lines.filter(present).map((l) => (typeof l === "object" ? l : h("span", { class: "lv-tip-line" }, l)));

/** The empty state inside a card: the card keeps its title; one sentence says what will appear. */
const lvEmpty = (sentence) => h("p", { class: "lv-empty" }, sentence);

/** The error inside the block that failed: what failed, the API's reason, and Retry when there is one. */
function lvFailed(what, e, retry = null) {
  const again = typeof retry === "function" ? h("button", { type: "button", class: "link-btn" }, "Retry") : null;
  if (again) again.addEventListener("click", retry);
  return h("div", { class: "error-box lv-error", role: "alert" },
    h("p", { class: "empty-title" }, `Couldn't load ${what}`, h("span", { class: "lv-error-why" }, `: ${errText(e)}`)),
    again && h("div", { class: "error-actions" }, again));
}

/** One KPI cell: label over value over a note, inside a `.kpis` strip. */
function kpiCell(label, value, note = null, extra = "", about = null) {
  const missing = value === DASH;
  return h("div", { class: `kpi ${extra}`.trim() },
    h("dt", { class: "kpi-label" }, explained(label, about)),
    h("dd", { class: `kpi-value${missing ? " is-missing" : ""}` }, value),
    note && h("dd", { class: "kpi-note" }, note));
}

// ---- the screen -------------------------------------------------------------------------------------

export async function render(root, ctx) {
  ensureStyles();
  const api = ctx.api;
  const announcer = h("p", { class: "ap-sr", role: "status", "aria-live": "polite" });
  const page = h("div", { class: "ap-page ap-live lv-page" });
  root.replaceChildren(page);
  const retry = () => render(root, ctx);
  const goTo = (label, where) =>
    typeof ctx.navigate === "function" && h("button", { class: "btn btn-primary", type: "button", onclick: () => ctx.navigate(where) }, label);
  const head = (sub) => pageHeadBlock({ title: "Live", sub });

  let bundle;
  try {
    bundle = await resolveGoal(ctx);
  } catch (e) {
    if (notFound(e)) {
      page.append(
        head(`There is no goal ${ctx.goalId}.`),
        h("div", { class: "empty" }, h("p", { class: "empty-title" }, "Goal not found"),
          h("p", null, "It may have been removed, or the link is mistyped."), goTo("Go to Goal", "goal")),
        announcer,
      );
    } else page.append(head(null), lvFailed("the goal", e, retry), announcer);
    return;
  }
  if (!bundle || !bundle.goal) {
    page.append(
      head("Nothing is running yet."),
      h("div", { class: "empty" }, h("p", { class: "empty-title" }, "No goal yet"),
        h("p", null, "Set a goal and approve its plan, and the agent's work shows up here."), goTo("Set a goal", "goal")),
      announcer,
    );
    return;
  }

  let goal = bundle.goal;
  const brief = bundle.brief || null;
  const goalId = goal.goal_id;
  const currency = goal.currency;
  const contentIndex = new Map((bundle.content || []).map((c) => [c.content_id, c]));
  const state = { panelClose: null, batches: undefined, batchesFor: null, batchLoad: null, breakdowns: new Map(), alerts: [], limits: null, since: null, inbox: null };
  const topbarPause = Boolean(ctx.pauseInTopBar);
  const pauseWord = ctx.pauseAllInTopBar ? "Pause this goal" : "Pause everything";

  // Header: the one page head (components.js pageHead): "Live", the goal's words, its status pill and Pause. The
  // autonomy level is the top bar's badge inside the app, so here its sentence (and since when) is the status pill's
  // description; outside the app (no top bar) the level gets its own pill. What Pause does is Pause's description.
  const ownAutonomyPill = !ctx.pauseAllInTopBar && !topbarPause;
  const statusChip = h("span", { class: "pill" });
  const statusTip = h("span", { class: "info-tip", hidden: true });
  describe(statusChip, statusTip);
  const autonomyChip = h("span", { class: "pill pill-neutral lv-autonomy" });
  const autonomyTip = h("span", { class: "info-tip", hidden: true });
  if (ownAutonomyPill) describe(autonomyChip, autonomyTip);
  const pauseBtn = h("button", { type: "button", class: "btn" });
  const pauseTip = h("span", { class: "info-tip", hidden: true });
  describe(pauseBtn, pauseTip);
  const selfPause = h("div", { class: "lv-selfpause-slot" });
  const paintHeader = () => {
    const tone = goal.status === "active" ? "pill-positive pill-dot" : goal.status === "paused" ? "pill-warning pill-dot" : "pill-neutral";
    statusChip.textContent = STATUS_WORDS[goal.status] || String(goal.status || DASH);
    statusChip.setAttribute("class", `pill ${tone} has-info ap-status-${goal.status || "unknown"}`);
    autonomyChip.textContent = AUTONOMY_WORDS[goal.autonomy] || goal.autonomy || DASH;
    autonomyChip.setAttribute("class", `pill pill-neutral has-info lv-autonomy lv-autonomy-${goal.autonomy || "unknown"}`);
    const level = [AUTONOMY_SENTENCE[goal.autonomy], present(state.since) ? `At this level since ${day(state.since)}.` : null].filter(Boolean).join(" ");
    const paused = goal.status === "paused";
    const canToggle = goal.status === "active" || paused;
    pauseBtn.textContent = paused ? "Resume" : pauseWord;
    // Pause is a red hairline (it stops sending); while paused, Resume is the one thing to do, so it is primary.
    pauseBtn.setAttribute("class", paused ? "btn btn-primary has-info lv-pause-btn" : "btn btn-danger has-info lv-pause-btn");
    pauseBtn.disabled = !canToggle;
    pauseBtn.setAttribute("aria-pressed", paused ? "true" : "false");
    let hint;
    if (topbarPause) {
      hint = canToggle
        ? paused
          ? "Paused: the agent decides nothing and sends nothing until you press Resume at the top."
          : "Pause, at the top, stops every send for this goal at once."
        : "This goal is not running yet, so there is nothing to pause.";
    } else {
      hint = canToggle
        ? paused
          ? "Paused: the agent decides nothing and sends nothing until you resume."
          : "Stops every send for this goal at once. You can resume any time."
        : "This goal is not running, so there is nothing to pause.";
    }
    pauseTip.textContent = hint;
    // A tap on a button is its action, so the same words are its accessible name for touch screens.
    pauseBtn.setAttribute("aria-label", `${pauseBtn.textContent}. ${hint}`);
    autonomyTip.textContent = level;
    statusTip.replaceChildren(...tipLines([ownAutonomyPill ? null : level, topbarPause || ownAutonomyPill ? hint : null]));
    if (!statusTip.childNodes.length) statusTip.textContent = hint;
    paintSelfPause();
    paintBatches();
  };
  // The banner when Autonomy paused itself (a stop rule fired), naming why. Drawn from the inbox's alerts. Red
  // when the whole goal stopped; a calm amber notice when only some sending (a channel) paused.
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
        h("h2", { class: "lv-selfpause-title", id: "lv-selfpause-title" },
          h("span", { class: "lv-selfpause-icon", "aria-hidden": "true" }),
          goalStopped ? "This goal paused itself" : `Autonomy paused some sending on its own (${auto.length})`,
          info([
            "It does this by itself when complaints, unsubscribes or delivery problems pass the limits in your plan. ",
            goalStopped ? "Nothing more goes out for this goal until you resume it." : "The rest keeps running. Every pause is in the Inbox.",
          ].join(""), { label: "Why it paused" })),
        goalStopped && h("p", { class: "lv-selfpause-why" }, latestGoalStop.message || humanize(latestGoalStop.code)),
        h("ul", { class: "lv-selfpause-list" }, ...shown.filter((a) => !goalStopped || a !== latestGoalStop).map((a) =>
          h("li", null, h("span", { class: "lv-selfpause-when" }, when(a.at)), h("span", null, a.message || humanize(a.code),
            !present(a.goal_id) && h("span", { class: "lv-selfpause-scope" }, " (every goal)")))))));
  }

  // Batches: shown only while the goal is at "approve each batch", loaded once per goal.
  const batchSec = liveSection("Waiting for your approval", { extraClass: "ap-batches lv-batches", skeleton: [16, 56, 56] });
  batchSec.about("Nothing in a batch is sent until you approve it. Each message still follows the plan's limits. One click approves the whole batch. The expected numbers are its own forecast before sending, not a result.");
  const batchSlot = h("div", { class: "ap-batch-slot lv-batch-slot" });
  const offerSlot = h("div", { class: "lv-offer-slot" });
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
    batchSec.body.replaceChildren(skeletonBlock([16, 56, 56]));
    let rows;
    try {
      // An API client without batches (an older build) is "not listed yet", not an error.
      rows = typeof api?.listBatches === "function" ? await api.listBatches(goalId) : null;
    } catch (e) {
      state.batchesFor = null; // try again on the next change
      batchSec.heading.textContent = "Waiting for your approval";
      batchSec.body.replaceChildren(lvFailed("the batches waiting for you", e, () => {
        state.batchesFor = goalId;
        state.batchLoad = loadBatches();
      }));
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

  const header = pageHeadBlock({
    title: "Live",
    sub: goal.text || goalId,
    status: [h("span", { class: "info-host" }, statusChip, statusTip), ownAutonomyPill && h("span", { class: "info-host" }, autonomyChip, autonomyTip)],
    actions: topbarPause ? [] : [h("span", { class: "info-host" }, pauseBtn, pauseTip)],
  });
  header.setAttribute("class", "page-head lv-head");

  const todaySec = liveSection("Yesterday", { extraClass: "lv-today", id: "lv-today-title", skeleton: [24, 92, 20, 20, 20] });
  const briefingSec = liveSection("Briefing", { extraClass: "ap-briefing lv-briefing", skeleton: [40, 20, 20] });
  const learnedSec = liveSection("What it learned", { extraClass: "lv-learned-card", skeleton: [20, 20] });
  const healthSec = liveSection("Health and safety stops", { extraClass: "lv-health-card", skeleton: [20, 20, 20, 20, 20] });
  const inboxSec = liveSection("Inbox", { extraClass: "lv-inbox-card", skeleton: [56, 56] });
  const feedSec = liveSection("Every decision", { extraClass: "lv-feed-card", skeleton: [40, 24, 24, 24, 24, 24] });
  feedSec.about("Each row is one decision: who, what it chose, the reason in short, and what happened. Hover or tap the reason for the whole of it, the message and the send time; tap a name for everything it decided for that person. Leaving someone alone is a decision too, with its own reason, under the list.");
  // Two columns of 8 + 4, each a stack of its own, so a short card never leaves a gap beside a tall one: the main
  // column is the batches (on Approve, the one thing that needs the marketer), yesterday and the long feed; the side
  // column is the inbox, health, what it learned and the briefing. On a phone the cards are one column in the order
  // of the questions (batches, yesterday, inbox, health, then the feed, what it learned and the briefing), set in
  // live.css, so the inbox and health still come before the long feed.
  const simulatedSlot = h("div", { class: "lv-simulated" });
  page.append(
    header,
    simulatedSlot,
    selfPause,
    h("div", { class: "grid lv-grid" },
      h("div", { class: "span-8 stack lv-main" }, offerSlot, batchSlot, todaySec.card, feedSec.card),
      h("div", { class: "span-4 stack lv-side" }, inboxSec.card, healthSec.card, learnedSec.card, briefingSec.card)),
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

  const [briefing, feed, health, alerts, today, proof, limits, inbox, readiness] = await Promise.allSettled([
    call(api, "getBriefing", goalId),
    feedLoad,
    call(api, "getHealth"),
    call(api, "listAlerts"),
    maybe(api, "getToday", goalId),
    maybe(api, "getProof", goalId),
    maybe(api, "getHealthLimits", goalId),
    maybe(api, "listInbox"),
    goal.autonomy !== "autopilot" && goal.status === "active" ? maybe(api, "getAutopilot", goalId) : Promise.resolve(null),
  ]);
  if (limits.status === "fulfilled") state.limits = limits.value;
  const todayValue = today.status === "fulfilled" ? today.value : null;
  // A shop we simulate says so before any number is read: the label is the first thing under the title.
  const proofNotes = proof.status === "fulfilled" && proof.value ? proof.value.notes : [];
  if (await isSimulatedShop(ctx, [...(proofNotes || []), ...(todayValue?.notes || [])])) {
    simulatedSlot.replaceChildren(simulatedLabel("The customers, messages and money on this page are from a shop we simulate. Nothing here reaches a real person."));
  }
  if (todayValue && present(todayValue.autonomy_since)) state.since = todayValue.autonomy_since;

  // Yesterday: the pitch's daily briefing for this goal.
  todaySec.heading.textContent = todayValue ? dayName(todayValue.date, todayValue.as_of).title : "Day by day";
  // The meta is the time of the numbers; how many it looked at and when it checks next are its description.
  if (todayValue && present(todayValue.as_of)) todaySec.setMeta(`As of ${when(todayValue.as_of)}`, checkedLine(todayValue));
  const otherNotes = (todayValue?.notes || []).filter((n) => !/^Purchases are/.test(n));
  if (otherNotes.length) todaySec.about(otherNotes.join(" "));
  if (today.status === "rejected") todaySec.body.replaceChildren(lvFailed("yesterday's numbers", today.reason, retry));
  else {
    todaySec.body.replaceChildren(...todayView(todayValue, {
      goal, brief, currency, proof: proof.status === "fulfilled" ? proof.value : null, proofError: proof.status === "rejected" ? proof.reason : null,
      navigate: ctx.navigate,
    }));
  }

  // The learning notes: from /today, else the briefing's learning sections.
  const briefingValue = briefing.status === "fulfilled" ? briefing.value : null;
  const learned = (todayValue && todayValue.learned && todayValue.learned.length ? todayValue.learned : null) ||
    (briefingValue?.sections || []).filter(isLearnedSection).map((s) => plainGroupWords(s.body)).filter(present);
  learnedSec.body.replaceChildren(...learnedView(learned));

  // The full (often weekly) briefing.
  if (briefing.status === "rejected") briefingSec.body.replaceChildren(lvFailed("today's briefing", briefing.reason, retry));
  else {
    // Beside a Yesterday card with numbers, the headline repeats them: it is the meta's description instead.
    const showHeadline = !(todayValue && todayValue.customers_considered);
    const range = briefingValue ? headlineRange(briefingValue, currency) : null;
    if (briefingValue) {
      briefingSec.setMeta(`${briefingValue.period === "weekly" ? "Weekly" : "Daily"} · ${day(briefingValue.as_of).replace(/ \d{4}$/, "")}`, tipLines([
        showHeadline ? null : plainGroupWords(briefingValue.headline),
        showHeadline || !range || String(plainGroupWords(briefingValue.headline) || "").includes(range) ? null : `Range ${range}.`,
        `Figures as of ${when(briefingValue.as_of)}.`,
      ]));
    }
    briefingSec.body.replaceChildren(...briefingView(briefingValue, { showHeadline, currency }));
  }

  // Activity feed
  if (feed.status === "rejected") feedSec.body.replaceChildren(lvFailed("the activity feed", feed.reason, retry));
  else {
    const n = (feed.value?.rows || []).length;
    if (n) feedSec.setMeta(`Latest ${count(n)}`, `The latest ${count(n)} ${n === 1 ? "decision" : "decisions"} for this goal, newest first.`);
    feedSec.body.replaceChildren(...feedView(feed.value, { brief, contentIndex, autonomy: goal.autonomy, today: todayValue, openCustomer }));
  }

  // The Autopilot offer: only once the goal has earned it, with what it would have made and its proof.
  const ready = readiness.status === "fulfilled" && readiness.value && readiness.value.ready ? readiness.value : null;
  const drawOffer = () => {
    if (!ready || goal.autonomy === "autopilot" || goal.status !== "active") {
      offerSlot.replaceChildren();
      return;
    }
    offerSlot.replaceChildren(offerView(ready, goal, async (btn) => {
      btn.disabled = true;
      try {
        const change = await call(api, "turnOnAutopilot", goalId);
        goal = { ...goal, autonomy: "autopilot" };
        notify(ctx, (change && change.words) || "Autopilot is on.", "good", announcer);
        if (typeof ctx.refreshGoals === "function") await ctx.refreshGoals();
        paintHeader();
        drawOffer();
        drawInbox();
      } catch (e) {
        notify(ctx, `Couldn't turn on Autopilot: ${errText(e)}`, "critical", announcer);
        btn.disabled = false;
      }
    }));
  };
  drawOffer();

  // Inbox: the exceptions-only inbox when the API has it; else the alerts from both endpoints (one failing still
  // shows the other's).
  const lists = [];
  if (alerts.status === "fulfilled") lists.push(alerts.value);
  if (health.status === "fulfilled" && health.value) lists.push(health.value.alerts);
  const merged = mergeAlerts(lists, goalId);
  state.alerts = merged;
  const box = inbox.status === "fulfilled" ? inbox.value : null;
  state.inbox = box ? inboxItemsFor(box, goalId) : null;
  function drawInbox() {
    if (!state.inbox) {
      inboxSec.heading.textContent = merged.length ? `Inbox (${merged.length})` : "Inbox";
      inboxSec.body.replaceChildren(
        ...(alerts.status === "rejected" ? [lvFailed("alerts", alerts.reason, retry)] : []),
        ...inboxView(merged, new Set(merged.filter((a) => a.auto_paused).slice(0, 3))),
      );
      return;
    }
    // The Autopilot offer for this goal is its own card above: say it once.
    const shown = state.inbox.filter((i) => !(ready && offerSlot.childNodes.length && i.button && i.button.action === "turn_on_autopilot" && i.goal_id === goalId));
    inboxSec.heading.textContent = shown.length ? `Inbox (${shown.length})` : "Inbox";
    inboxSec.about("Only what needs a person: a safety stop, a goal losing money, a reconnect, data that can't be trusted, something bigger than your signed limits, a customer who needs you. Everything else is in the weekly briefing.");
    inboxSec.body.replaceChildren(...inboxItemsView(shown, box, async (item, btn) => {
      const b = item.button || {};
      if (b.action === "open") {
        if (present(b.target) && typeof ctx.navigate === "function" && String(b.target).startsWith("#")) ctx.navigate(b.target);
        else if (present(b.target) && typeof window !== "undefined") window.location.assign(b.target);
        return;
      }
      btn.disabled = true;
      try {
        const out = await call(api, "actOnInbox", item.item_id);
        state.inbox = state.inbox.filter((i) => i.item_id !== item.item_id);
        notify(ctx, (out && out.words) || "Done.", "good", announcer);
        if (b.action === "turn_on_autopilot" && b.goal_id === goalId) goal = { ...goal, autonomy: "autopilot" };
        if (["resume_in_approve", "pause_goal", "turn_on_autopilot"].includes(b.action) && typeof ctx.refreshGoals === "function") await ctx.refreshGoals();
        paintHeader();
        drawInbox();
      } catch (e) {
        notify(ctx, `Couldn't do that: ${errText(e)}`, "critical", announcer);
        btn.disabled = false;
      }
    }));
  }
  drawInbox();
  paintHeader();

  // Health
  if (health.status === "rejected") healthSec.body.replaceChildren(lvFailed("customer health", health.reason, retry));
  else {
    const asOf = health.value?.as_of || state.limits?.as_of;
    if (present(asOf)) healthSec.setMeta(`As of ${when(asOf)}`);
    if (state.limits) healthSec.about("Each stop pauses sending by itself, without waiting for you. You can always press Pause yourself. Hover or tap a figure's name for its limit.");
    healthSec.body.replaceChildren(...healthView(health.value, brief, state.limits));
  }

  if (state.batchLoad) await state.batchLoad;
}

// ---- the sections -----------------------------------------------------------------------------------

const BATCH_TONE = { pending: "warn", approved: "good", sent: "good", rejected: "quiet" };

function batchesView(batches, currency, decide, breakdowns = new Map()) {
  if (batches === null) {
    return [lvEmpty("Batches to approve show here. This server does not list them yet.")];
  }
  const { pending, decided } = splitBatches(batches);
  const out = [];
  if (!pending.length) out.push(lvEmpty("Nothing is waiting for you."));
  else {
    out.push(h("ul", { class: "list lv-batch-list ap-batches-list", "aria-label": "Batches waiting for approval, newest first" }, ...pending.map((b, i) => {
      const words = breakdowns.get(b.batch_id)?.words;
      const label = words || b.summary || `A batch of ${count(b.n_contacts)} contacts`;
      // One primary per screen: the newest batch's Approve. Older batches keep the same button as a secondary.
      const approve = h("button", { type: "button", class: `btn ${i === 0 ? "btn-primary" : "btn-secondary"} btn-sm`, "aria-label": `Approve batch: ${label}` }, "Approve");
      const reject = h("button", { type: "button", class: "btn btn-secondary btn-sm", "aria-label": `Reject batch: ${label}` }, "Reject");
      approve.addEventListener("click", () => decide(b, true, [approve, reject]));
      reject.addEventListener("click", () => decide(b, false, [approve, reject]));
      return h("li", { class: "lv-batch ap-batch" },
        h("div", { class: "lv-batch-text" },
          h("p", { class: "row-title lv-batch-summary" }, label),
          h("p", { class: "row-meta lv-batch-facts" }, batchFacts(b, currency))),
        h("div", { class: "lv-batch-actions" }, reject, approve));
    })));
  }
  if (decided.length) {
    out.push(h("details", { class: "ap-left-alone lv-fold" },
      h("summary", null, `Decided (${decided.length})`),
      h("ul", { class: "list lv-batch-list ap-batches-list" }, ...decided.map((b) =>
        h("li", { class: "lv-batch lv-batch-done ap-batch" },
          h("div", { class: "lv-batch-text" },
            h("p", { class: "lv-batch-summary" }, chip(BATCH_WORDS[b.status] || String(b.status), BATCH_TONE[b.status] || "quiet", `ap-batch-${b.status}`), " ", b.summary || `A batch of ${count(b.n_contacts)} contacts`),
            h("p", { class: "row-meta" }, [batchFacts(b, currency), present(b.decided_at) ? `decided ${when(b.decided_at)}${present(b.decided_by) ? ` by ${b.decided_by}` : ""}` : null].filter(Boolean).join(" · "))))))));
  }
  return out;
}

/** Why a left-alone reason's people end up there, for the reason's description (beyond the API's own words). */
function reasonHint(code, cap) {
  if (code === "weekly_cap" && present(cap)) return `Your limit: ${cap} a week.`;
  if (code === "no_effect") return "This is where people who would buy anyway end up: a message, or a discount, would only cost you.";
  if (code === "holdout") return "A small random group, so the Proof page can show the real difference.";
  return null;
}

// Yesterday: the pitch's one sentence (its two counts set as figures), one strip of three numbers (each label holds
// how it is measured), and why it left people alone as short labelled bars (each label holds the API's full words).
// When it checked and checks next is the card's meta's description; only an overdue check shows as a line, because
// it is a problem.
function todayView(today, { brief, currency, proof, proofError }) {
  if (!today) {
    return [lvEmpty("The day-by-day count is not available from this server yet.")];
  }
  if (!today.customers_considered) {
    return [h("p", { class: "lv-say" }, "Nothing was decided for this goal yet.")];
  }
  const cur = today.currency || currency;
  const byGroup = today.contacted_by_group || {};
  const groupNote = present(byGroup.baseline) && byGroup.baseline > 0
    ? `${count(byGroup.agent || 0)} chosen by Autonomy, ${count(byGroup.baseline)} by the simple rule we compare it with.`
    : null;
  const shadow = today.sends_nothing || today.autonomy === "shadow";
  const reasons = today.reasons || [];
  const maxN = Math.max(1, ...reasons.map((r) => Number(r.n) || 0));
  const cap = brief?.envelope?.max_contacts_per_week;

  const reasonList = reasons.length
    ? h("ul", { class: "lv-reasons", "aria-label": `Why ${count(today.left_alone)} customers were left alone` }, ...reasons.map((r) => {
      const short = reasonShort(r.code, r.words);
      const full = [short !== r.words ? r.words : null, reasonHint(r.code, cap)].filter(Boolean);
      return h("li", { class: `lv-reason lv-reason-${r.code}` },
        h("span", { class: "lv-reason-words" }, explained(short, full.length ? full.join(". ").replace(/\.\./g, ".") : null)),
        h("span", { class: "lv-reason-bar", "aria-hidden": "true" }, h("span", { style: `width: ${Math.max(2, Math.round((100 * (Number(r.n) || 0)) / maxN))}%` })),
        h("span", { class: "lv-reason-n num" }, count(r.n)));
    }))
    : lvEmpty("Nobody was left alone that day.");

  const profit = profitWords(proof, cur);
  // A sure result is set large with its range right under it (never the bare point); "Not sure yet" is a word, and
  // its best estimate's range stays on screen too.
  const est = proof && proof.incremental_profit;
  const parts = rangeParts(est, (v) => money(v, cur));
  const rangeLine = parts ? parts.range.replace(/^\(range /, "Range ").replace(/\)$/, "") : null;
  const profitCell = h("div", { class: `kpi lv-profit ${profit.sure ? "lv-stat-sure" : "lv-stat-unsure"}` },
    h("dt", { class: "kpi-label" }, explained("Extra profit so far", [
      "Measured against a small random group of customers it never contacts, since the goal started.",
      proofError ? `Couldn't load the proof: ${errText(proofError)}` : profit.note,
    ])),
    profit.sure && parts
      ? h("dd", { class: "kpi-value lv-money" }, parts.point)
      : h("dd", { class: "kpi-value lv-unsure" }, profit.value),
    rangeLine && h("dd", { class: "kpi-range lv-kpi-range" }, rangeLine),
    proofError && h("dd", { class: "kpi-note" }, "Couldn't load the proof"));

  // The runtime's ledger (what the budget limit counts) prices the messages sent; the discounts customers used
  // are counted on Proof, so the two spend figures differ and the description says which one this is.
  const spent = present(today.spend_to_date);
  const spentShare = spent && present(today.budget) && Number(today.budget) > 0
    ? Math.min(100, Math.max(0, (100 * Number(today.spend_to_date)) / Number(today.budget))) : null;
  const spentCell = h("div", { class: "kpi lv-spent" },
    h("dt", { class: "kpi-label" }, explained("Spent on messages", "The cost of every message sent so far, as the budget limit counts it. Proof's spend counts a different set: the discounts customers used as well, but only contacts whose results are in, so the two can differ.")),
    spent
      ? h("dd", { class: "kpi-value" }, money(today.spend_to_date, cur), h("span", { class: "lv-kpi-of" }, ` of ${money(today.budget, cur)}`))
      : h("dd", { class: "kpi-value is-missing" }, DASH),
    spentShare !== null && h("dd", { class: "lv-budget", role: "img", "aria-label": `Spent ${money(today.spend_to_date, cur)} of the ${money(today.budget, cur)} budget` },
      h("span", { class: "lv-budget-fill", style: `width: ${spentShare.toFixed(1)}%` })),
    spent && present(today.budget_left) && h("dd", { class: "kpi-note" }, `${money(today.budget_left, cur)} left`));

  // The two counts are figures inside the pitch's sentence, so the strip below does not show them a second time.
  // What was sent and delivered, and the agent against simple-rule split, are the contacted figure's description.
  const sent = shadow ? "Nothing sent." : today.autonomy === "approve" && !Number(today.messages_sent)
    ? "None sent yet: a batch goes out only after you approve it."
    : [present(today.messages_sent) ? `${count(today.messages_sent)} sent` : null, present(today.delivered) ? `${count(today.delivered)} delivered` : null].filter(Boolean).join(" · ") || null;
  const about = {
    contacted: [sent, groupNote, shadow ? "Shadow mode: nobody was actually messaged." : null],
    left: ["Doing nothing is a decision too. Each one has a reason, below."],
  };
  const tips = [];
  const figure = (part) => {
    const node = h("strong", { class: "lv-say-n num" }, part.text);
    const words = (about[part.key] || []).filter(present);
    if (!words.length) return node;
    // The box sits after the sentence (it is fixed-positioned when open), so the sentence's own text stays whole.
    const tip = h("span", { class: "info-tip" }, ...tipLines(words));
    describe(node, tip);
    tips.push(tip);
    return node;
  };
  const say = h("p", { class: "lv-say" }, ...briefingParts(today).map((p) => (typeof p === "string" ? p : figure(p))));

  return [
    say,
    tips.length > 0 && h("span", { class: "info-host" }, ...tips),
    today.run_overdue && present(today.next_run_at) && h("p", { class: "lv-checked lv-overdue" }, `The next check was due ${when(today.next_run_at)} and has not run yet.`),
    h("dl", { class: "kpis lv-stats", style: "--kpi-count: 3" },
      kpiCell("Purchases", count(today.purchases), present(today.revenue) ? `${money(today.revenue, cur)} in sales` : null, "",
        "Every purchase by these customers, not only extra ones. The extra ones are the extra profit."),
      profitCell,
      spentCell),
    h("div", { class: "lv-block" },
      h("h3", { class: "lv-subhead" }, explained("Why it left them alone",
        "It looks for people whose behaviour a message can change, and weighs the cost of bothering someone every time, so leaving people alone often wins.")),
      reasonList),
  ].filter(Boolean);
}

function learnedView(learned) {
  if (!learned || !learned.length) {
    return [lvEmpty("Nothing new yet: it notes here what it changes as it learns.")];
  }
  return [h("ul", { class: "list lv-learned" }, ...learned.map((t) => h("li", null, t)))];
}

// The briefing (often weekly): one figure per line under the Analyst's labels ("Extra profit vs people we never
// contact: $1,846 ($697 to $3,115)"), each section's sentences in the label's description. The API rebuilds these
// figures from the proof and health it serves now (Services.current_briefing), so they match Proof and the "Extra
// profit so far" tile. A section about what it learned is in its own card, so it is not repeated here. Its
// headline is visible only when there is no Yesterday card: beside one it repeats Yesterday's counts or its extra
// profit, so it moves into the meta's description (DESIGN.md section 6, "say each thing once").
function briefingView(briefing, { showHeadline = true, currency = null } = {}) {
  if (!briefing) return [lvEmpty("No briefing yet. The first one arrives after the agent's first full day.")];
  const secs = (briefing.sections || [])
    .filter((sec) => present(sec.title) || present(sec.body))
    .filter((sec) => !isLearnedSection(sec));
  const range = headlineRange(briefing, currency);
  const headline = plainGroupWords(briefing.headline);
  const rangeShown = range && !String(headline || "").includes(range);
  return [
    showHeadline && h("p", { class: "lv-headline" }, headline || DASH),
    showHeadline && rangeShown && h("p", { class: "kpi-range lv-headline-range" }, `Range ${range}`),
    secs.length > 0 && h("ul", { class: "lv-brief-secs" }, ...secs.map((sec) => {
      const kind = briefKind(sec);
      const label = kind ? BRIEF_LABELS[kind] : plainGroupWords(sec.title) || "More";
      const figure = briefFigure(sec, currency);
      return h("li", { class: `ap-brief-sec lv-brief-sec${kind ? ` lv-brief-${kind}` : ""}` },
        explained(h("span", { class: "lv-brief-title" }, label), plainGroupWords(sec.body) || DASH),
        figure && h("span", { class: "lv-brief-sep" }, ": "),
        figure && h("span", { class: `lv-brief-fig num${/^Not sure yet/.test(figure) ? " lv-brief-unsure" : ""}` }, figure));
    })),
  ].filter(Boolean);
}

/** The Analyst's measured sections, labelled the same on every briefing whatever its stored title says. */
const BRIEF_LABELS = {
  money: "Extra profit vs people we never contact",
  baseline: "Against the simple rule",
  health: "Customer health",
};

/** A "What it learned" section: shown in its own card, not as a briefing line. */
export const isLearnedSection = (sec) => /learn/i.test(String(sec?.title || ""));

/**
 * Which of the Analyst's measured sections this is, by the numbers it always carries ("money", "baseline",
 * "health"), or null for any other section (other facts, a hand-written section).
 */
export function briefKind(sec) {
  const n = sec?.numbers || {};
  const has = (k) => present(n[k]);
  if (has("incremental_profit") || (has("agent_customers") && has("holdout_customers"))) return "money";
  if (has("lift_per_customer") || has("baseline_customers")) return "baseline";
  if (has("contacts_per_customer_week") || has("complaint_rate") || (has("alerts") && has("delivery_rate"))) return "health";
  return null;
}

/** An estimate's range alone, "$697 to $3,115", in the precision `rangeParts` picks for all three. */
const rangeOnly = (parts) => parts.range.replace(/^\(range /, "").replace(/\)$/, "");

/**
 * The one figure a briefing line shows, from the section's own numbers, never from its words. Money always comes
 * with its range: "$1,846 ($697 to $3,115)", "+$4.26 a customer ($1.17 to $7.34)", "Not sure yet (-$200 to $900)"
 * when the range includes zero. Health is "0.37 messages a week, 0% complaints". Null when there is nothing to show.
 */
export function briefFigure(sec, currency) {
  const n = sec?.numbers || {};
  const kind = briefKind(sec);
  const fmt = (v) => money(v, currency);
  const est = (key) => ({ point: n[key], low: n[`${key}_low`], high: n[`${key}_high`] });
  const unsure = (e) => Number(e.low) <= 0 && Number(e.high) >= 0;
  if (kind === "money") {
    const e = est("incremental_profit");
    const parts = rangeParts(e, fmt);
    if (!parts) return "Not sure yet";
    return unsure(e) ? `Not sure yet (${rangeOnly(parts)})` : `${parts.point} (${rangeOnly(parts)})`;
  }
  if (kind === "baseline") {
    const e = est("lift_per_customer");
    const parts = rangeParts(e, fmt);
    if (!parts) return "Not sure yet";
    if (unsure(e)) return `Not sure yet (${rangeOnly(parts)} a customer)`;
    const sign = Number(e.point) > 0 ? "+" : "";
    return `${sign}${parts.point} a customer (${rangeOnly(parts)})`;
  }
  if (kind === "health") {
    const bits = [];
    if (present(n.contacts_per_customer_week)) bits.push(`${num(n.contacts_per_customer_week, 2)} messages a week`);
    if (present(n.complaint_rate)) bits.push(`${pct(n.complaint_rate)} complaints`);
    // Only a critical alert earns a place on the line; the others are in the Inbox and the description.
    if (Number(n.critical_alerts) > 0) bits.push(`${count(n.critical_alerts)} critical ${Number(n.critical_alerts) === 1 ? "alert" : "alerts"}`);
    return bits.length ? bits.join(", ") : null;
  }
  return null;
}

/**
 * The range of the money figure in a briefing's headline, from the briefing's own numbers: when a section has
 * `x`, `x_low` and `x_high` and the headline shows `x` ("an extra $1,846"), "$636 to $3,056". Null when the
 * headline has no such figure, so nothing is guessed.
 */
export function headlineRange(briefing, currency) {
  const text = String(briefing?.headline || "");
  if (!text) return null;
  for (const sec of briefing.sections || []) {
    const nums = sec?.numbers || {};
    for (const key of Object.keys(nums)) {
      const parts = rangeParts({ point: nums[key], low: nums[`${key}_low`], high: nums[`${key}_high`] }, (v) => money(v, currency));
      if (parts && text.includes(parts.point)) return parts.range.replace(/^\(range /, "").replace(/\)$/, "");
    }
  }
  return null;
}

function feedView(feed, opts) {
  const { acted, leftAlone } = splitRows(feed?.rows);
  if (!acted.length && !leftAlone.length) return [lvEmpty("No decisions yet. They appear here as the agent makes them.")];

  const find = findCustomerForm(opts.openCustomer);
  const list = h("ol", { class: "list lv-feed ap-feed", "aria-label": "Customers contacted, newest first" });
  let shown = 0;
  const more = h("button", { type: "button", class: "btn btn-secondary btn-sm lv-more" });
  const showMore = () => {
    acted.slice(shown, shown + FEED_PAGE).forEach((r) => list.append(feedItem(r, opts)));
    shown = Math.min(acted.length, shown + FEED_PAGE);
    more.hidden = shown >= acted.length;
    more.textContent = `Show ${Math.min(FEED_PAGE, acted.length - shown)} more`;
  };
  more.addEventListener("click", showMore);
  showMore();

  const aloneList = h("ol", { class: "list lv-feed ap-feed ap-feed-quiet" }, ...leftAlone.map((r) => feedItem(r, opts)));
  const alone = h("details", { class: "ap-left-alone lv-fold" }, h("summary", null, `Left alone (${leftAlone.length})`), aloneList);

  // replaceChildren() turns `false` into the text "false", so drop the parts that are not shown.
  return [
    h("div", { class: "lv-feed-tools" }, h("h3", { class: "lv-subhead" }, `Contacted (${acted.length})`), find),
    acted.length ? list : lvEmpty("Nobody contacted in this window."),
    acted.length > FEED_PAGE ? more : null,
    leftAlone.length > 0 ? alone : null,
  ].filter(Boolean);
}

/** "Open a customer": any customer, by id, not only the ones in the latest decisions. */
function findCustomerForm(openCustomer) {
  const input = h("input", { class: "lv-find-input", id: "lv-find", name: "customer", type: "search", autocomplete: "off", placeholder: "Customer id", "aria-label": "Look up any customer by their id" });
  const btn = h("button", { type: "submit", class: "btn btn-secondary" }, "Open");
  const form = h("form", { class: "lv-find", role: "search" }, h("div", { class: "lv-find-row" }, input, btn));
  form.addEventListener("submit", (e) => {
    if (e && typeof e.preventDefault === "function") e.preventDefault();
    const id = String(input.value || "").trim();
    if (id) openCustomer(id, input);
  });
  return form;
}

/** The customer's first name when the data has one, with the id small and muted; else the id alone. */
function whoButton(customerId, name, openCustomer) {
  const btn = present(name)
    ? h("button", { type: "button", class: "lv-who", "aria-label": `Open ${name} (customer ${customerId})` }, name, h("span", { class: "lv-id" }, customerId))
    : h("button", { type: "button", class: "lv-who lv-who-id", "aria-label": `Open customer ${customerId}` }, customerId);
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

// One line per decision: the person (first name, id muted), the action, the reason in short and what happened. The
// whole reason, the message, the send time and when it decided are the short reason's description (hover or tap),
// and everything else about the person is one tap away in the side panel.
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
  const alone = !acted && x?.left_alone ? x.left_alone : null;
  const short = alone ? reasonShort(alone.code, alone.words) : shortReason(d.reason);
  const why = explained(h("span", { class: "lv-why ap-why" }, short), [
    alone && h("span", { class: "lv-tip-line lv-alone-why" }, alone.words),
    h("span", { class: "lv-tip-line" }, d.reason || DASH),
    h("span", { class: "lv-tip-line lv-combo" }, [...combo, ...meta].join(" · ")),
  ]);
  return h(
    "li",
    { class: `lv-row ap-feed-item ${acted ? "" : "ap-feed-nothing lv-row-alone"} ${img ? "lv-row-img" : ""}`.trim().replace(/\s+/g, " ") },
    h("div", { class: "lv-row-who" }, who),
    h("div", { class: "lv-row-what" },
      img && h("img", { class: "lv-thumb", src: img, alt: "The image in this message", loading: "lazy", width: "24", height: "24",
        // An image that cannot load leaves the row rather than showing a broken-image mark.
        onerror: (e) => e?.target?.remove?.() }),
      h("span", { class: "lv-row-act" },
        h("span", { class: "lv-action ap-what" }, describeAction(d, opts.brief)),
        d.group && d.group !== "agent" && chip(GROUP_CHIP[d.group] || groupLabel(d.group), "quiet", "lv-group")),
      why),
    status && statusMark(status.word, status.tone),
  );
}

// `inBanner`: the self-pauses the amber banner above already names. Say each thing once: their row keeps its chips
// and time, and its words become the description of a short line.
function inboxView(alerts, inBanner = new Set()) {
  if (!alerts.length) return [lvEmpty("Nothing needs you.")];
  return [
    h(
      "ul",
      { class: "list lv-inbox ap-inbox" },
      ...alerts.map((a) => {
        const sev = SEVERITY[a.severity] || SEVERITY.info;
        return h(
          "li",
          { class: `lv-alert ap-alert ap-sev-${a.severity || "info"}` },
          h("div", { class: "lv-alert-top" },
            chip(sev.word, sev.tone, "ap-sev"),
            a.auto_paused && chip("Auto-paused", "quiet", "ap-chip-paused"),
            h("span", { class: "lv-alert-when" }, when(a.at))),
          inBanner.has(a) && present(a.message)
            ? h("p", { class: "lv-alert-msg lv-alert-named" }, explained("Named in the banner above", a.message))
            : h("p", { class: "lv-alert-msg" }, a.message || DASH),
          !present(a.goal_id) && h("p", { class: "lv-meta" }, "Affects every goal"),
        );
      }),
    ),
  ];
}

/** Inbox items Live lists for a goal: its own and the account-wide ones, in the order the API sends (worst first). */
export function inboxItemsFor(view, goalId) {
  const items = view && Array.isArray(view.items) ? view.items : [];
  return items.filter((i) => i && (!present(i.goal_id) || i.goal_id === goalId));
}

/** The kind of an inbox item, in a word or two, and its tone (autonomy/inbox/items.py InboxKind). */
export const INBOX_KINDS = Object.freeze({
  safety_stop: ["Safety stop", "bad"],
  losing: ["Losing money", "bad"],
  reconnect: ["Reconnect", "warn"],
  data: ["Data", "warn"],
  approval: ["Your call", "info"],
  customer: ["Customer", "info"],
  privacy: ["Privacy request", "info"],
});

/** The exceptions-only inbox: each item its kind, one sentence (the why behind it), money with its range, one button. */
function inboxItemsView(items, view, press) {
  if (!items.length) {
    const quiet = view && view.quiet && present(view.quiet.body) ? view.quiet.body : null;
    return [lvEmpty(quiet ? explained("Nothing needs you.", quiet) : "Nothing needs you.")];
  }
  return [
    h(
      "ul",
      { class: "list lv-inbox lv-inbox-items" },
      ...items.map((item) => {
        const [word, tone] = INBOX_KINDS[item.kind] || [String(item.kind || ""), "quiet"];
        const btn = h("button", { type: "button", class: "btn btn-secondary btn-sm lv-inbox-btn", "aria-label": `${item.button?.label || "Open"}. ${item.action || ""}`.trim() }, item.button?.label || "Open");
        btn.addEventListener("click", () => press(item, btn));
        const cur = item.currency || "USD";
        return h(
          "li",
          { class: `lv-alert lv-inbox-item lv-inbox-${item.kind}`, dataset: { item: item.item_id } },
          h("div", { class: "lv-alert-top" }, chip(word, tone, "lv-inbox-kind"), present(item.opened_at) && h("span", { class: "lv-alert-when" }, when(item.opened_at))),
          h("p", { class: "lv-alert-msg" }, explained(item.sentence, [item.action, item.detail])),
          item.money && present(item.money.low) && h("p", { class: "lv-meta num" }, `${money(item.money.point, cur)} (${money(item.money.low, cur)} to ${money(item.money.high, cur)})`),
          h("div", { class: "lv-inbox-actions" }, withInfo(btn, item.action)),
        );
      }),
    ),
  ];
}

/**
 * "Turn on Autopilot?": what it would have made a month, with its range; the proof against the people we never
 * contact (Approve) or how well its spend forecasts matched (Shadow); the server's one sentence; one button.
 */
function offerView(r, goal, turnOn) {
  const cur = r.currency || goal.currency;
  const usd = (v) => money(v, cur);
  const cells = [];
  if (r.forecast) cells.push(kpiCell("Would make a month", usd(r.forecast.point), `${usd(r.forecast.low)} to ${usd(r.forecast.high)}`, "", "Its own forecast of extra profit after offer and message costs, at the rate of the days it ran."));
  if (r.observed) cells.push(kpiCell("Proof so far", usd(r.observed.point), `${usd(r.observed.low)} to ${usd(r.observed.high)}`, "", "Extra profit measured against the people we never contact, over closed outcome windows."));
  else if (r.value_error && present(r.value_error.point)) {
    const e = Number(r.value_error.point);
    cells.push(kpiCell("Spend forecasts", e >= 0 ? `+${pct(e)}` : pct(e), `${count(r.value_checks)} purchases checked`, "", "What customers really spent per purchase against what it forecast. Spending more than forecast makes its figure cautious."));
  }
  const btn = h("button", { type: "button", class: `btn ${goal.autonomy === "approve" ? "btn-secondary" : "btn-primary"} lv-offer-btn`, "aria-label": "Turn on Autopilot: it acts on its own inside your signed limits and drops back to Approve by itself if proof turns negative." }, "Turn on Autopilot");
  btn.addEventListener("click", () => turnOn(btn));
  const title = h("h2", { class: "card-title", id: "lv-offer-title" }, "Turn on Autopilot?");
  // The numbers are the strip's; the line says how it earned them, and the server's own sentence and every check
  // are its description (say each thing once).
  const level = r.level === "approve" ? "Approve" : "Shadow";
  const line = present(r.days) ? `Earned in ${count(r.days)} days of ${level}: every check passed.` : "Every check passed.";
  return h("section", { class: "card lv-card lv-offer", "aria-labelledby": "lv-offer-title" },
    h("div", { class: "card-head" }, h("div", { class: "lv-title-row" }, title)),
    cells.length && h("dl", { class: "kpis lv-offer-kpis", style: `--kpi-count: ${cells.length}` }, ...cells),
    h("p", { class: "lv-offer-sentence" }, explained(line, [r.sentence, r.detail])),
    h("div", { class: "lv-offer-actions" }, withInfo(btn, "It acts on its own inside your signed limits, keeps the people we never contact, and drops back to Approve by itself if proof turns negative.")));
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

/** The other safety stops' short names (the plan's own words are each one's description). */
const STOP_SHORT = { spend_pace: "Spending pace", whatsapp_quality: "WhatsApp quality" };

// Health: one compact list, a figure's name on the left and its value on the right. Each limit (where it pauses,
// and what) is the name's description; a figure past its limit says so in a red pill. The other safety stops
// follow in the same list, each with "On", "Not running yet" or its current figure.
function healthView(health, brief, limits) {
  if (!health && !limits) return [lvEmpty("No health numbers yet.")];
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
    { label: "Weekly messages per customer", value: num(health?.contacts_per_customer_week, 2),
      note: ["Messages per customer per week, on average.", present(cap) ? `Hard cap ${cap} per customer` : null].filter(Boolean).join(" "),
      bad: present(health?.contacts_per_customer_week) && present(cap) && health.contacts_per_customer_week > cap },
  ];
  const others = (limits?.rules || []).filter((r) => !["delivery_rate_below", "complaint_rate", "unsubscribe_multiple"].includes(r.metric));
  for (const r of others) {
    const now = r.metric === "spend_pace" && present(r.current) ? ` Now: spending at ${pct(r.current)} of the planned pace.` : "";
    tiles.push({
      label: STOP_SHORT[r.metric] || humanize(r.metric),
      value: r.judged === false ? "Not running yet" : r.metric === "spend_pace" && present(r.current) ? pct(r.current) : "On",
      note: `${r.words || humanize(r.metric)}${now}`,
      bad: false,
      stop: true,
    });
  }
  return [
    h("dl", { class: "lv-metrics" }, ...tiles.map((t) =>
      h("div", { class: `lv-metric ${t.stop ? "lv-metric-stop" : "ap-tile"} ${t.bad ? "ap-tile-bad lv-metric-bad" : ""}`.trim() },
        h("dt", { class: "lv-metric-label" }, explained(t.label, t.note)),
        h("dd", { class: `lv-metric-value ${t.stop ? "" : "ap-tile-value "}num${t.value === DASH ? " is-missing" : ""}${t.stop && !/\d/.test(t.value) ? " lv-metric-word" : ""}` }, t.value,
          t.bad && h("span", { class: "visually-hidden" }, " · over the limit")),
        t.bad && h("dd", { class: "lv-metric-flag" }, chip("Over the limit", "bad"))),
    )),
    !present(health?.as_of || limits?.as_of) && h("p", { class: "lv-foot" }, `As of ${DASH}`),
  ].filter(Boolean);
}

// ---- the customer side panel ------------------------------------------------------------------------

/**
 * Open the side panel for one customer; returns its close function. Focus moves into the panel, stays inside it
 * while it is open (Tab wraps), and goes back to the button that opened it; Escape and the backdrop close it.
 */
function customerPanel(host, ctx, customerId, opts, opener, onClosed) {
  const title = h("h2", { class: "lv-panel-title", id: "ap-panel-title" }, ...panelTitle(customerId, opts.name));
  const closeBtn = h("button", { type: "button", class: "btn btn-ghost btn-sm lv-panel-close", "aria-label": "Close customer panel" }, "Close");
  const body = h("div", { class: "ap-panel-body lv-panel-body" }, skeletonBlock([40, 20, 20, 20, 20, 120]));
  const panel = h("aside", { class: "ap-panel lv-panel", role: "dialog", "aria-modal": "true", "aria-labelledby": "ap-panel-title", tabindex: "-1" },
    h("div", { class: "ap-panel-head lv-panel-head" }, title, closeBtn), body);
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
  const load = () => Promise.all([call(ctx.api, "getCustomer", customerId), insightLoad]).then(
    ([bundle, insight]) => {
      if (!open) return;
      const name = insight?.display_name || opts.name;
      if (present(name)) title.replaceChildren(...panelTitle(customerId, name));
      body.replaceChildren(...customerView(bundle, insight || null, opts));
    },
    (e) => {
      if (!open) return;
      body.replaceChildren(notFound(e) ? lvEmpty(`No details for customer ${customerId} yet.`) : lvFailed(`customer ${customerId}`, e, () => {
        body.replaceChildren(skeletonBlock([40, 20, 20, 20]));
        load();
      }));
    },
  );
  load();
  return close;
}

function panelTitle(customerId, name) {
  return present(name) ? [String(name), " ", h("span", { class: "lv-id" }, customerId)] : [`Customer ${customerId}`];
}

const PREFERENCE_WORDS = { no_discounts: "No discounts" };

function knowList(st, insight, opts) {
  const items = [];
  const add = (label, text, note = null) => {
    if (present(text)) {
      items.push(h("li", { class: "lv-know-item" },
        h("span", { class: "lv-know-label" }, label, h("span", { class: "visually-hidden" }, ": ")),
        // How it worked it out ("from 14 opens, clicks and replies") is the words' description.
        h("span", { class: "lv-know-text" }, explained(text, note ? capital(String(note).replace(/^\((.*)\)$/, "$1")) : null))));
    }
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

/** A titled group in the panel, split from the one above by a hairline. */
const panelSection = (title, about, ...kids) =>
  h("section", { class: "lv-panel-sec" }, h("h3", { class: "lv-panel-h" }, title, present(about) ? info(about, { label: `About ${title}` }) : null), ...kids);

function customerView(bundle, insight, opts) {
  const { currency, brief } = opts;
  const st = bundle?.state;
  if (!st) return [lvEmpty("We have no record of this customer.")];
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
  const NUMERIC = new Set(["Purchases", "Spent with you", "Average order", "Messages (last 7 / 30 days)"]);

  const channels = Object.entries(st.channels || {}).map(([ch, cs]) => {
    const reach = cs.unsubscribed ? "unsubscribed" : cs.address_known === false ? "no address" : cs.consent === false ? "has not agreed to messages" : "can contact";
    const counts = present(cs.sends) ? ` · ${count(cs.sends)} sent · ${count(cs.delivers)} delivered · ${count(cs.clicks)} ${Number(cs.clicks) === 1 ? "click" : "clicks"} · ${count(cs.replies)} ${Number(cs.replies) === 1 ? "reply" : "replies"}` : "";
    return h("li", { class: "lv-channel" }, h("span", { class: "row-title" }, channelLabel(ch)), h("span", { class: "lv-channel-facts" }, `: ${reach}${counts}`));
  });

  const items = insight?.items || null;
  const entries = timeline(bundle).map(({ at, kind, item }) => {
    if (kind === "event") {
      return h("li", { class: "ap-tl-item ap-tl-event lv-tl-item" },
        h("span", { class: "ap-tl-when lv-tl-when" }, when(at)),
        h("span", { class: "lv-tl-event" }, describeEvent(item, currency, items)));
    }
    return decisionEntry(at, item, insight, opts);
  });
  const know = knowList(st, insight, opts);

  return [
    panelSection("What it knows about them", null,
      know.length ? h("ul", { class: "lv-know" }, ...know) : lvEmpty("Nothing worked out yet beyond the facts below.")),
    panelSection("Facts", null,
      h("dl", { class: "lv-facts ap-facts" }, ...rows.map(([k, v]) => {
        // A value we do not have is a faint dash, so the facts we do have stand out.
        const missing = /^[\s/\u2014]+$/.test(String(v));
        const cls = [NUMERIC.has(k) ? "num" : null, missing ? "is-missing" : null].filter(Boolean).join(" ") || null;
        return h("div", null, h("dt", null, k), h("dd", { class: cls }, v));
      }))),
    panelSection("Channels", null,
      channels.length ? h("ul", { class: "list lv-channels" }, ...channels) : lvEmpty("No channels known.")),
    panelSection("Every decision and what happened",
      brief ? "Forecasts, and each option it weighed, are its own estimate before acting, not results. Results are on Proof." : null,
      entries.length ? h("ol", { class: "ap-timeline lv-timeline" }, ...entries) : lvEmpty("Nothing has happened yet.")),
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
  // A left-alone decision already says its rule on the line under it; "Ruled out" does not say it again.
  const same = (a, b) => String(a || "").replace(/\.$/, "").trim().toLowerCase() === String(b || "").replace(/\.$/, "").trim().toLowerCase();
  const ruled = (d.blocked_by || []).map(ruleWords).filter((w) => !(alone && same(w, alone.words)));
  // The decision, what happened, the message and the reason in short stay on the panel; the whole reason is the short
  // reason's description, and its forecast, the bar a message had to clear and the group note are the action's.
  const head = acted ? `Decided: ${describeAction(d, brief)}` : "Decided: leave them alone";
  const notes = [forecast, bar, holdout ? "In the small random group it never contacts, so Proof can measure the difference." : null].filter(Boolean);
  return h("li", { class: "ap-tl-item ap-tl-decision lv-tl-item lv-tl-decision" },
    h("span", { class: "ap-tl-when lv-tl-when" }, when(at)),
    h("div", { class: "lv-tl-head" },
      explained(h("span", { class: "lv-action ap-what" }, head), notes.length ? notes.join(" ") : null),
      status && h("span", null, " ", chip(status.word, status.tone))),
    alone && h("p", { class: "lv-alone-why" }, alone.words),
    combo.length > 0 && h("p", { class: "lv-combo" }, combo.join(" · ")),
    // The reason in short, as in the feed; the whole of it is the short reason's description (hover, focus or tap).
    h("p", { class: "lv-why ap-why" }, h("span", { class: "ap-why-label" }, "Why: "),
      present(d.reason) && shortReason(d.reason) !== d.reason ? explained(shortReason(d.reason), d.reason) : d.reason || DASH),
    ruled.length > 0 && h("p", { class: "lv-meta" }, `Ruled out: ${ruled.join("; ")}.`),
    (d.bids || []).length > 0 && h("details", { class: "ap-bids lv-fold lv-bids" }, h("summary", null, `Options it weighed (${d.bids.length})`),
      h("ol", { class: "lv-bid-list" }, ...bidLabels(d.bids, opts).map((label, i) => bidItem(d.bids[i], d, label, opts)))),
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
  const chosen = verdict === "Chosen";
  return h("li", { class: chosen ? "lv-bid lv-bid-chosen" : "lv-bid" },
    h("span", { class: "lv-bid-name" }, label),
    `: ${lift ? `${capital(lift)}; ` : ""}${net} after ${costs.length ? costs.join(" and ") : "its costs"}. `,
    chosen ? chip(verdict, "good", "lv-verdict lv-verdict-chosen") : h("span", { class: "lv-verdict" }, verdict));
}
