# Causal ML, Uplift, Experimentation and Policy Learning: Methods a Marketing Decisioning Product Could Adopt

**Method note (read first).** WebFetch was blocked in this environment (DNS failure on arxiv.org, statsig.com, etsy.com, alphaxiv and others; the egress proxy returned 403 for statsig.com). Every finding below therefore comes from WebSearch result summaries, not from reading full pages. Quoted numbers are as reported in those summaries. Items flagged "vendor" are self-reported marketing or docs claims. Several 2026 arXiv IDs appeared in results (for example 2603.20775, 2608.00915, 2607.27376, 2609.22409); I could not open them, so treat them as unverified preprints. No source was found for some adopter claims (for example Booking's CUPED numbers); those are listed under Gaps.

## 1. Variance reduction: CUPED, CUPAC, ML-based regression adjustment, stratification

### Takeaway
CUPED (pre-period covariate regression) is the mature default; the variance reduction depends on the correlation between pre- and post-period outcome. Reported results range from about 7% (Etsy CUPED) to about 50% (Bing, a high-traffic metric). ML-based adjustment (CUPAC / MLRATE) adds a further 10-20 points of variance reduction in published industry cases. For low-frequency marketing outcomes (sparse purchase flags), the pre-period correlation is likely low, so gains are probably modest; this is an inference to verify on your own data, and the covariate-rich ML variants are the better bet.

### Cited Findings
- CUPED originates from Deng, Xu, Kohavi and Walker (Microsoft, WSDM 2013, pp. 123-132). On Bing's experimentation system it cut variance by roughly half, meaning the same power from half the users or half the run time. That result is specific to Bing and is not a general guarantee. — [exp-platform.com/cuped](https://exp-platform.com/cuped/) (found via search; summary only)
- Statsig says it uses a form of CUPED based on the 2013 Microsoft paper. — [Statsig variance reduction docs](https://docs.statsig.com/experiments/statistical-methods/variance-reduction)
- A rule of thumb from a secondary blog: the same metric measured on the same user one or two weeks before the test typically gives rho = 0.6-0.8, i.e. 35-65% variance reduction (variance reduction = rho squared). This is a heuristic from a non-primary source, and it is plausible for engagement metrics but not for sparse purchases. — [MetricGate CUPED blog](https://metricgate.com/blogs/cuped-variance-reduction-ab-testing/)
- Another source says CUPED can cut the variance of the treatment-effect estimator by 30-70%. A vendor help page (Intempt) cites Netflix reporting roughly 40% for some key engagement metrics in 2016, and a Microsoft team finding CUPED was akin to adding about 20% more traffic for most metrics. These are second-hand. — [Eppo CUPED search result set](https://docs.geteppo.com/statistics/cuped/); [Intempt help](https://help.intempt.com/en/articles/10436187-cuped)
- CUPAC (Control Using Predictions As Covariates) was introduced by DoorDash in 2020. It replaces the fixed pre-period covariate with an ML prediction (boosted trees) of the outcome, with variance reduction equal to Cor(Y, Y_hat) squared. DoorDash reported it let them shorten switchback tests by more than 25% at the same power. — [DoorDash CUPAC post via search](https://careersatdoordash.com/?p=1650); [Open Data Science summary](https://opendatascience.com/tag/control-using-predictions-as-covariates/)
- Etsy reported CUPED gave 7% variance reduction on its metric, while predicted control variates (CUPAC-style) gave an average of 27%. — [Etsy Code as Craft](https://www.etsy.com/de/codeascraft/reducing-experiment-duration-with-predicted-control-variates) (snippet only, could not open)
- A MetricGate page attributes CUPAC to Booking.com, which conflicts with the DoorDash 2020 origin; the DoorDash attribution is better supported. — [MetricGate CUPAC docs](https://metricgate.com/docs/cupac-analysis/)
- MLRATE (Guo et al., Meta, NeurIPS 2021) uses cross-fitted ML predictions as a regression covariate. It proves consistency and asymptotic normality, and the "naive" confidence intervals are asymptotically valid thanks to cross-fitting. If predictions are uncorrelated with outcomes it is asymptotically no worse than difference-in-means. In A/A tests on 48 Facebook outcome metrics it had over 70% lower variance than difference-in-means and about 19% lower than univariate pre-experiment adjustment. — [MLRATE NeurIPS 2021](https://proceedings.neurips.cc/paper/2021/hash/488b084119a1c7a4950f00706ec7ea16-Abstract.html); [arXiv 2106.07263](https://arxiv.org/pdf/2106.07263)
- Caveat from a vendor docs page: with many covariates and small samples the adjustment model can overfit and variance-reduction estimates become unreliable; if covariates explain little outcome variance, the gain is negligible. — [MetricGate regression adjusted experiment](https://metricgate.com/docs/regression-adjusted-experiment)
- Spotify Confidence uses the Negi-Wooldridge (2021) full regression estimator for CUPED (vendor claim). — [Spotify Confidence glossary](https://confidence.spotify.com/glossary/frequentist-ab-testing)
- Optimizely notes Netflix and Airbnb have implemented similar approaches but gives no figures. — [Optimizely CUPED blog](https://www.optimizely.com/insights/blog/cuped-in-ab-testing-and-experimentation/)
- A practitioner feature request on Optimizely's forum is titled "Variance Reduction for Experiments in Low-Frequency E-Commerce Context", indicating low-frequency contexts are a known pain point. — [Optimizely forum](https://feedback.optimizely.com/forums/964827-analytics/suggestions/51313969-variance-reduction-for-experiments-in-low-frequenc)
- Netflix's regression-adjusted anytime-valid approach (Lindon et al.) uses pre-treatment outcomes for adjustment on device-level log-play-delay, where pre/post correlation is high. — [arXiv 2210.08589](https://arxiv.org/pdf/2210.08589)
- Open-source: GrowthBook added CUPED to its frequentist engine in v2.0 (2023 blog; may be dated). Eppo documents CUPED / CUPED++ (docs.geteppo.com/statistics/cuped). Statsig, Spotify Confidence, Optimizely all offer CUPED. — [GrowthBook search result](https://hashnode.com/tag/cuped); [Eppo docs](https://docs.geteppo.com/statistics/cuped/)

### Inferences
- For win-back/retention offers with binary outcomes and an outcome frequency of a few percent, the pre-period outcome is a weak predictor on its own. Richer covariates (recency, frequency, monetary history, tenure, channel engagement) in a cross-fitted ML model (MLRATE/CUPAC style) are likely to beat plain CUPED, but the gain is bounded by the predictability of the outcome. Expect single-digit to low tens of percent variance reduction rather than 50%; measure R-squared on historical holdout data before promising anything.
- CUPED/MLRATE is safe to ship because it is guaranteed asymptotically no worse than difference-in-means; this fits the "honesty" requirement and the existing "replay campaigns CUPED" step.
- Stratification (blocked randomisation on predicted risk or on past value) achieves similar gains at the design stage and has no model-misspecification risk; I found no source in this search quantifying it, so it is an inference from standard theory.

### Gaps
- No primary source found for Booking, Uber, Airbnb or Spotify CUPED variance-reduction percentages.
- No quantitative evidence found for variance reduction on sparse binary marketing outcomes (email/offer response) specifically.
- Stratification / re-randomisation evidence not covered by the searches.

## 2. Sequential / anytime-valid inference and group sequential designs

### Takeaway
Always-valid p-values (mSPRT) have been in production since about 2015 (Optimizely) and are mature; confidence sequences and e-values are the 2020-2026 generalisation with good theory and a Netflix application, but vendor adoption evidence beyond Optimizely, Statsig and Spotify is thin in what I could retrieve. For a campaign cycle with a fixed audience and a pre-planned readout, a group sequential design or a fixed-horizon test is usually adequate; anytime-valid methods matter if you want to stop early or monitor continuously.

### Cited Findings
- Johari, Pekelis and Walsh's "Always Valid Inference" defines always-valid p-values so users can stop at any data-dependent time while controlling Type I error; the method is described as running in a large-scale commercial A/B platform (Optimizely) over hundreds of thousands of experiments. Ramesh Johari advises Optimizely. — [arXiv 1512.04922](https://arxiv.org/pdf/1512.04922)
- Optimizely Stats Engine combines sequential testing with false discovery rate control across metrics; confidence intervals widen with the number of looks. A third-party page says it derives from mSPRT. Optimizely's claimed error-rate reductions are marketing figures. — [Optimizely Stats Engine deep dive](https://certification.optimizely.com/concepts/experimentation/stats-engine-deep-dive/); [Optimizely whitepaper](https://www.optimizely.com/contentassets/9205a8a811e84957a7cca527d4af20be/whitepaper_optimizely_stats_engine.pdf)
- Statsig adopted mSPRT; in its A/A tests the overall false positive rate was 1.1% (mSPRT) versus 5.0% (fixed horizon) and 17.3% (naive peeking z-test) over 4 weeks. Statsig recommends sequential testing mainly for single-metric ship/regression decisions and fixed-horizon tests for multi-metric scorecards, because of the power cost. — [Statsig sequential testing blog](https://statsig.com/blog/sequential-testing-on-statsig)
- Spotify Confidence offers group sequential tests (GST), which Spotify says it uses internally to maximise power when the maximum sample size is known, and always-valid inference for continuous monitoring. The power claim is self-reported. — [Spotify Confidence experiment analysis blog](https://confidence.spotify.com/blog/experiment-analysis.md); [Spotify glossary](https://confidence.spotify.com/glossary/frequentist-ab-testing)
- Spotify's variance-reduction RFP guide states a sequential interval must be computed on the CUPED-adjusted estimate with the CUPED-adjusted variance; using the raw variance gives an interval that is too wide. — [Spotify RFP variance reduction](https://confidence.spotify.com/rfp/rfp-variance-reduction)
- Lindon et al. (Netflix) give anytime-valid linear models and regression-adjusted confidence sequences with closed-form expressions computable from a standard regression table, valid for randomized experiments with heteroskedasticity-robust asymptotics. — [arXiv 2210.08589](https://arxiv.org/pdf/2210.08589)
- Bibaut, Kallus and Lindon (Netflix) give near-optimal non-parametric sequential tests and confidence sequences with possibly dependent observations, with a real-data A/B test application at Netflix. — [arXiv 2212.14411](https://arxiv.org/pdf/2212.14411)
- Lindon, Sanden and Shirikian (Netflix) apply sequential testing to canary/regression detection in software deployments. — [arXiv 2205.14762](https://arxiv.org/pdf/2205.14762)
- E-values: nonnegative statistics with expectation at most 1 under the null; products form e-processes and Ville's inequality gives anytime-valid rejection when the e-process exceeds 1/alpha. — [theorempath e-values](https://theorempath.com/topics/e-values-and-anytime-valid-inference)
- Open-source: the `confseq` Python package (Howard et al.) implements confidence sequences; the maintainers describe it as early-stage and not stable. — [confseq on PyPI](https://pypi.org/project/confseq/0.0.4/)
- Eppo, GrowthBook: the searches did not retrieve either vendor's sequential-testing documentation, so I cannot confirm what they implement. Eppo's docs site (docs.geteppo.com) and GrowthBook's (docs.growthbook.io) need to be checked directly.

### Inferences
- Anytime-valid methods cost power relative to a fixed-horizon test at the planned end (Statsig's own advice implies this). For pilots of 20k-2M customers with effects of 0.5-3 pp, this cost matters, so prefer a pre-registered fixed horizon or a small number of group-sequential looks (e.g. 2-3) unless early stopping for harm is the goal.
- A "never keep a harmful offer running" guardrail (one-sided sequential test for harm) is the best fit for anytime-valid inference in a marketing pilot, mirroring the Netflix canary-regression use.

### Gaps
- No retrieved documentation for Eppo or GrowthBook sequential methods; no quantified power loss of mSPRT versus fixed horizon at effect sizes of 0.5-3 pp.
- No evidence retrieved on e-value adoption in commercial products (only theory).

## 3. Bayesian hierarchical / empirical-Bayes borrowing across campaigns

### Takeaway
Empirical-Bayes shrinkage across many experiments is established for correcting winner's curse; the 2025-2026 research front is local/hierarchical pooling that borrows only from comparable experiments. Fit for a multi-client, multi-campaign product is high in principle, but exchangeability must be checked and the evidence I retrieved is mostly preprints.

### Cited Findings
- A 2026 preprint on Bayesian hybrid shrinkage says global EB shrinkage is cheap but can be too aggressive when effects differ across product areas or segments, because it assumes exchangeability and a single prior. Hierarchical models handle heterogeneity and are more robust to misspecification but are costly at thousands of experiments. — [arXiv 2603.12867](https://arxiv.org/pdf/2603.12867)
- A November 2025 preprint proposes local empirical Bayes that borrows only from comparable experiments and time periods, reporting lower variance than global pooling without added bias. — [arXiv 2511.21282](https://arxiv.org/html/2511.21282v1)
- EB is presented as a correction for upward winner's-curse bias when selecting best-looking results across many tests; the sources say industry uses EB for aggregated effects across experiment collections but name no specific company in the summary. — [arXiv 2603.12867](https://arxiv.org/pdf/2603.12867)
- Statsig has a post on "informed Bayesian A/B testing: two approaches" (informative priors from past experiments); contents not retrieved. — [Statsig blog](https://statsig.com/blog/informed-bayesian-ab-testing)
- GrowthBook supports a normal prior on lift (default improper, no effect; user-specifiable priors since v3.0). — [GrowthBook statistics docs](https://docs.growthbook.io/statistics/details.md)
- Bibaut et al. (Netflix) learn the covariance of treatment effects across many weak experiments; they note the estimate can be highly biased when effects are weak. — [arXiv 2402.17637](https://arxiv.org/html/2402.17637v1)
- Meta research on "experiment splitting" improves treatment-effect estimators (contents not retrieved). — [Meta research PDF](https://research.facebook.com/file/526444545320512/Improving-Treatment-Effect-Estimators-Through-Experiment-Splitting.pdf)

### Inferences
- With effects of 0.5-3 pp and test noise of similar scale, raw per-campaign lifts are dominated by noise and winner's curse. Shrinking campaign-level (and segment-level / decile-level) lift estimates toward a pooled prior is likely the highest-value Bayesian technique, and it directly improves the honesty of the "Estimated" evidence label.
- Informative priors should be limited to well-matched past campaigns (same offer type, channel, and client vertical) and should be reported with and without prior to remain auditable.

### Gaps
- No Booking or Optimizely specifics retrieved. No peer-reviewed case of cross-client marketing meta-analysis found. Brand-level priors for uplift models (not just ATE) were not covered.

## 4. Off-policy evaluation and safe/conservative policy improvement

### Takeaway
Doubly robust OPE and Athey-Wager policy learning are mature theory with usable libraries; high-confidence policy improvement gives a probabilistic (1 - delta) "not worse than baseline" guarantee, which requires randomized logging with known propensities (which your randomised holdouts supply).

### Cited Findings
- Athey and Wager, "Policy Learning with Observational Data" (Econometrica 2021; arXiv 1702.02896, first titled "Efficient Policy Learning"): starts from a doubly robust estimate of the effect of treating everyone, learns policies restricted to classes such as shallow decision trees, and proves strong guarantees on asymptotic utilitarian regret. For bounded-VC policy classes, minimax-regret learning reduces to evaluating improvement over a randomized baseline. — [Econometric Society page](https://www.econometricsociety.org/publications/econometrica/2021/01/01/policy-learning-observational-data); [arXiv 1702.02896](https://arxiv.org/pdf/1702.02896v5)
- Thomas, Theocharous and Ghavamzadeh, "High Confidence Policy Improvement" (ICML 2015): the user picks a performance lower bound and confidence level; the algorithm keeps the probability of returning a policy below the bound at or under that level. It uses high-confidence off-policy evaluation plus a safe search. The guarantee is probabilistic: Pr(V_new >= V_baseline) >= 1 - delta. — [mlanthology](https://mlanthology.org/icml/2015/thomas2015icml-high); [paper PDF](https://people.cs.umass.edu/%7Epthomas/papers/Thomas2015b.pdf); [Stanford CS234 slides](https://web.stanford.edu/class/archive/cs/cs234/cs234.1224/CS234Win2019/slides/lecture15.pdf)
- Open Bandit Pipeline: open-source Python library for bandit algorithms and OPE, ships with a ZOZOTOWN logged dataset. — [OBP docs](https://zr-obp.readthedocs.io/en/latest/)
- SCOPE-RL: Python library for offline RL, OPE and off-policy selection, with doubly robust variants (SAM-DR, DRL) and "High Confidence OPE" (bootstrap, Hoeffding, empirical Bernstein, Student t); the high-confidence tools are bounds on evaluation, not a dedicated policy-improvement procedure. — [SCOPE-RL arXiv 2311.18206](https://arxiv.org/pdf/2311.18206); [SCOPE-RL docs](https://scope-rl.readthedocs.io/en/latest/documentation/distinctive_features.html)
- EconML (Microsoft) is a CATE/DML library; the search did not confirm its policy-learning module (DRPolicyForest), maintenance status, or release dates. — [best-of-web EconML listing](https://best-of-web.builder.io/library/py-why/EconML)
- CausalML (Uber): tree-based uplift, S/T/X meta-learners, IV. DoWhy: identification and refutation, not policy learning. scikit-uplift: only an old v0.2.0 docs page was retrieved, so current status is unknown. — [DoWhy paper](https://arxiv.org/pdf/2011.04216); [scikit-uplift docs](https://scikit-uplift.readthedocs.io/en/v0.2.0/index.html)

### Inferences
- A "conservative deployment" layer is feasible: treat the new policy only if a high-confidence lower bound on its incremental value (from IPW/DR on randomized holdout data) exceeds the baseline's; otherwise fall back to the baseline (random or risk ranking). Needs enough randomised data, which at 0.5-3 pp effects and 20k customers will often mean "not provable", which should be shown as such rather than hidden.
- Doubly robust OPE is the natural upgrade to IPW/replay; self-normalised IPW reduces variance when propensities are small. These claims come from standard theory, not from sources retrieved here.

### Gaps
- No empirical evidence retrieved on how often safe-improvement methods certify improvement with 20k-2M samples at 1 pp effects. Maintenance/release status of every library was not verified. SNIPS and DR specifics were not directly sourced.

## 5. Uplift advances: budget/cost-aware, multi-treatment, continuous treatment, ROI, calibration, benchmarks, when uplift fails

### Takeaway
The literature is moving from "estimate uplift, then allocate" to ranking by incremental profit or ROI directly, and to predict-then-optimise for dosage. Benchmarks warn that uplift model rankings are unstable across datasets and metric choices, consistent with your Hillstrom result. Robust baselines plus an honest "no better than risk ranking" outcome are credible.

### Cited Findings
- Vanderschueren, Verbeke, Moraes and Proenca (KU Leuven / Booking.com, 2024), "Metalearners for Ranking Treatment Effects": argues standard uplift models are misaligned with budget-constrained profit maximisation; trains a learning-to-rank model that orders instances by incremental profit and shows theoretically it can maximise the area under the incremental-profit curve. Numerical results were not retrieved. — [arXiv 2405.02183](https://arxiv.org/pdf/2405.02183)
- Rank-Learner (2026 preprint) cites this as one of few attempts at direct ranking of treatment effects. — [arXiv 2602.03517](https://arxiv.org/pdf/2602.03517)
- Goldenberg et al. (Booking.com, RecSys 2020) formulate promotion assignment as a knapsack maximising incremental outcome under ROI constraints, with retrospective estimation and online calibration; reports gains in live tests. — [arXiv 2008.06293](https://arxiv.org/pdf/2008.06293v1)
- Direct ROI Prediction (DRP) and a robust variant rDRP for cost-aware binary treatment; DRP is noted to struggle under covariate shift and scarce training data. — [catalyzex author page summary](https://www.catalyzex.com/author/Meng%20Ai)
- E3IR (RecSys 2024): end-to-end uplift prediction plus differentiable multiple-choice-knapsack allocation, arguing two-stage pipelines lose information. — [ACM link via unpaywall](https://unpaywall.org/10.1145%2F3640457.3688147)
- A workshop paper finds that methods optimised for unconstrained uplift often have few feasible solutions once ROI >= 0 is imposed. — [CEUR Vol-2959 paper 2](https://ceur-ws.org/Vol-2959/paper2.pdf)
- Multiple-treatment uplift: Gubela, Lessmann and Stoecker benchmark on eight B2C retail datasets (Information Systems Frontiers) report higher return on marketing for choosing the discount amount per customer. — [HU Berlin edoc](https://edoc.hu-berlin.de/18452/29518)
- Continuous dose: De Vos et al. (arXiv 2412.09232) predict-then-optimise with conditional average dose-response and integer linear programming. — [arXiv 2412.09232](https://arxiv.org/abs/2412.09232?context=cs)
- DoorDash treats discount level as continuous, uses Double ML to estimate per-user discount sensitivity, then optimises offers under a budget; a large A/B test reportedly beat uniform discounts (company blog, not peer-reviewed). — [DoorDash blog](https://careersatdoordash.com/blog/doordash-smarter-promotions-with-causal-machine-learning)
- UniMVT (2026 preprint): multi-valued coupon treatment network jointly debiasing CTR and uplift. — [arXiv 2602.12972](https://arxiv.org/html/2602.12972v1)
- Deep uplift benchmark (2024): most deep uplift models were not robust across test distributions; a model strong on Criteo (selection-biased test setting) was weak on Lazada (unbiased). — [arXiv 2406.00335](https://arxiv.org/pdf/2406.00335)
- 2026 preprint on structural biases: real uplift datasets have no counterfactual ground truth so metrics cannot be validated directly; selection bias, spillover and unobserved confounding distort models and metrics; proposes semi-synthetic benchmarks with tunable bias. — [arXiv 2603.20775](https://arxiv.org/pdf/2603.20775)
- "UpliftBench" (2026 preprint) title points to outcome-regime and objective mismatch in uplift evaluation; contents not read. — [arXiv 2608.00915](https://arxiv.org/pdf/2608.00915)
- An empirical comparison on four datasets found uplift random forests near the top on Qini/Gini but with considerable variability across datasets and folds. — [large-scale benchmark PDF](https://lig-aptikal.imag.fr/~amini/Publis/large-scale-benchmark.pdf)
- Criteo uplift dataset: large randomised trial, imbalanced treatment split, low response rate; propensity AUC about 0.509 supports internal validity. — [Criteo-related summary, arXiv 2604.06123](https://arxiv.org/pdf/2604.06123)
- Practitioner failure modes: uplift is a second-order effect and harder to fit; last deciles of a model were unstable because of small volumes; a control group of 2-3% instead of 10% badly hurt results; with small uplift relative to baseline the two-model errors can swamp the signal; Qini computed on training data is optimistic. — [Predictive Analytics World deck](https://www.predictiveanalyticsworld.com/sanfrancisco/2013/pdf/Day1_1140_Iyer_Shapotina.pdf); [MetricGate uplift docs](https://metricgate.com/docs/uplift-modeling/); [Qini-based uplift regression](https://arxiv.org/pdf/1911.12474)
- I found no source on Hillstrom specifically comparing uplift to response ranking (the search for it returned nothing).

### Inferences
- Your Hillstrom result is consistent with the benchmark literature: at small effects and moderate n, uplift ranking has high variance and may not beat risk ranking; expect "uplift beats risk" only when treatment effect heterogeneity is genuinely large relative to noise. A product feature that tests this explicitly (cross-validated Qini of uplift model vs risk model with confidence band, and default to risk or a blend when uncertain) would be honest and differentiating.
- For retention/win-back with cost, ranking by incremental profit (or net-value with shrinkage) rather than raw uplift is supported by the Booking.com line of work; this matches your net-value step.
- Two-model approaches suffer most under small effects; orthogonal/DR learners (EconML DR/DML learners, R-learner) and shrinkage of CATEs toward the ATE reduce variance (theory-based inference; not directly sourced here).

### Gaps
- No retrieved numerical comparisons of Booking's ranking meta-learners; no evidence on calibration of uplift scores specifically; X5 and Lenta benchmarks not covered; Hillstrom-specific results absent; Promotion ROI paper not retrieved.

## 6. Contact fatigue, cross-campaign arbitration, global control groups, long-term effects

### Takeaway
Global holdouts are standard in marketing platforms; the causal treatment of frequency capping and fatigue was not found in the retrieved sources. Surrogate indices (Athey, Chetty, Imbens, Kang) are the best-supported route to long-term value with short pilots, with Netflix evidence of about 95% agreement on ship decisions.

### Cited Findings
- Global holdout definition and formula (exposed conversion rate minus holdout rate, relative to holdout rate); Bird says it can also serve customers wanting less frequent messaging. — [Bird docs](https://docs.bird.com/applications/journeys/journeys/concepts/holdout-groups)
- Klaviyo offers global holdout groups with a recommended 3-month window, and allows transactional flows to override (Oct 2023 post, may be outdated). — [Klaviyo blog](https://klaviyo.com/blog/global-holdout-groups)
- SegmentStream (vendor opinion): most geo holdouts run 2-4 weeks, brand effects build over months, so short tests miss slow effects; holdouts can cause a visible sales drop. — [SegmentStream](https://segmentstream.com/measurement-engine/incrementality)
- Tredence (vendor case study) reports a retailer's short-term metrics missed loyalty and basket-size effects. — [Tredence PDF](https://campaign.tredence.com/hubfs/case_studies/Implementing-Global-Holdout.pdf)
- Surrogate index (Athey, Chetty, Imbens, Kang; NBER 2019, Review of Economic Studies 2025): combine several short-term outcomes into a predicted long-term outcome; valid if the long-term outcome is independent of treatment given the index (Prentice surrogacy); in a job-training application, six quarters of data gave about 35% lower standard errors for the nine-year effect than employment alone; provides ways to test assumptions. — [NBER w26463](https://www.nber.org/papers/26463); [Opportunity Insights PDF](https://opportunityinsights.org/wp-content/uploads/2019/11/surrogates_paper.pdf)
- Netflix: surrogate indices tested on 200 A/B tests; models using 14 days led to the same ship decisions as 63 days about 95% of the time, with 79% precision and 65% recall for launch decisions. — [NSF PAR / Netflix papers](https://par.nsf.gov//servlets/purl/10520733) (from search summary)
- Kallus et al.: regressing long-term on short-term metrics can be biased by confounding between them and direct effects; proposes past experiments as instruments. Tran et al.: surrogate methods fail for long-term treatments with continual exposure; propose doubly robust estimators. — [Kallus abstract PDF](https://economics.yale.edu/sites/default/files/2024-10/Kallus%20abstract%20and%20related%20papers.pdf); [arXiv 2311.08527](https://arxiv.org/pdf/2311.08527v2)
- Spotify: a paper on long-term off-policy evaluation and learning for cases where short-term clicks diverge from long-term engagement. — [arXiv 2402.17637 result set (search summary)](https://arxiv.org/pdf/2402.17637v2)
- PROXIMA (2026 preprint): audit reliability of existing proxy metrics instead of building new surrogates. — [arXiv 2604.14352](https://arxiv.org/pdf/2604.14352)

### Inferences
- Cross-campaign arbitration is a constrained optimisation (budget, per-customer contact caps) over estimated net values; frequency-cap effects are interference between campaigns for the same customer. A global control group (never contacted) plus within-customer randomised contact counts would identify fatigue causally; this is design inference, not sourced.
- For win-back and retention offers, a surrogate index built from early engagement (reactivation, visits, cart events) could shorten learning cycles, but the Prentice assumption is often violated by offers with direct long-term effects (discount-induced stockpiling); labels should mark surrogate-based estimates as "Estimated", never "Causal".

### Gaps
- No peer-reviewed source retrieved on frequency capping as a causal problem, contact fatigue measurement, or arbitration algorithms. Meta and Uber long-term work not found. Holdout-fatigue literature absent.

## 7. Synthetic control, geo experiments, quasi-experiments, DiD advances

### Takeaway
Augmented synthetic control (Meta GeoLift), synthetic DiD and modern staggered-DiD estimators are mature, with open-source tools; they matter for channels where customer-level randomisation is impossible (TV, out-of-home), less for CRM sends that can be randomised.

### Cited Findings
- Meta GeoLift: design tool selecting treatment markets and duration with minimum detectable effect and power; ridge-augmented synthetic control (Ben-Michael, Feller, Rothstein 2021) is the estimator. mlsynth ports it with conformal inference. Recast sells a separately named GeoLift (vendor claims, no independent benchmark). — [mlsynth GeoLift docs](https://mlsynth.readthedocs.io/en/stable/geolift.html); [Recast](https://getrecast.com/why-recasts-synthetic-control-methods-are-better/)
- Synthetic DiD (Arkhangelsky et al., AER 2021) combines DiD and synthetic control strengths, with theory for single and staggered adoption; Stata implementation by Athey, Clarke, Imbens and Pailanir. — [Stanford GSB](https://www.gsb.stanford.edu/index%2Ephp/node/625681); [SDID summary](https://sotaverified.org/papers/synthetic-difference-in-differences)
- Staggered-adoption DiD estimators (Callaway and Sant'Anna, Borusyak et al., de Chaisemartin and d'Haultfoeuille, Sun and Abraham) still rely on parallel trends; Sequential SDiD (Arkhangelsky and Samkov, 2025) relaxes this. Standard TWFE can be severely biased under heterogeneous effects. — [arXiv 2404.00164](https://arxiv.org/html/2404.00164v2); [MetricGate staggered DiD (secondary)](https://metricgate.com/docs/staggered-diff-in-diff)
- Google geo-experiment methodology was not found in the searches.

### Inferences
- For a CRM product, quasi-experimental methods are mainly a fallback for launches that were not randomised or for validating the effect on aggregate sales; tag results from such methods "Estimated" with explicit identifying assumptions.

### Gaps
- No quantified power of geo/synthetic-control tests at 0.5-3% effects; Google methods (matched markets, TBR) not retrieved.

## 8. Adaptive experiments and contextual bandits

### Takeaway
Bandits trade inferential cleanliness for lower regret; with low volume and small effects they are usually not worth it, and adaptively collected data needs special estimators for valid inference.

### Cited Findings
- Adaptive sampling (UCB, Thompson) makes sample means biased and non-normal; doubly-adaptive Thompson sampling uses adaptively weighted doubly robust estimators (Hadad et al. 2019/2021), claims better regret and best-arm identification. R package banditsCI implements inference on adaptively collected data (Zhan et al. 2021; Hadad et al. 2021). — [NeurIPS 2021 DATS paper](https://papers.neurips.cc/paper_files/paper/2021/file/0ec04cb3912c4f08874dd03716f80df1-Paper.pdf); [banditsCI manual](https://archive.linux.duke.edu/cran/web/packages/banditsCI/refman/banditsCI.html)
- Statsig (vendor): "low traffic? Stick with A/B tests; bandits need more volume." — [Statsig perspectives](https://statsig.com/perspectives/bandit-algorithms-vs-ab-testing)
- GrowthBook: contextual bandits are worthwhile only with a real reason to believe the best variation differs across segments, since each segment needs data. — [GrowthBook contextual bandits docs](https://docs.growthbook.io/bandits/contextual)
- Practitioner view: a bandit's winner may reflect early noise at small effect sizes; contextual bandits need serving infrastructure and drift monitoring. Vendors disagree on low traffic (Braze suggests bandits for limited traffic/short windows). — [Atticus Li](https://www.atticusli.com/blog/posts/ab-testing-vs-multivariate-vs-bandit-algorithms/); [Braze](https://braze.com/resources/articles/multi-armed-bandit-vs-ab-testing)

### Inferences
- A low-frequency campaign gives few decision rounds, so a bandit has little time to learn; between-cycle batch learning with a randomised exploration share (for example 10-20% random) and DR evaluation is the pragmatic design, which your learning loop already approximates. This is reasoning, not a sourced finding.

### Gaps
- No quantitative minimum-sample thresholds from peer-reviewed sources; mostly vendor opinion.

## 9. LLM-assisted causal inference

### Takeaway
LLMs can propose plausible causal relations, but studies show dependence on memorised or frequent relations, prompt sensitivity and hallucination. Use LLMs as a hypothesis generator and reviewer (leakage and confounder checklists, explanation), never as the identification step, and mark LLM-suggested structure "Not proven".

### Cited Findings
- Kiciman, Ness, Sharma and Tan (2023): GPT-3.5/4 beat prior methods on pairwise causal discovery (reported 97%, a 13-point gain) but show unpredictable failure modes. — [Microsoft Research page](https://microsoft.com/en-us/research/?p=938646); [DeepAI](https://deepai.org/publication/causal-reasoning-and-large-language-models-opening-a-new-frontier-for-causality)
- Feng et al.: LLMs recognise causal relations frequent in pre-training data but generalise poorly to novel or rare ones; wrong relations undermine confidence in right ones. — [arXiv 2407.19638](https://arxiv.org/pdf/2407.19638)
- Zecevic et al. (2023) conjecture LLMs are "causal parrots" reciting knowledge. — [as cited in Feng et al.](https://arxiv.org/pdf/2407.19638)
- "LLM Cannot Discover Causality": performance is highly sensitive to prompt variation and mitigations do not resolve limits. — [arXiv 2506.00844](https://arxiv.org/html/2506.00844v1)
- Surveys: causal hallucination in GPT-series (over-attribution), some models refusing; CausalBench found LLMs struggle with large networks and colliders; CausalPitfalls (2025) finds significant limitations on statistical pitfalls such as Simpson's paradox and selection bias. Hallucination in discovery can be reduced by RAG or multi-LLM debate. — [arXiv 2409.09822](https://arxiv.org/pdf/2409.09822); [arXiv 2505.13770](https://arxiv.org/html/2505.13770v2); [contextual causality survey](https://arxiv.org/pdf/2609.22409)

### Inferences
- Lower-risk uses for the product: leakage detection checklists (post-treatment variables in features), generating confounder candidates for human review, and plain-language explanation of results with numbers bound to computed outputs. These are design judgments; no retrieved study validated them for marketing.

### Gaps
- No evidence on confounder selection or leakage identification accuracy specifically; no marketing-domain evaluation found.

## 10. Long-term value and surrogate metrics (see also section 6)

### Takeaway
Covered in Section 6: surrogate indices have solid theory and a Netflix validation; their validity hinges on an untestable assumption that must be audited.

### Cited Findings
- See Section 6 for Athey et al., Netflix 200-test validation, Kallus, Tran, PROXIMA and Spotify long-term OPE sources.

### Inferences
- For marketing pilots, prefer short-horizon incremental profit with explicit decay assumptions and keep a long-running global holdout for validation of any surrogate-based claim.

### Gaps
- Meta, Uber, Airbnb and Booking surrogate practices not retrieved.

## Adoption and maturity summary (compiled from the above)

| Method | Maturity | Industry adopters evidenced in sources | Libraries named in sources | Fit for small, low-frequency pilots |
|---|---|---|---|---|
| CUPED | Mature (2013) | Microsoft Bing, Statsig, Spotify, Eppo, GrowthBook, Optimizely, Netflix (second-hand) | Platform-built | Medium: depends on pre/post correlation, likely low for sparse purchases (inference) |
| CUPAC / MLRATE | Established (2020-2021) | DoorDash, Etsy, Meta | Self-implemented; cross-fit + regression | High: no downside versus difference-in-means asymptotically |
| mSPRT / always-valid | Mature (2015) | Optimizely, Statsig | confseq (early-stage) | Low to medium: power cost; best for harm guardrails |
| Group sequential | Mature | Spotify Confidence | Standard stats packages | Medium: use 2-3 looks |
| Confidence sequences / e-values | Emerging in products | Netflix (papers) | confseq | Medium; theory strong, product evidence thin |
| EB / hierarchical shrinkage | Established; local EB emerging (2025-26 preprints) | Not named in retrieved sources | Self-implemented | High |
| DR OPE, policy trees | Mature theory | Not named in retrieved sources | OBP, SCOPE-RL, EconML (unverified) | High for evaluation; policy learning needs data |
| High-confidence policy improvement | Research (2015) | None found | SCOPE-RL (bounds only) | Medium: often cannot certify at small effects (inference) |
| Incremental-profit ranking / knapsack uplift | Industry-validated | Booking.com, DoorDash | Self-implemented | High for net-value logic |
| Surrogate index | Mature theory, Netflix validated | Netflix | Self-implemented | Medium: assumption risk |
| Synthetic control / SDID / GeoLift | Mature | Meta | mlsynth | Low for CRM; useful for non-randomisable channels |
| Bandits with adaptive inference | Research/early product | Statsig and GrowthBook offer bandits (vendor) | banditsCI (R) | Low at low volume |
| LLM-assisted causal reasoning | Experimental | None validated | n/a | Assistive only |
