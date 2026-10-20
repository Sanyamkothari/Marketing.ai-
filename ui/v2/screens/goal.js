// Screen 2, Goal: the marketer says what they want in their own words, with a budget. The Strategist answers
// with a plan (POST /v2/goals -> {goal, brief}) and we go straight to the Plan screen to review it. Below the box:
// the opportunities the Analyst found in the data, each one click from becoming a goal, and the goals already set.

import { button, card, clear, el, emptyState, errorBox, humanize, loading, money, pill, statusTone } from "../components.js";

/** Starting points. The words only: no number is suggested for the marketer. */
export const EXAMPLES = Object.freeze([
  { label: "Win back lapsed customers", text: "Win back customers who haven't bought in a while" },
  { label: "Grow repeat purchases", text: "Get first-time buyers to buy a second time" },
  { label: "Recover carts", text: "Recover carts people left without checking out" },
]);

export const CURRENCIES = Object.freeze(["USD", "EUR", "GBP", "INR", "AUD", "CAD", "SGD", "AED"]);

const GOAL_KIND_WORDS = {
  win_back: "Win back",
  repeat_purchase: "Repeat purchase",
  cart_recovery: "Cart recovery",
  cross_sell: "Cross-sell",
  onboarding: "Onboarding",
  retention: "Retention",
  custom: "Custom",
};

let uid = 0;

/** Create the goal, tell the shell, and open its plan. Shared by the form and the opportunity cards. */
async function startGoal(ctx, body, busy) {
  busy(true);
  try {
    const out = await ctx.api.createGoal(body);
    const goal = out && out.goal;
    if (!goal || !goal.goal_id) throw new Error("The answer had no goal in it.");
    if (ctx.refreshGoals) await ctx.refreshGoals();
    ctx.toast(out.brief ? "Here is the plan. Review it, then approve." : "Goal saved. The plan is still being drafted.", "good");
    ctx.navigate("plan", goal.goal_id);
  } catch (err) {
    ctx.toast(`Could not start the goal. ${err.message || ""}`.trim(), "critical");
    busy(false);
  }
}

function goalForm(ctx, defaultCurrency) {
  const id = `g${++uid}`;
  const text = el("textarea", {
    id: `${id}-text`,
    name: "text",
    class: "big-input",
    rows: "3",
    required: true,
    maxlength: "500",
    placeholder: "For example: win back customers who haven't bought in 60 days",
    "aria-describedby": `${id}-text-help ${id}-text-error`,
  });
  const textError = el("p", { id: `${id}-text-error`, class: "field-error", "aria-live": "polite" });
  const budget = el("input", {
    id: `${id}-budget`,
    name: "budget",
    type: "number",
    inputmode: "decimal",
    min: "0",
    step: "any",
    required: true,
    "aria-describedby": `${id}-budget-help ${id}-budget-error`,
  });
  const budgetError = el("p", { id: `${id}-budget-error`, class: "field-error", "aria-live": "polite" });
  const currency = el(
    "select",
    { id: `${id}-currency`, name: "currency" },
    [...new Set([defaultCurrency, ...CURRENCIES])].map((c) => el("option", { value: c, selected: c === defaultCurrency }, c)),
  );
  const submit = el("button", { type: "submit", class: "btn btn-primary btn-lg" }, "Make a plan");
  const status = el("p", { class: "muted", role: "status" });

  const chips = el(
    "div",
    { class: "chips", role: "group", "aria-label": "Examples" },
    EXAMPLES.map((ex) =>
      el(
        "button",
        {
          type: "button",
          class: "chip",
          onclick: () => {
            text.value = ex.text;
            textError.textContent = "";
            text.focus();
          },
        },
        ex.label,
      ),
    ),
  );

  const busy = (on) => {
    submit.disabled = on;
    submit.textContent = on ? "Drafting the plan…" : "Make a plan";
    status.textContent = on ? "The Strategist is turning your words into a plan." : "";
  };

  const form = el(
    "form",
    {
      class: "goal-form",
      novalidate: true,
      onsubmit: (e) => {
        e.preventDefault();
        const words = text.value.trim();
        const amount = budget.value === "" ? NaN : Number(budget.value);
        textError.textContent = words ? "" : "Say what you want to achieve, in your own words.";
        budgetError.textContent = Number.isFinite(amount) && amount >= 0 ? "" : "Enter a budget of zero or more.";
        text.setAttribute("aria-invalid", words ? "false" : "true");
        budget.setAttribute("aria-invalid", budgetError.textContent ? "true" : "false");
        if (!words) return text.focus();
        if (budgetError.textContent) return budget.focus();
        startGoal(ctx, { text: words, budget: amount, currency: currency.value }, busy);
      },
    },
    el("label", { for: text.id, class: "big-label" }, "What do you want to achieve?"),
    el("p", { id: `${id}-text-help`, class: "muted" }, "Plain words are best. Say who, and what you want them to do."),
    text,
    textError,
    chips,
    el(
      "div",
      { class: "form-row" },
      el("div", { class: "field" }, el("label", { for: budget.id }, "Budget"), budget, el("p", { id: `${id}-budget-help`, class: "muted small" }, "The most it may spend on offers and messages."), budgetError),
      el("div", { class: "field field-narrow" }, el("label", { for: currency.id }, "Currency"), currency),
    ),
    el("div", { class: "form-actions" }, submit, status),
  );
  return form;
}

function opportunityCard(ctx, opp) {
  const start = button("Start this goal", null, { kind: "primary", ariaLabel: `Start this goal: ${opp.text}` });
  start.addEventListener("click", () =>
    startGoal(ctx, { text: opp.text, budget: opp.budget, currency: opp.currency, kind: opp.kind, objective: opp.objective }, (on) => {
      start.disabled = on;
      start.textContent = on ? "Starting…" : "Start this goal";
    }),
  );
  return card({
    className: "opportunity",
    level: 3,
    title: opp.text,
    body: [
      el("p", {}, pill(GOAL_KIND_WORDS[opp.kind] || humanize(opp.kind), "info"), " ", el("span", { class: "muted" }, `Aims for ${humanize(opp.objective).toLowerCase()}`)),
      el("dl", { class: "facts" }, el("dt", {}, "Suggested budget"), el("dd", {}, money(opp.budget, opp.currency))),
    ],
    actions: [start],
  });
}

function goalsList(ctx, goals) {
  if (!goals.length) return null;
  return el(
    "section",
    { class: "section", "aria-labelledby": "your-goals" },
    el("h2", { id: "your-goals" }, "Your goals"),
    el(
      "ul",
      { class: "goal-list" },
      goals.map((g) =>
        el(
          "li",
          {},
          el("a", { href: `#/plan/${encodeURIComponent(g.goal_id)}` }, g.text || g.goal_id),
          " ",
          pill(humanize(g.status), statusTone(g.status)),
          el("span", { class: "muted small" }, ` ${money(g.budget, g.currency)} budget`),
        ),
      ),
    ),
  );
}

export async function render(root, ctx) {
  const head = [el("h1", {}, "Goal"), el("p", { class: "lede" }, "Tell Autonomy what you want. It drafts a plan you can check before anything is sent.")];
  clear(root, ...head, loading());
  const [goals, opps] = await Promise.all([
    ctx.api.listGoals().catch(() => []),
    ctx.api.listOpportunities().then(
      (v) => v,
      (err) => err,
    ),
  ]);
  const goalList = Array.isArray(goals) ? goals : [];
  const defaultCurrency = (ctx.goal && ctx.goal.currency) || (goalList[0] && goalList[0].currency) || "USD";

  let oppSection;
  if (opps instanceof Error) oppSection = errorBox("Could not load the opportunities.", opps);
  else if (!Array.isArray(opps) || !opps.length) oppSection = emptyState("No opportunities found yet", "When the Analyst spots one in your data, it shows up here.");
  else oppSection = el("div", { class: "card-grid" }, opps.map((o) => opportunityCard(ctx, o)));

  clear(
    root,
    ...head,
    card({ className: "goal-box", body: [goalForm(ctx, defaultCurrency)] }),
    el(
      "section",
      { class: "section", "aria-labelledby": "opps" },
      el("h2", { id: "opps" }, "Opportunities the Analyst found"),
      el("p", { class: "muted" }, "Spotted in your data. Start one and you will see its plan before anything happens."),
      oppSection,
    ),
    goalsList(ctx, goalList),
  );
}
