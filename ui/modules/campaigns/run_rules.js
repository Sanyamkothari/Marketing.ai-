// What a person chooses when they start a campaign, and how a campaign run is told back to them. Pure: no DOM,
// no fetch. The minutes below must match `campaign_speeds` in `configs/engine.yaml`, the server's.

import { COST_NEEDS_CONFIRMATION } from "../../cost.js";
import { esc, fmtInt } from "../../dom.js";
import { userText } from "../../labels.js";

/** How long to learn: Quick, Balanced (recommended) or Thorough, each with the time it takes about. */
export const SPEEDS = Object.freeze([
  Object.freeze({ id: "quick", name: "Quick", minutes: 5 }),
  Object.freeze({ id: "balanced", name: "Balanced", minutes: 15, recommended: true }),
  Object.freeze({ id: "thorough", name: "Thorough", minutes: 40 }),
]);

export const DEFAULT_SPEED = "balanced";

export const speedOf = (id) => SPEEDS.find((s) => s.id === id) || SPEEDS.find((s) => s.id === DEFAULT_SPEED);

/** "about 15 minutes". */
export const aboutMinutes = (speed) => `about ${speedOf(speed).minutes} minutes`;

/**
 * The sentence above the Start button, as HTML: what we learn from, how many customers, and how long it takes.
 * `table` is the person's own file name; `customers` may be unknown; `reuse` is a goal that already has a model,
 * which is used instead of learning again (so there is no choice of time).
 */
export function startSentenceHtml({ table, customers = null, speed = DEFAULT_SPEED, reuse = false }) {
  const from = table ? userText(table) : "your data";
  const many = Number.isFinite(customers) ? `, ${esc(fmtInt(customers))} customers,` : "";
  const verb = reuse ? "use last month's model on" : "learn from";
  const time = reuse ? "This takes a few minutes." : `${esc(speedOf(speed).name)}: ${esc(aboutMinutes(speed))}.`;
  return `We will ${verb} ${from}${many} and build your contact list. ${time}`;
}

/**
 * Next month's choice on Start, as plain words: `offer` is `GET /campaign-runs/model-offer`. Returns null the first
 * month (only learning, nothing to choose); else the two options, which one is picked (the person's `chosen`, else
 * the hint's suggestion; reusing only when it can) and the one-line hint.
 */
export function modelChoice(offer, chosen = null) {
  if (!offer || !offer.model_version_id) return null;
  const reuse = offer.usable && (chosen || offer.suggest) === "reuse";
  return Object.freeze({
    reuse,
    modelVersionId: offer.model_version_id,
    canReuse: Boolean(offer.usable),
    reason: offer.usable ? "" : offer.reason || "",
    hint: offer.hint || "",
  });
}

/** Plain words for what each state of a campaign run is. */
export const RUN_WORDS = Object.freeze({
  running: "In progress",
  done: "Ready",
  stopped: "Stopped",
  failed: "Did not finish",
  cancelled: "Cancelled",
});

/** Whether a run stopped only because its cost needs the person's yes. */
export const needsCostYes = (record) => Boolean(record.error && record.error.code === COST_NEEDS_CONFIRMATION);

/** The label of the button that starts a run that did not finish again. */
export function restartLabel(record) {
  if (needsCostYes(record)) return "Start it anyway";
  return record.state === "stopped" ? "Restart" : record.state === "cancelled" ? "Start again" : "Try again";
}

/** Where a finished run leads: the human gate (review, approve and download), else the goal's own result. */
export function readyHref(record) {
  if (record.campaign_id) return `#/campaigns/new/review/${encodeURIComponent(record.campaign_id)}`;
  return record.score_run_id
    ? `#/uc/${encodeURIComponent(record.use_case_id)}/output/${encodeURIComponent(record.score_run_id)}`
    : "#/campaigns";
}

export const runHref = (id) => `#/campaigns/run/${encodeURIComponent(id)}`;
