/* Plan M, M170: ui/labels.js, the glossary as data (no page needed, so no jsdom). */
import { test } from "node:test";
import assert from "node:assert/strict";
import fs from "node:fs";
import { GLOSSARY, findNeverShown, glossaryMarkdown, neverShown, term } from "../../../../../ui/labels.js";

const BASELINE = JSON.parse(fs.readFileSync(new URL("./baseline.json", import.meta.url), "utf8"));
const GLOSSARY_MD = new URL("../../../../../docs/ux/GLOSSARY.md", import.meta.url);

test("term(key) returns the word to show, and refuses a key that is not in the glossary", () => {
  assert.equal(term("goal"), "Goal");
  assert.equal(term("contactList"), "Contact list");
  assert.equal(term("comparisonGroup"), "Comparison group");
  assert.equal(term("likelihood"), "Likelihood");
  assert.equal(term("priority"), "Priority");
  assert.equal(term("currentModel"), "Current model");
  assert.equal(term("newCandidate"), "New candidate");
  assert.equal(term("evidenceProven"), "Proven");
  assert.equal(term("evidenceEstimated"), "Estimated");
  assert.equal(term("evidenceNotProven"), "Not yet proven");
  assert.throws(() => term("nope"), /no glossary entry "nope"/);
});

test("every entry has a key, a concept, a reason; keys and never-shown terms are unique", () => {
  const keys = GLOSSARY.map((e) => e.key);
  assert.equal(new Set(keys).size, keys.length, "keys are unique");
  for (const e of GLOSSARY) {
    assert.ok(e.concept && e.reason, `${e.key} says what it is and why it exists`);
    assert.ok(Array.isArray(e.never), `${e.key}.never is a list`);
  }
  const terms = neverShown().map((n) => n.term);
  assert.equal(new Set(terms).size, terms.length, "a term belongs to one entry only");
  for (const t of terms) assert.equal(t, t.toLowerCase(), `"${t}" is written in lower case`);
});

test("the plan's §4.1 terms and the spec's list are all there", () => {
  const terms = new Set(neverShown().map((n) => n.term));
  for (const t of [
    "use case", "run", "train", "scoring run", "treat list", "scored list", "treat = 1", "holdout = 1", "explore = 1",
    "holdout", "control group", "explore slice", "uplift", "x-learner", "s-learner", "t-learner", "auuc", "qini",
    "propensity", "score", "band", "champion", "challenger", "causal", "shap", "feature importance", "drift", "psi",
    "arbitration", "recipe", "lightgbm", "xgboost", "catboost", "ensemble",
  ]) {
    assert.ok(terms.has(t), `"${t}" is a never-shown term`);
  }
});

test("a shown word is never itself a never-shown word", () => {
  for (const e of GLOSSARY) {
    assert.deepEqual(findNeverShown(e.shown), {}, `the word shown for ${e.key} ("${e.shown}") must be clean`);
  }
});

test("matching: case-insensitive, whole words, with plurals", () => {
  assert.deepEqual(findNeverShown("Holdout, HOLDOUTS and a holdout."), { holdout: 3 });
  assert.deepEqual(findNeverShown("running, a prune and the rerun"), {}, "'run' inside a longer word is not a match");
  assert.deepEqual(findNeverShown("Three bands: a Band each"), { band: 2 });
  assert.deepEqual(findNeverShown("Recipes and one recipe"), { recipe: 2 });
  assert.deepEqual(findNeverShown("shapes and runes"), {}, "'es' is not a plural of every word: 'shapes' is not SHAP");
  assert.deepEqual(findNeverShown("Two boxes of prunes"), {});
  assert.deepEqual(findNeverShown("Propensities and propensity"), { propensity: 1, propensities: 1 });
  assert.deepEqual(findNeverShown("SHAP and PSI, AUUC and Qini"), { shap: 1, psi: 1, auuc: 1, qini: 1 });
  assert.deepEqual(findNeverShown("An X-learner (LightGBM) and an Ensemble"), { "x-learner": 1, lightgbm: 1, ensemble: 1 });
});

test("matching: the flag phrases ignore spacing, and a phrase claims its words before a single word does", () => {
  assert.deepEqual(findNeverShown("treat = 1, treat=1 and holdout =1"), { "treat = 1": 2, "holdout = 1": 1 });
  assert.deepEqual(findNeverShown("a scoring run and a run"), { "scoring run": 1, run: 1 });
  assert.deepEqual(findNeverShown("Treat list\nand the scored list"), { "treat list": 1, "scored list": 1 });
  assert.deepEqual(findNeverShown("hold-out group"), { "hold-out": 1 });
});

test("words around the glossary that are fine stay fine", () => {
  assert.deepEqual(findNeverShown("Contact list, Comparison group, Likelihood, Priority High, Current model."), {});
  assert.deepEqual(findNeverShown("Customers have changed since the model was trained."), {});
});

test("every term in the baseline is a real never-shown term with a positive whole count", () => {
  const known = new Set(neverShown().map((n) => n.term));
  for (const [page, terms] of Object.entries(BASELINE)) {
    for (const [t, count] of Object.entries(terms)) {
      assert.ok(known.has(t), `${page}: "${t}" is not a glossary term`);
      assert.ok(Number.isInteger(count) && count > 0, `${page}: "${t}" has count ${count}`);
    }
  }
});

test("docs/ux/GLOSSARY.md is the glossary rendered from ui/labels.js, word for word", () => {
  const regenerate = `node -e "import('./ui/labels.js').then((m) => process.stdout.write(m.glossaryMarkdown()))" > docs/ux/GLOSSARY.md`;
  assert.ok(fs.existsSync(GLOSSARY_MD), `docs/ux/GLOSSARY.md is missing; write it with: ${regenerate}`);
  assert.equal(
    fs.readFileSync(GLOSSARY_MD, "utf8"),
    glossaryMarkdown(),
    `docs/ux/GLOSSARY.md and ui/labels.js disagree; regenerate the document with: ${regenerate}`,
  );
});
