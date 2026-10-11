// The "New campaign" wizard's screens (Plan M §3.1, M177, DEC-1607): one page, five steps.
//
// Pure: every builder draws from its arguments (the API's answers, run through `wizard_rules.js`), so the
// jsdom tests call them with real answers. The page keeps Plan M §3.3's rules: one H1, one sentence under it,
// and at most one primary button on each step. Words follow the glossary (`ui/labels.js`, Plan M §4.1): goal,
// contact list, comparison group, priority; anything technical sits in a `details.tech` section.

import { term } from "../../labels.js";
import { crumbs, errorBox, esc, pageHead, techDetails } from "../../dom.js";
import {
  STEPS,
  complianceResult,
  costLine,
  expectedValue,
  fmtCount,
  fmtDay,
  fmtRupees,
  goalName,
  stepHref,
  testSizeLines,
} from "./wizard_rules.js";

export const TITLE = "New campaign";
export const LEAD =
  "Plan a campaign in five steps: choose a goal, add your customers, check who is contacted, review it, and send the list.";

/** The step indicator: done steps, the current one (`aria-current="step"`), the ones still to come. */
export function stepperHtml(current) {
  const at = STEPS.findIndex((s) => s.id === current);
  return `<ol class="cw-steps" aria-label="Steps">${STEPS.map((s, i) => {
    const state = i < at ? "done" : i === at ? "now" : "todo";
    return `<li class="cw-step ${state}" data-cw-stepper="${esc(s.id)}"${i === at ? ' aria-current="step"' : ""}><span class="cw-no" aria-hidden="true">${
      i < at ? "✓" : i + 1
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
 * Step 2 when the new model waits for an approval. A person who may approve does it here, in one click (the
 * details stay one link away, on the approvals page); anyone else sees that the campaign waits, and where an
 * Approver will find it (the bell, `#/approvals`). `own` says the server refused because this person started
 * the learning themselves (separation of duties).
 */
export function waitingHtml({ canApprove, own = false, busy = false, error = null }) {
  const lead =
    "Marketing AI has learned from your customers. Before it builds the contact list, someone with the Approver role must approve what it learned.";
  if (canApprove && !own) {
    return card(
      "Approve what Marketing AI learned",
      `${p(lead)}${p("You have that role. Approve it here and the contact list is built straight away.")}${error ? errorBox(error) : ""}${actions(
        `<button type="button" class="btn primary" data-cw-approve-model${busy ? " disabled" : ""}>${busy ? "Approving…" : "Approve and continue"}</button><a class="btn quiet" href="#/approvals" data-cw-approvals>See the details first</a>`,
      )}`,
      'data-cw-waiting="approve"',
    );
  }
  return noticeHtml({
    title: "Waiting for an approval",
    text: [
      `${lead} The campaign waits until then and carries on here.`,
      own
        ? "You started this learning, so another person with the Approver role must approve it. They find it under Waiting for approval."
        : "An Approver finds it under Waiting for approval. Come back to this page once it is approved.",
    ],
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
    .map(([label, n]) => `<tr><td>${esc(label)}</td><td class="num">${esc(fmtCount(n))}</td></tr>`)
    .join("");

/** "Who is contacted": the contact list's size, by priority, and by offer and channel when it has several. */
export function audienceCardHtml({ scoring, list }) {
  const contacted = list && typeof list.treat_rows === "number" ? list.treat_rows : null;
  const lead = contacted === null ? "" : p(`${fmtCount(contacted)} customers are on the ${term("contactList").toLowerCase()}.`, "data-cw-contacted");
  const bands = (scoring && scoring.bands) || [];
  const priority = bands.length
    ? `<table class="tbl cw-tbl" data-cw-priority><thead><tr><th>${esc(term("priority"))}</th><th class="num">Customers</th><th>What happens</th></tr></thead><tbody>${bands
        .map(
          (b) =>
            `<tr><td>${esc(b.name)}</td><td class="num">${esc(fmtCount(b.rows))}</td><td>${esc(b.action || "")}</td></tr>`,
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
    list && list.channel_counts && Object.keys(list.channel_counts).length
      ? `<h3 class="cw-h3">Which channel</h3><table class="tbl cw-tbl" data-cw-channels><thead><tr><th>Channel</th><th class="num">Customers</th></tr></thead><tbody>${countRows(
          list.channel_counts,
        )}</tbody></table>`
      : "";
  return card("Who is contacted", `${lead}${priority}${offers}${channels}`, "data-cw-audience");
}

/** The comparison group and the plain-language test size, from the planner's point (`testSizeLines`). */
export function comparisonCardHtml(preview) {
  const lines = testSizeLines(preview);
  const basis = preview && preview.basis ? `<details class="tech"><summary>Technical details</summary><p class="cw-note">${esc(preview.basis)}</p></details>` : "";
  const body = lines
    ? `${p(lines.group, "data-cw-group")}${p(lines.size, `data-cw-test-size="${lines.ok ? "ok" : "none"}"`)}`
    : p("The size of the test cannot be worked out for this campaign.", 'data-cw-test-size="none"');
  return card(term("comparisonGroup"), `${body}${basis}`, "data-cw-comparison");
}

export function audienceHtml({ ucId, scoring, list, preview }) {
  return `${audienceCardHtml({ scoring, list })}${comparisonCardHtml(preview)}${actions(
    `<button type="button" class="btn primary" data-cw-next>Continue</button><a class="btn quiet" href="${esc(
      stepHref("customers", ucId),
    )}" data-cw-again>Start again with another file</a>`,
  )}`;
}

// --- Step 4: Review --------------------------------------------------------------------------------------

const tile = (label, value, attrs) =>
  `<div class="kpi" ${attrs}><div class="l">${esc(label)}</div><div class="v">${esc(value)}</div></div>`;

/**
 * Step 4: the numbers the campaign will be judged by, then Approve. Tiles: contacted, comparison group,
 * expected value (when the list has one), the results date. Lines: the test size, the cost, the compliance
 * result (when the switch is on). `canApprove` is whether this person may register the test plan.
 */
export function reviewHtml({ campaign, list, preview, cost, compliance, resultsOn, canApprove = true, busy = false, error = null }) {
  const lines = testSizeLines(preview);
  const point = preview && preview.points && preview.current_index !== null ? preview.points[preview.current_index] : null;
  const value = expectedValue(list);
  const tiles = [
    list && typeof list.treat_rows === "number" ? tile("Contacted", fmtCount(list.treat_rows), "data-cw-tile=contacted") : "",
    point ? tile(term("comparisonGroup"), fmtCount(point.n_control), "data-cw-tile=comparison") : "",
    value ? tile("Expected value", fmtRupees(value.amount), "data-cw-tile=value") : "",
    resultsOn ? tile("Results from", fmtDay(resultsOn), "data-cw-tile=results") : "",
  ].join("");
  const costText = costLine(cost);
  const checked = complianceResult(compliance);
  const rows = [
    ["Test size", lines ? [lines.size].filter(Boolean).join(" ") : "", "data-cw-review-size"],
    ["Expected value", value ? `${fmtRupees(value.amount)} ${value.text}.` : "", "data-cw-review-value"],
    ["Cost", costText ? costText.text : "", "data-cw-review-cost"],
    [
      "Checked before sending",
      checked
        ? `${fmtCount(checked.allowed)} of ${fmtCount(checked.checked)} customers may be contacted; ${fmtCount(checked.withheld)} were taken off the list${
            checked.rerouted ? ` and ${fmtCount(checked.rerouted)} moved to another channel` : ""
          }.`
        : "",
      "data-cw-review-compliance",
    ],
    ["Results", resultsOn ? `Add the results after ${fmtDay(resultsOn)}.` : "", "data-cw-review-results"],
  ]
    .filter(([, text]) => text)
    .map(([label, text, attrs]) => `<div class="cw-kv" ${attrs}><dt>${esc(label)}</dt><dd>${esc(text)}</dd></div>`)
    .join("");
  const tech = techDetails([
    ["Campaign", campaign && campaign.campaign_id],
    ["How the cost is worked out", cost && cost.basis],
    ["Why the cost is not known", cost && !(costText && costText.known) ? cost.reason : null],
  ]);
  const why = canApprove ? "Approving fixes how this campaign will be judged. After that it changes only with a written reason." : "Approving needs the Analyst role.";
  return `${card(
    "Check and approve",
    `${tiles ? `<div class="kpis cw-kpis">${tiles}</div>` : ""}<dl class="cw-kvs">${rows}</dl>${error ? errorBox(error) : ""}${actions(
      `<button type="button" class="btn primary" data-cw-approve${!canApprove || busy ? " disabled" : ""}>${busy ? "Approving…" : "Approve"}</button><a class="btn quiet" href="${esc(
        stepHref("audience", campaign.campaign_id),
      )}" data-cw-back>Back</a>`,
    )}${p(why, "data-cw-approve-note")}`,
    "data-cw-review",
  )}${tech ? `<div class="cw-tech">${tech}</div>` : ""}`;
}

// --- Step 5: Send ---------------------------------------------------------------------------------------

/** Step 5: the one download, and what happens next. Offered only once the test plan is fixed. */
export function sendHtml({ campaign, plan, downloadHref, resultsOn }) {
  const when = resultsOn ? ` Add its results after ${fmtDay(resultsOn)}: the campaign's page will ask for them.` : "";
  const tech = techDetails([
    ["Campaign", campaign.campaign_id],
    ["List", (campaign.run_ids || [])[0]],
    ["Test plan fixed as", plan && plan.plan_hash],
  ]);
  return `${card(
    "Your campaign is live",
    `<p class="cw-p" data-cw-live><span class="chip" data-cw-status>Live</span> Download the ${esc(term("contactList").toLowerCase())} and send it from your e-mail or messaging tool.${esc(when)}</p>${actions(
      `<a class="btn primary" href="${esc(downloadHref)}" download data-cw-download>Download ${esc(term("contactList").toLowerCase())}</a><a class="btn quiet" href="#/campaigns/${encodeURIComponent(
        campaign.campaign_id,
      )}" data-cw-open>Open the campaign</a>`,
    )}`,
    "data-cw-send",
  )}${tech ? `<div class="cw-tech">${tech}</div>` : ""}`;
}
