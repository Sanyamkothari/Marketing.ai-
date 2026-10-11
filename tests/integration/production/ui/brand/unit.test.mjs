/* Plan M M182 (DEC-1612): ui/brand.js on its own - cleaning an answer, the markup it hands the bar and the title rule.
   Each `import("...?x")` is a fresh instance of the module, with its own state. */
import { test, afterEach } from "node:test";
import assert from "node:assert/strict";
import { installShellPage, me } from "./support.mjs";

installShellPage({ hash: "#/__start", world: { me: null, approvals: null } });
const instances = [];
const fresh = async (tag) => {
  const module = await import(`../../../../../ui/brand.js?unit-${tag}`);
  instances.push(module);
  return module;
};
// Every instance watches the same <title>: one that is left running would answer the next test's titles.
afterEach(() => {
  while (instances.length) instances.pop()._resetBrandForTests();
  document.title = "";
});
const { LOGO } = await import("../../../../../ui/dom.js");

test("cleanBrand: complete answers pass, everything else is today's value for that part", async () => {
  const { cleanBrand, DEFAULT_BRAND } = await fresh("clean");
  assert.deepEqual(cleanBrand(null), DEFAULT_BRAND);
  assert.deepEqual(cleanBrand("blue"), DEFAULT_BRAND);
  assert.deepEqual(cleanBrand({}), DEFAULT_BRAND);
  assert.deepEqual(cleanBrand(me("custom_classic").brand), {
    name: "Acme Growth",
    logo_url: "https://cdn.example.com/acme-logo.svg",
    logo_alt: "Acme",
    color: "#0B5D3B",
  });
  for (const bad of ["javascript:alert(1)", "data:image/png;base64,AAA", "http://a.example/x.png", "//a.example/x.png", 'https://a.example/"x', "https://a.example/a b"]) {
    assert.equal(cleanBrand({ logo_url: bad }).logo_url, "", bad);
  }
  assert.equal(cleanBrand({ logo_url: "/static/acme.svg" }).logo_url, "/static/acme.svg");
  for (const bad of ["red", "#12", "#1234567", "1E57BD", "#GG57BD", 5, null]) {
    assert.equal(cleanBrand({ color: bad }).color, "#1E57BD", String(bad));
  }
  assert.equal(cleanBrand({ name: "  Acme  " }).name, "Acme");
  assert.equal(cleanBrand({ name: "x".repeat(81) }).name, "Marketing AI");
});

test("the default brand hands the bar today's markup, character for character", async () => {
  const brand = await fresh("default");
  assert.equal(brand.brandNameHtml(), "Marketing AI");
  assert.equal(brand.brandLogoAlt(), "Minfy");
  assert.equal(brand.brandLogoHtml(LOGO), LOGO);
  assert.equal(brand.brandTitle("Campaigns · Marketing AI"), "Campaigns · Marketing AI");
  assert.equal(brand.brandTitle("Marketing AI · Minfy"), "Marketing AI · Minfy");
  assert.equal(document.documentElement.hasAttribute("style"), false);
  assert.equal(document.head.querySelector("style#brand-styles"), null);
});

test("a custom name is escaped, and marked as the deployment's own words", async () => {
  const brand = await fresh("escape");
  brand.setBrand({ name: "<b>Acme</b> & Co" });
  assert.equal(brand.brandNameHtml(), "<span data-user-text>&lt;b&gt;Acme&lt;/b&gt; &amp; Co</span>");
  brand.setBrand({ logo_url: "https://a.example/x.svg?a=1&b=2", logo_alt: 'The "A" team' });
  assert.equal(
    brand.brandLogoHtml(LOGO),
    '<img class="logo logo-img" src="https://a.example/x.svg?a=1&amp;b=2" alt="The &quot;A&quot; team">',
  );
});

test("the title rule: the product's name swaps, any other title stays, and it cannot loop", async () => {
  const brand = await fresh("title");
  brand.setBrand({ name: "Acme Growth", logo_alt: "Acme" });
  assert.equal(brand.brandTitle("Marketing AI · Minfy"), "Acme Growth · Acme");
  assert.equal(brand.brandTitle("Reports · Marketing AI"), "Reports · Acme Growth");
  assert.equal(brand.brandTitle("Reports · Hillstrom · Marketing AI"), "Reports · Hillstrom · Acme Growth");
  assert.equal(brand.brandTitle("Reports · Acme Growth"), "Reports · Acme Growth", "already done");
  assert.equal(brand.brandTitle("Sign in"), "Sign in");
  // a name that itself ends with the product's old name must not be rewritten again and again
  brand.setBrand({ name: "Q · Marketing AI" });
  const once = brand.brandTitle("Reports · Marketing AI");
  assert.equal(once, "Reports · Q · Marketing AI");
  assert.equal(brand.brandTitle(once), once);
});

test("the colour is a token on <html> and in the tab's icon, and the default removes both", async () => {
  const icon = document.createElement("link");
  icon.setAttribute("rel", "icon");
  const original = "data:image/svg+xml,<svg xmlns='http://www.w3.org/2000/svg'><circle fill='%231E57BD'/></svg>";
  icon.setAttribute("href", original);
  document.head.appendChild(icon);
  const brand = await fresh("colour");
  brand.setBrand({ color: "#0b5d3b" });
  assert.equal(document.documentElement.style.getPropertyValue("--brand-blue"), "#0B5D3B");
  assert.ok(icon.getAttribute("href").includes("%230B5D3B"));
  brand.setBrand({});
  assert.equal(document.documentElement.hasAttribute("style"), false);
  assert.equal(icon.getAttribute("href"), original);
});

test("a change is announced once, and the same brand is not announced again", async () => {
  const brand = await fresh("event");
  let heard = 0;
  window.addEventListener(brand.BRAND_EVENT, () => (heard += 1));
  brand.setBrand({ name: "Acme" });
  brand.setBrand({ name: "Acme" });
  assert.equal(heard, 1);
  brand.setBrand({});
  assert.equal(heard, 2);
});

test("a title written under one brand follows the next brand, and the default", async () => {
  const brand = await fresh("retitle");
  document.title = "Overview · Marketing AI";
  brand.setBrand({ name: "Acme" }, { fromServer: true });
  assert.equal(document.title, "Overview · Acme");
  brand.setBrand({ name: "Beta" });
  assert.equal(document.title, "Overview · Beta", "a remembered brand replaced by the server's answer");
  brand.setBrand({});
  assert.equal(document.title, "Overview · Marketing AI", "back to the defaults");
  document.title = "Marketing AI · Minfy";
  brand.setBrand({ name: "Acme", logo_alt: "A" });
  assert.equal(document.title, "Acme · A");
  brand.setBrand({ name: "Beta", logo_alt: "B" });
  assert.equal(document.title, "Beta · B");
  brand.setBrand({});
  assert.equal(document.title, "Marketing AI · Minfy");
  // a name that ends with an earlier name: the longest match is the one swapped
  document.title = "Reports · Marketing AI";
  brand.setBrand({ name: "Q · Marketing AI" });
  assert.equal(document.title, "Reports · Q · Marketing AI");
  brand.setBrand({});
  assert.equal(document.title, "Reports · Marketing AI");
});

test("the rules beyond the token exist for a custom colour only, and follow it", async () => {
  const brand = await fresh("rules");
  const styleText = () => document.getElementById("brand-styles")?.textContent ?? null;
  assert.equal(styleText(), null);
  brand.setBrand({ color: "#0B5D3B" });
  assert.equal(styleText(), "html .logo .b{fill:#1E57BD}html .logo .s{stroke:#1E57BD}html .run-pct{border-color:rgba(11,93,59,0.2)}");
  brand.setBrand({ color: "#7A1F1F", logo_url: "/ui/acme.svg" });
  assert.ok(styleText().startsWith(".logo-img{"));
  assert.ok(styleText().endsWith("rgba(122,31,31,0.2)}"));
  brand.setBrand({ logo_url: "/ui/acme.svg" });
  assert.equal(styleText(), ".logo-img{object-fit:contain;object-position:left center}");
  brand.setBrand({});
  assert.equal(styleText(), null, "the default leaves no style behind");
});
