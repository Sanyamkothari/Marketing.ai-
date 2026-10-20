// Getting started (#/start): the safe path from "nothing connected" to Autopilot, one step at a time, with each
// step's state read from the API, never assumed.
//
//   1 Connect your data        GET /v2/data/status (customers, orders, sources, Data Doctor questions)
//   2 Set one goal             GET /v2/goals
//   3 Shadow for two weeks     the goal's autonomy and status, GET /v2/today (what it would have done),
//                              GET /v2/expected (its own forecast, labelled as one), the approval date
//   4 Approve, small budget    PATCH /v2/goals/{id} {budget} (lowering is always allowed), POST .../approve
//   5 Autopilot when proven    GET /v2/proof: only a range wholly above zero counts as "real extra profit"
//
// With no goal yet it shows only the ask: your customer data and one goal. The product facts on this page
// ("not yet run with a real business", "3-10% is typical") are labelled as facts about Autonomy or as general
// guidance, never as numbers about this business. Every number about the business comes from the API.
//
// Words (DESIGN.md section 6): each step is its title, its state and at most one short sentence of what the data
// says. What a step means (the teaching) is the title's description, and the detail behind a number (who it left
// alone and why, what a forecast covers, what a budget change needs) is that number's description.

import { button, clear, describe, el, errorBox, info, int, money, pageHead, present, range, skeleton, when, withInfo } from "../components.js";

/** The Shadow trial the pitch asks for: two weeks. */
export const TRIAL_DAYS = 14;

/** Day n of the Shadow trial (day 1 is the day the plan was approved in Shadow), or null without a start date. */
export function trialDay(since, now = new Date()) {
  if (!present(since)) return null;
  const start = new Date(since).getTime();
  if (!Number.isFinite(start)) return null;
  return Math.max(1, Math.floor((now.getTime() - start) / 86400000) + 1);
}

/** "day 5 of 14", or "the two weeks are done (day 20)". */
export function trialWords(day) {
  if (!present(day)) return null;
  return day > TRIAL_DAYS ? `the two weeks are done (day ${day})` : `day ${day} of ${TRIAL_DAYS}`;
}

/**
 * What an estimate's range says, in words: "sure" (the whole range is above zero), "unsure" (it includes zero)
 * or "below" (the whole range is below zero). Null without a range.
 */
export function rangeVerdict(est) {
  if (!est || !present(est.low) || !present(est.high)) return null;
  if (Number(est.low) > 0) return "sure";
  if (Number(est.high) < 0) return "below";
  return "unsure";
}

const plural = (n, one, many) => `${int(n)} ${Number(n) === 1 ? one : many}`;

/** A settled promise's value, or null (and the error kept for the step that needs to say so). */
const valueOf = (r) => (r && r.status === "fulfilled" ? r.value : null);
const errorOf = (r) => (r && r.status === "rejected" ? r.reason : null);

/** A description box for `target` that sits where the caller puts it (at the end of a sentence, so the words read on). */
function tipFor(target, text) {
  const tip = el("span", {}, text);
  describe(target, tip);
  return tip;
}

const STATE_WORDS = {
  done: "Done",
  now: "Doing now",
  next: "Next",
  later: "Later",
  past: "Past this step",
  unknown: "Can't tell yet",
};

/** The pill tone for each state: green only for done, the accent only for the step being done now. */
const STATE_TONE = { done: "positive", now: "accent", next: "neutral", later: "neutral", past: "neutral", unknown: "warning" };

/** The state pill; `about` (what that state means here) is its description. */
function stateTag(state, about) {
  const tag = el("span", { class: ["pill", `pill-${STATE_TONE[state] || "neutral"}`, "ob-state", `ob-state-${state}`] }, STATE_WORDS[state] || state);
  return present(about) ? withInfo(tag, about) : tag;
}

/** The five steps: the title, and what the step means (the teaching), which is the title's description. */
const STEPS = {
  1: ["Connect your customer and order data", "Upload your files or connect your database. The Data Doctor cleans up messy data and asks you before it changes anything."],
  2: ["Set one goal, in plain words", "Type what you want, for example “Win back customers who stopped buying, $5,000 this month.” Or pick one of the opportunities it found in your data."],
  3: ["Run in Shadow mode for two weeks", "It decides every day what it would do for each customer, and why, but sends nothing. You see what it expects to earn before a single message goes out."],
  4: ["Turn on Approve mode, with a small budget", "Then it sends, but only the batches you approve with one click on the Live page. Start with a small budget."],
  5: ["Switch to Autopilot when Proof shows real extra profit", "Proof compares the customers it looks after with a small random group of people we never contact. Autopilot only makes sense once that difference is clearly above zero."],
};

/**
 * One step of the path: number, title (its meaning on hover), state (in words, not only colour), at most one short
 * sentence of what the data says, and actions. `about` is the state pill's description.
 */
function step(n, state, body, actions = [], about = null) {
  const [title, teach] = STEPS[n];
  const text = [body].flat().filter(Boolean);
  return el(
    "li",
    { class: ["ob-step", `ob-step-${state}`] },
    el("span", { class: "ob-num", "aria-hidden": "true" }, String(n)),
    el(
      "div",
      { class: "ob-step-body" },
      el(
        "div",
        { class: "ob-step-head" },
        el("h3", { class: "ob-step-title" }, el("span", { class: "visually-hidden" }, `Step ${n}: `), withInfo(el("span", {}, title), teach)),
        stateTag(state, about),
      ),
      text.length ? el("div", { class: "ob-step-text" }, text) : null,
      actions.length ? el("div", { class: "ob-actions" }, actions) : null,
    ),
  );
}

const link = (text, href) => el("a", { class: "ob-link", href }, text);

// ---- step 1: data --------------------------------------------------------------------------------------------
function dataStep(report, err) {
  const go = link("Go to Connect", "#/connect");
  if (err || !report) {
    const words = err ? "We couldn't check your data from here." : "This server can't report on your data yet.";
    return step(1, "unknown", el("p", {}, err && present(err.message) ? withInfo(el("span", {}, words), err.message) : words), [go]);
  }
  const sources = Array.isArray(report.sources) ? report.sources : [];
  const customers = Number(report.customers) || 0;
  if (!customers && !sources.length) {
    return step(1, "now", el("p", {}, "Upload your customer and order files, or connect your database."), [go]);
  }
  const questions = Array.isArray(report.questions) ? report.questions : [];
  return step(
    1,
    "done",
    [
      el(
        "p",
        {},
        `${plural(customers, "customer", "customers")}`,
        present(report.orders) ? ` and ${plural(report.orders, "order", "orders")}` : "",
        sources.length ? `, from ${plural(sources.length, "source", "sources")}.` : ".",
      ),
      questions.length
        ? el(
            "p",
            { class: "notice ob-attention" },
            withInfo(el("span", {}, `The Data Doctor has ${plural(questions.length, "question", "questions")} for you.`), "It won't change anything until you answer."),
          )
        : null,
    ],
    // A done step needs no link: Connect is step 1 in the nav. Only open questions ask for a visit.
    questions.length ? [link("Answer on Connect", "#/connect")] : [],
  );
}

// ---- step 2: one goal ------------------------------------------------------------------------------------------
function goalStep(goals, goal, dataMissing) {
  if (!goals.length) {
    return step(2, dataMissing ? "next" : "now", el("p", {}, "Type what you want, or pick an opportunity it found."), [link("Set a goal", "#/goal")]);
  }
  const name = el("strong", {}, goal.text || goal.goal_id);
  return step(
    2,
    "done",
    el(
      "p",
      {},
      goals.length > 1 ? withInfo(name, `You have ${plural(goals.length, "goal", "goals")}. Switch between them at the top; we suggest starting with one.`) : name,
    ),
    // Goal is step 2 in the nav, so the done step carries no link of its own.
  );
}

// ---- step 3: Shadow ----------------------------------------------------------------------------------------------
function shadowFacts(goal, today, expected) {
  const out = [];
  if (today && Number(today.customers_considered) === 0 && goal.status === "paused") {
    out.push(el("p", {}, "It made no decisions before it was paused."));
  } else if (today && Number(today.customers_considered) === 0) {
    const next = present(today.next_run_at) ? `; the next daily run is ${when(today.next_run_at)}` : "";
    out.push(el("p", {}, withInfo(el("span", {}, `No decisions yet${next}.`), "Come back after it runs to see what it would do, and why.")));
  } else if (today && present(today.contacted)) {
    // Who it left alone, and why: the reasons are the description of that number, read after the sentence.
    const alone = el("span", {}, `left ${int(today.left_alone)} alone`);
    const reasons = Array.isArray(today.reasons) ? today.reasons.filter((r) => r && present(r.n)).slice(0, 3) : [];
    const why = reasons.length ? `Why it left people alone: ${reasons.map((r) => `${r.words || r.code}: ${int(r.n)}`).join("; ")}.` : null;
    out.push(
      el(
        "p",
        {},
        `On ${when(today.date, { time: false })} it would have contacted ${plural(today.contacted, "customer", "customers")} and `,
        alone,
        ". Nothing was sent.",
        why ? tipFor(alone, why) : null,
      ),
    );
  }
  if (expected && expected.expected_net) {
    const verdict = rangeVerdict(expected.expected_net);
    const figure = el("strong", {}, range(expected.expected_net, expected.currency || goal.currency));
    const covers = `Its own forecast for the ${plural(expected.with_forecast, "contact", "contacts")} it would have made.`;
    out.push(
      el(
        "p",
        {},
        "Its forecast, not proof: ",
        figure,
        " extra profit.",
        verdict === "unsure" ? " Not sure yet." : "",
        tipFor(figure, verdict === "unsure" ? `${covers} That range includes zero, so it's not sure yet.` : covers),
      ),
    );
  } else if (expected && expected.words && !(today && Number(today.customers_considered) === 0)) {
    out.push(el("p", { class: "ob-muted" }, expected.words));
  }
  return out;
}

function shadowStep(goal, day, today, expected) {
  if (!goal) return step(3, "later", el("p", {}, "It decides every day, but sends nothing."));
  const plan = link("Open the plan", `#/plan/${encodeURIComponent(goal.goal_id)}`);
  if (goal.status === "draft") {
    return step(3, "next", el("p", {}, "Check its plan, then approve it in Shadow mode, the recommended start."), [plan]);
  }
  const words = trialWords(day);
  if (goal.autonomy === "shadow" && goal.status === "paused") {
    // Only running goals are run: a paused one neither decides nor sends, so the trial waits.
    return step(
      3,
      "now",
      [
        el("p", {}, withInfo(el("strong", {}, words ? `Shadow trial: ${words}, paused.` : "Shadow trial paused."), "While it is paused it neither decides nor sends. Resume it on the Live page to carry on.")),
        shadowFacts(goal, today, expected),
      ],
      [link("Resume it, or see what it decided so far", `#/live/${encodeURIComponent(goal.goal_id)}`)],
    );
  }
  if (goal.autonomy === "shadow") {
    return step(
      3,
      day && day > TRIAL_DAYS ? "done" : "now",
      [
        el("p", {}, withInfo(el("strong", {}, words ? `Shadow trial: ${words}.` : "Shadow trial running."), "Autonomy is deciding, but nothing is being sent.")),
        shadowFacts(goal, today, expected),
      ],
      [link("See what it would do, and why", `#/live/${encodeURIComponent(goal.goal_id)}`)],
    );
  }
  // "Past", not "Done": the level today says nothing about whether a Shadow trial ever ran.
  return step(3, "past", null, [], `This goal is past Shadow: it is at ${goal.autonomy === "approve" ? "Approve each batch" : "Autopilot"}.`);
}

// ---- step 4: Approve with a small budget -----------------------------------------------------------------------------
function budgetForm(ctx, goal, rerender) {
  const id = `ob-budget-${goal.goal_id}`;
  const input = el("input", { id, class: "tabular", type: "number", min: "0", step: "any", inputmode: "decimal", value: present(goal.budget) ? String(goal.budget) : "", "aria-describedby": `${id}-help` });
  const msg = el("p", { class: "ob-form-msg", role: "status" });
  const save = button("Save", null, { kind: "secondary", type: "submit", ariaLabel: "Save the budget" });
  const form = el(
    "form",
    {
      class: "ob-budget",
      onsubmit: async (e) => {
        e.preventDefault();
        const value = Number(input.value);
        if (input.value === "" || !Number.isFinite(value) || value < 0) {
          msg.textContent = "Type a budget of zero or more.";
          return;
        }
        save.disabled = true;
        msg.textContent = "Saving…";
        try {
          const out = await ctx.api.updateGoalBudget(goal.goal_id, value);
          const next = (out && out.goal) || { ...goal, budget: value };
          ctx.toast(`The budget for this goal is now ${money(next.budget, next.currency)}.`, "good");
          if (typeof ctx.refreshGoals === "function") await ctx.refreshGoals();
          await rerender(next);
        } catch (err) {
          msg.textContent = `Couldn't change the budget. ${err.message || ""}`.trim();
          save.disabled = false;
        }
      },
    },
    el("label", { for: id }, `Budget (${goal.currency || "USD"})`),
    el("div", { class: "ob-budget-row" }, input, save),
    el(
      "p",
      { id: `${id}-help`, class: "field-hint" },
      withInfo(el("span", {}, "You can always lower it."), `Now ${money(goal.budget, goal.currency)}. Lowering it is always allowed; raising it above an approved plan needs a new plan.`),
    ),
    msg,
  );
  return form;
}

function approveStep(ctx, goal, day, rerender) {
  if (!goal || goal.status === "draft") return step(4, "later", el("p", {}, "Then it sends only the batches you approve."));
  if (goal.autonomy === "shadow") {
    const early = !day || day <= TRIAL_DAYS;
    const turnOn = button(early ? "Turn on Approve mode now" : "Turn on Approve mode", async () => {
      turnOn.disabled = true;
      try {
        const out = await ctx.api.approveGoal(goal.goal_id, "approve");
        ctx.toast("Approve mode is on. Nothing goes out until you approve a batch on the Live page.", "good");
        if (typeof ctx.refreshGoals === "function") await ctx.refreshGoals();
        await rerender((out && out.goal) || { ...goal, autonomy: "approve" });
      } catch (err) {
        ctx.toast(`Couldn't change the autonomy level. ${err.message || ""}`.trim(), "critical");
        turnOn.disabled = false;
      }
    }, { kind: early ? "secondary" : "primary" });
    const left = day ? (day >= TRIAL_DAYS ? " (today is its last day)" : ` (${TRIAL_DAYS - day} more ${TRIAL_DAYS - day === 1 ? "day" : "days"})`) : "";
    return step(
      4,
      early ? "later" : "next",
      [
        el("p", {}, early ? `We suggest finishing the two-week trial first${left}.` : "The two weeks are up: next, let it send the batches you approve."),
        budgetForm(ctx, goal, rerender),
      ],
      [turnOn],
    );
  }
  if (goal.autonomy === "approve") {
    return step(4, "now", budgetForm(ctx, goal, rerender), [link("Batches waiting for you", `#/live/${encodeURIComponent(goal.goal_id)}`)]);
  }
  return step(4, "past", null, [], "This goal is past Approve mode: it is on Autopilot.");
}

// ---- step 5: Autopilot when Proof shows real extra profit -------------------------------------------------------------
/** General guidance about the field, labelled as such: the 3-10% is never a number about this shop. */
function guidance() {
  return el(
    "p",
    { class: "notice notice-neutral ob-guidance" },
    el("strong", {}, "In general: "),
    "well-run AI marketing adds ",
    withInfo(el("span", {}, "3–10%"), "That is general guidance about the field, not a number about your shop: what is typical when results are measured honestly. Your Proof page shows your real number, with its range."),
    " in measured extra profit.",
  );
}

function proofStep(goal, proof, proofErr) {
  if (!goal || goal.status === "draft") return step(5, "later", guidance());
  const proofLink = link("See the proof", `#/proof/${encodeURIComponent(goal.goal_id)}`);
  if (goal.autonomy === "autopilot") {
    return step(5, "done", [el("p", {}, "It acts on its own inside the limits you set."), guidance()], [proofLink]);
  }
  const est = proof && proof.incremental_profit;
  const verdict = rangeVerdict(est);
  const figure = () => el("strong", {}, range(est, goal.currency));
  let body;
  let state = "later";
  if (proofErr) {
    const words = el("span", {}, "We couldn't load the proof.");
    body = el("p", {}, present(proofErr.message) ? withInfo(words, proofErr.message) : words);
  } else if (!verdict && goal.autonomy === "shadow") {
    body = el("p", {}, "Nothing to prove yet: in Shadow mode nothing is sent.");
  } else if (!verdict) {
    body = el("p", {}, withInfo(el("span", {}, "No proof yet."), "It needs time: it compares the customers it looked after with people we never contact."));
  } else if (verdict === "sure") {
    state = goal.autonomy === "approve" ? "next" : "later";
    body = el("p", {}, "Proof shows real extra profit compared with people we never contact: ", figure(), ".");
  } else if (verdict === "unsure") {
    body = el("p", {}, withInfo(el("span", {}, "Not sure yet."), "That range includes zero. Stay where you are for now."), " Extra profit so far: ", figure(), ".");
  } else {
    body = el("p", {}, withInfo(el("span", {}, "So far it has made less than doing nothing:"), "Stay in Approve mode, or pause it."), " ", figure(), ".");
  }
  const actions = [proofLink];
  if (verdict === "sure" && goal.autonomy === "approve") actions.push(link("Switch to Autopilot on the Plan page", `#/plan/${encodeURIComponent(goal.goal_id)}`));
  return step(5, state, [body, guidance()], actions);
}

// ---- the page --------------------------------------------------------------------------------------------------------
// One question: where am I on the safe path, and what is next? The path is the biggest block (8 of 12 columns); the
// ask (first run only), the honest status and the control promise sit in the rail beside it (under it on a phone).

const TITLE = "Getting started";
const SUB = "Getting started is safe: it watches before it sends.";
const SUB_MORE = "You let it do more one step at a time, and only when the numbers say so.";

function honestNote() {
  return el(
    "section",
    { class: "card ob-honest", "aria-labelledby": "ob-honest-title" },
    el(
      "div",
      { class: "ob-card-title" },
      el("h2", { id: "ob-honest-title", class: "card-title" }, "Honest status"),
      info(
        "We test it in the Autonomy Gym, a simulated shop with customers who buy, ignore messages and unsubscribe, and replay it on past orders from real shops. Those results describe the product, not your customers; your own Proof page will.",
        { label: "How we tested it" },
      ),
    ),
    el("p", { class: "notice ob-honest-notice" }, "It has not yet run with a real business, so it starts in Shadow mode, risk-free."),
  );
}

function theAsk() {
  return el(
    "section",
    { class: "card ob-ask", "aria-labelledby": "ob-ask-title" },
    el("h2", { id: "ob-ask-title", class: "card-title" }, "What we ask: your customer data and one goal"),
    el("p", {}, "In two weeks of Shadow mode you'll see what it would do, why, and what it expects to earn, before a single message goes out."),
  );
}

function controlNote() {
  return el(
    "section",
    { class: "card ob-control", "aria-labelledby": "ob-control-title" },
    el(
      "div",
      { class: "ob-card-title" },
      el("h2", { id: "ob-control-title", class: "card-title" }, "You stay in control"),
      info("At every step. Autonomy also pauses itself if complaints or unsubscribes spike.", { label: "When it pauses itself" }),
    ),
    el("p", { class: "ob-footnote" }, "Pause, at the top, stops all sending."),
  );
}

// The ask is the first run's whole message; once a goal is set it has been answered, so the rail drops it.
const rail = (hasGoals) => el("div", { class: "span-4 stack ob-rail" }, hasGoals ? null : theAsk(), honestNote(), controlNote());

/** The path's frame: the card with its title, an optional grey meta on the right, and its rows. */
function pathCard(title, meta, children) {
  return el(
    "section",
    { class: "card card-flush span-8 ob-path", "aria-labelledby": "ob-path-title" },
    el(
      "div",
      { class: "card-head ob-path-head" },
      el("h2", { id: "ob-path-title", class: "card-title" }, title),
      present(meta) ? el("span", { class: "card-meta" }, meta) : null,
    ),
    children,
  );
}

/** While the goals load: the path's rows at their real size, numbers and all, so nothing jumps when they land. */
function pathSkeleton() {
  const row = (n) =>
    el(
      "li",
      { class: "ob-step ob-step-later" },
      el("span", { class: "ob-num", "aria-hidden": "true" }, String(n)),
      el("div", { class: "ob-step-body" }, skeleton({ width: "55%", height: "20px" }), skeleton({ width: "70%", height: "16px", className: "ob-skel-line" })),
    );
  return el("ol", { class: "ob-steps", "aria-busy": "true" }, [1, 2, 3, 4, 5].map(row), el("li", { class: "visually-hidden", role: "status" }, "Loading your path…"));
}

/** Choose the goal the path follows: the one in the switcher, else the first running one, else the first. */
function focusGoal(goals, ctx) {
  const byId = (id) => goals.find((g) => g.goal_id === id);
  return (ctx.goalId && byId(ctx.goalId)) || goals.find((g) => g.status === "active") || goals[0] || null;
}

const frame = (path, hasGoals) => [pageHead({ title: TITLE, sub: SUB, info: SUB_MORE }), el("div", { class: "grid ob-grid" }, path, rail(hasGoals))];

export async function render(root, ctx) {
  clear(root, ...frame(pathCard("Your path", null, pathSkeleton()), true));
  const api = ctx.api;
  const [goalsR, dataR] = await Promise.allSettled([api.listGoals(), typeof api.getDataReport === "function" ? api.getDataReport() : Promise.resolve(null)]);
  if (goalsR.status === "rejected") {
    const failed = pathCard("Your path", null, el("div", { class: "ob-error" }, errorBox("We couldn't load your goals.", goalsR.reason, { retry: () => render(root, ctx) })));
    clear(root, ...frame(failed, true));
    return;
  }
  const goals = Array.isArray(goalsR.value) ? goalsR.value : [];
  let goal = focusGoal(goals, ctx);
  if (goal && ctx.goal && ctx.goal.goal_id === goal.goal_id && ctx.goal !== goal) goal = { ...goal, ...ctx.goal };

  // The goal's own facts, each optional: a missing route or a goal with nothing yet shows its step without them.
  let today = null;
  let expected = null;
  let proof = null;
  let proofErr = null;
  let since = null;
  if (goal && goal.status !== "draft") {
    const shadow = goal.autonomy === "shadow";
    const [todayR, expectedR, proofR, detailR] = await Promise.allSettled([
      shadow && typeof api.getToday === "function" ? api.getToday(goal.goal_id) : Promise.resolve(null),
      shadow && typeof api.getExpected === "function" ? api.getExpected(goal.goal_id) : Promise.resolve(null),
      api.getProof(goal.goal_id),
      shadow ? api.getGoal(goal.goal_id) : Promise.resolve(null),
    ]);
    today = valueOf(todayR);
    expected = valueOf(expectedR);
    proof = valueOf(proofR);
    proofErr = errorOf(proofR);
    const detail = valueOf(detailR);
    since = (today && today.autonomy_since) || (detail && detail.brief && detail.brief.approved_at) || null;
  }
  const day = goal && goal.autonomy === "shadow" ? trialDay(since) : null;
  const rerender = async (nextGoal) => render(root, { ...ctx, goalId: nextGoal ? nextGoal.goal_id : ctx.goalId, goal: nextGoal || null });

  const report = valueOf(dataR);
  const dataMissing = Boolean(report) && !Number(report.customers) && !(Array.isArray(report.sources) && report.sources.length);
  const steps = [dataStep(report, errorOf(dataR)), goalStep(goals, goal, dataMissing)];
  if (goals.length) steps.push(shadowStep(goal, day, today, expected), approveStep(ctx, goal, day, rerender), proofStep(goal, proof, proofErr));
  // One step is "the current step" for a screen reader: the one being done now, else the next one.
  const isState = (s, state) => String(s.getAttribute("class") || "").split(/\s+/).includes(`ob-step-${state}`);
  const current = steps.find((s) => isState(s, "now")) || steps.find((s) => isState(s, "next"));
  if (current) current.setAttribute("aria-current", "step");

  // The current step's own pill ("Doing now", "Next") says where you are, so the card's head carries no "Step n of 5".
  const path = pathCard(
    goals.length ? "Your path" : "Two steps to start",
    null,
    [
      el("ol", { class: "ob-steps" }, steps),
      goals.length
        ? null
        : el(
            "p",
            { class: "ob-muted ob-then" },
            withInfo(
              el("span", {}, "Then: Shadow for two weeks, Approve with a small budget, Autopilot when proven."),
              "Two weeks in Shadow mode, where it decides but sends nothing; then Approve mode for one goal, with a small budget; then Autopilot, only when the Proof page shows real extra profit.",
            ),
          ),
    ],
  );
  clear(root, ...frame(path, goals.length > 0));
}
