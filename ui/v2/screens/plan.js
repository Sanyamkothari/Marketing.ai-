// Screen 3, Plan: the Strategist's plan in plain words, the rules it can never break, the messages it wrote
// (each with the strict editor's verdict), and how much it may do on its own. Approving here is the one decision
// the marketer must make before anything goes out: the plan (who, offers, channels, how often, quiet hours,
// budget, when it pauses itself) is the box the agent then acts inside.
//
// Data: GET /v2/goals/{id} (goal, brief, content), GET /v2/goals/{id}/plan (how many people match, the hard
// rules worded with this plan's values, the editor's checklist per message) and GET /v2/data/status (how many
// products the order data names). Every number comes from those answers; anything they leave out is an em dash
// or a sentence saying it is not known. The only numbers not about this business are the product test in
// "How we tested this", which is labelled as a fact about Autonomy, with its source.

import { button, card, clear, dash, el, emptyState, errorBox, goalStatusWords, hour, humanize, int, kpiStrip, money, notice, pageHead, pct, pill, present, skeletonKpis, skeletonLines, slotted, statusTone, when } from "../components.js";

/** The autonomy dial, in the order of how much the agent may do alone. One sentence each, in plain words. */
export const AUTONOMY = Object.freeze([
  {
    value: "shadow",
    label: "Shadow",
    hint: "Recommended to start",
    text: "It decides and logs what it would send, but sends nothing, so you can watch how it thinks first.",
  },
  {
    value: "approve",
    label: "Approve each batch",
    hint: "Next, with a small budget",
    text: "It prepares each batch of messages and waits for you to approve it before anything is sent.",
  },
  {
    value: "autopilot",
    label: "Autopilot",
    hint: "When Proof shows real extra profit",
    text: "It acts on its own inside this plan, and you can pause it at any time with one click.",
  },
]);

const CHANNEL_WORDS = {
  email: "Email",
  whatsapp: "WhatsApp",
  sms: "SMS",
  push: "Push notifications",
  web: "On your website",
  webhook: "Your own tool (webhook)",
};

/** Channels Autonomy hands to another tool, which does the sending (PITCH: "SMS and your own tools through a hand-off"). */
const HANDOFF = new Set(["sms", "webhook"]);

const OBJECTIVE_WORDS = {
  conversion: "Get people to buy",
  revenue: "Grow revenue",
  gross_profit: "Grow profit (revenue minus discounts and costs)",
  retention: "Keep customers coming back",
  lead: "Get new leads",
  activation: "Get new customers started",
};

const OP_WORDS = {
  eq: "is",
  ne: "is not",
  gt: "more than",
  gte: "at least",
  lt: "less than",
  lte: "at most",
  in: "is one of",
  not_in: "is not one of",
  is_null: "is not known",
  not_null: "is known",
};

/** The strict editor's five checks, in the order the pitch names them, in words a shop owner uses. */
export const CHECKS = Object.freeze([
  { check: "brand_voice", words: "On brand" },
  { check: "honesty", words: "No false claims" },
  { check: "no_invented_numbers", words: "No made-up numbers" },
  { check: "unsubscribe", words: "Unsubscribe line" },
  { check: "channel_rules", words: "Follows the channel's rules" },
]);

/** What each hard rule is called, so the list reads as named rules and not as sentences alone. */
const RULE_NAMES = {
  no_consent: "Consent.",
  unsubscribed: "Unsubscribes.",
  quiet_hours: "Quiet hours.",
  weekly_cap: "Weekly limit.",
  no_budget: "Budget.",
  open_complaint: "Complaints.",
  no_discounts: "No-discount requests.",
  holdout: "People we never contact.",
};

/** Formats that carry a picture, so a card can say when one is missing. */
const IMAGE_FORMATS = new Set(["text_image", "carousel", "video"]);

/**
 * Where the product test is written up. The docs are not served with the app, so the source is named in words; a
 * page that sets `window.AUTONOMY_DOCS_BASE` (the URL of the docs/v2 folder) gets a link as well.
 */
export const CATALOG_DOC = "docs/v2/CATALOG.md";

const channelName = (c) => CHANNEL_WORDS[c] || humanize(c);
const plural = (n, one, many) => `${int(n)} ${Number(n) === 1 ? one : many}`;

/** One audience filter as a phrase: "Purchases: at least 1", "Last purchase at: more than 60 days ago". */
export function filterWords(f) {
  const field = humanize(f.field);
  const value = Array.isArray(f.value) ? f.value.join(", ") : dash(f.value);
  if (f.op === "days_since_gt") return `${field}: more than ${value} days ago`;
  if (f.op === "days_since_lt") return `${field}: within the last ${value} days`;
  if (f.op === "is_null" || f.op === "not_null") return `${field} ${OP_WORDS[f.op]}`;
  return `${field}: ${OP_WORDS[f.op] || f.op} ${value}`;
}

/** One stop rule as a sentence: what is watched, the line, and what happens when it is crossed. */
export function stopRuleWords(rule) {
  const t = rule.threshold;
  const then = rule.action === "pause_channel" ? "pause that channel" : "pause the whole goal";
  const watch = {
    complaint_rate: `complaints go above ${pct(t)} of delivered messages`,
    unsubscribe_multiple: `unsubscribes reach ${dash(t)}× your usual rate`,
    delivery_rate_below: `delivery falls below ${pct(t)}`,
    spend_pace: `spending runs at ${dash(t)}× the planned pace`,
    whatsapp_quality: Number(t) >= 2 ? "the WhatsApp quality rating turns red" : "the WhatsApp quality rating turns yellow",
  }[rule.metric];
  return `If ${watch || `${humanize(rule.metric).toLowerCase()} crosses ${dash(t)}`}, ${then}.`;
}

/** One follow-up step as a sentence. */
export function followUpWords(step) {
  const days = `${int(step.after_days)} day${Number(step.after_days) === 1 ? "" : "s"}`;
  const cond = { no_purchase: "if they have not bought", no_response: "if they have not responded", always: "whatever happened" }[step.when] || "";
  const how = step.channel ? `by ${channelName(step.channel)}` : "on the channel that suits them best";
  return `After ${days}, ${cond}, follow up ${how}.`;
}

/**
 * Who the plan is for, from GET /v2/goals/{id}/plan: "2,140 customers match this today." and how they split
 * between the people we never contact, the simple rule and the agent. Null when the counts were not sent.
 */
export function audienceSentences(facts) {
  if (!facts || !present(facts.audience_n)) return null;
  const n = Number(facts.audience_n);
  if (n === 0) return { headline: "No customers match this today.", split: "It starts working as soon as someone does." };
  const headline = `${plural(n, "customer matches", "customers match")} this today.`;
  const parts = [];
  if (present(facts.holdout_n)) parts.push(`${int(facts.holdout_n)} of them are people we never contact (picked at random, so we can prove what it adds)`);
  if (present(facts.baseline_n)) parts.push(`${int(facts.baseline_n)} get a simple fixed rule, for comparison`);
  let split = parts.length ? `${parts.join(", and ")}.` : "";
  if (present(facts.agent_n)) split += `${split ? " " : ""}It looks after the other ${int(facts.agent_n)} one by one.`;
  return { headline, split };
}

/**
 * The editor's verdict on all the messages, from the plan facts' counts: "14 passed the editor; 2 failed and
 * will never be sent." Null without the counts, or with no messages yet.
 */
export function editorSummary(facts) {
  if (!facts || !present(facts.editor_passed) || !present(facts.editor_failed)) return null;
  if (Number(facts.editor_passed) + Number(facts.editor_failed) + Number(facts.not_reviewed || 0) === 0) return null;
  const failed = Number(facts.editor_failed) > 0 ? `${int(facts.editor_failed)} failed and will never be sent` : "none failed";
  let s = `${int(facts.editor_passed)} passed the editor; ${failed}`;
  if (Number(facts.not_reviewed) > 0) s += `; ${int(facts.not_reviewed)} not checked by the editor yet`;
  return `${s}.`;
}

/** What has happened to one message, in words: the editor's verdict and the marketer's choice kept apart. */
export function messageState(item, review) {
  const failed = Boolean(review && review.passed === false);
  const passed = Boolean(review && review.passed === true);
  switch (item.status) {
    case "approved":
      if (failed) return { label: "Approved by a person, failed the editor", tone: "warning", key: "ready" };
      if (passed) return { label: "Ready to send", tone: "good", key: "ready" };
      return { label: "Ready to send: approved by a person, not checked by the editor", tone: "warning", key: "ready" };
    case "rejected":
      return failed && review.reviewed_by !== "person"
        ? { label: "Failed the editor: never sent", tone: "critical", key: "failed" }
        : { label: "Turned off by you", tone: "neutral", key: "off" };
    case "retired":
      return { label: "Retired: no longer sent", tone: "neutral", key: "off" };
    case "draft":
      if (failed) return { label: "Failed the editor: never sent", tone: "critical", key: "failed" };
      if (passed) return { label: "Passed the editor, waiting for you", tone: "info", key: "waiting" };
      return { label: "Not checked by the editor yet", tone: "info", key: "waiting" };
    default:
      return { label: humanize(item.status), tone: statusTone(item.status), key: "other" };
  }
}

/**
 * The editor's verdict read from a message's own notes, for an API that does not send the plan facts. It reads only
 * the Critic's verdict lines (`rule: ...`, `rejected: ...`, `approved`), the same way the API does, so a message the
 * editor failed is never shown as unchecked or offered an Approve button. Null when the notes carry no verdict.
 */
export function reviewFromNotes(item) {
  const notes = (item && item.critic_notes) || [];
  if (notes.some((n) => typeof n === "string" && (n.startsWith("rejected:") || n.startsWith("rule:")))) {
    return { content_id: item.content_id, passed: false, reviewed_by: "critic", checks: [], notes };
  }
  if (notes.includes("approved")) {
    return { content_id: item.content_id, passed: true, reviewed_by: item.status === "rejected" ? "person" : "critic", checks: [], notes };
  }
  return null;
}

function offerWords(offer, currency) {
  const bits = [];
  if (offer.kind !== "none" && Number(offer.cost_per_redemption) > 0) bits.push(`costs about ${money(offer.cost_per_redemption, currency)} each time it is used`);
  if (offer.needs_approval) bits.push("needs your approval each time");
  return el("li", {}, el("strong", {}, offer.label || humanize(offer.kind)), bits.length ? ` (${bits.join("; ")})` : "");
}

const fact = (term, ...desc) => [el("dt", {}, term), el("dd", {}, desc)];

/** A card with an id, so the steps under the title can jump to it (the shim DOM's `id` is read-only). */
const withId = (node, id) => {
  node.setAttribute("id", id);
  return node;
};

/** The labels of the limits strip, also drawn while the plan loads so nothing shifts when it lands. */
const LIMIT_LABELS = Object.freeze(["Matching customers", "Budget", "Most messages a week", "Quiet hours"]);

/**
 * The plan's limits at a glance, each from the brief or the plan facts: who matches today (and how many of them
 * are never contacted), the budget, the weekly cap and the quiet hours. A value the API did not send is the em dash.
 */
function limitsStrip(goal, brief, facts) {
  const env = (brief && brief.envelope) || {};
  const cur = goal.currency;
  const counted = facts && present(facts.audience_n);
  const quiet = present(env.quiet_start_hour) && present(env.quiet_end_hour) ? `${hour(env.quiet_start_hour)}–${hour(env.quiet_end_hour)}` : null;
  const strip = kpiStrip(
    [
      {
        label: LIMIT_LABELS[0],
        value: counted ? int(facts.audience_n) : null,
        note: counted ? (present(facts.holdout_n) ? `Today; ${int(facts.holdout_n)} of them are never contacted` : "Today") : "Not counted by this server yet",
      },
      {
        label: LIMIT_LABELS[1],
        value: money(present(env.budget) ? env.budget : goal.budget, cur),
        note: present(env.max_daily_spend) ? `At most ${money(env.max_daily_spend, cur)} a day` : null,
      },
      {
        label: LIMIT_LABELS[2],
        value: present(env.max_contacts_per_week) ? int(env.max_contacts_per_week) : null,
        note: present(env.max_contacts_per_week) ? "Per customer, across all goals" : null,
      },
      { label: LIMIT_LABELS[3], value: quiet, note: quiet ? "In each customer's own time" : null },
    ],
    { label: "The plan's limits at a glance" },
  );
  strip.classList.add("plan-limits");
  return strip;
}

function whoFact(brief, facts) {
  const filters = (brief.audience && brief.audience.filters) || [];
  const words = facts && Array.isArray(facts.audience_words) && facts.audience_words.length ? facts.audience_words : filters.map(filterWords);
  const size = audienceSentences(facts);
  return fact(
    "Who it will contact",
    el("p", { class: "fact-lead" }, dash(brief.audience && brief.audience.description)),
    words.length ? el("ul", { class: "plain-list muted" }, words.map((w) => el("li", {}, w))) : null,
    size
      ? el("p", { class: "plan-audience" }, el("strong", {}, size.headline), size.split ? el("span", { class: "muted" }, ` ${size.split}`) : "")
      : el("p", { class: "muted small plan-audience" }, "How many customers match is not available from this server yet."),
  );
}

function channelsFact(env) {
  const channels = env.channels || [];
  if (!channels.length) return fact("Channels", "—");
  const handoff = channels.filter((c) => HANDOFF.has(c));
  return fact(
    "Channels",
    channels.map(channelName).join(", "),
    handoff.length
      ? el("p", { class: "muted small" }, `${handoff.map(channelName).join(" and ")}: sent through a hand-off. Autonomy passes the message to your tool, and your tool sends it.`)
      : null,
  );
}

function briefCard(goal, brief, facts) {
  const env = brief.envelope || {};
  const cur = goal.currency;
  const offers = env.offers || [];
  const followUps = brief.follow_ups || [];
  const node = card({
    title: "The plan in plain words",
    level: 2,
    className: "brief",
    meta: present(brief.version) ? `Version ${brief.version}` : null,
    body: [
      el(
        "dl",
        { class: "facts facts-wide plan-facts" },
        whoFact(brief, facts),
        fact("What it is trying to do", OBJECTIVE_WORDS[brief.objective] || humanize(brief.objective), el("p", { class: "muted small" }, `Each contact is judged on what happens within ${int(brief.horizon_days)} days.`)),
        fact(
          "Offers it may use",
          offers.length ? el("ul", { class: "plain-list" }, offers.map((o) => offerWords(o, cur))) : "—",
          offers.length ? el("p", { class: "muted small" }, "It gives an offer only when it expects the offer to pay for itself. Someone who buys at full price gets a reminder, not a coupon.") : null,
        ),
        channelsFact(env),
        fact("At most how often", present(env.max_contacts_per_week) ? `${int(env.max_contacts_per_week)} per customer per week, counting every message from every goal` : "—"),
        fact("Quiet hours", present(env.quiet_start_hour) && present(env.quiet_end_hour) ? `Nothing sent from ${hour(env.quiet_start_hour)} to ${hour(env.quiet_end_hour)}, in each customer's own time` : "—"),
        fact(
          "Budget",
          el("span", { class: "tabular" }, money(env.budget, cur)),
          present(env.max_daily_spend) ? el("p", { class: "muted small" }, `At most ${money(env.max_daily_spend, cur)} a day.`) : null,
        ),
        fact(
          "Proof built in",
          `${pct(env.holdout_share, 1)} of customers are people we never contact (the “holdout”), and ${pct(env.baseline_share, 1)} get a simple fixed rule. Comparing everyone else with them shows what the agent really adds.`,
          present(env.exploration_floor) ? el("p", { class: "muted small" }, `At least ${pct(env.exploration_floor, 1)} of its choices try something new, so it keeps learning.`) : null,
        ),
        fact("Follow-ups", followUps.length ? el("ul", { class: "plain-list" }, followUps.map((s) => el("li", {}, followUpWords(s)))) : "None"),
      ),
      brief.rationale ? el("blockquote", { class: "rationale" }, el("p", { class: "small muted" }, "Why this plan"), el("p", {}, brief.rationale)) : null,
    ],
  });
  return withId(node, "plan-brief");
}

/**
 * The hard rules from the plan facts. Without them (an older API), the same rules worded from the brief's own
 * values: consent, unsubscribes and open complaints are always on in decide(), the others carry the plan's numbers.
 */
export function hardRulesFrom(goal, brief, facts) {
  if (facts && Array.isArray(facts.hard_rules) && facts.hard_rules.length) return facts.hard_rules;
  const env = (brief && brief.envelope) || {};
  const quiet =
    present(env.quiet_start_hour) && present(env.quiet_end_hour)
      ? `No messages between ${hour(env.quiet_start_hour)} and ${hour(env.quiet_end_hour)}, in each person's own time zone.`
      : null;
  return [
    { code: "no_consent", words: "Only messages people who agreed to hear from you on that channel." },
    { code: "unsubscribed", words: "Never messages anyone who unsubscribed." },
    quiet ? { code: "quiet_hours", words: quiet } : null,
    present(env.max_contacts_per_week) ? { code: "weekly_cap", words: `At most ${plural(env.max_contacts_per_week, "message", "messages")} per person per week, across all goals.` } : null,
    present(env.budget) ? { code: "no_budget", words: `Stops spending when the budget of ${money(env.budget, goal.currency)} is used up.` } : null,
    { code: "open_complaint", words: "No messages to anyone with an open complaint." },
  ].filter(Boolean);
}

function hardRulesCard(goal, brief, facts) {
  const rules = hardRulesFrom(goal, brief, facts);
  return card({
    title: "Rules it can never break",
    level: 2,
    className: "plan-rules",
    meta: plural(rules.length, "rule", "rules"),
    body: [
      el("p", { class: "muted card-lede" }, "These are built into how it decides. No setting, batch or Autopilot can get round them, however good a message looks."),
      el(
        "ul",
        { class: "rule-list" },
        rules.map((r) =>
          el(
            "li",
            { class: "rule" },
            el("span", { class: "rule-mark", "aria-hidden": "true" }, "✓"),
            el("span", {}, el("strong", { class: "rule-name" }, RULE_NAMES[r.code] || `${humanize(r.code)}.`), " ", r.words),
          ),
        ),
      ),
    ],
  });
}

function stopRulesCard(brief) {
  const rules = (brief.envelope && brief.envelope.stop_rules) || [];
  return card({
    title: "When it pauses itself",
    level: 2,
    className: "plan-stops",
    body: [
      el("p", { class: "muted card-lede" }, "It watches these as it sends, and stops on its own if one is crossed. You hear about it in the Inbox."),
      rules.length ? el("ul", { class: "rule-list stop-list" }, rules.map((r) => el("li", { class: "rule" }, stopRuleWords(r)))) : el("p", {}, "No automatic pauses are set for this plan."),
      el("p", { class: "muted small plan-stops-foot" }, "You can also stop everything yourself at any time with the Pause button at the top."),
    ],
  });
}

function docsLink() {
  const base = globalThis.AUTONOMY_DOCS_BASE;
  if (typeof base === "string" && base) return el("a", { href: `${base.replace(/\/$/, "")}/CATALOG.md`, rel: "noopener" }, CATALOG_DOC);
  return el("code", {}, CATALOG_DOC);
}

/** Products it may suggest: what this shop's data allows, and the product test, labelled as a fact about Autonomy. */
function productsCard(data) {
  const products = data && present(data.products) ? Number(data.products) : null;
  let shop;
  if (products === null && data) shop = "Your order data does not name products yet, so its messages cannot suggest one. Connect your product list or order lines to turn this on.";
  else if (products === null) shop = "How many products your order data names is not available from this server.";
  else shop = `Your order data names ${plural(products, "product", "products")}. It may mention one in a message: a refill when someone is due to reorder, or something people often buy with what this customer bought.`;
  return card({
    title: "Products it may suggest",
    level: 2,
    className: "plan-products",
    body: [
      el("p", {}, shop),
      el(
        "aside",
        { class: "product-fact", "aria-label": "How we tested this: a fact about Autonomy, not about your shop" },
        el("p", { class: "product-fact-label" }, "How we tested this", el("span", { class: "product-fact-tag" }, "A fact about Autonomy in general, not a result from your shop.")),
        el(
          "p",
          {},
          "On a public real shop's order history (the UCI Online Retail data, 2010 to 2011), it learned from about nine months of orders and was then checked on the next 90 days. Of the 1,858 customers who went on to buy something new to them, 34.7% bought one of its 5 suggestions, against 22.0% for the shop's 5 best-sellers: about 58% more. Nobody was sent a message in this test; it measures how well it guesses what each person buys next.",
        ),
        el("p", { class: "small muted" }, "Your own results will differ; the Proof screen shows your real numbers. Source: ", docsLink(), " (Recommendations, Evaluation)."),
      ),
    ],
  });
}

/** The picture(s) a message carries: `asset_url` may hold several, separated by spaces (a carousel). */
const assetUrls = (item) => String(item.asset_url || "").split(/\s+/).filter(Boolean);

function mediaBlock(item) {
  const what = `${channelName(item.channel)} message about ${humanize(item.theme).toLowerCase()}`;
  const urls = assetUrls(item);
  if (urls.length) {
    const kind = item.format === "video" ? "video" : "picture";
    const missing = () => el("p", { class: "message-media-missing small" }, `The ${kind} for this message could not be loaded here.`);
    // A host under the reserved ".invalid" name can never load (RFC 6761): say so without asking for it.
    if (/^https?:\/\/[^/]*\.invalid(?::\d+)?(?:\/|$)/i.test(urls[0])) return missing();
    const caption = el(
      "figcaption",
      { class: "small muted" },
      urls.length > 1 ? `The first of the ${int(urls.length)} pictures it sends with this message` : `The ${kind} it sends with this message`,
    );
    let figure;
    const fail = () => {
      if (figure && figure.parentNode) figure.replaceWith(missing());
    };
    if (item.format === "video") {
      figure = el(
        "figure",
        { class: "message-media" },
        el("video", { src: urls[0], controls: true, preload: "none", "aria-label": `Video sent with the ${what}`, onerror: fail }),
        caption,
      );
      return figure;
    }
    figure = el(
      "figure",
      { class: "message-media" },
      el("img", { src: urls[0], alt: `Picture sent with the ${what}`, loading: "lazy", decoding: "async", onerror: fail }),
      caption,
    );
    return figure;
  }
  if (IMAGE_FORMATS.has(item.format)) {
    return el("p", { class: "message-media-missing small" }, "This message has room for a picture, but none is attached yet.");
  }
  return null;
}

function verdictLine(review) {
  if (review && review.passed === false) return el("p", { class: "editor-verdict small" }, el("strong", {}, "Failed the editor."), " It will never be sent. It stays here so you can see why.");
  return null;
}

/** "Checked by the editor: passed" on one line, so a message that passed reads in two short lines, not four. */
function editorTitle(review, words = "Checked by the editor") {
  const result = review && review.passed === true ? "passed" : review && review.passed === false ? "failed" : null;
  return el("p", { class: "editor-title small" }, words, result ? [": ", el("strong", { class: `editor-result editor-result-${result}` }, result)] : null);
}

function checklist(item, review, hasFacts) {
  if (!hasFacts) {
    return el(
      "div",
      { class: ["editor", review && review.passed === false ? "editor-failed" : null] },
      editorTitle(review, "The editor"),
      verdictLine(review),
      el("p", { class: "small muted" }, "The editor's checklist is not available from this server; its notes are below."),
    );
  }
  if (!review || review.reviewed_by === null || (review.passed === null && !(review.checks || []).length)) {
    return el(
      "div",
      { class: "editor editor-none" },
      el("p", { class: "small" }, el("strong", {}, "Not checked by the editor yet."), " It is not sent unless you read it and approve it yourself."),
    );
  }
  const byName = new Map((review.checks || []).map((c) => [c.check, c]));
  const items = CHECKS.map(({ check, words }) => {
    const c = byName.get(check);
    const state = !c || c.passed === null ? "none" : c.passed ? "pass" : "fail";
    const mark = { pass: "✓", fail: "✗", none: "–" }[state];
    const said = { pass: "passed", fail: "failed", none: "not checked" }[state];
    const note = state !== "pass" && c && c.note ? c.note : null;
    // Only email carries an unsubscribe line; on other channels the editor passes this check as not needed.
    const label = state === "pass" && check === "unsubscribe" && item.channel !== "email" ? `${words}: not needed on ${channelName(item.channel)}` : words;
    return el(
      "li",
      { class: ["check", `check-${state}`] },
      el("span", { class: "check-mark", "aria-hidden": "true" }, mark),
      el("span", { class: "check-words" }, label, el("span", { class: "visually-hidden" }, `: ${said}`)),
      note ? el("span", { class: "check-note small" }, note) : null,
    );
  });
  const verdict = verdictLine(review);
  return el(
    "div",
    { class: ["editor", review.passed === false ? "editor-failed" : "editor-passed"] },
    editorTitle(review),
    el("ul", { class: "check-list" }, items),
    verdict,
  );
}

function rawNotes(item, review) {
  const notes = (review && review.notes && review.notes.length ? review.notes : item.critic_notes) || [];
  if (!notes.length) return null;
  return el("details", { class: "editor-notes" }, el("summary", { class: "small" }, "Editor's notes"), el("ul", { class: "plain-list small muted" }, notes.map((n) => el("li", {}, n))));
}

function contentCard(ctx, item, review, hasFacts, offers, onChange) {
  const article = el("article", { class: "message", "aria-label": `${channelName(item.channel)} message, ${humanize(item.theme)}` });

  const set = async (next, buttons) => {
    for (const b of buttons) b.disabled = true;
    try {
      const out = await ctx.api.setContentStatus(item.content_id, next);
      const s = out && out.status ? out.status : next;
      item = { ...item, status: s };
      onChange(item);
      draw();
      ctx.toast(s === "approved" ? "Message approved. The agent may now send it." : "Message turned off. It will not be sent.", s === "approved" ? "good" : "info");
    } catch (err) {
      for (const b of buttons) b.disabled = false;
      ctx.toast(`Could not change the message. ${err.message || ""}`.trim(), "critical");
    }
  };

  function actions() {
    if (item.status === "retired") return null;
    if (review && review.passed === false && item.status !== "approved") {
      return el("p", { class: "small muted message-never" }, "There is no Approve button: a message the editor failed is never sent.");
    }
    const approve = button(item.status === "rejected" ? "Use it again" : "Approve", null, { kind: "secondary", size: "sm", ariaLabel: `Approve message ${item.content_id}` });
    const reject = button(item.status === "approved" ? "Don't use this one" : "Reject", null, { kind: "ghost", size: "sm", ariaLabel: `Reject message ${item.content_id}` });
    approve.addEventListener("click", () => set("approved", [approve, reject]));
    reject.addEventListener("click", () => set("rejected", [approve, reject]));
    if (item.status === "approved") return el("div", { class: "message-actions" }, reject, el("span", { class: "small muted" }, "Optional"));
    if (item.status === "rejected") return el("div", { class: "message-actions" }, approve);
    return el("div", { class: "message-actions" }, approve, reject);
  }

  function draw() {
    const offer = offers.find((o) => o.offer_id === item.offer_id);
    const offerLabel = offer ? offer.label : item.offer_id === "none" ? "No offer" : item.offer_id;
    const state = messageState(item, review);
    const hasMedia = present(item.asset_url) || IMAGE_FORMATS.has(item.format);
    article.setAttribute("data-state", state.key);
    clear(
      article,
      el(
        "div",
        { class: "message-main" },
        el("div", { class: "message-head" }, el("span", { class: "message-channel" }, channelName(item.channel)), pill(state.label, state.tone)),
        mediaBlock(item),
        item.subject ? el("p", { class: "message-subject" }, slotted(item.subject)) : null,
        el("p", { class: "message-body" }, slotted(item.body)),
        el(
          "ul",
          { class: "labels", "aria-label": "Labels" },
          el("li", {}, el("span", { class: "muted" }, "Theme "), humanize(item.theme)),
          el("li", {}, el("span", { class: "muted" }, "Tone "), humanize(item.tone)),
          el("li", {}, el("span", { class: "muted" }, "Offer "), offerLabel),
          el("li", {}, el("span", { class: "muted" }, "Picture "), hasMedia ? humanize(item.format) : "None, text only"),
          item.template_name ? el("li", { class: "label-template" }, el("span", { class: "muted" }, "WhatsApp template "), item.template_name) : null,
        ),
        checklist(item, review, hasFacts),
        rawNotes(item, review),
      ),
      // The failed message's "no Approve button" sentence reads under the message; buttons sit on the top line.
      el("div", { class: ["message-side", item.status !== "retired" && review && review.passed === false && item.status !== "approved" ? "message-side-end" : null] }, actions()),
    );
  }

  draw();
  return article;
}

/** How many messages show before "Show all"; only ready or turned-off messages fold, so one that needs a look always shows. */
const FIRST_MESSAGES = 8;

function contentSection(ctx, goal, brief, content, facts) {
  const offers = (brief && brief.envelope && brief.envelope.offers) || [];
  const hasFacts = Boolean(facts && Array.isArray(facts.content));
  const reviews = new Map(hasFacts ? facts.content.map((r) => [r.content_id, r]) : []);
  const reviewOf = (c) => (hasFacts ? reviews.get(c.content_id) : reviewFromNotes(c));
  const counts = el("p", { class: "plan-counts", role: "status" });
  const items = [...content];
  let expanded = items.length <= FIRST_MESSAGES + 2;
  let cards = [];
  const more = el("div", { class: "message-more" });

  /** Show the first few and every message that needs a look; the rest fold behind one "Show all" button. */
  const fold = () => {
    let hidden = 0;
    cards.forEach((node, i) => {
      const key = messageState(items[i], reviewOf(items[i])).key;
      const show = expanded || i < FIRST_MESSAGES || !(key === "ready" || key === "off");
      if (show) node.removeAttribute("hidden");
      else {
        node.setAttribute("hidden", "");
        hidden += 1;
      }
    });
    clear(
      more,
      hidden
        ? button(`Show all ${int(items.length)} messages`, () => {
            expanded = true;
            fold();
          }, { kind: "secondary", size: "sm" })
        : null,
      hidden ? el("span", { class: "small muted" }, `${int(hidden)} more, all ready to send or turned off`) : null,
    );
    if (!hidden) more.setAttribute("hidden", "");
    else more.removeAttribute("hidden");
  };

  const recount = () => {
    const by = { ready: 0, waiting: 0, failed: 0, off: 0, other: 0 };
    for (const c of items) by[messageState(c, reviewOf(c)).key] += 1;
    const bits = [`${int(by.ready)} ready to send`];
    if (by.waiting) bits.push(`${int(by.waiting)} waiting for you`);
    if (by.failed) bits.push(`${int(by.failed)} failed the editor and will never be sent`);
    if (by.off) bits.push(`${int(by.off)} turned off`);
    counts.textContent = `${bits.join(", ")}. Only messages that are ready to send ever go out.`;
  };
  recount();
  cards = items.map((item, i) =>
    contentCard(ctx, item, reviewOf(item), hasFacts, offers, (next) => {
      items[i] = next;
      recount();
      fold();
    }),
  );
  fold();
  const summary = editorSummary(facts);
  return el(
    "section",
    { class: "plan-library", "aria-labelledby": "library" },
    el(
      "div",
      { class: "section-head" },
      el("h2", { id: "library", class: "section-title" }, "The messages it wrote"),
      items.length ? el("span", { class: "card-meta" }, plural(items.length, "message", "messages")) : null,
    ),
    el(
      "p",
      { class: "plan-library-lede" },
      "An AI copywriter wrote these, and a second AI acts as a strict editor. It checks every message: on brand, no false claims, no made-up numbers, an unsubscribe line, and the channel's rules. ",
      el("strong", {}, "Nothing goes out without passing."),
    ),
    el("div", { class: "plan-tally" }, summary ? el("p", { class: "plan-editor-summary" }, summary) : null, counts),
    el(
      "p",
      { class: "muted small plan-library-note" },
      "You do not need to approve the messages that passed the editor one by one: turning one off is optional. Highlighted words are filled in for each customer, and the agent learns which themes, tones and offers work for whom.",
    ),
    items.length ? el("div", { class: "message-list" }, cards, more) : emptyState("No messages yet", "The copywriter is still writing them."),
  );
}

function dial(ctx, goal, brief, facts, onApproved) {
  const name = `autonomy-${goal.goal_id}`;
  const approved = Boolean(brief && brief.approved_at);
  const options = AUTONOMY.map((a) => {
    const id = `${name}-${a.value}`;
    return el(
      "div",
      { class: "dial-option" },
      el("input", { type: "radio", id, name, value: a.value, checked: goal.autonomy === a.value, "aria-describedby": `${id}-text` }),
      el(
        "label",
        { for: id },
        el("span", { class: "dial-label" }, a.label),
        a.hint ? el("span", { class: ["dial-hint", a.value === "shadow" ? "dial-hint-start" : null] }, a.hint) : null,
        el("span", { id: `${id}-text`, class: "dial-text" }, a.text),
      ),
    );
  });
  const fieldset = el("fieldset", { class: "dial" }, el("legend", {}, "How much should it do on its own?"), el("div", { class: "dial-options" }, options));
  const done = goal.status === "done";
  const go = button(approved ? "Save autonomy level" : "Approve plan", null, { kind: "primary", size: "lg", block: true, disabled: done || !brief });
  const note = el("p", { class: "muted small dial-note", role: "status" });
  if (approved) {
    const since = facts && present(facts.autonomy_since) ? ` At this level since ${when(facts.autonomy_since, { time: false })}.` : "";
    note.textContent = `Approved by ${dash(brief.approved_by)} on ${when(brief.approved_at)}. Plan version ${dash(brief.version)}.${since}`;
  } else note.textContent = "Nothing is sent until you approve. You can pause at any time afterwards.";

  go.addEventListener("click", async () => {
    const chosen = fieldset.querySelector("input:checked");
    const autonomy = chosen ? chosen.value : goal.autonomy;
    go.disabled = true;
    try {
      await ctx.api.approveGoal(goal.goal_id, autonomy);
      const label = (AUTONOMY.find((a) => a.value === autonomy) || {}).label || autonomy;
      ctx.toast(approved ? `Autonomy set to ${label}.` : `Plan approved. Autonomy: ${label}.`, "good");
      await onApproved();
    } catch (err) {
      go.disabled = false;
      ctx.toast(`Could not approve the plan. ${err.message || ""}`.trim(), "critical");
    }
  });

  const node = card({
    title: "You approve once",
    level: 2,
    className: "dial-card",
    meta: approved ? "Approved" : "Not approved yet",
    body: [
      el("p", { class: "muted card-lede" }, "Read the plan and the messages, then approve once. From then on it works inside this plan, at the level you choose here. You can change the level or pause at any time."),
      fieldset,
      notice(
        el(
          "p",
          {},
          "New to Autonomy? It has not yet run with a real business, so start in Shadow: for two weeks it shows you what it would do and why, and sends nothing. ",
          el("a", { href: "#/start" }, "How getting started works"),
        ),
        { tone: "warning" },
      ),
      el("div", { class: "dial-go" }, go, note),
    ],
  });
  return withId(node, "plan-approve");
}

/** The three things to do here, as quiet steps that jump to their block (the page can be long). */
function stepsNav(root) {
  const steps = [
    ["plan-brief", "Read the plan"],
    ["library", "Look at the messages it wrote"],
    ["plan-approve", "Choose how much it does alone, and approve once"],
  ];
  const jump = (id) => {
    const target = typeof document.getElementById === "function" ? document.getElementById(id) : null;
    if (!target || typeof target.scrollIntoView !== "function") return;
    target.scrollIntoView({ block: "start" });
    const heading = target.matches && target.matches("h2") ? target : target.querySelector && target.querySelector("h2");
    if (heading) {
      heading.setAttribute("tabindex", "-1");
      heading.focus({ preventScroll: true });
    }
  };
  return el(
    "ol",
    { class: "plan-steps", "aria-label": "What to do on this page" },
    steps.map(([id, words], i) =>
      el("li", {}, el("button", { type: "button", class: "plan-step", onclick: () => jump(id), "aria-label": `Step ${i + 1}: ${words}` }, words)),
    ),
  );
}

/** Keeps the decision card clear of the sticky top bar, whose height changes with the note under it. */
function stickBelowTopBar(aside) {
  if (typeof document.querySelector !== "function") return;
  const bar = document.querySelector(".topbar");
  if (bar && bar.offsetHeight) aside.style.setProperty("--plan-sticky-top", `${bar.offsetHeight + 16}px`);
}

/** The screen while the goal loads: the same frame at the real size, grey where the words will be. */
function loadingView(root) {
  return [
    pageHead({ title: "Plan", sub: "Loading the plan…" }),
    stepsNav(root),
    (() => {
      const strip = skeletonKpis(LIMIT_LABELS);
      strip.setAttribute("class", `${strip.getAttribute("class") || "kpis"} plan-limits plan-limits-loading`);
      return strip;
    })(),
    el(
      "div",
      { class: "plan-layout" },
      el("div", { class: "plan-main" }, el("section", { class: "card" }, el("h2", { class: "card-title" }, "The plan in plain words"), skeletonLines(9, { label: "Loading the plan…" }))),
      el("div", { class: "plan-aside" }, el("section", { class: "card" }, el("h2", { class: "card-title" }, "You approve once"), skeletonLines(6))),
    ),
  ];
}

/** A promise's value, or null when it fails or sends nothing: the extra facts are optional, the plan is not. */
const settle = (p) => Promise.resolve(p).then((v) => v ?? null, () => null);

export async function render(root, ctx) {
  clear(root, loadingView(root));
  const api = ctx.api;
  const factsP = typeof api.getPlanFacts === "function" ? settle(api.getPlanFacts(ctx.goalId)) : Promise.resolve(null);
  const dataP = typeof api.getDataReport === "function" ? settle(api.getDataReport()) : Promise.resolve(null);
  let detail;
  try {
    detail = await api.getGoal(ctx.goalId);
  } catch (err) {
    // A 404 is an answer (no such goal: a stale link or a goal from another workspace), not a failure.
    const missing = err && err.status === 404;
    clear(
      root,
      pageHead({ title: "Plan" }),
      missing
        ? emptyState("This goal was not found", "It may have been removed, or the link is mistyped. Pick another goal from the switcher at the top.", button("Go to Goal", () => ctx.navigate("goal"), { kind: "primary" }))
        : errorBox("Could not load this goal's plan.", err, { retry: () => render(root, ctx) }),
    );
    return;
  }
  const goal = (detail && detail.goal) || ctx.goal;
  if (!goal) {
    clear(root, pageHead({ title: "Plan" }), emptyState("This goal was not found", "Pick another goal from the switcher at the top."));
    return;
  }
  const brief = (detail && detail.brief) || null;
  let content = detail && Array.isArray(detail.content) ? detail.content : null;
  if (!content) content = await api.getContent(goal.goal_id).catch(() => []);
  const [factsRaw, data] = await Promise.all([factsP, dataP]);
  const facts = factsRaw && factsRaw.goal_id === goal.goal_id ? factsRaw : null;

  const rerender = async () => {
    if (ctx.refreshGoals) await ctx.refreshGoals();
    await render(root, { ...ctx, goal: null });
  };

  const level = AUTONOMY.find((a) => a.value === goal.autonomy);
  const liveLink =
    goal.status === "active" || goal.status === "paused"
      ? el(
          "p",
          { class: "plan-live-link" },
          el(
            "a",
            { href: `#/live/${encodeURIComponent(goal.goal_id)}` },
            goal.autonomy === "approve" ? "Approve its batches on the Live screen →" : "See it working on the Live screen →",
          ),
        )
      : null;
  const aside = brief ? el("div", { class: "plan-aside" }, dial(ctx, goal, brief, facts, rerender), liveLink) : null;
  if (aside) stickBelowTopBar(aside);

  clear(
    root,
    pageHead({
      title: "Plan",
      sub: `“${goal.text}”`,
      actions: [
        el(
          "p",
          { class: "plan-head-tags" },
          pill(goalStatusWords(goal.status), goal.status === "draft" ? "neutral" : statusTone(goal.status), { dot: true }),
          level ? pill(`Autonomy: ${level.label}`, "neutral") : null,
        ),
      ],
    }),
    brief ? stepsNav(root) : null,
    brief ? limitsStrip(goal, brief, facts) : null,
    el(
      "div",
      { class: ["plan-layout", brief ? null : "plan-layout-single"] },
      el(
        "div",
        { class: "plan-main" },
        brief ? briefCard(goal, brief, facts) : emptyState("The plan is still being drafted", "The Strategist has not finished this plan yet. Check back in a moment."),
        brief ? hardRulesCard(goal, brief, facts) : null,
        brief ? stopRulesCard(brief) : null,
        brief ? productsCard(data) : null,
      ),
      contentSection(ctx, goal, brief, content || [], facts),
      aside,
      aside ? null : liveLink,
    ),
  );
}
