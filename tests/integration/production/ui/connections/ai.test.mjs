/* The AI service screens (ui/modules/connections/ai.js: `#/connections/ai/product` and
   `#/connections/ai/deliverable`) in jsdom, against a hand-written fake of `/ai-service` built to the
   HTTP contract (the API is built in parallel; nothing here needs PB_FIXTURES). What is pinned: provider
   switching and each provider's key hint, the saved-key state, the model list, the test result, save,
   disconnect, the locked view, the third-party notice, the deliverable's "uses the Product AI" state,
   and that a key never appears in a rendered page, is cleared after a save, and is sent only in a body. */
import { test } from "node:test";
import assert from "node:assert/strict";
import { $, $$, installPage, until } from "../harness.mjs";

const KEY = "zk-test-0123456789abcdef"; // a made-up key, never a real one
const HOSTILE = `<img src=x onerror="window.__pwned=1">Evil`;

const PROVIDERS = [
  { id: "bedrock", label: "Amazon Bedrock", protocol: "bedrock", default_base_url: null, needs_key: false, needs_base_url: false, needs_region: true, key_hint: "", key_help: "", key_prefixes: [], model_suggestions: ["example.bedrock-model-v1"], embedding_suggestions: [], supports_embeddings: true, third_party: false },
  { id: "openai", label: "OpenAI", protocol: "openai", default_base_url: "https://api.example.test/v1", needs_key: true, needs_base_url: false, needs_region: false, key_hint: "sk-…", key_help: "Create a key on the provider's API keys page.", key_prefixes: ["sk-"], model_suggestions: ["test-model-1", "test-model-2"], embedding_suggestions: ["test-embed-1"], supports_embeddings: true, third_party: true },
  { id: "anthropic", label: "Claude (Anthropic)", protocol: "anthropic", default_base_url: "https://api.example.test", needs_key: true, needs_base_url: false, needs_region: false, key_hint: "sk-ant-…", key_help: "Create a key in the console.", key_prefixes: ["sk-ant-"], model_suggestions: ["claude-example"], embedding_suggestions: [], supports_embeddings: false, third_party: true },
  { id: "openrouter", label: "OpenRouter", protocol: "openai", default_base_url: "https://router.example.test/v1", needs_key: true, needs_base_url: false, needs_region: false, key_hint: "sk-or-…", key_help: "Create a key under Keys on the OpenRouter site.", key_prefixes: ["sk-or-"], model_suggestions: ["vendor/model-a"], embedding_suggestions: [], supports_embeddings: true, third_party: true },
  { id: "huggingface", label: "Hugging Face", protocol: "openai", default_base_url: "https://hf.example.test/v1", needs_key: true, needs_base_url: false, needs_region: false, key_hint: "hf_…", key_help: "Create a token in your settings.", key_prefixes: ["hf_"], model_suggestions: [], embedding_suggestions: [], supports_embeddings: false, third_party: true },
  { id: "openai_compatible", label: "Other / local", protocol: "openai", default_base_url: null, needs_key: false, needs_base_url: true, needs_region: false, key_hint: "", key_help: "A key is optional for a local server.", key_prefixes: [], model_suggestions: [], embedding_suggestions: [], supports_embeddings: true, third_party: true },
  { id: "hostile", label: HOSTILE, protocol: "openai", default_base_url: "https://h.example.test/v1", needs_key: true, needs_base_url: false, needs_region: false, key_hint: HOSTILE, key_help: HOSTILE, key_prefixes: [], model_suggestions: [HOSTILE], embedding_suggestions: [], supports_embeddings: false, third_party: true },
];

const none = { connected: false, source: "none", provider: null, provider_label: null, model: null, embedding_model: null, base_url: null, region: null, has_key: false, key_readable: true, third_party: false, last_test: null, editable: true, locked_reason: null };
const saved = (over = {}) => ({ ...none, connected: true, source: "saved", provider: "openai", provider_label: "OpenAI", model: "test-model-1", base_url: "https://api.example.test/v1", has_key: true, third_party: true, ...over });

let slots;
const reset = () => {
  slots = { product: saved(), deliverable: { ...saved(), source: "inherited", has_key: false, inherits_product: true } };
};
reset();

const seen = { puts: [], tests: [], models: [], deletes: [], gets: 0 };
let testAnswer = { ok: true, message: "It answered.", fix: null, latency_ms: 412, model: "test-model-1", embeddings_note: null };
let modelsAnswer = { models: ["test-model-1", "test-model-9"], note: null };
let putRefusal = null;

const server = (request) => {
  const { method, path, body } = request;
  if (method === "GET" && path === "/ai-service") {
    seen.gets += 1;
    return { status: 200, body: { slots, providers: PROVIDERS } };
  }
  const m = /^\/ai-service\/(product|deliverable)(?:\/(test|models))?$/.exec(path);
  if (!m) return null;
  const [, slot, action] = m;
  if (method === "PUT" && !action) {
    seen.puts.push({ slot, body });
    if (putRefusal) return putRefusal;
    const p = PROVIDERS.find((x) => x.id === body.provider);
    slots[slot] = saved({ provider: p.id, provider_label: p.label, model: body.model, region: body.region || null, base_url: body.base_url || p.default_base_url, embedding_model: body.embedding_model || null, has_key: Boolean(body.api_key) || slots[slot].has_key, third_party: p.third_party });
    return { status: 200, body: slots[slot] };
  }
  if (method === "POST" && action === "test") {
    seen.tests.push({ slot, body });
    return { status: 200, body: testAnswer };
  }
  if (method === "POST" && action === "models") {
    seen.models.push({ slot, body });
    return { status: 200, body: modelsAnswer };
  }
  if (method === "DELETE" && !action) {
    seen.deletes.push(slot);
    slots[slot] = slot === "deliverable" && slots.product.connected ? { ...slots.product, source: "inherited", has_key: false, inherits_product: true } : { ...none };
    return { status: 200, body: slots[slot] };
  }
  return null;
};

const { w } = installPage(server, { hash: "#/connections/ai/product" });
const router = await import("../../../../../ui/modules/router.js");
await import("../../../../../ui/modules/connections/index.js");
const availability = await import("../../../../../ui/availability.js");

const app = $("#app");
const text = (el) => (el ? el.textContent.replace(/\s+/g, " ").trim() : "");
async function show(slot) {
  const hash = `#/connections/ai/${slot}`;
  w.history.replaceState(null, "", `/ui/${hash}`);
  const parts = hash.replace(/^#\//, "").split("/");
  const module = router.resolveRoute(parts);
  assert.ok(module, "the connections module claims the route");
  await module.render(app, parts);
}
const pick = (id) => {
  const radio = $(`[data-ai-provider][value="${id}"]`);
  radio.checked = true;
  radio.dispatchEvent(new w.Event("change", { bubbles: true }));
};
const type = (selector, value) => {
  const el = $(selector);
  el.value = value;
  el.dispatchEvent(new w.Event("input", { bubbles: true }));
};
const click = (selector) => $(selector).click();
const submit = () => $("#ai-form").dispatchEvent(new w.Event("submit", { bubbles: true, cancelable: true }));
const facts = () => Object.fromEntries($$("[data-ai-facts] > div").map((row) => [text(row.querySelector("dt")), text(row.querySelector("dd"))]));
const words = () => $$(`[data-ai-provider]`).map((r) => r.value);

test("the screens are routes of the Connections module; an unknown slot is not found", async () => {
  await show("product");
  assert.equal(text($("h1")), "Product AI");
  await show("nonsense");
  assert.match(text($("h1")), /not found/i);
  assert.ok($('a[href="#/connections"]'));
});

test("each slot says what it is for", async () => {
  await show("product");
  assert.equal(
    text($("[data-ai-blurb]")),
    "Helps your team prepare data in Guided setup. It sees column names and masked samples of the customer's data.",
  );
  assert.match(text($("h1")), /^Product AI$/);
  assert.deepEqual(
    $$(".crumbs a, .crumbs .cur").map((e) => [text(e), e.getAttribute("href")]),
    [["Connections", "#/connections"], ["Product AI", null]],
  );
  reset();
  await show("deliverable");
  assert.equal(text($("h1")), "Deliverable AI");
  assert.match(text($("[data-ai-blurb]")), /^What your customer gets: the Onboarding Assistant's answers, root-cause summaries and campaign copy\. Use the customer's own account if they need their data to stay there\.$/);
});

test("every provider the API lists is a radio card, and the saved one is picked", async () => {
  reset();
  await show("product");
  assert.deepEqual(words(), PROVIDERS.map((p) => p.id));
  assert.equal($("[data-ai-provider]:checked").value, "openai");
  assert.equal($$("fieldset.ai-providers legend").length, 1, "a real fieldset with a legend: one radio group");
  assert.equal(text($("#ai-status")), "Connected · OpenAI · test-model-1");
});

test("the key field is a password input, autocomplete off, with the provider's format and help", async () => {
  reset();
  slots.product = { ...none };
  await show("product");
  assert.ok($("[data-ai-pick]"), "nothing picked yet: one line asking to pick");
  assert.equal($("#ai-save").disabled, true);
  pick("openrouter");
  const key = $("#ai-f-api_key");
  assert.equal(key.type, "password");
  assert.equal(key.getAttribute("autocomplete"), "off");
  assert.equal(key.getAttribute("placeholder"), "sk-or-…");
  assert.match(text($("#ai-key-help")), /Looks like sk-or-…\. Create a key under Keys on the OpenRouter site\./);
  assert.equal(key.value, "");
});

test("switching provider redraws its own fields and empties a typed key", async () => {
  pick("openai");
  type("#ai-f-api_key", KEY);
  assert.equal($("#ai-f-api_key").value, KEY);
  pick("openrouter");
  assert.equal($("#ai-f-api_key").value, "", "a key typed for one service is not carried to another");
  assert.match(text($("#ai-key-help")), /sk-or-…/);
  assert.ok(!app.innerHTML.includes(KEY));
  assert.equal($$(".ai-pcard.on").length, 1);
  assert.equal($(".ai-pcard.on").dataset.aiProviderCard, "openrouter");
});

test("the model box offers the provider's examples as a datalist and stays editable text", async () => {
  pick("openai");
  const model = $("#ai-f-model");
  assert.equal(model.type, "text");
  assert.equal(model.getAttribute("list"), "ai-models-list");
  assert.deepEqual($$("#ai-models-list option").map((o) => o.value), ["test-model-1", "test-model-2"]);
  assert.match(text($("#ai-model-help")), /examples/);
  type("#ai-f-model", "my-own-model");
  assert.equal($("#ai-f-model").value, "my-own-model");
});

test("Amazon Bedrock has no key field, asks for a region and links to the AWS sign-in", async () => {
  pick("bedrock");
  assert.equal($("#ai-f-api_key"), null, "no key to enter");
  assert.ok($("#ai-f-region"));
  assert.equal($("[data-ai-bedrock] a").getAttribute("href"), "#/generative/connection");
  assert.equal(text($('label[for="ai-f-model"]')), "Model ID");
  assert.equal($("[data-ai-third-party]"), null, "Bedrock is not a third party");
});

test("Other / local asks for the address, and a key is optional", async () => {
  pick("openai_compatible");
  assert.ok($("#ai-f-base_url"));
  assert.match(text($('label[for="ai-f-api_key"]')), /API key \(optional\)/);
  type("#ai-f-model", "local-model");
  submit();
  assert.match(text($("[data-ai-problem]")), /Enter the address of the service/);
  assert.equal($("#ai-f-base_url").getAttribute("aria-invalid"), "true");
  assert.equal(seen.puts.length, 0, "nothing is sent while a required field is empty");
});

test("a service without embeddings says the document assistant matches by keywords", async () => {
  pick("anthropic");
  assert.equal($("#ai-f-embedding_model"), null);
  assert.equal(text($("[data-ai-keywords]")), "Claude (Anthropic) has no embeddings. The document assistant will match by keywords.");
  pick("openai");
  assert.ok($("#ai-f-embedding_model"));
  assert.match(text($("#ai-adv")), /Leave blank and the document assistant will match by keywords/);
  assert.equal($("#ai-adv").open, false, "embedding and the address override are under Advanced");
  assert.ok($("#ai-adv #ai-f-base_url"));
});

test("the third-party notice names the service and what is sent, and differs by slot", async () => {
  reset();
  await show("product");
  assert.equal(
    text($("[data-ai-third-party]")),
    "Privacy. Your prompts (including column names and masked samples from Guided setup) are sent to OpenAI.",
  );
  pick("anthropic");
  assert.match(text($("[data-ai-third-party]")), /are sent to Claude \(Anthropic\)\.$/);
  await show("deliverable");
  $("#ai-use-own").click();
  pick("openai");
  assert.doesNotMatch(text($("[data-ai-third-party]")), /Guided setup/);
  assert.match(text($("[data-ai-third-party]")), /sent to OpenAI\.$/);
});

test("a saved key is shown as saved, never rendered back, and a blank field keeps it", async () => {
  reset();
  await show("product");
  const note = $("[data-ai-saved]");
  assert.match(text(note), /Saved\. Leave blank to keep it\./);
  assert.equal($("#ai-f-api_key").value, "");
  assert.equal($("#ai-f-api_key").getAttribute("placeholder"), "Saved. Leave blank to keep it.");
  seen.puts.length = 0;
  type("#ai-f-model", "test-model-2");
  submit();
  await until(() => seen.puts.length === 1, 2000, "the save");
  assert.equal("api_key" in seen.puts[0].body, false, "a blank key is left out, so the server keeps the saved one");
  assert.deepEqual(seen.puts[0].body, { provider: "openai", model: "test-model-2" }, "the provider's own address is not sent again");
  assert.equal(seen.puts[0].slot, "product");
  await until(() => $('[data-ai-result]'), 2000, "the test after saving");
});

test("switching to another service does not claim a key is saved for it", async () => {
  reset();
  await show("product");
  pick("anthropic");
  assert.equal($("[data-ai-saved]"), null);
  assert.equal($("#ai-f-api_key").getAttribute("placeholder"), "sk-ant-…");
  pick("openai");
  assert.ok($("[data-ai-saved]"), "back to the saved one: its key is still kept");
});

test("a key the server can no longer read asks for it again and needs it to save", async () => {
  reset();
  slots.product = saved({ key_readable: false, connected: false });
  await show("product");
  assert.equal(text($("#ai-status")), "Enter the key again");
  assert.match(text($("[data-ai-key-unreadable]")), /can no longer be read\. Enter the key again\./);
  type("#ai-f-model", "test-model-1");
  seen.puts.length = 0;
  submit();
  assert.match(text($("[data-ai-problem]")), /Enter the API key/);
  assert.equal(seen.puts.length, 0);
});

test("a key that starts oddly gets a soft hint and is still saved", async () => {
  reset();
  slots.product = { ...none };
  await show("product");
  pick("openrouter");
  assert.equal($("#ai-key-fmt").hidden, true);
  type("#ai-f-api_key", KEY);
  assert.equal($("#ai-key-fmt").hidden, false);
  assert.equal(text($("#ai-key-fmt")), "This does not look like a OpenRouter key (they usually start with sk-or-). You can still save it.");
  assert.ok(!text($("#ai-key-fmt")).includes(KEY), "the hint never repeats the key");
  type("#ai-f-api_key", "sk-or-abc");
  assert.equal($("#ai-key-fmt").hidden, true);
  type("#ai-f-api_key", KEY);
  type("#ai-f-model", "vendor/model-a");
  seen.puts.length = 0;
  submit();
  await until(() => seen.puts.length === 1, 2000, "the save goes ahead");
  assert.equal(seen.puts[0].body.api_key, KEY);
});

test("saving sends the key in the body once, clears the input, and the page never shows it", async () => {
  await until(() => $("[data-ai-result]") && !$("#ai-save").disabled, 2000, "the test after saving");
  assert.equal($("#ai-f-api_key").value, "", "the key input is empty after a save");
  assert.ok(!app.innerHTML.includes(KEY), "the key is nowhere in the page");
  assert.equal($("#ai-status [data-cn-status]").dataset.cnStatus, "connected");
  assert.equal(text($("#ai-status")), "Connected · OpenRouter · vendor/model-a");
  assert.ok($("[data-ai-saved]"), "and it is now shown as saved");
  assert.equal(seen.tests.at(-1).body, null, "the test after a save tests what is saved");
});

test("saving tells the rest of the app to ask again what is connected", async () => {
  let heard = 0;
  w.addEventListener(availability.AI_SERVICE_EVENT, () => (heard += 1));
  reset();
  await show("product");
  type("#ai-f-model", "test-model-2");
  submit();
  await until(() => heard > 0, 2000, "the event");
});

test("Load models asks the service, fills the datalist and says how many were found", async () => {
  reset();
  await show("product");
  seen.models.length = 0;
  type("#ai-f-api_key", KEY);
  click("#ai-models");
  await until(() => seen.models.length === 1 && $$("#ai-models-list option").some((o) => o.value === "test-model-9"), 2000, "the list");
  assert.deepEqual(seen.models[0], { slot: "product", body: { provider: "openai", api_key: KEY } });
  assert.deepEqual($$("#ai-models-list option").map((o) => o.value), ["test-model-1", "test-model-9"]);
  assert.match(text($("#ai-models-status")), /^2 models found/);
  assert.equal($("#ai-f-api_key").value, KEY, "listing models does not disturb what was typed");
});

test("a listing that fails is a note, not an error page", async () => {
  modelsAnswer = { models: [], note: "The service did not list its models." };
  click("#ai-models");
  await until(() => /did not list/.test(text($("#ai-models-status"))), 2000, "the note");
  assert.deepEqual($$("#ai-models-list option").map((o) => o.value), ["test-model-1", "test-model-2"], "back to the examples");
  assert.equal($(".apierr"), null);
  modelsAnswer = { models: ["test-model-1", "test-model-9"], note: null };
});

test("Test of the saved service sends no body and shows the message and the time", async () => {
  reset();
  await show("product");
  seen.tests.length = 0;
  click("#ai-test");
  await until(() => $('[data-ai-result="ok"]'), 2000, "the result");
  assert.deepEqual(seen.tests, [{ slot: "product", body: null }]);
  assert.match(text($("[data-ai-result]")), /Passed\. It answered\./);
  assert.match(text($("[data-ai-result]")), /Answered in 412 ms, model test-model-1\./);
  assert.equal($("[data-ai-unsaved]"), null);
});

test("Test of what is on screen sends it, including the typed key, and says it is not saved", async () => {
  type("#ai-f-model", "test-model-2");
  type("#ai-f-api_key", KEY);
  seen.tests.length = 0;
  click("#ai-test");
  await until(() => seen.tests.length === 1 && $("[data-ai-unsaved]"), 2000, "the result");
  assert.equal(seen.tests[0].body.api_key, KEY);
  assert.equal(seen.tests[0].body.model, "test-model-2");
  assert.match(text($("[data-ai-unsaved]")), /not saved yet/);
  assert.ok(!app.innerHTML.includes(KEY));
});

test("a failed test shows the plain fix and the embeddings note, as text", async () => {
  testAnswer = { ok: false, message: "The service refused the key.", fix: "Check the key and try again.", latency_ms: null, model: "test-model-1", embeddings_note: "Document search will match by keywords." };
  click("#ai-test");
  await until(() => $('[data-ai-result="failed"]'), 2000, "the failure");
  assert.match(text($("[data-ai-result]")), /Failed\. The service refused the key\./);
  assert.equal(text($("[data-ai-fix]")), "Check the key and try again.");
  assert.equal(text($("[data-ai-embeddings-note]")), "Document search will match by keywords.");
  testAnswer = { ok: true, message: "It answered.", fix: null, latency_ms: 412, model: "test-model-1", embeddings_note: null };
});

test("the last test the API remembers is shown when the screen opens, and a failure marks the badge", async () => {
  reset();
  slots.product = saved({ last_test: { ok: false, message: "The key was rejected.", fix: "Enter the key again.", latency_ms: null, tested_at: "2026-09-29T10:00:00Z" } });
  await show("product");
  assert.equal(text($("#ai-status")), "Failed: The key was rejected.");
  assert.equal(text($("[data-ai-fix]")), "Enter the key again.");
  assert.match(text($("[data-ai-result]")), /Last tested/);
});

test("a refusal names the field, marks it, and never echoes a value", async () => {
  reset();
  slots.product = { ...none };
  await show("product");
  pick("openai");
  type("#ai-f-model", "test-model-1");
  type("#ai-f-api_key", KEY);
  putRefusal = { status: 422, body: { detail: { code: "AI_BASE_URL_INVALID", message: "The address is not valid.", field: "base_url" } } };
  submit();
  await until(() => $("#ai-error .apierr"), 2000, "the refusal");
  assert.match(text($("#ai-error")), /The address is not valid\./);
  assert.equal($("#ai-f-base_url").getAttribute("aria-invalid"), "true");
  assert.equal($("#ai-f-api_key").value, KEY, "a refused save keeps what was typed so it need not be typed again");
  assert.ok(!app.innerHTML.includes(KEY));
  putRefusal = null;
});

test("Disconnect asks first, then removes the saved service and the badge says Not connected", async () => {
  reset();
  await show("product");
  seen.deletes.length = 0;
  click("#ai-disconnect");
  assert.ok($("#ai-disconnect-yes") && $("#ai-disconnect-no"));
  click("#ai-disconnect-no");
  assert.equal(seen.deletes.length, 0);
  assert.ok($("#ai-disconnect"), "kept");
  click("#ai-disconnect");
  click("#ai-disconnect-yes");
  await until(() => seen.deletes.length === 1 && /Not connected/.test(text($("#ai-status"))), 2000, "the disconnect");
  assert.deepEqual(seen.deletes, ["product"]);
  assert.equal($("#ai-disconnect"), null, "nothing saved to disconnect");
  assert.equal($("#ai-f-api_key"), null, "no provider is picked any more");
});

test("the Deliverable AI that uses the Product AI says so, offers Test, and 'Use its own' reveals the form", async () => {
  reset();
  await show("deliverable");
  const card = $("[data-ai-inherited]");
  assert.match(text(card), /Uses the Product AI unless you set its own/);
  assert.match(text(card), /Right now it uses OpenAI · test-model-1\./);
  assert.equal(text($("#ai-status")), "Uses the Product AI · OpenAI · test-model-1");
  assert.equal($("#ai-form"), null, "no form until it is given its own");
  seen.tests.length = 0;
  click("#ai-test");
  await until(() => seen.tests.length === 1, 2000, "the test");
  assert.deepEqual(seen.tests, [{ slot: "deliverable", body: null }]);
  click("#ai-use-own");
  assert.ok($("#ai-form"));
  assert.equal($("[data-ai-provider]:checked"), null, "nothing is picked for its own service yet");
  assert.equal($("[data-ai-saved]"), null, "the Product AI's key is not this slot's");
  click("#ai-use-inherited");
  assert.ok($("[data-ai-inherited]"), "back to using the Product AI's");
});

test("saving the Deliverable AI's own service, then 'Use the Product AI instead' removes it", async () => {
  reset();
  await show("deliverable");
  click("#ai-use-own");
  pick("anthropic");
  type("#ai-f-model", "claude-example");
  type("#ai-f-api_key", KEY);
  seen.puts.length = 0;
  submit();
  await until(() => seen.puts.length === 1, 2000, "the save");
  assert.equal(seen.puts[0].slot, "deliverable");
  await until(() => $("[data-ai-result]"), 2000, "the test");
  assert.equal(text($("#ai-status")), "Connected · Claude (Anthropic) · claude-example");
  assert.equal(text($("#ai-disconnect")), "Use the Product AI instead");
  click("#ai-disconnect");
  assert.match(text($("[data-cn-status]") ? $("#ai-disconnect-zone") : app), /It will use the Product AI again/);
  click("#ai-disconnect-yes");
  await until(() => $("[data-ai-inherited]"), 2000, "back to the Product AI's service");
  assert.deepEqual(seen.deletes.at(-1), "deliverable");
});

test("a locked slot is read-only: the reason, what is set, and Test, with no field to change", async () => {
  reset();
  slots.product = saved({ source: "config", editable: false, locked_reason: "Set by the operator in the deployment's settings.", has_key: false, provider: "bedrock", provider_label: "Amazon Bedrock", model: "example.bedrock-model-v1", region: "us-east-1", third_party: false });
  await show("product");
  const locked = $("[data-locked]");
  assert.match(text(locked), /This is managed by whoever runs Marketing AI/);
  assert.match(text(locked), /Set by the operator in the deployment's settings\./);
  assert.equal(facts().Service, "Amazon Bedrock");
  assert.equal(facts().Region, "us-east-1");
  assert.equal("Key" in facts(), false, "Bedrock has no key to report");
  assert.equal($$("input, select, textarea").length, 0, "nothing to type into");
  assert.equal($("#ai-save"), null);
  assert.equal($("#ai-disconnect"), null);
  seen.tests.length = 0;
  click("#ai-test");
  await until(() => seen.tests.length === 1 && $("[data-ai-result]"), 2000, "the test");
  assert.equal(seen.tests[0].body, null);
});

test("a locked slot with a key never shows one, only that it is saved; a third party is still disclosed", async () => {
  reset();
  slots.product = saved({ editable: false, locked_reason: "Managed by the operator." });
  await show("product");
  assert.equal(facts().Key, "Saved");
  assert.match(text($("[data-ai-third-party]")), /are sent to OpenAI\./);
  assert.equal($('input[type="password"]'), null);
});

test("everything the API sends is text: provider names, hints, suggestions and messages", async () => {
  reset();
  slots.product = { ...none };
  await show("product");
  assert.equal($$("img").length, 0);
  pick("hostile");
  assert.equal($$("img").length, 0, $("#ai-fields").innerHTML);
  assert.ok(text($("#ai-key-help")).includes(HOSTILE), "shown as words");
  assert.ok($$("#ai-models-list option").some((o) => o.value === HOSTILE));
  assert.ok(text($(".ai-pcard.on")).includes(HOSTILE));
  testAnswer = { ok: false, message: HOSTILE, fix: HOSTILE, latency_ms: 1, model: HOSTILE, embeddings_note: HOSTILE };
  type("#ai-f-model", "m");
  type("#ai-f-api_key", KEY);
  click("#ai-test");
  await until(() => $("[data-ai-result]"), 2000, "the result");
  assert.equal($$("img").length, 0, $("#ai-result").innerHTML);
  assert.equal(w.__pwned, undefined);
  testAnswer = { ok: true, message: "It answered.", fix: null, latency_ms: 412, model: "test-model-1", embeddings_note: null };
});

test("the key input is never given a value by the page, and no code path prints it", async () => {
  reset();
  await show("product");
  for (const input of $$('input[type="password"]')) {
    assert.equal(input.getAttribute("value"), null);
    assert.equal(input.getAttribute("autocomplete"), "off");
  }
  assert.equal($$("input[data-ai-input]").filter((i) => i.type !== "password" && /key|secret|token/i.test(i.id + i.name)).length, 0);
});

test("with the API unreachable the screen says so, with Try again", async () => {
  const original = w.fetch;
  w.fetch = async () => {
    throw new TypeError("offline");
  };
  await show("product");
  assert.ok($(".apierr [data-retry], [data-retry]"));
  w.fetch = original;
});
