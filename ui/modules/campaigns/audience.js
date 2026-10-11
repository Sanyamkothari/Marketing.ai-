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

const QUOTE = 34;
const COMMA = 44;
const LF = 10;
const CR = 13;

/**
 * The contact list, read in ONE pass over the CSV, as it arrives: `push(text)` any number of pieces (a piece may
 * end anywhere, even inside a quoted field or between CR and LF), then `end()` answers
 * `{ columns, rows, total, grouping }` as `contactList` does.
 *
 * No table of the file is ever built: only the header's names, and for each row the few cells the list shows (the
 * key, band or segment, offer, channel, reasons 1 to 3, and `treat`), are kept as strings; every other cell is
 * skipped without being copied. A row the file does not mark to contact (`treat` is not 1) is counted and dropped.
 * The parsing is RFC 4180 as `parseCsv`'s (quoted fields, doubled quotes, CRLF or LF; an empty line is skipped).
 */
export class ContactListReader {
  constructor(primaryKey) {
    this.primaryKey = primaryKey;
    this.first = true; // a byte-order mark is dropped from the first piece only
    this.quoted = false;
    this.quotePending = false; // a piece ended on a quote inside a quoted field: closing or doubled?
    this.cr = false; // a piece ended on CR: a LF opening the next one belongs to it
    this.field = "";
    this.width = 0; // characters in the current field, kept or not
    this.firstWidth = 0; // ... in the record's first field (an empty line is one empty field)
    this.col = 0;
    this.keep = true;
    this.header = null;
    this.cells = [];
    this.want = null; // column index -> slot of `cells`, or -1
    this.rows = [];
    this.total = 0;
  }

  take(s, from, to) {
    if (to <= from) return;
    this.width += to - from;
    if (this.keep) this.field += s.slice(from, to);
  }

  endField() {
    if (this.col === 0) this.firstWidth = this.width;
    if (this.header === null) this.cells.push(this.field);
    else if (this.keep) this.cells[this.want[this.col]] = this.field;
    this.col += 1;
    this.field = "";
    this.width = 0;
    this.keep = this.header === null || (this.col < this.want.length && this.want[this.col] >= 0);
  }

  endRecord() {
    const empty = this.col === 1 && this.firstWidth === 0;
    if (!empty) {
      if (this.header === null) this.start(this.cells);
      else this.row();
    } else if (this.header === null) this.cells = [];
    this.col = 0;
    if (this.header !== null) this.cells.fill("");
    this.keep = this.header === null || (this.want.length > 0 && this.want[0] >= 0);
  }

  /** The header is in: decide which columns are kept, and in which slot. */
  start(head) {
    this.header = head;
    const at = (name) => head.indexOf(name);
    const slots = [];
    const slotOf = (index) => {
      if (index < 0) return -1;
      let slot = slots.indexOf(index);
      if (slot < 0) {
        slot = slots.length;
        slots.push(index);
      }
      return slot;
    };
    const keys = keyColumns(this.primaryKey)
      .map(at)
      .filter((i) => i >= 0);
    this.keySlots = (keys.length ? keys : [0]).map(slotOf);
    this.treatSlot = slotOf(at("treat"));
    this.grouping = at("band") >= 0 ? "band" : "segment";
    this.bandSlot = slotOf(at("band") >= 0 ? at("band") : at("segment"));
    this.offerSlot = slotOf(at("offer"));
    this.channelSlot = slotOf(at("channel"));
    this.reasonSlots = ["reason_1", "reason_2", "reason_3"]
      .map(at)
      .filter((i) => i >= 0)
      .map(slotOf);
    this.want = new Int32Array(head.length).fill(-1);
    slots.forEach((index, slot) => {
      this.want[index] = slot;
    });
    this.cells = new Array(slots.length).fill("");
  }

  row() {
    const cells = this.cells;
    this.total += 1;
    if (this.treatSlot >= 0 && cells[this.treatSlot] !== "1") return;
    const reasons = [];
    for (const slot of this.reasonSlots) if (cells[slot]) reasons.push(cells[slot]);
    this.rows.push({
      key: this.keySlots.map((slot) => cells[slot] || "").join(" · "),
      priority: this.bandSlot >= 0 ? priorityWords(cells[this.bandSlot]) : "",
      offer: this.offerSlot >= 0 ? cells[this.offerSlot] || "" : "",
      channel: this.channelSlot >= 0 ? cells[this.channelSlot] || "" : "",
      reason: reasons[0] || "",
      reasons,
    });
  }

  /**
   * One whole line with no CR in it, read with native searches: true when it was read, false (nothing changed) when
   * it is not a plain record - a quote that does not open and close a whole field, or a quoted line end - which the
   * character-by-character reading then takes.
   */
  line(text) {
    if (!text) return true; // an empty line
    const all = this.header === null;
    const want = this.want;
    const got = all ? [] : this.cells.slice(0, 0);
    const slots = all ? null : [];
    const n = text.length;
    let pos = 0;
    let col = 0;
    let firstWidth = 0;
    let quoteAt = text.indexOf('"'); // the next quote at or after `pos` (-1: none), searched for once each
    for (;;) {
      const keep = all || (col < want.length && want[col] >= 0);
      let value = "";
      let width = 0;
      let next;
      if (text.charCodeAt(pos) === QUOTE) {
        let from = pos + 1;
        for (;;) {
          const q = text.indexOf('"', from);
          if (q < 0) return false; // the quotes go on past this line
          width += q - from;
          if (text.charCodeAt(q + 1) === QUOTE) {
            if (keep) value += text.slice(from, q + 1);
            width += 1;
            from = q + 2;
            continue;
          }
          if (keep) value += text.slice(from, q);
          next = q + 1;
          break;
        }
        if (next < n && text.charCodeAt(next) !== COMMA) return false; // text after the closing quote
      } else {
        const comma = text.indexOf(",", pos);
        next = comma < 0 ? n : comma;
        if (quoteAt >= 0 && quoteAt < pos) quoteAt = text.indexOf('"', pos);
        if (quoteAt >= 0 && quoteAt < next) return false; // a quote inside a field
        if (keep) value = text.slice(pos, next);
        width = next - pos;
      }
      if (col === 0) firstWidth = width;
      if (all) got.push(value);
      else if (keep) {
        got.push(value);
        slots.push(want[col]);
      }
      col += 1;
      if (next >= n) break;
      pos = next + 1;
      if (pos === n) {
        // a line that ends on a comma: one more, empty, field
        if (all) got.push("");
        else if (col < want.length && want[col] >= 0) {
          got.push("");
          slots.push(want[col]);
        }
        col += 1;
        break;
      }
    }
    if (col === 1 && firstWidth === 0) return true; // a line holding one empty field is an empty line
    if (all) {
      this.start(got);
      return true;
    }
    const cells = this.cells;
    for (let k = 0; k < got.length; k += 1) cells[slots[k]] = got[k];
    this.row();
    cells.fill("");
    return true;
  }

  push(piece) {
    let s = String(piece || "");
    if (this.first && s) {
      this.first = false;
      if (s.charCodeAt(0) === 0xfeff) s = s.slice(1);
    }
    const n = s.length;
    if (!n) return this;
    let i = 0;
    if (this.cr) {
      this.cr = false;
      if (s.charCodeAt(0) === LF) i = 1;
    }
    if (this.quotePending) {
      this.quotePending = false;
      if (s.charCodeAt(0) === QUOTE) {
        this.take('"', 0, 1); // a doubled quote: one quote, still inside the quotes
        i = 1;
      } else this.quoted = false;
    }
    while (i < n) {
      if (!this.quoted && this.col === 0 && this.width === 0) {
        // The common case, at the start of a record: a whole line with no quote and no CR is split natively.
        const lf = s.indexOf("\n", i);
        if (lf >= 0) {
          const line = s.slice(i, lf);
          if (line.indexOf("\r") < 0 && this.line(line)) {
            i = lf + 1;
            continue;
          }
        }
      }
      if (this.quoted) {
        const q = s.indexOf('"', i);
        if (q < 0) {
          this.take(s, i, n);
          break;
        }
        this.take(s, i, q);
        if (q + 1 >= n) {
          this.quotePending = true;
          break;
        }
        if (s.charCodeAt(q + 1) === QUOTE) {
          this.take(s, q, q + 1);
          i = q + 2;
        } else {
          this.quoted = false;
          i = q + 1;
        }
        continue;
      }
      let j = i;
      let c = 0;
      while (j < n) {
        c = s.charCodeAt(j);
        if (c === COMMA || c === LF || c === CR || c === QUOTE) break;
        j += 1;
      }
      this.take(s, i, j);
      if (j >= n) break;
      if (c === QUOTE) {
        this.quoted = true;
        i = j + 1;
      } else if (c === COMMA) {
        this.endField();
        i = j + 1;
      } else {
        this.endField();
        this.endRecord();
        if (c === CR && j + 1 >= n) {
          this.cr = true;
          i = n;
        } else i = c === CR && s.charCodeAt(j + 1) === LF ? j + 2 : j + 1;
      }
    }
    return this;
  }

  /** The last record (one with no line end), then the list. */
  end() {
    if (this.quotePending) {
      this.quotePending = false;
      this.quoted = false;
    }
    if (this.width > 0 || this.col > 0) {
      this.endField();
      this.endRecord();
    }
    if (this.header === null) return { columns: [], rows: [], total: 0, grouping: "band" };
    return { columns: this.header, rows: this.rows, total: this.total, grouping: this.grouping };
  }
}

/**
 * The contact list from the CSV text: `{ columns, rows, total, grouping }`, where `rows` are the customers to contact
 * as `{ key, priority, offer, channel, reason, reasons }` and `total` counts every row of the file. `key` is the
 * customer id (several key columns joined by " · "); `priority` is the row's band (or, on a campaign-effect list,
 * its group, in words), and `grouping` says which ("band" or "segment"); `reasons` are the row's reasons, strongest
 * first. One pass, keeping only those cells (`ContactListReader`).
 */
export function contactList(text, primaryKey) {
  return new ContactListReader(primaryKey).push(text).end();
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
