/* The comparison behind the visible-text check: a page may show a never-shown term only as often as its
   baseline allows. Pure functions, so the planted-violation tests can call them without a page. */
import { findNeverShown } from "../../../../../ui/labels.js";

/**
 * What is wrong with one page: terms the baseline does not list (`unlisted`) and terms shown more often
 * than the baseline allows (`worse`). `allowed` is `{term: count}`, empty for a page with no baseline.
 */
export function violations(text, allowed = {}) {
  const found = findNeverShown(text);
  const unlisted = [];
  const worse = [];
  for (const [term, count] of Object.entries(found)) {
    if (!(term in allowed)) unlisted.push({ term, count });
    else if (count > allowed[term]) worse.push({ term, count, allowed: allowed[term] });
  }
  return { found, unlisted, worse, ok: unlisted.length === 0 && worse.length === 0 };
}

/** A readable line for a failing page. */
export function describe(page, v) {
  const lines = [];
  for (const { term, count } of v.unlisted) {
    lines.push(`${page}: shows "${term}" ${count}x and it is not on this page's allow-list`);
  }
  for (const { term, count, allowed } of v.worse) {
    lines.push(`${page}: shows "${term}" ${count}x, the allow-list allows ${allowed}x`);
  }
  return lines.join("\n");
}

/** The baseline lowered to what the pages show now: a count can only go down, a term can only go away. */
export function lowered(baseline, shown) {
  const next = {};
  for (const [page, terms] of Object.entries(baseline)) {
    const kept = {};
    for (const [term, count] of Object.entries(terms)) {
      const now = (shown[page] || {})[term] || 0;
      if (now > 0) kept[term] = Math.min(count, now);
    }
    if (Object.keys(kept).length) next[page] = kept;
  }
  return next;
}

/** A table of the baseline: page, term, count (the to-do list for M172). */
export function table(baseline) {
  const rows = [];
  for (const [page, terms] of Object.entries(baseline)) {
    for (const [term, count] of Object.entries(terms).sort((a, b) => b[1] - a[1])) {
      rows.push([page, term, String(count)]);
    }
  }
  const head = ["page", "term", "count"];
  const widths = head.map((h, i) => Math.max(h.length, ...rows.map((r) => r[i].length)));
  const line = (r) => r.map((c, i) => c.padEnd(widths[i])).join("  ");
  return [line(head), widths.map((w) => "-".repeat(w)).join("  "), ...rows.map(line)].join("\n");
}
