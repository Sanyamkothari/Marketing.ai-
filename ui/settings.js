// Advanced settings, generated entirely from `GET /use-cases/{id}.advanced_settings`.
//
// Nothing in this file knows the name of a single setting: stages, fields, widgets, choices, the
// bounds of every number and the per-stage summary line all come from the schema, so a new setting
// added to the engine config appears on the screen without a change here (plan §9.2).

import { esc, glossarySetting } from "./dom.js";

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
 * The `overrides` body of `POST /runs`: every non-advisory field the user moved away from its
 * default, keyed by the schema's own dotted path, which is exactly what the config resolver accepts.
 */
export function overridesFrom(schema, values) {
  const overrides = {};
  for (const stage of schema.stages || []) {
    for (const field of stage.fields || []) {
      // An advisory field changes no model, so it never goes back as an override even if something
      // other than its (disabled) control moved it; the API would accept it, the screen never sends it.
      if (field.advisory) continue;
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

/**
 * The plain meaning of a setting (`configs/pilot/help.yaml`, read through the glossary seam), shown
 * under its control; "" until the catalogue is registered, or when it has no entry. The catalogue
 * writes list indexes as `[*]`.
 */
function captionFor(field) {
  const entry = glossarySetting(String(field.path).replace(/\[\d+\]/g, "[*]"));
  const meaning = entry && (typeof entry === "string" ? entry : entry.meaning);
  return meaning ? `<span class="fcap">${esc(meaning)}</span>` : "";
}

function selectField(field, value) {
  return `<div class="field"><span class="sub">${esc(field.label)}</span><div class="control sel"><select data-path="${esc(
    field.path,
  )}" data-kind="${esc(field.type)}" aria-label="${esc(field.label)}">${optionList(
    field.choices || [],
    value,
    null,
  )}</select></div>${captionFor(field)}</div>`;
}

function columnSelectField(field, value, columns) {
  return `<div class="field"><span class="sub">${esc(field.label)}</span><div class="control sel"><select data-path="${esc(
    field.path,
  )}" data-kind="string" data-nullable="1" aria-label="${esc(field.label)}">${optionList(
    columnChoices(field, columns),
    value,
    field.empty_label || "None",
  )}</select></div>${captionFor(field)}</div>`;
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
  )}" aria-label="${esc(field.label)}"></div>${captionFor(field)}</div>`;
}

/** A checkbox is its own label: no empty `.sub` above it, so a "?" added to the field sits in `.check`. */
function checkboxField(field, value) {
  return `<div class="field fcheck"><label class="check"><input type="checkbox" data-path="${esc(
    field.path,
  )}" data-kind="boolean"${value ? " checked" : ""}> ${esc(field.label)}</label>${captionFor(field)}</div>`;
}

function multiSelectField(field, value, choices, asChips) {
  const selected = Array.isArray(value) ? value : [];
  const visible = field.max_visible ? choices.slice(0, field.max_visible) : choices;
  const hidden = choices.length - visible.length;
  const head = field.label
    ? `<span class="sub algos-head">${esc(field.label)}${
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
    .join("")}${hidden > 0 ? `<span class="sub">+${hidden} more</span>` : ""}${captionFor(field)}</div>`;
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

/** A widget that lists the file's columns has nothing to offer until there is a file. */
const waitsForColumns = (field, columns) =>
  Boolean(field.column_source) && !(columns[field.column_source] || []).length;

/**
 * A stage body: the fields in the schema's own order, in one grid (a list of checkboxes spans the
 * whole row). `advisory` picks which of the stage's fields: the ones the engine acts on, or the
 * planned ones. A field that lists the file's columns waits for a file; one line says so.
 */
function stageBody(stage, values, columns, advisory) {
  const visible = (stage.fields || [])
    .filter((field) => Boolean(field.advisory) === advisory && isVisible(field, values))
    .slice()
    .sort((a, b) => (a.order || 0) - (b.order || 0));
  const out = [];
  let waiting = 0;
  for (const field of visible) {
    if (waitsForColumns(field, columns)) {
      waiting += 1;
      continue;
    }
    const html = fieldHtml(field, values, columns);
    if (html) out.push(html);
  }
  const note = waiting
    ? `<p class="stage-cap">${
        waiting === 1 ? "One more setting appears" : `${waiting} more settings appear`
      } once a file is uploaded: ${waiting === 1 ? "it lists" : "they list"} the file's columns.</p>`
    : "";
  return `<div class="sfields">${out.join("")}</div>${note}`;
}

/**
 * The note a collapsed stage leads with when every one of its fields is advisory, else "".
 *
 * The summary line of such a stage still reads the recorded values ("encoding Auto · max 100"), and
 * with the stage folded nothing else on screen says they are inert. The text is the fields' own
 * `help`, which the schema sets to its "Coming later" note, so this file still names no setting.
 */
function parkedNote(stage) {
  const fields = stage.fields || [];
  return fields.length && fields.every((field) => field.advisory) ? `${fields[0].help} ` : "";
}

/** How many of a stage's working settings differ from the use case's own defaults. */
export function changedCount(stage, values) {
  return (stage.fields || []).filter((field) => {
    if (field.advisory) return false;
    const current = readPath(values, field.path);
    return current !== undefined && !sameValue(current, field.default);
  }).length;
}

/**
 * A folded stage's one line: "Using recommended settings", or how many were changed followed by the
 * stage's own summary, so a change (a time-based split, say) can be read without opening the stage.
 */
function foldedLine(stage, byPath, values) {
  const changed = changedCount(stage, values);
  if (!changed) return "Using recommended settings";
  return `You changed ${changed} setting${changed === 1 ? "" : "s"} · ${stageSummary(stage, byPath, values)}`;
}

/** One folded stage. "Configure" is the disclosure's own label; the summary is the real control. */
const stageShell = (id, title, line, body) =>
  `<details class="stage-d" data-stage="${esc(id)}"><summary><span><div class="st">${esc(
    title,
  )}</div><div class="ss">${esc(line)}</div></span><span class="sc">Configure</span></summary>${body}</details>`;

/**
 * The stages, folded, one per stage the schema returned. The settings the engine does not act on yet
 * (`field.advisory`) wait behind one toggle at the foot - whole stages and single fields alike - so
 * nothing on the main list looks like a step that has to be done.
 */
export function stagesHtml(schema, values, columns) {
  const byPath = indexSchema(schema);
  const stages = schema.stages || [];
  const working = stages
    .filter((stage) => (stage.fields || []).some((field) => !field.advisory))
    .map((stage) =>
      stageShell(
        stage.id,
        stage.title,
        foldedLine(stage, byPath, values),
        `<p class="stage-cap">${esc(stageSummary(stage, byPath, values))}</p>${stageBody(
          stage,
          values,
          columns,
          false,
        )}`,
      ),
    );
  const planned = stages.filter((stage) => (stage.fields || []).some((field) => field.advisory));
  const count = planned.reduce((n, stage) => n + stage.fields.filter((field) => field.advisory).length, 0);
  const plannedStages = planned.map((stage) => {
    const whole = stage.fields.every((field) => field.advisory);
    return stageShell(
      whole ? stage.id : `${stage.id}--planned`,
      stage.title,
      whole ? parkedNote(stage) + stageSummary(stage, byPath, values) : stage.fields.find((f) => f.advisory).help,
      stageBody(stage, values, columns, true),
    );
  });
  const later = count
    ? `<details class="planned" data-planned><summary>Show settings planned for a later release (${count})</summary><p class="stage-cap">They are recorded with each run but do not change results yet.</p><div class="stages plain">${plannedStages.join(
        "",
      )}</div></details>`
    : "";
  return `<div class="stages plain">${working.join("")}</div>${later}`;
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
