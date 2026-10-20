// Small helpers every Autonomy screen shares. Copied in spirit from v1 (ui/dom.js, ui/components.js), not
// imported: v2 stands alone. One rule runs through all of them: a value nobody sent is an em dash, never a
// made-up number, and every estimate is shown as its range, never as a bare point.

export const EM_DASH = "—";

/** True only for a value the API actually sent. `0` and `false` are values; `null`, `undefined`, "", NaN and
 * infinities are not. */
export const present = (v) =>
  v !== null && v !== undefined && v !== "" && !(typeof v === "number" && !Number.isFinite(v));

/** A value, or the em dash. `fmt` only runs on a value that is present. */
export const dash = (v, fmt = (x) => String(x)) => (present(v) ? fmt(v) : EM_DASH);

const NUMBER_LOCALE = "en-US";

/**
 * Build an element. `attrs` keys: `class`, `text`, `style` (object), `dataset` (object), `on<event>` (function),
 * booleans (true sets the attribute, false leaves it off) and any other attribute as a string. Children may be
 * strings, numbers, nodes, arrays or null/false (skipped). Text is always set as text, so nothing is parsed as HTML.
 */
export function el(tag, attrs = {}, ...children) {
  const node = document.createElement(tag);
  for (const [key, value] of Object.entries(attrs || {})) {
    if (value === null || value === undefined || value === false) continue;
    if (key === "class") node.className = Array.isArray(value) ? value.filter(Boolean).join(" ") : value;
    else if (key === "text") node.textContent = String(value);
    else if (key === "style" && typeof value === "object") Object.assign(node.style, value);
    else if (key === "dataset" && typeof value === "object") Object.assign(node.dataset, value);
    else if (key.startsWith("on") && typeof value === "function") node.addEventListener(key.slice(2).toLowerCase(), value);
    else if (value === true) node.setAttribute(key, "");
    else node.setAttribute(key, String(value));
  }
  append(node, children);
  return node;
}

/** Append children (same rules as `el`) to `node`. */
export function append(node, children) {
  for (const child of [children].flat(Infinity)) {
    if (child === null || child === undefined || child === false) continue;
    node.append(child instanceof Node ? child : document.createTextNode(String(child)));
  }
  return node;
}

/** Empty `node` and fill it with `children`. */
export function clear(node, ...children) {
  node.replaceChildren();
  return append(node, children);
}

/** A whole number with digit grouping. */
export const int = (n) => dash(n, (x) => Number(x).toLocaleString(NUMBER_LOCALE, { maximumFractionDigits: 0 }));

/** A plain number with at most `places` decimals. */
export const num = (n, places = 2) =>
  dash(n, (x) => Number(x).toLocaleString(NUMBER_LOCALE, { maximumFractionDigits: places }));

/** A fraction as a percentage: 0.0008 -> "0.08%", 0.1 -> "10%". */
export const pct = (fraction, places = 2) =>
  dash(fraction, (x) => `${(Number(x) * 100).toLocaleString(NUMBER_LOCALE, { maximumFractionDigits: places })}%`);

/**
 * Money in the goal's currency, built by Intl (so no currency sign is typed here). Whole units from 100 up,
 * cents below. An unknown currency code still shows the amount and the code; a missing value is the em dash.
 */
export function money(value, currency = "USD") {
  if (!present(value) || !Number.isFinite(Number(value))) return EM_DASH;
  const amount = Number(value);
  const whole = Math.abs(amount) >= 100;
  const code = String(currency || "").toUpperCase();
  const opts = { style: "currency", currency: code };
  if (whole) Object.assign(opts, { minimumFractionDigits: 0, maximumFractionDigits: 0 });
  try {
    return new Intl.NumberFormat(NUMBER_LOCALE, opts).format(amount);
  } catch {
    return `${amount.toLocaleString(NUMBER_LOCALE, { maximumFractionDigits: whole ? 0 : 2 })} ${code}`.trim();
  }
}

/**
 * An estimate (`{point, low, high}`) as its range, "low – high". With a currency it is money; with
 * `{format: "pct"}` a percentage; `{format: "x"}` a multiple (ROI); otherwise a plain number. The point is never
 * shown alone: `{withPoint: true}` reads "about P (low – high)". A missing estimate or bound is the em dash.
 */
export function range(estimate, currency, opts = {}) {
  if (!estimate || !present(estimate.low) || !present(estimate.high)) return EM_DASH;
  const fmt =
    opts.format === "pct"
      ? (x) => pct(x, 1)
      : opts.format === "x"
        ? (x) => `${num(x, 2)}×`
        : currency
          ? (x) => money(x, currency)
          : (x) => num(x, 2);
  const span = `${fmt(estimate.low)} – ${fmt(estimate.high)}`;
  if (opts.withPoint && present(estimate.point)) return `about ${fmt(estimate.point)} (${span})`;
  return span;
}

/** "win_back" -> "Win back"; the em dash for nothing. */
export const humanize = (v) =>
  dash(v, (x) => {
    const s = String(x).replace(/_/g, " ").trim();
    return s.charAt(0).toUpperCase() + s.slice(1);
  });

/** An ISO instant as "19 Oct 2026, 06:00"; the em dash if missing or unreadable. */
export function when(iso, { time = true } = {}) {
  if (!present(iso)) return EM_DASH;
  const at = new Date(iso);
  if (Number.isNaN(at.getTime())) return EM_DASH;
  const opts = { day: "numeric", month: "short", year: "numeric" };
  if (time) Object.assign(opts, { hour: "2-digit", minute: "2-digit" });
  return at.toLocaleString("en-GB", opts);
}

/** A two-digit hour of the day: 9 -> "09:00". */
export const hour = (h) => dash(h, (x) => `${String(x).padStart(2, "0")}:00`);

const TONES = new Set(["good", "warning", "critical", "info", "neutral"]);

/** A small status label. Colour is never the only signal: the word is always there. */
export function pill(text, tone = "neutral") {
  return el("span", { class: ["pill", `pill-${TONES.has(tone) ? tone : "neutral"}`] }, text);
}

/** A goal's status in the words every screen uses ("active" reads "Running"). */
export function goalStatusWords(status) {
  return { active: "Running", paused: "Paused", draft: "Not started", done: "Finished" }[status] || humanize(status);
}

/** The tone for a goal or content status, for `pill`. */
export function statusTone(status) {
  return (
    {
      active: "good",
      approved: "good",
      paused: "warning",
      draft: "info",
      rejected: "critical",
      retired: "neutral",
      done: "neutral",
      critical: "critical",
      warning: "warning",
      info: "info",
    }[status] || "neutral"
  );
}

/** A card: optional title (with a heading level), body children and an actions row. */
export function card({ title, level = 3, body = [], actions = [], className = "", label } = {}) {
  const heading = title ? el(`h${level}`, { class: "card-title" }, title) : null;
  const actionRow = actions.length ? el("div", { class: "card-actions" }, actions) : null;
  return el("section", { class: ["card", className], "aria-label": label || null }, heading, body, actionRow);
}

/**
 * A table with a caption. `columns` is `[{key, label, format?, align?}]`; `rows` are objects. A missing cell is
 * the em dash. Wrapped in a scroller so it never widens the page on a phone.
 */
export function table({ caption, columns, rows }) {
  const head = el("thead", {}, el("tr", {}, columns.map((c) => el("th", { scope: "col", class: c.align === "right" ? "num" : null }, c.label))));
  const body = el(
    "tbody",
    {},
    rows.map((row) =>
      el(
        "tr",
        {},
        columns.map((c) => {
          const raw = row[c.key];
          const value = c.format ? c.format(raw, row) : dash(raw);
          return el("td", { class: c.align === "right" ? "num" : null }, value);
        }),
      ),
    ),
  );
  return el("div", { class: "table-wrap" }, el("table", {}, caption ? el("caption", {}, caption) : null, head, body));
}

/** What a screen shows when there is nothing yet: a title, a sentence, and maybe one action. */
export function emptyState(title, text, action) {
  return el("div", { class: "empty", role: "status" }, el("p", { class: "empty-title" }, title), text ? el("p", {}, text) : null, action || null);
}

/** A button. `kind` is "primary", "secondary", "danger" or "ghost". */
export function button(label, onClick, { kind = "secondary", type = "button", disabled = false, ariaLabel } = {}) {
  return el("button", { type, class: ["btn", `btn-${kind}`], onclick: onClick, disabled, "aria-label": ariaLabel || null }, label);
}

/** A loading line for a screen that is waiting on the API. */
export const loading = (text = "Loading…") => el("p", { class: "loading", role: "status" }, text);

/** A plain-language error box, with the server's message when there is one. */
export function errorBox(title, err) {
  const detail = err && err.message ? err.message : null;
  return el("div", { class: "error-box", role: "alert" }, el("p", { class: "empty-title" }, title), detail ? el("p", {}, detail) : null);
}

/**
 * A short message in the corner that also reaches screen readers (polite live region; "critical" is assertive).
 * Disappears after a few seconds; the close button dismisses it sooner.
 */
export function toast(message, tone = "info") {
  let host = document.getElementById("toasts");
  if (!host) {
    host = el("div", { id: "toasts", class: "toasts" });
    document.body.append(host);
  }
  const item = el(
    "div",
    { class: ["toast", `toast-${TONES.has(tone) ? tone : "info"}`], role: tone === "critical" ? "alert" : "status" },
    el("span", {}, message),
    el("button", { type: "button", class: "toast-close", "aria-label": "Dismiss", onclick: () => item.remove() }, "×"),
  );
  host.append(item);
  setTimeout(() => item.remove(), tone === "critical" ? 9000 : 5000);
  return item;
}

/** A body with `{{slot}}` placeholders, with each slot drawn as a marked chip so the marketer sees what varies. */
export function slotted(text) {
  const parts = String(text || "").split(/(\{\{\s*[a-zA-Z0-9_]+\s*\}\})/g);
  return parts.filter((p) => p !== "").map((p) => {
    const m = p.match(/^\{\{\s*([a-zA-Z0-9_]+)\s*\}\}$/);
    return m ? el("span", { class: "slot", title: "Filled in for each customer" }, humanize(m[1]).toLowerCase()) : p;
  });
}
