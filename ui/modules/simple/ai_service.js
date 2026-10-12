// Plan M M192 (DEC-1622): Settings › AI service (`#/settings/ai`) - the AI service in one click.
//
// "I am not able to find where to configure my LLM API keys in this new UI": Settings sent the AI service to the
// Connections page, which the campaigns layout redirects to the Data page, whose note sent the person back to
// Settings - a loop. This page ends it. It shows the two AI services in plain words, side by side (stacked on a
// phone), each with its state and one direct button to its key form, which already exists at
// `#/connections/ai/<slot>` (`modules/connections/ai.js`):
//
//   "AI that helps your team"           the `product` slot: the chat and suggestions in Guided setup and Data Doctor;
//   "AI that writes for your customers" the `deliverable` slot: campaign messages and the notes on why a result
//                                       changed.
//
// What it says comes from `GET /ai-service` (`{ slots: { product, deliverable }, providers }`) and nothing else: the
// state word, the service's name (`provider_label`), whether the second uses the first's service (`source:
// "inherited"`) and the server's own reason when a slot is locked. A failed load is the error with "Try again". The
// engineering names ("Product AI", the slot ids) appear only behind "Show technical details" (`data-technical`).
//
// The page is drawn only for a person who may read the AI service (`GET /ai-service`); the Connect / Change buttons
// only for one who may save it (`PUT /ai-service/{slot}`), and anyone else reads "Ask an admin to connect it". In the
// campaigns layout the key form a button opens uses these same names (`AI_SLOTS`) and puts Settings › AI service in its
// breadcrumb (`modules/connections/ai.js`). The builder (`aiServiceHtml`) is pure, for the jsdom tests.

import { SETTINGS_CRUMB, crumbs, errorBox, esc, noticeCard } from "../../dom.js";
import { button, icon, pageHeader, statusChip } from "../../components.js";

export const AI_SETTINGS_TITLE = "AI service";
export const AI_SETTINGS_SUBTITLE = "The AI that helps your team, and the AI that writes for your customers.";
export const SAME_KEY_NOTE = "You can use the same key for both.";

/** The two services in plain words, in the order they are drawn. `slot` is the API's id; `tech` the old name. */
export const AI_SLOTS = Object.freeze([
  Object.freeze({
    slot: "product",
    title: "AI that helps your team",
    text: "Powers the chat and the suggestions in Guided setup and Data Doctor.",
    tech: "Product AI",
  }),
  Object.freeze({
    slot: "deliverable",
    title: "AI that writes for your customers",
    text: "Writes campaign messages and the notes that explain why a result changed.",
    tech: "Deliverable AI",
  }),
]);

/** The key form of one service. */
export const keyFormHref = (slot) => `#/connections/ai/${slot}`;

/** The service's name as the API gives it, or "" when it gives none. */
const serviceName = (d) => (d && (d.provider_label || d.provider)) || "";

/** Whether the API says this service is connected (a slot that uses the other's service counts). */
const isConnected = (d) => Boolean(d && d.connected);

/** The state chip: Connected · <service> / Not connected / Enter the key again / Last test failed. */
export function slotStatusHtml(d) {
  if (!d) return statusChip("health", "unknown", { word: "Not known", attrs: 'data-ai-svc-status="unknown"' });
  if (d.has_key && d.key_readable === false) {
    return statusChip("health", "needs_attention", { word: "Enter the key again", attrs: 'data-ai-svc-status="key_unreadable"' });
  }
  if (!d.connected) return statusChip("health", "needs_attention", { word: "Not connected", attrs: 'data-ai-svc-status="not_connected"' });
  const name = serviceName(d);
  if (d.last_test && d.last_test.ok === false) {
    return statusChip("health", "failed", {
      word: name ? `Last test failed · ${name}` : "Last test failed",
      attrs: 'data-ai-svc-status="failed"',
    });
  }
  return statusChip("health", "connected", { word: name ? `Connected · ${name}` : "Connected", attrs: 'data-ai-svc-status="connected"' });
}

/** One service's card. `primary` makes its button the page's one primary button. */
function slotCard(meta, d, { canPut, primary }) {
  const connected = isConnected(d);
  const titleId = `ai-svc-${meta.slot}-h`;
  const notes = [];
  if (d && d.source === "inherited" && connected) {
    notes.push(`<p class="ai-svc-note" data-ai-svc-inherited>Uses the same service as the AI that helps your team.</p>`);
  }
  if (d && d.editable === false && d.locked_reason) {
    notes.push(`<p class="ai-svc-note" data-ai-svc-locked>${esc(d.locked_reason)}</p>`);
  }
  const verb = connected ? "Change" : "Connect";
  const action = canPut
    ? button({
        label: verb,
        href: keyFormHref(meta.slot),
        kind: primary ? "primary" : "secondary",
        icon: "arrowRight",
        attrs: `data-ai-svc-go="${esc(meta.slot)}" aria-label="${esc(
          `${verb}: ${meta.title}`,
        )}"`,
      })
    : `<p class="ai-svc-ask" data-ai-svc-ask>${esc(connected ? "Ask an admin to change it." : "Ask an admin to connect it.")}</p>`;
  const tech = [`Technical name: ${meta.tech}`, d && d.model ? `model ${d.model}` : ""].filter(Boolean).join(", ");
  return `<section class="card ai-svc-card" data-ai-svc-card="${esc(meta.slot)}" aria-labelledby="${titleId}"><h2 class="ai-svc-title" id="${titleId}">${esc(
    meta.title,
  )}</h2><p class="ai-svc-text">${esc(meta.text)}</p><div class="ai-svc-state">${slotStatusHtml(d)}</div>${notes.join(
    "",
  )}<p class="ai-svc-tech" data-technical>${esc(tech)}.</p><div class="ai-svc-act">${action}</div></section>`;
}

/**
 * The page. `state` is `GET /ai-service`'s answer (or null while it loads), `error` the failed call, `can(method, path)`
 * what this person may do.
 */
export function aiServiceHtml({ state = null, error = null, can = () => true } = {}) {
  const head = pageHeader({
    title: AI_SETTINGS_TITLE,
    subtitle: AI_SETTINGS_SUBTITLE,
    crumbs: crumbs([SETTINGS_CRUMB, { label: AI_SETTINGS_TITLE }]),
  });
  const open = `<main class="screen sp ai-svc" data-module="simple" data-ai-settings>${head}`;
  if (!can("GET", "/ai-service")) {
    return `${open}${noticeCard({
      title: "You cannot see the AI service",
      text: "Ask an admin to show you which AI service is connected.",
      action: { label: "Back to Settings", href: "#/settings", kind: "secondary" },
      attrs: "data-ai-svc-denied",
    })}</main>`;
  }
  if (error) return `${open}<div class="ai-svc-error" data-ai-svc-error>${errorBox(error, { retry: true })}</div></main>`;
  if (!state) return `${open}<p class="ai-svc-loading" role="status">Loading…</p></main>`;
  const slots = (state && state.slots) || {};
  const canPut = Boolean(can("PUT", "/ai-service/{slot}"));
  // The one primary button: the first service that is not connected yet. With both connected, both are "Change".
  const first = AI_SLOTS.find((meta) => !isConnected(slots[meta.slot]));
  const cards = AI_SLOTS.map((meta) => slotCard(meta, slots[meta.slot] || null, { canPut, primary: first === meta })).join("");
  return `${open}<div class="ai-svc-grid">${cards}</div><p class="ai-svc-same" data-ai-svc-same>${icon("info", {
    size: 16,
  })}<span>${esc(SAME_KEY_NOTE)}</span></p></main>`;
}

// --- styles: tokens only (ui/theme.css), so a deployment's brand colour reaches the buttons ------------------------

export const AI_SERVICE_CSS = `
.ai-svc .ai-svc-grid{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:var(--s4,16px);align-items:stretch}
.ai-svc .ai-svc-card{display:flex;flex-direction:column;gap:var(--s3,12px);padding:var(--s5,24px);margin:0}
.ai-svc .ai-svc-card .ai-svc-title{margin:0;padding:0;font-size:var(--fs-section,18px);line-height:var(--lh-section,26px);font-weight:600;color:var(--ink)}
.ai-svc .ai-svc-text{margin:0;font-size:var(--fs-body,14px);line-height:var(--lh-body,22px);color:var(--ink2)}
.ai-svc .ai-svc-state{display:flex;flex-wrap:wrap;gap:var(--s2,8px)}
.ai-svc .ai-svc-note{margin:0;font-size:var(--fs-small,13px);line-height:var(--lh-small,20px);color:var(--muted)}
.ai-svc .ai-svc-tech{margin:0;font-size:var(--fs-caption,12px);line-height:var(--lh-caption,16px);color:var(--muted)}
.ai-svc .ai-svc-act{margin-top:auto;padding-top:var(--s2,8px)}
.ai-svc .ai-svc-ask{margin:0;font-size:var(--fs-small,13px);line-height:var(--lh-small,20px);color:var(--ink2)}
.ai-svc .ai-svc-same{display:flex;align-items:center;gap:var(--s2,8px);margin:var(--s4,16px) 0 0;font-size:var(--fs-small,13px);line-height:var(--lh-small,20px);color:var(--ink2)}
.ai-svc .ai-svc-same svg{flex:none;color:var(--brand-blue)}
@media (max-width:700px){
.ai-svc .ai-svc-grid{grid-template-columns:minmax(0,1fr)}
.ai-svc .ai-svc-card{padding:var(--s4,16px)}
.ai-svc .ai-svc-act .btn{width:100%;justify-content:center}
}
`;
