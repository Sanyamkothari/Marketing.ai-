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

import { button, clear, el, errorBox, int, loading, money, present, range, when } from "../components.js";

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

const STATE_WORDS = {
  done: "Done",
  now: "Doing now",
  next: "Next",
  later: "Later",
  past: "Past this step",
  unknown: "Can't tell yet",
};

function stateTag(state) {
  return el("span", { class: ["ob-state", `ob-state-${state}`] }, STATE_WORDS[state] || state);
}

/** One step of the path: number, title, state (in words, not only colour), what the data says, and actions. */
function step(n, title, state, body, actions = []) {
  return el(
    "li",
    { class: ["ob-step", `ob-step-${state}`] },
    el("span", { class: "ob-num", "aria-hidden": "true" }, String(n)),
    el(
      "div",
      { class: "ob-step-body" },
      el("div", { class: "ob-step-head" }, el("h3", { class: "ob-step-title" }, el("span", { class: "visually-hidden" }, `Step ${n}: `), title), stateTag(state)),
      body,
      actions.length ? el("div", { class: "ob-actions" }, actions) : null,
    ),
  );
}

const link = (text, href) => el("a", { class: "ob-link", href }, text);

// ---- step 1: data --------------------------------------------------------------------------------------------
function dataStep(report, err) {
  const go = link("Go to Connect", "#/connect");
  if (err || !report) {
    return step(
      1,
      "Connect your customer and order data",
      "unknown",
      el("p", {}, err ? `We couldn't check your data from here. ${err.message || ""}`.trim() : "This server can't report on your data yet, so we can't tell whether it's connected."),
      [go],
    );
  }
  const sources = Array.isArray(report.sources) ? report.sources : [];
  const customers = Number(report.customers) || 0;
  if (!customers && !sources.length) {
    return step(
      1,
      "Connect your customer and order data",
      "now",
      el("p", {}, "Upload your customer and order files, or connect your database. The Data Doctor cleans up messy data and asks you before it changes anything."),
      [go],
    );
  }
  const questions = Array.isArray(report.questions) ? report.questions : [];
  return step(
    1,
    "Connect your customer and order data",
    "done",
    [
      el(
        "p",
        {},
        `Autonomy has ${plural(customers, "customer", "customers")}`,
        present(report.orders) ? ` and ${plural(report.orders, "order", "orders")}` : "",
        sources.length ? `, from ${plural(sources.length, "source", "sources")}.` : ".",
      ),
      questions.length
        ? el("p", { class: "ob-attention" }, `The Data Doctor has ${plural(questions.length, "question", "questions")} for you. It won't change anything until you answer.`)
        : null,
    ],
    [link(questions.length ? "Answer on Connect" : "See your data", "#/connect")],
  );
}

// ---- step 2: one goal ------------------------------------------------------------------------------------------
function goalStep(goals, goal, dataMissing) {
  if (!goals.length) {
    return step(
      2,
      "Set one goal, in plain words",
      dataMissing ? "next" : "now",
      el("p", {}, "Type what you want, for example: ", el("q", {}, "Win back customers who stopped buying, $5,000 this month."), " Or pick one of the opportunities it found in your data."),
      [link("Set a goal", "#/goal")],
    );
  }
  return step(
    2,
    "Set one goal, in plain words",
    "done",
    [
      el("p", {}, "Following: ", el("strong", {}, goal.text || goal.goal_id)),
      goals.length > 1 ? el("p", { class: "ob-muted" }, `You have ${plural(goals.length, "goal", "goals")}. Switch between them at the top; we suggest starting with one.`) : null,
    ],
    [link("See your goals", "#/goal")],
  );
}

// ---- step 3: Shadow ----------------------------------------------------------------------------------------------
function shadowFacts(goal, today, expected) {
  const out = [];
  if (today && Number(today.customers_considered) === 0 && goal.status === "paused") {
    out.push(el("p", {}, "It made no decisions before it was paused."));
  } else if (today && Number(today.customers_considered) === 0) {
    out.push(el("p", {}, `It hasn't made its first decisions yet${present(today.next_run_at) ? `; the next daily run is ${when(today.next_run_at)}` : ""}. Come back after it runs to see what it would do.`));
  } else if (today && present(today.contacted)) {
    out.push(
      el(
        "p",
        {},
        `On ${when(today.date, { time: false })} it would have contacted ${plural(today.contacted, "customer", "customers")} and left ${int(today.left_alone)} alone. Nothing was sent.`,
      ),
    );
    const reasons = Array.isArray(today.reasons) ? today.reasons.filter((r) => r && present(r.n)).slice(0, 3) : [];
    if (reasons.length) {
      out.push(
        el("p", { class: "ob-sub" }, "Why it left people alone:"),
        el("ul", { class: "ob-reasons" }, reasons.map((r) => el("li", {}, `${r.words || r.code}: ${int(r.n)}`))),
      );
    }
  }
  if (expected && expected.expected_net) {
    const verdict = rangeVerdict(expected.expected_net);
    out.push(
      el(
        "p",
        {},
        `Its own forecast for the ${plural(expected.with_forecast, "contact", "contacts")} it would have made: `,
        el("strong", {}, range(expected.expected_net, expected.currency || goal.currency)),
        " extra profit",
        verdict === "unsure" ? ", so it's not sure yet: that range includes zero." : ".",
        " This is a forecast, not proof.",
      ),
    );
  } else if (expected && expected.words && !(today && Number(today.customers_considered) === 0)) {
    out.push(el("p", { class: "ob-muted" }, expected.words));
  }
  return out;
}

function shadowStep(goal, day, today, expected) {
  if (!goal) {
    return step(3, "Run in Shadow mode for two weeks", "later", el("p", {}, "It decides every day what it would do for each customer, and why, but sends nothing."));
  }
  const plan = link("Open the plan", `#/plan/${encodeURIComponent(goal.goal_id)}`);
  if (goal.status === "draft") {
    return step(
      3,
      "Run in Shadow mode for two weeks",
      "next",
      el("p", {}, "Check its plan and the messages it wrote, then approve it in Shadow mode, the recommended start. It decides every day but sends nothing."),
      [plan],
    );
  }
  if (goal.autonomy === "shadow" && goal.status === "paused") {
    // Only running goals are run: a paused one neither decides nor sends, so the trial waits.
    const words = trialWords(day);
    return step(
      3,
      "Run in Shadow mode for two weeks",
      "now",
      [
        el("p", {}, el("strong", {}, words ? `Shadow trial: ${words}, paused.` : "Shadow trial paused."), " While it is paused it neither decides nor sends. Resume it on the Live page to carry on."),
        shadowFacts(goal, today, expected),
      ],
      [link("Resume it, or see what it decided so far", `#/live/${encodeURIComponent(goal.goal_id)}`)],
    );
  }
  if (goal.autonomy === "shadow") {
    const words = trialWords(day);
    return step(
      3,
      "Run in Shadow mode for two weeks",
      day && day > TRIAL_DAYS ? "done" : "now",
      [
        el("p", {}, el("strong", {}, words ? `Shadow trial: ${words}.` : "Shadow trial running."), " Autonomy is deciding, but nothing is being sent."),
        shadowFacts(goal, today, expected),
      ],
      [link("See what it would do, and why", `#/live/${encodeURIComponent(goal.goal_id)}`)],
    );
  }
  return step(
    3,
    "Run in Shadow mode for two weeks",
    // "Past", not "Done": the level today says nothing about whether a Shadow trial ever ran.
    "past",
    el("p", {}, `This goal is past Shadow: it is at ${goal.autonomy === "approve" ? "Approve each batch" : "Autopilot"}.`),
  );
}

// ---- step 4: Approve with a small budget -----------------------------------------------------------------------------
function budgetForm(ctx, goal, rerender) {
  const id = `ob-budget-${goal.goal_id}`;
  const input = el("input", { id, type: "number", min: "0", step: "any", inputmode: "decimal", value: present(goal.budget) ? String(goal.budget) : "", "aria-describedby": `${id}-help` });
  const msg = el("p", { class: "ob-form-msg", role: "status" });
  const save = button("Save budget", null, { kind: "secondary", type: "submit" });
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
    el("label", { for: id }, `Budget for this goal (${goal.currency || "USD"})`),
    el("div", { class: "ob-budget-row" }, input, save),
    el("p", { id: `${id}-help`, class: "ob-muted" }, `Now ${money(goal.budget, goal.currency)}. Lowering it is always allowed. Raising it above an approved plan needs a new plan.`),
    msg,
  );
  return form;
}

function approveStep(ctx, goal, day, rerender) {
  const title = "Turn on Approve mode, with a small budget";
  if (!goal || goal.status === "draft") {
    return step(4, title, "later", el("p", {}, "After the trial it sends, but only the batches you approve with one click. Start with a small budget."));
  }
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
    return step(
      4,
      title,
      early ? "later" : "next",
      [
        el(
          "p",
          {},
          early
            ? `We suggest finishing the two-week trial first${day ? (day >= TRIAL_DAYS ? " (today is its last day)" : ` (${TRIAL_DAYS - day} more ${TRIAL_DAYS - day === 1 ? "day" : "days"})`) : ""}. Then it sends, but only the batches you approve. You can lower the budget now.`
            : "The two weeks are up. Next: let it send, but only the batches you approve with one click, inside a small budget you choose.",
        ),
        budgetForm(ctx, goal, rerender),
      ],
      [turnOn],
    );
  }
  if (goal.autonomy === "approve") {
    return step(
      4,
      title,
      "now",
      [el("p", {}, "It sends only the batches you approve, with one click on the Live page."), budgetForm(ctx, goal, rerender)],
      [link("Batches waiting for you", `#/live/${encodeURIComponent(goal.goal_id)}`)],
    );
  }
  return step(4, title, "past", el("p", {}, "This goal is past Approve mode: it is on Autopilot."), []);
}

// ---- step 5: Autopilot when Proof shows real extra profit -------------------------------------------------------------
function proofStep(goal, proof, proofErr) {
  const title = "Switch to Autopilot when Proof shows real extra profit";
  const guidance = el(
    "p",
    { class: "ob-guidance" },
    el("strong", {}, "What to expect, in general: "),
    "well-run AI marketing typically adds 3–10% in real, measured extra profit. That is general guidance about the field, not a number about your shop; the Proof page shows your real number, with its range.",
  );
  if (!goal || goal.status === "draft") {
    return step(5, title, "later", [el("p", {}, "Proof compares the customers it looks after with a small random group it never contacts. Autopilot only makes sense once that difference is clearly above zero."), guidance]);
  }
  const proofLink = link("See the proof", `#/proof/${encodeURIComponent(goal.goal_id)}`);
  const planLink = link("Change the level on the Plan page", `#/plan/${encodeURIComponent(goal.goal_id)}`);
  if (goal.autonomy === "autopilot") {
    return step(5, title, "done", [el("p", {}, "This goal is on Autopilot: it acts on its own inside the limits you set. The big Pause button at the top stops it at once."), guidance], [proofLink]);
  }
  const est = proof && proof.incremental_profit;
  const verdict = rangeVerdict(est);
  let body;
  let state = "later";
  if (proofErr) body = el("p", {}, `We couldn't load the proof. ${proofErr.message || ""}`.trim());
  else if (!verdict) body = el("p", {}, "No proof yet. It needs time: it compares the customers it looked after with people we never contact.");
  else if (verdict === "sure") {
    state = goal.autonomy === "approve" ? "next" : "later";
    body = el("p", {}, "Proof shows real extra profit compared with people we never contact: ", el("strong", {}, range(est, goal.currency)), ". If you're happy with how it works, you can switch to Autopilot.");
  } else if (verdict === "unsure") {
    body = el("p", {}, "Not sure yet. Extra profit so far is somewhere between ", el("strong", {}, range(est, goal.currency)), ", and that range includes zero. Stay where you are for now.");
  } else {
    body = el("p", {}, "So far it has made less than doing nothing: ", el("strong", {}, range(est, goal.currency)), ". Stay in Approve mode, or pause it.");
  }
  const actions = [proofLink];
  if (verdict === "sure" && goal.autonomy === "approve") actions.push(planLink);
  return step(5, title, state, [body, goal.autonomy === "shadow" ? el("p", { class: "ob-muted" }, "In Shadow mode nothing is sent, so there is nothing to prove yet.") : null, guidance], actions);
}

// ---- the page --------------------------------------------------------------------------------------------------------
function honestNote() {
  return el(
    "aside",
    { class: "ob-honest", "aria-labelledby": "ob-honest-title" },
    el("h2", { id: "ob-honest-title", class: "ob-honest-title" }, "Honest status"),
    el(
      "p",
      {},
      "Autonomy has been tested on simulated shops and on past shop data. It has not yet run with a real business, so it is not proven on yours. That is why it starts in Shadow mode, where it decides but sends nothing: risk-free.",
    ),
    el(
      "details",
      { class: "ob-details" },
      el("summary", {}, "How we tested it"),
      el(
        "p",
        {},
        "We run it in the Autonomy Gym, a simulated shop with customers who buy, ignore messages and unsubscribe, and we replay it on historical orders from real shops. Those results describe the product in general. They say nothing yet about your customers; your own Proof page will.",
      ),
    ),
  );
}

function theAsk(hasGoals) {
  return el(
    "section",
    { class: "ob-ask", "aria-labelledby": "ob-ask-title" },
    el("h2", { id: "ob-ask-title" }, "What we ask: your customer data and one goal"),
    el(
      "p",
      {},
      "In two weeks of Shadow mode you'll see what it would do, why, and what it expects to earn, before a single message goes out.",
    ),
    hasGoals ? null : el("p", { class: "ob-muted" }, "That's all for today. The next steps appear here once your goal is set."),
  );
}

/** Choose the goal the path follows: the one in the switcher, else the first running one, else the first. */
function focusGoal(goals, ctx) {
  const byId = (id) => goals.find((g) => g.goal_id === id);
  return (ctx.goalId && byId(ctx.goalId)) || goals.find((g) => g.status === "active") || goals[0] || null;
}

export async function render(root, ctx) {
  clear(root, el("h1", {}, "Getting started is safe"), loading());
  const api = ctx.api;
  const [goalsR, dataR] = await Promise.allSettled([api.listGoals(), typeof api.getDataReport === "function" ? api.getDataReport() : Promise.resolve(null)]);
  if (goalsR.status === "rejected") {
    clear(root, el("h1", {}, "Getting started is safe"), errorBox("We couldn't load your goals.", goalsR.reason));
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

  clear(
    root,
    el("h1", {}, "Getting started is safe"),
    el(
      "p",
      { class: "lede" },
      "Autonomy starts by watching, not sending. You let it do more one step at a time, and only when the numbers say so.",
    ),
    el(
      "div",
      { class: "ob-top" },
      theAsk(goals.length > 0),
      honestNote(),
    ),
    el(
      "section",
      { class: "ob-path", "aria-labelledby": "ob-path-title" },
      el("h2", { id: "ob-path-title" }, goals.length ? "Your path" : "Two steps to start"),
      el("ol", { class: "ob-steps" }, steps),
      goals.length
        ? null
        : el(
            "p",
            { class: "ob-muted ob-then" },
            "Then: two weeks in Shadow mode, where it decides but sends nothing; then Approve mode for one goal, with a small budget; then Autopilot, only when the Proof page shows real extra profit.",
          ),
    ),
    el(
      "p",
      { class: "ob-muted ob-footnote" },
      "You stay in control at every step: the big Pause button at the top stops all sending at once, and Autonomy also pauses itself if complaints or unsubscribes spike.",
    ),
  );
}
