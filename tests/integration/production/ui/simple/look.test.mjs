/* Plan M M188 (DEC-1618): the look of Settings and Help in jsdom - the markup the CSS and a screen reader read. The links
   and words are settings.test.mjs's and help.test.mjs's business; this file holds the look's contracts:

   - Settings: the look's header with no breadcrumb (a top-level place); grouped cards of rows - the label is the link,
     one sentence under it, a chevron that screen readers skip; the technical switch is a real checkbox with role=switch
     (a proper toggle in CSS); Advanced tools is a quiet list under a disclosure;
   - Glossary: a search box that filters the two-column list (word, what it means); the engine's names stay hidden;
   - What's new: dated entries, each a date, a title and a sentence. */
import { test } from "node:test";
import fs from "node:fs";
import assert from "node:assert/strict";
import { $, $$, settle, until } from "../harness.mjs";
import { fixture, installWholePage } from "./fake.mjs";

const NEWS_FILE = new URL("../../../../../ui/modules/simple/whats-new.json", import.meta.url);
const world = {
  runs: fixture("runs_empty"),
  waiting: null,
  extra: (request) => (request.path.endsWith("/whats-new.json") ? { status: 200, body: fs.readFileSync(NEWS_FILE, "utf8") } : undefined),
};
const { w } = await installWholePage({ hash: "#/settings", world });
const { settingsHtml } = await import("../../../../../ui/modules/simple/pages.js");
const { glossaryHtml, filterGlossary, whatsNewHtml } = await import("../../../../../ui/modules/simple/help.js");

const text = (el) => (el ? el.textContent.replace(/\s+/g, " ").trim() : "");
const go = async (hash) => {
  w.location.hash = hash;
  await settle(4);
};

test("Settings: the look's header, no breadcrumb, one sentence and no action", async () => {
  await until(() => $("[data-settings-main]") && $("[data-signin-off]"), 3000, "the Settings page");
  const header = $("main > .page-header");
  assert.ok(header, "components.js pageHeader");
  assert.equal(header.querySelectorAll("h1").length, 1);
  assert.equal(text(header.querySelector("h1")), "Settings");
  assert.equal(header.querySelectorAll(".desc").length, 1);
  assert.equal($(".crumbs"), null, "Settings is a top-level place: no breadcrumb");
  assert.equal(header.querySelectorAll(".btn").length, 0, "nothing to do on this page but read and choose");
});

test("Settings: grouped cards of rows - the label is the link, one sentence under it, a chevron screen readers skip", () => {
  const groups = $$("main .card.st-group");
  assert.ok(groups.length >= 4, "Services and data, Advanced tools, What you see, About");
  const rows = $$("[data-settings-main] .st-row");
  assert.equal(rows.length, 4);
  for (const r of rows) {
    const link = r.querySelector("a.st-label");
    assert.ok(link && link.getAttribute("href").startsWith("#/"), "the label is the link");
    assert.ok(text(r.querySelector(".st-desc")).length > 20, "and one sentence says what it is");
    const chevron = r.querySelector(".st-go svg");
    assert.ok(chevron && chevron.getAttribute("aria-hidden") === "true", "a chevron, hidden from screen readers");
    assert.equal(r.querySelectorAll("a").length, 1, "one link per row: the whole row is its target in CSS, not a second link");
  }
  assert.ok($$("[data-settings-main] h3").every((h) => text(h) === "Services and data"));
});

test("Settings: the technical switch is a real checkbox with role=switch and its label, drawn as a toggle", () => {
  const section = $("[data-settings-view]");
  assert.ok(section.classList.contains("st-group"));
  const input = section.querySelector("input[data-tech-switch]");
  assert.equal(input.type, "checkbox");
  assert.equal(input.getAttribute("role"), "switch");
  const label = input.closest("label.tech-switch");
  assert.ok(label, "the whole row is the label: a click anywhere on it toggles");
  assert.equal(text(label.querySelector(".ts-label")), "Show technical details");
  assert.match(text(label.querySelector(".ts-hint")), /It changes what you see, not what you are allowed to do\./);
});

test("Settings: Advanced tools is a closed disclosure holding a quiet list of the same rows", () => {
  const card = $("[data-settings-advanced]");
  assert.ok(card.classList.contains("st-quiet"));
  const details = card.querySelector("details.adv");
  assert.equal(details.open, false);
  const rows = [...details.querySelectorAll(".st-row")];
  assert.ok(rows.length >= 8);
  const plain = rows.find((r) => r.hasAttribute("data-raw-tables"));
  assert.ok(plain.classList.contains("st-plain"), "an entry with no screen of its own is a row without a link or a chevron");
  assert.equal(plain.querySelector("a"), null);
});

test("Settings drawn for a signed-in admin: the Admin group is a card of rows too", () => {
  const holder = w.document.createElement("div");
  holder.innerHTML = settingsHtml({ can: () => true, status: "signed-in", version: "1.2.3" });
  const admin = holder.querySelector("[data-settings-admin]");
  assert.ok(admin && admin.classList.contains("st-group"));
  assert.deepEqual([...admin.querySelectorAll("a.st-label")].map(text), ["Users", "Audit log"]);
  assert.equal(holder.querySelector("[data-signin-off]"), null);
});

test("Glossary: the look's header with its breadcrumb, a labelled search box, and the two-column list", async () => {
  await go("#/help/glossary");
  await until(() => $("[data-glossary]"), 3000, "the Glossary");
  const header = $("main > .page-header");
  assert.ok(header && header.querySelector(".crumbs"), "a page below a place keeps its breadcrumb");
  const box = $("[data-glossary] input[data-glossary-filter]");
  assert.equal(box.type, "search");
  assert.ok(box.closest("label"), "a label names it");
  assert.match(text(box.closest("label")), /Search the glossary/);
  assert.equal($("[data-glossary] svg[aria-hidden='true']") !== null, true, "the magnifier is decoration");
  assert.deepEqual([...$$("[data-glossary] thead th")].map(text), ["Word", "What it means"]);
  assert.ok($("section[data-technical]"), "the engine's names, and their whole card, are technical: hidden with the switch off");
});

test("Glossary: typing narrows the list to the words and meanings that hold it, and says when nothing does", async () => {
  const root = $("[data-glossary]");
  const box = root.querySelector("input[data-glossary-filter]");
  const rows = () => [...root.querySelectorAll("tbody tr")];
  const shown = () => rows().filter((r) => !r.hidden).map((r) => text(r.querySelector("td")));
  const all = rows().length;
  assert.ok(all >= 13);
  box.value = "comparison";
  box.dispatchEvent(new w.Event("input", { bubbles: true }));
  assert.ok(shown().includes("Comparison group"), "found by its word");
  assert.ok(shown().length < all, "narrowed");
  box.value = "QUALIFIES"; // case does not matter; found by its meaning ("One message per customer")
  box.dispatchEvent(new w.Event("input", { bubbles: true }));
  assert.deepEqual(shown(), ["One message per customer"]);
  assert.equal(root.querySelector("[data-glossary-none]").hidden, true);
  box.value = "zzzz";
  box.dispatchEvent(new w.Event("input", { bubbles: true }));
  assert.deepEqual(shown(), []);
  assert.equal(root.querySelector("[data-glossary-none]").hidden, false, "no word matches is said, not left blank");
  assert.equal(rows().length, all, "nothing was removed");
  box.value = "";
  box.dispatchEvent(new w.Event("input", { bubbles: true }));
  assert.equal(shown().length, all, "an empty search shows everything");
  assert.equal(root.querySelector("[data-glossary-none]").hidden, true);
});

test("filterGlossary: the count it returns, on any glossary markup", () => {
  const holder = w.document.createElement("div");
  holder.innerHTML = glossaryHtml([
    { shown: "Goal", means: "What you want a campaign to achieve." },
    { shown: "Contact list", means: "The one file of customers to contact." },
  ]);
  const root = holder.querySelector("[data-glossary]");
  assert.equal(filterGlossary(root, "file"), 1);
  assert.equal(filterGlossary(root, "  "), 2);
  assert.equal(filterGlossary(root, "nothing like it"), 0);
});

test("What's new: dated entries, each a date, a title and a sentence, newest first", async () => {
  await go("#/help/whats-new");
  await until(() => $("[data-whats-new]"), 3000, "What's new");
  const file = JSON.parse(fs.readFileSync(NEWS_FILE, "utf8"));
  const entries = $$("[data-news]");
  assert.equal(entries.length, file.entries.length);
  entries.forEach((el, i) => {
    assert.equal(text(el.querySelector(".sp-when")), file.entries[i].when, "the date, as its own cell");
    assert.equal(text(el.querySelector("strong")), file.entries[i].title);
    assert.equal(text(el.querySelector(".sp-text")), file.entries[i].text);
    assert.ok(el.classList.contains("st-entry"));
  });
  const empty = w.document.createElement("div");
  empty.innerHTML = whatsNewHtml([]);
  assert.ok(empty.querySelector(".empty-state .es-icon svg"), "with no entries: an icon, a sentence and one action");
  assert.equal(empty.querySelectorAll("a.btn").length, 1);
});
