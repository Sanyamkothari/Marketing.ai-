// The "New campaign" wizard's screens (Plan M §3.1, M177, DEC-1607): one page, five steps.
//
// Pure: every builder draws from its arguments (the API's answers, run through `wizard_rules.js`), so the
// jsdom tests call them with real answers. The page keeps Plan M §3.3's rules: one H1, one sentence under it,
// and at most one primary button on each step. Words follow the glossary (`ui/labels.js`, Plan M §4.1): goal,
// contact list, comparison group, priority; anything technical sits in a `details.tech` section.

import { term, userText } from "../../labels.js";
import { count } from "../../format.js";
import { crumbs, errorBox, esc, pageHead, techDetails } from "../../dom.js";
import {
  SHARED_STEPS,
  STEPS,
  complianceResult,
  costLine,
  expectedValue,
  dayOf,
  goalName,
  heldBack,
  stepHref,
  testSizeLines,
  valueFigure,
  valueSentence,
} from "./wizard_rules.js";

export const TITLE = "New campaign";
export const LEAD =
  "Plan a campaign in five steps: choose a goal, add your customers, check who is contacted, review it, and send the list.";

/**
 * The step indicator: done steps, the current one (`aria-current="step"`), the ones still to come. The five
 * named steps are always drawn. A screen that covers two of them (Review shows Audience too, `SHARED_STEPS`)
 * marks both as on screen; `aria-current` stays on the step whose button the screen ends with.
 */
export function stepperHtml(current) {
  const at = STEPS.findIndex((s) => s.id === current);
  const shown = new Set(SHARED_STEPS[current] || [current]);
  return `<ol class="cw-steps" aria-label="Steps">${STEPS.map((s, i) => {
    const state = shown.has(s.id) ? "now" : i < at ? "done" : "todo";
    return `<li class="cw-step ${state}" data-cw-stepper="${esc(s.id)}"${i === at ? ' aria-current="step"' : ""}><span class="cw-no" aria-hidden="true">${
      state === "done" ? "✓" : i + 1
    }</span><span class="cw-sl">${esc(s.label)}</span></li>`;
  }).join("")}</ol>`;
}

/** The whole page: the header, the steps and the step's own body. */
export function pageHtml(step, body) {
  const head = pageHead(
    `${crumbs([{ label: "Campaigns", href: "#/campaigns" }, { label: TITLE }])}<h1 class="h1">${esc(TITLE)}</h1><p class="desc">${esc(LEAD)}</p>`,
  );
  return `<main class="screen cw" data-module="campaign-wizard" data-cw-step="${esc(step)}">${head}${stepperHtml(step)}<div class="cw-body">${body}</div></main>`;
}

const card = (title, inner, attrs = "") =>
  `<section class="card cw-card"${attrs ? ` ${attrs}` : ""}><div class="cw-card-body"><h2 class="cw-h">${esc(title)}</h2>${inner}</div></section>`;

const p = (text, attrs = "") => (text ? `<p class="cw-p"${attrs ? ` ${attrs}` : ""}>${esc(text)}</p>` : "");

const actions = (inner) => `<div class="cw-actions">${inner}</div>`;

/** A notice in place of a step that cannot go on, with at most one way forward. */
export function noticeHtml({ title, text = [], action = null, attrs = "" }) {
  const button = action
    ? action.href
      ? `<a class="btn ${esc(action.kind || "primary")}" href="${esc(action.href)}"${action.attrs ? ` ${action.attrs}` : ""}>${esc(action.label)}</a>`
      : `<button type="button" class="btn ${esc(action.kind || "primary")}"${action.attrs ? ` ${action.attrs}` : ""}>${esc(action.label)}</button>`
    : "";
  return card(title, `${[].concat(text).map((t) => p(t)).join("")}${button ? actions(button) : ""}`, attrs);
}

export const loadingHtml = (text = "Loading…") => `<p class="loading" role="status">${esc(text)}</p>`;

// --- Step 1: Goal ----------------------------------------------------------------------------------------

const goalButton = (g) =>
  `<button type="button" class="cw-goal" data-cw-goal="${esc(g.id)}"><span class="cw-goal-name">${esc(g.name)}</span>${
    g.stage ? `<span class="cw-goal-stage">${esc(g.stage)}</span>` : ""
  }</button>`;

/** The goal cards (`goalCards`), and the operational goals folded under "More goals". */
export function goalHtml({ goals = [], more = [], canStart = true }) {
  if (!canStart) {
    return noticeHtml({
      title: "You can look, but not start a campaign",
      text: ["Starting a campaign needs the Analyst role. Ask an administrator, or someone with that role, to start it."],
      attrs: "data-cw-no-role",
    });
  }
  if (!goals.length && !more.length) {
    return noticeHtml({
      title: "No goal is set up yet",
      text: ["An administrator adds goals for this installation. Until then there is nothing to plan a campaign for."],
      attrs: "data-cw-no-goals",
    });
  }
  const folded = more.length
    ? `<details class="cw-more" data-cw-more><summary>More goals</summary>${p(
        "These goals do not contact customers, so they have no campaign. Each opens its own page.",
      )}<div class="cw-goals">${more.map(goalButton).join("")}</div></details>`
    : "";
  return card("What is the campaign for?", `<div class="cw-goals" data-cw-goals>${goals.map(goalButton).join("")}</div>${folded}`, "data-cw-goal-card");
}

// --- Step 2: Customers -----------------------------------------------------------------------------------

/** What the person is asked for: the first campaign of a goal learns from history; later ones reuse it. */
export function customersLead(first) {
  return first
    ? "This is the first campaign for this goal. Choose a file of your customers that says what each one did last time. Marketing AI learns from it, then picks who to contact from the same customers."
    : "Choose the file of customers for this campaign. It is checked and prepared the way you approved before.";
}

/** The validation report a refused start carries (`409` with `validation`): its own messages, read-only. */
function checksHtml(report) {
  const items = (report.checks || [])
    .filter((c) => c.severity === "error")
    .map((c) => `<li>${esc(c.message)}${c.suggestion ? ` <span class="cw-note">${esc(c.suggestion)}</span>` : ""}</li>`)
    .join("");
  return `<div class="cw-problem" role="alert" data-cw-checks><p class="cw-p">The file cannot be used as it is. Change your answers above and approve again.</p>${
    items ? `<ul class="cw-list">${items}</ul>` : ""
  }</div>`;
}

/**
 * Step 2 before anything has started: the lead, the two places Guided setup (Plan G/L, unchanged) mounts
 * into, and the last refusal - a validation report, a cost the server wants confirmed, or an error.
 */
export function customersHtml({ uc, first, canStart = true, hasGuided = true, problem = null, busy = "" }) {
  const name = goalName(uc);
  if (!canStart) {
    return noticeHtml({
      title: "You can look, but not start a campaign",
      text: ["Starting a campaign needs the Analyst role. Ask an administrator, or someone with that role, to start it."],
      attrs: "data-cw-no-role",
    });
  }
  if (!hasGuided) {
    return noticeHtml({
      title: "Guided setup is not available here",
      text: [`Add the customers for "${name}" on the goal's own page.`],
      action: { label: "Open the goal's page", href: `#/uc/${encodeURIComponent(uc.id)}` },
      attrs: "data-cw-no-guided",
    });
  }
  let trouble = "";
  if (problem && problem.validation) trouble = checksHtml(problem.validation);
  else if (problem && problem.confirm) {
    trouble = `<div class="cw-problem" role="alert" data-cw-cost-confirm>${p(problem.confirm)}${actions(
      `<button type="button" class="btn secondary" data-cw-confirm-cost${busy ? " disabled" : ""}>Start it anyway</button>`,
    )}</div>`;
  } else if (problem && problem.error) trouble = errorBox(problem.error);
  const working = busy ? `<p class="loading" role="status" data-cw-busy>${esc(busy)}</p>` : "";
  return card(
    `Customers for "${name}"`,
    `${p(customersLead(first), "data-cw-lead")}${working}${trouble}<div class="setup-grid cw-guided" data-cw-guided><div data-cw-guided-main></div><aside class="setup-side" data-cw-guided-aside></aside></div>`,
    `data-cw-customers="${esc(first ? "first" : "again")}"`,
  );
}

/** Step 2 while the list is being prepared: one sentence and how far it has got. */
export function progressHtml({ learning, share = null }) {
  const pct = share === null ? null : Math.round(share * 100);
  const meter =
    pct === null
      ? ""
      : `<div class="run-meter cw-meter" role="progressbar" aria-label="Progress" aria-valuemin="0" aria-valuemax="100" aria-valuenow="${pct}"><div class="run-meter-fill" style="width:${pct}%"></div></div>`;
  const text = learning
    ? "Marketing AI is learning from your customers' history, then it builds the contact list. This takes a few minutes. You can leave this page and come back: the campaign carries on."
    : "Marketing AI is building the contact list from your customers. This usually takes under a minute.";
  return card(learning ? "Learning from your customers" : "Building the contact list", `${p(text)}${meter}`, `data-cw-working="${learning ? "learning" : "list"}" role="status"`);
}

/**
 * Step 2 when the new model waits for an approval and this person does not have the Approver role: the
 * campaign waits, and the notice says where an Approver will find it (the bell, `#/approvals`). A person with
 * that role is not held here: their list is built straight away and they approve the new model on the Review
 * screen, in the same click as the plan (M181 part C, DEC-1611; `reviewHtml`).
 */
export function waitingHtml({ own = false } = {}) {
  const who = own
    ? "Marketing AI has learned from your customers. You started this learning, so another person with the Approver role must approve what it learned before the contact list is built. The campaign waits until then and carries on here."
    : "Marketing AI has learned from your customers. Before it builds the contact list, someone with the Approver role must approve what it learned. The campaign waits until then and carries on here.";
  return noticeHtml({
    title: "Waiting for an approval",
    text: [who, "An Approver finds it under Waiting for approval. Come back to this page once it is approved."],
    action: { label: "Open Waiting for approval", href: "#/approvals" },
    attrs: 'data-cw-waiting="wait"',
  });
}

/** Step 2 when the work stopped: what happened, and starting again. The engine's own words are technical. */
export function stoppedHtml({ ucId, detail, reason }) {
  const run = detail && detail.run;
  const tech = techDetails([
    ["Reference", run && run.run_id],
    ["What the system said", run && run.error && (run.error.message || run.error.code)],
  ]);
  return `${noticeHtml({
    title: "The contact list could not be prepared",
    text: [reason, "Start again with the same or another file. Nothing was sent to any customer."],
    action: { label: "Start again", href: stepHref("customers", ucId), attrs: "data-cw-restart" },
    attrs: "data-cw-stopped",
  })}${tech ? `<div class="cw-tech">${tech}</div>` : ""}`;
}

// --- Step 3: Audience -----------------------------------------------------------------------------------

const countRows = (counts) =>
  Object.entries(counts || {})
    .sort((a, b) => b[1] - a[1])
    // An offer's or a channel's name is the person's own (their configuration): marked as theirs (M172 userText).
    .map(([label, n]) => `<tr><td>${userText(label)}</td><td class="num">${esc(count(n))}</td></tr>`)
    .join("");

/**
 * "Who is contacted": the contact list's size, by priority, and by offer and channel when it has several. The
 * channel table counts the customers sent on each channel (`channel_rows`: treat = 1 rows per channel), never
 * `channel_counts`, which counts the ones a channel cannot reach.
 */
export function audienceCardHtml({ scoring, list }) {
  const contacted = list && typeof list.treat_rows === "number" ? list.treat_rows : null;
  const lead = contacted === null ? "" : p(`${count(contacted)} customers are on the ${term("contactList").toLowerCase()}.`, "data-cw-contacted");
  const bands = (scoring && scoring.bands) || [];
  const priority = bands.length
    ? `<table class="tbl cw-tbl" data-cw-priority><thead><tr><th>${esc(term("priority"))}</th><th class="num">Customers</th><th>What happens</th></tr></thead><tbody>${bands
        .map(
          (b) =>
            `<tr><td>${esc(b.name)}</td><td class="num">${esc(count(b.rows))}</td><td>${esc(b.action || "")}</td></tr>`,
        )
        .join("")}</tbody></table>`
    : "";
  const offers =
    list && list.offer_counts && Object.keys(list.offer_counts).length > 1
      ? `<h3 class="cw-h3">Which offer</h3>${p("Each customer gets the offer most likely to change what they do.")}<table class="tbl cw-tbl" data-cw-offers><thead><tr><th>Offer</th><th class="num">Customers</th></tr></thead><tbody>${countRows(
          list.offer_counts,
        )}</tbody></table>`
      : "";
  const channels =
    list && list.channel_rows && Object.keys(list.channel_rows).length
      ? `<h3 class="cw-h3">Which channel</h3><table class="tbl cw-tbl" data-cw-channels><thead><tr><th>Channel</th><th class="num">Customers</th></tr></thead><tbody>${countRows(
          list.channel_rows,
        )}</tbody></table>`
      : "";
  return card("Who is contacted", `${lead}${priority}${offers}${channels}`, "data-cw-audience");
}

/** The comparison group (the list's own held-back count) and the plain-language test size (`testSizeLines`). */
export function comparisonCardHtml(preview) {
  const lines = testSizeLines(preview);
  const basis = preview && preview.basis ? `<details class="tech"><summary>Technical details</summary><p class="cw-note">${esc(preview.basis)}</p></details>` : "";
  const body = lines
    ? `${lines.group ? p(lines.group, "data-cw-group") : ""}${p(lines.size || "The size of the test cannot be worked out for this campaign.", `data-cw-test-size="${lines.ok ? "ok" : "none"}"`)}`
    : p("The size of the test cannot be worked out for this campaign.", 'data-cw-test-size="none"');
  return card(term("comparisonGroup"), `${body}${basis}`, "data-cw-comparison");
}

/** Step 3's part of the Review screen: who is contacted and the comparison group (no button of its own). */
export function audienceHtml({ scoring, list, preview }) {
  return `${audienceCardHtml({ scoring, list })}${comparisonCardHtml(preview)}`;
}

// --- Step 4: Review (with step 3 on the same screen) -------------------------------------------------------

const tile = (label, value, attrs) =>
  `<div class="kpi" ${attrs}><div class="l">${esc(label)}</div><div class="v">${esc(value)}</div></div>`;

/** The Review screen's one primary button, by where the list's model stands (`modelState`). */
export const APPROVE_LABEL = "Approve and download";
export const APPROVE_BOTH_LABEL = "Approve the new model and this plan, then download";

/**
 * What the Review card ends with, by where the list's model stands (`model.state`, `modelState`):
 *   - approved: "Approve and download" fixes the test plan, then downloads the contact list;
 *   - pending, for an Approver: one button that says it approves the new model too, with its details one link
 *     away; the model is approved first, then the plan, then the download, each only once the one before it
 *     worked;
 *   - pending, for anyone else (or for the person who started the learning, whom the server does not let
 *     approve it, `model.own`): the campaign waits, with the way to Waiting for approval, and no Approve;
 *   - refused: the list cannot be sent; start again.
 * Returns `{ lead, buttons, note }`.
 */
/** The Review and Send screens' words for a list whose model's approval could not be checked. */
export const MODEL_UNKNOWN_TEXT =
  "Whether the model behind this list is approved could not be checked, so nothing can be approved or downloaded yet. This page tries again by itself.";

/** ... and for a list built with a new model that can no longer be approved. */
export const MODEL_SUPERSEDED_TEXT =
  "Another model came into use while the new model behind this list waited for an approval, so it can no longer be approved and this list cannot be sent. Start again: the list is then built with the model in use now.";

function decisionParts({ campaign, model, canApprove, busy }) {
  const restart = stepHref("customers", campaign.use_case_id);
  const again = `<a class="btn quiet" href="${esc(restart)}" data-cw-again>Start again with another file</a>`;
  const state = (model && model.state) || "approved";
  if (state === "unknown") {
    // The registry did not answer: whether the model is approved is not known, so nothing is offered (fail closed).
    return {
      lead: `<div class="cw-problem" data-cw-model-unknown>${p(MODEL_UNKNOWN_TEXT)}${model.error ? errorBox(model.error, { retry: true }) : ""}</div>`,
      buttons: again,
      note: "",
    };
  }
  if (state === "superseded") {
    return {
      lead: p(MODEL_SUPERSEDED_TEXT, "data-cw-model-superseded"),
      buttons: `<a class="btn primary" href="${esc(restart)}" data-cw-restart>Start again</a>`,
      note: "",
    };
  }
  if (state === "refused") {
    return {
      lead: p(
        "An Approver turned down the new model this list was built with, so this list cannot be sent. Start again: Marketing AI learns once more from the file you choose.",
        "data-cw-model-refused",
      ),
      buttons: `<a class="btn primary" href="${esc(restart)}" data-cw-restart>Start again</a>`,
      note: "",
    };
  }
  if (state === "pending" && (!model.canApprove || model.own)) {
    const who = model.own
      ? "You started this learning, so another person with the Approver role must approve the new model. They find it under Waiting for approval."
      : "This is the first campaign for this goal, and the list was built with a new model that someone with the Approver role must approve first. They find it under Waiting for approval.";
    return {
      lead: `<div class="cw-problem" data-cw-waiting="review">${p(who)}${p("This page carries on once it is approved. Nothing is downloaded before then.")}</div>`,
      buttons: `<a class="btn primary" href="#/approvals" data-cw-approvals>Open Waiting for approval</a>${again}`,
      note: "",
    };
  }
  const disabled = !canApprove || Boolean(busy);
  if (state === "pending") {
    return {
      lead: p(
        "This is the first campaign for this goal, so the list was built with what Marketing AI learned from your customers: a new model that still needs an Approver's approval. You have that role.",
        "data-cw-new-model",
      ),
      buttons: `<button type="button" class="btn primary" data-cw-approve="model-and-plan"${disabled ? " disabled" : ""}>${esc(
        busy || APPROVE_BOTH_LABEL,
      )}</button><a class="btn quiet" href="#/approvals" data-cw-approvals>See the new model's details first</a>${again}`,
      note: canApprove
        ? "This one click approves the new model, then fixes how this campaign will be judged, then downloads the contact list. Each happens only once the one before it has worked. After that the plan changes only with a written reason."
        : "Approving needs the Analyst role.",
    };
  }
  return {
    lead: "",
    buttons: `<button type="button" class="btn primary" data-cw-approve="plan"${disabled ? " disabled" : ""}>${esc(busy || APPROVE_LABEL)}</button>${again}`,
    note: canApprove
      ? "Approving fixes how this campaign will be judged, then the contact list downloads. After that the plan changes only with a written reason."
      : "Approving needs the Analyst role.",
  };
}

/**
 * Steps 3 and 4 on one screen (M181 part C, DEC-1611; Plan M §3.1, "five steps, one screen"): who is
 * contacted, the comparison group and the test size (`audienceHtml`), then the numbers the campaign will be
 * judged by and the one primary button (`decisionParts`). Tiles: contacted, comparison group, expected value
 * (when the list has one: its range when the engine gives one, `expectedValue`), the results date. Lines: the
 * expected value, the cost, the compliance result (when the switch is on), the results date. `policy` is the
 * list's run's `policy_recommendation.json` (or null). `canApprove` is whether this person may register the
 * test plan; `model` is `{ state, canApprove, own, error }` for the model behind the list (`state` from
 * `listModelState`: approved, pending, refused, superseded, or unknown when the registry did not answer, which offers
 * nothing). `busy` is the button's
 * label while its calls are on their way, else "".
 */
export function reviewHtml({
  campaign,
  scoring = null,
  list,
  policy = null,
  preview,
  cost,
  compliance,
  resultsOn,
  canApprove = true,
  model = null,
  busy = "",
  error = null,
}) {
  const held = heldBack(preview);
  const value = expectedValue(list, policy);
  const tiles = [
    list && typeof list.treat_rows === "number" ? tile("Contacted", count(list.treat_rows), "data-cw-tile=contacted") : "",
    held ? tile(term("comparisonGroup"), count(held.count), "data-cw-tile=comparison") : "",
    value ? tile("Expected value", valueFigure(value), "data-cw-tile=value") : "",
    resultsOn ? tile("Results from", dayOf(resultsOn), "data-cw-tile=results") : "",
  ].join("");
  const costText = costLine(cost);
  const checked = complianceResult(compliance);
  const rows = [
    ["Expected value", value ? valueSentence(value) : "", "data-cw-review-value"],
    ["Cost", costText ? costText.text : "", "data-cw-review-cost"],
    [
      "Checked before sending",
      checked
        ? `${count(checked.allowed)} of ${count(checked.checked)} customers may be contacted; ${count(checked.withheld)} were taken off the list${
            checked.rerouted ? ` and ${count(checked.rerouted)} moved to another channel` : ""
          }.`
        : "",
      "data-cw-review-compliance",
    ],
    ["Results", resultsOn ? `Add the results after ${dayOf(resultsOn)}.` : "", "data-cw-review-results"],
  ]
    .filter(([, text]) => text)
    .map(([label, text, attrs]) => `<div class="cw-kv" ${attrs}><dt>${esc(label)}</dt><dd>${esc(text)}</dd></div>`)
    .join("");
  const tech = techDetails([
    ["Campaign", campaign && campaign.campaign_id],
    ["How the cost is worked out", cost && cost.basis],
    ["Why the cost is not known", cost && !(costText && costText.known) ? cost.reason : null],
  ]);
  const decision = decisionParts({ campaign, model, canApprove, busy });
  return `${audienceHtml({ scoring, list, preview })}${card(
    "Check and approve",
    `${tiles ? `<div class="kpis cw-kpis">${tiles}</div>` : ""}<dl class="cw-kvs">${rows}</dl>${decision.lead}${error ? errorBox(error) : ""}${actions(
      decision.buttons,
    )}${p(decision.note, "data-cw-approve-note")}`,
    "data-cw-review",
  )}${tech ? `<div class="cw-tech">${tech}</div>` : ""}`;
}

// --- Step 5: Send ---------------------------------------------------------------------------------------

/** A campaign's status as the API gives it (`live` until a report is stored, then `measured`), in words. */
const STATUS = { live: { chip: "Live", title: "Your campaign is live" }, measured: { chip: "Measured", title: "Your campaign has its results" } };

/**
 * Step 5: the download, and what happens next. Reached only once the test plan is fixed. The status chip and
 * the title are the campaign's own `status`, never assumed.
 *
 * `downloaded` says the Review screen's Approve has just started the download (M181 part C, DEC-1611): the
 * page says so, the next step (the campaign's page) is the primary button and "Download again" is a secondary
 * link to the same file. Otherwise (a refresh, a person who comes back later) the download is the primary
 * button, as it always was: nothing claims a download this page did not start.
 */
export function sendHtml({ campaign, plan, downloadHref, resultsOn, downloaded = false, held = null }) {
  const status = STATUS[campaign.status] || { chip: String(campaign.status || ""), title: "Your campaign" };
  const when = resultsOn ? ` Add its results after ${dayOf(resultsOn)}: the campaign's page will ask for them.` : "";
  const tech = techDetails([
    ["Campaign", campaign.campaign_id],
    ["List", (campaign.run_ids || [])[0]],
    ["Test plan fixed as", plan && plan.plan_hash],
  ]);
  const listWord = esc(term("contactList").toLowerCase());
  const open = (kind) =>
    `<a class="btn ${kind}" href="#/campaigns/${encodeURIComponent(campaign.campaign_id)}" data-cw-open>Open the campaign</a>`;
  // `held` (M181 part C review): the list's model is not approved, or that could not be checked. A plan registered
  // elsewhere does not make it sendable: no download is offered until the model is approved.
  if (held) return heldSendHtml({ campaign, status, held, open });
  const lead = downloaded
    ? `Your ${listWord} is downloading. Send it from your e-mail or messaging tool.`
    : `Download the ${listWord} and send it from your e-mail or messaging tool.`;
  const buttons = downloaded
    ? `${open("primary")}<a class="btn secondary" href="${esc(downloadHref)}" download data-cw-download="again">Download again</a>`
    : `<a class="btn primary" href="${esc(downloadHref)}" download data-cw-download>Download ${listWord}</a>${open("quiet")}`;
  return `${card(
    status.title,
    `<p class="cw-p" data-cw-live>${status.chip ? `<span class="chip" data-cw-status>${esc(status.chip)}</span> ` : ""}${lead}${esc(when)}</p>${actions(buttons)}`,
    "data-cw-send",
  )}${tech ? `<div class="cw-tech">${tech}</div>` : ""}`;
}

/** Send for a list whose model is not approved (`held`: pending, refused, superseded or unknown): no download. */
function heldSendHtml({ campaign, status, held, open }) {
  const words = {
    pending:
      "The contact list was built with a new model that still waits for an Approver's approval, so it cannot be downloaded yet. This page carries on once it is approved.",
    refused: "An Approver turned down the new model this list was built with, so this list cannot be sent.",
    superseded: MODEL_SUPERSEDED_TEXT,
    unknown: MODEL_UNKNOWN_TEXT,
  };
  const primary =
    held === "pending"
      ? `<a class="btn primary" href="#/approvals" data-cw-approvals>Open Waiting for approval</a>`
      : held === "unknown"
        ? ""
        : `<a class="btn primary" href="${esc(stepHref("customers", campaign.use_case_id))}" data-cw-restart>Start again</a>`;
  return card(
    status.title,
    `<div class="cw-problem" data-cw-send-held="${esc(held)}">${p(words[held] || MODEL_UNKNOWN_TEXT)}</div>${actions(
      `${primary}${open(primary ? "quiet" : "secondary")}`,
    )}`,
    "data-cw-send",
  );
}
