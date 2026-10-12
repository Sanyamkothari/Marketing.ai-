// The "New campaign" wizard's screens (Plan M §3.1, M177, DEC-1607): one page, five steps.
//
// Pure: every builder draws from its arguments (the API's answers, run through `wizard_rules.js`), so the
// jsdom tests call them with real answers. The page keeps Plan M §3.3's rules: one H1, one sentence under it,
// and at most one primary button on each step. Words follow the glossary (`ui/labels.js`, Plan M §4.1): goal,
// contact list, comparison group, priority; anything technical sits in a `details.tech` section.
//
// The look (Plan M M187, DEC-1617) is the design system's (`ui/components.js`, `ui/theme.css`,
// docs/ux/VISUAL_DESIGN.md): the page header, the stepper, tag and status chips, stat tiles with a value range,
// attention items and buttons are its helpers; `wizard_styles.js` only lays them out for the wizard (the goal
// cards, the Review summary, the Send success state, the phone). The words, the routes and the flow are M177's
// and M181's, unchanged.

import { term, userText } from "../../labels.js";
import { count, percent } from "../../format.js";
import { crumbs, errorBox, esc, techDetails } from "../../dom.js";
import {
  attentionItem,
  button,
  icon,
  kpiRow,
  pageHeader,
  statTile,
  statusChip,
  stepper,
  tagChip,
} from "../../components.js";
import { progressMeterHtml, progressNoteHtml } from "../../run_progress.js"; // Plan M M189 (DEC-1619)
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

// --- the goals' line icons ----------------------------------------------------------------------------------

/**
 * One line icon per goal, drawn on the design system's 24px grid with its stroke (`components.js` `icon()`): paths
 * only. A goal not listed here gets the flag. Decorative: the goal's name says what it is.
 */
export const GOAL_ICONS = Object.freeze({
  "targeted-advertisement": '<circle cx="12" cy="12" r="9"/><circle cx="12" cy="12" r="5"/><circle cx="12" cy="12" r="1.2"/>',
  "win-back-campaign": '<path d="M3 12a9 9 0 1 0 2.64-6.36L3 8.3"/><path d="M3 3.5v4.8h4.8"/>',
  "telco-churn":
    '<path d="M19.5 12.6 12 20l-7.5-7.4A4.9 4.9 0 0 1 12 6.1a4.9 4.9 0 0 1 7.5 6.5z"/><path d="M12 6.1 10 10l3 2-2 3.5"/>',
  "payment-propensity": '<rect x="2.5" y="5" width="19" height="14" rx="2"/><path d="M2.5 10h19"/><path d="M6.5 15h4"/>',
  rca: '<circle cx="11" cy="11" r="7"/><path d="m20.5 20.5-4.55-4.55"/><path d="M11 8v3.2"/><path d="M11 14h.01"/>',
  "hillstrom-email": '<rect x="3" y="5" width="18" height="14" rx="2"/><path d="m3.5 7 8.5 6 8.5-6"/>',
  "ai-onboarding-assistant":
    '<path d="M20.5 11.5a8 8 0 0 1-11.7 7.1L3.5 20.5l1.9-5A8 8 0 1 1 20.5 11.5z"/><path d="M8.5 11.5h.01"/><path d="M12.5 11.5h.01"/><path d="M16.5 11.5h.01"/>',
  "card-default-propensity":
    '<rect x="2.5" y="5" width="19" height="14" rx="2"/><path d="M2.5 10h19"/><path d="M15.5 14v1.5"/><path d="M15.5 17.6h.01"/>',
  "first-purchase-propensity": '<path d="M5 8h14l-1.2 12H6.2z"/><path d="M9 8V6.5a3 3 0 0 1 6 0V8"/>',
  "criteo-uplift": '<rect x="3" y="4" width="18" height="12" rx="2"/><path d="M8 20h8"/><path d="M12 16v4"/><path d="m9 10 2 2 4-4"/>',
  "order-fulfillment": '<path d="M21 7.5 12 3 3 7.5v9L12 21l9-4.5z"/><path d="m3 7.5 9 4.5 9-4.5"/><path d="M12 12v9"/>',
  "fault-prediction": '<path d="M22 12h-4l-3 8L9 4l-3 8H2"/>',
});
const GOAL_ICON_FALLBACK = '<path d="M5 21V4"/><path d="M5 4h12.5l-2.5 4 2.5 4H5"/>';

/** A goal's line icon as inline SVG, drawn exactly as `components.js` `icon()` draws the design system's. */
export function goalIcon(goalId, { size = 20 } = {}) {
  const body = GOAL_ICONS[goalId] || GOAL_ICON_FALLBACK;
  return `<svg class="ds-ico" viewBox="0 0 24 24" width="${size}" height="${size}" fill="none" stroke="currentColor" stroke-width="1.75" stroke-linecap="round" stroke-linejoin="round" focusable="false" aria-hidden="true">${body}</svg>`;
}

// --- the page and its stepper -------------------------------------------------------------------------------

const LI = /<li class="st-step[\s\S]*?<\/li>/g;

/**
 * The step indicator, drawn by the design system's `stepper()`: done steps, the current one (`aria-current="step"`),
 * the ones still to come; on a phone "Step 4 of 5 · Review". The five named steps are always drawn, each tagged with
 * its id (`data-cw-stepper`). A screen that covers two of them (Review shows Audience too, `SHARED_STEPS`) draws
 * both as current, the other one exactly as `stepper()` draws a current step; `aria-current` stays on the step
 * whose button the screen ends with.
 */
export function stepperHtml(current) {
  const at = STEPS.findIndex((s) => s.id === current);
  const draw = (index) => stepper({ steps: STEPS.map((s) => s.label), current: index, label: "Steps" });
  const shown = SHARED_STEPS[current] || [current];
  let i = -1;
  return draw(at).replace(LI, (li) => {
    i += 1;
    const id = STEPS[i].id;
    const own = i !== at && shown.includes(id) ? (draw(i).match(LI) || [])[i].replace(' aria-current="step"', "") : li;
    return own.replace(/^<li /, `<li data-cw-stepper="${esc(id)}" `);
  });
}

/** The whole page: the header (breadcrumbs, the H1, its one sentence), the steps and the step's own body. */
export function pageHtml(step, body) {
  const head = pageHeader({
    title: TITLE,
    subtitle: LEAD,
    crumbs: crumbs([{ label: "Campaigns", href: "#/campaigns" }, { label: TITLE }]),
  });
  return `<main class="screen cw" data-module="campaign-wizard" data-cw-step="${esc(step)}">${head}${stepperHtml(step)}<div class="cw-body">${body}</div></main>`;
}

/** A card of the wizard: a title (with an optional icon before it) over its body. */
const card = (title, inner, attrs = "", { lead = "" } = {}) =>
  `<section class="card cw-card"${attrs ? ` ${attrs}` : ""}><div class="cw-card-body"><div class="cw-card-head">${
    lead ? `<span class="cw-card-icon">${lead}</span>` : ""
  }<h2 class="cw-h">${esc(title)}</h2></div>${inner}</div></section>`;

const p = (text, attrs = "", cls = "") =>
  text ? `<p class="cw-p${cls ? ` ${cls}` : ""}"${attrs ? ` ${attrs}` : ""}>${esc(text)}</p>` : "";

const actions = (inner, extra = "") => `<div class="cw-actions${extra ? ` ${extra}` : ""}">${inner}</div>`;

/** A link or button the wizard draws: the design system's `button()`, with the wizard's own hook (`attrs`). */
const act = ({ label, href = null, kind = "primary", attrs = "", disabled = false, busy = false, iconName = "" }) =>
  button({ label, href, kind, attrs, disabled, busy, icon: iconName });

/** Each tone's icon in a problem box, and the name a screen reader hears for it (as `attentionItem` names its). */
const PROBLEM_ICONS = Object.freeze({
  warning: { name: "alert", label: "Warning" },
  critical: { name: "critical", label: "Problem" },
  info: { name: "info", label: "Information" },
});

/** A problem inside a card (the checks' report, a cost to confirm, a model to wait for): an icon and its words. */
const problemBox = (inner, attrs = "", tone = "warning") => {
  const t = PROBLEM_ICONS[tone] ? tone : "warning";
  return `<div class="cw-problem tone-${t}"${attrs ? ` ${attrs}` : ""}><span class="cw-problem-icon">${icon(PROBLEM_ICONS[t].name, {
    size: 18,
    label: PROBLEM_ICONS[t].label,
  })}</span><div class="cw-problem-body">${inner}</div></div>`;
};

/**
 * A notice in place of a step that cannot go on, with at most one way forward: the design system's attention item
 * (its tone's icon, the title, the words, the action on the right; under the words on a phone), in a card.
 */
export function noticeHtml({ title, text = [], action = null, attrs = "", tone = "warning" }) {
  const way = action
    ? act({ label: action.label, href: action.href || null, kind: action.kind || "primary", attrs: action.attrs || "" })
    : null;
  return `<section class="card cw-card cw-notice"${attrs ? ` ${attrs}` : ""}>${attentionItem({
    tone,
    title,
    text: [].concat(text).filter(Boolean).join(" "),
    action: way,
  })}</section>`;
}

export const loadingHtml = (text = "Loading…") => `<p class="loading" role="status">${esc(text)}</p>`;

// --- Step 1: Goal ----------------------------------------------------------------------------------------

const goalButton = (g, selected) =>
  `<button type="button" class="cw-goal${g.id === selected ? " is-selected" : ""}" data-cw-goal="${esc(g.id)}"><span class="cw-goal-icon">${goalIcon(
    g.id,
  )}</span><span class="cw-goal-go" aria-hidden="true">${icon("arrowRight", { size: 16 })}</span><span class="cw-goal-name">${esc(
    g.name,
  )}</span>${g.stage ? ` <span class="cw-goal-stage">${tagChip(g.stage)}</span>` : ""}</button>`;

/**
 * The goal cards (`goalCards`), and the operational goals folded under "More goals". `selected` is the goal the
 * person last chose (a goal card is drawn chosen until the next step opens, and when they come back to this step).
 */
export function goalHtml({ goals = [], more = [], canStart = true, selected = null }) {
  if (!canStart) {
    return noticeHtml({
      title: "You can look, but not start a campaign",
      text: ["Starting a campaign needs the Analyst role. Ask an administrator, or someone with that role, to start it."],
      attrs: "data-cw-no-role",
      tone: "info",
    });
  }
  if (!goals.length && !more.length) {
    return noticeHtml({
      title: "No goal is set up yet",
      text: ["An administrator adds goals for this installation. Until then there is nothing to plan a campaign for."],
      attrs: "data-cw-no-goals",
      tone: "neutral",
    });
  }
  const folded = more.length
    ? `<details class="cw-more" data-cw-more><summary><span class="cw-more-chev" aria-hidden="true"><svg class="ds-ico" viewBox="0 0 24 24" width="16" height="16" fill="none" stroke="currentColor" stroke-width="1.75" stroke-linecap="round" stroke-linejoin="round" focusable="false" aria-hidden="true"><path d="m9 6 6 6-6 6"/></svg></span><span>More goals</span> <span class="cw-more-count">${esc(
        count(more.length),
      )}</span></summary>${p(
        "These goals do not contact customers, so they have no campaign. Each opens its own page.",
      )}<div class="cw-goals">${more.map((g) => goalButton(g, selected)).join("")}</div></details>`
    : "";
  return `<section class="cw-goal-section" data-cw-goal-card><h2 class="cw-h cw-section-title">What is the campaign for?</h2><div class="cw-goals" data-cw-goals>${goals
    .map((g) => goalButton(g, selected))
    .join("")}</div>${folded}</section>`;
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
  return problemBox(
    `<p class="cw-p">The file cannot be used as it is. Change your answers above and approve again.</p>${
      items ? `<ul class="cw-list">${items}</ul>` : ""
    }`,
    'role="alert" data-cw-checks',
    "critical",
  );
}

/**
 * Step 2 before anything has started: the lead, the two places Guided setup (Plan G/L, unchanged) mounts
 * into, and the last refusal - a validation report, a cost the server wants confirmed, or an error. The card is
 * a calm frame around Guided setup: the goal's icon and name, one sentence, then Guided setup as it is.
 */
export function customersHtml({ uc, first, canStart = true, hasGuided = true, problem = null, busy = "" }) {
  const name = goalName(uc);
  if (!canStart) {
    return noticeHtml({
      title: "You can look, but not start a campaign",
      text: ["Starting a campaign needs the Analyst role. Ask an administrator, or someone with that role, to start it."],
      attrs: "data-cw-no-role",
      tone: "info",
    });
  }
  if (!hasGuided) {
    return noticeHtml({
      title: "Guided setup is not available here",
      text: [`Add the customers for "${name}" on the goal's own page.`],
      action: { label: "Open the goal's page", href: `#/uc/${encodeURIComponent(uc.id)}` },
      attrs: "data-cw-no-guided",
      tone: "info",
    });
  }
  let trouble = "";
  if (problem && problem.validation) trouble = checksHtml(problem.validation);
  else if (problem && problem.confirm) {
    trouble = problemBox(
      `${p(problem.confirm)}${actions(
        act({ label: "Start it anyway", kind: "secondary", attrs: "data-cw-confirm-cost", disabled: Boolean(busy) }),
      )}`,
      'role="alert" data-cw-cost-confirm',
    );
  } else if (problem && problem.error) trouble = errorBox(problem.error);
  const working = busy ? `<p class="loading" role="status" data-cw-busy>${esc(busy)}</p>` : "";
  return card(
    `Customers for "${name}"`,
    `${p(customersLead(first), "data-cw-lead", "cw-lead")}${working}${trouble}<div class="setup-grid cw-guided" data-cw-guided><div data-cw-guided-main></div><aside class="setup-side" data-cw-guided-aside></aside></div>`,
    `data-cw-customers="${esc(first ? "first" : "again")}"`,
    { lead: goalIcon(uc && uc.id) },
  );
}

/**
 * Step 2 while the list is being prepared: one sentence and how far it has got. `progress` is
 * `runProgress(status)` (Plan M M189, DEC-1619): when the status carries the time-weighted estimate the bar
 * shows it, with the time left or "Finishing up…"; otherwise `share` (the stages done, 0 to 1) as before.
 */
export function progressHtml({ learning, share = null, progress = null }) {
  const reading =
    progress && progress.estimated
      ? progress
      : { pct: share === null ? null : Math.round(share * 100), estimated: false, finishing: false, eta: "" };
  const meter = `${progressMeterHtml(reading, "cw-meter")}${progressNoteHtml(reading)}`;
  const text = learning
    ? "Marketing AI is learning from your customers' history, then it builds the contact list. This takes a few minutes. You can leave this page and come back: the campaign carries on."
    : "Marketing AI is building the contact list from your customers. This usually takes under a minute.";
  return card(
    learning ? "Learning from your customers" : "Building the contact list",
    `${p(text)}${meter}`,
    `data-cw-working="${learning ? "learning" : "list"}" role="status"`,
    { lead: icon("clock", { size: 20 }) },
  );
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
    tone: "warning",
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
    tone: "critical",
  })}${tech ? `<div class="cw-tech">${tech}</div>` : ""}`;
}

// --- Step 3: Audience -----------------------------------------------------------------------------------

/**
 * The share of a count among its table's counts, as a whole percentage for its bar: 0 for nothing (or for a value
 * that is not a number), and at least 2 for any count above nothing, so a small one still shows.
 */
export function shareWidth(n, most) {
  const v = Number(n);
  const top = Number(most);
  if (!Number.isFinite(v) || !Number.isFinite(top) || top <= 0 || v <= 0) return 0;
  return Math.min(100, Math.max(2, Math.round((v / top) * 100)));
}

/** The share bar of a count among its table's counts: a track and a brand bar, hidden from screen readers. */
const shareBar = (n, most) =>
  `<span class="cw-bar" aria-hidden="true"><span style="width:${shareWidth(n, most)}%"></span></span>`;

const numCell = (n, most) => `<td class="num"><span class="cw-num">${shareBar(n, most)}<span>${esc(count(n))}</span></span></td>`;

const countRows = (counts) => {
  const rows = Object.entries(counts || {}).sort((a, b) => b[1] - a[1]);
  const most = Math.max(0, ...rows.map(([, n]) => Number(n) || 0));
  return (
    rows
      // An offer's or a channel's name is the person's own (their configuration): marked as theirs (M172 userText).
      .map(([label, n]) => `<tr><td>${userText(label)}</td>${numCell(n, most)}</tr>`)
      .join("")
  );
};

/** One of the audience's small tables: a caption-sized title, the table, and its one-line note (optional). */
const breakdown = (title, note, table) =>
  `<div class="cw-breakdown"><h3 class="cw-h3">${esc(title)}</h3>${note ? p(note, "", "cw-note") : ""}${table}</div>`;

/**
 * "Who is contacted": the contact list's size, by priority, and by offer and channel when it has several. Each is
 * a small table of counts with a share bar; what happens to a priority is a second line under its name, not a
 * column of prose. The channel table counts the customers sent on each channel (`channel_rows`: treat = 1 rows per
 * channel), never `channel_counts`, which counts the ones a channel cannot reach.
 */
export function audienceCardHtml({ scoring, list }) {
  const contacted = list && typeof list.treat_rows === "number" ? list.treat_rows : null;
  const lead = contacted === null ? "" : p(`${count(contacted)} customers are on the ${term("contactList").toLowerCase()}.`, "data-cw-contacted");
  const bands = (scoring && scoring.bands) || [];
  const most = Math.max(0, ...bands.map((b) => Number(b.rows) || 0));
  const priority = bands.length
    ? breakdown(
        `By ${term("priority").toLowerCase()}`,
        "",
        `<table class="tbl cw-tbl" data-cw-priority><thead><tr><th>${esc(term("priority"))}</th><th class="num">Customers</th></tr></thead><tbody>${bands
          .map(
            (b) =>
              `<tr><td><span class="cw-pri-name">${esc(b.name)}</span>${
                b.action ? `<span class="td-sub cw-pri-action">${esc(b.action)}</span>` : ""
              }</td>${numCell(b.rows, most)}</tr>`,
          )
          .join("")}</tbody></table>`,
      )
    : "";
  const offers =
    list && list.offer_counts && Object.keys(list.offer_counts).length > 1
      ? breakdown(
          "Which offer",
          "Each customer gets the offer most likely to change what they do.",
          `<table class="tbl cw-tbl" data-cw-offers><thead><tr><th>Offer</th><th class="num">Customers</th></tr></thead><tbody>${countRows(
            list.offer_counts,
          )}</tbody></table>`,
        )
      : "";
  const channels =
    list && list.channel_rows && Object.keys(list.channel_rows).length
      ? breakdown(
          "Which channel",
          "",
          `<table class="tbl cw-tbl" data-cw-channels><thead><tr><th>Channel</th><th class="num">Customers</th></tr></thead><tbody>${countRows(
            list.channel_rows,
          )}</tbody></table>`,
        )
      : "";
  const tables = [priority, offers, channels].filter(Boolean);
  return card(
    "Who is contacted",
    `${lead}${tables.length ? `<div class="cw-breakdowns">${tables.join("")}</div>` : ""}`,
    "data-cw-audience",
  );
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

/** The Review screen's one primary button, by where the list's model stands (`modelState`). */
export const APPROVE_LABEL = "Approve and download";
export const APPROVE_BOTH_LABEL = "Approve the new model and this plan, then download";

/** The Review and Send screens' words for a list whose model's approval could not be checked. */
export const MODEL_UNKNOWN_TEXT =
  "Whether the model behind this list is approved could not be checked, so nothing can be approved or downloaded yet. This page tries again by itself.";

/** ... and for a list built with a new model that can no longer be approved. */
export const MODEL_SUPERSEDED_TEXT =
  "Another model came into use while the new model behind this list waited for an approval, so it can no longer be approved and this list cannot be sent. Start again: the list is then built with the model in use now.";

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
function decisionParts({ campaign, model, canApprove, busy }) {
  const restart = stepHref("customers", campaign.use_case_id);
  const again = act({ label: "Start again with another file", href: restart, kind: "tertiary", attrs: "data-cw-again" });
  const state = (model && model.state) || "approved";
  if (state === "unknown") {
    // The registry did not answer: whether the model is approved is not known, so nothing is offered (fail closed).
    return {
      lead: problemBox(`${p(MODEL_UNKNOWN_TEXT)}${model.error ? errorBox(model.error, { retry: true }) : ""}`, "data-cw-model-unknown"),
      buttons: again,
      note: "",
    };
  }
  if (state === "superseded") {
    return {
      lead: problemBox(p(MODEL_SUPERSEDED_TEXT), "data-cw-model-superseded"),
      buttons: act({ label: "Start again", href: restart, attrs: "data-cw-restart" }),
      note: "",
    };
  }
  if (state === "refused") {
    return {
      lead: problemBox(
        p(
          "An Approver turned down the new model this list was built with, so this list cannot be sent. Start again: Marketing AI learns once more from the file you choose.",
        ),
        "data-cw-model-refused",
        "critical",
      ),
      buttons: act({ label: "Start again", href: restart, attrs: "data-cw-restart" }),
      note: "",
    };
  }
  if (state === "pending" && (!model.canApprove || model.own)) {
    const who = model.own
      ? "You started this learning, so another person with the Approver role must approve the new model. They find it under Waiting for approval."
      : "This is the first campaign for this goal, and the list was built with a new model that someone with the Approver role must approve first. They find it under Waiting for approval.";
    return {
      lead: problemBox(`${p(who)}${p("This page carries on once it is approved. Nothing is downloaded before then.")}`, 'data-cw-waiting="review"'),
      buttons: `${act({ label: "Open Waiting for approval", href: "#/approvals", attrs: "data-cw-approvals" })}${again}`,
      note: "",
    };
  }
  const disabled = !canApprove || Boolean(busy);
  if (state === "pending") {
    return {
      lead: problemBox(
        p(
          "This is the first campaign for this goal, so the list was built with what Marketing AI learned from your customers: a new model that still needs an Approver's approval. You have that role.",
        ),
        "data-cw-new-model",
        "info",
      ),
      buttons: `${act({
        label: busy || APPROVE_BOTH_LABEL,
        attrs: 'data-cw-approve="model-and-plan"',
        disabled,
        busy: Boolean(busy),
        iconName: busy ? "" : "download",
      })}${act({ label: "See the new model's details first", href: "#/approvals", kind: "tertiary", attrs: "data-cw-approvals" })}${again}`,
      note: canApprove
        ? "This one click approves the new model, then fixes how this campaign will be judged, then downloads the contact list. Each happens only once the one before it has worked. After that the plan changes only with a written reason."
        : "Approving needs the Analyst role.",
    };
  }
  return {
    lead: "",
    buttons: `${act({
      label: busy || APPROVE_LABEL,
      attrs: 'data-cw-approve="plan"',
      disabled,
      busy: Boolean(busy),
      iconName: busy ? "" : "download",
    })}${again}`,
    note: canApprove
      ? "Approving fixes how this campaign will be judged, then the contact list downloads. After that the plan changes only with a written reason."
      : "Approving needs the Analyst role.",
  };
}

/**
 * The Review screen's summary at the top (M187): who is contacted, the comparison group and the expected value as
 * stat tiles; the expected value's likely range is drawn as a value range bar with the sentence beside it
 * (`data-cw-review-value`: the range, or the plain note that none can be given).
 */
function summaryTiles({ list, preview, value }) {
  const held = heldBack(preview);
  const tiles = [];
  if (list && typeof list.treat_rows === "number") {
    tiles.push(
      statTile({
        label: "Contacted",
        value: count(list.treat_rows),
        caption: `Customers on the ${term("contactList").toLowerCase()}`,
        attrs: 'data-cw-tile="contacted"',
      }),
    );
  }
  if (held) {
    tiles.push(
      statTile({
        label: term("comparisonGroup"),
        value: count(held.count),
        caption: `Customers not contacted (${percent(held.share)})`,
        attrs: 'data-cw-tile="comparison"',
      }),
    );
  }
  if (value) {
    const ranged = value.low !== null && value.high !== null;
    tiles.push(
      statTile({
        label: "Expected value",
        value: valueFigure(value),
        range: {
          low: ranged ? value.low : null,
          high: ranged ? value.high : null,
          text: valueSentence(value),
          attrs: "data-cw-review-value",
        },
        attrs: 'data-cw-tile="value"',
      }),
    );
  }
  if (!tiles.length) return "";
  // The wide value tile's grid (1fr 1fr 2fr) only for the three tiles it was drawn for; any other row lays its tiles
  // out evenly (a list without a comparison group, or without a value, has two).
  return kpiRow(tiles, { attrs: `data-cw-summary="${value && tiles.length === 3 ? "value" : "counts"}"` });
}

/** The compliance gate's result as a status chip and its sentence: who may be contacted, who was taken off. */
function complianceLine(checked) {
  const none = checked.allowed === 0;
  const chip = statusChip("health", none ? "failed" : "read", { word: none ? "No customer may be contacted" : "Checked" });
  const text = `${count(checked.allowed)} of ${count(checked.checked)} customers may be contacted; ${count(checked.withheld)} were taken off the list${
    checked.rerouted ? ` and ${count(checked.rerouted)} moved to another channel` : ""
  }.`;
  return `${chip}<span class="cw-kv-text">${esc(text)}</span>`;
}

/**
 * Steps 3 and 4 on one screen (M181 part C, DEC-1611; Plan M §3.1, "five steps, one screen"): a summary of the
 * numbers the campaign will be judged by (`summaryTiles`), who is contacted, the comparison group and the test size
 * (`audienceHtml`), then the cost, the compliance result (when the switch is on) and the results date, and the one
 * primary button (`decisionParts`). `policy` is the list's run's `policy_recommendation.json` (or null).
 * `canApprove` is whether this person may register the test plan; `model` is `{ state, canApprove, own, error }`
 * for the model behind the list (`state` from `listModelState`: approved, pending, refused, superseded, or unknown
 * when the registry did not answer, which offers nothing). `busy` is the button's label while its calls are on
 * their way, else "".
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
  const value = expectedValue(list, policy);
  const costText = costLine(cost);
  const checked = complianceResult(compliance);
  const rows = [
    ["Cost", costText ? esc(costText.text) : "", "data-cw-review-cost"],
    ["Checked before sending", checked ? complianceLine(checked) : "", "data-cw-review-compliance"],
    ["Results", resultsOn ? esc(`Add the results after ${dayOf(resultsOn)}.`) : "", "data-cw-review-results"],
  ]
    .filter(([, html]) => html)
    .map(([label, html, attrs]) => `<div class="cw-kv" ${attrs}><dt>${esc(label)}</dt><dd>${html}</dd></div>`)
    .join("");
  const tech = techDetails([
    ["Campaign", campaign && campaign.campaign_id],
    ["How the cost is worked out", cost && cost.basis],
    ["Why the cost is not known", cost && !(costText && costText.known) ? cost.reason : null],
  ]);
  const decision = decisionParts({ campaign, model, canApprove, busy });
  return `${summaryTiles({ list, preview, value })}${audienceHtml({ scoring, list, preview })}${card(
    "Check and approve",
    `${rows ? `<dl class="cw-kvs">${rows}</dl>` : ""}${decision.lead}${error ? errorBox(error) : ""}<div class="cw-decide">${actions(
      decision.buttons,
      "cw-decide-actions",
    )}${p(decision.note, "data-cw-approve-note", "cw-note")}</div>`,
    "data-cw-review",
  )}${tech ? `<div class="cw-tech">${tech}</div>` : ""}`;
}

// --- Step 5: Send ---------------------------------------------------------------------------------------

/** A campaign's status as the API gives it (`live` until a report is stored, then `measured`), in words. */
const STATUS = { live: { chip: "Live", title: "Your campaign is live" }, measured: { chip: "Measured", title: "Your campaign has its results" } };

/**
 * Step 5: the download, and what happens next. Reached only once the test plan is fixed. The status chip and
 * the title are the campaign's own `status`, never assumed. Drawn as a success state (M187): a check, the title,
 * the status chip, one sentence, then the actions (full width on a phone).
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
  const list = term("contactList").toLowerCase();
  const open = (kind) =>
    act({ label: "Open the campaign", href: `#/campaigns/${encodeURIComponent(campaign.campaign_id)}`, kind, attrs: "data-cw-open" });
  // `held` (M181 part C review): the list's model is not approved, or that could not be checked. A plan registered
  // elsewhere does not make it sendable: no download is offered until the model is approved.
  if (held) return heldSendHtml({ campaign, status, held, open });
  const lead = downloaded
    ? `Your ${list} is downloading. Send it from your e-mail or messaging tool.`
    : `Download the ${list} and send it from your e-mail or messaging tool.`;
  const download = (kind, label, attrs) =>
    `<a class="btn ${kind}" href="${esc(downloadHref)}" download ${attrs}>${icon("download")}<span>${esc(label)}</span></a>`;
  const buttons = downloaded
    ? `${open("primary")}${download("secondary", "Download again", 'data-cw-download="again"')}`
    : `${download("primary", `Download ${list}`, "data-cw-download")}${open("secondary")}`;
  const chip = status.chip ? statusChip("campaign", campaign.status, { word: status.chip, attrs: "data-cw-status" }) : "";
  return `<section class="card cw-card cw-done" data-cw-send><div class="cw-done-body"><span class="cw-done-icon">${icon("check", {
    size: 28,
  })}</span><h2 class="cw-h cw-done-title">${esc(status.title)}</h2>${chip ? `<p class="cw-done-chip">${chip}</p>` : ""}<p class="cw-p cw-done-text" data-cw-live>${esc(
    lead,
  )}${esc(when)}</p>${actions(buttons, "cw-done-actions")}</div></section>${tech ? `<div class="cw-tech">${tech}</div>` : ""}`;
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
      ? act({ label: "Open Waiting for approval", href: "#/approvals", attrs: "data-cw-approvals" })
      : held === "unknown"
        ? ""
        : act({ label: "Start again", href: stepHref("customers", campaign.use_case_id), attrs: "data-cw-restart" });
  return card(
    status.title,
    `${problemBox(p(words[held] || MODEL_UNKNOWN_TEXT), `data-cw-send-held="${esc(held)}"`, held === "pending" ? "warning" : "critical")}${actions(
      `${primary}${open(primary ? "tertiary" : "secondary")}`,
    )}`,
    "data-cw-send",
  );
}
