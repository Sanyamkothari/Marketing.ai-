// The client picker in the page header (Plan A M35; prototype `clientPicker()`, screenshot
// `01-client-selector`).
//
// Tables, recipes and the datasets built from them are kept per client, so every onboarding call
// names one. This module owns which client that is: it asks the API for the default client ("Demo",
// created the first time it is asked for) and for the list, remembers the choice in this browser,
// and draws the `<select>` that `ui/dom.js`'s `pageHead` places beside the logo.
//
// The picker is drawn inside every screen's header, and every screen is repainted wholesale, so its
// events are read off `document` with one delegated listener each rather than bound per paint. A
// change of client is a change to what the current screen shows - the Setup form's raw-tables panel
// belongs to one client - so it asks the router to repaint the current route.

import { announceModulesChanged } from "../router.js";
import { createClient, ensureDefaultClient, listClients } from "./api.js";
import { errorBox, esc } from "../../dom.js";

const STORAGE_KEY = "marketing-ai.client";
const NEW_CLIENT = "__new__";

const state = {
  clients: [],
  current: null,
  loading: true,
  adding: false,
  error: null,
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

async function load() {
  try {
    const fallback = await ensureDefaultClient();
    const { clients } = await listClients();
    state.clients = clients || [];
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
    state.error = null;
    state.current = null; // so `select` below always announces, even if the id were reused
    select(clientId);
  } catch (error) {
    state.error = error;
    announceModulesChanged();
  }
}

/** The header tool `ui/dom.js` places beside the logo. */
export function clientPickerHtml() {
  if (state.loading) {
    return `<div class="clientpick" data-client-picker><span class="clabel">Client</span><span class="chint">Loading…</span></div>`;
  }
  if (state.adding) {
    return `<div class="clientpick" data-client-picker><span class="clabel">New client</span>
      <div class="control"><input id="f-client-name" aria-label="Name of the new client" autocomplete="off"></div>
      <button type="button" class="linkbtn" id="f-client-add">Add</button>
      <button type="button" class="linkbtn" id="f-client-cancel">Cancel</button></div>
      ${state.error ? errorBox(state.error) : ""}`;
  }
  const options = state.clients
    .map(
      (client) =>
        `<option value="${esc(client.client_id)}"${client.client_id === state.current ? " selected" : ""}>${esc(
          client.name,
        )}</option>`,
    )
    .join("");
  return `<div class="clientpick" data-client-picker><span class="clabel">Client</span><div class="control sel"><select id="f-client" aria-label="Client">${options}<option value="${NEW_CLIENT}">+ New client</option></select></div></div>
    <span class="chint">Tables and recipes are kept per client.</span>
    ${state.error ? errorBox(state.error) : ""}`;
}

document.addEventListener("change", (event) => {
  if (!event.target || event.target.id !== "f-client") return;
  if (event.target.value === NEW_CLIENT) {
    state.adding = true;
    announceModulesChanged();
    return;
  }
  select(event.target.value);
});

document.addEventListener("click", (event) => {
  const target = event.target && event.target.closest ? event.target.closest("button") : null;
  if (!target) return;
  if (target.id === "f-client-cancel") {
    state.adding = false;
    announceModulesChanged();
  } else if (target.id === "f-client-add") {
    const input = document.getElementById("f-client-name");
    const name = input ? input.value.trim() : "";
    if (name) add(name);
  }
});

load();
