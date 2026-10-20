// Node tests for the Proof screen's promises (PITCH.md screen 5), run by test_proof.py.
//
// A tiny fake document stands in for a browser (no jsdom, no npm). Each test renders ui/v2/screens/proof.js
// against a fake API built from the real mock JSON files (proof.json, expected.json, goal_detail.json), changing
// only the ranges, so every number checked here comes from a file with the API's shape.

import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { test } from "node:test";

// ---- a tiny fake DOM ----------------------------------------------------------------------------------

class FakeNode {
  constructor() {
    this.childNodes = [];
    this.parentNode = null;
  }
  appendChild(child) {
    if (child.parentNode) child.parentNode.removeChild(child);
    child.parentNode = this;
    this.childNodes.push(child);
    return child;
  }
  append(...kids) {
    // Like the real DOM: anything that is not a node (null and false included) becomes its text.
    for (const k of kids) this.appendChild(k instanceof FakeNode ? k : new FakeText(String(k)));
  }
  removeChild(child) {
    const i = this.childNodes.indexOf(child);
    if (i >= 0) this.childNodes.splice(i, 1);
    child.parentNode = null;
    return child;
  }
  remove() {
    if (this.parentNode) this.parentNode.removeChild(this);
  }
  replaceChildren(...kids) {
    for (const c of [...this.childNodes]) this.removeChild(c);
    this.append(...kids);
  }
  get textContent() {
    return this.childNodes.map((c) => c.textContent).join("");
  }
  set textContent(v) {
    for (const c of [...this.childNodes]) this.removeChild(c);
    if (String(v) !== "") this.appendChild(new FakeText(String(v)));
  }
}

class FakeText extends FakeNode {
  constructor(text) {
    super();
    this.data = text;
  }
  get textContent() {
    return this.data;
  }
  set textContent(v) {
    this.data = String(v);
  }
}

class FakeElement extends FakeNode {
  constructor(tag, ns = null) {
    super();
    this.tagName = tag.toLowerCase();
    this.namespaceURI = ns;
    this.attributes = {};
    this.listeners = {};
    this.hidden = false;
    this.disabled = false;
    this.value = "";
  }
  setAttribute(k, v) {
    this.attributes[k] = String(v);
    if (k === "id") this.id = String(v);
  }
  getAttribute(k) {
    return k in this.attributes ? this.attributes[k] : null;
  }
  removeAttribute(k) {
    delete this.attributes[k];
  }
  addEventListener(type, fn) {
    (this.listeners[type] ||= []).push(fn);
  }
  removeEventListener(type, fn) {
    this.listeners[type] = (this.listeners[type] || []).filter((f) => f !== fn);
  }
  focus() {
    fakeDocument.activeElement = this;
  }
  select() {}
  /** Fire an event and wait for async handlers. */
  async fire(type, extra = {}) {
    const ev = { type, target: this, preventDefault() {}, ...extra };
    await Promise.all((this.listeners[type] || []).map((f) => f(ev)));
    await settle();
  }
}

const fakeDocument = {
  activeElement: null,
  listeners: {},
  head: new FakeElement("head"),
  body: new FakeElement("body"),
  createElement: (tag) => new FakeElement(tag),
  createElementNS: (ns, tag) => new FakeElement(tag, ns),
  createTextNode: (text) => new FakeText(text),
  getElementById(id) {
    return all(this.head).find((n) => n.id === id) || all(this.body).find((n) => n.id === id) || null;
  },
  addEventListener(type, fn) {
    (this.listeners[type] ||= []).push(fn);
  },
  removeEventListener(type, fn) {
    this.listeners[type] = (this.listeners[type] || []).filter((f) => f !== fn);
  },
  execCommand: () => true,
};
globalThis.document = fakeDocument;

function all(node) {
  const out = [];
  const walk = (n) => {
    for (const c of n.childNodes) {
      if (c instanceof FakeElement) {
        out.push(c);
        walk(c);
      }
    }
  };
  walk(node);
  return out;
}
const byTag = (root, tag) => all(root).filter((n) => n.tagName === tag);
const byClass = (root, cls) => all(root).filter((n) => (n.getAttribute("class") || "").split(/\s+/).includes(cls));
const button = (root, text) => byTag(root, "button").find((b) => b.textContent.trim() === text);
const settle = () => new Promise((r) => setTimeout(r, 0));
/** A real DOM turns a stray `false`/`null`/`undefined` child into that word, so none may be rendered. */
const noJunk = (text) => !/\b(undefined|null|false|NaN)\b/.test(text);

// ---- the screen and a fake API on the mock files ---------------------------------------------------------

const mockDir = new URL("../../../ui/v2/mock/", import.meta.url);
const mock = (name) => JSON.parse(readFileSync(new URL(name, mockDir), "utf8"));
const proofScreen = await import(new URL("../../../ui/v2/screens/proof.js", import.meta.url).href);

function fakeApi({ goal = {}, proof, expected, ...overrides } = {}) {
  const detail = mock("goal_detail.json");
  const g = { ...detail.goal, ...goal };
  return {
    listGoals: async () => [g],
    getGoal: async () => ({ ...detail, goal: g }),
    getProof: async () => (proof === undefined ? mock("proof.json") : proof),
    getExpected: async () => (expected === undefined ? mock("expected.json") : expected),
    ...overrides,
  };
}

async function draw(opts) {
  const root = new FakeElement("main");
  const toasts = [];
  await proofScreen.render(root, { api: fakeApi(opts), goalId: "goal_winback", navigate: () => {}, toast: (m, k) => toasts.push([m, k]) });
  return { root, text: root.textContent, toasts };
}

const verdictOf = (root) => all(root).find((n) => n.id === "pf-verdict").textContent;
const withProfit = (est) => ({ ...mock("proof.json"), incremental_profit: est });
const order = (text, ...parts) => {
  let at = -1;
  for (const p of parts) {
    const i = text.indexOf(p, at + 1);
    assert.ok(i > at, `"${p}" should come after the previous part`);
    at = i;
  }
};

// ---- the verdicts (rows 48, 49, 51) ---------------------------------------------------------------------

test("direction only compares the range's ends with zero", () => {
  assert.equal(proofScreen.direction({ point: 4300, low: 900, high: 7700 }), "up");
  assert.equal(proofScreen.direction({ point: 10, low: -5, high: 20 }), "unsure");
  assert.equal(proofScreen.direction({ point: -10, low: 0, high: 0 }), "unsure");
  assert.equal(proofScreen.direction({ point: -10, low: -20, high: -1 }), "down");
  assert.equal(proofScreen.direction(null), "early");
  assert.equal(proofScreen.direction({ point: 1, low: null, high: 2 }), "early");
});

test("a real result leads with the verdict, then the money against people we never contact, with its range", async () => {
  const { root, text } = await draw();
  assert.match(verdictOf(root), /Real extra profit/);
  order(text, "Extra profit compared with people we never contact", "Real extra profit", "$4,300 (range $900 to $7,700)", "How this is measured");
  assert.ok(text.includes("Range $900 to $7,700"));
  assert.ok(text.includes("Range $6,100 to $22,300"));
  assert.ok(text.includes("1,015 so far"));
  assert.ok(text.includes('not "attributed revenue"') || text.includes('It is not "attributed revenue"'));
  assert.ok(!text.includes("Not sure yet"));
  assert.ok(noJunk(text));
});

test("a range that includes zero says 'Not sure yet' before any number", async () => {
  const { root, text } = await draw({ proof: withProfit({ point: 600, low: -200, high: 1400 }) });
  order(text, "Not sure yet", "$600 (range -$200 to $1,400)");
  assert.ok(text.includes("The range includes zero"));
  assert.match(verdictOf(root), /Not sure yet/);
  assert.ok(!text.includes("Ready for Autopilot"));
  assert.ok(text.includes("Not ready for Autopilot yet"));
});

test("a range all below zero says 'Losing money so far'", async () => {
  const { root, text } = await draw({ proof: withProfit({ point: -500, low: -900, high: -100 }) });
  assert.match(verdictOf(root), /Losing money so far/);
  assert.ok(text.includes("Not ready for Autopilot: it is losing money so far"));
  assert.equal(byClass(byTag(root, "svg")[0], "ap-bar-neg").length, 1);
});

test("no estimate is 'Too early to tell' with no number made up", async () => {
  const thin = { ...mock("proof.json"), incremental_profit: null, incremental_revenue: null, lift_vs_baseline: null, roi: null };
  const { root, text } = await draw({ proof: thin });
  assert.equal((text.match(/Too early to tell/g) || []).length, 2);
  assert.ok(text.includes("Not enough data yet"));
  assert.equal(byTag(root, "svg").length, 0);
  assert.ok(text.includes("once there is enough data"));
});

test("the simple rule gets its own verdict", async () => {
  assert.ok((await draw()).text.includes("Beats the simple rule"));
  const unsure = { ...mock("proof.json"), lift_vs_baseline: { point: 0.1, low: -0.2, high: 0.4 } };
  const t = (await draw({ proof: unsure })).text;
  assert.ok(t.includes("has not yet clearly beaten it"));
  assert.ok(!t.includes("Beats the simple rule"));
  const behind = { ...mock("proof.json"), lift_vs_baseline: { point: -0.3, low: -0.5, high: -0.1 } };
  assert.ok((await draw({ proof: behind })).text.includes("Behind the simple rule so far"));
});

// ---- Autopilot when the proof is real (row 53) ------------------------------------------------------------

test("'Ready for Autopilot' links to the Plan dial only when the low end is above zero and it is not on Autopilot", async () => {
  const { root, text } = await draw({ goal: { autonomy: "approve" } });
  assert.ok(text.includes("Ready for Autopilot"));
  const link = byTag(root, "a").find((a) => a.textContent === "Switch to Autopilot on the Plan screen");
  assert.equal(link.getAttribute("href"), "#/plan/goal_winback");
  const on = await draw({ goal: { autonomy: "autopilot" } });
  assert.ok(on.text.includes("On Autopilot, and the proof backs it"));
  assert.ok(!on.text.includes("Ready for Autopilot"));
  const unproven = await draw({ goal: { autonomy: "autopilot" }, proof: withProfit({ point: 5, low: -1, high: 11 }) });
  assert.ok(unproven.text.includes("On Autopilot, but the proof is not there yet"));
});

// ---- what it expects to earn, before a message goes out (row 54) -----------------------------------------

test("Shadow shows its own forecast, labelled as a forecast, straight after the verdict", async () => {
  const { text } = await draw({ goal: { autonomy: "shadow" }, proof: null });
  order(text, "Too early to tell", "In Shadow mode it sends nothing", "What it expects to earn", "Its own forecast, not proof", "$3,900", "Range $1,500 to $6,300", "Not ready for Autopilot yet");
  assert.ok(text.includes("nothing was sent"));
  assert.ok(text.includes("over the 14 days after each contact"));
  assert.ok(text.includes("Of 4,620 contacts, 4,410 carry a forecast"));
  assert.ok(text.includes("switch to Approve each batch with a small budget"));
  assert.ok(noJunk(text));
});

test("a forecast that is missing or fails never blanks the proof", async () => {
  let r = await draw({ expected: null });
  assert.ok(!r.text.includes("What it expects to earn"));
  assert.ok(r.text.includes("Real extra profit"));
  r = await draw({ getExpected: async () => { throw new Error("boom"); } });
  assert.ok(r.text.includes("Couldn't load the forecast: boom"));
  assert.ok(r.text.includes("Real extra profit"));
  const words = { ...mock("expected.json"), expected_net: null, words: "No contacts decided yet, so there is no forecast." };
  r = await draw({ expected: words });
  assert.ok(r.text.includes("No contacts decided yet, so there is no forecast."));
});

// ---- the product fact, the currency, the share text (rows 52, 55, 56) --------------------------------

test("3-10% is shown only as labelled general guidance, with its source", async () => {
  const { root, text } = await draw();
  const fact = byTag(root, "aside").find((a) => a.textContent.includes("3-10%"));
  assert.ok(fact, "the guidance is in its own labelled aside");
  assert.ok(fact.textContent.includes("General guidance about AI marketing, not a result from your business."));
  assert.ok(fact.textContent.includes(proofScreen.EXPECT_DOC));
  assert.equal((text.match(/3-10%/g) || []).length, 1);
  // Nothing about pricing: there are no pricing terms in the API yet.
  assert.ok(!/paid in results|billed|fee/i.test(text));
});

test("money is in the goal's own currency", async () => {
  const { text } = await draw({ goal: { currency: "INR" } });
  assert.ok(text.includes("₹4,300"));
  assert.ok(!text.includes("$4,300"));
});

test("the shareable summary leads with the people we never contact and says the verdict", () => {
  const goal = mock("goal_detail.json").goal;
  const text = proofScreen.proofSummaryText(mock("proof.json"), goal);
  assert.match(text, /Compared with the people we never contact/);
  assert.match(text, /real extra profit/);
  assert.match(text, /It beats the simple rule/);
  const unsure = proofScreen.proofSummaryText(withProfit({ point: 1, low: -1, high: 3 }), goal);
  assert.match(unsure, /not sure yet/);
});

test("the chart's zero line runs through each bar only, never through the value written under it", async () => {
  const { root } = await draw({ proof: withProfit({ point: 600, low: -200, high: 1400 }) });
  const svg = byTag(root, "svg")[0];
  const zeros = byClass(svg, "ap-zero");
  const notes = byClass(svg, "pf-chart-note").map((n) => Number(n.getAttribute("y")));
  for (const z of zeros) {
    const [y1, y2] = [Number(z.getAttribute("y1")), Number(z.getAttribute("y2"))];
    for (const y of notes) assert.ok(y - 13 > y2 || y + 4 < y1, `note at ${y} crosses zero line ${y1}-${y2}`);
  }
});
