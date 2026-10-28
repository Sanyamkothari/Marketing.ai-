/* Plan M, M181 part A (DEC-1611): colour contrast of the palette against WCAG 2.1 AA (4.5:1; 3:1 for large text).

   Two checks, both read from the real style sheets (a11y/contrast.mjs):

   1. THE PALETTE: the pairs a marketer reads most - body text, secondary text, links, the primary button, the
      badges, the dark stage pills - are asserted by name, so a change to a token that breaks one of them fails
      with that pair's name. Each pair is also confirmed to be used by a rule in the style sheets.
   2. EVERY RULE: every rule in index.html and in every .js file under ui/ (not ui/v2/, the Autonomy app with its own palette) that sets a text colour is checked on the ground it sets
      (or, with none set, on --surface and --soft). The rules that fail today are listed in contrast_known.json, each
      with a reason, where it is, and the fix; the test fails on a failing pair that is not listed, and on a listed
      pair that now passes (so the list can only shrink).

   M185 (DEC-1615): the tokens are the ones the browser ends up with - ui/index.html's, then ui/theme.css's, which the
   page loads after every <style> - and theme.css's own rules are checked like every other style source. Since M185
   the primary button is the brand colour (--btn is --brand-blue, not --ink), pages sit on the canvas (--bg) instead
   of a white card, and the status tones have names of their own; the palette below names those pairs too.

   Environment: A11Y_DUMP_CONTRAST=<file> writes every failing pair as JSON, to author contrast_known.json. */
import { test } from "node:test";
import assert from "node:assert/strict";
import fs from "node:fs";
import { colourOf, luminance, pairsOf, ratio, required, rulesIn, styleSources, tokens } from "./contrast.mjs";

const KNOWN = new URL("./contrast_known.json", import.meta.url);
const known = JSON.parse(fs.readFileSync(KNOWN, "utf8"));
const sources = styleSources();
const indexHtml = sources.find((s) => s.file === "ui/index.html");
const themeCss = sources.find((s) => s.file === "ui/theme.css");
const tok = tokens(indexHtml.text, themeCss.text);

/** The pairs a person reads most. [name, foreground token, background token, what it is] */
const PALETTE = [
  ["body text", "--ink", "--surface", "headings and body text on a card"],
  ["body text on the page", "--ink", "--bg", "text directly on the page background"],
  ["secondary text", "--ink2", "--surface", "table cells, hints"],
  ["muted text", "--muted", "--surface", "captions, sub-headings, help text"],
  ["muted text on a panel", "--muted", "--soft", "captions on a soft panel"],
  ["link and linkbutton", "--brand-blue", "--surface", "links and link-style buttons"],
  ["primary button", "--btn-ink", "--btn", "the one primary button on a page"],
  ["dark stage pill", "--stage-ink", "--stage", "journey stage names"],
  ["ok badge", "--ok", "--ok-t", "good / passed"],
  ["warning badge", "--warn", "--warn-t", "warnings"],
  ["bad badge", "--bad", "--bad-t", "errors, refused"],
  ["blue tag", "--p", "--p-t", "type chip, blue"],
  ["violet tag", "--g", "--g-t", "type chip, violet"],
  ["teal tag", "--ink2", "--h-t", "type chip, teal (.chip.type.t-H uses --ink2 on the tint)"],
  ["ok text", "--ok", "--surface", "success text on a card"],
  ["warning text", "--warn", "--surface", "warning text on a card"],
  ["error text", "--bad", "--surface", "error text on a card"],
  // M185: the look's own pairs
  ["muted text on the page", "--muted", "--bg", "a page's subtitle and captions, straight on the canvas"],
  ["secondary text on the page", "--ink2", "--bg", "notes straight on the canvas"],
  ["link on the page", "--brand-blue", "--bg", "a link straight on the canvas"],
  ["selected place", "--brand-blue", "--brand-50", "the sidebar's active place, a selected menu entry, an info chip"],
  ["good chip", "--good", "--good-t", "Measured, Proven, Read correctly"],
  ["warning chip", "--warning", "--warning-t", "Waiting for results, Not tested yet"],
  ["critical chip", "--critical", "--critical-t", "Failed"],
  ["neutral chip", "--neutral", "--neutral-t", "Draft, Not yet proven, quiet tags"],
];

const hex = (name) => colourOf(`var(${name})`, tok);

test("the tokens are read from index.html, then theme.css (M185), as the browser reads them", () => {
  for (const name of ["--ink", "--surface", "--bg", "--soft", "--muted", "--btn", "--btn-ink", "--brand-blue", "--ok", "--ok-t"]) {
    assert.match(hex(name) || "", /^#[0-9a-f]{6}$/i, `${name} did not resolve to a colour`);
  }
  for (const name of ["--brand-50", "--good", "--good-t", "--warning", "--critical", "--neutral", "--neutral-t"]) {
    assert.match(hex(name) || "", /^#[0-9a-f]{6}$/i, `${name} (theme.css) did not resolve to a colour`);
  }
  // M185 (DEC-1615): the primary button is the brand colour, not black; v1 made --btn an alias of --ink
  assert.equal(hex("--btn"), hex("--brand-blue"), "the alias --btn is --brand-blue since M185");
  assert.equal(hex("--btn-ink"), hex("--surface"));
  // a later file's token replaces an earlier one's: theme.css darkens --muted for the canvas
  assert.equal(tokens(indexHtml.text)["--muted"], "#6B7280");
  assert.equal(tok["--muted"], "#667085");
});

test("the contrast formula is the WCAG one", () => {
  assert.equal(Math.round(ratio("#000000", "#ffffff") * 100) / 100, 21);
  assert.equal(Math.round(ratio("#767676", "#ffffff") * 100) / 100, 4.54);
  assert.equal(Math.round(ratio("#ffffff", "#ffffff") * 100) / 100, 1);
  assert.ok(luminance("#ffffff") > luminance("#777777"));
  assert.equal(required({ "font-size": "28px" }), 3);
  assert.equal(required({ "font-size": "20px", "font-weight": "700" }), 3);
  assert.equal(required({ "font-size": "20px", "font-weight": "400" }), 4.5);
  assert.equal(required({ "font-size": "13px" }), 4.5);
});

test("the rule reader finds a planted low-contrast rule and ignores what is not CSS", () => {
  const text = [
    ".bad{color:#aaaaaa;background:#ffffff;font-size:13px}",
    "@media (max-width:700px){.alsobad{color:var(--faint)}}",
    "if (x) { color: red; }",
    "const s = `.tpl{color:${c}}`;",
    ".grad{color:#fff;background:linear-gradient(red,blue)}",
    ".off:disabled{color:#ccc;background:#fff}",
  ].join("\n");
  const pairs = pairsOf(rulesIn(text), tok);
  const names = pairs.map((p) => p.selector);
  assert.ok(names.includes(".bad") && names.includes(".alsobad"));
  assert.ok(!names.includes(".tpl") && !names.includes(".grad") && !names.includes(".off:disabled"));
  const bad = pairs.find((p) => p.selector === ".bad");
  assert.ok(ratio(bad.fg, bad.bg) < bad.need, "#aaa on white is below 4.5");
  assert.equal(pairs.filter((p) => p.selector === ".alsobad").length, 2, "a text-only rule is checked on --surface and --soft");
});

for (const [name, fg, bg, what] of PALETTE) {
  test(`palette: ${name} (${what}) reaches 4.5:1, or is listed as known`, () => {
    const r = ratio(hex(fg), hex(bg));
    const key = `palette: ${name}`;
    if (known.palette[key]) {
      assert.ok(r < 4.5, `${key} now passes at ${r.toFixed(2)}:1; remove it from contrast_known.json`);
    } else {
      assert.ok(r >= 4.5, `${key}: ${fg} on ${bg} is ${r.toFixed(2)}:1, below 4.5:1`);
    }
  });
}

test("every palette pair that names two tokens is used by a rule", () => {
  const rules = sources.flatMap((s) => rulesIn(s.text));
  const used = (fg, bg) =>
    rules.some(({ props }) => {
      const c = colourOf(props.color, tok);
      const b = colourOf(props["background-color"] || props.background, tok);
      return c === hex(fg) && b === hex(bg);
    });
  const unused = PALETTE.filter(
    ([, fg, bg]) =>
      ["--ok", "--warn", "--bad", "--p", "--g", "--h", "--good", "--warning", "--critical", "--neutral"].includes(fg) &&
      bg.endsWith("-t") &&
      !used(fg, bg),
  ).map(([n]) => n);
  // A tag colour reaches its tint through custom properties on a local rule (`.t-P{--c:...}`), which this reader does
  // not follow, so only the status badges must be found.
  assert.deepEqual(unused.filter((n) => /ok|warning|bad|chip/.test(n)), [], "a status badge pair in this list is not used by any rule");
});

const keyOf = (p) => `${p.selector} :: ${p.fgText} on ${p.bgText}`;
const worst = new Map();
for (const source of sources) {
  const rules = rulesIn(source.text);
  for (const pair of pairsOf(rules, tok)) {
    const r = Math.round(ratio(pair.fg, pair.bg) * 100) / 100;
    const seen = worst.get(keyOf(pair));
    if (r < pair.need && (!seen || r < seen.ratio)) worst.set(keyOf(pair), { ...pair, file: source.file, ratio: r });
  }
}
const failing = [...worst.values()];

test("every text colour in the style sheets reaches AA on its ground, or is listed as known", () => {
  const dump = process.env.A11Y_DUMP_CONTRAST;
  if (dump) {
    const byKey = {};
    for (const p of failing) byKey[keyOf(p)] = { ratio: p.ratio, need: p.need, file: p.file, fg: p.fg, bg: p.bg };
    fs.writeFileSync(dump, `${JSON.stringify(byKey, null, 1)}\n`);
  }
  const now = new Set(failing.map(keyOf));
  const fresh = [...now].filter((k) => !known.rules[k]);
  assert.deepEqual(
    fresh.map((k) => `${k} (${failing.find((p) => keyOf(p) === k).ratio}:1 in ${failing.find((p) => keyOf(p) === k).file})`),
    [],
    "text below AA contrast that is not on contrast_known.json (change the colour, do not add to the list)",
  );
  const fixed = Object.keys(known.rules).filter((k) => !now.has(k));
  assert.deepEqual(fixed, [], "contrast_known.json lists a rule that now passes or is gone; remove it");
  for (const [k, why] of Object.entries({ ...known.rules, ...known.palette })) {
    assert.ok(why.reason && why.where && why.fix, `${k}: reason, where and fix are all required`);
  }
  process.stdout.write(
    `\nContrast: ${failing.length} text rules below AA, all listed (${Object.keys(known.palette).length} named palette pairs)\n` +
      `${[...failing].sort((a, b) => a.ratio - b.ratio).map((p) => `#   ${String(p.ratio).padStart(5)}:1 (needs ${p.need})  ${keyOf(p)}  [${p.file}]`).join("\n")}\n`,
  );
});

test("the checked sources include the page and its modules", () => {
  assert.ok(sources.length > 50, "ui/ has far more than 50 files");
  assert.ok(sources.some((s) => s.file === "ui/modules/connections/styles.js"));
  assert.ok(rulesIn(indexHtml.text).length > 100, "index.html's style sheet has hundreds of rules");
});
