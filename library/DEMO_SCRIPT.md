# The manager demo (Plan J M111, DEC-1321)

**What you are showing.** The product deciding and proving, on real randomised data, in its own screens, with
nothing planted and nothing fabricated: the MineThatData e-mail test (Hillstrom), 64,000 customers who were each
sent, at random, the men's e-mail, the women's e-mail or nothing. The story is five questions: who to contact,
which offer, who to leave alone, what it is worth, and how we know.

**The results are mixed, and the demo says so.** The list beats sending nothing. On conversions it does not beat the
obvious alternative, sending everyone the men's e-mail (in money, it is not shown to differ). Uplift modelling does not beat plain risk ranking. The model is
stable but not calibrated. That is the product doing its job: it proves value where there is some and says plainly
where there is not. Never spin it; the managers will trust the rest only if you do not.

**Status of the rehearsal.** A dry run by an agent (not the rehearsal) is done and recorded in
[`DEMO_REHEARSAL.md`](DEMO_REHEARSAL.md) Part 2; the rehearsal by a person outside the team, using only this page, is
**pending**. Its checklist is in the same file. Until it is ticked, treat
this script as untested by a stranger.

One page of numbers to hand out or keep open:
[`hillstrom-email/DEMO_SUMMARY.md`](hillstrom-email/DEMO_SUMMARY.md), generated from the run's artefact by
`python -m scripts.demo_summary`, every number traced to a field of it. **Quote nothing that is not on that page.**
The other datasets (telecom, banking, insurance, e-commerce win-back) are in
[`DEMO_OTHER_DATASETS.md`](DEMO_OTHER_DATASETS.md); they are not randomised and cannot show value.

---

## Before you start (the day before, about ten minutes of work and three of waiting)

1. **Get the full file.** `python library/hillstrom-email/fetch.py` downloads it and refuses any copy whose SHA-256
   is not the validated one (`--from PATH` checks a copy you already have). The file is never in git.
   **Without it the next step uses the committed sample, a tenth of the file: fine for rehearsing the clicks,
   never for showing numbers.** The command says which it used, and a page generated from the sample says so in its
   first lines.
2. **Seed a clean store.** `python -m scripts.seed_validated --force`. It runs the whole journey through the
   product's own API (about three minutes on four cores) and prints the command that serves the store and the
   address of every screen below. Nothing trains while anybody watches.
3. **Serve it.** `MARKETING_AI_DATA_DIR=<the store it printed> .venv/bin/uvicorn api.main:app --port 8000`, then
   open `http://localhost:8000/ui`.
4. **Check the screens carry the validated numbers.** On the full file, `diff <the store>/../DEMO_SUMMARY.md
   library/hillstrom-email/DEMO_SUMMARY.md` prints nothing. If it prints anything, you are not on the full file or
   the run changed: do not demo until you know why.
5. Open `hillstrom-email/DEMO_SUMMARY.md` in a second window.

## The honest limits (say them first, not last)

<!-- numbers-checked:start -->
- **Public data, retrospective.** The file is an old public e-mail test. The product sent nothing. A client's own
  campaign, with a holdout the engine draws before anything is sent, is the next proof and the one that counts.
- **Half the file trained everything; half was never seen by a model.** Every result is measured on the half no
  model saw: **32,000** customers.
- **The campaign screens are a replay.** The file's own random e-mail rarely matches what our list says, so the
  replay keeps the customers where it does and counts the other **17,165** as having no outcome. The screens that
  count the customers the list meant to reach therefore show bigger groups than the **4,263** e-mailed and
  **4,171** held-back customers actually measured.
- **Rupees are an assumption.** **83** rupees to the dollar, revenue before margin. Change the rate and every rupee
  figure moves.
- **Money is skewed.** A few large orders dominate the amount spent, so the rupee ranges may be too narrow (the
  summary's limits say so; the report's resampled range is the check). Quote the ranges, never a single rupee figure.

## The story, in the order to tell it

**1. Who to contact, and with which e-mail?** The list e-mails **12,862** of **32,000** customers: the men's e-mail
to **7,597** and the women's to **5,265**. As a policy over every evaluation customer that is the men's e-mail for
**47.27%**, the women's for **32.73%** and nothing for **20.00%**. Say: *"An offer is chosen on every row. How well
it is chosen comes in question five."*

**2. Who to leave alone?** **2,351** customers are left alone because every e-mail would make them less likely to buy,
and **787** more because no e-mail earns back its cost. Say what the page says: *"These are the model's best guess,
not a measured fact."* The model is not calibrated, and in the replay the customers the scoring run called sleeping
dogs, e-mailed anyway (**553** of them, with **538** like them held back), changed by **+0.72 pts** with a range of
**-0.28 pts** to **+1.92 pts**: no harm is shown, so the label is not confirmed.

**3. What is it worth?** Against sending nothing the list raises the conversion rate by **+0.45 pts**, range
**+0.23 pts** to **+0.67 pts**, measured on customers no model saw. Measured as a campaign against the engine's own
random control group it is **+0.60 pts**, range **+0.21 pts** to **+1.00 pts**, **p = 0.002**, about **26** extra
conversions (**9** to **43**). Both ranges are wholly above zero: on this file the list beats sending nothing. In
rupees the list nets **₹16,31,009** on the evaluation customers, range **₹7,46,838** to **₹25,15,180**. The Value Proof
Pack shows **₹88,187** to **₹4,18,481**; it is a floor, because it counts the outcomes of only a third of each group
while costing every e-mail.

**4. Does it beat the obvious alternative?** On conversions, no. Sending everyone the men's e-mail raises the
conversion rate by **+0.69 pts**, range **+0.43 pts** to **+0.94 pts**. The list against that, on the same customers,
is **-0.24 pts**, range **-0.43 pts** to **-0.04 pts**: measurably worse. **In money it is not shown to be worse.** The
difference in revenue per customer, the list minus the men's e-mail to everyone, is **-$0.278**, range **-$0.611** to
**$0.055** (resampled **-$0.598** to **$0.056**): the range includes zero. In rupees, the men's e-mail to everyone
nets **₹23,70,016**, range **₹12,69,896** to **₹34,70,135**, against the list's **₹16,31,009**, range **₹7,46,838** to
**₹25,15,180**: the two overlap, so do not say the list "loses" a number of rupees. Against the women's e-mail to
everyone the list is **+0.14 pts**, range **-0.08 pts** to **+0.36 pts**, which cannot be told apart. Say: *"On
conversions the simple rule wins, and the product told us so itself. In money it is not shown to differ."*

**5. How do we know, and how far can we trust it?** The risk model's ROC-AUC is **0.534**, against **0.560** for its
plain logistic-regression baseline: a weak model. The uplift model does not beat plain risk ranking: its difference
from the risk ranking is **+0.0002**, range **-0.0016** to **+0.0019**, in the engine's own check, and out of sample
**+0.0000** (**-0.0010** to **+0.0007**) for the men's e-mail and **+0.0001** (**-0.0007** to **+0.0010**) for the
women's. It is stable across folds and not calibrated by decile. Say: *"Whatever the list earns, it does not earn it
because uplift modelling added to risk ranking. The value that is there is the e-mail itself, and the proof that it is
there is the held-back group."*
<!-- numbers-checked:end -->

**Close on:** a product that could say *no* to itself in front of you, and that shows its working. The next proof is
a client's own campaign: plan the test with them first (the power sheet says what size of effect their holdout can
see), then measure.

## The screens to click (the same story, with the clicks)

Every instruction below was followed literally in the dry run (see `DEMO_REHEARSAL.md`). Numbers are deliberately
not repeated here: read them off the screen and match them to the summary page.

1. **Results** (top bar). Read the first card aloud: **Needs your attention: "The model that picks who to contact does
   not beat ranking by risk."** The product volunteered that. Below it, **Value proven to date** is a floor, the
   lower end of each range, not an estimate; the conversion campaign is listed apart because it measures the same
   customers as the spend one and is not counted twice. *(Question 5.)*
2. **Results → Runs → the "Trained a model" row whose outcome starts "AUUC", Open ›; on the run page, the MODEL box's
   View details ›.** (The other "Trained a model" row is the risk model, "Ranking quality".) The model page: **Model
   beats random targeting: No.** Scroll to **Each offer against no offer**: *"Choosing the offer for each customer is not yet
   shown to do better than the first offer alone."* Say that the row's outcome reads "AUUC 0" because the table
   rounds it; the page shows the range. *(Question 5.)*
3. **Results → Runs → the "Scored new data" row whose page says "Scored with X-learner", Open ›; on the run page, the
   OUTPUT box's View details ›.** (There are two "Scored new data" rows and the table does not say which is which:
   the lower, older one is the campaign-effect model's; the upper one is the risk model scoring the same customers.)
   The Output page, **Who to contact**. Point at the banner **"This list is ranked by the propensity model, not by uplift"** (the product
   reports its own fallback), then scroll to **Which offer each customer gets** (the offers and the net value of each),
   open **Why some customers get no offer** (the two reasons in question 2), and the **Treat list** card with
   **Download treat list (CSV)**. *(Questions 1 and 2.)*
4. **Results → Campaigns → "Hillstrom e-mail (conversion) …".** The **Result** card: *"The campaign added about 26
   conversions"*, with the range and the p-value, against the engine's own random control group. **Plan the test**
   below it is the power sheet: what size of effect this holdout could see. *(Question 3.)*
5. **Results → Value Proof Packs → the conversion pack.** Walk **What the campaign changed**, **Naive credit against
   measured credit** (a tool that credited every response would claim nearly twice the measured effect),
   **Groups where the campaign backfired** (none shown; one group had too few customers to judge) and **Method and limits**. Read the line **Customers left out for
   having no outcome in the file**. *(Question 3.)*
6. **Close with `DEMO_SUMMARY.md`** open: the answers in short, then the limits. *(All five.)*

## What looks contradictory on the screens, and the true answer

These are real, known and recorded (`docs/DECISIONS.md` DEC-1320 (o), DEC-1321). Do not wait to be asked.

- **"Contact 7,596" on the Output page, "To treat 12,862" on the Treat list card.** Two lists. The contact list was
  re-ranked by the risk model when uplift did not beat it; the offer choice, and so the treat list, still comes from
  the campaign-effect model. The fallback does not reach a model of several offers. *The summary page and the
  campaign are about the treat list.*
- **"Customers measured 25,599" in the campaigns table, "12,862 contacted and 12,737 held back are compared" on the
  campaign page.** Those are the customers the list meant to reach. The measurement is on the ones with an outcome;
  the Pack's **Method and limits** says how many were left out, and the summary says how many were kept.
- **The Pack's Method text says the held-back group was chosen "before the campaign went out" and that outcomes are
  counted for everyone the campaign was meant to reach.** Nothing went out: it is a replay of a 2008 log, and the
  Pack's campaign name says so. Say it aloud.
- **The "Persuadables / Sleeping dogs" counts on the Output page differ from the two reasons for no offer.** The first
  cut every customer by the first e-mail's predicted effect; the second is per offer, among customers not held back.
- **The rupee figure on the Output page ("Net value of the offers given") is larger than the measured rupee range.**
  The first is the model's prediction, from a model that is not calibrated; the second is measured. Quote only the
  measured one.
- **The screens round.** "AUUC 0" and "+1 pts" are rounded; the model page and the summary have the ranges.

## Questions you will get

| Question | Answer |
|---|---|
| "So does it work?" | Against sending nothing, yes, on this file: both ranges are wholly above zero. Against the simplest alternative, on conversions no; in money, not shown to differ. And the part that is new, uplift modelling, did not add anything over plain risk ranking here. That is a result, not a failure of the demo. |
| "Why is the list worse than just e-mailing the men's version?" | On conversions it is measurably worse; in money the difference is not shown (its range includes zero). Because the model that picks who gets which e-mail is not good enough on this file to beat a one-line rule, and the product measured that rather than hiding it. A different dataset or more data could change it; this one does not. |
| "Is this ROI?" | No. It is revenue before margin, at an assumed exchange rate, on a retrospective replay, on public data. It shows the method and that the method can say no. ROI needs a client's own campaign and their own value per customer. |
| "Did you tune on the test half?" | No. The data was split once, by a fixed seed; every model, check and setting uses the first half; the second half is only measured on, and both test plans were registered before any outcome of it was read. |
| "Why does the replay keep only a third?" | The file's random e-mail matches our list for about a third of each group; the match is random, so the comparison stays randomised. The others are counted as no outcome, never as non-converters. The Pack understates the list for that reason. |
| "Can we use this data commercially?" | No statement of terms comes with it. It is for internal validation; ask the author before redistributing it or anything derived from it. |
| "What about the other datasets?" | Only this one is randomised, so only this one can show value. The others show the engine runs on other industries ([`DEMO_OTHER_DATASETS.md`](DEMO_OTHER_DATASETS.md)). **Criteo Uplift: do not demo** (CC BY-NC-SA, non-commercial). |
| "Why does my screen show different numbers?" | You are on the sample, or a different run. Re-seed on the full file and diff against the summary page. |

## If something goes wrong

- **The seed stops with "already holds a store".** Add `--force`; it replaces only the store under the directory you gave it.
- **The seed says SAMPLE.** The full file is missing. Run `python library/hillstrom-email/fetch.py`; if the
  download is blocked, `--from PATH` with a copy you have; then seed again with `--force`.
- **A page is empty or says "Loading…".** Reload once; the server is local and every page is read from the store.
- **The numbers on a screen disagree with the summary.** Stop quoting the screen. Quote only the summary, and say
  you will check the screen.

## Other datasets, and the one you must not show

The telecom, banking, insurance and win-back walkthroughs moved to [`DEMO_OTHER_DATASETS.md`](DEMO_OTHER_DATASETS.md)
unchanged. **Criteo Uplift: do not demo.** It is CC BY-NC-SA 4.0, non-commercial, and may not appear in a sales
deck, a customer pilot or a shipped product. See [`criteo-uplift/README.md`](criteo-uplift/README.md).
