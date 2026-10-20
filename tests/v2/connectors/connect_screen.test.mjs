// Node checks for the Connect screen's platform cards (ui/v2/screens/connect.js) against GET /v2/connectors
// (api/v2/connectors.py): Shopify and Klaviyo first, each with one line, a status pill and one button, then files,
// databases and "Coming next" as quiet rows; "Needs reconnect" the only loud state; health in words, never a raw
// error; the connect forms (the shop address, OAuth or a pasted key); and an older API without these routes. Run by
// tests/v2/connectors/test_api_ui.py, or directly: `node tests/v2/connectors/connect_screen.test.mjs`.

import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";
import { fileURLToPath } from "node:url";

// ---- a tiny DOM ------------------------------------------------------------------------------------------
class Node {
  constructor() {
    this.parentNode = null;
    this.childNodes = [];
  }
  remove() {
    if (this.parentNode) this.parentNode.childNodes = this.parentNode.childNodes.filter((n) => n !== this);
    this.parentNode = null;
  }
}

class Text extends Node {
  constructor(text) {
    super();
    this.data = String(text);
  }
  get textContent() {
    return this.data;
  }
}

const BOOL_PROPS = ["disabled", "hidden", "selected", "required", "checked"];

class Element extends Node {
  constructor(tag) {
    super();
    this.tagName = tag.toUpperCase();
    this.attrs = new Map();
    this.listeners = {};
    this.style = {};
    this.dataset = {};
    this.value = "";
    const self = this;
    this.classList = {
      add: (...c) => self.setAttribute("class", [...new Set([...self.classes(), ...c])].join(" ")),
      remove: (...c) => self.setAttribute("class", self.classes().filter((x) => !c.includes(x)).join(" ")),
      toggle: (c, on) => ((on ?? !self.classes().includes(c)) ? self.classList.add(c) : self.classList.remove(c)),
      contains: (c) => self.classes().includes(c),
    };
    for (const p of BOOL_PROPS) {
      Object.defineProperty(this, p, {
        get: () => this.attrs.has(p),
        set: (v) => (v ? this.attrs.set(p, "") : this.attrs.delete(p)),
      });
    }
  }
  classes() {
    return (this.attrs.get("class") || "").split(/\s+/).filter(Boolean);
  }
  get className() {
    return this.attrs.get("class") || "";
  }
  set className(v) {
    this.attrs.set("class", String(v));
  }
  get id() {
    return this.attrs.get("id") || "";
  }
  setAttribute(k, v) {
    this.attrs.set(k, String(v));
    if (k === "value") this.value = String(v);
  }
  getAttribute(k) {
    return this.attrs.has(k) ? this.attrs.get(k) : null;
  }
  removeAttribute(k) {
    this.attrs.delete(k);
  }
  hasAttribute(k) {
    return this.attrs.has(k);
  }
  append(...nodes) {
    for (const n of nodes) {
      const node = n instanceof Node ? n : new Text(n);
      node.remove();
      node.parentNode = this;
      this.childNodes.push(node);
    }
  }
  replaceChildren(...nodes) {
    for (const n of this.childNodes) n.parentNode = null;
    this.childNodes = [];
    this.append(...nodes);
  }
  get textContent() {
    return this.childNodes.map((n) => n.textContent).join("");
  }
  set textContent(v) {
    this.replaceChildren(new Text(v));
  }
  addEventListener(type, fn) {
    (this.listeners[type] ||= []).push(fn);
  }
  dispatch(type, extra = {}) {
    const event = { type, target: this, preventDefault() {}, ...extra };
    return Promise.all((this.listeners[type] || []).map((fn) => fn(event)));
  }
  click() {
    return this.dispatch("click");
  }
  focus() {}
  matches(sel) {
    const m = sel.match(/^([a-z0-9]*)((?:\.[\w-]+)*)(?:#([\w-]+))?(:checked)?$/i);
    if (!m) throw new Error(`shim cannot match ${sel}`);
    const [, tag, cls, id, checked] = m;
    if (tag && this.tagName !== tag.toUpperCase()) return false;
    if (cls && !cls.split(".").filter(Boolean).every((c) => this.classes().includes(c))) return false;
    if (id && this.id !== id) return false;
    if (checked && !this.checked) return false;
    return true;
  }
  *walk() {
    for (const n of this.childNodes) {
      if (n instanceof Element) {
        yield n;
        yield* n.walk();
      }
    }
  }
  querySelectorAll(sel) {
    return [...this.walk()].filter((n) => n.matches(sel));
  }
  querySelector(sel) {
    return this.querySelectorAll(sel)[0] || null;
  }
}

globalThis.Node = Node;
globalThis.document = {
  body: new Element("body"),
  createElement: (t) => new Element(t),
  createTextNode: (t) => new Text(t),
  getElementById(id) {
    return [...this.body.walk()].find((n) => n.id === id) || null;
  },
};

// ---- the code under test -----------------------------------------------------------------------------------
const UI = new URL("../../../ui/v2/", import.meta.url);
const MOCK = new URL("mock/", UI);
const { createApi } = await import(new URL("api.js", UI).href);
const C = await import(new URL("components.js", UI).href);
const connect = await import(new URL("screens/connect.js", UI).href);

const sample = async (name) => JSON.parse(await readFile(fileURLToPath(new URL(name, MOCK)), "utf8"));

function fakeFetch(server) {
  return async (url, init = {}) => {
    const u = String(url);
    if (u.startsWith("file:")) {
      try {
        const text = await readFile(fileURLToPath(u), "utf8");
        return { ok: true, status: 200, text: async () => text, headers: { get: () => "application/json" } };
      } catch {
        return { ok: false, status: 404, text: async () => "", headers: { get: () => "text/plain" } };
      }
    }
    if (!server) throw new TypeError("fetch failed");
    return server(u, init);
  };
}

const json = (status, body) => ({ ok: status < 400, status, text: async () => JSON.stringify(body), headers: { get: () => "application/json" } });
const NOW = new Date("2026-10-20T08:00:00Z");
const HEALTH = { as_of: "2026-10-20T00:00:00Z", delivery_rate: 1, complaint_rate: 0, unsubscribe_rate: 0, block_rate: 0, contacts_per_customer_week: 0.2, alerts: [] };
const mockApi = () => createApi({ mock: true, fetch: fakeFetch(), now: () => NOW });
const liveApi = (routes, seen = []) =>
  createApi({
    base: "",
    mock: undefined,
    fetch: fakeFetch((url, init) => {
      const key = `${init.method || "GET"} ${url.split("?")[0]}`;
      seen.push([key, init.body]);
      if (key in routes) {
        const r = routes[key];
        return typeof r === "function" ? r(init) : json(200, r);
      }
      return json(404, { detail: "Not Found" });
    }),
  });

function makeCtx(api) {
  const calls = { toast: [], redirect: [] };
  return { calls, ctx: { api, goalId: null, goal: null, navigate: () => {}, toast: (m, t) => calls.toast.push([m, t]), redirect: (u) => calls.redirect.push(u), refreshGoals: async () => {} } };
}
const byClass = (root, cls) => root.querySelectorAll(`.${cls}`);
const byText = (root, tag, text) => root.querySelectorAll(tag).filter((n) => n.textContent.trim() === text);
const settle = () => new Promise((r) => setTimeout(r, 0));
async function until(check, what) {
  for (let i = 0; i < 200; i += 1) {
    if (check()) return;
    await new Promise((r) => setTimeout(r, 5));
  }
  assert.fail(`timed out waiting for ${what}`);
}
const rowsOf = (root) => byClass(root, "cn-platform");
const rowFor = (root, kind) => rowsOf(root).find((r) => r.dataset.kind === kind);
/** What a sighted person reads: the text outside the descriptions (role=tooltip boxes are hidden until opened). */
function visible(node) {
  if (node.data !== undefined) return node.data;
  if (node.getAttribute && (node.getAttribute("role") === "tooltip" || node.hasAttribute("hidden"))) return "";
  return node.childNodes.map(visible).join("");
}
const primaries = (root) => root.querySelectorAll("button").concat(root.querySelectorAll("a")).filter((n) => n.classes().includes("btn-primary"));

const conn = (over = {}) => ({
  connection_id: "conn_1",
  kind: "shopify",
  label: "Acme Goods",
  external_id: "acme.myshopify.com",
  status: "active",
  phase: "healthy",
  words: "Up to date, 4 minutes ago",
  detail: "Autonomy checks Shopify every hour, and right away when Shopify reports a change.",
  can_act: true,
  last_sync_at: "2026-10-20T07:56:00Z",
  history_progress: null,
  rows_last_day: 200,
  connected_at: "2026-09-10T00:00:00Z",
  setup: "automatic",
  streams: [{ stream: "orders", label: "Orders", critical: true, freshness: "fresh", last_sync_at: "2026-10-20T07:56:00Z", lag_seconds: 240, words: "Up to date, 4 minutes ago", history_ready: true, history_progress: 1, history_words: "History loaded", rows_last_day: 140, last_rows: 37 }],
  problems: [],
  ...over,
});
const platform = (kind, over = {}) => ({
  kind,
  label: kind === "shopify" ? "Shopify" : "Klaviyo",
  tagline: kind === "shopify" ? "Brings in orders, customers and products." : "Sends Autonomy's messages through your own flows.",
  description: `${kind} description`,
  auth: "oauth2",
  oauth_ready: true,
  missing_settings: [],
  key_allowed: true,
  install_url: null,
  scopes: kind === "shopify" ? ["read_orders", "read_customers"] : ["profiles:read"],
  setup: "automatic",
  setup_note: null,
  config_fields: kind === "shopify" ? [{ name: "shop", label: "Shop address", help: "Your shop's permanent address, ending in .myshopify.com", pattern: "[a-z0-9-]+\\.myshopify\\.com", required: true }] : [],
  streams: [],
  sends: kind === "klaviyo",
  regions: ["US", "UK", "AU"],
  api_version: "2026-10",
  connections: [],
  ...over,
});
const overview = (platforms, over = {}) => ({ business_id: "demo", as_of: "2026-10-20T08:00:00Z", can_act: true, on_hold: [], platforms, ...over });

// ---- sample data -------------------------------------------------------------------------------------------
test("Connect (sample data): Shopify and Klaviyo lead, then files, databases and what comes next", async () => {
  const { ctx } = makeCtx(mockApi());
  const root = C.el("div");
  await connect.render(root, ctx);
  await settle();
  const data = await sample("connectors.json");
  const rows = rowsOf(root);
  assert.deepEqual(rows.slice(0, 2).map((r) => r.dataset.kind), ["shopify", "klaviyo"]);
  const shop = data.platforms.find((p) => p.kind === "shopify").connections[0];
  const shopRow = rowFor(root, "shopify");
  assert.equal(byClass(shopRow, "cn-p-words")[0].textContent, shop.words);
  assert.equal(byClass(shopRow, "pill")[0].textContent, "Connected");
  const kl = data.platforms.find((p) => p.kind === "klaviyo").connections[0];
  const klRow = rowFor(root, "klaviyo");
  assert.equal(byClass(klRow, "cn-p-words")[0].textContent, kl.words);
  assert.equal(byClass(klRow, "pill")[0].textContent, "Syncing");
  // each line's meaning is its description, still in the page
  assert.ok(root.textContent.includes(shop.detail) && root.textContent.includes(kl.detail));
  // files, the database, and the eight platforms that come next, as quiet rows with nothing to click
  const text = root.textContent;
  for (const s of ["Upload your files", "Or connect your database", "Coming next"]) assert.ok(text.includes(s), s);
  assert.deepEqual(byClass(root, "cn-soon-item").map((n) => n.textContent), [...connect.COMING_NEXT]);
  assert.equal(byClass(root, "cn-soon-row")[0].querySelectorAll("button").length, 0);
  // the sample's Klaviyo still has its one-time step to do: the steps are one click away
  const setup = byClass(klRow, "cn-p-setup")[0];
  await until(() => !setup.hidden, "the guided setup line");
  assert.match(setup.textContent, /One step to finish in Klaviyo\./);
  const steps = setup.querySelector("ol");
  assert.equal(steps.hidden, true);
  await byText(setup, "button", "Show the steps")[0].click();
  assert.equal(steps.hidden, false);
  const sampleSetup = Object.values(await sample("connector_setup.json"))[0];
  assert.deepEqual(steps.querySelectorAll("li").map((li) => li.textContent), sampleSetup.steps);
  // while history loads, the screen says decisions wait, once, with why in its description
  assert.ok(text.includes("Autonomy waits to decide until your data is ready."));
  assert.ok(text.includes(data.on_hold[0]));
  assert.ok(!/connector\.[a-z_]+/.test(visible(root)), "no error code is ever on screen");
});

test("Connect (sample data): Details shows each stream in words, and a write says it needs the live API", async () => {
  const { ctx, calls } = makeCtx(mockApi());
  const root = C.el("div");
  await connect.render(root, ctx);
  const shopRow = rowFor(root, "shopify");
  const panel = byClass(shopRow, "cn-p-panel")[0];
  assert.equal(panel.hidden, true);
  await byText(shopRow, "button", "Details")[0].click();
  assert.equal(panel.hidden, false);
  const data = await sample("connectors.json");
  const shop = data.platforms.find((p) => p.kind === "shopify").connections[0];
  assert.deepEqual(byClass(panel, "cn-stream-name").map((n) => n.textContent), shop.streams.map((s) => s.label));
  assert.match(byClass(panel, "cn-stream-words")[0].textContent, /^Up to date, 4 minutes ago · 140 in the last day$/);
  await byText(panel, "button", "Sync now")[0].click();
  await settle();
  assert.deepEqual(calls.toast.at(-1), ["This page shows sample data. Connecting needs the live Autonomy API.", "warning"]);
});

// ---- the live API ------------------------------------------------------------------------------------------
test("Connect: nothing connected yet, Connect Shopify is the one primary and asks for the shop address first", async () => {
  const seen = [];
  const routes = {
    "GET /v2/health": HEALTH,
    "GET /v2/connectors": overview([platform("shopify"), platform("klaviyo")]),
    "POST /v2/connectors/shopify/connect": (init) => {
      const body = JSON.parse(init.body);
      if (body.config.shop !== "acme") return json(400, { detail: "Shop address is not valid." });
      return json(200, { kind: "shopify", url: "https://acme.myshopify.com/admin/oauth/authorize?state=s" });
    },
  };
  const { ctx, calls } = makeCtx(liveApi(routes, seen));
  const root = C.el("div");
  await connect.render(root, ctx);
  const shopRow = rowFor(root, "shopify");
  assert.equal(byClass(shopRow, "cn-p-line")[0].textContent, "Brings in orders, customers and products.");
  assert.equal(byClass(shopRow, "pill")[0].textContent, "Not connected");
  assert.deepEqual(primaries(root).map((b) => b.textContent), ["Connect"]);
  assert.equal(primaries(root)[0], shopRow.querySelector("button"));
  await shopRow.querySelector("button").click();
  const form = shopRow.querySelector("form");
  assert.ok(form, "the shop address form opens under the row");
  assert.equal(shopRow.querySelector("button").hidden, true, "the Connect button steps aside while the form is open");
  assert.equal(form.querySelector("input").getAttribute("placeholder"), "your-shop.myshopify.com");
  form.querySelector("input").value = "oops";
  await form.dispatch("submit");
  assert.equal(byClass(form, "field-error")[0].textContent, "Shop address is not valid.");
  form.querySelector("input").value = " acme ";
  await form.dispatch("submit");
  assert.deepEqual(calls.redirect, ["https://acme.myshopify.com/admin/oauth/authorize?state=s"]);
  assert.deepEqual(JSON.parse(seen.filter(([k]) => k === "POST /v2/connectors/shopify/connect").at(-1)[1]), { config: { shop: "acme" } });
});

test("Connect: without our app on the server, a pasted key connects and the card redraws", async () => {
  const seen = [];
  let connected = false;
  const shop = () => platform("shopify", { oauth_ready: false, missing_settings: ["AUTONOMY_SHOPIFY_CLIENT_ID"], connections: connected ? [conn({ phase: "syncing", words: "First sync running" })] : [] });
  const routes = {
    "GET /v2/health": HEALTH,
    "GET /v2/connectors": () => json(200, overview([shop(), platform("klaviyo", { oauth_ready: false })])),
    "POST /v2/connectors/shopify/key": (init) => {
      const body = JSON.parse(init.body);
      if (body.api_key !== "shpat_good_token") return json(400, { detail: "Shopify did not accept this key. Check it and try again." });
      connected = true;
      return json(200, conn({ phase: "syncing", words: "First sync running" }));
    },
  };
  const { ctx, calls } = makeCtx(liveApi(routes, seen));
  const root = C.el("div");
  await connect.render(root, ctx);
  const shopRow = rowFor(root, "shopify");
  await shopRow.querySelector("button").click();
  const form = shopRow.querySelector("form");
  const [shopInput, keyInput] = form.querySelectorAll("input");
  assert.equal(keyInput.getAttribute("type"), "password");
  assert.ok(byText(form, "label", "Admin API access token").length === 1);
  assert.ok(form.textContent.includes("read_orders, read_customers"), "the access the key needs is in its description");
  assert.equal(byText(form, "button", "Use an access key instead").length, 0, "no sign-in to switch to");
  shopInput.value = "acme";
  keyInput.value = "shpat_bad";
  await form.dispatch("submit");
  assert.equal(byClass(form, "field-error")[0].textContent, "Shopify did not accept this key. Check it and try again.");
  keyInput.value = "shpat_good_token";
  await form.dispatch("submit");
  assert.deepEqual(JSON.parse(seen.find(([k, b]) => k === "POST /v2/connectors/shopify/key" && b.includes("good"))[1]), { api_key: "shpat_good_token", config: { shop: "acme" } });
  assert.deepEqual(calls.toast.at(-1), ["Shopify is connected. The first sync has started.", "good"]);
  const redrawn = rowFor(root, "shopify");
  assert.equal(byClass(redrawn, "cn-p-words")[0].textContent, "First sync running");
  assert.equal(byClass(redrawn, "pill")[0].textContent, "Syncing");
  assert.ok(!root.textContent.includes("shpat_good_token"), "the key is never drawn back");
});

test("Connect: needs reconnect is the only loud state, and its Reconnect is the screen's one primary", async () => {
  const lost = conn({ status: "needs_reconnect", phase: "needs_reconnect", words: "Needs reconnecting", detail: "Autonomy's access to Shopify stopped working.", can_act: false });
  const healthy = { ...conn({ connection_id: "conn_2", kind: "klaviyo", label: "Acme", words: "Up to date, 12 minutes ago" }) };
  const report = { as_of: "2026-10-20T00:00:00Z", customers: 10, orders: 20, products: 3, sources: [], fixes: [], problems: [], questions: [{ question_id: "q1", text: "Merge two columns?", rows_affected: 4 }], doctor_connected: true };
  const routes = {
    "GET /v2/health": HEALTH,
    "GET /v2/data/status": report,
    "GET /v2/connectors": overview([platform("shopify", { connections: [lost] }), platform("klaviyo", { connections: [healthy] })], { can_act: false, on_hold: ["Shopify (Acme Goods): needs reconnecting."] }),
    "GET /v2/connectors/connections/conn_2/setup": { connection_id: "conn_2", kind: "klaviyo", state: "ready", words: "Sending is set up in Klaviyo.", steps: [], channels: [], checked_at: "2026-10-20T08:00:00Z" },
  };
  const { ctx } = makeCtx(liveApi(routes));
  const root = C.el("div");
  await connect.render(root, ctx);
  await settle();
  const shopRow = rowFor(root, "shopify");
  assert.ok(shopRow.classes().includes("is-loud"));
  assert.deepEqual(byClass(shopRow, "pill").map((p) => [p.textContent, p.classes().includes("pill-critical")]), [["Needs reconnect", true]]);
  assert.deepEqual(primaries(root).map((b) => b.textContent), ["Reconnect"], "the Data Doctor's Answer steps back");
  assert.equal(byText(root, "a", "Answer in the Data Doctor").length, 1);
  // every other pill is calm
  const loud = root.querySelectorAll("span").filter((n) => n.classes().includes("pill-critical"));
  assert.equal(loud.length, 1);
  assert.equal(rowsOf(root).filter((r) => r.classes().includes("is-loud")).length, 1);
  // a ready Klaviyo says nothing more
  assert.equal(byClass(rowFor(root, "klaviyo"), "cn-p-setup")[0].hidden, true);
  // Reconnect opens the same form, with the shop already filled in
  await byText(shopRow, "button", "Reconnect")[0].click();
  assert.equal(shopRow.querySelector("form").querySelector("input").value, "acme.myshopify.com");
});

test("Connect: Klaviyo with sign-in set up goes straight to Klaviyo; Disconnect asks once more", async () => {
  const seen = [];
  const routes = {
    "GET /v2/health": HEALTH,
    "GET /v2/connectors": overview([platform("shopify", { connections: [conn()] }), platform("klaviyo")]),
    "POST /v2/connectors/klaviyo/connect": { kind: "klaviyo", url: "https://www.klaviyo.com/oauth/authorize?state=k" },
    "DELETE /v2/connectors/connections/conn_1": conn({ status: "disconnected", phase: "disconnected", words: "Disconnected" }),
  };
  const { ctx, calls } = makeCtx(liveApi(routes, seen));
  const root = C.el("div");
  await connect.render(root, ctx);
  const klRow = rowFor(root, "klaviyo");
  assert.deepEqual(primaries(root).map((b) => b.textContent), ["Connect"]);
  await klRow.querySelector("button").click();
  assert.deepEqual(calls.redirect, ["https://www.klaviyo.com/oauth/authorize?state=k"]);
  assert.equal(klRow.querySelector("form"), null, "nothing to type for Klaviyo");
  const shopRow = rowFor(root, "shopify");
  await byText(shopRow, "button", "Details")[0].click();
  const off = byText(shopRow, "button", "Disconnect")[0];
  await off.click();
  assert.equal(off.textContent, "Confirm disconnect");
  assert.equal(seen.filter(([k]) => k.startsWith("DELETE")).length, 0, "one click never disconnects");
  await off.click();
  assert.equal(seen.filter(([k]) => k === "DELETE /v2/connectors/connections/conn_1").length, 1);
  assert.deepEqual(calls.toast.at(-1), ["Shopify is disconnected.", "info"]);
});

test("Connect on an API without the connector routes: the platforms say so, files still work", async () => {
  const { ctx } = makeCtx(liveApi({ "GET /v2/health": HEALTH }));
  const root = C.el("div");
  await connect.render(root, ctx);
  const rows = rowsOf(root);
  assert.deepEqual(rows.slice(0, 2).map((r) => r.dataset.kind), ["shopify", "klaviyo"]);
  for (const r of rows.slice(0, 2)) {
    assert.equal(byClass(r, "cn-p-line")[0].textContent, "Not available on this server yet.");
    assert.equal(r.querySelector("button").disabled, true);
  }
  assert.ok(root.querySelectorAll("a").some((a) => a.getAttribute("href") === "/" && a.classes().includes("btn-primary")), "no data yet: the Data Doctor is the next step");
});

test("Connect: problems and a stale card read in words, behind a calm pill", async () => {
  const stale = conn({
    phase: "stale",
    words: "Last synced 4 hours ago",
    can_act: false,
    problems: [{ code: "connector.stale", severity: "critical", stream: "orders", words: "Orders last synced 4 hours ago. Decisions wait until it catches up." }],
  });
  const routes = { "GET /v2/health": HEALTH, "GET /v2/connectors": overview([platform("shopify", { connections: [stale] }), platform("klaviyo")]) };
  const { ctx } = makeCtx(liveApi(routes));
  const root = C.el("div");
  await connect.render(root, ctx);
  const shopRow = rowFor(root, "shopify");
  const p = byClass(shopRow, "pill")[0];
  assert.equal(p.textContent, "Behind");
  assert.ok(p.classes().includes("pill-warning") && !shopRow.classes().includes("is-loud"));
  await byText(shopRow, "button", "Details")[0].click();
  assert.deepEqual(byClass(shopRow, "cn-p-problems")[0].querySelectorAll("li").map((li) => li.textContent), ["Orders last synced 4 hours ago. Decisions wait until it catches up."]);
  assert.ok(!/connector\.[a-z_]+/.test(visible(root)));
});

test("choosePrimary: reconnect, then the Data Doctor's questions, then the first platform not connected", () => {
  const lost = { connections: [{ connection_id: "c1", phase: "needs_reconnect" }] };
  assert.equal(connect.choosePrimary([{ kind: "shopify", ...lost }], { waiting: true }), "reconnect:c1");
  assert.equal(connect.choosePrimary([{ kind: "shopify", connections: [] }], { waiting: true }), null);
  assert.equal(connect.choosePrimary([{ kind: "shopify", connections: [{ phase: "healthy" }] }, { kind: "klaviyo", connections: [] }]), "connect:klaviyo");
  assert.equal(connect.choosePrimary([{ kind: "shopify", connections: [{ phase: "healthy" }] }], { hasData: true }), null);
  assert.equal(connect.choosePrimary(null, {}), "files");
  assert.equal(connect.choosePrimary([{ kind: "shopify", connections: [{ phase: "disconnected" }] }]), "connect:shopify");
});
