// Screen 3, Plan: the Strategist's brief in plain words, the messages it may send, and how much it may do on
// its own. Approving here is the one decision the marketer must make before anything goes out: the envelope
// (who, offers, channels, caps, quiet hours, budget, stop rules) is the box the agent then acts inside.
//
// Every number shown comes from the brief as sent; anything the brief leaves out is an em dash.

import {
  button,
  card,
  clear,
  dash,
  el,
  emptyState,
  errorBox,
  hour,
  humanize,
  int,
  loading,
  money,
  pct,
  pill,
  present,
  slotted,
  statusTone,
  when,
} from "../components.js";

/** The autonomy dial, in the order of how much the agent may do alone. One sentence each, in plain words. */
export const AUTONOMY = Object.freeze([
  { value: "shadow", label: "Shadow", text: "It decides and logs what it would send, but sends nothing, so you can watch how it thinks first." },
  { value: "approve", label: "Approve each batch", text: "It prepares each batch of messages and waits for you to approve it before anything is sent." },
  { value: "autopilot", label: "Autopilot", text: "It acts on its own inside this plan, and you can pause it at any time with one click." },
]);

const CHANNEL_WORDS = {
  email: "Email",
  whatsapp: "WhatsApp",
  sms: "SMS",
  push: "Push notifications",
  web: "On your website",
  webhook: "Your own tool (webhook)",
};

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

const channelName = (c) => CHANNEL_WORDS[c] || humanize(c);

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

function offerWords(offer, currency) {
  const bits = [];
  if (offer.kind !== "none" && Number(offer.cost_per_redemption) > 0) bits.push(`costs about ${money(offer.cost_per_redemption, currency)} each time it is used`);
  if (offer.needs_approval) bits.push("needs your approval each time");
  return el("li", {}, el("strong", {}, offer.label || humanize(offer.kind)), bits.length ? ` (${bits.join("; ")})` : "");
}

const fact = (term, ...desc) => [el("dt", {}, term), el("dd", {}, desc)];

function briefCard(goal, brief) {
  const env = brief.envelope || {};
  const cur = goal.currency;
  const offers = env.offers || [];
  const rules = env.stop_rules || [];
  const followUps = brief.follow_ups || [];
  const filters = (brief.audience && brief.audience.filters) || [];
  return card({
    title: "The plan in plain words",
    level: 2,
    className: "brief",
    body: [
      el(
        "dl",
        { class: "facts facts-wide" },
        fact(
          "Who",
          el("p", {}, dash(brief.audience && brief.audience.description)),
          filters.length ? el("ul", { class: "plain-list muted" }, filters.map((f) => el("li", {}, filterWords(f)))) : null,
        ),
        fact("Objective", OBJECTIVE_WORDS[brief.objective] || humanize(brief.objective), el("p", { class: "muted small" }, `Each contact is judged on what happens within ${int(brief.horizon_days)} days.`)),
        fact("Offers", offers.length ? el("ul", { class: "plain-list" }, offers.map((o) => offerWords(o, cur))) : "—"),
        fact("Channels", (env.channels || []).length ? (env.channels || []).map(channelName).join(", ") : "—"),
        fact("Most contacts per week", present(env.max_contacts_per_week) ? `${int(env.max_contacts_per_week)} per customer, across all goals` : "—"),
        fact("Quiet hours", present(env.quiet_start_hour) && present(env.quiet_end_hour) ? `Nothing sent from ${hour(env.quiet_start_hour)} to ${hour(env.quiet_end_hour)}, in each customer's own time` : "—"),
        fact(
          "Budget",
          money(env.budget, cur),
          present(env.max_daily_spend) ? el("p", { class: "muted small" }, `At most ${money(env.max_daily_spend, cur)} a day.`) : null,
        ),
        fact(
          "Proof built in",
          `${pct(env.holdout_share, 1)} of customers are never contacted and ${pct(env.baseline_share, 1)} get a simple fixed rule, so you can see what the agent adds.`,
          present(env.exploration_floor) ? el("p", { class: "muted small" }, `At least ${pct(env.exploration_floor, 1)} of its choices try something new, so it keeps learning.`) : null,
        ),
        fact("Stop rules", rules.length ? el("ul", { class: "plain-list" }, rules.map((r) => el("li", {}, stopRuleWords(r)))) : "None set"),
        fact("Follow-ups", followUps.length ? el("ul", { class: "plain-list" }, followUps.map((s) => el("li", {}, followUpWords(s)))) : "None"),
      ),
      brief.rationale ? el("blockquote", { class: "rationale" }, el("p", { class: "small muted" }, "Why this plan"), el("p", {}, brief.rationale)) : null,
    ],
  });
}

function contentCard(ctx, item, offers, onChange) {
  const offer = offers.find((o) => o.offer_id === item.offer_id);
  const offerLabel = offer ? offer.label : item.offer_id === "none" ? "No offer" : item.offer_id;
  const status = el("span", {}, pill(humanize(item.status), statusTone(item.status)));
  const approve = button("Approve", null, { kind: "secondary", ariaLabel: `Approve message ${item.content_id}` });
  const reject = button("Reject", null, { kind: "ghost", ariaLabel: `Reject message ${item.content_id}` });
  const sync = (s) => {
    approve.disabled = s === "approved";
    reject.disabled = s === "rejected";
    clear(status, pill(humanize(s), statusTone(s)));
  };
  const set = async (next) => {
    approve.disabled = reject.disabled = true;
    try {
      const out = await ctx.api.setContentStatus(item.content_id, next);
      const s = out && out.status ? out.status : next;
      item = { ...item, status: s };
      sync(s);
      onChange(item);
      ctx.toast(s === "approved" ? "Message approved. The agent may now send it." : "Message rejected. It will not be sent.", s === "approved" ? "good" : "info");
    } catch (err) {
      sync(item.status);
      ctx.toast(`Could not change the message. ${err.message || ""}`.trim(), "critical");
    }
  };
  approve.addEventListener("click", () => set("approved"));
  reject.addEventListener("click", () => set("rejected"));
  sync(item.status);

  return el(
    "article",
    { class: "card message", "aria-label": `${channelName(item.channel)} message, ${humanize(item.theme)}` },
    el("div", { class: "message-head" }, pill(channelName(item.channel), "neutral"), status),
    item.subject ? el("p", { class: "message-subject" }, slotted(item.subject)) : null,
    el("p", { class: "message-body" }, slotted(item.body)),
    el(
      "ul",
      { class: "labels", "aria-label": "Labels" },
      el("li", {}, el("span", { class: "muted" }, "Theme "), humanize(item.theme)),
      el("li", {}, el("span", { class: "muted" }, "Tone "), humanize(item.tone)),
      el("li", {}, el("span", { class: "muted" }, "Offer "), offerLabel),
      item.format && item.format !== "text" ? el("li", {}, el("span", { class: "muted" }, "Format "), humanize(item.format)) : null,
      item.template_name ? el("li", {}, el("span", { class: "muted" }, "Template "), item.template_name) : null,
    ),
    (item.critic_notes || []).length ? el("div", { class: "critic" }, el("p", { class: "small muted" }, "Critic's notes"), el("ul", { class: "plain-list small" }, item.critic_notes.map((n) => el("li", {}, n)))) : null,
    el("div", { class: "card-actions" }, approve, reject),
  );
}

function contentSection(ctx, goal, brief, content) {
  const offers = (brief && brief.envelope && brief.envelope.offers) || [];
  const counts = el("p", { class: "muted", role: "status" });
  const items = [...content];
  const recount = () => {
    const approved = items.filter((c) => c.status === "approved").length;
    const waiting = items.filter((c) => c.status === "draft").length;
    counts.textContent = `${approved} approved, ${waiting} waiting for review. Only approved messages are ever sent.`;
  };
  recount();
  return el(
    "section",
    { class: "section", "aria-labelledby": "library" },
    el("h2", { id: "library" }, "Messages it may send"),
    el("p", { class: "muted" }, "Highlighted words are filled in for each customer. The agent learns which themes, tones and offers work for whom."),
    counts,
    items.length
      ? el(
          "div",
          { class: "card-grid" },
          items.map((item, i) =>
            contentCard(ctx, item, offers, (next) => {
              items[i] = next;
              recount();
            }),
          ),
        )
      : emptyState("No messages yet", "The Creative is still writing them."),
  );
}

function dial(ctx, goal, brief, onApproved) {
  const name = `autonomy-${goal.goal_id}`;
  const approved = Boolean(brief && brief.approved_at);
  const options = AUTONOMY.map((a) => {
    const id = `${name}-${a.value}`;
    return el(
      "div",
      { class: "dial-option" },
      el("input", { type: "radio", id, name, value: a.value, checked: goal.autonomy === a.value, "aria-describedby": `${id}-text` }),
      el("label", { for: id }, el("span", { class: "dial-label" }, a.label), el("span", { id: `${id}-text`, class: "dial-text" }, a.text)),
    );
  });
  const fieldset = el("fieldset", { class: "dial" }, el("legend", {}, "How much should it do on its own?"), el("div", { class: "dial-options" }, options));
  const done = goal.status === "done";
  const go = button(approved ? "Save autonomy level" : "Approve plan", null, { kind: "primary", disabled: done || !brief });
  go.classList.add("btn-lg");
  const note = el("p", { class: "muted small", role: "status" });
  if (approved) note.textContent = `Approved by ${dash(brief.approved_by)} on ${when(brief.approved_at)}. Plan version ${dash(brief.version)}.`;
  else note.textContent = "Nothing is sent until you approve. You can pause at any time afterwards.";

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

  return card({ title: "Autonomy", level: 2, className: "dial-card", body: [fieldset, note], actions: [go] });
}

export async function render(root, ctx) {
  clear(root, el("h1", {}, "Plan"), loading("Loading the plan…"));
  let detail;
  try {
    detail = await ctx.api.getGoal(ctx.goalId);
  } catch (err) {
    // A 404 is an answer (no such goal: a stale link or a goal from another workspace), not a failure.
    const missing = err && err.status === 404;
    clear(
      root,
      el("h1", {}, "Plan"),
      missing
        ? emptyState("This goal was not found", "It may have been removed, or the link is mistyped. Pick another goal from the switcher at the top.", button("Go to Goal", () => ctx.navigate("goal"), { kind: "primary" }))
        : errorBox("Could not load this goal's plan.", err),
    );
    return;
  }
  const goal = (detail && detail.goal) || ctx.goal;
  if (!goal) {
    clear(root, el("h1", {}, "Plan"), emptyState("This goal was not found", "Pick another goal from the switcher at the top."));
    return;
  }
  const brief = (detail && detail.brief) || null;
  let content = detail && Array.isArray(detail.content) ? detail.content : null;
  if (!content) content = await ctx.api.getContent(goal.goal_id).catch(() => []);

  const rerender = async () => {
    if (ctx.refreshGoals) await ctx.refreshGoals();
    await render(root, { ...ctx, goal: null });
  };

  clear(
    root,
    el("h1", {}, "Plan"),
    el(
      "div",
      { class: "plan-head" },
      el("p", { class: "lede" }, `“${goal.text}”`),
      el("p", {}, pill(humanize(goal.status), statusTone(goal.status)), " ", el("span", { class: "muted" }, `Budget ${money(goal.budget, goal.currency)}`)),
    ),
    brief ? briefCard(goal, brief) : emptyState("The plan is still being drafted", "The Strategist has not finished this plan yet. Check back in a moment."),
    contentSection(ctx, goal, brief, content || []),
    brief ? dial(ctx, goal, brief, rerender) : null,
    goal.status === "active" || goal.status === "paused"
      ? el(
          "p",
          {},
          el(
            "a",
            { href: `#/live/${encodeURIComponent(goal.goal_id)}` },
            goal.autonomy === "approve" ? "Approve its batches on the Live screen →" : "See it working on the Live screen →",
          ),
        )
      : null,
  );
}
