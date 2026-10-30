// "What the Data Doctor changed": the Data tab's card for a run whose file came through a Data Doctor case (pure: it
// draws from the case's own screen answer, `GET /cases/{id}/screen`, so the jsdom tests call it with recorded answers).
// Every kind of change the approved plan made is listed, grouped, each with its before and after numbers where the
// case counted them (the plan's ledger): value fixes, rows left out, columns removed, columns added (the outcome and
// every column of the training table), files joined or reshaped, the person's own decisions, and who approved and when.
// Nothing here is a cell value: only names, counts and the plan's own sentences, as the Data Doctor screen shows them.

import { button } from "../../components.js";
import { esc, fmtStamp } from "../../dom.js";
import { count } from "../../format.js";
import { userText } from "../../labels.js";
import { SECTIONS, kindOf, sectionOf } from "../datadoctor/plan.js";
import { columnWords, doctorHref } from "./summary_rules.js";

const FIX = new Set([
  "parse_number",
  "parse_date",
  "map_boolean",
  "normalise_text",
  "set_missing",
  "set_missing_text",
  "map_values",
  "convert_units",
  "correct_cells",
  "split_column",
  "fill_missing",
  "rename_column",
  "normalise_key",
  "convert_currency",
]);
const ROWS_OUT = new Set(["remove_rows", "valid_range", "keep_latest_per_id", "suppression", "split_file"]);
const JOINED = new Set(["join_files", "stack_files", "build_dataset", "unpivot", "combine_rows", "sql_query"]);

const list = (v) => (Array.isArray(v) ? v : []);
const names = (columns) => list(columns).map((c) => (c && typeof c === "object" ? c.name : c)).filter((n) => typeof n === "string" && n);
const whole = (n) => Number.isFinite(n);

/** "Rows 541,909 to 516,384 · Customers 4,372 to 4,331", from one step of the plan's ledger; "" when it counted nothing. */
function numbersOf(step, entity) {
  if (!step) return "";
  const pair = (label, a, b) => (whole(a) && whole(b) ? `${label} ${count(a)} → ${count(b)}` : "");
  return [pair("Rows", step.rows_in, step.rows_out), pair(`${entity[0].toUpperCase()}${entity.slice(1)}s`, step.customers_in, step.customers_out)]
    .filter(Boolean)
    .join(" · ");
}

/**
 * What the approved plan changed, as groups of lines. `screen` is the case's screen answer (`{ case, view }`); `entity`
 * is the goal's word for a customer; `describe(name)` is the goal's own one-line description of a column (or null);
 * `outcome` is `{ name, text }`, the column the model predicts and what it means. `trainingColumns` are the names of the
 * columns the training table holds (from the training run, no cell values): they stand in for the screen's own column
 * lists when the reader may not see the file preview. Returns null when the screen holds no case. A group with nothing
 * in it is left out; no accepted change is left off the card: a kind with no group of its own goes under the plan
 * section it belongs to ("Read the file", "Outcome", "Privacy").
 */
export function doctorChanges(screen, { entity = "customer", describe = () => null, outcome = null, trainingColumns = [] } = {}) {
  const found = screen && screen.case;
  if (!found) return null;
  const view = screen.view || {};
  const review = view.review || {};
  const ledger = view.ledger || null;
  const planText = new Map(list(review.plan).map((p) => [p.item_id, p.text]));
  const decisions = list(review.decisions);
  const steps = new Map(list(ledger && ledger.steps).map((s) => [s.item_id, s]));
  // What is in force: accepted and not replaced by a newer proposal about the same thing.
  const items = list(found.items);
  const live = items.filter((i) => i.state === "accepted" && !i.superseded_by);
  const textOf = (item) => planText.get(item.item_id) || item.title || "";
  const line = (item) => ({ text: textOf(item), numbers: numbersOf(steps.get(item.item_id), entity) });

  const fixes = [];
  const rowsOut = [];
  const dropped = [];
  const joined = [];
  const kept = [];
  const others = new Map(SECTIONS.map((s) => [s.id, []]));
  const answeredIds = (item) => decisions.some((d) => d.question_id && d.answered && d.current && item.item_id.startsWith(`${d.question_id}:`));
  const build = live.find((i) => kindOf(i) === "sql_query");
  for (const item of live) {
    const kind = kindOf(item);
    if (FIX.has(kind)) fixes.push(line(item));
    else if (ROWS_OUT.has(kind)) rowsOut.push(line(item));
    else if (kind === "drop_column") dropped.push(item.payload && item.payload.step && item.payload.step.column);
    else if (JOINED.has(kind)) joined.push(line(item));
    else if (item.kind === "goal_setting") {
      const chosen = decisions.find((d) => d.question_id && item.item_id.startsWith(`${d.question_id}:`));
      rowsOut.push({
        text: `Audience: ${chosen && chosen.current ? chosen.current : textOf(item)}`,
        numbers: numbersOf(build && steps.get(build.item_id), entity),
      });
    } else if (item.kind === "data_check") kept.push({ text: `Checked and kept as it is: ${textOf(item)}`, numbers: "" });
    else if (!answeredIds(item)) others.get(sectionOf(item)).push(line(item)); // the key, roles, dates, outcome, privacy: said under "Your own decisions" when a question was answered
  }
  fixes.push(...others.get("fix"));
  rowsOut.push(...others.get("exclusions"));
  joined.push(...others.get("combine"));
  for (const item of items) {
    if (item.state === "rejected" && !item.superseded_by) kept.push({ text: `Left out of the plan at your request: ${item.title || textOf(item)}`, numbers: "" });
  }

  // Columns: what each file had, and what it has after the plan (the file's own columns, or the training table built from it).
  const columnsRemoved = [];
  const columnsAdded = [];
  const files = list(view.preview && view.preview.files);
  for (const file of files) {
    const before = names(file.before_columns);
    const after = names(file.columns);
    const label = file.after_table ? `${file.file_name} (becomes the table ${file.after_table})` : file.file_name;
    const gone = before.filter((n) => !after.includes(n) || dropped.includes(n));
    const added = after.filter((n) => !before.includes(n));
    if (gone.length) columnsRemoved.push({ file: label, names: gone });
    if (added.length) {
      columnsAdded.push({
        file: label,
        columns: added.map((name) => ({
          name,
          words: columnWords(name),
          outcome: Boolean(outcome && outcome.name === name),
          note: outcome && outcome.name === name ? outcome.text || "What the model predicts" : describe(name) || "",
        })),
      });
    }
  }
  // No file preview (the reader is not an Analyst): the training table's own column names stand in, with no before list.
  const trained = files.length ? [] : [...new Set([...names(trainingColumns), ...(outcome && outcome.name ? [outcome.name] : [])])];
  if (trained.length) {
    columnsAdded.push({
      file: "",
      columns: trained.map((name) => ({
        name,
        words: columnWords(name),
        outcome: Boolean(outcome && outcome.name === name),
        note: outcome && outcome.name === name ? outcome.text || "What the model predicts" : describe(name) || "",
      })),
    });
  }
  const needsAnalyst = !files.length;
  if (!columnsRemoved.length && dropped.filter(Boolean).length) columnsRemoved.push({ file: "", names: dropped.filter(Boolean) });

  const answered = decisions
    .filter((d) => d.question_id && d.answered && d.current)
    .map((d) => ({ text: d.title, answer: d.current, by: d.decided_by === "person" ? "You chose" : "Suggested and approved" }));

  const approvals = list(found.approvals).map((a) => ({
    by: a.approved_by,
    at: a.approved_at,
    text: a.text_shown || "",
  }));
  const funnel =
    ledger && whole(ledger.rows_in) && whole(ledger.rows_out)
      ? {
          rows: [ledger.rows_in, ledger.rows_out],
          customers: whole(ledger.customers_in) && whole(ledger.customers_out) ? [ledger.customers_in, ledger.customers_out] : null,
          rate: whole(ledger.positive_rate_out) ? ledger.positive_rate_out : null,
        }
      : null;
  const groups = [
    { id: "fixes", title: "Values fixed", lines: fixes },
    { id: "rows", title: "Rows left out", lines: rowsOut },
    { id: "joined", title: "Files joined, stacked or reshaped", lines: joined },
    ...["read", "outcome", "privacy"].map((id) => ({ id, title: SECTIONS.find((s) => s.id === id).title, lines: others.get(id) })),
  ].filter((g) => g.lines.length);
  return { funnel, groups, columnsRemoved, columnsAdded, needsAnalyst, decisions: answered, kept, approvals, entity };
}

const ul = (inner, attrs = "") => (inner ? `<ul class="pg-list"${attrs ? ` ${attrs}` : ""}>${inner}</ul>` : "");
const sect = (title, inner, attrs = "") => (inner ? `<p class="pg-sect pg-p"${attrs ? ` ${attrs}` : ""}>${title}</p>${inner}` : "");
const SHOWN_COLUMNS = 12;

function columnsAddedHtml(added) {
  return added
    .map((group) => {
      const li = (c) =>
        `<li data-dd-column="${c.name.replace(/[^\w-]/g, "")}"><b>${userText(c.words)}</b> <code class="mono">${userText(c.name)}</code>${
          c.outcome ? ' <span class="badge tone-info">What the model predicts</span>' : ""
        }${c.note ? ` <span class="muted">${userText(c.note)}</span>` : ""}</li>`;
      const shown = group.columns.slice(0, SHOWN_COLUMNS).map(li).join("");
      const rest = group.columns.slice(SHOWN_COLUMNS);
      return `${group.file ? `<p class="pg-p muted">In ${userText(group.file)}</p>` : ""}${ul(shown)}${
        rest.length ? `<details class="more" data-dd-more><summary>Show the other ${count(rest.length)} columns</summary>${ul(rest.map(li).join(""))}</details>` : ""
      }`;
    })
    .join("");
}

/**
 * The card. `changes` is `doctorChanges(...)` (null draws a short card saying the record could not be read, with the
 * link still offered); `caseId` the case the link opens.
 */
export function doctorChangesHtml(changes, caseId) {
  if (!caseId) return "";
  const link = `<div class="pg-p">${button({ label: "See the full approved plan", href: doctorHref(caseId), kind: "tertiary", attrs: "data-dd-plan" })}</div>`;
  if (!changes) {
    return `<section class="card" data-dd-changes><h3>What the Data Doctor changed</h3><p class="pg-p muted" data-dd-error>The Data Doctor's record of the changes could not be loaded just now. Reload the page to try again.</p>${link}</section>`;
  }
  const who = `${changes.entity}s`;
  const funnel = changes.funnel
    ? `<p class="pg-p" data-dd-funnel>${count(changes.funnel.rows[0])} rows in, ${count(changes.funnel.rows[1])} rows out${
        changes.funnel.customers ? `: ${count(changes.funnel.customers[0])} ${who} became ${count(changes.funnel.customers[1])}` : ""
      }${changes.funnel.rate !== null ? `, ${Math.round(changes.funnel.rate * 10) / 10}% of them with the outcome` : ""}.</p>`
    : "";
  const group = (g) =>
    sect(
      g.title,
      ul(g.lines.map((l) => `<li>${userText(l.text)}${l.numbers ? `<span class="pg-desc" data-dd-numbers>${userText(l.numbers)}</span>` : ""}</li>`).join("")),
      `data-dd-group="${g.id}"`,
    );
  const removed = sect(
    "Columns removed",
    changes.columnsRemoved
      .map(
        (r) =>
          `${r.file ? `<p class="pg-p muted">From ${userText(r.file)}</p>` : ""}<p class="pg-p" data-dd-removed>${r.names.map((n) => userText(columnWords(n))).join(", ")}</p>`,
      )
      .join(""),
    'data-dd-group="removed"',
  );
  const addedNote = changes.needsAnalyst && changes.columnsAdded.length
    ? `<p class="pg-p muted" data-dd-needs-analyst>These are the columns the model learns from. Which columns the file had before, and so which are new or removed, is shown to people with the Analyst role.</p>`
    : "";
  const added = sect("Columns added", `${addedNote}${columnsAddedHtml(changes.columnsAdded)}`, 'data-dd-group="added"');
  const decided = sect(
    "Your own decisions",
    ul(
      [
        ...changes.decisions.map((d) => `<li>${userText(d.text)}<span class="pg-desc">${esc(d.by)}: ${userText(d.answer)}</span></li>`),
        ...changes.kept.map((k) => `<li>${userText(k.text)}</li>`),
      ].join(""),
    ),
    'data-dd-group="decisions"',
  );
  const approved = sect(
    "Who approved it, and when",
    ul(
      changes.approvals
        .map((a) => `<li>${userText(a.by || "Someone")} approved this on ${esc(fmtStamp(a.at))}${a.text ? `<span class="pg-desc">${userText(a.text)}</span>` : ""}</li>`)
        .join(""),
    ),
    'data-dd-group="approved"',
  );
  const nothing = !changes.groups.length && !changes.columnsRemoved.length && !changes.columnsAdded.length;
  const body = nothing
    ? `<p class="pg-p muted" data-dd-none>The approved plan made no change to the file itself.</p>`
    : `${funnel}${changes.groups.slice(0, 2).map(group).join("")}${removed}${added}${changes.groups.slice(2).map(group).join("")}`;
  return `<section class="card" data-dd-changes><h3>What the Data Doctor changed</h3>${body}${decided}${approved}${link}<div class="pg-body"></div></section>`;
}
