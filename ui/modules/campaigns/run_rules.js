// What a person chooses when they start a campaign, and how a campaign run is told back to them. Pure: no DOM,
// no fetch. The minutes are the server's (`campaign_speeds` in `configs/engine.yaml`, sent with the model offer);
// 5, 15 and 40 stand until it has answered.

import { COST_NEEDS_CONFIRMATION } from "../../cost.js";
import { esc, fmtInt } from "../../dom.js";
import { userText } from "../../labels.js";

/** The minutes each speed falls back to when the server has not said (the same as `campaign_speeds` in configs/engine.yaml). */
const FALLBACK_MINUTES = Object.freeze({ quick: 5, balanced: 15, thorough: 40 });
const minutes = { ...FALLBACK_MINUTES };

/**
 * The server's minutes per speed (`speeds` in `GET /campaign-runs/model-offer`, from `campaign_speeds` in
 * configs/engine.yaml). A speed the server did not give, or gave as anything but a positive number, keeps its fallback.
 */
export function applySpeedMinutes(speeds) {
  for (const id of Object.keys(FALLBACK_MINUTES)) {
    const n = speeds && typeof speeds === "object" ? speeds[id] : null;
    minutes[id] = Number.isFinite(n) && n > 0 ? Math.round(n) : FALLBACK_MINUTES[id];
  }
}

const makeSpeed = (id, name, recommended = false) =>
  Object.freeze({
    id,
    name,
    get minutes() {
      return minutes[id];
    },
    ...(recommended ? { recommended: true } : {}),
  });

/** How long to learn: Quick, Balanced (recommended) or Thorough, each with the time it takes about. */
export const SPEEDS = Object.freeze([makeSpeed("quick", "Quick"), makeSpeed("balanced", "Balanced", true), makeSpeed("thorough", "Thorough")]);

export const DEFAULT_SPEED = "balanced";

export const speedOf = (id) => SPEEDS.find((s) => s.id === id) || SPEEDS.find((s) => s.id === DEFAULT_SPEED);

/** "1 minute", "15 minutes". */
export const minutesText = (n) => `${n} ${n === 1 ? "minute" : "minutes"}`;

/** "about 15 minutes". */
export const aboutMinutes = (speed) => `about ${minutesText(speedOf(speed).minutes)}`;

/**
 * The sentence above the Start button, as HTML: what we learn from, how many customers, and how long it takes.
 * `table` is the person's own file name; `customers` may be unknown; `reuse` is a goal that already has a model,
 * which is used instead of learning again (so there is no choice of time). `time: false` leaves the minutes out, where the
 * three speeds are drawn beside the sentence (the minutes are said in one place).
 */
export function startSentenceHtml({ table, customers = null, speed = DEFAULT_SPEED, reuse = false, time: withTime = true }) {
  const from = table ? userText(table) : "your data";
  const many = Number.isFinite(customers) ? `, ${esc(fmtInt(customers))} customers,` : "";
  const verb = reuse ? "use last month's model on" : "learn from";
  const time = reuse ? "This takes a few minutes." : `${esc(speedOf(speed).name)}: ${esc(aboutMinutes(speed))}.`;
  return `We will ${verb} ${from}${many} and build your contact list.${withTime ? ` ${time}` : ""}`;
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
