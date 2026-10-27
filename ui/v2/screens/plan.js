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

import { button, card, clear, dash, el, emptyState, errorBox, goalStatusWords, hour, humanize, info, int, isSimulatedShop, kpiStrip, money, notice, pageHead, pct, pill, present, segmented, simulatedLabel, skeletonKpis, skeletonLines, slotted, statusTone, when, withInfo } from "../components.js";

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
    short: "Approve",
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

/** What the plan is trying to do: the short words on the page, and what they mean (its description). */
const OBJECTIVE_WORDS = {
  conversion: ["Get people to buy", ""],
  revenue: ["Grow revenue", ""],
  gross_profit: ["Grow profit", "Revenue minus discounts and costs."],
  retention: ["Keep customers coming back", ""],
  lead: ["Get new leads", ""],
  activation: ["Get new customers started", ""],
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

/** What each hard rule is called: the name shows on the page, the rule's own words (with the plan's value) in its description. */
const RULE_NAMES = {
  no_consent: "Consent",
  unsubscribed: "Unsubscribes",
  quiet_hours: "Quiet hours",
  weekly_cap: "Weekly limit",
  no_budget: "Budget",
  open_complaint: "Complaints",
  no_discounts: "No-discount requests",
  holdout: "People we never contact",
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

/**
 * One stop rule as a short row: what is watched with its line ("Complaints above 0.08%"), and what happens
 * ("Pauses the goal"). The full sentence (stopRuleWords) is the row's description.
 */
export function stopRuleShort(rule) {
  const t = rule.threshold;
  const then = rule.action === "pause_channel" ? "Pauses that channel" : "Pauses the goal";
  const watch = {
    complaint_rate: `Complaints above ${pct(t)}`,
    unsubscribe_multiple: `Unsubscribes at ${dash(t)}× usual`,
    delivery_rate_below: `Delivery below ${pct(t)}`,
    spend_pace: `Spending at ${dash(t)}× the pace`,
    whatsapp_quality: Number(t) >= 2 ? "WhatsApp quality turns red" : "WhatsApp quality turns yellow",
  }[rule.metric];
  return [watch || `${humanize(rule.metric)} past ${dash(t)}`, then];
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

/** Said once, in the pill's description, for a message the editor failed. */
const FAILED_ABOUT = "It stays here so you can see why. There is no Approve button: a message the editor failed is never sent.";

/**
 * What has happened to one message, in words: the editor's verdict and the marketer's choice kept apart. `about` is
 * the pill's description (what that state means), when it needs one.
 */
export function messageState(item, review) {
  const failed = Boolean(review && review.passed === false);
  const passed = Boolean(review && review.passed === true);
  switch (item.status) {
    case "approved":
      if (failed) return { label: "Approved by a person, failed the editor", tone: "warning", key: "ready", about: "A person approved it although the editor failed it, so it may be sent." };
      if (passed) return { label: "Ready to send", tone: "good", key: "ready" };
      return { label: "Ready to send: approved by a person, not checked by the editor", tone: "warning", key: "ready" };
    case "rejected":
      return failed && review.reviewed_by !== "person"
        ? { label: "Failed the editor: never sent", tone: "critical", key: "failed", about: FAILED_ABOUT }
        : { label: "Turned off by you", tone: "neutral", key: "off", about: "It will not be sent. Use it again at any time." };
    case "retired":
      return { label: "Retired: no longer sent", tone: "neutral", key: "off" };
    case "draft":
      if (failed) return { label: "Failed the editor: never sent", tone: "critical", key: "failed", about: FAILED_ABOUT };
      if (passed) return { label: "Passed the editor, waiting for you", tone: "info", key: "waiting" };
      return { label: "Not checked by the editor yet", tone: "info", key: "waiting", about: "It is not sent until the editor has checked it and passed it. There is no Approve button before then." };
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

/** A word or a number with its description: the dotted underline, and the box on hover, focus or tap. */
const described = (text, about, attrs = {}) => withInfo(el("span", attrs, text), about);

/** One offer: its name, then its cost and approval in grey. Why it uses offers at all is the row's description. */
function offerWords(offer, currency) {
  const bits = [];
  if (offer.kind !== "none" && Number(offer.cost_per_redemption) > 0) bits.push(`about ${money(offer.cost_per_redemption, currency)} a use`);
  if (offer.needs_approval) bits.push("needs your approval each time");
  return el("li", {}, el("strong", {}, offer.label || humanize(offer.kind)), bits.length ? el("span", { class: "muted" }, ` · ${bits.join(" · ")}`) : "");
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
 * The plan's limits at a glance, each from the brief or the plan facts, said here once (the plan card and the rules
 * do not repeat them): who matches today, the budget, the weekly cap and the quiet hours. What each one means, and
 * how the matching customers split, is in the label's description. A value the API did not send is the em dash.
 */
function limitsStrip(goal, brief, facts) {
  const env = (brief && brief.envelope) || {};
  const cur = goal.currency;
  const counted = facts && present(facts.audience_n);
  const size = audienceSentences(facts);
  const quiet = present(env.quiet_start_hour) && present(env.quiet_end_hour);
  const weekly = present(env.max_contacts_per_week);
  const strip = kpiStrip(
    [
      {
        label: described(LIMIT_LABELS[0], size ? [size.headline, size.split].filter(Boolean).join(" ") : "How many customers match is not available from this server yet."),
        value: counted ? int(facts.audience_n) : null,
        note: counted ? (present(facts.holdout_n) && Number(facts.audience_n) > 0 ? `${int(facts.holdout_n)} we never contact` : "Today") : "Not counted yet",
      },
      {
        label: described(LIMIT_LABELS[1], present(env.budget) ? `It stops spending when the budget of ${money(env.budget, cur)} is used up.` : null),
        value: money(present(env.budget) ? env.budget : goal.budget, cur),
        note: present(env.max_daily_spend) ? `At most ${money(env.max_daily_spend, cur)} a day` : null,
      },
      {
        label: described(LIMIT_LABELS[2], weekly ? `${int(env.max_contacts_per_week)} per customer per week, counting every message from every goal.` : null),
        value: weekly ? int(env.max_contacts_per_week) : null,
        note: weekly ? "Per customer" : null,
      },
      {
        label: described(LIMIT_LABELS[3], quiet ? `Nothing sent from ${hour(env.quiet_start_hour)} to ${hour(env.quiet_end_hour)}, in each customer's own time.` : null),
        value: quiet ? `${hour(env.quiet_start_hour)}–${hour(env.quiet_end_hour)}` : null,
        note: quiet ? "Their own time" : null,
      },
    ],
    { label: "The plan's limits at a glance" },
  );
  strip.classList.add("plan-limits");
  return strip;
}

/** Who it will contact, in the plan's words; the filters behind them are its description (the count is in the strip). */
function whoFact(brief, facts) {
  const filters = (brief.audience && brief.audience.filters) || [];
  const words = facts && Array.isArray(facts.audience_words) && facts.audience_words.length ? facts.audience_words : filters.map(filterWords);
  const about = words.length ? `${words.join(". ")}.` : null;
  return fact("Who it will contact", el("p", { class: "fact-lead" }, described(dash(brief.audience && brief.audience.description), about)));
}

function channelsFact(env) {
  const channels = env.channels || [];
  if (!channels.length) return fact("Channels", "—");
  const handoff = channels.filter((c) => HANDOFF.has(c));
  const about = handoff.length ? `${handoff.map(channelName).join(" and ")}: sent through a hand-off. Autonomy passes the message to your tool, and your tool sends it.` : null;
  return fact("Channels", described(channels.map(channelName).join(", "), about));
}

/**
 * The follow-up steps, only when this server sends them (`facts.follow_ups_run`, true only with the v3 journeys on).
 * The plain agent decides once per customer and never sends a later step, so listing "After 3 days, follow up" would
 * promise a message that never goes out. Without the plan facts (an older API) nothing is promised either.
 */
export function followUpsFact(followUps, facts) {
  if (facts && facts.follow_ups_run === true && followUps.length) return el("ul", { class: "plain-list" }, followUps.map((s) => el("li", {}, followUpWords(s))));
  if (facts && facts.follow_ups_run === true) return "None";
  return described("Not sent", "Each decision sends at most one message. The agent decides again every day, so someone who has not bought may be contacted again then, inside the weekly limit. A planned follow-up step is not sent by this version.");
}

function briefCard(goal, brief, facts) {
  const env = brief.envelope || {};
  const cur = goal.currency;
  const offers = env.offers || [];
  const followUps = brief.follow_ups || [];
  const [aim, aimAbout] = OBJECTIVE_WORDS[brief.objective] || [humanize(brief.objective), ""];
  const judged = present(brief.horizon_days) ? `Each contact is judged on what happens within ${int(brief.horizon_days)} days.` : "";
  const proofAbout = [
    "People we never contact are picked at random. Comparing everyone else with them shows what the agent really adds.",
    present(env.exploration_floor) ? `At least ${pct(env.exploration_floor, 1)} of its choices try something new, so it keeps learning.` : null,
  ]
    .filter(Boolean)
    .join(" ");
  const meta = [
    brief.rationale ? described("Why this plan", brief.rationale, { class: "plan-why" }) : null,
    brief.rationale && present(brief.version) ? " · " : null,
    present(brief.version) ? `Version ${brief.version}` : null,
  ].filter(Boolean);
  const node = card({
    title: "The plan in plain words",
    level: 2,
    className: "brief",
    meta: meta.length ? meta : null,
    body: el(
      "dl",
      { class: "facts facts-wide plan-facts" },
      whoFact(brief, facts),
      fact("What it is trying to do", described(aim, [aimAbout, judged].filter(Boolean).join(" "))),
      fact(
        offers.length
          ? described("Offers it may use", "It gives an offer only when it expects the offer to pay for itself. Someone who buys at full price gets a reminder, not a coupon.")
          : "Offers it may use",
        offers.length ? el("ul", { class: "plain-list" }, offers.map((o) => offerWords(o, cur))) : "—",
      ),
      channelsFact(env),
      fact(
        "Proof built in",
        described(`${pct(env.holdout_share, 1)} are people we never contact; ${pct(env.baseline_share, 1)} get a simple fixed rule`, proofAbout),
      ),
      fact("Follow-ups", followUpsFact(followUps, facts)),
    ),
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

/** The rules by name; each rule's own words, with this plan's value, are its description. */
function hardRulesCard(goal, brief, facts) {
  const rules = hardRulesFrom(goal, brief, facts);
  return card({
    title: ["Rules it can never break", info("Built into how it decides. No setting, batch or Autopilot can get round them, however good a message looks.", { label: "About these rules" })],
    level: 2,
    className: "plan-rules",
    meta: plural(rules.length, "rule", "rules"),
    body: el(
      "ul",
      { class: "rule-list" },
      rules.map((r) =>
        el(
          "li",
          { class: "rule" },
          el("span", { class: "rule-mark", "aria-hidden": "true" }, "✓"),
          described(RULE_NAMES[r.code] || humanize(r.code), r.words, { class: "rule-name" }),
        ),
      ),
    ),
  });
}

function stopRulesCard(brief) {
  const rules = (brief.envelope && brief.envelope.stop_rules) || [];
  return card({
    title: [
      "When it pauses itself",
      info("It watches these as it sends, and stops on its own if one is crossed. You hear about it in the Inbox. You can also stop everything yourself at any time with the Pause button at the top.", {
        label: "About automatic pauses",
      }),
    ],
    level: 2,
    className: "plan-stops",
    // The rows are grouped by what happens, so "Pauses the goal" is said once, not on every row.
    body: rules.length
      ? ["pause_goal", "pause_channel"].map((action) => {
          const mine = rules.filter((r) => (r.action === "pause_channel" ? "pause_channel" : "pause_goal") === action);
          if (!mine.length) return null;
          return el(
            "div",
            { class: "stop-group" },
            el("p", { class: "stop-then" }, stopRuleShort(mine[0])[1]),
            el(
              "ul",
              { class: "rule-list stop-list" },
              mine.map((r) => el("li", { class: "rule stop" }, described(stopRuleShort(r)[0], stopRuleWords(r), { class: "stop-watch" }))),
            ),
          );
        })
      : el("p", {}, "No automatic pauses are set for this plan."),
  });
}

/** The product test's method and source, said in its description (a description cannot hold a link). */
const PRODUCT_TEST_ABOUT =
  "A fact about Autonomy, not a result from your shop. Tested on a public real shop's order history (the UCI Online Retail data, 2010 to 2011): it learned from about nine months of orders, then was checked on the next 90 days, on the 1,858 customers who went on to buy something new to them. Nobody was sent a message. Your own results will differ; Proof shows your real numbers. Source: " +
  `${CATALOG_DOC} (Recommendations, Evaluation).`;

/** Products it may suggest: what this shop's data allows, and the product test, labelled as a fact about Autonomy. */
function productsCard(data) {
  const products = data && present(data.products) ? Number(data.products) : null;
  let shop;
  if (products === null && data) shop = described("Your order data does not name products yet, so it cannot suggest one.", "Connect your product list or order lines to turn this on.");
  else if (products === null) shop = "How many products your order data names is not available from this server.";
  else {
    shop = described(
      `Your order data names ${plural(products, "product", "products")}.`,
      "It may mention one in a message: a refill when someone is due to reorder, or something people often buy with what this customer bought.",
    );
  }
  const base = globalThis.AUTONOMY_DOCS_BASE;
  const source = typeof base === "string" && base ? el("a", { class: "small", href: `${base.replace(/\/$/, "")}/CATALOG.md`, rel: "noopener" }, "Source") : null;
  return card({
    title: "Products it may suggest",
    level: 2,
    className: "plan-products",
    body: [
      el("p", {}, shop),
      el(
        "aside",
        { class: "product-fact", "aria-label": "How we tested this: a fact about Autonomy, not about your shop" },
        el("p", { class: "product-fact-label" }, described("How we tested this", PRODUCT_TEST_ABOUT), el("span", { class: "product-fact-tag" }, "About Autonomy, not your shop.")),
        el("p", {}, "34.7% bought one of its 5 suggestions, against 22.0% for the shop's 5 best-sellers: about 58% more.", source ? [" ", source] : null),
      ),
    ],
  });
}

/** The picture(s) a message carries: `asset_url` may hold several, separated by spaces (a carousel). */
const assetUrls = (item) => String(item.asset_url || "").split(/\s+/).filter(Boolean);

/** A host under the reserved ".invalid" name can never load (RFC 6761): it is never asked for. */
const neverLoads = (url) => /^https?:\/\/[^/]*\.invalid(?::\d+)?(?:\/|$)/i.test(url);

/** The picture, when there is one that can load; a missing one is said in the message's labels instead. */
function mediaBlock(item) {
  const what = `${channelName(item.channel)} message about ${humanize(item.theme).toLowerCase()}`;
  const urls = assetUrls(item);
  if (!urls.length || neverLoads(urls[0])) return null;
  const kind = item.format === "video" ? "video" : "picture";
  const caption = urls.length > 1 ? el("figcaption", { class: "small muted" }, `1 of ${int(urls.length)} pictures`) : null;
  let figure;
  const fail = () => {
    if (figure && figure.parentNode) figure.replaceWith(el("p", { class: "message-media-missing small" }, `The ${kind} could not load here.`));
  };
  const media =
    item.format === "video"
      ? el("video", { src: urls[0], controls: true, preload: "none", "aria-label": `Video sent with the ${what}`, onerror: fail })
      : el("img", { src: urls[0], alt: `Picture sent with the ${what}`, loading: "lazy", decoding: "async", onerror: fail });
  figure = el("figure", { class: "message-media" }, media, caption);
  return figure;
}

/** The picture label: its format, or why there is none. */
function pictureWords(item) {
  const urls = assetUrls(item);
  if (urls.length && neverLoads(urls[0])) return `${humanize(item.format)}, could not load here`;
  if (urls.length) return humanize(item.format);
  if (IMAGE_FORMATS.has(item.format)) return described("None attached yet", "This message has room for a picture, but none is attached yet.");
  return "Text only";
}

/**
 * True when the editor passed a message on every one of its five checks, from the plan facts' checklist. Those
 * messages sit together under ONE "Checked by the editor" line instead of each repeating it; a message with a check
 * that failed or was not run keeps its own line, so whatever needs a look is said on the message itself.
 */
export function passedEveryCheck(review, hasFacts) {
  if (!hasFacts || !review || review.passed !== true || !review.reviewed_by) return false;
  const byName = new Map((review.checks || []).map((c) => [c.check, c]));
  return CHECKS.every(({ check }) => Boolean(byName.get(check) && byName.get(check).passed === true));
}

/**
 * The description of the group's "Checked by the editor" line: the five checks, said once for every message under
 * it. The unsubscribe check passes as not needed on channels other than email, so that is said when one is there.
 */
export function groupChecklist(items) {
  const others = [...new Set(items.filter((c) => c.channel !== "email").map((c) => channelName(c.channel)))];
  const names = CHECKS.map(({ check, words }) => (check === "unsubscribe" && others.length ? `${words} (not needed on ${others.join(" or ")})` : words));
  return `The strict editor passed each of these messages on every check: ${names.join(", ")}.`;
}

/**
 * The strict editor's verdict on one message, in one line. Checks that did not pass show with their reason; the full
 * checklist is the description of "Checked by the editor". Null for a message whose pill already says it is unchecked.
 */
function editorLine(item, review, hasFacts, state) {
  if (!hasFacts) {
    const result = review && review.passed === true ? "passed" : review && review.passed === false ? "failed" : null;
    return el(
      "p",
      { class: ["editor", "small", result === "failed" ? "editor-failed" : null] },
      described("The editor", "The editor's checklist is not available from this server; its notes are below."),
      result ? [": ", el("strong", { class: `editor-result editor-result-${result}` }, result)] : null,
    );
  }
  if (!review || review.reviewed_by === null || (review.passed === null && !(review.checks || []).length)) {
    return state.label.includes("Not checked by the editor yet") ? null : el("p", { class: "editor small" }, "Not checked by the editor yet.");
  }
  const byName = new Map((review.checks || []).map((c) => [c.check, c]));
  const rows = CHECKS.map(({ check, words }) => {
    const c = byName.get(check);
    const result = !c || c.passed === null ? "none" : c.passed ? "pass" : "fail";
    // Only email carries an unsubscribe line; on other channels the editor passes this check as not needed.
    const label = result === "pass" && check === "unsubscribe" && item.channel !== "email" ? `${words}: not needed on ${channelName(item.channel)}` : words;
    return { result, label, note: result !== "pass" && c && c.note ? c.note : null };
  });
  const group = (result, said) => {
    const names = rows.filter((r) => r.result === result).map((r) => r.label);
    return names.length ? `${said}: ${names.join(", ")}.` : null;
  };
  const about = [group("pass", "Passed"), group("fail", "Failed"), group("none", "Not checked")].filter(Boolean).join(" ");
  const shown = rows.filter((r) => r.result !== "pass");
  const ok = review.passed !== false && !shown.length;
  return el(
    "div",
    { class: ["editor", "small", review.passed === false ? "editor-failed" : "editor-passed", shown.length ? "editor-with-checks" : null] },
    el("p", { class: "editor-title" }, ok ? el("span", { class: "check-mark check-pass-mark", "aria-hidden": "true" }, "✓") : null, described("Checked by the editor", about)),
    shown.length
      ? el(
          "ul",
          { class: "check-list" },
          shown.map((r) =>
            el(
              "li",
              { class: ["check", `check-${r.result}`] },
              el("span", { class: "check-mark", "aria-hidden": "true" }, r.result === "fail" ? "✗" : "–"),
              el("span", { class: "check-words" }, r.label, el("span", { class: "visually-hidden" }, r.result === "fail" ? ": failed" : ": not checked")),
              r.note ? el("span", { class: "check-note" }, r.note) : null,
            ),
          ),
        )
      : null,
  );
}

function rawNotes(item, review) {
  const notes = (review && review.notes && review.notes.length ? review.notes : item.critic_notes) || [];
  if (!notes.length) return null;
  return el("details", { class: "editor-notes" }, el("summary", { class: "small" }, "Editor's notes"), el("ul", { class: "plain-list small muted" }, notes.map((n) => el("li", {}, n))));
}

/** What the quiet "turn off" icon on a ready message does, said on hover and focus (and as its label for touch). */
const TURN_OFF_ABOUT = "Don't use this one. Optional: a message that passed the editor needs no approval one by one.";

/**
 * One message. `grouped` is true for a message under the group's "Checked by the editor" line: it does not repeat
 * that line, nor a "Ready to send" pill (the group and the counts say it), and its one choice is a quiet icon.
 */
function contentCard(ctx, item, review, hasFacts, offers, onChange, grouped = false) {
  const article = el("article", { class: "message", "data-content-id": item.content_id, "aria-label": `${channelName(item.channel)} message, ${humanize(item.theme)}` });

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

  /** Approve / Reject. A message the editor failed has none: its pill's description says why. */
  function actions() {
    if (item.status === "retired") return null;
    // The server refuses to approve a message the editor did not pass (docs/v2/PROMISE_MAP.md row 23), so the
    // screen offers Approve only on one it passed. A draft the editor has not finished may still be turned off.
    const editorPassed = Boolean(review && review.passed === true);
    if (!editorPassed && item.status !== "approved") {
      if (item.status !== "draft" || (review && review.passed === false)) return null;
      const turnOff = button("Reject", null, { kind: "ghost", size: "sm", ariaLabel: `Reject message ${item.content_id}` });
      turnOff.addEventListener("click", () => set("rejected", [turnOff]));
      return el("div", { class: "message-actions" }, turnOff);
    }
    const approve = button(item.status === "rejected" ? "Use it again" : "Approve", null, { kind: "secondary", size: "sm", ariaLabel: `Approve message ${item.content_id}` });
    const reject = button(item.status === "approved" ? "Don't use this one" : "Reject", null, { kind: "ghost", size: "sm", ariaLabel: `Reject message ${item.content_id}` });
    approve.addEventListener("click", () => set("approved", [approve, reject]));
    reject.addEventListener("click", () => set("rejected", [approve, reject]));
    if (item.status === "approved" && grouped) {
      const off = el(
        "button",
        { type: "button", class: "btn btn-ghost btn-sm btn-icon message-off", "aria-label": `Don't use this one: message ${item.content_id}` },
        el("span", { class: "icon-off", "aria-hidden": "true" }),
      );
      off.addEventListener("click", () => set("rejected", [off]));
      return el("div", { class: "message-actions" }, withInfo(off, TURN_OFF_ABOUT));
    }
    if (item.status === "approved") return el("div", { class: "message-actions" }, withInfo(reject, "Optional. A message that passed the editor needs no approval one by one."));
    if (item.status === "rejected") return el("div", { class: "message-actions" }, approve);
    return el("div", { class: "message-actions" }, approve, reject);
  }

  function draw() {
    const offer = offers.find((o) => o.offer_id === item.offer_id);
    const offerLabel = offer ? offer.label : item.offer_id === "none" ? "No offer" : item.offer_id;
    const state = messageState(item, review);
    const tag = (name, value) => el("li", {}, el("span", { class: "visually-hidden" }, `${name}: `), value);
    // Under the group's line a plain "Ready to send" is the default, so only a different state shows its pill.
    const quiet = grouped && state.key === "ready" && state.tone === "good";
    article.setAttribute("data-state", state.key);
    // A screen reader hears the state with the message's name, so a quiet card loses nothing.
    article.setAttribute("aria-label", `${channelName(item.channel)} message, ${humanize(item.theme)}: ${state.label}`);
    clear(
      article,
      el(
        "div",
        { class: "message-main" },
        el(
          "div",
          { class: "message-head" },
          el("span", { class: "message-channel" }, channelName(item.channel)),
          quiet ? null : withInfo(pill(state.label, state.tone), state.about),
        ),
        mediaBlock(item),
        item.subject ? el("p", { class: "message-subject" }, slotted(item.subject)) : null,
        el("p", { class: "message-body" }, slotted(item.body)),
        el(
          "ul",
          { class: "labels message-tags", "aria-label": "Labels" },
          tag("Theme", humanize(item.theme)),
          tag("Tone", humanize(item.tone)),
          tag("Offer", offerLabel),
          tag("Picture", pictureWords(item)),
          item.template_name ? el("li", { class: "label-template" }, el("span", { class: "muted" }, "WhatsApp template "), item.template_name) : null,
        ),
        el("div", { class: "message-foot" }, grouped ? null : editorLine(item, review, hasFacts, state), rawNotes(item, review)),
      ),
      el("div", { class: "message-side" }, actions()),
    );
  }

  draw();
  return article;
}

/**
 * How many of the quiet messages (those under the editor's line, and the ones turned off) show before "Show all".
 * A message that needs a look (waiting for you, failed the editor, a check not run) always shows, first.
 */
const FIRST_MESSAGES = 2;

function contentSection(ctx, goal, brief, content, facts) {
  const offers = (brief && brief.envelope && brief.envelope.offers) || [];
  const hasFacts = Boolean(facts && Array.isArray(facts.content));
  const reviews = new Map(hasFacts ? facts.content.map((r) => [r.content_id, r]) : []);
  const reviewOf = (c) => (hasFacts ? reviews.get(c.content_id) : reviewFromNotes(c));
  const summary = editorSummary(facts);
  const countsText = el("span", { class: "plan-counts", role: "status" });
  const counts = withInfo(countsText, [summary, "Only messages that are ready to send ever go out."].filter(Boolean).join(" "));
  const items = [...content];
  let expanded = items.length <= FIRST_MESSAGES + 3;
  const more = el("div", { class: "message-more" });

  // Three runs, in this order: the messages that need a look, each with its own editor line; the ones the editor
  // passed on every check, under ONE "Checked by the editor" line; then the rest that are turned off. Membership
  // follows the editor's verdict, which a person's choice does not change, so a card never moves when clicked.
  const index = items.map((item, i) => i);
  const grouped = new Set(index.filter((i) => passedEveryCheck(reviewOf(items[i]), hasFacts)));
  // A message that needs a look never folds, even one under the group's line (a draft the editor passed is still
  // waiting for you), and it leads its run. Read once, when the screen opens, so a click never hides a card.
  const needsLook = new Set(index.filter((i) => !["ready", "off"].includes(messageState(items[i], reviewOf(items[i])).key)));
  const look = index.filter((i) => !grouped.has(i) && needsLook.has(i));
  const rest = index.filter((i) => !grouped.has(i) && !needsLook.has(i));
  const inGroup = index.filter((i) => grouped.has(i));
  const order = [...look, ...inGroup.filter((i) => needsLook.has(i)), ...inGroup.filter((i) => !needsLook.has(i)), ...rest];
  const foldable = (i) => !needsLook.has(i);
  const cards = new Map();

  /** Show every message that needs a look and the first few quiet ones; the rest fold behind one "Show all" button. */
  const fold = () => {
    let hidden = 0;
    let quiet = 0;
    for (const i of order) {
      const node = cards.get(i);
      const show = expanded || !foldable(i) || quiet < FIRST_MESSAGES;
      if (foldable(i)) quiet += 1;
      if (show) node.removeAttribute("hidden");
      else {
        node.setAttribute("hidden", "");
        hidden += 1;
      }
    }
    clear(
      more,
      hidden
        ? withInfo(
            button(`Show all ${int(items.length)} messages`, () => {
              expanded = true;
              fold();
            }, { kind: "secondary", size: "sm" }),
            `${int(hidden)} more, all ready to send or turned off.`,
          )
        : null,
    );
    if (!hidden) more.setAttribute("hidden", "");
    else more.removeAttribute("hidden");
  };

  const recount = () => {
    const by = { ready: 0, waiting: 0, failed: 0, off: 0, other: 0 };
    for (const c of items) by[messageState(c, reviewOf(c)).key] += 1;
    const bits = [`${int(by.ready)} ready to send`];
    if (by.waiting) bits.push(`${int(by.waiting)} waiting for you`);
    if (by.failed) bits.push(`${int(by.failed)} failed the editor`);
    if (by.off) bits.push(`${int(by.off)} turned off`);
    countsText.textContent = bits.join(" · ");
  };
  recount();
  for (const i of order) {
    cards.set(
      i,
      contentCard(ctx, items[i], reviewOf(items[i]), hasFacts, offers, (next) => {
        items[i] = next;
        recount();
        fold();
      }, grouped.has(i)),
    );
  }
  fold();
  const groupItems = order.filter((i) => grouped.has(i));
  const group = groupItems.length
    ? el(
        "div",
        { class: "message-group", role: "group", "aria-labelledby": "plan-checked" },
        el(
          "p",
          { class: "message-group-head small", id: "plan-checked" },
          el("span", { class: "check-mark check-pass-mark", "aria-hidden": "true" }, "✓"),
          described("Checked by the editor", groupChecklist(groupItems.map((i) => items[i]))),
        ),
        groupItems.map((i) => cards.get(i)),
      )
    : null;
  return el(
    "section",
    { class: "plan-library", "aria-labelledby": "library" },
    el(
      "div",
      { class: "section-head" },
      el(
        "h2",
        { id: "library", class: "section-title" },
        "The messages it wrote",
        info("You do not need to approve the messages that passed the editor one by one: turning one off is optional. Highlighted words are filled in for each customer, and the agent learns which themes, tones and offers work for whom.", {
          label: "About the messages",
        }),
      ),
    ),
    items.length
      ? el(
          "p",
          { class: "plan-tally" },
          withInfo(
            el("strong", { class: "plan-editor-summary" }, "Nothing goes out without passing."),
            "An AI copywriter wrote these, and a second AI acts as a strict editor. It checks every message: on brand, no false claims, no made-up numbers, an unsubscribe line, and the channel's rules.",
          ),
          " ",
          counts,
        )
      : null,
    items.length
      ? el("div", { class: "message-list" }, look.map((i) => cards.get(i)), group, rest.map((i) => cards.get(i)), more)
      : emptyState("No messages yet", "The copywriter is still writing them."),
  );
}

function dial(ctx, goal, brief, facts, onApproved) {
  const name = `autonomy-${goal.goal_id}`;
  const approved = Boolean(brief && brief.approved_at);
  // DESIGN.md's segmented control: Shadow | Approve | Autopilot, one sentence under it for the chosen level; the
  // other levels' sentences (and when to pick each) open on hover over their segment.
  const control = segmented({
    name,
    legend: "How much should it do on its own?",
    className: "dial-track",
    value: goal.autonomy,
    options: AUTONOMY.map((a) => ({ value: a.value, label: a.short || a.label, hint: a.short ? `${a.label}: ${a.hint.toLowerCase()}` : a.hint, text: a.text })),
  });
  const fieldset = control.querySelector("fieldset") || control;
  const done = goal.status === "done";
  const go = button(approved ? "Save autonomy level" : "Approve plan", null, { kind: "primary", size: "lg", block: true, disabled: done || !brief });
  let note;
  if (approved) {
    const about = [present(brief.version) ? `Plan version ${brief.version}.` : null, facts && present(facts.autonomy_since) ? `At this level since ${when(facts.autonomy_since, { time: false })}.` : null]
      .filter(Boolean)
      .join(" ");
    note = el("p", { class: "muted small dial-note", role: "status" }, described(`Approved by ${dash(brief.approved_by)} on ${when(brief.approved_at)}.`, about));
  } else note = el("p", { class: "muted small dial-note", role: "status" }, "Nothing is sent until you approve.");

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
    title: [
      "You approve once",
      info("Read the plan and the messages, then approve once. From then on it works inside this plan, at the level you choose here. You can change the level or pause at any time.", {
        label: "About approving",
      }),
    ],
    level: 2,
    className: "dial-card",
    meta: approved ? null : "Not approved yet",
    body: [
      control,
      notice(
        el(
          "p",
          {},
          described(
            "Not proven on a real business yet: start in Shadow.",
            "New to Autonomy? It has not yet run with a real business, so start in Shadow: for two weeks it shows you what it would do and why, and sends nothing.",
          ),
          " ",
          el("a", { href: "#/start" }, "Getting started"),
        ),
        { tone: "warning" },
      ),
      el("div", { class: "dial-go" }, go, note),
    ],
  });
  return withId(node, "plan-approve");
}

/** The three things to do here, as quiet steps that jump to their block (the page can be long). */
function stepsNav() {
  const steps = [
    ["plan-brief", "Read the plan"],
    ["library", "Check messages"],
    ["plan-approve", "Approve once"],
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
function loadingView() {
  return [
    pageHead({ title: "Plan", sub: "Loading the plan…" }),
    stepsNav(),
    (() => {
      const strip = skeletonKpis(LIMIT_LABELS);
      strip.setAttribute("class", `${strip.getAttribute("class") || "kpis"} plan-limits plan-limits-loading`);
      return strip;
    })(),
    el(
      "div",
      { class: "plan-layout" },
      el("div", { class: "plan-main" }, el("section", { class: "card" }, el("h2", { class: "card-title" }, "The plan in plain words"), skeletonLines(6, { label: "Loading the plan…" }))),
      el("div", { class: "plan-aside" }, el("section", { class: "card" }, el("h2", { class: "card-title" }, "You approve once"), skeletonLines(5))),
    ),
  ];
}

/** A promise's value, or null when it fails or sends nothing: the extra facts are optional, the plan is not. */
const settle = (p) => Promise.resolve(p).then((v) => v ?? null, () => null);

export async function render(root, ctx) {
  clear(root, loadingView());
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
  const simulated = await isSimulatedShop(ctx);

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

  // The autonomy level is in the top bar's badge and on the dial, so the head shows only the goal's state.
  clear(
    root,
    pageHead({
      title: "Plan",
      sub: `“${goal.text}”`,
      status: pill(goalStatusWords(goal.status), goal.status === "draft" ? "neutral" : statusTone(goal.status), { dot: true }),
    }),
    simulated ? simulatedLabel("The customers, budget and messages in this plan are for a shop we simulate, not for your customers.") : null,
    brief ? stepsNav() : null,
    brief ? limitsStrip(goal, brief, facts) : null,
    el(
      "div",
      { class: ["plan-layout", brief ? null : "plan-layout-single"] },
      el(
        "div",
        { class: "plan-main" },
        brief ? briefCard(goal, brief, facts) : emptyState("The plan is still being drafted", "The Strategist has not finished this plan yet. Check back in a moment."),
        brief ? el("div", { class: "plan-pair" }, hardRulesCard(goal, brief, facts), stopRulesCard(brief)) : null,
        brief ? productsCard(data) : null,
      ),
      contentSection(ctx, goal, brief, content || [], facts),
      aside,
      aside ? null : liveLink,
    ),
  );
}
