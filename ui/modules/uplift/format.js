// Number formats the uplift screens share. Each takes a value the API sent and returns text, and
// each returns the em dash for a value the API did not send - `ui/dom.js`'s one placeholder rule
// (plan §13.3), applied to the shapes only uplift artefacts have: a `ConfidenceValue`, a difference
// of two rates, a calendar date with no time zone.

import { EM_DASH, fmtNum, present } from "../../dom.js";

const MINUS = "−";

/** A signed number with a real minus sign, so "-0.4" and "+0.4" line up in a column. */
export function signed(value, places = 1) {
  const text = fmtNum(Math.abs(value), places);
  if (Number(text) === 0) return "0";
  return `${value < 0 ? MINUS : "+"}${text}`;
}

/** A difference of two rates, in percentage points: 0.023 -> "+2.3 pts". */
export const fmtPts = (value, places = 1) => (present(value) ? `${signed(value * 100, places)} pts` : EM_DASH);

/** A rate as a percentage, or the em dash. */
export const fmtRate = (value, places = 1) => (present(value) ? `${fmtNum(value * 100, places)}%` : EM_DASH);

/** A plain number with `places` decimals and a real minus sign, or the em dash. */
export const fmtVal = (value, places = 4) =>
  present(value) ? `${value < 0 ? MINUS : ""}${fmtNum(Math.abs(value), places)}` : EM_DASH;

/** A count of people or conversions, which may be fractional when it is an expectation. */
export const fmtCount = (value, places = 1) =>
  present(value) ? `${value < 0 ? MINUS : ""}${fmtNum(Math.abs(value), places)}` : EM_DASH;

/**
 * A `ConfidenceValue` as one line: "0.0123 (95% CI 0.0041 to 0.0205)". A missing interval stays
 * visibly missing ("CI —") rather than disappearing, so a point estimate never reads as certain.
 */
export function fmtCi(cv, fmt = fmtVal) {
  if (!cv || !present(cv.value)) return EM_DASH;
  const level = present(cv.confidence_level) ? `${fmtNum(cv.confidence_level * 100, 0)}% CI` : "CI";
  const interval =
    present(cv.ci_low) && present(cv.ci_high) ? `${fmt(cv.ci_low)} to ${fmt(cv.ci_high)}` : EM_DASH;
  return `${fmt(cv.value)} (${level} ${interval})`;
}

/** Just the interval of a `ConfidenceValue`, for a KPI tile's second line. */
export function fmtInterval(cv, fmt = fmtVal) {
  if (!cv || !present(cv.ci_low) || !present(cv.ci_high)) return EM_DASH;
  const level = present(cv.confidence_level) ? `${fmtNum(cv.confidence_level * 100, 0)}% CI` : "CI";
  return `${level} ${fmt(cv.ci_low)} to ${fmt(cv.ci_high)}`;
}

/**
 * A calendar date the API sent as `YYYY-MM-DD` (`results_available_on`). Read as UTC and printed
 * in UTC: parsed as local time it would show the day before for anyone west of Greenwich.
 */
export function fmtDay(iso) {
  if (!present(iso)) return EM_DASH;
  const at = new Date(/^\d{4}-\d{2}-\d{2}$/.test(iso) ? `${iso}T00:00:00Z` : iso);
  if (Number.isNaN(at.getTime())) return EM_DASH;
  return at.toLocaleDateString("en-IN", { day: "2-digit", month: "short", year: "numeric", timeZone: "UTC" });
}

/** A p-value the way a reader expects it: "< 0.001" rather than a string of zeroes. */
export const fmtP = (p) => (present(p) ? (p < 0.001 ? "< 0.001" : fmtNum(p, 3)) : EM_DASH);
