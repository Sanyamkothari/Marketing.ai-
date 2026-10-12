/* Plan M M185 (DEC-1615): ui/components.js, the look's components, in jsdom.

   Each helper's markup is parsed into a real document and read the way a browser and a screen reader read it: the
   roles, the names, what is hidden, what is escaped. Run through tests/integration/test_components_ui.py. */
import { test } from "node:test";
import assert from "node:assert/strict";
import { JSDOM } from "jsdom";

const dom = new JSDOM(`<!DOCTYPE html><html><head></head><body><div id="app"></div></body></html>`, {
  url: "http://localhost/ui/#/",
  pretendToBeVisual: true,
});
globalThis.window = dom.window;
globalThis.document = dom.window.document;

const C = await import("../../../../../ui/components.js");
const app = document.getElementById("app");

/** Draw `html` into the page and return its first element. */
const draw = (html) => {
  app.innerHTML = html;
  return app.firstElementChild;
};
const EVIL = `<img src=x onerror="alert(1)">"&'`;

test("statusOf: the one mapping, by kind, in every spelling a page meets", () => {
  const campaign = (v) => C.statusOf("campaign", v);
  assert.deepEqual(
    ["draft", "live", "waiting", "measured"].map((v) => [campaign(v).tone, campaign(v).word]),
    [
      ["neutral", "Draft"],
      ["info", "Live"],
      ["warning", "Waiting for results"],
      ["good", "Measured"],
    ],
  );
  assert.equal(campaign("waiting_for_results").key, "waiting");
  assert.equal(campaign("Waiting for results").key, "waiting");
  assert.equal(campaign("MEASURED").tone, "good");
  const evidence = (v) => C.statusOf("evidence", v);
  assert.deepEqual([evidence("proven").tone, evidence("proven").word], ["good", "Proven"]);
  assert.deepEqual([evidence("estimated").tone, evidence("estimated").word], ["info", "Estimated"]);
  assert.deepEqual([evidence("not_yet_proven").tone, evidence("not_yet_proven").word], ["neutral", "Not yet proven"]);
  for (const spelling of ["Causal", "causal"]) assert.equal(evidence(spelling).key, "proven", spelling);
  for (const spelling of ["not-proven", "Not proven", "Not yet proven", "not_proven"]) assert.equal(evidence(spelling).key, "not_yet_proven", spelling);
  const health = (v) => C.statusOf("health", v);
  assert.deepEqual([health("read").tone, health("read").word], ["good", "Read correctly"]);
  assert.equal(health("not_tested").tone, "warning");
  assert.equal(health("failed").tone, "critical");
  // every entry has a tone from the five, an icon that exists, and a word
  for (const [kind, table] of Object.entries(C.STATUS_MAP)) {
    for (const [key, s] of Object.entries(table)) {
      assert.ok(C.TONES.includes(s.tone), `${kind}.${key} tone`);
      assert.ok(C.ICONS[s.icon], `${kind}.${key} icon`);
      assert.ok(s.word, `${kind}.${key} word`);
    }
  }
  // a server's own sentence keeps the tone and the icon
  assert.deepEqual(C.statusOf("health", "failed", { word: "Failed: the file is empty" }), {
    key: "failed",
    tone: "critical",
    icon: "critical",
    word: "Failed: the file is empty",
  });
  // an unknown value never claims a colour
  assert.equal(C.statusOf("campaign", "archived").tone, "neutral");
  assert.equal(C.statusOf("campaign", "archived").word, "archived");
  assert.throws(() => C.statusOf("mood", "happy"), /no status kind/);
  // the result is a copy: a page cannot change the mapping
  campaign("live").tone = "critical";
  assert.equal(campaign("live").tone, "info");
});

test("statusChip: an icon hidden from screen readers and the word, never colour alone", () => {
  for (const [kind, table] of Object.entries(C.STATUS_MAP)) {
    for (const key of Object.keys(table)) {
      const chip = draw(C.statusChip(kind, key));
      const s = C.statusOf(kind, key);
      assert.ok(chip.classList.contains("badge") && chip.classList.contains(`tone-${s.tone}`), `${kind}.${key}`);
      const svg = chip.querySelector("svg");
      assert.ok(svg, `${kind}.${key} has an icon`);
      assert.equal(svg.getAttribute("aria-hidden"), "true");
      assert.equal(svg.getAttribute("focusable"), "false");
      assert.equal(chip.textContent.trim(), s.word, `${kind}.${key} says its word`);
      assert.equal(chip.dataset.status, s.key);
      assert.equal(chip.dataset.tone, s.tone);
    }
  }
  const odd = draw(C.statusChip("health", "failed", { word: EVIL }));
  assert.equal(odd.querySelector("img"), null, "the word is escaped");
  assert.equal(odd.textContent.trim(), EVIL);
});

test("tagChip: a quiet grey tag with its word, escaped", () => {
  const tag = draw(C.tagChip("Win-back"));
  assert.equal(tag.className, "tag");
  assert.equal(tag.textContent, "Win-back");
  assert.equal(draw(C.tagChip(EVIL)).querySelector("img"), null);
});

test("button: the four kinds, the small size, link or button, disabled and busy", () => {
  const primary = draw(C.button({ label: "New campaign", href: "#/campaigns/new", icon: "plus" }));
  assert.equal(primary.tagName, "A");
  assert.equal(primary.className, "btn primary");
  assert.equal(primary.getAttribute("href"), "#/campaigns/new");
  assert.equal(primary.textContent, "New campaign");
  assert.equal(primary.querySelector("svg").getAttribute("aria-hidden"), "true", "the icon is decoration");
  const secondary = draw(C.button({ label: "Refresh the list", kind: "secondary", size: "sm", id: "refresh", attrs: "data-refresh" }));
  assert.equal(secondary.tagName, "BUTTON");
  assert.equal(secondary.getAttribute("type"), "button");
  assert.equal(secondary.className, "btn secondary sm");
  assert.equal(secondary.id, "refresh");
  assert.ok(secondary.hasAttribute("data-refresh"));
  assert.equal(draw(C.button({ label: "More", kind: "tertiary" })).className, "btn tertiary");
  assert.equal(draw(C.button({ label: "Delete", kind: "danger" })).className, "btn danger");
  assert.equal(draw(C.button({ label: "Odd", kind: "weird" })).className, "btn primary", "an unknown kind is primary");
  const off = draw(C.button({ label: "Approve", disabled: true }));
  assert.ok(off.disabled);
  const busy = draw(C.button({ label: "Saving", busy: true }));
  assert.ok(busy.disabled);
  assert.equal(busy.getAttribute("aria-busy"), "true");
  const offLink = draw(C.button({ label: "Download", href: "/x.csv", disabled: true }));
  assert.equal(offLink.getAttribute("href"), null, "a disabled link goes nowhere");
  assert.equal(offLink.getAttribute("aria-disabled"), "true");
  assert.equal(draw(C.button({ label: "Go", type: "submit" })).getAttribute("type"), "submit");
  const evil = draw(C.button({ label: EVIL, href: `javascript:"><img src=x>` }));
  assert.equal(evil.querySelector("img"), null);
  assert.equal(evil.textContent, EVIL);
});

test("pageHeader: one H1, one subtitle, the one primary action on the right, breadcrumbs only when given", () => {
  const head = draw(
    C.pageHeader({
      title: "Campaigns",
      subtitle: "Every campaign: where it stands.",
      primary: { label: "New campaign", href: "#/campaigns/new", icon: "plus" },
    }),
  );
  assert.equal(head.tagName, "HEADER");
  assert.equal(head.querySelectorAll("h1").length, 1);
  assert.equal(head.querySelector("h1").textContent, "Campaigns");
  assert.equal(head.querySelector(".desc").textContent, "Every campaign: where it stands.");
  assert.equal(head.querySelectorAll(".btn.primary").length, 1);
  assert.equal(head.querySelector(".crumbs"), null, "a top-level place has no breadcrumbs");
  const below = draw(
    C.pageHeader({
      title: "Best e-mail",
      crumbs: `<nav class="crumbs" aria-label="Breadcrumb"><a href="#/campaigns">Campaigns</a></nav>`,
      primary: `<a class="btn primary" href="#/x">Open</a>`,
      secondary: [{ label: "Refresh the list", id: "r" }],
    }),
  );
  assert.ok(below.querySelector(".ph-main > nav.crumbs"));
  assert.equal(below.querySelectorAll(".ph-actions .btn").length, 2);
  assert.equal(below.querySelector(".ph-actions .btn.secondary").id, "r");
  assert.equal(draw(C.pageHeader({ title: "Data" })).querySelector(".ph-actions"), null, "no actions, no empty box");
  assert.equal(draw(C.pageHeader({ title: EVIL, subtitle: EVIL })).querySelector("img"), null);
});

test("statTile and kpiRow: label, value with its unit, caption, and an optional range", () => {
  const tile = draw(C.statTile({ label: "Contacted", value: "12,862", unit: "customers", caption: "Sent on 11 Oct 2026" }));
  assert.equal(tile.querySelector(".stat-label").textContent, "Contacted");
  assert.equal(tile.querySelector(".stat-value").textContent, "12,862customers");
  assert.equal(tile.querySelector(".stat-unit").textContent, "customers");
  assert.equal(tile.querySelector(".stat-caption").textContent, "Sent on 11 Oct 2026");
  const ranged = draw(C.statTile({ label: "Net value", value: "2.5 lakh", range: { low: 1, high: 4, estimate: 2.5, text: "Likely 1 to 4 lakh" } }));
  assert.ok(ranged.querySelector(".vrange"));
  const row = draw(C.kpiRow([{ label: "A", value: "1" }, { label: "B", value: "2" }, C.statTile({ label: "C", value: "3" })]));
  assert.equal(row.className, "stat-row");
  assert.equal(row.querySelectorAll(".stat").length, 3);
  assert.equal(draw(C.statTile({ label: EVIL, value: EVIL, caption: EVIL })).querySelector("img"), null);
});

test("valueRange: the words are the accessible equivalent; the bar is hidden, placed by the numbers", () => {
  const r = draw(C.valueRange({ low: 88187, high: 418481, estimate: 250000, text: "Likely 88,187 to 4.18 lakh" }));
  assert.equal(r.querySelector(".vr-text").textContent, "Likely 88,187 to 4.18 lakh");
  const track = r.querySelector(".vr-track");
  assert.equal(track.getAttribute("aria-hidden"), "true", "the drawing is not read out; the words are");
  assert.equal(r.getAttribute("role"), null, "no role=img hides the words from a screen reader");
  const left = (el) => parseFloat(el.style.left);
  const bar = r.querySelector(".vr-bar");
  const dot = r.querySelector(".vr-dot");
  // 88,187 is about a fifth of 418,481: near zero, so the scale starts at zero and draws its tick
  assert.ok(r.querySelector(".vr-zero"), "a range near zero shows where zero is");
  assert.equal(left(r.querySelector(".vr-zero")), 0);
  assert.ok(left(bar) > 0 && left(bar) < 25, `bar starts near the left (${bar.style.left})`);
  assert.ok(left(bar) + parseFloat(bar.style.width) <= 100);
  assert.ok(left(dot) > left(bar) && left(dot) < left(bar) + parseFloat(bar.style.width), "the dot is inside the range");
  // a range that crosses zero: the tick is inside the bar
  const across = draw(C.valueRange({ low: -20, high: 60, text: "Likely -20 to +60" }));
  const zero = left(across.querySelector(".vr-zero"));
  const b = across.querySelector(".vr-bar");
  assert.ok(zero > left(b) && zero < left(b) + parseFloat(b.style.width));
  assert.equal(across.querySelector(".vr-dot"), null, "no estimate, no dot");
  // a range far from zero: no tick, the bar sits in the middle
  const far = draw(C.valueRange({ low: 900, high: 1100, estimate: 1000, text: "Likely 900 to 1,100" }));
  assert.equal(far.querySelector(".vr-zero"), null);
  assert.ok(Math.abs(left(far.querySelector(".vr-dot")) - 50) < 0.01);
  // reversed bounds are put in order; no numbers draws only the words
  const reversed = draw(C.valueRange({ low: 10, high: 2, text: "x" }));
  assert.ok(parseFloat(reversed.querySelector(".vr-bar").style.width) > 0);
  const words = draw(C.valueRange({ low: null, high: "n", text: "Not measured yet" }));
  assert.equal(words.querySelector(".vr-track"), null);
  assert.equal(words.textContent, "Not measured yet");
  assert.equal(draw(C.valueRange({ low: 1, high: 2, text: EVIL })).querySelector("img"), null);
});

test("emptyState: a line icon, one sentence, one action", () => {
  const empty = draw(C.emptyState({ title: "No campaign is live right now", text: "Start one.", action: { label: "New campaign", href: "#/campaigns/new" } }));
  assert.ok(empty.classList.contains("empty-state") && empty.classList.contains("has-icon"));
  assert.equal(empty.querySelector(".es-icon svg").getAttribute("aria-hidden"), "true");
  assert.equal(empty.querySelector(".es-t").textContent, "No campaign is live right now");
  assert.equal(empty.querySelectorAll(".btn").length, 1);
  assert.ok(empty.querySelector(".btn.primary"), "the one action is primary unless it says otherwise");
  assert.ok(draw(C.emptyState({ title: "x", action: { label: "y", kind: "secondary" } })).querySelector(".btn.secondary"));
  assert.equal(draw(C.emptyState({ title: EVIL })).querySelector("img"), null);
});

test("attentionItem: the tone is named for a screen reader, the action sits apart from the text", () => {
  const item = draw(
    C.attentionItem({
      tone: "warning",
      title: "The model does not beat ranking by risk",
      text: "This contact list was ranked by the approved risk model instead.",
      meta: "Hillstrom e-mail (spend)",
      action: { label: "Open the campaign", href: "#/campaigns/c1" },
    }),
  );
  assert.equal(item.dataset.tone, "warning");
  const svg = item.querySelector(".attn-icon svg");
  assert.equal(svg.getAttribute("role"), "img");
  assert.equal(svg.getAttribute("aria-label"), "Warning", "colour is never alone: the icon says what it means");
  assert.equal(item.querySelector(".attn-title").textContent, "The model does not beat ranking by risk");
  assert.equal(item.querySelector(".attn-meta").textContent, "Hillstrom e-mail (spend)");
  assert.ok(item.querySelector(".attn-action > a.btn.secondary"), "its own column, so a phone can put it below");
  assert.equal(draw(C.attentionItem({ tone: "nonsense", title: "x" })).dataset.tone, "neutral");
  assert.equal(draw(C.attentionItem({ title: EVIL, text: EVIL, meta: EVIL })).querySelector("img"), null);
});

test("tabs: role=tablist, role=tab, aria-selected and a roving tabindex", () => {
  const list = draw(
    C.tabs({
      label: "Campaign",
      current: "audience",
      items: [
        { id: "overview", label: "Overview", href: "#/campaigns/c1/overview" },
        { id: "audience", label: "Audience", href: "#/campaigns/c1/audience", count: 25 },
        { id: "results", label: "Results", href: "#/campaigns/c1/results" },
      ],
    }),
  );
  assert.equal(list.getAttribute("role"), "tablist");
  assert.equal(list.getAttribute("aria-label"), "Campaign");
  const all = [...list.querySelectorAll('[role="tab"]')];
  assert.equal(all.length, 3);
  assert.deepEqual(all.map((t) => t.getAttribute("aria-selected")), ["false", "true", "false"]);
  assert.deepEqual(all.map((t) => t.getAttribute("tabindex")), ["-1", "0", "-1"]);
  assert.equal(all[1].textContent, "Audience 25");
  assert.equal(all[0].getAttribute("href"), "#/campaigns/c1/overview");
  const buttons = draw(C.tabs({ label: "Data", current: "a", items: [{ id: "a", label: "Sources" }, { id: "b", label: "Check my data" }] }));
  assert.equal(buttons.querySelector('[role="tab"]').tagName, "BUTTON");
  assert.equal(draw(C.tabs({ label: EVIL, items: [{ id: "x", label: EVIL }] })).querySelector("img"), null);
});

test("tabs: Left and Right move between tabs (wrapping), Home and End jump, other keys do nothing", () => {
  C.wireTabs(document);
  C.wireTabs(document); // twice is once
  draw(
    C.tabs({
      label: "Reports",
      current: "value",
      items: [
        { id: "value", label: "Value proof", href: "#/reports" },
        { id: "programme", label: "Programme summary", href: "#/reports/programme" },
        { id: "check", label: "Check a past campaign", href: "#/reports/check" },
      ],
    }),
  );
  const tab = (id) => app.querySelector(`[data-tab-id="${id}"]`);
  const press = (key) => {
    const event = new window.KeyboardEvent("keydown", { key, bubbles: true, cancelable: true });
    document.activeElement.dispatchEvent(event);
    return event.defaultPrevented;
  };
  tab("value").focus();
  assert.equal(press("ArrowRight"), true);
  assert.equal(document.activeElement, tab("programme"));
  assert.equal(tab("programme").getAttribute("tabindex"), "0");
  assert.equal(tab("value").getAttribute("tabindex"), "-1");
  press("ArrowRight");
  assert.equal(document.activeElement, tab("check"));
  press("ArrowRight");
  assert.equal(document.activeElement, tab("value"), "wraps to the first");
  press("ArrowLeft");
  assert.equal(document.activeElement, tab("check"), "and back to the last");
  press("Home");
  assert.equal(document.activeElement, tab("value"));
  press("End");
  assert.equal(document.activeElement, tab("check"));
  assert.equal(press("a"), false, "a letter is not a tab key");
  assert.equal(tab("value").getAttribute("aria-selected"), "true", "moving focus does not change the page's selection");
  // keys outside a tablist are left alone
  app.innerHTML = `<button id="b">x</button>`;
  document.getElementById("b").focus();
  assert.equal(press("ArrowRight"), false);
});

test("stepper: done steps are checked, the current one is marked, the phone line names it", () => {
  const html = C.stepper({
    steps: [{ label: "Goal", href: "#/campaigns/new" }, "Customers", "Audience", "Review", "Send"],
    current: 1,
    label: "New campaign steps",
  });
  app.innerHTML = html;
  const list = app.querySelector("ol.stepper");
  assert.equal(list.getAttribute("aria-label"), "New campaign steps");
  const steps = [...list.querySelectorAll(".st-step")];
  assert.deepEqual(steps.map((s) => [...s.classList].filter((c) => c !== "st-step")[0]), ["done", "current", "upcoming", "upcoming", "upcoming"]);
  assert.equal(steps[1].getAttribute("aria-current"), "step");
  assert.equal(list.querySelectorAll("[aria-current]").length, 1);
  assert.ok(steps[0].querySelector(".st-dot svg"), "a done step shows a check");
  assert.match(steps[0].textContent, /Goal \(done\)/, "and says so in words");
  assert.equal(steps[0].querySelector("a").getAttribute("href"), "#/campaigns/new", "a done step can lead back");
  assert.equal(steps[2].querySelector("a"), null, "a step not reached yet is not a link");
  assert.equal(steps[2].querySelector(".st-dot").textContent, "3");
  const phone = app.querySelector(".stepper-phone");
  assert.equal(phone.textContent, "Step 2 of 5 · Customers");
  assert.equal(phone.getAttribute("aria-hidden"), "true", "screen readers hear the list, not the line twice");
  app.innerHTML = C.stepper({ steps: ["A", "B"], current: 9 });
  assert.equal(app.querySelector(".stepper-phone").textContent, "Step 2 of 2 · B", "out of range is clamped");
  app.innerHTML = C.stepper({ steps: [EVIL] });
  assert.equal(app.querySelector("img"), null);
});

test("icon: decoration unless labelled, and nothing for an unknown name", () => {
  const plain = draw(C.icon("check"));
  assert.equal(plain.getAttribute("aria-hidden"), "true");
  assert.equal(plain.getAttribute("stroke-width"), "1.75");
  assert.equal(plain.getAttribute("viewBox"), "0 0 24 24");
  const named = draw(C.icon("alert", { label: "Warning", size: 20 }));
  assert.equal(named.getAttribute("role"), "img");
  assert.equal(named.getAttribute("aria-label"), "Warning");
  assert.equal(named.getAttribute("width"), "20");
  assert.equal(C.icon("no-such-icon"), "");
});

test("the Overview page draws its own rules even when the classic Home's sheet came first (M185 review)", async () => {
  const { injectStyles } = await import("../../../../../ui/modules/overview/page.js");
  const page = new JSDOM(`<!DOCTYPE html><html><head><style id="ov-styles">.ov .ov-other-list{margin:0}</style></head><body></body></html>`);
  const doc = page.window.document;
  injectStyles(doc);
  injectStyles(doc);
  const own = [...doc.head.querySelectorAll("style")].filter((s) => s.textContent.includes(".ov .ov-grid{"));
  assert.equal(own.length, 1, "the Overview page's grid rules are in the document, once");
  assert.notEqual(own[0].id, "ov-styles");
});
