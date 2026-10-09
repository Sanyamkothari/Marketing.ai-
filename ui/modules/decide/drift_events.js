// The "Events behind the change" card on a scoring run's Output page (Plan J M109, DEC-1319).
//
// When the customers scored look different from the ones the model learned on, the run's own report
// says how (the notice above). This card lets a person note what happened in the business - a price
// change, a competitor's launch - and shows the server's reading of those notes beside the change:
// which ones can be a reason, which measures they name, and one sentence. Every word and flag it draws
// is the server's (`GET /runs/{id}/drift-events`); nothing here decides whether an event counts.
// Pure: what it draws comes from its arguments, so node can test it.

import { EM_DASH, esc, fmtDate } from "../../dom.js";

const noteList = (view) =>
  view.events
    .map((event) => {
      const where = event.measures.length ? ` · ${event.measures.map(esc).join(", ")}` : "";
      const status = event.counted
        ? `<span class="dc-note" data-event-counted>Counted as a possible reason.</span>`
        : `<span class="dc-note" data-event-not-counted>Not counted. ${esc(event.reason || "")}</span>`;
      return `<li class="de-item" data-drift-event="${esc(event.annotation_id)}">
        <div><strong>${esc(event.kind_label)}</strong> · ${esc(fmtDate(event.event_date))}${where}</div>
        <div class="dc-text">${esc(event.note)}</div>
        <div>${status}${
          view.can_edit
            ? ` <button type="button" class="btn secondary" data-drift-event-remove="${esc(event.annotation_id)}">Remove</button>`
            : ""
        }</div>
      </li>`;
    })
    .join("");

const form = (view) => {
  const kinds = view.kinds.map(([value, label]) => `<option value="${esc(value)}">${esc(label)}</option>`).join("");
  const measures = view.moved_measures.length
    ? `<fieldset class="dc-basis"><legend>Which measures did it touch? (leave empty if it touched the customers as a whole)</legend>${view.moved_measures
        .map(
          (name) =>
            `<label class="dc-check"><input type="checkbox" name="measures" value="${esc(name)}"> ${esc(name)}</label>`,
        )
        .join("")}</fieldset>`
    : "";
  return `<form class="dc-form" data-drift-event-form>
    <label>When did it happen?<input name="event_date" type="date" required></label>
    <label>What kind of event?<select name="kind">${kinds}</select></label>
    <label>A short note (no personal details)<input name="note" type="text" maxlength="200" required></label>
    ${measures}
    <div class="dc-actions"><button type="submit" class="btn secondary">Note this event</button></div>
  </form>`;
};

/**
 * The card. `view` is `GET /runs/{id}/drift-events` with `can_edit` added by the caller (what this person
 * may do comes from the access list, not from the answer). It draws nothing for a run whose change was
 * not measured, or one that is stable and has no notes: there is nothing to explain and nothing noted.
 * `error` is what the last add or remove answered, in the server's words.
 */
export function driftEventsCardHtml(view, { error = null } = {}) {
  if (!view || !view.drift_measured) return "";
  const stable = view.drift_status === "stable";
  if (stable && !view.events.length) return "";
  const problem = error ? `<p class="dc-error" role="alert" data-drift-event-error>${esc(error.message || String(error))}</p>` : "";
  const headline = view.headline ? `<p class="dc-headline" data-drift-event-headline>${esc(view.headline)}</p>` : "";
  const window = view.window_note ? `<p class="dc-note" data-drift-event-window>${esc(view.window_note)}</p>` : "";
  const list = view.events.length ? `<ul class="de-list">${noteList(view)}</ul>` : "";
  return `<div class="dc"><section class="card dc-card" data-drift-events>
    <h3>Events behind the change</h3>
    ${headline}
    ${list || `<p class="dc-text">${stable ? EM_DASH : "No event has been noted yet."}</p>`}
    ${window}
    <p class="dc-note" data-drift-event-caveat>${esc(view.note)}</p>
    ${problem}
    ${view.can_edit ? form(view) : ""}
  </section></div>`;
}
