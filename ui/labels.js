/* The glossary (Plan M §4.1, M170, DEC-1600): one word for each thing a marketer sees, and the engine
   words that must never be shown outside a "Technical details" section.

   It is data, in a plain ES module that the browser and node import alike. Three things read it:

   - pages, through `term(key)`, to get the word to show (M172 sweeps every page onto it);
   - the visible-text check (tests/integration/production/ui/glossary/visible_text.test.mjs), through
     `neverShown()` and `findNeverShown(text)`, which fail a page that shows a never-shown word beyond that page's recorded baseline;
   - docs/ux/GLOSSARY.md, which `glossaryMarkdown()` writes and a test compares, so the document for humans
     cannot drift from this file.

   Each entry says WHY it exists in `reason`, so the next person to add a word knows what the rule protects.

   How a never-shown term is matched (`findNeverShown`):
   - case-insensitive, and as a whole word: "run" does not match "running" or "prune";
   - an optional plural is allowed after any term: "s" ("band" matches "bands", "recipe" matches
     "recipes"), or "es" after a term ending in s, x, z, ch or sh; an irregular plural is listed on its
     own ("propensities");
   - a space in a term matches any run of white space, and "=" matches with or without spaces around it
     ("treat = 1" matches "treat=1");
   - the text a person reads is matched, never the markup: the collector in the test removes technical
     sections, hidden elements, scripts and styles first.
*/

/**
 * @typedef {object} Entry
 * @property {string} key       stable id used by `term(key)`
 * @property {string} concept   what the thing is, in the engine's own words (for the human glossary)
 * @property {string} shown     the word(s) a marketer sees
 * @property {string[]} never   words never shown outside technical details (see the matching rules above)
 * @property {string} reason    why this entry exists
 */

/** @type {Entry[]} */
export const GLOSSARY = [
  {
    key: "goal",
    concept: "use case",
    shown: "Goal",
    never: ["use case"],
    reason:
      "A marketer plans a goal (win back lapsed customers); 'use case' is how the engine names its configuration (Plan M §4.1).",
  },
  {
    key: "refresh",
    concept: "a run (train or score)",
    shown: "Refresh the list",
    never: ["run", "train", "training", "scoring run"],
    reason:
      "Training happens on a goal's first campaign and scoring again is 'Refresh the list'. The person never chooses between running, training and scoring (Plan M §1 M3). 'Trained' is not listed: the glossary's own phrase for drift says 'since the model was trained'.",
  },
  {
    key: "contactList",
    concept: "treat list, contact list, scored list",
    shown: "Contact list",
    never: ["treat list", "scored list", "treat = 1", "holdout = 1", "explore = 1"],
    reason:
      "One file, one name. The file a sending tool needs is the contact list; the flag columns in it are engine columns (Plan M §3 and M171).",
  },
  {
    key: "comparisonGroup",
    concept: "holdout, control group",
    shown: "Comparison group",
    never: ["holdout", "hold-out", "control group", "explore slice", "explore share"],
    reason:
      "The customers who are deliberately not contacted so the result can be measured: a marketer calls them a comparison group (not contacted).",
  },
  {
    key: "effect",
    concept: "uplift, campaign-effect model",
    shown: "the effect of contacting them",
    never: [
      "uplift",
      "x-learner",
      "s-learner",
      "t-learner",
      "s/t-learner",
      "r-learner",
      "dr-learner",
      "meta-learner",
      "auuc",
      "qini",
    ],
    reason:
      "The question is 'does contacting them change what they do?'. The estimator names and the curves that rank them are analyst words.",
  },
  {
    key: "likelihood",
    concept: "propensity, score, probability",
    shown: "Likelihood",
    never: ["propensity", "propensities", "score"],
    reason:
      "How likely a customer is to act. The plan forbids 'score' as a noun, and a text check cannot tell a noun from a verb, so every whole-word 'score' is flagged: the verb ('Score new data') belongs to the hidden run steps and goes with them.",
  },
  {
    key: "priority",
    concept: "band High / Medium / Low",
    shown: "Priority",
    never: ["band"],
    reason: "Customers are grouped into High, Medium and Low priority; 'band' is the engine's name for the group.",
  },
  {
    key: "currentModel",
    concept: "champion",
    shown: "Current model",
    never: ["champion"],
    reason: "The model in use is the current model; 'champion' comes from the model registry's promotion rule.",
  },
  {
    key: "newCandidate",
    concept: "challenger",
    shown: "New candidate",
    never: ["challenger"],
    reason: "A model waiting to replace the current one is a new candidate; 'challenger' is a registry word.",
  },
  {
    key: "evidenceProven",
    concept: "Causal",
    shown: "Proven",
    never: ["causal", "evidence label"],
    reason: "The evidence label in words: Proven, Estimated, Not yet proven. 'Causal' is a statistics word.",
  },
  {
    key: "evidenceEstimated",
    concept: "Estimated",
    shown: "Estimated",
    never: [],
    reason: "The middle evidence label; already a plain word, kept so the three labels are listed together.",
  },
  {
    key: "evidenceNotProven",
    concept: "Not proven",
    shown: "Not yet proven",
    never: [],
    reason: "The third evidence label: nothing was shown, which is not the same as 'it does not work'.",
  },
  {
    key: "drivers",
    concept: "SHAP, feature importance",
    shown: "What drives it",
    never: ["shap", "feature importance", "feature", "importance"],
    reason:
      "The reasons behind a ranking, in business words. SHAP, 'feature' and 'importance' are modelling words; a column is 'a detail about the customer'.",
  },
  {
    key: "changed",
    concept: "drift, PSI",
    shown: "Customers have changed since the model was trained",
    never: ["drift", "psi"],
    reason: "What drift means to a marketer: the customers look different from the ones the model learned on.",
  },
  {
    key: "onePerCustomer",
    concept: "arbitration",
    shown: "One message per customer",
    never: ["arbitration"],
    reason: "When several campaigns want the same customer only one message goes; 'arbitration' is the engine's rule.",
  },
  {
    key: "fixes",
    concept: "recipe",
    shown: "The fixes you approved",
    never: ["recipe"],
    reason:
      "The data fixes the person approved in Guided setup, saved so next month's file is prepared the same way. 'Recipe' is the engine's name.",
  },
  {
    key: "modelNames",
    concept: "model names (the algorithm that won)",
    shown: "",
    never: [
      "lightgbm",
      "xgboost",
      "catboost",
      "ensemble",
      "random forest",
      "randomforest",
      "logistic regression",
      "logisticregression",
      "gradient boosting",
      "neural net",
      "neural network",
      "neuralnet",
      "autogluon",
    ],
    reason: "Which algorithm won is hidden from a marketer. An analyst sees it under technical details.",
  },
];

/** The word(s) to show for a concept. An unknown key is a programming error, so it throws. */
export function term(key) {
  const entry = GLOSSARY.find((e) => e.key === key);
  if (!entry) throw new Error(`labels.term: no glossary entry "${key}"`);
  return entry.shown;
}

/** Every never-shown term with the entry it belongs to: `[{term, key}]`, in glossary order. */
export function neverShown() {
  return GLOSSARY.flatMap((entry) => entry.never.map((t) => ({ term: t, key: entry.key })));
}

const escapeRegex = (s) => s.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");

/** The regular expression for one never-shown term, per the matching rules at the top of this file. */
export function termPattern(text) {
  const words = text
    .trim()
    .split(/\s*=\s*|\s+/)
    .filter(Boolean);
  const hasEquals = /=/.test(text);
  const body = hasEquals
    ? text
        .trim()
        .split(/\s*=\s*/)
        .map((part) => part.trim().split(/\s+/).map(escapeRegex).join("\\s+"))
        .join("\\s*=\\s*")
    : words.map(escapeRegex).join("\\s+");
  // A term ending in a non-letter (such as "= 1") takes no plural; "es" only after s, x, z, ch, sh.
  const plural = !/[a-z]$/i.test(text) ? "" : /(s|x|z|ch|sh)$/i.test(text) ? "(?:es)?" : "(?:s)?";
  return new RegExp(`(?<![A-Za-z0-9_])${body}${plural}(?![A-Za-z0-9_])`, "gi");
}

let compiled = null;
const patterns = () => {
  compiled ??= neverShown().map((item) => ({ ...item, pattern: termPattern(item.term) }));
  return compiled;
};

/**
 * The never-shown terms found in `text`, as `{term: count}` keyed by the glossary term (not by the form
 * found, so "Recipes" and "recipe" count together). Overlapping matches are not double counted: a text
 * span matched by a longer term ("scoring run", "treat = 1") is not matched again by a shorter one ("run").
 */
export function findNeverShown(text) {
  const taken = [];
  const found = {};
  // Longest terms first, so a phrase claims its words before a single word can.
  const ordered = [...patterns()].sort((a, b) => b.term.length - a.term.length);
  for (const { term: t, pattern } of ordered) {
    pattern.lastIndex = 0;
    for (const m of text.matchAll(pattern)) {
      const start = m.index;
      const end = start + m[0].length;
      if (taken.some(([s, e]) => start < e && end > s)) continue;
      taken.push([start, end]);
      found[t] = (found[t] || 0) + 1;
    }
  }
  return found;
}

/** docs/ux/GLOSSARY.md, written from the data above. A test compares the file with this text. */
export function glossaryMarkdown() {
  const lines = [
    "# Glossary: the words a marketer sees",
    "",
    "<!-- Generated from ui/labels.js (Plan M, M170). Do not edit by hand. To regenerate:",
    "     node -e \"import('./ui/labels.js').then((m) => process.stdout.write(m.glossaryMarkdown()))\" > docs/ux/GLOSSARY.md -->",
    "",
    "One word for each thing. The right-hand column lists the words that are never shown outside a",
    '"Technical details" section; a test renders the main pages and fails on any of them beyond each page\'s',
    "recorded baseline (`tests/integration/production/ui/glossary/baseline.json`), which M172 empties (Plan M §4.1).",
    "",
    "| Concept | Shown to a marketer | Never shown outside Technical details | Why |",
    "|---|---|---|---|",
  ];
  for (const e of GLOSSARY) {
    const never = e.never.length ? e.never.map((t) => `\`${t}\``).join(", ") : "(none)";
    const shown = e.shown ? `**${e.shown}**` : "*(hidden)*";
    lines.push(`| ${e.concept} | ${shown} | ${never} | ${e.reason} |`);
  }
  lines.push(
    "",
    "## How a term is matched",
    "",
    "- Case-insensitive, whole words: `run` does not match `running`.",
    "- An optional plural is allowed after any term (`s`, or `es` after a term ending in s, x, z, ch or sh);",
    "  irregular plurals are listed separately.",
    '- A space in a term matches any white space, and `=` matches with or without spaces around it.',
    "- Only text a person can read is checked: technical sections (`<details class=\"tech\">`, `[data-technical]`,",
    '  a `<details>` whose summary reads exactly "Technical details"), hidden elements, scripts and styles are',
    "  left out; `aria-label`, `title` and `placeholder` count as text. The summary of an excluded `<details>`",
    '  stays on screen when closed, so it is still checked unless it reads "Technical details" or "Details".',
    "",
  );
  return lines.join("\n");
}
