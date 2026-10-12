/* Plan M M192 (DEC-1622): Settings › AI service (`#/settings/ai`) - the AI service in one click.

   The whole page (app.js and every module of ui/index.html) against the simple module's fake API, with `GET /ai-service`
   answered here in the shape `api/routes/ai_service.py` gives (`{ slots: { product, deliverable }, providers }`):
   - the page: breadcrumb Settings › AI service, two cards in plain words, each slot's state from the API and one
     direct button to its key form; "You can use the same key for both."; no engineering name outside technical details;
   - both connected, neither connected, the second using the first's service, a key the server can no longer read;
   - a person who may read but not save it ("Ask an admin to connect it"), and one who may not read it;
   - a failed load: the error with "Try again", which loads again;
   - the route is the simple module's (`resolveRoute`), Settings' row links here, and the key form opened from here
     puts Settings › AI service in its breadcrumb (from Connections it keeps Connections). */
import { test } from "node:test";
import assert from "node:assert/strict";
import { $, $$, settle, until } from "../harness.mjs";
import { fixture, installWholePage } from "./fake.mjs";

const slot = (over = {}) => ({
  connected: false,
  source: "none",
  provider: null,
  provider_label: null,
  model: null,
  has_key: false,
  key_readable: true,
  last_test: null,
  editable: true,
  locked_reason: null,
  third_party: false,
  ...over,
});
const OPENAI = { id: "openai", label: "OpenAI", protocol: "openai", needs_key: true, key_hint: "sk-…", key_help: "", model_suggestions: [] };
const connected = (label = "OpenAI", over = {}) =>
  slot({ connected: true, source: "saved", provider: "openai", provider_label: label, model: "gpt-4o-mini", has_key: true, ...over });

const api = { status: 200, body: { slots: { product: slot(), deliverable: slot() }, providers: [OPENAI] } };
let asked = 0;
const setSlots = (product, deliverable) => {
  api.status = 200;
  api.body = { slots: { product, deliverable }, providers: [OPENAI] };
};

const world = {
  runs: fixture("runs_empty"),
  waiting: null,
  extra: (request) => {
    if (request.method === "GET" && request.path === "/ai-service") {
      asked += 1;
      return { status: api.status, body: api.body };
    }
    return undefined;
  },
};
const { w } = await installWholePage({ hash: "#/settings", world });
const { aiServiceHtml, AI_SLOTS } = await import("../../../../../ui/modules/simple/ai_service.js");
const { settingsHtml } = await import("../../../../../ui/modules/simple/pages.js");
const router = await import("../../../../../ui/modules/router.js");
const { visibleText } = await import("../glossary/collect.mjs");

const text = (el) => (el ? el.textContent.replace(/\s+/g, " ").trim() : "");
const card = (name) => $(`[data-ai-svc-card="${name}"]`);
const go = async (hash) => {
  w.location.hash = hash;
  await settle(6);
};
const draw = (options) => {
  const holder = w.document.createElement("div");
  holder.innerHTML = aiServiceHtml(options);
  return holder;
};

test("the route is the simple module's: #/settings/ai is registered in the router", () => {
  const owner = router.resolveRoute(["settings", "ai"]);
  assert.ok(owner, "a module claims #/settings/ai");
  assert.equal(owner.name, "simple");
});

test("Settings' AI service row opens Settings › AI service, in plain words", async () => {
  await until(() => $("[data-settings-main]"), 3000, "the Settings page");
  const row = $("[data-settings-main] a[data-settings-ai]");
  assert.ok(row, "the AI service row");
  assert.equal(row.getAttribute("href"), "#/settings/ai");
  assert.equal(text(row), "AI service");
  assert.equal(
    text(row.closest("li").querySelector(".st-desc")),
    "Connect the AI that helps your team and the AI that writes for your customers.",
  );
  assert.equal($('[data-settings-main] a[href="#/connections"]'), null, "no longer the general Connections page");
});

test("neither connected: two cards in plain words, 'Not connected', and a Connect button straight to each key form", async () => {
  setSlots(slot(), slot());
  await go("#/settings/ai");
  await until(() => card("product") && card("deliverable"), 3000, "the two cards");
  assert.equal(text($("main h1")), "AI service");
  const trail = $$("main .crumbs a, main .crumbs .cur").map(text);
  assert.deepEqual(trail, ["Settings", "AI service"], "Settings › AI service");
  assert.equal($("main .crumbs a").getAttribute("href"), "#/settings");
  assert.equal(text(card("product").querySelector("h2")), "AI that helps your team");
  assert.equal(text(card("deliverable").querySelector("h2")), "AI that writes for your customers");
  assert.match(text(card("product")), /Guided setup and Data Doctor/);
  assert.match(text(card("deliverable")), /campaign messages/);
  for (const name of ["product", "deliverable"]) {
    const chip = card(name).querySelector("[data-ai-svc-status]");
    assert.equal(chip.getAttribute("data-ai-svc-status"), "not_connected");
    assert.equal(text(chip), "Not connected");
    assert.equal(chip.querySelector("svg").getAttribute("aria-hidden"), "true", "an icon and a word, never colour alone");
    const go = card(name).querySelector(`[data-ai-svc-go="${name}"]`);
    assert.equal(go.getAttribute("href"), `#/connections/ai/${name}`, "one click to the key form");
    assert.equal(text(go), "Connect");
    assert.equal(go.getAttribute("data-ai-from-settings"), name);
  }
  assert.equal($$("main .btn.primary").length, 1, "the page's one primary button: the first service to connect");
  assert.ok(card("product").querySelector(".btn.primary"));
  assert.equal(text($("[data-ai-svc-same]")), "You can use the same key for both.");
  assert.equal(document.title, "AI service · Marketing AI");
  assert.ok(asked >= 1, "drawn from GET /ai-service");
});

test("no engineering name is shown: 'product', 'deliverable' and 'slot' only behind the technical switch", () => {
  const shown = visibleText(document.body);
  const page = $("main[data-ai-settings]");
  const words = visibleText(page);
  assert.ok(words.length > 100, "the page is drawn");
  assert.doesNotMatch(words, /\bproduct\b|\bdeliverable\b|\bslot\b/i);
  assert.match(shown, /AI that helps your team/);
  for (const tech of $$("main [data-ai-svc-card] .ai-svc-tech")) assert.ok(tech.hasAttribute("data-technical"));
});

test("both connected: 'Connected · <service>' from the API and a Change button each", async () => {
  setSlots(connected("OpenAI"), connected("Anthropic", { provider: "anthropic" }));
  await go("#/settings/__away");
  await go("#/settings/ai");
  await until(() => card("product") && card("product").querySelector('[data-ai-svc-status="connected"]'), 3000, "connected");
  assert.equal(text(card("product").querySelector("[data-ai-svc-status]")), "Connected · OpenAI");
  assert.equal(text(card("deliverable").querySelector("[data-ai-svc-status]")), "Connected · Anthropic");
  assert.deepEqual($$("[data-ai-svc-go]").map(text), ["Change", "Change"]);
  assert.equal($$("main .btn.primary").length, 0, "nothing left to connect: no primary button");
  assert.match(text(card("product").querySelector("[data-technical]")), /gpt-4o-mini/, "the model only behind the switch");
});

test("the second using the first's service says so; a key the server cannot read asks for it again", () => {
  const html = draw({
    state: {
      slots: {
        product: connected("OpenAI", { has_key: true, key_readable: false }),
        deliverable: connected("OpenAI", { source: "inherited" }),
      },
    },
  });
  const product = html.querySelector('[data-ai-svc-card="product"]');
  const deliverable = html.querySelector('[data-ai-svc-card="deliverable"]');
  assert.equal(product.querySelector("[data-ai-svc-status]").getAttribute("data-ai-svc-status"), "key_unreadable");
  assert.equal(text(product.querySelector("[data-ai-svc-status]")), "Enter the key again");
  assert.equal(text(deliverable.querySelector("[data-ai-svc-status]")), "Connected · OpenAI");
  assert.equal(text(deliverable.querySelector("[data-ai-svc-inherited]")), "Uses the same service as the AI that helps your team.");
  const failed = draw({ state: { slots: { product: connected("OpenAI", { last_test: { ok: false, message: "401" } }), deliverable: slot() } } });
  assert.equal(text(failed.querySelector('[data-ai-svc-card="product"] [data-ai-svc-status]')), "Last test failed · OpenAI");
  const locked = draw({ state: { slots: { product: slot({ editable: false, locked_reason: "Set on the server by your IT team." }), deliverable: slot() } } });
  assert.equal(text(locked.querySelector("[data-ai-svc-locked]")), "Set on the server by your IT team.", "the server's own reason");
});

test("a person who may read but not save it: the state, and 'Ask an admin to connect it' instead of a button", () => {
  const viewer = (method) => method === "GET";
  const html = draw({ state: { slots: { product: slot(), deliverable: connected("OpenAI") } }, can: viewer });
  assert.equal(html.querySelectorAll("[data-ai-svc-go]").length, 0, "no button to a form they could not save");
  assert.equal(html.querySelectorAll("a[href^='#/connections/ai/']").length, 0);
  assert.equal(text(html.querySelector('[data-ai-svc-card="product"] [data-ai-svc-ask]')), "Ask an admin to connect it.");
  assert.equal(text(html.querySelector('[data-ai-svc-card="deliverable"] [data-ai-svc-ask]')), "Ask an admin to change it.");
  assert.equal(text(html.querySelector('[data-ai-svc-card="deliverable"] [data-ai-svc-status]')), "Connected · OpenAI");
  // Settings: the row is there for a reader (the page says who may connect), gone for one who may not read it.
  const reader = w.document.createElement("div");
  reader.innerHTML = settingsHtml({ can: viewer });
  assert.equal(reader.querySelector("a[data-settings-ai]").getAttribute("href"), "#/settings/ai");
  const none = w.document.createElement("div");
  none.innerHTML = settingsHtml({ can: (method, path) => path !== "/ai-service" });
  assert.equal(none.querySelector("a[data-settings-ai]"), null);
});

test("a person who may not read the AI service: one notice and a way back, no cards", () => {
  const html = draw({ can: (method, path) => path !== "/ai-service" });
  assert.ok(html.querySelector("[data-ai-svc-denied]"));
  assert.equal(html.querySelectorAll("[data-ai-svc-card]").length, 0);
  assert.equal(html.querySelector("[data-ai-svc-denied] a").getAttribute("href"), "#/settings");
});

test("a failed load is the error with Try again, which loads again", async () => {
  api.status = 500;
  api.body = { detail: { code: "INTERNAL", message: "Something went wrong." } };
  await go("#/settings/__away");
  await go("#/settings/ai");
  await until(() => $("[data-ai-svc-error] [data-retry]"), 3000, "the error with Try again");
  assert.equal($$("[data-ai-svc-card]").length, 0, "nothing invented while the answer is missing");
  assert.ok($("[data-ai-svc-error] .apierr[role=alert]"));
  setSlots(connected("OpenAI"), slot());
  const before = asked;
  $("[data-ai-svc-error] [data-retry]").click();
  await until(() => card("product"), 3000, "the page after Try again");
  assert.ok(asked > before, "asked again");
  assert.equal(text(card("product").querySelector("[data-ai-svc-status]")), "Connected · OpenAI");
});

test("the key form opened from here goes back to Settings › AI service; opened from Connections, to Connections", async () => {
  setSlots(slot(), slot());
  await go("#/settings/__away");
  await go("#/settings/ai");
  await until(() => card("deliverable"), 3000, "the cards");
  const button = card("deliverable").querySelector("[data-ai-svc-go]");
  button.click(); // jsdom does not follow a link: the click is remembered, then the address changes as the browser would
  await go(button.getAttribute("href"));
  await until(() => $("[data-ai-slot='deliverable'] .crumbs"), 3000, "the key form");
  const trail = $$("main .crumbs a, main .crumbs .cur").map(text);
  assert.deepEqual(trail, ["Settings", "AI service", "Deliverable AI"]);
  assert.equal($("main .crumbs a[href='#/settings/ai']") !== null, true, "the back link is Settings › AI service");
  // The same form reached any other way keeps the Connections breadcrumb.
  await go("#/connections");
  await go("#/connections/ai/deliverable");
  await until(() => $("[data-ai-slot='deliverable'] .crumbs"), 3000, "the key form again");
  assert.deepEqual($$("main .crumbs a").map(text), ["Connections"]);
});

test("every slot the API has is drawn, in the order of the plain names", () => {
  assert.deepEqual(
    AI_SLOTS.map((s) => s.slot),
    ["product", "deliverable"],
  );
});
