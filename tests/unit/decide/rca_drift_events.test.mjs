/* Plan J M109 (review fix): the root-cause screen shows the events noted behind a run's change, read-only.
   The first build put the notes only on the Output page. These tests render the real `rcaHtml`
   (`ui/modules/generative/rca.js`) with the server's drift-events answer and look for the card.
   Run by tests/unit/decide/test_rca_drift_events_js.py, or directly:
   `node --test tests/unit/decide/rca_drift_events.test.mjs`. No npm install is needed. */
import { test } from "node:test";
import assert from "node:assert/strict";

// `ui/api.js` reads `window` when it loads; the screen's pure renderer never calls it.
globalThis.window = { location: { origin: "http://localhost" } };
const { rcaHtml } = await import(new URL("../../../ui/modules/generative/rca.js", import.meta.url));

const uc = {
  id: "churn-prevention",
  name: "Churn prevention",
  marker: "G",
  stars: "*",
  type_label: "Generative",
  ai_type: "generative",
  pages: { data: "Data", model: "Model", output: "Output" },
};
const run = { run_id: "r-score", mode: "score", created_at: "2026-09-20T09:00:00Z" };
const drift = (over = {}) => ({
  run_id: "r-score",
  drift_measured: true,
  drift_status: "drifted",
  moved_measures: ["visits_last_7d"],
  window_start: "2026-08-01",
  window_end: "2026-09-20",
  window_note: "Events count from 1 Aug 2026 (when the model was trained) to 20 Sep 2026 (when this change was measured).",
  events: [
    {
      annotation_id: "a1b2c3",
      event_date: "2026-08-25",
      kind: "price_change",
      kind_label: "A price change",
      note: "Prices went up 8%.",
      measures: ["visits_last_7d"],
      recorded_by: null,
      counted: true,
      reason: null,
      explains: ["visits_last_7d"],
    },
  ],
  explained_measures: ["visits_last_7d"],
  unexplained_measures: [],
  headline: "Possibly explained by a price change on 25 Aug 2026.",
  note: "A noted event is a possible reason, not a proof.",
  kinds: [["price_change", "A price change"]],
  measure_choices: ["visits_last_7d"],
  ...over,
});
const state = (over = {}) => ({ run, art: {}, drift: null, generating: false, generateError: null, ...over });

test("the root-cause screen shows the noted events with the server's headline and caveat", () => {
  const html = rcaHtml(uc, state({ drift: drift() }));
  assert.match(html, /Events behind the change/);
  assert.match(html, /Possibly explained by a price change on 25 Aug 2026\./);
  assert.match(html, /Prices went up 8%\./);
  assert.match(html, /possible reason, not a proof/);
});

test("the root-cause screen's card is read-only: no form, no remove button", () => {
  // Even when the answer carries can_edit, the screen never offers the actions; they live on the Output page.
  const html = rcaHtml(uc, state({ drift: { ...drift(), can_edit: true } }));
  assert.match(html, /data-drift-events/);
  assert.doesNotMatch(html, /data-drift-event-form|data-drift-event-remove/);
});

test("a run whose change was not measured, or whose notes could not be read, shows no card", () => {
  assert.doesNotMatch(rcaHtml(uc, state({ drift: null })), /Events behind the change/);
  assert.doesNotMatch(
    rcaHtml(uc, state({ drift: drift({ drift_measured: false, drift_status: null, events: [] }) })),
    /Events behind the change/,
  );
});
