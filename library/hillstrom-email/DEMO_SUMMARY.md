# Hillstrom e-mail test: what the product found

**Public dataset, retrospective.** 64,000 customers of a retailer, from a public e-mail test in which each was sent, at random, the men's e-mail, the women's e-mail or nothing. Half of them (32,000) trained every model and setting; the other half (32,000) was never seen by a model, and every result below is measured on it. Every range is a 95% range. This is a retrospective reading of a public file, not a client's own campaign, and the product sent nothing.

## The answers in short

| Question | Answer |
|---|---|
| 1. Who to contact, and with which e-mail? | **A list, with an e-mail chosen on every row** |
| 2. Who to leave alone? | **Named, but not established** |
| 3. What is it worth? | **Beats sending nothing** |
| 4. Does it beat the obvious alternative, the men's e-mail to everyone? | **No, it is worse** |
| 5. How do we know? | **Uplift does not beat risk ranking** |

## The five questions, in full

### 1. Who to contact, and with which e-mail?

The list gives an e-mail to 12,862 of 32,000 customers: the men's e-mail to 7,597 and the women's e-mail to 5,265. The engine's own random control group is held back from the rest.

Counted as a policy over all 32,000 evaluation customers, it means the men's e-mail for 47.27%, the women's e-mail for 32.73% and no e-mail for 20.00%.

**Verdict.** A list was made, with an offer on every row. Which e-mail each customer gets comes from the campaign-effect model, which does not beat plain risk ranking on this file (question five).

### 2. Who to leave alone?

2,351 customers are left alone because contacting them looks harmful (the model's "sleeping dogs"), and 787 more because the expected gain is below the cost of the e-mail.

In the replay campaign, 553 customers the scoring run labelled sleeping dogs were e-mailed anyway (the offer choice and the segment labels come from different steps) and 538 like them were held back. E-mailing them changed the conversion rate by +0.72 pts (range -0.28 pts to +1.92 pts).

**Verdict.** The predicted effects do not match what was measured, by decile: the model is not calibrated, so these names are its best guess. The measurement does not confirm the label: for the customers called sleeping dogs the range includes zero, so no harm is shown from e-mailing them.

### 3. What is it worth?

On the evaluation customers, by inverse-probability weighting of the e-mail the file sent at random, the list raises the conversion rate by +0.45 pts over sending no e-mail (range +0.23 pts to +0.67 pts).

Measured as a campaign against the engine's own random control group, with a third of each group kept (the replay), the lift is +0.60 pts (range +0.21 pts to +1.00 pts; p = 0.002), about 26 extra conversions (range 9 to 43).

In rupees (revenue before margin, at the stated exchange rate) the list nets ₹16,31,009 (16.31 lakh) on the evaluation customers after the cost of the e-mails, range ₹7,46,838 (7.47 lakh) to ₹25,15,180 (25.15 lakh). The campaign's Value Proof Pack, built only on the replay's measured outcomes, shows a net value of ₹88,187 to ₹4,18,481 (4.18 lakh); it credits the outcomes of only a third of each group while costing every e-mail, so it understates the list.

**Verdict.** Yes. The list beats sending no e-mail, both off the file and as measured by the replay campaign.

### 4. Does it beat the obvious alternative, the men's e-mail to everyone?

Sending everyone the men's e-mail raises the conversion rate by +0.69 pts over no e-mail. The list against that, on the same customers, is -0.24 pts (range -0.43 pts to -0.04 pts). In rupees, sending everyone the men's e-mail nets ₹23,70,016 (23.70 lakh) (range ₹12,69,896 (12.70 lakh) to ₹34,70,135 (34.70 lakh)). Against sending everyone the women's e-mail the list is +0.14 pts (range -0.08 pts to +0.36 pts).

**Verdict.** No. The list is measurably worse than sending everyone the men's e-mail: on this file the simple rule wins. Against the women's e-mail to everyone: not shown either way.

### 5. How do we know?

The risk model's ROC-AUC is 0.534, against 0.560 for its plain logistic-regression baseline.

Does the campaign-effect (uplift) model beat plain risk ranking? Judged by the area under the uplift curve, in the engine's own check on its hold-out the difference is +0.0002 (range -0.0016 to +0.0019). Repeated on the evaluation customers, where neither model saw a row, the difference is +0.0000 (range -0.0010 to +0.0007) for the men's e-mail and +0.0001 (range -0.0007 to +0.0010) for the women's.

**Verdict.** Uplift does not beat risk ranking: not in the engine's check on its hold-out, and not out of sample, where neither model saw a row, for either e-mail. Whatever the list earns, it does not earn it because uplift modelling added to plain risk ranking. The model is stable across the folds of the data but not calibrated by decile.

## The limits, said plainly

- Public data and a retrospective reading. The file is an old public e-mail test; nothing was sent by the product, and the licence of the file is not stated, so it is for internal validation. A client's own campaign is the next proof.
- The replay keeps the outcomes of 4,263 e-mailed customers and 4,171 held back, a third of each group, and counts the other 17,165 customers the list meant to reach as having no outcome, so the campaign screens, which count the customers meant to be reached, show larger groups than the ones measured. It uses an outcome window of 0 days, because the product refuses to measure a window that has not passed; the file's own outcomes cover two weeks.
- Rupee figures turn the file's dollars into rupees at 83 rupees to the dollar, an assumed rate, count revenue before margin, and take an e-mail to cost 0.05 rupee. Change the rate and every rupee figure moves with it.
- The amount spent is skewed: a few large purchases dominate it, so the ranges on money may be too narrow. The other public datasets considered, Criteo among them, were not run: not reachable from here, or not licensed for this use.

---

*Generated by `python -m scripts.demo_summary` from `journey.results.json`; do not edit by hand. Every number above is a field of that file: `DEMO_SUMMARY.provenance.json` lists each one with the field it was read from, and `python -m scripts.demo_summary --check` reads the file again and fails if a number, a printed text or a verdict no longer follows from it. The words around the numbers contain no digits, and the verdicts are computed.*
