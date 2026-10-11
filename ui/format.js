// One number and money format for every page (Plan M §3.3 and M172, DEC-1602).
//
// Numbers carry their units and are rounded for display; the exact figure goes in a tooltip (`title`) and in
// the downloads. The rules:
//
// - Rupees: Indian digit grouping (Rs 4,18,250), and lakh and crore in words once an amount reaches them
//   (Rs 4.18 lakh, Rs 1.2 crore), at most two decimals of the word, trailing zeroes dropped. Below Rs 100 an amount
//   keeps its paise (Rs 0.40 a contact matters); from Rs 100 it is whole rupees.
// - An amount whose source names another currency keeps it (`money(v, "USD")`), and an amount whose source names
//   none (the Hillstrom "spend" column) stays a plain number: a currency is never invented.
// - Percentages have one decimal (39.0%); counts are whole with separators (32,000), in the number locale the
//   engine writes its own sentences in (`ui/dom.js` NUMBER_LOCALE), so a tile and a sentence beside it agree.
// - A signed value has a real minus sign (−) or a plus sign (+), so a gain and a loss line up.
// - A value nobody measured is the em dash; the HTML forms put the reason in the tooltip when the page has one.
//
// No currency sign is typed into this file (tests/integration/test_ui.py looks for one): the rupee is
// written as its code point. Nothing here touches `window` or `document`, so node imports it as the browser does.

export const EM_DASH = "\u2014";
export const MINUS = "\u2212";
const RUPEE = "\u20B9";
const NUMBER_LOCALE = "en-US"; // ui/dom.js NUMBER_LOCALE; repeated so this file imports nothing

const LAKH = 1e5;
const CRORE = 1e7;

/** True only for a finite number (a numeric string counts). `null`, `undefined`, "" and NaN are not values. */
export function isValue(value) {
  if (value === null || value === undefined || value === "" || typeof value === "boolean") return false;
  return Number.isFinite(Number(value));
}

/** A whole number's digits in Indian grouping: 41825000 -> "4,18,25,000". `digits` is a string of digits. */
export function groupIndian(digits) {
  const text = String(digits);
  if (text.length <= 3) return text;
  let head = text.slice(0, -3);
  const groups = [];
  while (head.length > 2) {
    groups.unshift(head.slice(-2));
    head = head.slice(0, -2);
  }
  if (head) groups.unshift(head);
  return `${groups.join(",")},${text.slice(-3)}`;
}

/** At most `places` decimals, trailing zeroes dropped: 4.10 -> "4.1", 2.00 -> "2". */
function trimmed(value, places) {
  const fixed = Number(value).toFixed(places);
  return fixed.includes(".") ? fixed.replace(/0+$/, "").replace(/\.$/, "") : fixed;
}

/** "-", "+" or "" in front of a number, with the real minus sign. Zero takes no sign. */
function sign(value, signed) {
  if (value < 0) return MINUS;
  return signed && value > 0 ? "+" : "";
}

/** An absolute amount of rupees in Indian grouping with `places` decimals kept as written: 1234567.8 -> "12,34,567.80". */
function indianAmount(abs, places) {
  const [whole, fraction] = abs.toFixed(places).split(".");
  return fraction ? `${groupIndian(whole)}.${fraction}` : groupIndian(whole);
}

/**
 * Rupees rounded for display: "Rs 45,300", "Rs 4.18 lakh", "Rs 1.2 crore", "−Rs 2.3 lakh" (`signed`: "+Rs 4.18 lakh").
 * Below Rs 100 the paise stay ("Rs 0.40"). An amount that rounds up to the next word moves to it: Rs 99,999.60 is
 * "Rs 1 lakh", and 99.999 lakh is "Rs 1 crore". Not a number: the em dash.
 */
export function rupees(value, { signed = false } = {}) {
  if (!isValue(value)) return EM_DASH;
  const n = Number(value);
  const abs = Math.abs(n);
  const lead = `${sign(Math.abs(n) < 0.005 ? 0 : n, signed)}${RUPEE}`;
  if (abs < 100) {
    const cents = Math.round(abs * 100);
    return `${cents === 0 ? RUPEE : lead}${cents % 100 === 0 ? String(cents / 100) : (cents / 100).toFixed(2)}`;
  }
  const whole = Math.round(abs);
  if (whole < LAKH) return `${lead}${groupIndian(String(whole))}`;
  const lakh = Number((abs / LAKH).toFixed(2));
  if (lakh < 100) return `${lead}${trimmed(lakh, 2)} lakh`;
  const crore = Number((abs / CRORE).toFixed(2));
  const [crWhole, crFraction] = trimmed(crore, 2).split(".");
  return `${lead}${groupIndian(crWhole)}${crFraction ? `.${crFraction}` : ""} crore`;
}

/** Rupees exactly, for the tooltip: Indian grouping, paise only when there are some ("Rs 4,18,250", "Rs 1,23,45,678.90"). */
export function rupeesExact(value, { signed = false } = {}) {
  if (!isValue(value)) return EM_DASH;
  const n = Number(value);
  const abs = Math.abs(n);
  const places = Math.round(abs * 100) % 100 === 0 ? 0 : 2;
  return `${sign(Math.round(abs * 100) === 0 ? 0 : n, signed)}${RUPEE}${indianAmount(abs, places)}`;
}

/** Grouped in the number locale with at most `places` decimals. */
function grouped(abs, places) {
  return abs.toLocaleString(NUMBER_LOCALE, { maximumFractionDigits: places, minimumFractionDigits: 0 });
}

/** A currency other than the rupee, in its own sign where the platform knows one ("$3,578"), else its code. */
function foreign(abs, currency, places) {
  try {
    return new Intl.NumberFormat(NUMBER_LOCALE, {
      style: "currency",
      currency,
      minimumFractionDigits: places ? 2 : 0,
      maximumFractionDigits: places,
    }).format(abs);
  } catch {
    return `${currency} ${grouped(abs, places)}`;
  }
}

/**
 * An amount of money in the unit its source names. "INR" (the default) is `rupees`; another ISO code keeps
 * that currency, whole from 100 up ("$3,578"); `null` (the source names no currency) is a plain grouped
 * number ("3,578"), never rupees.
 */
export function money(value, currency = "INR", { signed = false } = {}) {
  if (!isValue(value)) return EM_DASH;
  if (currency === "INR") return rupees(value, { signed });
  const n = Number(value);
  const abs = Math.abs(n);
  const places = abs < 100 && Math.round(abs * 100) % 100 !== 0 ? 2 : 0;
  const body = currency ? foreign(abs, currency, places) : grouped(abs, places);
  return `${sign(n, signed)}${body}`;
}

/** The exact figure behind `money`, for its tooltip. */
export function moneyExact(value, currency = "INR", { signed = false } = {}) {
  if (!isValue(value)) return EM_DASH;
  if (currency === "INR") return rupeesExact(value, { signed });
  const n = Number(value);
  const abs = Math.abs(n);
  const body = currency ? foreign(abs, currency, 2) : grouped(abs, 6);
  return `${sign(n, signed)}${body}`;
}

/** A count of things: whole and grouped ("32,000"), with the real minus sign (`signed` adds "+"). */
export function count(value, { signed = false } = {}) {
  if (!isValue(value)) return EM_DASH;
  const whole = Math.round(Number(value));
  return `${sign(whole, signed)}${grouped(Math.abs(whole), 0)}`;
}

/** A fraction as a percentage with one decimal: 0.39 -> "39.0%", -0.0123 with `signed` -> "−1.2%". */
export function percent(fraction, { signed = false, places = 1 } = {}) {
  if (!isValue(fraction)) return EM_DASH;
  const pct = Number(fraction) * 100;
  const text = Math.abs(pct).toFixed(places);
  return `${sign(Number(text) === 0 ? 0 : pct, signed)}${text}%`;
}

/** The exact percentage behind `percent`, for its tooltip (up to four decimals). */
export function percentExact(fraction) {
  if (!isValue(fraction)) return EM_DASH;
  const pct = Number(fraction) * 100;
  return `${sign(pct, false)}${trimmed(Math.abs(pct), 4)}%`;
}

/** A plain number with a sign and at most `places` decimals: 0.75 -> "+0.75", -3 -> "−3". */
export function signedNumber(value, places = 2) {
  if (!isValue(value)) return EM_DASH;
  const n = Number(value);
  const text = trimmed(Math.abs(n), places);
  return `${sign(Number(text) === 0 ? 0 : n, true)}${text}`;
}

const escapeHtml = (s) =>
  String(s).replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" })[c]);

// --- campaign names on lists (M172) ----------------------------------------------------------------

/** The longest short campaign name a list shows (Plan M M172). */
export const SHORT_NAME_MAX = 40;

/** "Oct 2026" for an ISO instant (in UTC, so every viewer reads the same month); "" when there is none. */
export function monthYear(iso) {
  if (!iso) return "";
  const at = new Date(iso);
  if (Number.isNaN(at.getTime())) return "";
  return at.toLocaleDateString("en-GB", { month: "short", year: "numeric", timeZone: "UTC" });
}

/**
 * A campaign's short name: the goal and the month, at most `SHORT_NAME_MAX` characters ("Win back lapsed
 * customers — Oct 2026"). A goal too long for the room left is cut at a word with an ellipsis.
 */
export function shortCampaignName(goal, iso) {
  const month = monthYear(iso);
  const tail = month ? ` — ${month}` : "";
  const room = SHORT_NAME_MAX - tail.length;
  let head = String(goal || "Campaign").trim();
  if (head.length > room) {
    const cut = head.slice(0, room - 1);
    // Cut at the last whole word: the cut already ends one when the next character is a space.
    const space = head.charAt(room - 1) === " " ? cut.length : cut.lastIndexOf(" ");
    head = `${(space > room / 2 ? cut.slice(0, space) : cut).trimEnd()}…`;
  }
  return `${head}${tail}`;
}

/**
 * The two names a campaign shows (`GET /campaigns` record): `short` for lists, built from its goal's short name
 * (`ui/labels.js` goalShortName) and the month it went out; `long`, the label recorded with it, for the subtitle.
 * A campaign with no goal (one checked after another tool ran it) is a "Past campaign".
 */
export function campaignNames(campaign, goalShort) {
  const c = campaign || {};
  const when = c.treatment_start || c.created_at || null;
  const goal = c.use_case_id && goalShort ? goalShort(c.use_case_id) : "Past campaign";
  return { short: shortCampaignName(goal, when), long: c.name ? String(c.name) : "" };
}

/**
 * The HTML of a campaign's name on a list: the short name, and the recorded label under it as a subtitle. The
 * label is the person's own wording, so it is marked as theirs (`data-user`, see ui/labels.js `userText`).
 */
export function campaignNameHtml(names, { link = "", attrs = "" } = {}) {
  const short = escapeHtml(names.short);
  const title = link ? `<a href="${escapeHtml(link)}"${attrs ? ` ${attrs}` : ""}>${short}</a>` : short;
  const sub =
    names.long && names.long !== names.short
      ? `<span class="cn-sub" data-user style="display:block;font-size:12px;color:var(--muted)">${escapeHtml(names.long)}</span>`
      : "";
  return `<span class="cn" data-campaign-name>${title}${sub}</span>`;
}

// --- a server figure ------------------------------------------------------------------------------

/**
 * A figure the server sent (`{value, text, format}`, engine/pilot/proof.py FigureFormat) drawn with this file's
 * rules, the server's own text in the tooltip: rupees ("inr", "inr_unit") in lakh and crore, an amount with no
 * currency ("amount", "signed_amount") as a plain number, counts and shares. Any other format is the server's text.
 */
export function figureHtml(figure, { reason = "" } = {}) {
  if (!figure || typeof figure !== "object") return numberHtml(EM_DASH, "", reason);
  const text = figure.text != null ? String(figure.text) : "";
  const value = figure.value;
  const exact = text || "";
  switch (isValue(value) ? figure.format : "") {
    case "inr":
    case "inr_unit":
      return numberHtml(rupees(value), exact || rupeesExact(value), reason);
    case "amount":
      return numberHtml(money(value, null), exact || moneyExact(value, null), reason);
    case "signed_amount":
      return numberHtml(money(value, null, { signed: true }), exact || moneyExact(value, null, { signed: true }), reason);
    case "count":
      return numberHtml(count(value), exact, reason);
    case "signed_count":
      return numberHtml(count(value, { signed: true }), exact, reason);
    case "share":
      return numberHtml(percent(value), exact, reason);
    default:
      return text ? escapeHtml(text) : numberHtml(EM_DASH, "", reason);
  }
}

/**
 * A displayed number with its exact figure as the tooltip: `<span class="fmt" title="Rs 4,18,250">Rs 4.18 lakh</span>`.
 * The tooltip is left off when it would say the same thing. For a missing value pass `reason`: the em dash
 * then carries it as its tooltip ("No results yet"), else it is a bare em dash.
 */
export function numberHtml(text, exact, reason = "") {
  if (text === EM_DASH) {
    return reason ? `<span class="fmt" title="${escapeHtml(reason)}">${EM_DASH}</span>` : EM_DASH;
  }
  const title = exact && exact !== text ? ` title="${escapeHtml(exact)}"` : "";
  return `<span class="fmt"${title}>${escapeHtml(text)}</span>`;
}

/** `money` as HTML, exact in the tooltip; `reason` explains a missing amount. */
export const moneyHtml = (value, { currency = "INR", signed = false, reason = "" } = {}) =>
  numberHtml(money(value, currency, { signed }), moneyExact(value, currency, { signed }), reason);

/** `percent` as HTML, exact in the tooltip. */
export function percentHtml(fraction, { signed = false, reason = "" } = {}) {
  const shown = percent(fraction, { signed });
  const exact = percentExact(fraction);
  // "39.0%" needs no tooltip saying "39%": only a figure that rounding changed gets one.
  const same = isValue(fraction) && Number(Math.abs(Number(fraction) * 100).toFixed(1)) === Math.abs(Number(fraction) * 100);
  return numberHtml(shown, same ? "" : exact, reason);
}

/** `count` as HTML (a count is exact already, so it has a tooltip only for a missing value's reason). */
export const countHtml = (value, { signed = false, reason = "" } = {}) =>
  numberHtml(count(value, { signed }), "", reason);
