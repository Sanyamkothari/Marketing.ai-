// Advanced settings, generated entirely from `GET /use-cases/{id}.advanced_settings`.
//
// Nothing in this file knows the name of a single setting: stages, fields, widgets, choices, the
// bounds of every number and the per-stage summary line all come from the schema, so a new setting
// added to the engine config appears on the screen without a change here (plan §9.2).

import { esc } from "./dom.js";

// --- paths -----------------------------------------------------------------------------------
// A field's `path` is the dotted config path the API also accepts as an override key, including
// array indexes: `actions.bands[0].min_score`.

function tokens(path) {
  const out = [];
  for (const part of String(path).split(".")) {
    const match = /^([^[\]]+)((?:\[\d+\])*)$/.exec(part);
    if (!match) {
      out.push(part);
      continue;
    }
    out.push(match[1]);
    for (const index of match[2].match(/\d+/g) || []) out.push(Number(index));
  }
  return out;
}

export function readPath(root, path) {
  let node = root;
  for (const key of tokens(path)) {
    if (node === null || node === undefined) return undefined;
    node = node[key];
  }
  return node;
}

export function writePath(root, path, value) {
  const keys = tokens(path);
  let node = root;
  for (let i = 0; i < keys.length - 1; i += 1) {
    const key = keys[i];
    if (node[key] === null || typeof node[key] !== "object") {
      node[key] = typeof keys[i + 1] === "number" ? [] : {};
    }
    node = node[key];
  }
  node[keys[keys.length - 1]] = value;
  return root;
}

// --- the schema ------------------------------------------------------------------------------

/** Every field of every stage, indexed by path, plus the starting values. */
export function indexSchema(schema) {
  const fields = new Map();
  for (const stage of schema.stages || []) {
    for (const field of stage.fields || []) fields.set(field.path, field);
  }
  return fields;
}

/**
 * The values a fresh Setup screen starts from.
 *
 * `base` is the merged config the API returned, so a summary line may name a config path that is
 * not itself editable (`model_search.candidate_pool`, for one) and still resolve. Each field's
 * `value` is then written over the top; only those fields are ever sent back as overrides.
 */
export function initialValues(schema, base) {
  const values = deepClone(base || {});
  for (const stage of schema.stages || []) {
    for (const field of stage.fields || []) writePath(values, field.path, clone(field.value));
  }
  return values;
}

const clone = (v) => (Array.isArray(v) ? v.slice() : v);

const deepClone = (v) =>
  Array.isArray(v)
    ? v.map(deepClone)
    : v && typeof v === "object"
      ? Object.fromEntries(Object.entries(v).map(([k, x]) => [k, deepClone(x)]))
      : v;

const sameValue = (a, b) =>
  Array.isArray(a) && Array.isArray(b) ? a.length === b.length && a.every((x, i) => x === b[i]) : a === b;

/**
 * The `overrides` body of `POST /runs`: every field the user moved away from its default, keyed by
 * the schema's own dotted path, which is exactly what the config resolver accepts.
 */
export function overridesFrom(schema, values) {
  const overrides = {};
  for (const stage of schema.stages || []) {
    for (const field of stage.fields || []) {
      const current = readPath(values, field.path);
      if (current === undefined) continue;
      if (!sameValue(current, field.default)) overrides[field.path] = clone(current);
    }
  }
  return overrides;
}

/** `visible_when: {path, equals}` against the live values; `__ui.*` paths carry screen state. */
export const isVisible = (field, values) =>
  !field.visible_when || readPath(values, field.visible_when.path) === field.visible_when.equals;

// --- the summary line on each collapsed stage --------------------------------------------------
// Grammar used by `summary_template`:
//   {path}            the value, as its choice label when the field has choices
//   {path|count}      the length of an array value
//   {path|pct}        a fraction as a percentage
//   {?path: text}     text only when the value is truthy   ({?!path: ...} inverts it)
//   {?path=value: …}  text only when the value equals `value`; `text` may nest more of the above

function readBrace(template, start) {
  let depth = 0;
  for (let i = start; i < template.length; i += 1) {
    if (template[i] === "{") depth += 1;
    else if (template[i] === "}") {
      depth -= 1;
      if (depth === 0) return { inner: template.slice(start + 1, i), next: i + 1 };
    }
  }
  return { inner: template.slice(start + 1), next: template.length };
}

function splitCondition(inner) {
  let depth = 0;
  for (let i = 0; i < inner.length; i += 1) {
    if (inner[i] === "{") depth += 1;
    else if (inner[i] === "}") depth -= 1;
    else if (inner[i] === ":" && depth === 0) return i;
  }
  return -1;
}

const truthy = (value) =>
  Array.isArray(value)
    ? value.length > 0
    : value !== null && value !== undefined && value !== "" && value !== false;

function labelFor(field, value) {
  if (field && Array.isArray(field.choices)) {
    const choice = field.choices.find((c) => c.value === value);
    if (choice) return choice.label;
  }
  if (value === null || value === undefined) return "";
  if (typeof value === "number") return String(Number(value.toFixed(6)));
  return String(value);
}

function formatted(fields, path, filter) {
  const field = fields.byPath.get(path);
  const value = readPath(fields.values, path);
  if (filter === "count") return String(Array.isArray(value) ? value.length : 0);
  if (filter === "pct") return String(Math.round(Number(value) * 1000) / 10);
  return labelFor(field, value);
}

function holds(test, fields) {
  if (test.startsWith("!")) return !truthy(readPath(fields.values, test.slice(1)));
  const eq = test.indexOf("=");
  if (eq > -1) {
    const path = test.slice(0, eq);
    return String(readPath(fields.values, path)) === test.slice(eq + 1);
  }
  return truthy(readPath(fields.values, test));
}

export function renderTemplate(template, byPath, values) {
  const fields = { byPath, values };
  const walk = (text) => {
    let out = "";
    let i = 0;
    while (i < text.length) {
      if (text[i] !== "{") {
        out += text[i];
        i += 1;
        continue;
      }
      const { inner, next } = readBrace(text, i);
      i = next;
      if (inner.startsWith("?")) {
        const cut = splitCondition(inner);
        const test = cut === -1 ? inner.slice(1) : inner.slice(1, cut);
        const body = cut === -1 ? "" : inner.slice(cut + 1);
        out += holds(test, fields) ? walk(body) : "";
      } else {
        const [path, filter] = inner.split("|");
        out += formatted(fields, path, filter);
      }
    }
    return out;
  };
  return walk(template);
}

export function stageSummary(stage, byPath, values) {
  if (!stage.summary_template) return stage.summary || "";
  try {
    return renderTemplate(stage.summary_template, byPath, values);
  } catch {
    return stage.summary || "";
  }
}

// --- widgets ----------------------------------------------------------------------------------
// Each one renders into the markup the prototype already styles; only the source of the field is
// new. `columns` supplies the options for the column-backed widgets.

const scaled = (field, value) => (field.scale ? Number(value) * field.scale : value);

const trimNumber = (n) => String(Number(Number(n).toFixed(6)));

const optionList = (choices, current, emptyLabel) =>
  `${emptyLabel ? `<option value="">${esc(emptyLabel)}</option>` : ""}${choices
    .map(
      (c) =>
        `<option value="${esc(c.value)}"${String(c.value) === String(current) ? " selected" : ""}${
          c.enabled === false ? " disabled" : ""
        }>${esc(c.label)}</option>`,
    )
    .join("")}`;

function columnChoices(field, columns) {
  const source = columns[field.column_source] || [];
  return source.map((name) => ({ value: name, label: name, enabled: true }));
}

function selectField(field, value) {
  return `<div class="field"><span class="sub">${esc(field.label)}</span><div class="control sel"><select data-path="${esc(
    field.path,
  )}" data-kind="${esc(field.type)}" aria-label="${esc(field.label)}">${optionList(
    field.choices || [],
    value,
    null,
  )}</select></div></div>`;
}

function columnSelectField(field, value, columns) {
  return `<div class="field"><span class="sub">${esc(field.label)}</span><div class="control sel"><select data-path="${esc(
    field.path,
  )}" data-kind="string" data-nullable="1" aria-label="${esc(field.label)}">${optionList(
    columnChoices(field, columns),
    value,
    field.empty_label || "None",
  )}</select></div></div>`;
}

function numberField(field, value) {
  const attr = (name, raw) =>
    raw === null || raw === undefined ? "" : ` ${name}="${trimNumber(scaled(field, raw))}"`;
  return `<div class="field xs"><span class="sub">${esc(field.label)}</span><div class="control"><input type="number" data-path="${esc(
    field.path,
  )}" data-kind="${esc(field.type)}"${field.scale ? ` data-scale="${field.scale}"` : ""}${attr(
    "step",
    field.step,
  )}${attr("min", field.min)}${attr("max", field.max)} value="${trimNumber(
    scaled(field, value),
  )}" aria-label="${esc(field.label)}"></div></div>`;
}

function checkboxField(field, value) {
  return `<div class="field"><span class="sub">&nbsp;</span><label class="check"><input type="checkbox" data-path="${esc(
    field.path,
  )}" data-kind="boolean"${value ? " checked" : ""}> ${esc(field.label)}</label></div>`;
}

function multiSelectField(field, value, choices, asChips) {
  const selected = Array.isArray(value) ? value : [];
  const visible = field.max_visible ? choices.slice(0, field.max_visible) : choices;
  const hidden = choices.length - visible.length;
  const head = field.label
    ? `<span class="sub" style="width:100%;font-size:12px;color:var(--muted)">${esc(field.label)}${
        selected.length ? ` · ${selected.length} selected` : ""
      }</span>`
    : "";
  return `<div class="algos">${head}${visible
    .map(
      (c) =>
        `<label><input type="checkbox" data-path="${esc(field.path)}" data-kind="array" data-member="${esc(
          c.value,
        )}"${field.min_selected ? ` data-min="${field.min_selected}"` : ""}${
          selected.includes(c.value) ? " checked" : ""
        }> ${asChips ? `<span class="colchip">${esc(c.label)}</span>` : esc(c.label)}</label>`,
    )
    .join("")}${hidden > 0 ? `<span class="sub">+${hidden} more</span>` : ""}</div>`;
}

/**
 * A setting the engine records but does not act on yet: shown, so the agreed shape is not lost,
 * but inert, so the screen never promises behaviour the engine does not have. The schema decides
 * which ones — this file still knows no setting by name.
 *
 * Disabling is what makes it inert, not decoration: a disabled control fires no `change` event, so
 * its value never reaches `applyControl` and never becomes a run override.
 */
function asAdvisory(html, field) {
  if (!html) return html;
  return html
    .replace(/^<div class="/, '<div class="advisory ')
    .replace(/<(input|select)\b/g, "<$1 disabled")
    .replace(/<\/div>$/, `<span class="advisory-note">${esc(field.help)}</span></div>`);
}

/** One field, in the widget the schema asked for. Unknown widgets are skipped, never guessed. */
export function fieldHtml(field, values, columns) {
  const value = readPath(values, field.path);
  const html = widgetHtml(field, value, columns);
  return field.advisory ? asAdvisory(html, field) : html;
}

function widgetHtml(field, value, columns) {
  switch (field.widget) {
    case "select":
      return selectField(field, value);
    case "column-select":
      return columnSelectField(field, value, columns);
    case "number":
      return numberField(field, value);
    case "checkbox":
      return checkboxField(field, value);
    case "multi-select":
      return multiSelectField(field, value, field.choices || [], false);
    case "column-multi-select":
      return multiSelectField(field, value, columnChoices(field, columns), true);
    default:
      return "";
  }
}

const inRow = (field) => field.widget !== "multi-select" && field.widget !== "column-multi-select";

/** A stage body: fields in the schema's own order, consecutive inline ones sharing one `.frow`. */
function stageBody(stage, values, columns) {
  const visible = (stage.fields || [])
    .filter((field) => isVisible(field, values))
    .slice()
    .sort((a, b) => (a.order || 0) - (b.order || 0));
  const out = [];
  let row = [];
  const flush = () => {
    if (row.length) out.push(`<div class="frow">${row.join("")}</div>`);
    row = [];
  };
  for (const field of visible) {
    const html = fieldHtml(field, values, columns);
    if (!html) continue;
    if (inRow(field)) row.push(html);
    else {
      flush();
      out.push(html);
    }
  }
  flush();
  return out.join("");
}

/** The collapsed stages of the prototype, one per stage the schema returned. */
export function stagesHtml(schema, values, columns) {
  const byPath = indexSchema(schema);
  const stages = (schema.stages || []).map(
    (stage) =>
      `<details class="stage-d" data-stage="${esc(stage.id)}"><summary><span class="sn">${esc(
        stage.number,
      )}</span><span><div class="st">${esc(stage.title)}</div><div class="ss">${esc(
        stageSummary(stage, byPath, values),
      )}</div></span><span class="sc">Configure</span></summary>${stageBody(stage, values, columns)}</details>`,
  );
  return `<div class="stages">${stages.join("")}</div>`;
}

/** Apply one control's change to the values object; returns the values for chaining. */
export function applyControl(element, values) {
  const path = element.dataset.path;
  const kind = element.dataset.kind;
  if (kind === "array") {
    const current = readPath(values, path);
    const list = Array.isArray(current) ? current.slice() : [];
    const member = element.dataset.member;
    const next = element.checked ? [...new Set([...list, member])] : list.filter((x) => x !== member);
    const min = Number(element.dataset.min || 0);
    writePath(values, path, next.length >= min ? next : [member]);
    return values;
  }
  if (kind === "boolean") return writePath(values, path, element.checked);
  if (kind === "integer" || kind === "number") {
    const raw = Number(element.value);
    if (Number.isNaN(raw)) return values;
    const scale = Number(element.dataset.scale || 0);
    const value = scale ? raw / scale : raw;
    return writePath(values, path, kind === "integer" ? Math.round(value) : Number(value.toFixed(6)));
  }
  const text = element.value;
  return writePath(values, path, text === "" && element.dataset.nullable ? null : text);
}
