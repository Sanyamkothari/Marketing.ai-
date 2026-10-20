// Small helpers every Autonomy screen shares. Copied in spirit from v1 (ui/dom.js, ui/components.js), not
// imported: v2 stands alone. One rule runs through all of them: a value nobody sent is an em dash, never a
// made-up number, and every estimate is shown as its range, never as a bare point.
//
// They draw the design system in styles.css (docs/v2/DESIGN.md; "How to use the system" at its end lists the
// classes): cards, the divided KPI strip, tables that fold to two-line rows on a phone, pills, notices, the
// segmented control, skeleton blocks, empty and error states, the freshness stamp, toasts and dialogs.

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
    else if (key === "style" && typeof value === "object") setStyle(node, value);
    else if (key === "dataset" && typeof value === "object") Object.assign(node.dataset, value);
    else if (key.startsWith("on") && typeof value === "function") node.addEventListener(key.slice(2).toLowerCase(), value);
    else if (value === true) node.setAttribute(key, "");
    else node.setAttribute(key, String(value));
  }
  append(node, children);
  return node;
}

/** Set inline styles; custom properties (`--kpi-count`) need `setProperty`, plain ones are assigned. */
function setStyle(node, styles) {
  for (const [name, v] of Object.entries(styles)) {
    if (name.startsWith("--") && node.style && typeof node.style.setProperty === "function") node.style.setProperty(name, String(v));
    else node.style[name] = v;
  }
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
/** DESIGN.md's names for the same tones. */
const TONE_ALIASES = Object.freeze({ positive: "good", negative: "critical", accent: "info" });
const toneOf = (tone, fallback = "neutral") => {
  const t = TONE_ALIASES[tone] || tone;
  return TONES.has(t) ? t : fallback;
};

/**
 * A small status label: pale tint, dark ink of the same hue. Colour is never the only signal: the word is always
 * there. Tones: good (positive), warning, critical (negative), info (accent), neutral. `{dot: true}` adds a 6px dot.
 */
export function pill(text, tone = "neutral", { dot = false } = {}) {
  return el("span", { class: ["pill", `pill-${toneOf(tone)}`, dot && "pill-dot"] }, text);
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

/**
 * A card: optional title (with a heading level), body children and an actions row. `meta` (the card's own period
 * or a freshness stamp) sits right of the title in grey; `flush: true` lets a table or list run edge to edge.
 */
export function card({ title, level = 3, body = [], actions = [], className = "", label, meta, flush = false } = {}) {
  let heading = title ? el(`h${level}`, { class: "card-title" }, title) : null;
  if (heading && present(meta)) heading = el("div", { class: "card-head" }, heading, el("span", { class: "card-meta" }, meta));
  const actionRow = actions.length ? el("div", { class: "card-actions" }, actions) : null;
  return el("section", { class: ["card", flush && "card-flush", className], "aria-label": label || null }, heading, body, actionRow);
}

/**
 * A table with a caption. `columns` is `[{key, label, format?, align?, lead?}]`; `rows` are objects. A missing
 * cell is the em dash. Wrapped in a scroller so it never widens the page on a phone, and (unless `stack: false`)
 * folded into two-line rows under 640px: the first column and the `lead` column (the key figure) on line 1, the others in
 * grey on line 2, each with its header as a label. `empty` is the sentence for a table with no rows.
 */
export function table({ caption, columns, rows, stack = true, empty } = {}) {
  const head = el("thead", {}, el("tr", {}, columns.map((c) => el("th", { scope: "col", class: c.align === "right" ? "num" : null }, c.label))));
  const list = Array.isArray(rows) ? rows : [];
  const body = el(
    "tbody",
    {},
    !list.length && present(empty)
      ? el("tr", {}, el("td", { colspan: String(columns.length), class: "muted" }, empty))
      : list.map((row) =>
          el(
            "tr",
            {},
            columns.map((c) => {
              const raw = row[c.key];
              const value = c.format ? c.format(raw, row) : dash(raw);
              const label = typeof c.label === "string" ? c.label : null;
              return el("td", { class: [c.align === "right" && "num", c.lead && "is-key"], "data-label": label }, value);
            }),
          ),
        ),
  );
  return el("div", { class: "table-wrap" }, el("table", { class: stack ? "table-stack" : null }, caption ? el("caption", {}, caption) : null, head, body));
}

/** What a screen shows when there is nothing yet: a title, a sentence, and maybe one action. */
export function emptyState(title, text, action) {
  return el("div", { class: "empty", role: "status" }, el("p", { class: "empty-title" }, title), text ? el("p", {}, text) : null, action || null);
}

/**
 * A button. `kind` is "primary" (near-black; one per screen), "secondary", "ghost" or "danger" (also spelled
 * "destructive": red text and hairline). `size` is "sm" or "lg"; `block: true` spans the width.
 */
export function button(label, onClick, { kind = "secondary", type = "button", disabled = false, ariaLabel, size, block = false } = {}) {
  const k = kind === "destructive" ? "danger" : kind;
  return el(
    "button",
    { type, class: ["btn", `btn-${k}`, size && `btn-${size}`, block && "btn-block"], onclick: onClick, disabled, "aria-label": ariaLabel || null },
    label,
  );
}

/** A loading line for a screen that is waiting on the API. */
export const loading = (text = "Loading…") => el("p", { class: "loading", role: "status" }, text);

/**
 * A plain-language error box for the block that failed, with the server's message when there is one. Options:
 * `lastGood` (an ISO time: "Last updated 20 Oct 2026, 09:00") and `retry` (a function: a Retry text button).
 */
export function errorBox(title, err, { lastGood, retry } = {}) {
  const detail = err && err.message ? err.message : null;
  const footer =
    present(lastGood) || typeof retry === "function"
      ? el(
          "div",
          { class: "error-actions" },
          present(lastGood) ? el("span", { class: "meta" }, `Last updated ${when(lastGood)}`) : null,
          typeof retry === "function" ? el("button", { type: "button", class: "link-btn", onclick: retry }, "Retry") : null,
        )
      : null;
  return el("div", { class: "error-box", role: "alert" }, el("p", { class: "empty-title" }, title), detail ? el("p", {}, detail) : null, footer);
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

// ---- the design system's newer pieces (DESIGN.md, "How to use the system") ---------------------------------------

/**
 * The title row: an h1 (a noun: "Live", "Proof"), an optional grey line about the data, and on the right the
 * real actions and/or a freshness stamp. Returns a `.page-head` block.
 */
export function pageHead({ title, sub, actions = [], stamp = null, level = 1 } = {}) {
  const left = el("div", {}, el(`h${level}`, { class: "page-title" }, title), present(sub) ? el("p", { class: "page-sub" }, sub) : null);
  const right = [stamp, ...[actions].flat()].filter(Boolean);
  return el("div", { class: "page-head" }, left, right.length ? el("div", { class: "page-actions" }, right) : null);
}

/**
 * The divided KPI strip: one container split by hairlines. `items` is `[{label, value, range?, note?}]`, where
 * `value` and `range` are already formatted strings (money with `range()`, so it always carries its range). A
 * missing value is the grey em dash, never a made-up number. Four items get four columns; a phone gets two.
 */
export function kpiStrip(items = [], { label } = {}) {
  const list = items.filter(Boolean);
  return el(
    "div",
    { class: "kpis", role: "group", "aria-label": label || null, style: { "--kpi-count": String(Math.max(1, list.length)) } },
    list.map((k) => {
      const shown = present(k.value) && k.value !== EM_DASH;
      return el(
        "div",
        { class: "kpi" },
        el("p", { class: "kpi-label" }, k.label),
        el("p", { class: ["kpi-value", !shown && "is-missing"] }, shown ? k.value : EM_DASH),
        present(k.range) ? el("p", { class: "kpi-range tabular" }, k.range) : null,
        present(k.note) ? el("p", { class: "kpi-note" }, k.note) : null,
      );
    }),
  );
}

/**
 * A quiet notice (the product's honest status, for example "Not proven on your business yet: start in Shadow"):
 * pale amber with dark amber text. `tone: "neutral"` is a grey aside instead.
 */
export function notice(children, { tone = "warning", role = null } = {}) {
  return el("div", { class: ["notice", tone === "neutral" && "notice-neutral"], role }, children);
}

/**
 * A grey block at the real size of what is loading (no shimmer, nothing shifts when the data lands). `width` and
 * `height` are CSS lengths; the default is one line of body text across the block.
 */
export function skeleton({ width = "100%", height = "20px", inline = false, className = "" } = {}) {
  return el("span", { class: ["skeleton", inline && "skeleton-inline", className], "aria-hidden": "true", style: { width, height } });
}

/** `n` skeleton lines (the last one shorter), with a screen-reader "Loading" for the block. */
export function skeletonLines(n = 3, { label = "Loading…" } = {}) {
  const lines = Array.from({ length: Math.max(1, n) }, (_, i) => skeleton({ width: i === n - 1 && n > 1 ? "60%" : "100%" }));
  return el("div", { class: "skeleton-stack", role: "status" }, el("span", { class: "visually-hidden" }, label), lines);
}

/** A KPI strip whose labels are known while the values load: each value is a grey block at the value's size. */
export function skeletonKpis(labels = []) {
  return el(
    "div",
    { class: "kpis", role: "status", "aria-label": "Loading…", "aria-busy": "true", style: { "--kpi-count": String(Math.max(1, labels.length)) } },
    labels.map((label) =>
      el("div", { class: "kpi" }, el("p", { class: "kpi-label" }, label), el("p", { class: "kpi-value" }, skeleton({ width: "96px", height: "28px", inline: true }))),
    ),
  );
}

/**
 * The quiet freshness stamp: "Updated 20 Oct 2026, 09:00", with an optional Refresh text button. Nothing (an
 * empty string) when the time is unknown, so no made-up time is shown.
 */
export function freshness(iso, { label = "Updated", onRefresh } = {}) {
  const at = when(iso);
  if (at === EM_DASH && typeof onRefresh !== "function") return "";
  return el(
    "span",
    { class: "stamp" },
    at === EM_DASH ? null : el("span", {}, `${label} ${at}`),
    typeof onRefresh === "function" ? el("button", { type: "button", class: "link-btn", onclick: onRefresh }, "Refresh") : null,
  );
}

let segmentedSeq = 0;

/**
 * A segmented control (the autonomy dial: Shadow | Approve | Autopilot): radios in one rounded track, so arrow keys
 * move the choice. `options` is `[{value, label, disabled?}]`; `onChange(value)` runs on a choice; `note` is the
 * one sentence under it that explains the choice (pass a node to update it yourself).
 */
export function segmented({ name, legend, options = [], value, onChange, note, disabled = false } = {}) {
  const group = name || `seg-${(segmentedSeq += 1)}`;
  const track = el(
    "fieldset",
    { class: "segmented", disabled },
    present(legend) ? el("legend", {}, legend) : null,
    options.map((o, i) => {
      const id = `${group}-${i}`;
      return [
        el("input", {
          type: "radio",
          id,
          name: group,
          value: String(o.value),
          checked: o.value === value,
          disabled: Boolean(o.disabled),
          onchange: (e) => e.target.checked && typeof onChange === "function" && onChange(o.value),
        }),
        el("label", { for: id }, o.label),
      ];
    }),
  );
  if (!present(note)) return track;
  return el("div", { class: "segmented-field" }, track, el("p", { class: "segmented-note" }, note));
}

/**
 * A dialog (one of the two things with a shadow). Returns the `<dialog>`; call `.showModal()` to open it. Each
 * action is a node (use `button()`); a button with `value` inside a `<form method="dialog">` closes it.
 */
export function dialog({ title, body = [], actions = [], label } = {}) {
  const node = el(
    "dialog",
    { class: "dialog", "aria-label": label || (typeof title === "string" ? title : null) },
    title ? el("h2", { class: "dialog-title" }, title) : null,
    body,
    actions.length ? el("div", { class: "dialog-actions" }, actions) : null,
  );
  node.addEventListener("close", () => node.remove());
  return node;
}
