// The prototype's rendering helpers, unchanged in what they produce, plus the one rule that runs
// through every screen: a value nobody measured is an em dash, never a sample number (plan §13.3).

import { headerToolHtml } from "./modules/router.js";

export const EM_DASH = "—";

export const LOGO = `<svg class="logo" viewBox="0 0 2576 690" role="img" aria-label="Minfy"><rect class="b" x="0" y="0" width="493" height="493"/><rect class="y" x="246" y="0" width="247" height="246"/><g fill="none" class="s" stroke-width="87"><path d="M719.5 493V338A121.5 121.5 0 0 1 962.5 338V493"/><path d="M961 493V338A121 121 0 0 1 1203 338V493"/><path d="M1548.5 493V338A120.5 120.5 0 0 1 1789.5 338V493"/><path d="M1962.5 493V182A121 121 0 0 1 2204.5 182"/><path d="M2291 354A120.75 120.75 0 0 0 2532.5 354"/><path d="M2532.5 182V520A120.5 120.5 0 0 1 2319.7 597.5"/></g><circle class="b" cx="1376" cy="61" r="61"/><rect class="b" x="1332" y="182" width="87" height="311"/><rect class="b" x="2006" y="268" width="156" height="86"/><rect class="y" x="2248" y="182" width="86" height="86"/></svg>`;

export const esc = (s) =>
  String(s).replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" })[c]);

/** True only for a value the API actually sent. `0` and `false` are values; `null` is not. */
export const present = (v) => v !== null && v !== undefined && v !== "";

/** A value, or the em dash. `fmt` only ever runs on a value that is present. */
export const dash = (v, fmt = (x) => String(x)) => (present(v) ? fmt(v) : EM_DASH);

/** A number with at most `places` decimals and no trailing zeroes: 0.9000 → "0.9", 70 → "70". */
export function fmtNum(value, places = 2) {
  const fixed = Number(value).toFixed(places);
  if (!fixed.includes(".")) return fixed;
  return fixed.replace(/0+$/, "").replace(/\.$/, "");
}

/** The prototype's compact row count: a million rows reads "1.0M", four thousand reads "4K". */
export const fmtN = (n) =>
  n >= 1e6 ? `${(n / 1e6).toFixed(1)}M` : n >= 1e3 ? `${Math.round(n / 1e3)}K` : String(n);

export const fmtInt = (n) => Number(n).toLocaleString("en-IN");

export const fmtPct = (fraction, places = 1) => `${fmtNum(Number(fraction) * 100, places)}%`;

export const fmtSize = (b) =>
  b < 1024 ? `${b} B` : b < 1048576 ? `${(b / 1024).toFixed(1)} KB` : `${(b / 1048576).toFixed(1)} MB`;

/** The prototype's timestamp format, applied to an ISO instant from the API. */
export function fmtStamp(iso) {
  if (!present(iso)) return EM_DASH;
  const at = new Date(iso);
  if (Number.isNaN(at.getTime())) return EM_DASH;
  return at
    .toLocaleString("en-IN", {
      day: "2-digit",
      month: "short",
      year: "numeric",
      hour: "2-digit",
      minute: "2-digit",
    })
    .replace(/(\d{4}),?\s*/, "$1, ");
}

/** A date alone, for the date-range line on the Data page. */
export function fmtDate(iso) {
  if (!present(iso)) return EM_DASH;
  const at = new Date(iso);
  if (Number.isNaN(at.getTime())) return EM_DASH;
  return at.toLocaleDateString("en-IN", { day: "2-digit", month: "short", year: "numeric" });
}

export const typeChip = (entry) =>
  `<span class="chip type t-${esc(entry.marker)}">${esc(entry.stars)} ${esc(
    entry.label || entry.type_label || "",
  )}</span>`;

export const stageChip = (s) => `<span class="chip stage"><span>Stage</span>${esc(s)}</span>`;

export const kvs = (pairs) =>
  pairs
    .map(([k, v]) => `<div class="kv"><span class="k">${esc(k)}</span><span class="v">${esc(v)}</span></div>`)
    .join("");

export function bandPill(v) {
  const cls = /High|Critical/.test(v) ? "bad" : /Medium/.test(v) ? "warn" : "ok";
  return `<span class="pill ${cls}">${esc(v)}</span>`;
}

export const table = (cols, rows, cls = "", band = -1) =>
  `<div class="tbl-wrap"><table class="${cls}"><thead><tr>${cols
    .map((c) => `<th>${esc(c)}</th>`)
    .join("")}</tr></thead><tbody>${rows
    .map((r) => `<tr>${r.map((v, j) => `<td>${j === band ? bandPill(v) : esc(v)}</td>`).join("")}</tr>`)
    .join("")}</tbody></table></div>`;

export const kpis = (arr) =>
  `<div class="kpis">${arr
    .map(([l, v]) => `<div class="kpi"><div class="l">${esc(l)}</div><div class="v">${esc(v)}</div></div>`)
    .join("")}</div>`;

export const steps = (arr) =>
  `<ol class="steps">${arr
    .map(
      ([a, b], i) =>
        `<li><span class="no">${i + 1}</span><span><div class="s1">${esc(a)}</div><div class="s2">${esc(
          b,
        )}</div></span></li>`,
    )
    .join("")}</ol>`;

/**
 * Every screen's header. A phase module may register one tool for its right-hand side - the client
 * picker (Plan A M35) - through `modules/router.js`; with none registered the logo stands alone, as
 * it always did.
 */
export function pageHead(inner) {
  const tool = headerToolHtml();
  const right = tool ? `<div class="headtools">${tool}${LOGO}</div>` : LOGO;
  return `<div class="head"><div class="titles">${inner}</div>${right}</div><div class="rule"></div>`;
}

export const errorBox = (error) =>
  `<div class="apierr" role="alert"><b>${esc(error.code || "ERROR")}</b>${esc(
    error.message || String(error),
  )}</div>`;

// --- charts: inline SVG (plan §9.3), drawn to the prototype's own geometry -------------------
// No `viewBox`: percentage widths resolve against the element itself, so the 4px corner radius
// stays 4 real pixels exactly as the prototype's `border-radius` did, at any column width.

export function barTrack(sharePct, label) {
  const width = Math.max(0, Math.min(100, Number(sharePct)));
  return `<svg class="track" height="8" role="img" aria-label="${esc(
    label,
  )}"><rect x="0" y="0" width="100%" height="8" rx="4" fill="var(--track)"/><rect x="0" y="0" width="${width.toFixed(
    2,
  )}%" height="8" rx="4" fill="var(--c)"/></svg>`;
}

export function columnBar(heightPx, top, label) {
  const height = Math.max(3, Math.round(heightPx));
  const r = Math.min(4, height / 2);
  const fill = top ? "var(--c)" : "var(--t)";
  return `<svg class="bb" height="${height}" role="img" aria-label="${esc(
    label,
  )}"><rect x="0" y="0" width="100%" height="${height}" rx="${r}" fill="${fill}"/><rect x="0" y="${r}" width="100%" height="${
    height - r
  }" fill="${fill}"/></svg>`;
}
