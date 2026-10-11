/* The glossary (Plan M §4.1, M170, DEC-1600): one word for each thing a marketer sees, and the engine
   words that must never be shown outside a "Technical details" section.

   It is data, in a plain ES module that the browser and node import alike. Three things read it:

   - pages, through `term(key)` for the word to show, `goalName(id)` for a goal's name (Plan M §4.2),
     `plainText(text)` for an engine sentence in the page's words and `userText(value)` for a name the
     person supplied (M172, DEC-1602);
   - the visible-text check (tests/integration/production/ui/glossary/visible_text.test.mjs), through
     `neverShown()` and `findNeverShown(text)`, which fail a page that shows a never-shown word: M172 emptied
     the per-page baseline, so any such word outside technical details now fails;
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

/* Goal names (Plan M §4.2, M172, DEC-1602): what each goal is called on screen, by its use-case id. Display
   only: the id, the use case's own `name` in configs/use_cases/*.yaml and every API answer are unchanged
   (the engine writes that name into run records, reports and the helper's prompts). The first seven are the
   plan's table; the rest are goals of the other industry journeys whose configured name carries an engine word.
   A goal not listed here keeps its configured name. */
export const GOAL_NAMES = Object.freeze({
  "targeted-advertisement": "Find customers likely to buy",
  "win-back-campaign": "Win back lapsed customers",
  "telco-churn": "Keep customers likely to leave",
  "payment-propensity": "Get customers to pay on time",
  rca: "Understand why customers leave",
  "hillstrom-email": "Choose the best e-mail for each customer",
  "ai-onboarding-assistant": "Answer customer questions (AI assistant)",
  "card-default-propensity": "Find card accounts likely to miss a payment",
  "first-purchase-propensity": "Find browsers likely to buy for the first time",
  "criteo-uplift": "Show ads where they change what people do",
});

/* A goal's short name, for a campaign's short name on lists ("Win back lapsed customers — Oct 2026", at most 40
   characters with the month). Only the goals whose display name is too long for that have one. */
export const GOAL_SHORT_NAMES = Object.freeze({
  "telco-churn": "Keep customers from leaving",
  rca: "Why customers leave",
  "hillstrom-email": "Best e-mail for each customer",
  "ai-onboarding-assistant": "Answer customer questions",
  "card-default-propensity": "Card accounts at risk",
  "first-purchase-propensity": "First-time buyers",
  "criteo-uplift": "Ads that change behaviour",
});

/** The short name of a goal: its short form when it has one, else its display name (`goalName`). */
export function goalShortName(id, fallback = "") {
  return (id && GOAL_SHORT_NAMES[id]) || goalName(id, fallback);
}

/** The operational goals (Plan M §1 M6): they contact no customer, so Home folds them under "More goals". */
export const MORE_GOALS = Object.freeze(["order-fulfillment", "fault-prediction"]);

/** The name to show for a goal: its Plan M name by id, else the name its configuration gives (`fallback`). */
export function goalName(id, fallback = "") {
  return (id && GOAL_NAMES[id]) || fallback || id || "";
}

/* Names the person supplied are not our wording (M172, DEC-1602). A file name ("training.csv"), a campaign
   label they typed, a column of their data: the glossary cannot rename them and the visible-text check must
   not count them. Each is drawn inside an element carrying `data-user` (`userText(value)` writes one), which
   the check leaves out together with its title. Only the person's own string goes inside the mark, never a
   sentence of ours around it, so the mark cannot hide our own words. */
export const USER_TEXT_ATTRIBUTE = "data-user";

const escapeHtml = (s) =>
  String(s).replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" })[c]);

/** A name the person supplied, escaped and marked as theirs: `<span data-user>training.csv</span>`. */
export function userText(value, tag = "span") {
  return `<${tag} ${USER_TEXT_ATTRIBUTE}>${escapeHtml(value)}</${tag}>`;
}

/* Engine sentences, in a marketer's words (M172, DEC-1602). Some text a page shows is written by the engine or the
   configuration (a mode label, a check's message, a column's reason) and is pinned there by tests that read its
   meaning, so the page maps it instead of the engine changing it. Each rule is [what the engine writes, what the page
   shows]; a string rule matches the whole text, a regular expression rule replaces inside it. The API answer is
   unchanged; only what is drawn differs. */
export const ENGINE_TEXT = Object.freeze([
  // configs/engine.yaml ui.mode_labels / run_button / mode_help (the Setup mode switch and its button)
  ["Train a model", "Learn from past results"],
  ["Score new data", "Make a contact list"],
  ["Run training", "Build the model"],
  ["Score data", "Make the contact list"],
  [
    "Upload new records without the outcome. The saved model scores every row and recommends an action.",
    "Upload new records without the outcome. The saved model rates every row and recommends an action.",
  ],
  [
    "The ID column identifies each row so scores can be joined back to your systems.",
    "The ID column identifies each row so the list can be joined back to your systems.",
  ],
  // configs/use_cases/hillstrom_email.yaml template: the description of the history_segment column
  ["Band of last year's spend, as published", "Range of last year's spend, as published"],
  // configs/use_cases/hillstrom_email.yaml ui.pages.model: the Model page's title
  ["E-mail Effect and Purchase Propensity", "E-mail effect and purchase likelihood"],
  // engine/uplift/contracts.py NOT_CAUSAL_NOTE, the label every output of a non-random run carries
  [
    "Not causal: the treatment was not randomly assigned, so these numbers describe who was contacted, not what contacting them changed.",
    "Not yet proven: the customers contacted were not chosen at random, so these numbers describe who was contacted, not what contacting them changed.",
  ],
  // engine evidence labels (Plan M §4.1: Causal / Estimated / Not proven read Proven / Estimated / Not yet proven)
  [/\bCausal: /g, "Proven: "],
  [/^Causal$/, "Proven"],
  [/^Not proven$/, "Not yet proven"],
  // every measurement and summary sentence (engine/measurement, engine/decide, engine/pilot): the customers kept
  // out of a campaign are its comparison group
  [/\bcontrol group(s?)\b/g, "comparison group$1"],
  [/\bControl group(s?)\b/g, "Comparison group$1"],
  // the campaign summary's next steps (engine/decide/summary.py)
  [/\buse case(s?)\b/g, "goal$1"],
  [/\bUse case(s?)\b/g, "Goal$1"],
  [/\btrain the model on more customers\b/g, "build the model from more customers"],
  // engine/uplift (ranking_choice.json, the profit curve, a model of several offers): the effect of contacting them
  [/ \((?:ensemble|x-learner|s-learner|t-learner|r-learner|dr-learner|lightgbm|xgboost|catboost|autogluon)\b[^()]*(?:\([^()]*\)[^()]*)*\)/gi, ""],
  [/\bby the approved propensity model's score\b/g, "by the approved model's likelihood"],
  [/\bthe AUUC difference is\b/g, "the difference in gain over a random pick is"],
  [/\bthe uplift model\b/g, "the effect model"],
  [/\bpropensity model\b/g, "likelihood model"],
  [/\bThe band is\b/g, "The range is"],
  [/\bobserved on the hold-out\b/g, "observed on the test customers"],
  [/\bon this hold-out\b/g, "on the test customers"],
  [/\bthe champion rule\b/g, "the rule that picks the model in use"],
  [/\bscore with it\b/g, "make a contact list with it"],
  [/\buplift\b/g, "effect"],
  [/\bUplift\b/g, "Effect"],
  // engine/aws/run_cost.py: the basis of the monthly spend and the estimate beside the start button
  [/\beach finished run recorded\b/g, "each finished model or list recorded"],
  [/\ba run with no recorded figure\b/g, "one with no recorded figure"],
  [/\bThe cost of this run\b/g, "The cost"],
  // engine/stages/prepare.py: a column the person left out in Advanced settings
  ["Excluded in the run settings.", "You left it out in Advanced settings."],
  // engine/run_checks.py: "Things we checked" on the Model page
  [/\bBefore training\b/g, "Before the model was built"],
  [/\bbefore training\b/g, "before the model was built"],
  [/\bTraining went ahead\b/g, "The model was still built"],
  [/^Not run: /, "Not checked: "],
  [/\bthis run's settings\b/g, "the settings"],
  [/^Every score on this page\b/, "Every figure on this page"],
  [/ \(baseline \([^()]*\)\)/g, ""],
  ["A scoring run trains nothing: the checks are on its model's training run.", "A contact list builds no model: the checks are on the model it used."],
  // engine/stages/validate.py: the class-imbalance warning
  [/\bTraining continues\./g, "The model is still built."],
  [/\bTraining continues either way\./g, "The model is still built either way."],
]);

/** An engine or configuration sentence in the page's words (`ENGINE_TEXT`); anything else comes back unchanged. */
export function plainText(text) {
  if (typeof text !== "string" || !text) return text;
  let out = text;
  for (const [from, to] of ENGINE_TEXT) {
    if (typeof from === "string") {
      if (out === from) return to;
    } else {
      out = out.replace(from, to);
    }
  }
  return out;
}

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
    "recorded baseline (`tests/integration/production/ui/glossary/baseline.json`), which M172 emptied (Plan M §4.1):",
    "the baseline is now an empty object, so any of these words outside technical details fails the check.",
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
    "## Names the person supplied",
    "",
    "A file name (`training.csv`), a campaign label someone typed or a column of their data is not our wording,",
    `so the glossary cannot rename it. The page draws each one inside an element with the \`${USER_TEXT_ATTRIBUTE}\` attribute`,
    "(`userText(value)` in `ui/labels.js`), and the visible-text check leaves that element out. Only the person's",
    "own string goes inside the mark, never a sentence of ours around it.",
    "",
    "## Engine sentences in the page's words",
    "",
    "Some text a page shows is written by the engine or the configuration (a mode label, a check's message, a",
    "server sentence) and is pinned there by tests that read its meaning. The page rewords it with `plainText()`",
    `(\`ENGINE_TEXT\` in \`ui/labels.js\`, ${ENGINE_TEXT.length} rules); the API answer itself is unchanged.`,
    "",
    "## Goal names (Plan M §4.2)",
    "",
    "Display names only: ids, configuration keys and API answers are unchanged. Order Fulfillment and Fault",
    'Prediction keep their names and sit under "More goals" on Home, because they contact no customer.',
    "",
    "| Goal id | Shown as |",
    "|---|---|",
    ...Object.entries(GOAL_NAMES).map(([id, name]) => `| \`${id}\` | ${name} |`),
    "",
  );
  return lines.join("\n");
}
