// Number formats the uplift screens share. Each takes a value the API sent and returns text, and
// each returns the em dash for a value the API did not send - `ui/dom.js`'s one placeholder rule
// (plan §13.3), applied to the shapes only uplift artefacts have: a `ConfidenceValue`, a difference
// of two rates, a calendar date with no time zone.
//
// One precision everywhere (docs/UI_AUDIT.md §3 NUMBERS): rates and points 1 decimal, people whole,
// metrics 3 decimals. The locales are `dom.js`'s two constants; nothing here names another.

import { DATE_LOCALE, EM_DASH, NUMBER_LOCALE, fmtNum, present } from "../../dom.js";

const MINUS = "−";

/** A number with digit grouping and at most `places` decimals, in the one number locale. */
const grouped = (value, places) =>
  Number(value).toLocaleString(NUMBER_LOCALE, { maximumFractionDigits: places, minimumFractionDigits: 0 });

/** A signed number with a real minus sign, so "-0.4" and "+0.4" line up in a column. */
export function signed(value, places = 1) {
  const text = fmtNum(Math.abs(value), places);
  if (Number(text) === 0) return "0";
  return `${value < 0 ? MINUS : "+"}${grouped(Math.abs(value), places)}`;
}

/** A difference of two rates, in percentage points: 0.023 -> "+2.3 pts". */
export const fmtPts = (value, places = 1) => (present(value) ? `${signed(value * 100, places)} pts` : EM_DASH);

/** A rate as a percentage with a fixed number of decimals, so rates line up: 0.39 -> "39.0%". */
export const fmtRate = (value, places = 1) => (present(value) ? `${(Number(value) * 100).toFixed(places)}%` : EM_DASH);

/** A plain number with `places` decimals and a real minus sign, or the em dash. */
export const fmtVal = (value, places = 3) =>
  present(value) ? `${value < 0 ? MINUS : ""}${fmtNum(Math.abs(value), places)}` : EM_DASH;

/** A count of people: whole, grouped, with a real minus sign ("−3", "1,204"). */
export const fmtCount = (value) => {
  if (!present(value)) return EM_DASH;
  const whole = Math.round(Number(value));
  if (whole === 0) return "0";
  return `${whole < 0 ? MINUS : ""}${grouped(Math.abs(whole), 0)}`;
};

/** An amount the client entered (a cost, a value per customer): at most 2 decimals, grouped. */
export const fmtAmount = (value) =>
  present(value) ? `${value < 0 ? MINUS : ""}${grouped(Math.abs(value), 2)}` : EM_DASH;

/**
 * A `ConfidenceValue` as one line: "0.012 (95% CI 0.004 to 0.021)". A missing interval stays
 * visibly missing ("CI —") rather than disappearing, so a point estimate never reads as certain.
 */
export function fmtCi(cv, fmt = fmtVal) {
  if (!cv || !present(cv.value)) return EM_DASH;
  const level = present(cv.confidence_level) ? `${fmtNum(cv.confidence_level * 100, 0)}% CI` : "CI";
  const interval =
    present(cv.ci_low) && present(cv.ci_high) ? `${fmt(cv.ci_low)} to ${fmt(cv.ci_high)}` : EM_DASH;
  return `${fmt(cv.value)} (${level} ${interval})`;
}

/** Just the interval of a `ConfidenceValue`, for a tile's second line. */
export function fmtInterval(cv, fmt = fmtVal) {
  if (!cv || !present(cv.ci_low) || !present(cv.ci_high)) return EM_DASH;
  const level = present(cv.confidence_level) ? `${fmtNum(cv.confidence_level * 100, 0)}% CI` : "CI";
  return `${level} ${fmt(cv.ci_low)} to ${fmt(cv.ci_high)}`;
}

/** "likely 133 to 357": an interval in plain words, or `""` when either end is missing. */
export function fmtLikely(low, high, fmt = fmtCount) {
  if (!present(low) || !present(high)) return "";
  return `likely ${fmt(low)} to ${fmt(high)}`;
}

/**
 * A calendar date the API sent as `YYYY-MM-DD` (`results_available_on`). Read as UTC and printed
 * in UTC: parsed as local time it would show the day before for anyone west of Greenwich.
 */
export function fmtDay(iso) {
  if (!present(iso)) return EM_DASH;
  const at = new Date(/^\d{4}-\d{2}-\d{2}$/.test(iso) ? `${iso}T00:00:00Z` : iso);
  if (Number.isNaN(at.getTime())) return EM_DASH;
  return at.toLocaleDateString(DATE_LOCALE, { day: "2-digit", month: "short", year: "numeric", timeZone: "UTC" });
}

/** A p-value the way a reader expects it: "< 0.001" rather than a string of zeroes. */
export const fmtP = (p) => (present(p) ? (p < 0.001 ? "< 0.001" : fmtNum(p, 3)) : EM_DASH);
