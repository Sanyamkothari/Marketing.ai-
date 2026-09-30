// The AI service screens: `#/connections/ai/product` and `#/connections/ai/deliverable`, one form, two
// slots (`GET /ai-service` answers `{ slots: { product, deliverable }, providers }`).
//
//   Product AI      helps the team prepare data in Guided setup (the chat helper). It sees column names
//                   and masked samples of the customer's data.
//   Deliverable AI  what the customer receives: the Onboarding Assistant's answers, root-cause summaries
//                   and campaign copy. It uses the Product AI until it is given its own service.
//
// Neither is the model that scores your data; that is trained on your files.
//
// One form, plain steps: pick a service, paste its key (or, for Amazon Bedrock, use the AWS sign-in
// this machine already has), type or load the model, Test, Save. `GET /ai-service` says what is
// connected, the last test, whether this person may change it (`editable`) and the provider list with
// each provider's key format, help text and model examples, so nothing about a provider is written
// here: a provider added on the server appears as one more card.
//
// The key: a password input, `autocomplete="off"`, that is read only at the moment of a save, a test
// or a model listing, sent in the request body and nowhere else, and never stored by this module. The
// API never sends it back (only `has_key`), so a saved key is shown as "Saved" and a blank field
// keeps it. The input is cleared after a save, and when the provider changes. The soft format hint
// (an unexpected start) warns and never blocks. Test and model calls repaint only their own part of
// the screen, so what someone typed survives them.
//
// Every string from the API goes through `esc()` (or a text node); nothing is put in with `innerHTML`
// unescaped.

import { CONNECTIONS_CRUMB, announceStatus, crumbs, errorBox, esc, fmtStamp, notFound, pageHead, skeleton } from "../../dom.js";
import { forgetAiServiceStatus } from "../../availability.js";
import { deleteAiService, getAiService, listAiModels, putAiService, testAiService } from "./api.js";
import { injectConnectionStyles } from "./styles.js";

const BEDROCK_SIGN_IN = "#/generative/connection";

// --- the two slots -------------------------------------------------------------------------------

export const SLOTS = Object.freeze({
  product: {
    id: "product",
    title: "Product AI",
    blurb: "Helps your team prepare data in Guided setup. It sees column names and masked samples of the customer's data.",
    privacy: (label) => `Your prompts (including column names and masked samples from Guided setup) are sent to ${label}.`,
  },
  deliverable: {
    id: "deliverable",
    title: "Deliverable AI",
    blurb:
      "What your customer gets: the Onboarding Assistant's answers, root-cause summaries and campaign copy. Use the customer's own account if they need their data to stay there.",
    privacy: (label) =>
      `The questions asked, the passages of documents they are answered from, and the results being summarised are sent to ${label}.`,
  },
});

export const slotHref = (slot) => `#/connections/ai/${slot}`;

// --- what the Connections page shows -------------------------------------------------------------

/** A slot's badge: Not connected / Connected · service · model / Failed: reason (an inherited one says so). */
export function aiStatusBadge(ai) {
  if (!ai) return `<span class="chip neutral cn-badge" data-cn-status="unknown">Status not available</span>`;
  if (ai.has_key && ai.key_readable === false) {
    return `<span class="pill warn cn-badge" data-cn-status="key_unreadable">Enter the key again</span>`;
  }
  if (!ai.connected) return `<span class="pill warn cn-badge" data-cn-status="not_connected">Not connected</span>`;
  const test = ai.last_test;
  if (test && test.ok === false) {
    return `<span class="pill bad cn-badge" data-cn-status="failed">${esc(`Failed: ${test.message || "see the test"}`)}</span>`;
  }
  const via = ai.source === "inherited" ? "Uses the Product AI" : "Connected";
  const words = [via, ai.provider_label || ai.provider, ai.model].filter(Boolean).join(" · ");
  return `<span class="pill ok cn-badge" data-cn-status="connected">${esc(words)}</span>`;
}

// --- state ---------------------------------------------------------------------------------------

/** The screen's one state. Never holds a key: that is read from its input when a call is made. */
const s = {
  slot: "product",
  payload: null, // the whole `GET /ai-service` answer
  data: null, // this slot's state
  own: false, // Deliverable: the person chose to give it its own service rather than use the Product AI's
  loadError: null,
  provider: "", // the card picked ("" = none yet)
  values: { model: "", embedding_model: "", base_url: "", region: "" },
  dirty: false, // anything changed since the saved state was drawn
  models: [], // what "Load models" found, as datalist options
  modelsNote: "",
  modelsBusy: false,
  saving: false,
  testing: false,
  testedUnsaved: false, // the shown result tested what is on screen, not what is saved
  result: null, // { ok, message, fix, latency_ms, model, embeddings_note } or the saved `last_test`
  resultError: null,
  saveError: null, // an ApiError from a save or a disconnect
  problem: "", // what is missing on screen, said before any call
  confirming: false,
  advancedOpen: false,
};

let view = { app: null, token: 0 };

const providers = () => (s.payload && s.payload.providers) || [];
const inherited = () => Boolean(s.data) && s.data.source === "inherited" && !s.own;
const productConnected = () => Boolean(s.payload && s.payload.slots && s.payload.slots.product && s.payload.slots.product.connected);
const providerOf = (id) => providers().find((p) => p.id === id) || null;
const current = () => providerOf(s.provider);
const editable = () => Boolean(s.data) && s.data.editable !== false;

/** The saved service is the picked provider's; its key counts only while the server can still read it. */
const savedIs = (p) => Boolean(s.data && p && s.data.provider === p.id && s.data.source !== "inherited");
const keySaved = (p) => savedIs(p) && s.data.has_key === true && s.data.key_readable !== false;
const keyUnreadable = (p) => savedIs(p) && s.data.has_key === true && s.data.key_readable === false;

function valuesFromSaved(p) {
  const blank = { model: "", embedding_model: "", base_url: "", region: "" };
  if (!p || !savedIs(p)) return blank;
  const d = s.data;
  return {
    model: d.model || "",
    embedding_model: d.embedding_model || "",
    base_url: d.base_url && (p.needs_base_url || d.base_url !== p.default_base_url) ? d.base_url : "",
    region: d.region || "",
  };
}

const advancedNeeded = (p) => Boolean(p && (s.values.embedding_model || (s.values.base_url && !p.needs_base_url)));

function resetForm() {
  const own = s.data && s.data.source !== "inherited";
  const saved = own && s.data.provider && providerOf(s.data.provider) ? s.data.provider : "";
  s.provider = saved;
  s.values = valuesFromSaved(providerOf(saved));
  s.dirty = false;
  s.models = [];
  s.modelsNote = "";
  s.result = s.data && s.data.last_test ? s.data.last_test : null;
  s.testedUnsaved = false;
  s.resultError = null;
  s.saveError = null;
  s.problem = "";
  s.confirming = false;
  s.own = false;
  s.advancedOpen = advancedNeeded(providerOf(saved));
}

// --- pieces --------------------------------------------------------------------------------------

const thirdPartyHtml = (label) =>
  `<p class="ai-privacy" data-ai-third-party role="note"><b>Privacy.</b> ${esc(SLOTS[s.slot].privacy(label))}</p>`;

function providerCard(p) {
  const on = p.id === s.provider;
  const sub = p.protocol === "bedrock" ? "AWS sign-in, no key" : p.needs_base_url ? "Your own address" : "API key";
  return `<label class="ai-pcard${on ? " on" : ""}" data-ai-provider-card="${esc(p.id)}"><input type="radio" name="ai-provider" value="${esc(
    p.id,
  )}" data-ai-provider${on ? " checked" : ""}><span class="ai-pname">${esc(p.label)}</span><span class="ai-psub">${esc(sub)}</span></label>`;
}

const optionsHtml = (list) => list.map((m) => `<option value="${esc(m)}"></option>`).join("");

function keyFieldHtml(p) {
  const kept = keySaved(p);
  const optional = p.needs_key ? "" : ` <span class="sub">(optional)</span>`;
  const looks = p.key_hint ? `Looks like <code class="colchip">${esc(p.key_hint)}</code>. ` : "";
  const note = kept
    ? `<p class="ai-saved" data-ai-saved><span aria-hidden="true">✓</span> Saved. Leave blank to keep it.</p>`
    : keyUnreadable(p)
      ? `<p class="ai-warn" data-ai-key-unreadable role="status">The saved key can no longer be read. Enter the key again.</p>`
      : "";
  return `<div class="field ai-wide"><label for="ai-f-api_key">API key${optional}</label><div class="control"><input type="password" id="ai-f-api_key" name="ai-api-key" data-ai-input autocomplete="off" spellcheck="false" autocapitalize="off" placeholder="${esc(
    kept ? "Saved. Leave blank to keep it." : p.key_hint || "",
  )}" aria-describedby="ai-key-help ai-key-fmt"></div><span class="sub" id="ai-key-help">${looks}${esc(
    p.key_help || "",
  )}</span>${note}<p class="ai-warn" id="ai-key-fmt" role="status" hidden></p></div>`;
}

const modelsStatus = () => {
  if (s.modelsBusy) return "Asking the service…";
  if (s.models.length) {
    const count = `${s.models.length} model${s.models.length === 1 ? "" : "s"} found. Pick one from the list in the box.`;
    return s.modelsNote ? `${count} ${s.modelsNote}` : count;
  }
  return s.modelsNote || "";
};

function fieldsHtml() {
  const p = current();
  if (!p) return `<p class="ai-pick" data-ai-pick>Pick a service above to continue.</p>`;
  const bedrock = p.protocol === "bedrock";
  const parts = [];
  if (bedrock) {
    parts.push(
      `<p class="ai-note" data-ai-bedrock>Amazon Bedrock uses the AWS sign-in this computer or role already has, so there is no key to enter. <a href="${BEDROCK_SIGN_IN}">Choose or check the AWS sign-in</a></p>`,
    );
  } else {
    parts.push(keyFieldHtml(p));
  }
  if (p.needs_base_url) {
    parts.push(
      `<div class="field ai-wide"><label for="ai-f-base_url">Address of the service</label><div class="control"><input type="text" id="ai-f-base_url" data-ai-input data-ai-field="base_url" value="${esc(
        s.values.base_url,
      )}" placeholder="${esc(p.default_base_url || "https://…")}" autocomplete="off" spellcheck="false" inputmode="url"></div><span class="sub">Where your service listens, such as a local server or a company gateway.</span></div>`,
    );
  }
  if (p.needs_region) {
    parts.push(
      `<div class="field"><label for="ai-f-region">AWS region</label><div class="control"><input type="text" id="ai-f-region" data-ai-input data-ai-field="region" value="${esc(
        s.values.region,
      )}" placeholder="For example us-east-1" autocomplete="off" spellcheck="false"></div></div>`,
    );
  }
  const example = (p.model_suggestions || [])[0];
  parts.push(
    `<div class="field ai-wide"><label for="ai-f-model">${bedrock ? "Model ID" : "Model"}</label><div class="control"><input type="text" id="ai-f-model" data-ai-input data-ai-field="model" list="ai-models-list" value="${esc(
      s.values.model,
    )}" placeholder="${esc(example ? `For example ${example}` : "")}" autocomplete="off" spellcheck="false" aria-describedby="ai-model-help"></div><datalist id="ai-models-list">${optionsHtml(
      s.models.length ? s.models : p.model_suggestions || [],
    )}</datalist><span class="sub" id="ai-model-help">The suggestions are examples. Type the exact name your account can use, or load the list from the service.</span><div class="ai-row"><button type="button" class="btn secondary sm" id="ai-models"${
      s.modelsBusy ? " disabled" : ""
    }>${s.modelsBusy ? "Loading…" : "Load models from this service"}</button><span class="sub" id="ai-models-status" role="status">${esc(
      modelsStatus(),
    )}</span></div></div>`,
  );
  const embedding = p.supports_embeddings
    ? `<div class="field ai-wide"><label for="ai-f-embedding_model">Embedding model <span class="sub">(optional)</span></label><div class="control"><input type="text" id="ai-f-embedding_model" data-ai-input data-ai-field="embedding_model" list="ai-embed-list" value="${esc(
        s.values.embedding_model,
      )}" placeholder="${esc((p.embedding_suggestions || [])[0] || "")}" autocomplete="off" spellcheck="false"></div><datalist id="ai-embed-list">${optionsHtml(
        p.embedding_suggestions || [],
      )}</datalist><span class="sub">Used to search your documents. Leave blank and the document assistant will match by keywords.</span></div>`
    : `<p class="ai-note" data-ai-keywords>${esc(p.label)} has no embeddings. The document assistant will match by keywords.</p>`;
  const overrideBase = !p.needs_base_url && !bedrock;
  const base = overrideBase
    ? `<div class="field ai-wide"><label for="ai-f-base_url">Address of the service <span class="sub">(optional)</span></label><div class="control"><input type="text" id="ai-f-base_url" data-ai-input data-ai-field="base_url" value="${esc(
        s.values.base_url,
      )}" placeholder="${esc(p.default_base_url || "")}" autocomplete="off" spellcheck="false" inputmode="url"></div><span class="sub">Only change this to go through a proxy or gateway.</span></div>`
    : "";
  parts.push(
    `<details class="adv ai-adv" id="ai-adv"${s.advancedOpen ? " open" : ""}><summary>Advanced</summary><div class="cn-advbody">${embedding}${base}</div></details>`,
  );
  if (p.third_party) parts.push(thirdPartyHtml(p.label));
  return parts.join("");
}

function resultHtml() {
  if (s.testing) return `<p class="loading" role="status" data-ai-testing>Testing…</p>`;
  if (s.resultError) return errorBox(s.resultError);
  const r = s.result;
  if (!r) return "";
  const facts = [
    r.tested_at ? `Last tested ${fmtStamp(r.tested_at)}` : "",
    r.latency_ms !== null && r.latency_ms !== undefined ? `answered in ${r.latency_ms} ms` : "",
    r.model ? `model ${r.model}` : "",
  ].filter(Boolean);
  const line = facts.length ? `${facts.join(", ")}.`.replace(/^./, (c) => c.toUpperCase()) : "";
  const unsaved = s.testedUnsaved ? `<p class="sub" data-ai-unsaved>This tested the values above, which are not saved yet.</p>` : "";
  return `<div class="ai-result ${r.ok ? "ok" : "bad"}" data-ai-result="${r.ok ? "ok" : "failed"}"><p class="ai-verdict"><span aria-hidden="true">${
    r.ok ? "✓" : "✕"
  }</span> <span class="sr">${r.ok ? "Passed. " : "Failed. "}</span>${esc(r.message || (r.ok ? "It works." : "The test failed."))}</p>${
    r.fix ? `<p class="ai-fix" data-ai-fix>${esc(r.fix)}</p>` : ""
  }${line ? `<p class="sub">${esc(line)}</p>` : ""}${
    r.embeddings_note ? `<p class="sub" data-ai-embeddings-note>${esc(r.embeddings_note)}</p>` : ""
  }${unsaved}</div>`;
}

function readOnlyHtml() {
  const d = s.data;
  const row = (label, value) => `<div><dt>${esc(label)}</dt><dd>${esc(value)}</dd></div>`;
  const rows = [
    row("Service", d.provider_label || d.provider || "None"),
    d.model ? row("Model", d.model) : "",
    d.embedding_model ? row("Embedding model", d.embedding_model) : "",
    d.region ? row("Region", d.region) : "",
    d.base_url ? row("Address", d.base_url) : "",
    d.provider && d.provider !== "bedrock" ? row("Key", d.has_key ? "Saved" : "None") : "",
  ].join("");
  return `<section class="card notice-card" role="status" data-locked><h3>This is managed by whoever runs Marketing AI</h3><p>${esc(
    d.locked_reason || "It is set on the server, so it cannot be changed from this screen.",
  )}</p>${d.connected ? `<dl class="ai-facts" data-ai-facts>${rows}</dl>` : `<p>No AI service is connected.</p>`}</section>`;
}

function disconnectHtml() {
  if (!s.data || s.data.source !== "saved") return "";
  const label = "Disconnect";
  if (s.confirming) {
    const ask = "Disconnect? Text features stop until you connect one again.";
    return `<span class="cn-confirm" role="group" aria-label="${esc(label)}?"><span>${esc(ask)}</span><button type="button" class="btn danger confirm sm" id="ai-disconnect-yes">${esc(
      "Disconnect",
    )}</button><button type="button" class="btn quiet sm" id="ai-disconnect-no">Keep it</button></span>`;
  }
  return `<button type="button" class="btn quiet" id="ai-disconnect">${esc(label)}</button>`;
}

function sourceNoteHtml() {
  if (!s.data || s.data.source !== "config") return "";
  return `<p class="ai-note" data-ai-config>${esc(
    `Now using ${s.data.provider_label || "the AI service"} as set in this installation's configuration. Save a service here to use that instead.`,
  )}</p>`;
}

/** What is wrong before any call, then what the server refused a save with. */
const errorsHtml = () =>
  `${s.problem ? `<p class="ai-problem" data-ai-problem role="alert">${esc(s.problem)}</p>` : ""}${s.saveError ? errorBox(s.saveError) : ""}`;

/** Whether Test has something to test: something on screen, or something saved. */
const canTest = () => Boolean(s.provider) && (s.dirty || Boolean(s.data && s.data.connected));

function headHtml() {
  const slot = SLOTS[s.slot];
  return pageHead(
    `${crumbs([CONNECTIONS_CRUMB, { label: slot.title }])}<h1 class="h1">${esc(slot.title)}</h1><p class="desc" data-ai-blurb>${esc(
      slot.blurb,
    )}</p><p class="sub">It does not train or score your models.</p>`,
  );
}

/** Deliverable while it uses the Product AI's service: say so, test it, and offer its own. */
function inheritedHtml(head, status, out) {
  const d = s.data;
  const what = [d.provider_label || d.provider, d.model].filter(Boolean).join(" · ");
  return `<main class="screen cn ai" data-ai-slot="${esc(s.slot)}">${head}${status}<section class="card notice-card" role="status" data-ai-inherited><h3>Uses the Product AI unless you set its own</h3><p>${esc(
    what ? `Right now it uses ${what}.` : "Right now it uses the service connected as the Product AI.",
  )}</p><div class="notice-actions"><button type="button" class="btn secondary" id="ai-use-own">Use its own</button><button type="button" class="btn quiet" id="ai-test">Test</button></div></section>${
    d.third_party ? thirdPartyHtml(d.provider_label || "the AI service") : ""
  }${out}</main>`;
}

function mainHtml() {
  const head = headHtml();
  if (s.loadError) return `<main class="screen cn ai">${head}${errorBox(s.loadError, { retry: true })}</main>`;
  const status = `<p class="ai-status" id="ai-status" data-ai-status>${aiStatusBadge(s.data)}</p>`;
  const out = `<section class="ai-out" id="ai-result" aria-live="polite">${resultHtml()}</section>`;
  if (!editable()) {
    return `<main class="screen cn ai" data-ai-slot="${esc(s.slot)}">${head}${status}${readOnlyHtml()}${
      s.data.third_party ? thirdPartyHtml(s.data.provider_label || "the AI service") : ""
    }<div class="actions ai-actions"><button type="button" class="btn primary" id="ai-test"${
      s.data.connected ? "" : " disabled"
    }>Test</button></div>${out}</main>`;
  }
  if (s.slot === "deliverable" && inherited()) return inheritedHtml(head, status, out);
  const own =
    s.slot === "deliverable" && s.data.source !== "saved"
      ? `<p class="ai-note" data-ai-own>${esc("Connect the service the customer's deliverables use.")}</p>`
      : "";
  return `<main class="screen cn ai" data-ai-slot="${esc(s.slot)}">${head}${status}${sourceNoteHtml()}${own}
    <form class="cn-form ai-form" id="ai-form" novalidate>
      <fieldset class="ai-providers"><legend>Which AI service?</legend><div class="ai-pgrid">${providers().map(providerCard).join("")}</div></fieldset>
      <div class="ai-fields" id="ai-fields">${fieldsHtml()}</div>
      <div id="ai-error">${errorsHtml()}</div>
      <div class="actions ai-actions"><button type="submit" class="btn primary" id="ai-save"${s.saving || !s.provider ? " disabled" : ""}>${
        s.saving ? "Saving…" : "Save"
      }</button><button type="button" class="btn secondary" id="ai-test"${s.testing || s.saving || !canTest() ? " disabled" : ""}>${
        s.testing ? "Testing…" : "Test"
      }</button><span class="spacer"></span><span id="ai-disconnect-zone">${disconnectHtml()}</span></div>
    </form>
    ${out}
  </main>`;
}

// --- reading the form ----------------------------------------------------------------------------

const $ = (selector) => (view.app ? view.app.querySelector(selector) : null);

/** The typed key, read at the moment of use and handed straight to a request; never kept. */
const typedKey = () => {
  const input = $("#ai-f-api_key");
  return input ? input.value.trim() : "";
};

function readValues() {
  view.app.querySelectorAll("[data-ai-field]").forEach((input) => {
    s.values[input.dataset.aiField] = input.value.trim();
  });
}

/** What is on screen as the API takes it: only the fields this provider uses, a blank key left out. */
function bodyFromForm(p) {
  readValues();
  const body = { provider: p.id, model: s.values.model };
  const key = typedKey();
  if (key !== "") body.api_key = key;
  if (p.needs_region) body.region = s.values.region;
  if (p.protocol !== "bedrock" && s.values.base_url) body.base_url = s.values.base_url;
  if (p.supports_embeddings && s.values.embedding_model) body.embedding_model = s.values.embedding_model;
  return body;
}

/** The first thing missing before a save or a test, so the person is not sent to the server to be told. */
function missing(p, body) {
  if (!body.model) return { field: "model", message: "Enter the model to use, or load the list from the service." };
  if (p.needs_base_url && !body.base_url) return { field: "base_url", message: "Enter the address of the service." };
  if (p.needs_region && !body.region) return { field: "region", message: "Enter the AWS region." };
  if (p.needs_key && !body.api_key && !keySaved(p)) return { field: "api_key", message: "Enter the API key." };
  return null;
}

/** Mark the field a refusal names (never a value) and put the cursor there. */
function markInvalid(field) {
  view.app.querySelectorAll("[aria-invalid]").forEach((el) => el.removeAttribute("aria-invalid"));
  const input = field ? $(`#ai-f-${field}`) : null;
  if (!input) return;
  input.setAttribute("aria-invalid", "true");
  input.focus();
}

const fieldOf = (error) => {
  const detail = error && error.body && (error.body.detail || error.body);
  return detail && typeof detail.field === "string" ? detail.field : null;
};

// --- repainting parts ----------------------------------------------------------------------------

function paintResult() {
  const el = $("#ai-result");
  if (el) el.innerHTML = resultHtml();
}

function paintError() {
  const el = $("#ai-error");
  if (el) el.innerHTML = errorsHtml();
}

function paintStatus() {
  const el = $("#ai-status");
  if (el) el.innerHTML = aiStatusBadge(s.data);
}

function syncButtons() {
  const save = $("#ai-save");
  if (save) {
    save.disabled = s.saving || !s.provider;
    save.textContent = s.saving ? "Saving…" : "Save";
  }
  const test = $("#ai-test");
  if (test) {
    test.disabled = s.testing || s.saving || (editable() ? !canTest() : !(s.data && s.data.connected));
    test.textContent = s.testing ? "Testing…" : "Test";
  }
}

function paintModels() {
  const status = $("#ai-models-status");
  if (status) status.textContent = modelsStatus();
  const list = $("#ai-models-list");
  const p = current();
  if (list && p) {
    list.textContent = "";
    for (const name of s.models.length ? s.models : p.model_suggestions || []) {
      const option = document.createElement("option");
      option.value = name;
      list.appendChild(option);
    }
  }
  const button = $("#ai-models");
  if (button) {
    button.disabled = s.modelsBusy;
    button.textContent = s.modelsBusy ? "Loading…" : "Load models from this service";
  }
}

function paintFields() {
  const el = $("#ai-fields");
  if (el) el.innerHTML = fieldsHtml();
  bindFields();
  syncButtons();
}

function paintDisconnect() {
  const el = $("#ai-disconnect-zone");
  if (el) el.innerHTML = disconnectHtml();
  bindDisconnect();
}

function paintAll() {
  if (!view.app) return;
  injectConnectionStyles();
  view.app.innerHTML = mainHtml();
  bind();
}

// --- actions -------------------------------------------------------------------------------------

function pickProvider(id) {
  if (id === s.provider) return;
  s.provider = id;
  s.values = valuesFromSaved(providerOf(id)); // another service: its own settings, and the key field drawn empty
  s.models = [];
  s.modelsNote = "";
  s.dirty = true;
  s.result = null;
  s.resultError = null;
  s.saveError = null;
  s.problem = "";
  s.testedUnsaved = false;
  s.advancedOpen = advancedNeeded(providerOf(id));
  view.app.querySelectorAll(".ai-pcard").forEach((card) => card.classList.toggle("on", card.dataset.aiProviderCard === id));
  paintError();
  paintResult();
  paintFields();
}

async function loadModels() {
  const p = current();
  if (!p || s.modelsBusy) return;
  readValues();
  const body = { provider: p.id };
  const key = typedKey();
  if (key !== "") body.api_key = key;
  if (s.values.base_url && p.protocol !== "bedrock") body.base_url = s.values.base_url;
  if (s.values.region && p.needs_region) body.region = s.values.region;
  const token = view.token;
  s.modelsBusy = true;
  s.modelsNote = "";
  paintModels();
  try {
    const answer = await listAiModels(s.slot, body);
    if (token !== view.token) return;
    s.models = Array.isArray(answer.models) ? answer.models.filter((m) => typeof m === "string") : [];
    s.modelsNote = answer.note || (s.models.length ? "" : "The service listed no models. Type the name instead.");
  } catch {
    if (token !== view.token) return;
    s.models = [];
    s.modelsNote = "Could not load the list. Type the model name instead.";
  }
  s.modelsBusy = false;
  paintModels();
}

/** What a save or a disconnect answered, as this slot's state (the whole payload, or the state alone). */
function stateFrom(answer) {
  if (answer && answer.slots && answer.slots[s.slot]) {
    s.payload = { ...s.payload, ...answer };
    return answer.slots[s.slot];
  }
  const state = answer && typeof answer === "object" ? answer : s.data;
  s.payload = { ...s.payload, slots: { ...(s.payload && s.payload.slots), [s.slot]: state } };
  return state;
}

const putSlot = async (body) => stateFrom(await putAiService(s.slot, body));
const removeSlot = async () => stateFrom(await deleteAiService(s.slot));

/** After a test of what is saved the API has a new `last_test`: read the state again for the badge. */
async function refreshSaved(token) {
  try {
    const payload = await getAiService();
    if (token !== view.token) return;
    s.payload = payload;
    s.data = payload.slots[s.slot];
    forgetAiServiceStatus();
    paintStatus();
  } catch {
    // the badge keeps what it had; the result on screen is the answer that matters
  }
}

async function runTest() {
  if (s.testing) return;
  const p = current();
  let body;
  if (editable() && s.dirty) {
    if (!p) return;
    body = bodyFromForm(p);
    const gap = missing(p, body);
    if (gap) {
      s.problem = gap.message;
      paintError();
      markInvalid(gap.field);
      return;
    }
  }
  s.problem = "";
  paintError();
  const token = view.token;
  s.testing = true;
  s.resultError = null;
  s.testedUnsaved = body !== undefined;
  paintResult();
  syncButtons();
  try {
    const answer = await testAiService(s.slot, body);
    if (token !== view.token) return;
    s.result = answer;
    announceStatus(answer.ok ? "The AI service works" : "The test found a problem");
  } catch (error) {
    if (token !== view.token) return;
    s.result = null;
    s.resultError = error;
  }
  s.testing = false;
  paintResult();
  syncButtons();
  if (body === undefined) refreshSaved(token);
}

async function save() {
  const p = current();
  if (!p || s.saving) return;
  const body = bodyFromForm(p);
  s.saveError = null;
  s.problem = "";
  const gap = missing(p, body);
  if (gap) {
    s.problem = gap.message;
    paintError();
    markInvalid(gap.field);
    return;
  }
  const token = view.token;
  s.saving = true;
  paintError();
  syncButtons();
  try {
    s.data = await putSlot(body);
  } catch (error) {
    if (token !== view.token) return;
    s.saving = false;
    s.saveError = error;
    paintError();
    syncButtons();
    markInvalid(fieldOf(error));
    return;
  }
  if (token !== view.token) return;
  forgetAiServiceStatus();
  s.saving = false;
  resetForm(); // the saved state is the form now, and the key input is drawn empty
  paintAll();
  announceStatus("Saved");
  await runTest(); // saving makes no call to the service: check it works right away
}

async function disconnect() {
  const token = view.token;
  s.confirming = false;
  try {
    s.data = await removeSlot();
  } catch (error) {
    if (token !== view.token) return;
    s.saveError = error;
    paintError();
    paintDisconnect();
    return;
  }
  if (token !== view.token) return;
  forgetAiServiceStatus();
  resetForm();
  paintAll();
  announceStatus("AI service disconnected");
}

// --- events --------------------------------------------------------------------------------------

/** A soft hint when the key starts differently than this service's keys do. It never blocks. */
function checkKeyFormat() {
  const p = current();
  const note = $("#ai-key-fmt");
  if (!p || !note) return;
  const value = typedKey();
  const prefixes = p.key_prefixes || [];
  const odd = value !== "" && prefixes.length > 0 && !prefixes.some((x) => value.startsWith(x));
  note.hidden = !odd;
  note.textContent = odd
    ? `This does not look like a ${p.label} key (they usually start with ${prefixes.join(" or ")}). You can still save it.`
    : "";
}

function touch() {
  s.dirty = true;
  syncButtons();
}

function bindFields() {
  view.app.querySelectorAll("[data-ai-input]").forEach((input) => input.addEventListener("input", touch));
  const key = $("#ai-f-api_key");
  if (key) key.addEventListener("input", checkKeyFormat);
  const models = $("#ai-models");
  if (models) models.addEventListener("click", loadModels);
  const adv = $("#ai-adv");
  if (adv) adv.addEventListener("toggle", () => (s.advancedOpen = adv.open));
}

function bindDisconnect() {
  const ask = $("#ai-disconnect");
  if (ask) {
    ask.addEventListener("click", () => {
      s.confirming = true;
      paintDisconnect();
      const keep = $("#ai-disconnect-no");
      if (keep) keep.focus();
    });
  }
  const yes = $("#ai-disconnect-yes");
  if (yes) yes.addEventListener("click", disconnect);
  const no = $("#ai-disconnect-no");
  if (no) {
    no.addEventListener("click", () => {
      s.confirming = false;
      paintDisconnect();
      const again = $("#ai-disconnect");
      if (again) again.focus();
    });
  }
}

function bind() {
  const form = $("#ai-form");
  if (form) {
    form.addEventListener("submit", (event) => {
      event.preventDefault();
      save();
    });
    view.app.querySelectorAll("[data-ai-provider]").forEach((radio) =>
      radio.addEventListener("change", () => {
        if (radio.checked) pickProvider(radio.value);
      }),
    );
    bindFields();
    bindDisconnect();
  }
  const test = $("#ai-test");
  if (test) test.addEventListener("click", () => runTest());
  const own = $("#ai-use-own");
  if (own) {
    own.addEventListener("click", () => {
      s.own = true;
      s.result = null;
      paintAll();
      const first = $("[data-ai-provider]");
      if (first) first.focus();
    });
  }
  const back = $("#ai-use-inherited");
  if (back) {
    back.addEventListener("click", () => {
      resetForm();
      paintAll();
    });
  }
}

// --- the route -----------------------------------------------------------------------------------

/** `#/connections/ai/<slot>`: paint a skeleton, load the state, draw. */
export async function renderAiService(app, slot) {
  injectConnectionStyles();
  view = { app, token: view.token + 1 };
  const token = view.token;
  if (!SLOTS[slot]) {
    const back = { label: "Connections", href: "#/connections" };
    app.innerHTML = `<main class="screen cn">${pageHead(
      `${crumbs([CONNECTIONS_CRUMB, { label: "Not found" }])}<h1 class="h1">AI service not found</h1>`,
    )}${notFound("AI service", back)}</main>`;
    return;
  }
  s.slot = slot;
  app.innerHTML = skeleton("form", { title: SLOTS[slot].title });
  s.loadError = null;
  s.saving = false;
  s.testing = false;
  s.modelsBusy = false;
  try {
    s.payload = await getAiService();
    s.data = (s.payload.slots || {})[slot] || null;
    if (!s.data) throw Object.assign(new Error("The API did not describe this AI service."), { code: "UI_ERROR" });
  } catch (error) {
    s.data = null;
    s.loadError = error;
  }
  if (token !== view.token) return; // the person moved on
  resetForm();
  paintAll();
  document.title = `${SLOTS[slot].title} · Marketing AI`;
}
