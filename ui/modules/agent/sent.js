// "What the AI looked at" (Guided setup): the words under a helper reply that show, item by item, what
// was sent to the AI service to write it, and the one-line status under the chat input that says what
// the AI may look at.
//
// The API's `sent` items are `{ tool, args, preview, chars, mode }` (`mode`: masked_data | summaries_only).
// `preview` is the masked payload itself, so it is content from the person's file: it is shown as text
// only (escaped, in a scrollable block), never as markup. Both `sent` and the session's `chat.data_access`
// / `chat.third_party` may be missing on an older API: no `sent` is an empty list, no `data_access` means
// nothing is claimed. Every string shown first loses the characters that hide or reorder text (bidi
// overrides, zero-width and control characters), so what the person reads is what was sent; at most
// `MAX_ITEMS` items are drawn (the API keeps 12 per reply) and the rest are counted.

import { esc } from "../../dom.js";

/** The tools' plain phrases; a tool not listed here is shown by its own name. */
export const TOOL_PHRASES = {
  sample_rows: "Looked at some rows",
  value_counts: "Counted the values of a column",
  find_values: "Searched a column",
  describe_numbers: "Checked the numbers in a column",
  describe_dates: "Checked the dates in a column",
  compare_columns: "Compared two columns",
  describe_missing: "Checked what is empty",
  describe_duplicates: "Checked for repeats",
  inspect_column: "Looked at a column",
  get_profile: "Read the file's overview",
  find_format_issues: "Looked for messy formats",
  describe_outcome: "Checked the outcome column",
  find_roles: "Looked for the ID and outcome columns",
  describe_repeats: "Checked how often an ID repeats",
  check_data: "Checked the data against the run's rules",
  advisor_state: "Read the helper's own suggestions and questions",
};

/** Argument names that hold a column name (or a list of them). */
const COLUMN_KEYS = ["column", "columns", "left", "right", "other", "column_a", "column_b", "a", "b", "primary_key", "target"];

/** Items drawn per reply; more are counted, never drawn. */
export const MAX_ITEMS = 12;
/** Column names listed per item; more are counted. */
const MAX_COLUMNS = 8;

// Personal details are found by pattern (e-mail, phone, card and ID numbers, names in personal columns);
// a name inside a sentence or another country's ID number can slip past, so the copy says "recognise".
export const SENT_FOOTER = "Personal details that our checks recognise are hidden before anything is sent.";
export const STATUS_MASKED = "The AI can look at your data, with the personal details our checks recognise hidden.";
export const STATUS_SUMMARIES = "The AI sees only summaries of your data, never values.";
export const STATUS_THIRD_PARTY = "This AI service is run by another company.";

// Bidi controls and marks (U+200E/F, U+202A-E, U+2066-9), zero-width and joiner characters, U+2028/9,
// the BOM, the Arabic letter mark, and control characters other than tab and newline.
const INVISIBLE = /[\u200B-\u200F\u202A-\u202E\u2060-\u2064\u2066-\u2069\u2028\u2029\uFEFF\u061C\u0000-\u0008\u000B\u000C\u000E-\u001F\u007F]/g;

/** `value` as text, without the characters that hide or reorder what a person reads. */
export const plain = (value) => String(value ?? "").replace(INVISIBLE, "");

export const toolPhrase = (tool) => {
  const name = plain(tool);
  return Object.hasOwn(TOOL_PHRASES, name) ? TOOL_PHRASES[name] : name;
};

/** The column names an item's args point at, in order, without repeats. */
export function columnsOf(args) {
  const found = [];
  const source = args && typeof args === "object" ? args : {};
  for (const key of COLUMN_KEYS) {
    const value = source[key];
    for (const name of Array.isArray(value) ? value : [value]) {
      const shown = typeof name === "string" ? plain(name) : "";
      if (shown && !found.includes(shown)) found.push(shown);
    }
  }
  return found;
}

function itemHtml(item) {
  const meta = [];
  const columns = columnsOf(item.args);
  if (columns.length) {
    const more = columns.length > MAX_COLUMNS ? ` and ${columns.length - MAX_COLUMNS} more` : "";
    meta.push(`${columns.length > 1 ? "Columns" : "Column"}: ${columns.slice(0, MAX_COLUMNS).join(", ")}${more}`);
  }
  if (Number.isFinite(item.chars)) meta.push(`${item.chars.toLocaleString("en-US")} characters sent`);
  const preview =
    typeof item.preview === "string" && item.preview
      ? `<pre class="ag-sent-pre" role="region" tabindex="0" aria-label="What was sent">${esc(plain(item.preview))}</pre>`
      : "";
  return `<li class="ag-sent-item" data-ag-sent-item="${esc(plain(item.tool))}"><span class="ag-sent-tool">${esc(
    toolPhrase(item.tool),
  )}</span> <span class="ag-sent-meta">${esc(meta.join(" · "))}</span>${preview}</li>`;
}

/** The disclosure under a helper reply; nothing when the reply sent nothing. Collapsed unless `open`;
 * `id` (the message's place in the transcript) lets a redraw of the page keep an opened one open. */
export function sentHtml(sent, { id = "", open = false } = {}) {
  const items = Array.isArray(sent) ? sent.filter((item) => item && typeof item === "object") : [];
  if (!items.length) return "";
  const rest = items.length - MAX_ITEMS;
  const more = rest > 0 ? `<li class="ag-sent-more">${esc(`+${rest} more`)}</li>` : "";
  return `<details class="ag-sent" data-ag-sent="${esc(String(id))}"${open ? " open" : ""}><summary>${esc(
    `What the AI looked at (${items.length})`,
  )}</summary><ul class="ag-sent-list">${items.slice(0, MAX_ITEMS).map(itemHtml).join("")}${more}</ul><p class="ag-sent-foot">${esc(
    SENT_FOOTER,
  )}</p></details>`;
}

/** The line under the chat input: what the AI may look at, and whose service it is. Nothing for the
 * practice service, and nothing claimed when the API does not say. */
export function statusHtml(chat) {
  if (!chat || chat.backend === "fake") return "";
  const parts = [];
  if (chat.data_access === "masked_data") parts.push(esc(STATUS_MASKED));
  else if (chat.data_access === "summaries_only") parts.push(esc(STATUS_SUMMARIES));
  if (chat.third_party === true) {
    parts.push(`${esc(STATUS_THIRD_PARTY)} <a href="#/connections">Change on Connections</a>`);
  }
  return parts.length ? `<p class="ag-access" data-ag-access>${parts.join(" ")}</p>` : "";
}
