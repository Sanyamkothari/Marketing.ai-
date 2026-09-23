// Small pieces the M48 privacy and M49 monitoring screens share: a control drawn already refused,
// tab strips, status pills and the audit reference line.
//
// A control on one of *this module's* screens is refused as it is drawn, not afterwards by
// `gate.js`'s table (DEC-792). The table exists for other workstreams' screens, which this branch may
// not edit; these screens are ours, so they ask `reasonFor(method, path)` - the very `GET /auth/me`
// permission list the table reads - while painting. What a person sees is identical either way: the
// control stays on screen, disabled, with the server's own sentence beside it and as its title, and
// it carries the same `data-pb-gate` marker, so `gate.js`'s capture-phase listener swallows a click or
// submit on it exactly as it does for a gated control anywhere else. Disabled and explained, never
// hidden: a Viewer should learn why the Run now button does nothing for them (plan M46).

import { EM_DASH, esc } from "../../dom.js";
import { can, reasonFor } from "./session.js";

/** `attrs` is raw HTML attributes; `label` is text. The button is refused when the route is. */
export function actionButton(method, path, { attrs = "", label, cls = "run", type = "button", busy = false } = {}) {
  const reason = reasonFor(method, path);
  if (reason) {
    return `<button type="${type}" class="${cls} pb-off" ${attrs} disabled data-pb-gate="${esc(reason)}" title="${esc(
      reason,
    )}">${esc(label)}</button><span class="pb-why" role="note">${esc(reason)}</span>`;
  }
  return `<button type="${type}" class="${cls}" ${attrs}${busy ? " disabled" : ""}>${esc(label)}</button>`;
}

/** A tab strip: `[key, label, href]` rows, the current one marked. */
export const tabStrip = (current, entries, label) =>
  `<div class="tabs-bar" style="margin-bottom:8px"><nav class="tabs" aria-label="${esc(label)}">${entries
    .map(
      ([key, text, href]) =>
        `<a class="tab${key === current ? " on" : ""}" href="${href}"${key === current ? ' aria-current="page"' : ""}>${esc(
          text,
        )}</a>`,
    )
    .join("")}</nav></div>`;

/** The whole screen refused: the server's sentence where the content would be. */
export const refusal = (reason) => `<div class="apierr" role="alert"><b>ROLE_REQUIRED</b>${esc(reason)}</div>`;

const PILL = {
  // firings and runs
  succeeded: "ok",
  done: "ok",
  running: "warn",
  queued: "warn",
  pending: "warn",
  failed: "bad",
  missed: "bad",
  cancelled: "warn",
  // erasure register
  completed: "ok",
  // alert severities
  info: "ok",
  warning: "warn",
  critical: "bad",
  // consent states
  valid: "ok",
  granted: "ok",
  withdrawn: "bad",
  expired: "warn",
};

/** A status as the house pill; an unknown status reads neutral (`warn`) rather than green. */
export const statusPill = (status) =>
  status ? `<span class="pill ${PILL[status] || "warn"}">${esc(String(status).replace(/_/g, " "))}</span>` : EM_DASH;

/** A monospace id, or the em dash. */
export const mono = (value) => (value ? `<span class="pb-mono">${esc(value)}</span>` : EM_DASH);

/**
 * Where to find the one audit event a change wrote: its action and object id, and - for an Admin -
 * a link that opens the audit viewer already filtered to it (`data-audit-*`, handled by the screen).
 */
export function auditReference(action, objectId) {
  const link = can("GET", "/audit/events")
    ? ` <button type="button" class="linkbtn" data-audit-action="${esc(action)}" data-audit-object="${esc(
        objectId,
      )}">Open in the audit log</button>`
    : "";
  return `<div class="pb-small" style="margin-top:8px">Audit: <span class="pb-mono">${esc(action)}</span> on <span class="pb-mono">${esc(
    objectId,
  )}</span>.${link}</div>`;
}

/** A labelled text input for a `.pb-form`. */
export const textField = (name, label, { value = "", placeholder = "", attrs = "" } = {}) =>
  `<label class="pb-field field"><span class="sub">${esc(label)}</span><input class="pb-input" name="${esc(
    name,
  )}" value="${esc(value)}" placeholder="${esc(placeholder)}" autocomplete="off" spellcheck="false" ${attrs}></label>`;

/** A form's trimmed text value by name, or "" when the field is absent. */
export function fieldValue(form, name) {
  const field = form.elements.namedItem(name);
  return field && typeof field.value === "string" ? field.value.trim() : "";
}
