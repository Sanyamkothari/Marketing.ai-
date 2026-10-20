// Screen 2, Goal: the marketer says what they want in their own words ("Win back customers who stopped buying,
// $5,000 this month"). The server reads the amount and the period from the words (POST /v2/goals/read) and the
// screen says what it read, so the budget never has to be typed twice. The Strategist answers with a plan
// (POST /v2/goals -> {goal, brief}) and we go straight to the Plan screen to review it. Below the box: the
// opportunities the Analyst found, each with how many customers it is about and why, one click from a plan.
// Every number shown comes from the API or from the marketer's own words. Words (DESIGN.md section 6): each field is
// its label, with its hint in the label's (i); the rest (what the budget covers, why an opportunity was found, what Shadow means) is a
// description on the label, number or pill it explains.

import { button, card, clear, el, emptyState, errorBox, goalStatusWords, humanize, info, int, money, pageHead, pill, present, skeleton, skeletonLines, statusTone, titleInfo, withInfo } from "../components.js";

/**
 * Starting points. The first is the pitch's own example, amount included: it is the marketer's words to change,
 * and the screen reads the amount back as "We read $5,000 as your budget. Change it if that's wrong."
 */
export const EXAMPLES = Object.freeze([
  { label: "Win back lapsed customers", text: "Win back customers who stopped buying, $5,000 this month" },
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

/** What follows the number of customers in an opportunity's headline: "1,900 customers are drifting away". */
const KIND_PHRASE = {
  win_back: ["customer is drifting away", "customers are drifting away"],
  retention: ["customer may be about to drift away", "customers may be about to drift away"],
  repeat_purchase: ["customer bought once and not again", "customers bought once and not again"],
  cart_recovery: ["customer left a cart behind", "customers left a cart behind"],
  cross_sell: ["customer could be ready for something new", "customers could be ready for something new"],
  onboarding: ["customer signed up but has not bought yet", "customers signed up but have not bought yet"],
  custom: ["customer matches this", "customers match this"],
};

export const AUTONOMY_WORDS = Object.freeze({
  shadow: ["Shadow", "info", "decides, sends nothing"],
  approve: ["Approve", "warning", "you approve each batch"],
  autopilot: ["Autopilot", "good", "acts inside your limits"],
});

/** The words after the count, e.g. "customers are drifting away"; `opportunityHeadline` puts the count first. */
export function opportunityWords(kind, n) {
  const [one, many] = KIND_PHRASE[kind] || KIND_PHRASE.custom;
  return Number(n) === 1 ? one : many;
}

/** "1,900 customers are drifting away." */
export const opportunityHeadline = (kind, n) => `${int(n)} ${opportunityWords(kind, n)}.`;

/** A date as "31 Oct 2026" in UTC. The server's end is exclusive (midnight after the last day), so step back 1s. */
export function lastDay(iso) {
  if (!present(iso)) return null;
  const at = new Date(iso);
  if (Number.isNaN(at.getTime())) return null;
  return new Date(at.getTime() - 1000).toLocaleDateString("en-GB", { day: "numeric", month: "short", year: "numeric", timeZone: "UTC" });
}

/** What the screen says about what it read from the words. Built from the reading's fields, never invented. */
export function readingWords(reading, typedBudget, currency) {
  if (!reading) return "";
  const cur = reading.currency || currency || "USD";
  const parts = [];
  if (present(reading.budget)) parts.push(`${money(reading.budget, cur)} as your budget`);
  const end = lastDay(reading.ends_at);
  if (present(reading.period) && end) parts.push(`'${reading.period}' as the period, so it runs until ${end}`);
  if (!parts.length) return "";
  let out = `We read ${parts.join(" and ")}. Change it if that's wrong.`;
  if (present(reading.budget) && present(typedBudget) && Number(typedBudget) !== Number(reading.budget)) {
    out += ` The Budget box says ${money(typedBudget, currency)}, and that is what will be used.`;
  }
  return out;
}

let uid = 0;

/** Create the goal, tell the shell, and open its plan. Shared by the form and the opportunity cards. */
async function startGoal(ctx, body, busy) {
  busy(true);
  try {
    const out = await ctx.api.createGoal(body);
    const goal = out && out.goal;
    if (!goal || !goal.goal_id) throw new Error("The answer had no goal in it.");
    if (ctx.refreshGoals) await ctx.refreshGoals();
    const end = lastDay(goal.ends_at);
    const runs = end ? ` It runs until ${end}.` : "";
    ctx.toast(out.brief ? `Here is the plan. Review it, then approve.${runs}` : `Goal saved. The plan is still being drafted.${runs}`, "good");
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
    placeholder: `For example: ${EXAMPLES[0].text}`,
    "aria-describedby": `${id}-text-help ${id}-reading ${id}-text-error`,
  });
  const textError = el("p", { id: `${id}-text-error`, class: "field-error", "aria-live": "polite" });
  const readNote = el("p", { id: `${id}-reading`, class: "gl-reading", role: "status", "aria-live": "polite", hidden: true });
  const budget = el("input", {
    id: `${id}-budget`,
    name: "budget",
    type: "number",
    inputmode: "decimal",
    min: "0",
    step: "any",
    "aria-describedby": `${id}-budget-help ${id}-budget-error`,
  });
  const budgetError = el("p", { id: `${id}-budget-error`, class: "field-error", "aria-live": "polite" });
  const options = [...new Set([defaultCurrency, ...CURRENCIES])];
  const currency = el(
    "select",
    { id: `${id}-currency`, name: "currency" },
    options.map((c) => el("option", { value: c, selected: c === defaultCurrency }, c)),
  );
  currency.value = defaultCurrency;
  const submit = el("button", { type: "submit", class: "btn btn-primary" }, "Make a plan");
  const status = el("p", { class: "gl-status", role: "status" });

  // ---- reading the words: the amount and the period ----
  let reading = null; // the latest POST /v2/goals/read answer for the words in the box
  let readFor = null; // the words that answer was for
  let autoFilled = null; // the budget value this screen put in the box (so a typed one is never overwritten)
  let canRead = typeof ctx.api.readGoal === "function";
  let pending = null;
  let timer = null;

  const showReading = () => {
    const words = readingWords(reading, budget.value === "" ? null : budget.value, currency.value);
    readNote.textContent = words;
    readNote.hidden = !words;
  };

  const readNow = async () => {
    clearTimeout(timer);
    timer = null;
    const words = text.value.trim();
    if (words === readFor) return pending;
    readFor = words;
    if (!words || !canRead) {
      reading = null;
      if (autoFilled !== null && budget.value === autoFilled) budget.value = "";
      autoFilled = null;
      showReading();
      return null;
    }
    const mine = (pending = ctx.api.readGoal(words).then(
      (r) => r,
      (err) => {
        // An API without the reader: the server still reads the words when the budget is left empty.
        if (err && (err.status === 404 || err.status === 405)) canRead = false;
        return null;
      },
    ));
    const r = await mine;
    if (readFor !== words) return pending; // the words changed while we waited
    reading = r;
    if (r && present(r.budget)) {
      if (budget.value === "" || budget.value === autoFilled) {
        budget.value = String(r.budget);
        autoFilled = budget.value;
        budgetError.textContent = "";
        budget.setAttribute("aria-invalid", "false");
      }
      if (r.currency && autoFilled === budget.value) {
        if (!options.includes(r.currency)) currency.append(el("option", { value: r.currency }, r.currency));
        currency.value = r.currency;
      }
    } else if (autoFilled !== null && budget.value === autoFilled) {
      budget.value = "";
      autoFilled = null;
    }
    showReading();
    return r;
  };
  const readSoon = () => {
    clearTimeout(timer);
    timer = setTimeout(readNow, 400);
  };
  text.addEventListener("input", () => {
    textError.textContent = "";
    readSoon();
  });
  text.addEventListener("blur", () => readNow());
  budget.addEventListener("input", showReading);
  currency.addEventListener("change", showReading);

  const chips = el(
    "div",
    { class: "chips gl-examples", role: "group", "aria-label": "Examples" },
    el("span", { class: "gl-examples-label", "aria-hidden": "true" }, "Try"),
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
            return readNow();
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

  const onSubmit = async (e) => {
    e.preventDefault();
    const words = text.value.trim();
    textError.textContent = words ? "" : "Say what you want to achieve, in your own words.";
    text.setAttribute("aria-invalid", words ? "false" : "true");
    if (!words) return text.focus();
    await readNow();
    const typed = budget.value === "" ? null : Number(budget.value);
    let problem = "";
    if (typed !== null && !(Number.isFinite(typed) && typed >= 0)) problem = "Enter a budget of zero or more.";
    // With no budget typed or read, an API that cannot read the words here still reads them itself (400 if none).
    else if (typed === null && canRead) problem = "Type a budget, or say it in your words (for example “$2,000 this month”).";
    budgetError.textContent = problem;
    budget.setAttribute("aria-invalid", problem ? "true" : "false");
    if (problem) return budget.focus();
    const body = { text: words, currency: currency.value };
    if (typed !== null) body.budget = typed;
    if (reading && present(reading.ends_at) && present(reading.period)) body.ends_at = reading.ends_at;
    return startGoal(ctx, body, busy);
  };

  return el(
    "form",
    { class: "goal-form", novalidate: true, onsubmit: onSubmit },
    // The box's placeholder already shows an example; how to say it is the label's (i), read with the box.
    el(
      "label",
      { for: text.id, class: "big-label" },
      "What do you want to achieve?",
      info("Say who, what you want them to do, and how much to spend.", { label: "How to say it", id: `${id}-text-help` }),
    ),
    text,
    textError,
    readNote,
    chips,
    el(
      "div",
      { class: "form-row gl-money" },
      el(
        "div",
        { class: "field" },
        // The (i) is a button inside the label, so a tap opens the description instead of focusing the box.
        el(
          "label",
          { for: budget.id, class: "gl-label" },
          "Budget",
          info("The most it may spend on offers and messages. Filled in when your words name an amount.", { label: "What the budget covers", id: `${id}-budget-help` }),
        ),
        budget,
        budgetError,
      ),
      el("div", { class: "field field-narrow" }, el("label", { for: currency.id }, "Currency"), currency),
    ),
    el("div", { class: "form-actions" }, submit, status),
    el(
      "p",
      { class: "gl-safe" },
      withInfo(
        el("span", {}, "Every new goal starts in Shadow."),
        "Nothing is sent yet. In Shadow, Autonomy decides and shows you what it would do, but sends nothing. Messages go out only after you approve the plan and choose to move it out of Shadow.",
      ),
    ),
  );
}

// ---- opportunities ------------------------------------------------------------------------------------------
// One card, one row per opportunity: the count of customers (the figure the pitch leads with) and what they did,
// then the goal in its own words and why, then the suggested budget and the one-click ask. A row list rather than a
// grid of cards, so one opportunity or five read the same and no half-empty row is left behind.
function opportunityRow(ctx, opp) {
  const goal = opp.goal;
  const sized = present(opp.audience_n);
  const label = "Yes, draft a plan";
  const start = button(label, null, { kind: "secondary", ariaLabel: `Yes, draft a plan: ${goal.text}` });
  start.addEventListener("click", () =>
    startGoal(ctx, { text: goal.text, budget: goal.budget, currency: goal.currency, kind: goal.kind, objective: goal.objective }, (on) => {
      start.disabled = on;
      start.textContent = on ? "Drafting…" : label;
    }),
  );
  const none = sized && Number(opp.audience_n) === 0;
  // Why it was found, and that the count is today's, is the count's description (the headline already says what they did).
  // Its kind ("Aims for …") joins the why: the headline already says what these customers did, so no pill repeats it.
  const aims = `${GOAL_KIND_WORDS[goal.kind] || humanize(goal.kind)}: aims for ${humanize(goal.objective).toLowerCase()}.`;
  const why = [present(opp.reason) ? `Why: ${opp.reason}` : "", sized ? "Counted from your customers today." : "", aims].filter(Boolean).join(" ");
  return el(
    "li",
    { class: ["opportunity", "gl-opp", !sized && "gl-opp-unsized"] },
    sized ? el("p", { class: "gl-opp-count" }, withInfo(el("span", { class: "gl-opp-n" }, int(opp.audience_n)), why), " ", el("span", { class: "gl-opp-words" }, `${opportunityWords(goal.kind, opp.audience_n)}.`)) : null,
    el(
      "div",
      { class: "gl-opp-main" },
      // Without a count there is no number to hold the why: the reason is on the page, the kind in its description.
      el("h3", { class: "gl-opp-title" }, !sized && !present(opp.reason) ? withInfo(el("span", {}, goal.text), aims) : goal.text),
      !sized && present(opp.reason) ? el("p", { class: "gl-opp-why" }, withInfo(el("span", {}, `Why: ${opp.reason}`), aims)) : null,
    ),
    el(
      "div",
      { class: "gl-opp-act" },
      el("dl", { class: "gl-opp-budget" }, el("dt", {}, "Suggested budget"), el("dd", { class: "tabular" }, money(goal.budget, goal.currency))),
      el("p", { class: "gl-opp-ask" }, none ? "No customers match today. Want a plan anyway?" : sized ? "Want me to handle them?" : "Want me to handle this?"),
      start,
    ),
  );
}

function opportunitiesSection(ctx, list, error, retry) {
  let body;
  if (error) body = el("div", { class: "gl-pad" }, errorBox("Could not load the opportunities.", error, { retry }));
  else if (!list.length) body = el("div", { class: "gl-pad" }, emptyState("No opportunities found yet", "When the Analyst spots one in your data, it shows up here."));
  else body = el("ul", { class: "gl-opps" }, list.map((o) => opportunityRow(ctx, o)));
  return el(
    "section",
    { class: "card card-flush gl-opps-card", "aria-labelledby": "opps" },
    el(
      "div",
      { class: "card-head gl-opps-head" },
      el("h2", { id: "opps", class: "card-title" }, titleInfo("Or pick one it found for you", "Spotted in your data by the Analyst. Each one starts in Shadow, and you see its plan before anything is sent.", { label: "About these suggestions" })),
    ),
    body,
  );
}

// ---- the rail: your goals, or what happens next ----------------------------------------------------------------
function goalsList(goals) {
  if (!goals.length) return null;
  return el(
    "section",
    { class: "card gl-goals", "aria-labelledby": "your-goals" },
    el("h2", { id: "your-goals", class: "card-title" }, "Your goals"),
    el(
      "ul",
      { class: "goal-list" },
      goals.map((g) => {
        const auto = AUTONOMY_WORDS[g.autonomy];
        const end = lastDay(g.ends_at);
        return el(
          "li",
          {},
          el("a", { class: "row-title gl-goal-link", href: `#/plan/${encodeURIComponent(g.goal_id)}` }, g.text || g.goal_id),
          el(
            "div",
            { class: "gl-goal-pills" },
            pill(goalStatusWords(g.status), g.status === "draft" ? "neutral" : statusTone(g.status)),
            " ",
            auto ? withInfo(pill(auto[0], "neutral"), `${auto[0]}: ${auto[2]}.`) : null,
          ),
          el("p", { class: "row-meta gl-goal-meta" }, `${money(g.budget, g.currency)} budget${end ? ` · until ${end}` : ""}`),
        );
      }),
    ),
  );
}

/** With no goal yet, the rail says what happens after "Make a plan": the safe path, in the pitch's own order. */
function nextSteps() {
  return el(
    "section",
    { class: "card gl-next", "aria-labelledby": "gl-next-title" },
    el("h2", { id: "gl-next-title", class: "card-title" }, "What happens next"),
    el(
      "ol",
      { class: "gl-next-list" },
      el("li", {}, withInfo(el("span", {}, "You check the plan."), "It shows in plain words who it will contact, with what message, and within which limits.")),
      el("li", {}, withInfo(el("span", {}, "It runs in Shadow."), "It decides every day what it would do, and why, but sends nothing.")),
      el("li", {}, withInfo(el("span", {}, "You choose when it sends."), "First you approve each batch, inside a small budget.")),
    ),
    el("p", { class: "gl-next-link" }, el("a", { href: "#/start" }, "See the whole path in Getting started")),
  );
}

/** The opportunities with their sizes when the API has /v2/opportunities/sized; otherwise the plain list. */
async function loadOpportunities(api) {
  if (typeof api.listSizedOpportunities === "function") {
    try {
      const sized = await api.listSizedOpportunities();
      if (Array.isArray(sized)) return { list: sized.filter((o) => o && o.goal) };
    } catch {
      // fall back to the plain list below
    }
  }
  try {
    const plain = await api.listOpportunities();
    return { list: (Array.isArray(plain) ? plain : []).map((goal) => ({ goal, audience_n: null, reason: null })) };
  } catch (error) {
    return { list: [], error };
  }
}

// ---- the screen -----------------------------------------------------------------------------------------------
// One question: what do you want to achieve? The box answers it, in 8 of 12 columns; your goals (or what happens
// next) sit beside it; the opportunities it found run full width underneath.
// The form's own label asks the question, so the head has no grey line: what happens next is its description.
const head = () => pageHead({ title: "Goal", info: "Tell Autonomy what you want, in your own words. It drafts a plan you can check before anything is sent." });

function skeletonScreen() {
  return [
    head(),
    el(
      "div",
      { class: "grid gl-grid" },
      el("div", { class: "card goal-box span-8", "aria-busy": "true" }, skeleton({ width: "220px", height: "24px" }), skeleton({ height: "100px", className: "gl-skel-box" }), skeletonLines(2)),
      card({ title: "Your goals", level: 2, className: "span-4", body: [skeletonLines(3)] }),
    ),
  ];
}

export async function render(root, ctx) {
  clear(root, ...skeletonScreen());
  const [goals, opps] = await Promise.all([ctx.api.listGoals().catch(() => []), loadOpportunities(ctx.api)]);
  const goalList = Array.isArray(goals) ? goals : [];
  const defaultCurrency = (ctx.goal && ctx.goal.currency) || (goalList[0] && goalList[0].currency) || "USD";
  clear(
    root,
    head(),
    el(
      "div",
      { class: "grid gl-grid" },
      el("section", { class: "card goal-box span-8", "aria-label": "Your goal, in your own words" }, goalForm(ctx, defaultCurrency)),
      el("div", { class: "span-4 stack gl-rail" }, goalsList(goalList) || nextSteps()),
    ),
    opportunitiesSection(ctx, opps.list, opps.error, () => render(root, ctx)),
  );
}
