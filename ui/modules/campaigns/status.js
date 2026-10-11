// What a campaign is called and where it stands (Plan M M176, DEC-1606).
//
// The ONE rule for a campaign's status chip, used by the Campaigns list and by the campaign page, so the two
// can never disagree. It reads only what the API already answers: the campaign record (`GET /campaigns` or
// `GET /campaigns/{id}`), its test plan (the `plan` of `GET /campaigns/{id}`) and its stored report. Nothing here
// changes what the engine decided; it only puts it in four words.
//
//   Measured             the campaign's final result is stored (`status: measured`). An early look never makes a
//                        campaign Measured: the engine keeps it `live` until the analysis date (DEC-1304).
//   Draft                one of our own campaigns with no test plan, no results file and no result: how it will be
//                        judged is not fixed yet, and Plan M sends no list out before it is (§3.1).
//   Waiting for results  a results file is in but the result is not final yet, or the results date has come.
//   Live                 the list went out and the results date has not come yet.
//
// The results date is the test plan's analysis date; without a plan, the day every customer's outcome window
// has closed (the day the campaign went out plus the outcome window). With neither, there is no date to wait
// for, and the campaign stays Live until its results are added.
//
// Every function is pure: no request, no DOM, so node tests can call it directly.

import { term } from "../../labels.js";

/** The four chips, in the order a campaign moves through them. `tone` picks the chip's colour. */
export const STATUS = Object.freeze({
  draft: Object.freeze({ key: "draft", label: "Draft", tone: "neutral" }),
  live: Object.freeze({ key: "live", label: "Live", tone: "live" }),
  waiting: Object.freeze({ key: "waiting", label: "Waiting for results", tone: "warn" }),
  measured: Object.freeze({ key: "measured", label: "Measured", tone: "ok" }),
});

const DAY_MS = 24 * 60 * 60 * 1000;

/** `YYYY-MM-DD` of an ISO instant or a Date, in UTC; null for nothing or an unreadable value. */
export function isoDay(value) {
  if (value === null || value === undefined || value === "") return null;
  if (typeof value === "string" && /^\d{4}-\d{2}-\d{2}$/.test(value)) return value;
  const at = value instanceof Date ? value : new Date(value);
  return Number.isNaN(at.getTime()) ? null : at.toISOString().slice(0, 10);
}

/**
 * The day the campaign's results can be read: the test plan's analysis date, else the day the outcome window
 * closes (`treatment_start` + `outcome_window_days`), else null.
 */
export function resultsDue(campaign, plan = null) {
  if (plan && plan.analysis_date) return isoDay(plan.analysis_date);
  const days = campaign ? campaign.outcome_window_days : null;
  if (days === null || days === undefined || !campaign.treatment_start) return null;
  const start = new Date(campaign.treatment_start);
  if (Number.isNaN(start.getTime())) return null;
  return isoDay(new Date(start.getTime() + Number(days) * DAY_MS));
}

/**
 * The status chip: one of `STATUS`. `report` is the stored report when known (an early look is still not a
 * result), `today` the day to judge "the results date has come" by (a Date or `YYYY-MM-DD`; now by default).
 */
export function campaignStatus({ campaign, plan = null, report = null, today = new Date() } = {}) {
  if (!campaign) return STATUS.draft;
  if (campaign.status === "measured") return STATUS.measured;
  const ours = !campaign.kind || campaign.kind === "scored";
  if (ours && !campaign.test_plan_hash && !plan && !campaign.outcomes && !report) return STATUS.draft;
  const due = resultsDue(campaign, plan);
  const day = isoDay(today);
  if (campaign.outcomes || report || (due && day && day >= due)) return STATUS.waiting;
  return STATUS.live;
}

/** A campaign another tool ran and that was audited here (Plan M §8 q3): listed, marked "Run elsewhere". */
export const runElsewhere = (campaign) => !!campaign && campaign.kind === "external";

/** A whole-programme readout (every customer against the universal comparison group), not one campaign. */
export const wholeProgramme = (campaign) => !!campaign && campaign.kind === "programme";

// --- names ------------------------------------------------------------------------------------------------

/**
 * Goal names in business words (Plan M §4.2), by goal id. Display only: ids, configuration and the API keep
 * their own names. ui/labels.js gains these in M172; the integrator then reads them from there.
 */
export const GOAL_NAMES = Object.freeze({
  "targeted-advertisement": "Find customers likely to buy",
  "win-back-campaign": "Win back lapsed customers",
  "telco-churn": "Keep customers likely to leave",
  churn: "Keep customers likely to leave",
  "payment-propensity": "Get customers to pay on time",
  rca: "Understand why customers leave",
  "hillstrom-email": "Choose the best e-mail for each customer",
  "ai-onboarding-assistant": "Answer customer questions (AI assistant)",
});

/** A goal's name: the §4.2 name, else the name the API gave (`fallback`), else a plain "Campaign". */
export function goalName(useCaseId, fallback = null) {
  if (useCaseId && GOAL_NAMES[useCaseId]) return GOAL_NAMES[useCaseId];
  return fallback || "Campaign";
}

const MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];

/** "Oct 2026": the month a campaign went out, in UTC; "" when unknown. */
export function monthOf(iso) {
  const day = isoDay(iso);
  if (!day) return "";
  const [year, month] = day.split("-");
  return `${MONTHS[Number(month) - 1]} ${year}`;
}

export const SHORT_NAME_MAX = 40;

/** Cut `text` to at most `max` characters at a word boundary, ending in "…" when it was cut. */
export function clip(text, max) {
  const s = String(text || "").trim();
  if (s.length <= max) return s;
  const room = s.slice(0, Math.max(1, max - 1));
  const space = room.lastIndexOf(" ");
  const cut = (space > max / 2 ? room.slice(0, space) : room).replace(/[\s,;:·-]+$/, "");
  return `${cut}…`;
}

/**
 * The name a list shows (Plan M M172 rule, used here first): the goal and the month it went out, at most 40
 * characters. A campaign run elsewhere, or a programme readout, has no goal of ours: its own recorded name is
 * shortened instead. The long recorded name is the subtitle (`campaign.name`).
 */
export function shortName(campaign, goal = null) {
  if (!campaign) return "";
  const month = monthOf(campaign.treatment_start);
  if (runElsewhere(campaign) || wholeProgramme(campaign) || !campaign.use_case_id) {
    return clip(campaign.name || "Campaign", SHORT_NAME_MAX);
  }
  const head = goal || goalName(campaign.use_case_id);
  if (!month) return clip(head, SHORT_NAME_MAX);
  const tail = ` · ${month}`;
  return `${clip(head, SHORT_NAME_MAX - tail.length)}${tail}`;
}

// --- the next step -----------------------------------------------------------------------------------------

/**
 * One sentence: what to do next, with the date when there is one. `fmtDay(day)` formats a `YYYY-MM-DD` (the
 * page's own date format is passed in, so this file stays free of the DOM helpers).
 */
export function nextStep({ status, campaign, plan = null, report = null }, fmtDay = (d) => d) {
  const due = resultsDue(campaign, plan);
  switch (status.key) {
    case "draft":
      return "Next step: fix how the result will be judged, then download the contact list.";
    case "live":
      return due
        ? `Next step: add results on or after ${fmtDay(due)}.`
        : "Next step: add results once customers have had time to respond.";
    case "waiting":
      if (report && report.early_look && plan) {
        return `An early look is in. The result is final on ${fmtDay(isoDay(plan.analysis_date))}: add the results again then.`;
      }
      if (campaign && campaign.outcomes) return "The results file is in. Read the result on the Results tab.";
      return due ? `Next step: add results. They were due on ${fmtDay(due)}.` : "Next step: add results.";
    case "measured":
      return campaign && campaign.measured_at
        ? `Measured on ${fmtDay(isoDay(campaign.measured_at))}. The full report is ready.`
        : "Measured. The full report is ready.";
    default:
      return "";
  }
}

/** The evidence label in words (Plan M §4.1): the Proof Pack's `claim`, never its technical label. */
export const EVIDENCE = Object.freeze({
  proven: Object.freeze({
    label: term("evidenceProven"),
    text: "The customers held back for comparison were chosen at random, so the difference was caused by the campaign.",
  }),
  stated_random: Object.freeze({
    label: term("evidenceEstimated"),
    text: "You said the comparison group was chosen at random. The files cannot confirm it, so the result is an estimate.",
  }),
  descriptive: Object.freeze({
    label: term("evidenceNotProven"),
    text: "The comparison group was not chosen at random, so the difference may have other causes.",
  }),
});

/** The evidence entry for a Proof Pack `claim`; "Not yet proven" for anything else. */
export const evidenceOf = (claim) => EVIDENCE[claim] || EVIDENCE.descriptive;
