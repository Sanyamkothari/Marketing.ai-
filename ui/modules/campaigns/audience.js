// The Audience tab's data (Plan M M176, DEC-1606): the campaign's contact list, read from the file a sending tool
// is given (`GET /runs/{id}/treat_list.csv`, Plan J M98), searched and shown 25 rows at a time, and "Why these
// customers?" counted from the reasons already in that file (`reason_1`, the strongest reason per customer, in the
// words `configs/decide/reasons.yaml` gives it, else the model's own short text).
//
// The contact list is the rows the file marks to contact (`treat` is 1): the same customers, in the same order, a
// sending tool would contact. Nothing is ranked, re-scored or worked out again here. Every function is pure.

export const PAGE_SIZE = 25;

/** RFC 4180 CSV text to an array of rows (arrays of strings): quoted fields, doubled quotes, CRLF or LF. */
export function parseCsv(text) {
  const rows = [];
  let row = [];
  let field = "";
  let quoted = false;
  const s = String(text || "").replace(/^﻿/, "");
  for (let i = 0; i < s.length; i += 1) {
    const c = s[i];
    if (quoted) {
      if (c === '"') {
        if (s[i + 1] === '"') {
          field += '"';
          i += 1;
        } else quoted = false;
      } else field += c;
    } else if (c === '"') quoted = true;
    else if (c === ",") {
      row.push(field);
      field = "";
    } else if (c === "\n" || c === "\r") {
      if (c === "\r" && s[i + 1] === "\n") i += 1;
      row.push(field);
      rows.push(row);
      row = [];
      field = "";
    } else field += c;
  }
  if (field !== "" || row.length) {
    row.push(field);
    rows.push(row);
  }
  return rows;
}

/** The key columns of a campaign's `primary_key` (one name, or several). */
export const keyColumns = (primaryKey) => [].concat(primaryKey || []).filter(Boolean);

/**
 * The contact list from the CSV text: `{ columns, rows, total, grouping }`, where `rows` are the customers to contact
 * as `{ key, priority, offer, channel, reason, reasons }` and `total` counts every row of the file. `key` is the
 * customer id (several key columns joined by " · "); `priority` is the row's band (or, on a campaign-effect list,
 * its group, in words), and `grouping` says which ("band" or "segment"); `reasons` are the row's reasons, strongest
 * first.
 */
export function contactList(text, primaryKey) {
  const table = parseCsv(text);
  if (!table.length) return { columns: [], rows: [], total: 0, grouping: "band" };
  const [head, ...body] = table;
  const at = (name) => head.indexOf(name);
  const keys = keyColumns(primaryKey)
    .map(at)
    .filter((i) => i >= 0);
  const keyIdx = keys.length ? keys : [0];
  const treat = at("treat");
  const band = at("band") >= 0 ? at("band") : at("segment");
  const offer = at("offer");
  const channel = at("channel");
  const reasonIdx = ["reason_1", "reason_2", "reason_3"].map(at).filter((i) => i >= 0);
  const rows = [];
  let total = 0;
  for (const cells of body) {
    if (cells.length === 1 && cells[0] === "") continue; // a trailing empty line
    total += 1;
    if (treat >= 0 && cells[treat] !== "1") continue;
    const reasons = reasonIdx.map((i) => cells[i] || "").filter(Boolean);
    rows.push({
      key: keyIdx.map((i) => cells[i] || "").join(" · "),
      priority: band >= 0 ? priorityWords(cells[band]) : "",
      offer: offer >= 0 ? cells[offer] || "" : "",
      channel: channel >= 0 ? cells[channel] || "" : "",
      reason: reasons[0] || "",
      reasons,
    });
  }
  return { columns: head, rows, total, grouping: at("band") >= 0 ? "band" : "segment" };
}

const GROUP_WORDS = {
  persuadable: "Responds to contact",
  sure_thing: "Buys anyway",
  lost_cause: "Unlikely to respond",
  sleeping_dog: "Put off by contact",
};

/** A band (High, Medium, Low) as it is; a campaign-effect group in words; "" for none. */
export function priorityWords(value) {
  const v = String(value || "").trim();
  if (!v) return "";
  return GROUP_WORDS[v.toLowerCase()] || v;
}

/** The rows whose customer id contains `query` (case-insensitive, surrounding spaces ignored); all for "". */
export function search(rows, query) {
  const q = String(query || "")
    .trim()
    .toLowerCase();
  if (!q) return rows;
  return rows.filter((row) => row.key.toLowerCase().includes(q));
}

/**
 * One page of `rows`: `{ rows, page, pages, from, to, total }`, where `page` is 1-based and clamped to the pages
 * there are, and `from`/`to` are the 1-based positions shown (0 and 0 when there is nothing).
 */
export function pageOf(rows, page = 1, size = PAGE_SIZE) {
  const total = rows.length;
  const pages = Math.max(1, Math.ceil(total / size));
  const current = Math.min(Math.max(1, Math.floor(Number(page) || 1)), pages);
  const start = (current - 1) * size;
  const shown = rows.slice(start, start + size);
  return { rows: shown, page: current, pages, from: shown.length ? start + 1 : 0, to: start + shown.length, total };
}

// --- "Why these customers?" -----------------------------------------------------------------------------------

const MODEL_TEXT = /^(.+?)\s*([↑↓])\s*\((.*)\)\s*$/u;
const MODEL_EQUALS = /^([^=]+?)\s*=\s*(.+)$/;
const NUMBER = /-?\d[\d,]*(?:\.\d+)?/g;

/** A column name in words: `history_segment` -> "history segment". */
export const columnWords = (name) =>
  String(name || "")
    .replace(/[_]+/g, " ")
    .trim();

/**
 * What a reason is about, so customers with the same reason count together whatever their own value: `{ id, text }`.
 * The model's own short texts are put in words ("history ↓ (142.44)" -> "Their history lowers their priority";
 * "zip_code = Urban" -> "Their zip code is Urban"); a sentence from the reasons dictionary keeps its words, with the
 * customer's own number left out.
 */
export function reasonTopic(reason) {
  const text = String(reason || "").trim();
  if (!text) return null;
  const arrow = MODEL_TEXT.exec(text);
  if (arrow) {
    const what = columnWords(arrow[1]);
    const up = arrow[2] === "↑";
    return { id: `${arrow[1]}|${arrow[2]}`, text: `Their ${what} ${up ? "raises" : "lowers"} their priority` };
  }
  const equals = MODEL_EQUALS.exec(text);
  if (equals && !/\s{2,}/.test(equals[1]) && equals[1].split(/\s+/).length <= 3) {
    return { id: `${equals[1]}=${equals[2]}`, text: `Their ${columnWords(equals[1])} is ${equals[2].trim()}` };
  }
  const general = text.replace(NUMBER, "…");
  return { id: general, text: general };
}

/**
 * The top three reasons among the customers to contact: each `{ text, customers, share }`, most customers first,
 * counted on each customer's strongest reason (`reason_1`). `share` is a fraction of the customers to contact.
 */
export function topReasons(rows, limit = 3) {
  const counts = new Map();
  for (const row of rows) {
    const topic = reasonTopic(row.reason);
    if (!topic) continue;
    const seen = counts.get(topic.id) || { text: topic.text, customers: 0 };
    seen.customers += 1;
    counts.set(topic.id, seen);
  }
  const all = rows.length || 1;
  return [...counts.values()]
    .sort((a, b) => b.customers - a.customers || a.text.localeCompare(b.text))
    .slice(0, limit)
    .map((entry) => ({ ...entry, share: entry.customers / all }));
}
