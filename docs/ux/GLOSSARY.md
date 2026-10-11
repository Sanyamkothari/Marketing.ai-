# Glossary: the words a marketer sees

<!-- Generated from ui/labels.js (Plan M, M170). Do not edit by hand. To regenerate:
     node -e "import('./ui/labels.js').then((m) => process.stdout.write(m.glossaryMarkdown()))" > docs/ux/GLOSSARY.md -->

One word for each thing. The right-hand column lists the words that are never shown outside a
"Technical details" section; a test renders the main pages and fails on any of them beyond each page's
recorded baseline (`tests/integration/production/ui/glossary/baseline.json`), which M172 empties (Plan M §4.1).

| Concept | Shown to a marketer | Never shown outside Technical details | Why |
|---|---|---|---|
| use case | **Goal** | `use case` | A marketer plans a goal (win back lapsed customers); 'use case' is how the engine names its configuration (Plan M §4.1). |
| a run (train or score) | **Refresh the list** | `run`, `train`, `training`, `scoring run` | Training happens on a goal's first campaign and scoring again is 'Refresh the list'. The person never chooses between running, training and scoring (Plan M §1 M3). 'Trained' is not listed: the glossary's own phrase for drift says 'since the model was trained'. |
| treat list, contact list, scored list | **Contact list** | `treat list`, `scored list`, `treat = 1`, `holdout = 1`, `explore = 1` | One file, one name. The file a sending tool needs is the contact list; the flag columns in it are engine columns (Plan M §3 and M171). |
| holdout, control group | **Comparison group** | `holdout`, `hold-out`, `control group`, `explore slice`, `explore share` | The customers who are deliberately not contacted so the result can be measured: a marketer calls them a comparison group (not contacted). |
| uplift, campaign-effect model | **the effect of contacting them** | `uplift`, `x-learner`, `s-learner`, `t-learner`, `s/t-learner`, `r-learner`, `dr-learner`, `meta-learner`, `auuc`, `qini` | The question is 'does contacting them change what they do?'. The estimator names and the curves that rank them are analyst words. |
| propensity, score, probability | **Likelihood** | `propensity`, `propensities`, `score` | How likely a customer is to act. The plan forbids 'score' as a noun, and a text check cannot tell a noun from a verb, so every whole-word 'score' is flagged: the verb ('Score new data') belongs to the hidden run steps and goes with them. |
| band High / Medium / Low | **Priority** | `band` | Customers are grouped into High, Medium and Low priority; 'band' is the engine's name for the group. |
| champion | **Current model** | `champion` | The model in use is the current model; 'champion' comes from the model registry's promotion rule. |
| challenger | **New candidate** | `challenger` | A model waiting to replace the current one is a new candidate; 'challenger' is a registry word. |
| Causal | **Proven** | `causal`, `evidence label` | The evidence label in words: Proven, Estimated, Not yet proven. 'Causal' is a statistics word. |
| Estimated | **Estimated** | (none) | The middle evidence label; already a plain word, kept so the three labels are listed together. |
| Not proven | **Not yet proven** | (none) | The third evidence label: nothing was shown, which is not the same as 'it does not work'. |
| SHAP, feature importance | **What drives it** | `shap`, `feature importance`, `feature`, `importance` | The reasons behind a ranking, in business words. SHAP, 'feature' and 'importance' are modelling words; a column is 'a detail about the customer'. |
| drift, PSI | **Customers have changed since the model was trained** | `drift`, `psi` | What drift means to a marketer: the customers look different from the ones the model learned on. |
| arbitration | **One message per customer** | `arbitration` | When several campaigns want the same customer only one message goes; 'arbitration' is the engine's rule. |
| recipe | **The fixes you approved** | `recipe` | The data fixes the person approved in Guided setup, saved so next month's file is prepared the same way. 'Recipe' is the engine's name. |
| model names (the algorithm that won) | *(hidden)* | `lightgbm`, `xgboost`, `catboost`, `ensemble`, `random forest`, `randomforest`, `logistic regression`, `logisticregression`, `gradient boosting`, `neural net`, `neural network`, `neuralnet`, `autogluon` | Which algorithm won is hidden from a marketer. An analyst sees it under technical details. |

## How a term is matched

- Case-insensitive, whole words: `run` does not match `running`.
- An optional plural is allowed after any term (`s`, or `es` after a term ending in s, x, z, ch or sh);
  irregular plurals are listed separately.
- A space in a term matches any white space, and `=` matches with or without spaces around it.
- Only text a person can read is checked: technical sections (`<details class="tech">`, `[data-technical]`,
  a `<details>` whose summary reads exactly "Technical details"), hidden elements, scripts and styles are
  left out; `aria-label`, `title` and `placeholder` count as text. The summary of an excluded `<details>`
  stays on screen when closed, so it is still checked unless it reads "Technical details" or "Details".
