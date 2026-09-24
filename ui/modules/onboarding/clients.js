// The client chooser in the top bar (Plan A M35; prototype `clientPicker()`, screenshot
// `01-client-selector`; v1 docs/UI_AUDIT.md section 1).
//
// Tables, data set-ups and the datasets built from them are kept per client, so every onboarding call
// names one. This module owns which client that is: it asks the API for the list (and for the default
// client "Demo", created the first time an installation has none), remembers the choice in this
// browser, and draws "Client: [Demo Telecom ▾]" into the top bar's context slot (`registerHeaderTool`
// in `index.js`; `ui/chrome.js` draws it).
//
// Where it is drawn. Only on the screens whose content depends on the client - Home (where a person
// starts and picks who they work for), a use case's Setup, Build data, the uplift Setup, the
// Reports list and the privacy screens that act for one client (consent, erasure, access: they say
// "For client: ... (chosen in the top bar)") - never on sign-in, account, admin, privacy retention,
// approvals or monitoring, where it would mean nothing. The top bar leaves the whole slot out for a signed-out visitor, so the sign-in screen
// never shows a refused client list.
//
// In demo mode the default is the demo's clean client (`GET /pilot/demo` names it), and the client the
// demo seeded with a planted problem is listed last and named as practice data. "+ New client" (kept
// as the option label) is offered only to a role that may add one (`POST /clients`, read from the
// `GET /auth/me` permission list); it opens a small form under the chooser: "Client name", Enter or
// "Add client" adds, Escape or "Cancel" closes, and an empty name is explained under the field.
//
// The top bar is repainted wholesale, so events are read off `document` with one delegated listener
// each rather than bound per paint. A change of client is a change to what the current screen shows -
// the Setup form's raw-tables panel belongs to one client - so it asks the router to repaint.

import { announceModulesChanged } from "../router.js";
import { createClient, ensureDefaultClient, listClients } from "./api.js";
import { errorBox, esc, toggletip } from "../../dom.js";

const STORAGE_KEY = "marketing-ai.client";
const NEW_CLIENT = "__new__";
const HINT = "Your uploaded tables and data set-ups are saved separately for each client.";

const state = {
  clients: [],
  current: null,
  loading: true,
  adding: false,
  nameMissing: false,
  error: null,
  practiceId: null, // the demo's planted-problem client, listed last
};

/** A per-browser convenience, nothing more: storage can be missing or refuse, and the picker then
 * simply starts on the default client again. */
function remembered() {
  try {
    return window.localStorage.getItem(STORAGE_KEY);
  } catch {
    return null;
  }
}

function remember(clientId) {
  try {
    window.localStorage.setItem(STORAGE_KEY, clientId);
  } catch {
    // A private window or blocked storage: the choice lasts for this page only.
  }
}

/** The client every onboarding call names right now, or `null` until the list has loaded. */
export function currentClient() {
  return state.clients.find((client) => client.client_id === state.current) || null;
}

/** `GET /pilot/demo` (the demo manifest), or null: no demo, an older API, or the call refused. */
async function demoManifest() {
  const base = (window.MARKETING_AI_API || window.location.origin).replace(/\/+$/, "");
  try {
    const response = await fetch(`${base}/pilot/demo`);
    if (!response.ok) return null;
    const body = await response.json();
    return body && body.demo_mode && body.seeded ? body.manifest || null : null;
  } catch {
    return null;
  }
}

/** Whether the signed-in role may add a client, from `GET /auth/me`'s permission list. Unknown (no
 * sign-in on this API, or not answered yet) is "yes": the server stays the one that refuses. */
let permissions = null;

async function readPermissions() {
  const base = (window.MARKETING_AI_API || window.location.origin).replace(/\/+$/, "");
  try {
    const response = await fetch(`${base}/auth/me`);
    if (!response.ok) return null;
    const body = await response.json();
    return Array.isArray(body && body.permissions) ? body.permissions : null;
  } catch {
    return null;
  }
}

export function mayAddClient() {
  if (!permissions) return true;
  const row = permissions.find((p) => p.method === "POST" && p.path === "/clients");
  return row ? Boolean(row.allowed) : true;
}

/** Clients in the order the chooser lists them: the list's own order, the practice client last. */
export function orderedClients(clients, practiceId) {
  const list = [...(clients || [])];
  return [...list.filter((c) => c.client_id !== practiceId), ...list.filter((c) => c.client_id === practiceId)];
}

/** The name a client is listed under: its own, or the practice client's plain description. */
function label(client) {
  if (client.client_id !== state.practiceId) return client.name;
  const clean = state.clients.find((c) => c.client_id !== state.practiceId && client.name.startsWith(c.name));
  return `${clean ? clean.name : client.name}: practice data with problems`;
}

/** Reads the list first and asks for the default client only when there is no client at all: the
 * `POST` writes (and is audited, and needs the Analyst role), so an installation that already has a
 * client - every load after the first - never sends it, and a Viewer can still pick among them. */
async function load() {
  try {
    const [listed, manifest, perms] = await Promise.all([listClients(), demoManifest(), readPermissions()]);
    permissions = perms;
    let { clients } = listed;
    // Newest first (`GET /clients`), so the last is the oldest: the default client, on every
    // installation that started here - unless the demo names its own.
    let fallback = (clients || [])[(clients || []).length - 1];
    if (!fallback) {
      fallback = await ensureDefaultClient();
      ({ clients } = await listClients());
    }
    state.clients = clients || [];
    state.practiceId = manifest ? manifest.broken_client_id || null : null;
    const demoClient = manifest && state.clients.find((c) => c.client_id === manifest.client_id);
    if (demoClient) fallback = demoClient;
    const wanted = remembered();
    state.current = state.clients.some((c) => c.client_id === wanted) ? wanted : fallback.client_id;
    state.error = null;
  } catch (error) {
    state.error = error;
  }
  state.loading = false;
  announceModulesChanged();
}

function select(clientId) {
  if (clientId === state.current) return;
  state.current = clientId;
  remember(clientId);
  announceModulesChanged();
}

async function add(name) {
  const industry = (currentClient() || state.clients[0] || {}).industry;
  try {
    const { client_id: clientId } = await createClient(name, industry);
    const { clients } = await listClients();
    state.clients = clients || [];
    state.adding = false;
    state.nameMissing = false;
    state.error = null;
    state.current = null; // so `select` below always announces, even if the id were reused
    select(clientId);
  } catch (error) {
    state.error = error;
    announceModulesChanged();
  }
}

function closeForm() {
  state.adding = false;
  state.nameMissing = false;
  state.error = null;
  announceModulesChanged();
  // The bar is redrawn in a microtask; put focus back on the chooser once it is there.
  setTimeout(() => {
    const chooser = document.getElementById("f-client");
    if (chooser) chooser.focus();
  }, 0);
}

// --- where the chooser means something ----------------------------------------------------------

/** Whether a route's screen depends on the chosen client (see the module comment). */
export function perClientRoute(hash) {
  const [a, b, c] = String(hash || "").replace(/^#\/?/, "").split("/").filter(Boolean);
  if (!a || a === "industry") return true; // Home
  if (a === "uc") return Boolean(b) && (!c || c === "run"); // a use case's Setup (and its run view)
  if (a === "uplift") return Boolean(b) && (!c || c === "run"); // the uplift Setup
  if (a === "pilot") return !b || b === "kit"; // Reports and Build data
  // Consent, erasure and access say "For client: ... (chosen in the top bar)", so the chooser is drawn.
  if (a === "privacy") return b === "consent" || b === "erasure" || b === "access";
  return false;
}

// --- drawing ------------------------------------------------------------------------------------

function newClientForm() {
  const invalid = state.nameMissing ? ` aria-invalid="true" aria-describedby="f-client-name-err"` : "";
  return `<form class="menu-pop right client-new" id="f-client-form" novalidate>
    <label class="cn-label" for="f-client-name">Client name</label>
    <div class="control"><input id="f-client-name" autocomplete="off" placeholder="e.g. Acme Broadband"${invalid}></div>
    ${state.nameMissing ? `<p class="cn-err" id="f-client-name-err" role="alert">Enter a name for the client.</p>` : ""}
    ${state.error ? errorBox(state.error) : ""}
    <div class="btn-row"><button type="submit" class="btn primary sm" id="f-client-add">Add client</button><button type="button" class="btn quiet sm" id="f-client-cancel">Cancel</button></div>
  </form>`;
}

/** The top bar's context slot: "Client: [name ▾]", or nothing where the client means nothing. */
export function clientPickerHtml() {
  if (typeof window === "undefined" || !perClientRoute(window.location.hash)) return "";
  injectStyles();
  if (state.loading) {
    return `<div class="clientpick" data-client-picker><span class="clabel">Client</span><span class="cn-wait">Loading…</span></div>`;
  }
  if (!state.clients.length) {
    // The list could not be read: say so quietly, with the reason under Details.
    return `<div class="clientpick" data-client-picker><span class="clabel">Client</span><span class="cn-wait">Not available</span></div>${
      state.error ? errorBox(state.error) : ""
    }`;
  }
  const options = orderedClients(state.clients, state.practiceId)
    .map(
      (client) =>
        `<option value="${esc(client.client_id)}"${client.client_id === state.current ? " selected" : ""}>${esc(
          label(client),
        )}</option>`,
    )
    .join("");
  const addOption = mayAddClient() ? `<option value="${NEW_CLIENT}">+ New client</option>` : "";
  return `<div class="clientpick" data-client-picker><label class="clabel" for="f-client">Client</label><div class="control sel"><select id="f-client">${options}${addOption}</select></div>${toggletip(
    HINT,
    "Client",
  )}</div>${state.adding ? newClientForm() : state.error ? errorBox(state.error) : ""}`;
}

function focusName() {
  setTimeout(() => {
    const input = document.getElementById("f-client-name");
    if (input) input.focus();
  }, 0);
}

document.addEventListener("change", (event) => {
  if (!event.target || event.target.id !== "f-client") return;
  if (event.target.value === NEW_CLIENT) {
    event.target.value = state.current || "";
    state.adding = true;
    state.nameMissing = false;
    state.error = null;
    announceModulesChanged();
    focusName();
    return;
  }
  select(event.target.value);
});

function submitName() {
  const input = document.getElementById("f-client-name");
  const name = input ? input.value.trim() : "";
  if (!name) {
    state.nameMissing = true;
    announceModulesChanged();
    focusName();
    return;
  }
  add(name);
}

document.addEventListener("submit", (event) => {
  if (!event.target || event.target.id !== "f-client-form") return;
  event.preventDefault();
  submitName();
});

document.addEventListener("click", (event) => {
  const target = event.target && event.target.closest ? event.target.closest("button") : null;
  if (!target) return;
  if (target.id === "f-client-cancel") {
    closeForm();
  } else if (target.id === "f-client-add" && !target.closest("form")) {
    submitName(); // the button outside a form (never drawn today); inside one, `submit` handles it
  }
});

document.addEventListener("keydown", (event) => {
  if (event.key !== "Escape" || !state.adding) return;
  const target = event.target;
  if (target && typeof target.closest === "function" && target.closest("#f-client-form")) closeForm();
});

// The form under the chooser, and the "Loading…" / "Not available" text beside it. The chooser itself
// uses `index.html`'s `.clientpick` rules; tokens only, so both themes apply.
const CSS = `
.tb-context .client-new{gap:8px;width:300px;max-width:calc(100vw - 32px);padding:16px}
.client-new .cn-label{font-size:12px;font-weight:500;color:var(--ink2)}
.client-new .control{height:36px}
.client-new .control input{width:100%;height:100%;border:0;background:transparent;color:inherit;font:inherit;font-size:13px;padding:0 12px}
.client-new .cn-err{margin:0;font-size:12px;color:var(--bad)}
.client-new .apierr{position:static;width:auto;margin-top:0}
.client-new .btn-row{margin-top:4px}
.clientpick .cn-wait{font-size:12px;color:var(--muted)}
@media (max-width:700px){.tb-context .clientpick .toggletip{display:none}.tb-context .clientpick .control{width:140px}}
`;

function injectStyles() {
  if (document.getElementById("cn-styles")) return;
  const style = document.createElement("style");
  style.id = "cn-styles";
  style.textContent = CSS;
  document.head.appendChild(style);
}

load();
