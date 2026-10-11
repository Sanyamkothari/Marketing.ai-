// Plan L follow-up (DEC-1599): "Things we checked" on a training run's Model page, drawn from the server's
// answer (`GET /runs/{id}/checks`, `engine/run_checks.py`). Every sentence and verdict is the server's;
// nothing is computed here. Importable under node (no `window`), so the tests render it directly.

import { esc } from "../../dom.js";

/** Each status's pill: class and word. A check that did not run says so; it is never shown as passed. */
const ALARM = "RESULT_SUSPICIOUSLY_GOOD";

const PILL = {
  passed: ["ok", "Passed"],
  warning: ["warn", "Worth a look"],
  info: ["neutral", "Measured"],
  not_run: ["neutral", "Not run"],
};

// The page's stylesheet is Phase 1's; the panel's few rules ride on its own tokens rather than add a block there.
const ALARM_STYLE = "margin:12px 20px 0;padding:10px 12px;border-radius:8px;background:var(--warn-t);color:var(--warn)";
const LIST_STYLE = "list-style:none;margin:0;padding:12px 20px;display:grid;gap:10px";

/** The one-line alarm above the list, when the result is too good to be true; "" otherwise. */
export function alarmHtml(view) {
  const alarm = view && (view.checks || []).find((c) => c.code === ALARM);
  if (!alarm) return "";
  return `<p role="alert" data-result-suspicious style="${ALARM_STYLE}"><b>Too good to be true?</b> ${esc(alarm.message)}</p>`;
}

/** The card: the alarm, then one line per check the run's reports show; "" with nothing to show. */
export function checksPanelHtml(view) {
  const checks = (view && view.checks) || [];
  if (!checks.length) return "";
  const rows = checks
    .map((c) => {
      const [cls, word] = PILL[c.status] || PILL.info;
      return `<li data-check="${esc(c.id)}" data-status="${esc(c.status)}"><span class="pill ${cls}">${esc(word)}</span> <b>${esc(
        c.title,
      )}</b><div class="caption">${esc(c.code === ALARM ? "See the warning above." : c.message)}</div></li>`;
    })
    .join("");
  return `<section class="card" data-things-checked><h3>Things we checked</h3>${alarmHtml(view)}<ul style="${LIST_STYLE}">${rows}</ul><p class="caption" style="padding:0 20px 16px">Read from this run's own reports. A check that did not run says so. Warnings block nothing.</p></section>`;
}
