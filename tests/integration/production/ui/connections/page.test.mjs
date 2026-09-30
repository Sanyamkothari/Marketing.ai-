/* The Connections page (ui/modules/connections/, Plan H M80) in jsdom, against REAL bodies
   (PB_FIXTURES, written by tests/integration/connections/test_connections_ui.py). */
import { test } from "node:test";
import assert from "node:assert/strict";
import { $, $$, fixture, installPage, until } from "../harness.mjs";

const ids = fixture("ids");
const kinds = fixture("kinds");
const s3Tested = fixture("s3_tested");
const pgTested = fixture("pg_tested");

let connections = fixture("list").connections;
// `GET /ai-service` as the API answers it: two slots and the providers (only what the card reads).
const slot = (over) => ({ connected: false, source: "none", provider: null, provider_label: null, model: null, has_key: false, key_readable: true, third_party: false, last_test: null, editable: true, locked_reason: null, ...over });
const aiState = {
  slots: {
    product: slot({ connected: true, source: "saved", provider: "openai", provider_label: "OpenAI", model: "test-model-1", has_key: true, third_party: true }),
    deliverable: slot({ connected: true, source: "inherited", provider: "openai", provider_label: "OpenAI", model: "test-model-1", inherits_product: true }),
  },
  providers: [],
};
const sent = { creates: [], updates: [], tests: [], deletes: [] };

const server = (request) => {
  const { method, path, body } = request;
  if (method === "GET" && path === "/connections/kinds") return { status: 200, body: kinds };
  if (method === "GET" && path === "/connections") return { status: 200, body: { connections } };
  if (method === "GET" && path === "/ai-service") return { status: 200, body: aiState };
  if (method === "POST" && path === "/connections") {
    sent.creates.push(body);
    return { status: 201, body: fixture("pg_created") };
  }
  const one = /^\/connections\/([^/]+)(?:\/(\w+))?$/.exec(path);
  if (one) {
    const [, id, action] = one;
    if (method === "PUT" && !action) {
      sent.updates.push({ id, body });
      return { status: 200, body: connections.find((c) => c.connection_id === id) };
    }
    if (method === "DELETE" && !action) {
      sent.deletes.push(id);
      connections = connections.filter((c) => c.connection_id !== id);
      return { status: 204, body: null };
    }
    if (method === "POST" && action === "test") {
      sent.tests.push(id);
      return { status: 200, body: id === ids.s3 ? s3Tested : pgTested };
    }
  }
  return null;
};

const { w, calls } = installPage(server, { hash: "#/connections" });
const router = await import("../../../../../ui/modules/router.js");
await import("../../../../../ui/modules/connections/index.js");

const app = $("#app");
async function show(hash) {
  const parts = hash.replace(/^#\//, "").split("/");
  w.history.replaceState(null, "", `/ui/${hash}`);
  const module = router.resolveRoute(parts);
  assert.ok(module, `a module claims ${hash}`);
  await module.render(app, parts);
}
const text = (el) => (el ? el.textContent.replace(/\s+/g, " ").trim() : "");
const card = (id) => $(`[data-cn-connection="${id}"]`);

test("the page is registered at #/connections, the top bar's Connections item", () => {
  assert.deepEqual(
    router.phaseModules().find((m) => m.name === "connections"),
    { name: "connections", routes: ["connections"] },
  );
});

test("each saved connection is a card with a plain status badge", async () => {
  await show("#/connections");
  assert.equal(text($("h1")), "Connections");
  const ok = card(ids.s3);
  assert.match(text(ok.querySelector("[data-cn-status]")), /^Connected · tested /);
  const failed = card(ids.pg);
  assert.equal(text(failed.querySelector("[data-cn-status]")), `Failed: ${pgTested.connection.failure}`);
  assert.ok(ok.querySelector("[data-cn-test]") && ok.querySelector('a[href$="' + ids.s3 + '"]'), "Test and Edit are there");
});

test("a name with markup is shown as text, never as markup", () => {
  const failed = card(ids.pg);
  assert.equal(text(failed.querySelector("h3")), ids.hostile);
  assert.equal(failed.querySelectorAll("img").length, 0);
  assert.equal(w.__pwned, undefined);
});

test("the catalogue offers every service; one without its add-on says so", () => {
  for (const kind of kinds.kinds.filter((k) => k.creatable)) {
    const el = $(`[data-cn-kind="${kind.kind}"]`);
    assert.ok(el, kind.kind);
    const badge = el.querySelector("[data-cn-status]");
    if (!kind.available) {
      assert.equal(text(badge), "Needs the add-on");
      assert.match(text(el), new RegExp(`marketing-ai\\[${kind.addon}\\]`));
      assert.equal(el.querySelector("a.btn"), null, "nothing to set up without the add-on");
    } else if (!connections.some((c) => c.kind === kind.kind)) {
      assert.equal(text(badge), "Not set up");
    }
  }
});

test("the AI service section has two cards, each with its own badge and a button to its screen", () => {
  const product = $('[data-cn-ai="product"]');
  const deliverable = $('[data-cn-ai="deliverable"]');
  assert.equal(text(product.querySelector("h3")), "Product AI");
  assert.equal(text(deliverable.querySelector("h3")), "Deliverable AI");
  assert.equal(text(product.querySelector("[data-cn-status]")), "Connected · OpenAI · test-model-1");
  assert.equal(text(deliverable.querySelector("[data-cn-status]")), "Uses the Product AI · OpenAI · test-model-1");
  assert.equal(product.querySelector("a.btn").getAttribute("href"), "#/connections/ai/product");
  assert.equal(deliverable.querySelector("a.btn").getAttribute("href"), "#/connections/ai/deliverable");
  assert.equal(text(product.querySelector("a.btn")), "Change");
  assert.match(text(product), /Helps your team prepare data in Guided setup/);
  assert.match(text(deliverable), /What your customer gets/);
});

test("the services to add come before the AI service, right under the person's connections", () => {
  const sections = $$("main.cn section.cn-sec h2").map((h) => h.id);
  assert.deepEqual(sections, ["cn-yours", "cn-add", "cn-ai"]);
});

test("Test connection shows every step with its fix", async () => {
  card(ids.pg).querySelector("[data-cn-test]").click();
  await until(() => card(ids.pg).querySelector("[data-cn-report]"), 2000, "the report");
  assert.deepEqual(sent.tests, [ids.pg]);
  const steps = $$(`[data-cn-connection="${ids.pg}"] [data-cn-step]`);
  assert.deepEqual(steps.map((s) => s.dataset.cnStep), pgTested.report.steps.map((s) => s.name));
  const first = pgTested.report.steps[0];
  assert.match(text(steps[0]), new RegExp(first.fix.replace(/[.*+?^${}()|[\]\\]/g, "\\$&")));
  assert.match(text(card(ids.pg)), /Not connected yet/);
});

test("the set-up form asks for little, hides the rest under More options, and never pre-fills a secret", async () => {
  await show("#/connections/new/postgres");
  const visible = $$("#cn-form > label.field [data-cn-field], #cn-form > label.field [data-cn-secret]").map(
    (el) => el.dataset.cnField || el.dataset.cnSecret,
  );
  assert.deepEqual(visible, ["host", "database", "user", "password"]);
  const more = $("#cn-form details.adv");
  assert.equal(text(more.querySelector("summary")), "More options");
  assert.equal(more.open, false);
  assert.deepEqual(
    [...more.querySelectorAll("[data-cn-field]")].map((el) => el.dataset.cnField),
    ["port", "schema", "sslmode"],
  );
  const password = $('[data-cn-secret="password"]');
  assert.equal(password.type, "password");
  assert.equal(password.value, "");
  assert.equal($$(".btn.primary").length, 1, "one primary button");
  assert.equal(text($(".btn.primary")), "Save and test");
});

test("Save and test sends the settings and the secret, then shows the test", async () => {
  const fill = (sel, value) => {
    const el = $(sel);
    el.value = value;
    el.dispatchEvent(new w.Event("change", { bubbles: true }));
  };
  fill("#cn-name", "Sales DB");
  fill('[data-cn-field="host"]', "db.example.com");
  fill('[data-cn-field="database"]', "crm");
  fill('[data-cn-field="user"]', "reader");
  $('[data-cn-secret="password"]').value = "typed-secret";
  $("#cn-form").dispatchEvent(new w.Event("submit", { bubbles: true, cancelable: true }));
  await until(() => $("[data-cn-report]") && !$("#cn-save").disabled, 2000, "the test after saving");
  assert.equal(sent.creates.length, 1);
  const body = sent.creates[0];
  assert.equal(body.kind, "postgres");
  assert.equal(body.name, "Sales DB");
  assert.deepEqual(body.secrets, { password: "typed-secret" });  // secret-scan: allow (a fake password the test types)
  assert.equal(body.config.host, "db.example.com");
  assert.equal(body.config.port, 5432, "the default port is sent as the field's default");
  assert.ok(!("password" in body.config), "a secret never travels as a setting");
  assert.equal($('[data-cn-secret="password"]').value, "", "the typed secret is gone from the page");
  assert.match(w.location.hash, new RegExp(`#/connections/${ids.pg}$`));
});

test("editing: the saved secret is not shown, a blank keeps it", async () => {
  await show(`#/connections/${ids.s3}`);
  const key = $('[data-cn-secret="secret_access_key"]');
  assert.equal(key.value, "");
  assert.equal(key.placeholder, "Saved. Leave blank to keep it.");
  assert.equal($('[data-cn-field="bucket"]').value, fixture("s3_created").config.bucket);
  $("#cn-form").dispatchEvent(new w.Event("submit", { bubbles: true, cancelable: true }));
  await until(() => sent.updates.length === 1 && !$("#cn-save").disabled, 2000, "the update");
  assert.deepEqual(sent.updates[0].body.secrets, { access_key_id: "", secret_access_key: "" });
});

test("Delete asks first, then removes the card", async () => {
  await show("#/connections");
  card(ids.s3).querySelector("[data-cn-delete]").click();
  assert.match(text(card(ids.s3)), /Delete Exports\?/);
  card(ids.s3).querySelector("[data-cn-delete-no]").click();
  assert.deepEqual(sent.deletes, []);
  card(ids.s3).querySelector("[data-cn-delete]").click();
  card(ids.s3).querySelector("[data-cn-delete-yes]").click();
  await until(() => !card(ids.s3), 2000, "the card to go");
  assert.deepEqual(sent.deletes, [ids.s3]);
  assert.ok(!calls.some((c) => JSON.stringify(c).includes("typed-secret") && c.method === "GET"), "no secret in a URL");
});
