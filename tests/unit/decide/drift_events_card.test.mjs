/* Plan J M109: the "Events behind the change" card on a scoring run's Output page
   (`ui/modules/decide/drift_events.js`), every word the server's (`GET /runs/{id}/drift-events`).
   Run by tests/unit/decide/test_drift_events_card_js.py, or directly:
   `node --test tests/unit/decide/drift_events_card.test.mjs`. No npm install is needed. */
import { test } from "node:test";
import assert from "node:assert/strict";

const { driftEventsCardHtml } = await import(new URL("../../../ui/modules/decide/drift_events.js", import.meta.url));

const event = (over = {}) => ({
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
  ...over,
});
const view = (over = {}) => ({
  run_id: "r1",
  drift_measured: true,
  drift_status: "drifted",
  moved_measures: ["visits_last_7d", "tenure_months"],
  window_start: "2026-08-01",
  window_end: "2026-09-20",
  window_note: "Events count from 1 Aug 2026 (when the model was trained) to 20 Sep 2026 (when this change was measured).",
  events: [event()],
  explained_measures: ["visits_last_7d"],
  unexplained_measures: ["tenure_months"],
  headline: "Possibly explained by a price change on 25 Aug 2026.",
  note: "A noted event is a possible reason, not a proof.",
  kinds: [
    ["price_change", "A price change"],
    ["other", "Something else"],
  ],
  measure_choices: ["visits_last_7d", "tenure_months"],
  can_edit: true,
  ...over,
});

test("a noted event appears with the server's headline, reading and caveat", () => {
  const html = driftEventsCardHtml(view());
  assert.match(html, /Events behind the change/);
  assert.match(html, /data-drift-event-headline>Possibly explained by a price change on 25 Aug 2026\./);
  assert.match(html, /A price change<\/strong> · .*2026 · visits_last_7d/);
  assert.match(html, /Prices went up 8%\./);
  assert.match(html, /data-event-counted/);
  assert.match(html, /Events count from 1 Aug 2026/);
  assert.match(html, /a possible reason, not a proof/);
});

test("an event the server did not count says why, in the server's words", () => {
  const html = driftEventsCardHtml(
    view({ events: [event({ counted: false, reason: "It is dated before the model was trained (1 Aug 2026)." })] }),
  );
  assert.match(html, /data-event-not-counted>Not counted\. It is dated before the model was trained/);
  assert.doesNotMatch(html, /data-event-counted/);
});

test("the add form and the remove buttons are offered only to a person who may use them", () => {
  const editor = driftEventsCardHtml(view());
  assert.match(editor, /data-drift-event-form/);
  assert.match(editor, /data-drift-event-remove="a1b2c3"/);
  assert.match(editor, /name="measures" value="visits_last_7d"/);
  assert.match(editor, /<option value="price_change">A price change<\/option>/);
  const reader = driftEventsCardHtml(view({ can_edit: false }));
  assert.doesNotMatch(reader, /data-drift-event-form/);
  assert.doesNotMatch(reader, /data-drift-event-remove/);
});

test("nothing is drawn when the change was not measured, or the run is stable with no notes", () => {
  assert.equal(driftEventsCardHtml(null), "");
  assert.equal(driftEventsCardHtml(view({ drift_measured: false })), "");
  assert.equal(driftEventsCardHtml(view({ drift_status: "stable", events: [], headline: null })), "");
  const stableNoted = driftEventsCardHtml(view({ drift_status: "stable", headline: null }));
  assert.match(stableNoted, /Events behind the change/);
  assert.doesNotMatch(stableNoted, /data-drift-event-headline/);
});

test("an answer from the server that refused the last action is shown, and the text is escaped", () => {
  const html = driftEventsCardHtml(view({ events: [event({ note: "<b>x</b>" })] }), {
    error: { message: "The change report did not compare 'x'." },
  });
  assert.match(html, /data-drift-event-error>The change report did not compare/);
  assert.doesNotMatch(html, /<b>x<\/b>/);
  assert.match(html, /&lt;b&gt;x&lt;\/b&gt;/);
});

test("a run with no notes yet says so", () => {
  const html = driftEventsCardHtml(view({ events: [], headline: "No event has been noted for the period." }));
  assert.match(html, /No event has been noted yet\./);
  assert.match(html, /No event has been noted for the period\./);
});
