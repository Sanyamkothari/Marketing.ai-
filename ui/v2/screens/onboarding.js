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

import { EM_DASH, button, clear, describe, el, errorBox, info, int, money, pageHead, pct, present, range, skeleton, when, withInfo } from "../components.js";

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

// ---- zero-touch setup: five clicks from Connect to a running Shadow plan (docs/v2/ZERO_TOUCH.md) -----------------
// While no goal runs yet (or the goal set up here still waits for its voice), Getting started is the setup itself:
//
//   1 Connect Shopify        one click (the shop address is typed in the row; OAuth, or a pasted key for pilots)
//   2 Connect Klaviyo        one click; Autonomy sets up sending in their Klaviyo by itself ("Sending is set up in
//                            your Klaviyo"), and a calm line says what it is reading while the history loads
//   3 Pick a goal            GET /v2/onboarding/proposals: each proposal sized in money with its range, from the
//                            shop's own history; the best one is preselected, so "Use this goal" is the one click
//   4 Confirm limits         GET /v2/onboarding/limits: what we found -> what we will use; one button signs them as
//                            the mandate and starts the goal in Shadow (POST /v2/onboarding/accept)
//   5 Approve the voice      GET /v2/onboarding/brand: three sample messages in the shop's own voice (read
//                            automatically once a tool is connected); one button approves it
//
// Then "Shadow is running", with a link to Live. Every number on it comes from those answers; a step the server
// cannot serve says so in words, and nothing is made up to fill it.

/** The setup flag a platform's sign-in round trip carries: Connect sends the browser back here while it is set. */
export const SETUP_KEY = "autonomy.v2.setup";

/** The clicks the setup takes when every default is kept (the measure in ZERO_TOUCH.md: 5 or fewer). */
export const SETUP_CLICKS = Object.freeze(["Continue to Shopify", "Connect Klaviyo", "Use this goal", "Sign and start Shadow", "Approve this voice"]);

const SETUP_STEPS = {
  1: ["Connect Shopify", "Your orders, customers and products. Autonomy reads them, and only writes single-use discount codes and customer tags."],
  2: ["Connect Klaviyo", "Your marketing consent and how people answer your messages. Autonomy sends through your own Klaviyo, so your unsubscribes, sender and footer stay yours."],
  3: ["Pick a goal", "Sized in money from your own order history and published test results, never made up. The range is honest about what is not known yet."],
  4: ["Confirm your limits", "Learned from your own history. Signed once, they become the mandate: inside it Autonomy never asks you again."],
  5: ["Approve the voice", "Read from your own Klaviyo messages and Shopify shop. Every message is checked against it before anything is sent."],
};

const remember = (on) => {
  try {
    if (typeof sessionStorage === "undefined") return;
    if (on) sessionStorage.setItem(SETUP_KEY, "1");
    else sessionStorage.removeItem(SETUP_KEY);
  } catch {
    /* private mode: the round trip lands on Connect, one click from here */
  }
};

const livePlatform = (overview, kind) => {
  const p = overview && Array.isArray(overview.platforms) ? overview.platforms.find((x) => x.kind === kind) : null;
  return p || null;
};
const liveConnections = (p) => (p && Array.isArray(p.connections) ? p.connections.filter((c) => c.phase !== "disconnected") : []);
const LOADING_PHASES = new Set(["syncing", "backfilling"]);
const running = (goals) => goals.filter((g) => g.status === "active" || g.status === "paused");

/** Only a web address or a path can be an image; a host under ".invalid" can never load, so it is not requested. */
const imageSrc = (url) => {
  const u = present(url) ? String(url).trim() : "";
  if (!/^(https?:\/\/|\/|\.\/)/i.test(u)) return null;
  return /^https?:\/\/[^/]*\.invalid(?::\d+)?(?:\/|$)/i.test(u) ? null : u;
};

/**
 * Everything the setup reads: the platforms and the mandates signed here first (both quick), then, only when the
 * setup is wanted (nothing runs yet, or the goal set up here still runs), the proposals, which read the shop's whole
 * history. Null when this server has none of the setup routes, or the setup is not wanted.
 */
async function loadSetup(api, goals) {
  if (typeof api.getProposals !== "function" && typeof api.listConnectors !== "function") return null;
  const call = (name, ...args) => (typeof api[name] === "function" ? Promise.resolve(api[name](...args)) : Promise.resolve(null));
  const [connR, mandR] = await Promise.allSettled([call("listConnectors"), call("listMandates")]);
  const setup = { connectors: valueOf(connR), scan: null, scanError: null, mandates: Array.isArray(valueOf(mandR)) ? valueOf(mandR) : [] };
  if (running(goals).length && !setupGoal(setup, goals)) return setup; // a shop already running: no scan needed
  const [scanR] = await Promise.allSettled([call("getProposals")]);
  setup.scan = valueOf(scanR);
  setup.scanError = errorOf(scanR);
  if (!setup.connectors && !setup.scan && !setup.scanError) return null;
  return setup;
}

/** The goal set up here, when it still runs: the newest mandate's goal. */
function setupGoal(setup, goals) {
  for (const m of setup.mandates) {
    const g = goals.find((x) => x.goal_id === m.goal_id && (x.status === "active" || x.status === "paused"));
    if (g) return { goal: g, record: m };
  }
  return null;
}

const anyConnection = (overview) => ["shopify", "klaviyo"].some((k) => liveConnections(livePlatform(overview, k)).length > 0);

/**
 * Whether Getting started is the setup: nothing runs yet, or the goal set up here still waits for its voice and the
 * voice can be read (a tool is connected, or it was read already).
 */
export function wantsSetup(setup, goals, brand) {
  if (!setup) return false;
  if (!running(goals).length) return true;
  if (!setupGoal(setup, goals) || !brand) return false;
  return brand.status === "waiting" || (brand.status === "not_read" && anyConnection(setup.connectors));
}

const STEP_PILL = { done: "Done", now: "Doing now", next: "Next", later: "Later", optional: "Optional", unknown: "Can't tell yet" };
const STEP_TONE = { done: "positive", now: "accent", next: "neutral", later: "neutral", optional: "neutral", unknown: "warning" };

/** One setup step: the number, the title (its meaning on hover), the state in a word, then the body and one button. */
function setupStep(n, state, body = [], actions = [], about = null) {
  const [title, teach] = SETUP_STEPS[n];
  const tag = el("span", { class: ["pill", `pill-${STEP_TONE[state] || "neutral"}`, "ob-state", `ob-state-${state}`] }, STEP_PILL[state] || state);
  const kids = [body].flat().filter(Boolean);
  return el(
    "li",
    { class: ["ob-step", `ob-step-${state === "optional" ? "past" : state}`, "zt-step"], dataset: { step: String(n) } },
    el("span", { class: "ob-num", "aria-hidden": "true" }, String(n)),
    el(
      "div",
      { class: "ob-step-body" },
      el(
        "div",
        { class: "ob-step-head" },
        el("h3", { class: "ob-step-title" }, el("span", { class: "visually-hidden" }, `Step ${n}: `), withInfo(el("span", {}, title), teach)),
        present(about) ? withInfo(tag, about) : tag,
      ),
      kids.length ? el("div", { class: "ob-step-text" }, kids) : null,
      actions.length ? el("div", { class: "ob-actions" }, actions) : null,
    ),
  );
}

/** A labelled field for the connect rows; returns [field, input]. */
let ztField = 0;
function setupField(name, label, help, { secret = false, placeholder = null } = {}) {
  ztField += 1;
  const id = `zt-${name}-${ztField}`;
  const input = el("input", { id, name, type: secret ? "password" : "text", autocomplete: "off", spellcheck: "false", placeholder });
  const lab = el("label", { for: id }, label);
  return [el("div", { class: "field zt-field" }, present(help) ? withInfo(lab, help) : lab, input), input];
}

/**
 * Connect a platform from its step: OAuth goes straight to the platform's consent (Shopify needs the shop address,
 * typed in the row, so it is still one click); a server without our app's credentials takes a pasted key (pilots).
 */
function connectBlock(platform, env, { primary }) {
  const p = platform;
  const oauth = Boolean(p.oauth_ready);
  const fields = (p.config_fields || []).map((f) => setupField(f.name, f.label, f.help, { placeholder: f.name === "shop" ? "your-shop.myshopify.com" : null }));
  const key = oauth ? null : setupField("api_key", p.kind === "shopify" ? "Admin API access token" : "Private API key", `Pilots: create a key in ${p.label} with this access: ${(p.scopes || []).join(", ")}. Autonomy keeps it encrypted and uses only that access.`, { secret: true });
  const label = oauth ? (fields.length ? `Continue to ${p.label}` : `Connect ${p.label}`) : `Connect ${p.label}`;
  const msg = el("p", { class: "field-error", role: "alert" });
  const submit = el("button", { type: "submit", class: ["btn", primary ? "btn-primary" : "btn-secondary"] }, label);
  const config = () => Object.fromEntries((p.config_fields || []).map((f, i) => [f.name, String(fields[i][1].value || "").trim()]));
  return el(
    "form",
    {
      class: "zt-connect",
      novalidate: true,
      onsubmit: async (event) => {
        if (event && event.preventDefault) event.preventDefault();
        msg.textContent = "";
        const cfg = config();
        const missing = (p.config_fields || []).find((f) => f.required && !cfg[f.name]);
        if (missing) {
          msg.textContent = `Type your ${missing.label.toLowerCase()} first.`;
          return;
        }
        submit.disabled = true;
        try {
          if (oauth) {
            remember(true);
            const out = await env.api.startConnect(p.kind, cfg);
            env.redirect(out.url);
            return;
          }
          const value = String(key[1].value || "").trim();
          if (!value) {
            msg.textContent = "Paste the key first.";
            return;
          }
          await env.api.connectWithKey(p.kind, value, cfg);
          env.toast(`${p.label} is connected. The first sync has started.`, "good");
          await env.rerender();
        } catch (err) {
          msg.textContent = (err && err.message) || `Could not connect ${p.label}.`;
        } finally {
          submit.disabled = false;
        }
      },
    },
    fields.length || key ? el("div", { class: "form-row zt-connect-row" }, fields.map(([f]) => f), key ? key[0] : null) : null,
    msg,
    el("div", { class: "zt-connect-actions" }, submit),
  );
}

/** "Reading Klaviyo: Marketing consent, history 12% loaded." The calm progress line while the backfill runs. */
export function readingLine(overview) {
  const parts = [];
  let detail = null;
  for (const kind of ["shopify", "klaviyo"]) {
    const p = livePlatform(overview, kind);
    for (const c of liveConnections(p)) {
      if (!LOADING_PHASES.has(c.phase)) continue;
      const loading = (c.streams || []).filter((s) => present(s.history_progress) && Number(s.history_progress) < 1);
      const what = loading.length ? loading.map((s) => `${String(s.label).toLowerCase()} ${Math.round(Number(s.history_progress) * 100)}%`).join(", ") : String(c.words || "").toLowerCase();
      parts.push(`${p.label}: ${what}`);
      detail = detail || c.detail;
    }
  }
  if (!parts.length) return null;
  const line = el("p", { class: "zt-reading", role: "status" }, el("span", { class: "zt-reading-dot", "aria-hidden": "true" }), withInfo(el("span", {}, `Reading your history. ${parts.join("; ")}.`), detail));
  return line;
}

/** Step 1 and step 2: a platform's row. */
function platformStep(n, kind, s, env) {
  const p = livePlatform(s.connectors, kind);
  const label = kind === "shopify" ? "Shopify" : "Klaviyo";
  if (!s.connectors) {
    return setupStep(n, "unknown", el("p", {}, withInfo(el("span", {}, `This server can't connect ${label} yet.`), "Connect your data on the Connect page instead.")), [link("Go to Connect", "#/connect")]);
  }
  const conns = liveConnections(p);
  if (conns.length) {
    const c = conns[0];
    const loud = c.phase === "needs_reconnect";
    const lines = [el("p", {}, el("span", { class: "zt-account" }, c.label), " · ", withInfo(el("span", {}, c.words), c.detail))];
    if (kind === "klaviyo") lines.push(s.setupLine);
    return setupStep(n, loud ? "now" : "done", lines, loud ? [link(`Reconnect on the Connect page`, "#/connect")] : []);
  }
  if (!p) return setupStep(n, "unknown", el("p", {}, `${label} is not available on this server yet.`), [link("Go to Connect", "#/connect")]);
  const state = s.states[n - 1];
  if (state === "optional") {
    const why = s.otherSource ? `Your data already comes from ${s.otherSource}, so this step can wait.` : "This step can wait.";
    return setupStep(n, "optional", el("p", {}, withInfo(el("span", {}, p.tagline), `${why} Connect ${label} on the Connect page any time.`)));
  }
  if (state !== "now") return setupStep(n, state, el("p", {}, p.tagline));
  return setupStep(n, "now", [el("p", {}, p.tagline), connectBlock(p, env, { primary: true })]);
}

/** Step 2's line once Klaviyo is connected: "Sending is set up in your Klaviyo." (or the one step to do by hand). */
async function sendingLine(api, overview) {
  const c = liveConnections(livePlatform(overview, "klaviyo"))[0];
  if (!c || typeof api.getConnectorSetup !== "function" || c.status !== "active") return null;
  try {
    const out = await api.getConnectorSetup(c.connection_id);
    if (!out) return null;
    if (out.state === "ready") return el("p", { class: "zt-ok" }, withInfo(el("span", {}, "Sending is set up in your Klaviyo."), "Autonomy created its own send flows and template there. Nothing goes out while the goal is in Shadow."));
    if (out.state === "guided") return el("p", { class: "zt-attention" }, withInfo(el("span", {}, out.words), (out.steps || []).join(" ")), " ", link("See the steps", "#/connect"));
    if (out.state === "pending") return el("p", { class: "ob-muted" }, out.words);
    return null;
  } catch {
    return null;
  }
}

/** Money as people say it: whole units from 10 up ("$60", "$29"), cents below ("$8.69"). */
function roundMoney(value, cur) {
  const v = Number(value);
  if (!Number.isFinite(v) || Math.abs(v) < 10) return money(value, cur);
  return money(Math.round(v), cur).replace(/\.00$/, "");
}
const moneyMonth = (est, cur) => `${roundMoney(est.point, cur)} a month`;
/** "$29 – $94": the range, in the same rounding as its point. */
const moneyRange = (est, cur) => (est && present(est.low) && present(est.high) ? `${roundMoney(est.low, cur)} – ${roundMoney(est.high, cur)}` : EM_DASH);

/**
 * A number's source in an owner's words: a code or file reference ("autonomy/decide/bidders.py REFILL_LIFT") is
 * "Autonomy's documented default", a path in brackets after a sentence is dropped, and "holdout" is always "people
 * we never contact" (DESIGN.md section 6).
 */
export function plainSource(source) {
  const raw = String(source || "").trim();
  const words = raw.replace(/\s*\((?:library|autonomy|docs)\/[^)]*\)/g, "").trim();
  const plain = /^(?:autonomy|library|docs)\/\S+/.test(words) ? "Autonomy's documented default" : words;
  return plain.replace(/\bholdouts?\b/gi, "people we never contact");
}

/** One proposal as a radio row: title (why behind it), the people, and the money with its range on the right. */
function proposalRow(p, scan, chosen, onPick) {
  const cur = p.currency || scan.currency;
  const id = `zt-opp-${p.opportunity_id}`;
  const sized = Boolean(p.monthly_profit) && p.enough_history;
  const disabled = p.already_running || !sized;
  const input = el("input", { type: "radio", name: "zt-goal", id, value: p.opportunity_id, checked: chosen === p.opportunity_id, disabled });
  input.addEventListener("change", () => onPick(p.opportunity_id));
  const basis = (p.basis || []).map((b) => `${b.label}: ${b.value} (${b.assumed ? "assumed: " : ""}${plainSource(b.source)}).`);
  const why = [p.why, ...basis].join(" ");
  const people = p.already_running ? "Already running" : sized ? `${int(p.audience_now)} customers now` : "Not enough history yet";
  return el(
    "li",
    { class: ["zt-opp", chosen === p.opportunity_id && "is-chosen", disabled && "is-off"], dataset: { opportunity: p.opportunity_id } },
    input,
    el(
      "label",
      { for: id, class: "zt-opp-main" },
      el("span", { class: "zt-opp-title" }, p.title),
      el("span", { class: "zt-opp-meta" }, people),
    ),
    el(
      "div",
      { class: "zt-opp-money" },
      sized ? withInfo(el("span", { class: "zt-opp-value num" }, moneyMonth(p.monthly_profit, cur)), why) : withInfo(el("span", { class: "zt-opp-value is-missing" }, EM_DASH), p.not_enough || why),
      sized ? el("span", { class: "zt-opp-range num" }, moneyRange(p.monthly_profit, cur)) : null,
    ),
  );
}

/** Step 3: the proposals, best first, the best one preselected; one click takes it to the limits. */
function goalStepSetup(s, env) {
  const { scan } = s;
  if (s.accepted) {
    return setupStep(3, "done", el("p", {}, el("strong", {}, s.accepted.goal.text || s.accepted.goal.goal_id)));
  }
  if (!scan) {
    const words = s.scanError ? "We couldn't size goals from your history." : "This server can't size goals from your history yet.";
    return setupStep(3, s.states[2] === "now" ? "unknown" : s.states[2], el("p", {}, s.scanError && present(s.scanError.message) ? withInfo(el("span", {}, words), s.scanError.message) : words), [link("Set a goal yourself", "#/goal")]);
  }
  const state = s.states[2];
  const list = Array.isArray(scan.proposals) ? scan.proposals : [];
  const chosen = list.find((p) => p.opportunity_id === s.chosen);
  if (s.stage === "limits" && chosen && chosen.monthly_profit) {
    const cur = chosen.currency || scan.currency;
    return setupStep(3, "done", el("p", {}, el("strong", {}, chosen.title), " · ", withInfo(el("span", { class: "num" }, `${moneyMonth(chosen.monthly_profit, cur)} (${moneyRange(chosen.monthly_profit, cur)})`), chosen.why)));
  }
  if (state !== "now") {
    const best = list.find((p) => p.opportunity_id === scan.preselected_id);
    return setupStep(3, state, el("p", {}, best && best.monthly_profit ? `${int(list.length)} found in your history; the best is about ${moneyMonth(best.monthly_profit, best.currency || scan.currency)}.` : "Goals sized in money, from your own order history."));
  }
  if (!list.length || !scan.preselected_id) {
    const words = scan.not_enough || "No goal has enough history behind it yet.";
    return setupStep(3, "now", [el("p", {}, words), list.length ? el("ul", { class: "zt-opps" }, list.map((p) => proposalRow(p, scan, null, () => {}))) : null], [link("Set a goal yourself", "#/goal")]);
  }
  const notes = [...(scan.notes || []), ...(scan.skipped || []).map((x) => `Left out: ${x}`)].join(" ");
  const use = button(SETUP_CLICKS[2], () => env.pick(), { kind: "primary" });
  return setupStep(
    3,
    "now",
    [
      el("p", { class: "zt-lead" }, withInfo(el("span", {}, "Extra profit over 30 days, from your own history."), notes)),
      el("ul", { class: "zt-opps", role: "radiogroup", "aria-label": "Goals found in your history" }, list.map((p) => proposalRow(p, scan, s.chosen, env.choose))),
    ],
    [use],
  );
}

const hh = (h) => `${String(Number(h)).padStart(2, "0")}:00`;

/**
 * What we will use, in a few words, from the signed terms themselves (MandateTerms): the figure the mandate holds.
 * The full sentence stays the row's description. Null for a limit the terms do not hold as one figure.
 */
export function shortUse(key, terms) {
  const t = terms || {};
  switch (key) {
    case "cadence":
      if (!present(t.max_touches)) return null;
      return Number(t.window_days) === 7 ? `At most ${int(t.max_touches)} a week` : `At most ${int(t.max_touches)} in ${int(t.window_days)} days`;
    case "discount_depth":
      if (!present(t.max_offer_pct)) return t.offers_on === false ? "No offers" : null;
      return t.offers_on ? `Offers up to ${pct(t.max_offer_pct, 0)}` : `No offers; never over ${pct(t.max_offer_pct, 0)}`;
    case "margin":
      return present(t.margin_rate) && present(t.margin_floor_pct) ? `${pct(t.margin_rate, 0)}, floor ${pct(t.margin_floor_pct, 0)}` : null;
    case "quiet_hours":
      return present(t.quiet_start) && present(t.quiet_end) ? `Quiet ${hh(t.quiet_start)} to ${hh(t.quiet_end)}` : null;
    case "budget":
      return present(t.budget_ceiling) ? `${money(t.budget_ceiling, t.currency || "USD")} for ${int(t.valid_days || 30)} days` : null;
    default:
      return null;
  }
}

/** Limits shown for reference only (not part of what is signed) fold into the row they explain. */
const FOLDED = { discount_frequency: "discount_depth" };

/** One limit: what we found, then what we will use; a flagged default says so, and the margin can be answered once. */
function limitRow(l, s, env, folded = []) {
  const terms = s.limits && s.limits.terms;
  const short = shortUse(l.key, terms);
  const about = [short ? `We will use: ${l.use}` : null, l.description, `Source: ${plainSource(l.source)}.`, ...folded.map((f) => `${f.label}: ${f.found} ${f.use}`)].filter(Boolean).join(" ");
  const extra = [];
  if (!l.from_data) extra.push(withInfo(el("span", { class: "pill pill-neutral zt-default" }, "Safe default"), "Your data could not tell us this, so we start with a safe default. It is part of what you sign, and you can change it later."));
  if (l.key === "margin" && present(l.question)) {
    const [field, input] = setupField("margin_pct", "Your margin, %", l.question, { placeholder: "40" });
    input.setAttribute("type", "number");
    input.setAttribute("min", "1");
    input.setAttribute("max", "99");
    input.setAttribute("inputmode", "decimal");
    if (present(s.marginPct)) input.value = String(s.marginPct);
    input.addEventListener("change", () => env.margin(input.value));
    extra.push(field);
  }
  if (l.key === "discount_depth" && s.limits && s.limits.terms && present(s.limits.terms.max_offer_pct)) {
    ztField += 1;
    const id = `zt-offers-${ztField}`;
    const box = el("input", { type: "checkbox", id, checked: Boolean(s.offers) });
    box.addEventListener("change", () => env.offers(Boolean(box.checked)));
    extra.push(el("div", { class: "zt-check" }, box, withInfo(el("label", { for: id }, `Allow offers up to ${pct(s.limits.terms.max_offer_pct, 0)}`), "Off by default. On, the agent may offer up to your usual discount, only to people the evidence says need it, and never below your margin floor.")));
  }
  return el(
    "li",
    { class: "zt-limit", dataset: { key: l.key } },
    el("div", { class: "zt-limit-label" }, withInfo(el("span", {}, l.label), about)),
    el("div", { class: "zt-limit-found" }, l.found),
    el("div", { class: "zt-limit-use" }, el("span", { class: "zt-arrow", "aria-hidden": "true" }, "→ "), el("span", { class: "visually-hidden" }, "We will use: "), short ? el("strong", { class: "num" }, short) : l.use, extra.length ? el("div", { class: "zt-limit-extra" }, extra) : null),
  );
}

function limitRows(s, env) {
  const all = s.limits.limits || [];
  return all.filter((l) => !FOLDED[l.key]).map((l) => limitRow(l, s, env, all.filter((f) => FOLDED[f.key] === l.key)));
}

/** Step 4: the learned limits for the chosen goal, and the one button that signs them and starts Shadow. */
function limitsStep(s, env) {
  if (s.accepted) {
    const rec = s.accepted.mandate || {};
    const m = rec.mandate || {};
    const unmet = Array.isArray(rec.unmet) ? rec.unmet : [];
    const who = unmet.length ? "Waiting for a second signature." : rec.single_owner ? "Signed by you alone." : "Signed.";
    const until = present(m.valid_until) ? ` Valid until ${when(m.valid_until, { time: false })}.` : "";
    return setupStep(4, unmet.length ? "now" : "done", el("p", {}, withInfo(el("span", {}, `${who}${until}`), unmet.length ? "The goal stays in Shadow until the limits are signed." : "The single-owner path: one signature is enough for a shop run by one person. Inside these limits Autonomy never asks you again.")));
  }
  if (s.states[3] !== "now") return setupStep(4, s.states[3], el("p", {}, "What we found in your history, and what we will use."));
  if (s.limitsError) return setupStep(4, "unknown", errorBox("We couldn't learn your limits.", s.limitsError, { retry: env.rerender }));
  if (!s.limits) return setupStep(4, "now", skeleton({ width: "100%", height: "120px" }));
  const sign = button(SETUP_CLICKS[3], () => env.sign(sign), {
    kind: "primary",
    ariaLabel: "Sign and start Shadow: signs these limits as your mandate and starts the goal in Shadow, where it decides every day and sends nothing.",
  });
  return setupStep(
    4,
    "now",
    [
      el("ul", { class: "zt-limits", "aria-label": "What we found, and what we will use" }, limitRows(s, env)),
      present((s.limits.notes || []).join(" ")) ? el("p", { class: "ob-muted" }, s.limits.notes.join(" ")) : null,
    ],
    [withInfo(sign, "Signs these limits as your mandate (you alone, the single-owner path) and starts the goal in Shadow: it decides every day and sends nothing."), el("button", { type: "button", class: "link-btn", onclick: () => env.back() }, "Choose another goal")],
  );
}

/** The email's own footer ("You can unsubscribe at any time."), which every message keeps; the card shows the rest. */
export function withoutFooter(body) {
  const parts = String(body || "").split(/\n\s*\n/);
  return parts.length > 1 && /unsubscribe/i.test(parts[parts.length - 1]) ? parts.slice(0, -1).join("\n\n") : String(body || "");
}

/** One sample message, as the customer would see it: subject, the words, the offer when there is one. */
function sampleCard(m) {
  const src = imageSrc(m.image_url);
  return el(
    "li",
    { class: "zt-sample" },
    src ? el("img", { class: "zt-sample-img", src, alt: m.image_alt || "", loading: "lazy" }) : null,
    el(
      "div",
      { class: "zt-sample-body" },
      present(m.preview_subject) ? el("p", { class: "zt-sample-subject" }, m.preview_subject) : null,
      el("p", { class: "zt-sample-text" }, withoutFooter(m.preview_body)),
      present(m.offer_label) ? el("span", { class: "pill pill-neutral zt-sample-offer" }, m.offer_label) : null,
    ),
  );
}

/** Step 5: the voice read from their tools, three samples, and one approval. */
function voiceStep(s, env) {
  const b = s.brand;
  if (b && b.status === "approved") {
    return setupStep(5, "done", el("p", {}, withInfo(el("span", {}, `Approved${present(b.approved_by) ? ` by ${b.approved_by}` : ""}.`), b.words)));
  }
  if (!s.accepted) return setupStep(5, "later", el("p", {}, "Three sample messages, in your own voice."));
  if (s.brandError) return setupStep(5, "unknown", errorBox("We couldn't read your voice.", s.brandError, { retry: env.rerender }));
  if (!b) return setupStep(5, "unknown", el("p", {}, "This server can't read your voice yet."));
  if (b.status === "not_read") {
    if (!anyConnection(s.connectors)) return setupStep(5, "optional", el("p", {}, withInfo(el("span", {}, "Connect Klaviyo or Shopify and we read your voice from them."), "Shadow runs without it: it sends nothing. The voice is needed before anything is sent.")));
    return setupStep(5, "now", [el("p", { role: "status" }, "Reading your voice from Klaviyo and Shopify…"), skeleton({ width: "100%", height: "96px" })]);
  }
  const hard = (b.found || []).filter((f) => f.hard);
  const rules = `${hard.map((f) => `${f.use} (${f.found}).`).join(" ")} Every email keeps your unsubscribe footer.`;
  const read = `Read from ${int(b.messages_read)} of your messages: ${int(hard.length)} ${hard.length === 1 ? "rule" : "rules"} every message must follow.`;
  const samples = Array.isArray(b.samples) ? b.samples : [];
  const approve = button(SETUP_CLICKS[4], () => env.approveVoice(approve), {
    kind: "primary",
    disabled: !present(b.fingerprint),
    ariaLabel: `Approve this voice: ${b.words}`,
  });
  return setupStep(
    5,
    "now",
    [
      el("p", {}, withInfo(el("span", {}, read), rules), (b.colours || []).length ? el("span", { class: "zt-swatches", "aria-hidden": "true" }, b.colours.slice(0, 4).map((c) => el("span", { class: "zt-swatch", style: { background: /^#[0-9a-f]{3,8}$/i.test(c) ? c : "transparent" } }))) : null),
      samples.length ? el("ul", { class: "zt-samples", "aria-label": "Sample messages" }, samples.map(sampleCard)) : el("p", { class: "ob-muted" }, "No sample could be written that passes your rules yet."),
    ],
    [withInfo(approve, b.words)],
  );
}

/** The end of the setup: Shadow runs, and the weekly routine from here. */
function runningCard(s) {
  const goal = s.accepted && s.accepted.goal;
  if (!goal) return null;
  return el(
    "div",
    { class: "zt-done", role: "status" },
    el("p", { class: "zt-done-line" }, el("strong", {}, "Shadow is running."), " ", withInfo(el("span", {}, "It decides every day and sends nothing."), "Come back once a week: Live lists only what needs you, and when Shadow has earned it, one click turns on Autopilot.")),
    link("See what it would do", `#/live/${encodeURIComponent(goal.goal_id)}`),
  );
}

/** Where each step stands. Exactly one is "now" (the one with the primary button), or none once all are done. */
function stepStates(s) {
  const has = (k) => liveConnections(livePlatform(s.connectors, k)).length > 0;
  const shop = has("shopify");
  const klav = has("klaviyo");
  const optional = Boolean(s.otherSource) && !shop && !klav;
  const out = [];
  out[0] = shop ? "done" : optional ? "optional" : "now";
  out[1] = klav ? "done" : optional ? "optional" : shop ? "now" : "next";
  const connected = (shop || optional) && (klav || optional);
  out[2] = s.accepted ? "done" : connected && s.stage !== "limits" ? "now" : connected ? "done" : "later";
  out[3] = s.accepted ? "done" : connected && s.stage === "limits" ? "now" : connected ? "next" : "later";
  out[4] = s.brand && s.brand.status === "approved" ? "done" : s.accepted ? "now" : "later";
  // A step left behind before its "done" (step 3 while its limits are confirmed) still reads done: its choice stands.
  return out;
}

const SETUP_TITLE = "Five clicks to Shadow";

/** The setup card: the five steps, the reading line, and "Shadow is running" once it is. */
function setupCard(s, env) {
  s.states = stepStates(s);
  const steps = [platformStep(1, "shopify", s, env), platformStep(2, "klaviyo", s, env), goalStepSetup(s, env), limitsStep(s, env), voiceStep(s, env)];
  const reading = readingLine(s.connectors);
  if (reading) {
    const at = s.states[1] === "done" ? 1 : 0;
    steps[at].querySelector(".ob-step-body").append(reading);
  }
  const isNow = (node) => String(node.getAttribute("class") || "").split(/\s+/).includes("ob-step-now");
  const current = steps.find(isNow);
  if (current) current.setAttribute("aria-current", "step");
  const done = s.states.filter((x) => x === "done").length;
  return pathCard(SETUP_TITLE, `${int(done)} of 5 done`, [el("ol", { class: "ob-steps zt-steps" }, steps), runningCard(s)]);
}

/** Draw the setup, and keep it drawn as each click lands. */
async function renderSetup(root, ctx, setup, goals, report, brand) {
  const api = ctx.api;
  const sources = report && Array.isArray(report.sources) ? report.sources : [];
  const other = sources.find((x) => x && x.kind !== "shop");
  const found = setupGoal(setup, goals);
  const s = {
    ...setup,
    goals,
    brand,
    brandError: null,
    accepted: found ? { goal: found.goal, mandate: found.record } : null,
    chosen: setup.scan ? setup.scan.preselected_id : null,
    stage: "pick",
    limits: null,
    limitsError: null,
    offers: false,
    marginPct: null,
    otherSource: other && !anyConnection(setup.connectors) && Number(report.customers) > 0 ? (SOURCE_WORDS[other.kind] || "your files") : null,
    setupLine: await sendingLine(api, setup.connectors),
  };
  const say = typeof ctx.toast === "function" ? ctx.toast : () => {};
  const draw = () => clear(root, ...frame(setupCard(s, env), true));
  const loadLimits = async () => {
    s.limits = null;
    s.limitsError = null;
    draw();
    try {
      s.limits = await api.getLimits(s.chosen, { offers: s.offers, marginPct: s.marginPct });
    } catch (err) {
      s.limitsError = err;
    }
    draw();
  };
  const loadBrand = async () => {
    const goalId = s.accepted && s.accepted.goal ? s.accepted.goal.goal_id : null;
    try {
      s.brand = typeof api.getBrand === "function" ? await api.getBrand(goalId) : null;
      if (s.brand && s.brand.status === "not_read" && anyConnection(s.connectors) && typeof api.readBrand === "function") {
        draw();
        s.brand = await api.readBrand(goalId);
      }
    } catch (err) {
      s.brandError = err;
    }
    draw();
  };
  const env = {
    api,
    toast: say,
    redirect: typeof ctx.redirect === "function" ? ctx.redirect : (url) => window.location.assign(url),
    rerender: () => render(root, ctx),
    choose: (id) => {
      s.chosen = id;
      draw();
    },
    back: () => {
      s.stage = "pick";
      draw();
    },
    pick: () => {
      s.stage = "limits";
      remember(true);
      loadLimits();
    },
    offers: (on) => {
      s.offers = on;
      loadLimits();
    },
    margin: async (value) => {
      const v = Number(value);
      if (!Number.isFinite(v) || v <= 0 || v >= 100) return;
      s.marginPct = v;
      try {
        if (typeof api.answerMargin === "function") s.scan = (await api.answerMargin(v)) || s.scan;
      } catch (err) {
        say(`Couldn't keep your margin. ${err.message || ""}`.trim(), "critical");
      }
      loadLimits();
    },
    sign: async (btn) => {
      btn.disabled = true;
      try {
        const body = { opportunity_id: s.chosen, offers: s.offers };
        if (present(s.marginPct)) body.margin_pct = s.marginPct;
        const out = await api.acceptProposal(body);
        s.accepted = out;
        say(out.words || "Shadow is running: it decides every day and sends nothing.", "good");
        if (typeof ctx.refreshGoals === "function") await ctx.refreshGoals();
        await loadBrand();
      } catch (err) {
        say(`Couldn't sign the limits. ${err.message || ""}`.trim(), "critical");
        btn.disabled = false;
      }
    },
    approveVoice: async (btn) => {
      btn.disabled = true;
      try {
        s.brand = await api.approveBrand(s.brand.fingerprint);
        remember(false);
        say("Voice approved. Every message is checked against it.", "good");
        draw();
      } catch (err) {
        say(`Couldn't approve the voice. ${err.message || ""}`.trim(), "critical");
        btn.disabled = false;
      }
    },
  };
  draw();
  if (s.accepted && (!s.brand || s.brand.status === "not_read")) await loadBrand();
}

const SOURCE_WORDS = { file: "your uploaded files", database: "your database", simulation: "the simulated sample shop", other: "another source" };

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
  const [goalsR, dataR] = await Promise.allSettled([
    api.listGoals(),
    typeof api.getDataReport === "function" ? api.getDataReport() : Promise.resolve(null),
  ]);
  if (goalsR.status === "rejected") {
    const failed = pathCard("Your path", null, el("div", { class: "ob-error" }, errorBox("We couldn't load your goals.", goalsR.reason, { retry: () => render(root, ctx) })));
    clear(root, ...frame(failed, true));
    return;
  }
  const goals = Array.isArray(goalsR.value) ? goalsR.value : [];

  // Zero-touch setup first: while nothing runs yet (or the goal set up here waits for its voice), this page is the setup.
  const setup = await loadSetup(api, goals).catch(() => null);
  let brand = null;
  const mine = setup ? setupGoal(setup, goals) : null;
  if (mine && typeof api.getBrand === "function") brand = await Promise.resolve(api.getBrand(mine.goal.goal_id)).catch(() => null);
  if (wantsSetup(setup, goals, brand)) {
    await renderSetup(root, ctx, setup, goals, valueOf(dataR), brand);
    return;
  }
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
