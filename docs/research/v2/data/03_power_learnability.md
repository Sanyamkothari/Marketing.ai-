# Power and learnability: is there enough data to learn and prove?

21 Oct 2026. Question from the founder: when Autonomy connects to real customer data, is there enough
of it to (a) prove a play makes money against the never-contacted holdout, (b) tell two variants
apart, and (c) learn who to target? Scope changed mid-analysis: the main client is now a large
company (food delivery, SaaS, bank, telco) with 1M, 10M or 50M users and high-frequency
interactions. The small-shop tables (5k to 500k profiles) are kept in section 7.

Everything below is computed, not eyeballed. The code is in
`/tmp/claude-0/-home-user-Marketing-ai-/c7787998-074f-51f3-9065-71bef613b34b/scratchpad/power/`
(commands in the appendix). Lenta and Starbucks promo were not touched.

## The answers in one screen

1. **Enterprise, one play vs holdout: provable in 1 to 5 weeks at 1M users, and the wait is set by
   the outcome window, not by volume.** At 10M and 50M every play in the list is provable in the first 1 to 5.5 weeks. The slow ones are rare, long-window outcomes (bank cross-sell 30 days, SaaS
   trial-to-paid 30 days): 9 to 16 weeks at 1M, 4.4 to 5.5 weeks at 10M and above (Table E1).
2. **Variant differences are 3 to 10 times slower.** A variant worth 25% of the play's lift takes 2.4 to 99 weeks at 1M (never for the bank programme play) and 1 to 14 weeks at 10M. A variant worth 10% of the lift needs 10M to 50M
   users for most plays (Table E2). A discount-vs-no-discount test on conversion is feasible; on
   profit it is about 2x slower (Hillstrom: spend needs 1.9x the sample of conversion).
3. **Five-segment heterogeneity** (omnibus test, effects 0.4x to 1.6x of the mean) needs about the
   same sample as a 25% variant difference: 2.5 weeks to more than 2 years at 1M (never for the two programme plays), 1 to 14 weeks at 10M (Table E3).
   If the segments differ by only half as much, multiply the sample by about 4.
4. **Per-person heterogeneity cannot be learned from a person's own interactions.** In a
   hierarchical normal-normal model a per-person policy captures only 25% of the available
   per-person gain after a year of 5 pushes a week (260 interactions: 6%). Reaching 25% takes 1,000 to 4,600 interactions per person even when person effects vary as much as the average effect, and up to 235,000 when they vary less or the segment is not near break-even (Table P1). A food-delivery user gets about 260 pushes a
   year. Per-person value has to come from features shared across people, not from each person's
   own history.
5. **Targeting on features, learning curve (Criteo up to 11M rows, MegaFon, Hillstrom).** At a cost
   equal to the average lift (break-even blanket), a numpy T-learner beats the best blanket policy
   from about 30k training rows on MegaFon and from about 10k on Criteo. On Criteo the gain then
   plateaus: +0.54 net conversions per 1,000 customers at 10k rows, +0.78 at 100k, +0.86 at 3M,
   +0.93 at 11M (Table L1). Targeting keeps pulling ahead with scale, but slowly: 100x more data
   (100k to 11M) adds 19%. On Hillstrom it never beats blanket (up to 21k rows). When contact is
   cheap (cost half the average lift) the same T-learner loses to send-to-all on MegaFon at every
   size up to 300k.
6. **Pooling (section 6, shops).** With a shared prior from 100 comparable shops, a 20k-profile shop's abandoned-cart effect is usable in 1 week instead of 5.6 years, and a 100k shop needs only 20 other shops (1 week instead of 58). This holds only if shops differ by no more than about 30% of the mean effect. At 60% spread the gain is small (20k shop: 290 weeks alone, 192 with 100 other shops) and at 100% almost none.
7. **Small shops (<100k profiles) cannot prove most plays inside a year.** At 20k profiles an
   abandoned-cart flow needs 5.6 years, at 100k 59 weeks, at 500k 13 weeks (Table S1).

## 1. Base rates and lift sizes

### 1.1 Measured on the randomised datasets on disk

All numbers computed from the files in `/home/user/datasets` (full file for Hillstrom, MegaFon,
X5; a random 30% sample, 4.19M rows, for the headline Criteo rates; the learning curve uses all
13.98M).

| Dataset | Outcome | Control | Treated | Lift | Relative | Total n for 80% power at a 10% holdout |
|---|---|---:|---:|---:|---:|---:|
| Hillstrom, men's e-mail vs none (21,307 vs 21,306) | purchase in 2 weeks | 0.57% | 1.25% | +0.68 pp | +119% | 12,000 |
| Hillstrom, women's e-mail vs none | purchase | 0.57% | 0.88% | +0.31 pp | +54% | 54,000 |
| Hillstrom, men's e-mail | visit | 10.6% | 18.3% | +7.7 pp | +72% | 650 (50/50) |
| Hillstrom, men's e-mail | spend, USD | 0.653 | 1.423 | +0.770 | +118% | 1.9x the conversion sample |
| X5 RetailHero SMS (100k vs 100k) | bought in the following period | 60.3% | 63.7% | +3.3 pp | +5.5% | 18,900 |
| Criteo (85/15) | conversion | 0.190% | 0.309% | +0.118 pp | +62% | 126,000 |
| Criteo | visit | 3.83% | 4.84% | +1.01 pp | +26% | 32,000 |
| MegaFon (50/50) | conversion | 17.94% | 22.89% | +4.95 pp | +28% | 5,300 |

How to read them. These are one-shot campaigns on engaged or opted-in customers, several of them
with unusually large effects (Hillstrom is a 2008 catalogue-buyer list; MegaFon is an offer with a
big built-in effect). They set the upper end of what modern flows will show, not the typical value.
Hillstrom's formula check: the formula in section 2 predicts 6,100 customers for men's e-mail at
50/50, and the observed z at 42,600 customers is 7.4, consistent.

### 1.2 Published benchmarks (web, vendor data, not holdout-based)

| Quantity | Value | Source and caveat |
|---|---|---|
| Klaviyo abandoned-cart flow, attributed placed-order rate | average 3.33% per recipient, top 10% 7.69%; revenue per recipient $3.65 | [Klaviyo 2024 benchmark report via Attribuly](https://attribuly.com/blogs/abandoned-cart-recovery-rate-klaviyo/). Attributed to a click or open, so it includes purchases that would have happened anyway |
| Klaviyo flows vs campaigns | flows are 5.3% of sends, about 41% of email revenue; placed-order rate about 13x a campaign's (2.11% vs 0.16% per a secondary write-up); revenue per recipient about 18x | [Klaviyo 2026 benchmarks](https://klaviyo.com/products/email-marketing/benchmarks) (183k customers); the 2.11% and 0.16% come from a [secondary summary](https://www.klaviyo.com/uk/products/email-marketing/benchmarks) |
| Campaign revenue per recipient | $0.11 (2024) | [Klaviyo community summary of the 2024 report](https://community.klaviyo.com/show-tell-27/klaviyo-s-benchmark-report-takeaways-and-use-cases-12028) |
| Browse abandonment | 0.59% conversion (Omnisend 2026), 0.96% (Klaviyo, 9.6x campaigns) | [Omnisend / Klaviyo via search](https://www.omnisend.com/blog/browse-abandonment-email/); definitions differ per vendor |
| Lapsed-purchase (win-back) | 0.52% conversion, about $0.49 per email (Omnisend); one store 0.9% then 1.4% on emails 1 and 2 | [Klaviyo community thread](https://community.klaviyo.com/marketing-30/winback-flow-benchmarks-5148), a single merchant |
| Replenishment | no clean public figure; a post-purchase style flow is quoted at 0.4 to 0.8% | [flow checklist](https://topgrowthmarketing.com/tools/klaviyo-flow-checklist/), directional |
| Cart abandonment and flow reach | about 70% of carts abandoned; native Klaviyo identifies and enters only about 15% of abandoners (vendor estimate) | [Attribuly](https://attribuly.com/blog/klaviyo-abandoned-cart-flow-guide/); sells a fix, treat as an upper bound on the problem |
| Repeat purchase rate | 25% average, 22.5% median, 1.8 orders per customer (1,500+ Shopify stores); vendor figure 28% | [joy.so summary of Little Stream study](https://joy.so/customer-retention-benchmarks/), [Rivo](https://rivo.io/blog/repeat-purchase-rate-complete-guide) |
| List size | no benchmark by revenue band found; one study of 619 Klaviyo brands gives a mean of about 123k contacts; email is 12 to 18% of revenue under $10k a month and 30 to 40% above $200k | [CustomersAI](https://customers.ai/wp-content/uploads/2026/03/CustomersAI-2026-Klaviyo-Email-Marketing-Benchmark-Report.pdf), [EasyApps](https://easyappsecom.com/guides/shopify-email-list-monetization) |
| Incrementality of existing-customer email | one 3-month, 10% holdout case: clicks and site visits up 36%, but orders down 2% and revenue up 4%, neither significant | [Polar Analytics](https://www.polaranalytics.com/case-studies/reuse-klaviyo-holdout-incrementality-test), one brand |
| Discount vs no discount | no clean incremental study found. One A/B: a 10% code raised click-through about 63%; conversion not reported. Free shipping beat 10% off in another | [SmartMarketer](https://smartmarketer.com/3-easy-email-wins/), [Barilliance](https://www.barilliance.com/?p=435) |
| Food delivery frequency | Swiggy 4.07 orders per user per month in FY26 (4.43 in FY25), 23.5M monthly transacting users; Zomato 24.9M monthly transacting users | [Inc42](https://inc42.com/buzz/over-35-of-swiggy-users-try-multiple-services-but-order-frequency-slips/) |
| Push incrementality, food delivery | vendor case: +33% daily retention, +22% purchase intent with local and global controls; no confidence interval | [nGrow FoodRocket](https://ngrow.ai/case-studies/foodrocket), vendor marketing |

The key warning from the benchmarks: **every published conversion rate is attributed, not
incremental.** Attributed rates (3.33% cart, 0.16% campaign) are an upper bound on the lift. The
proof against a holdout will show smaller numbers, and a few brands show none (Polar). That is the
reason the power calculation matters.

### 1.3 Assumed effects used in the tables (the weakest part)

No public source gives holdout-measured lifts for most of these plays, so the lifts below are
assumptions, set below the attributed rates and informed by the measured datasets. The weeks scale
with 1/(lift squared), so halving a lift multiplies the time by about 4. Change the numbers in
`power.py` to re-run.

**Enterprise plays** (a user is a contactable profile on the channel; "wk" = per week):

| Play (channel) | Type | Users entering per wk | Control outcome | Assumed lift | Window | Basis |
|---|---|---|---|---|---|---|
| Food: always-on push programme, 5 sends a week (push) | programme, all users, persistent holdout | 100% | 0.9 orders per user-week (Swiggy 4.07 a month), person CV 0.8 | +2% relative | rolling | assumption; vendor claims are far larger |
| Food: lapsed win-back (push, SMS) | flow | 3% | 8% order in 14 days | +25% rel (+2.0 pp) | 14 d | Hillstrom/MegaFon scale discounted |
| Food: cart-abandon push | flow | 10% | 35% order in 24 h (read at 7 d) | +4% rel (+1.4 pp) | 7 d | assumption |
| Bank: card-spend nudge (push, SMS) | programme | 100% | 3.5 card transactions per user-week, person CV 1.2 | +1.5% rel | rolling | assumption |
| Bank: cross-sell offer (WhatsApp, email) | flow | 2% | 0.6% take-up in 30 d | +25% rel (+0.15 pp) | 30 d | assumption |
| Telco: recharge reminder (SMS) | flow | 25% | 45% recharge in 7 d | +3% rel (+1.35 pp) | 7 d | assumption |
| Telco: data-pack offer (SMS) | flow | 10% | 17.9% convert in 14 d | +4.95 pp (+28%) | 14 d | measured on MegaFon |
| SaaS: onboarding nudge (email, in-app) | flow | 1% (new users) | 25% activate in 14 d | +8% rel (+2 pp) | 14 d | assumption |
| SaaS: trial-to-paid nudge (email) | flow | 2% | 4% upgrade in 30 d | +15% rel (+0.6 pp) | 30 d | assumption |

**Shop plays** (a profile on Klaviyo; used in section 7):

| Play | Users entering per wk | Control outcome | Assumed lift | Window |
|---|---|---|---|---|
| Campaign email, 1 a week to 70% of the list | 70% | 0.5% buy in 7 d | +0.06 pp (+12%); attributed rate 0.16%, about 40% incremental | 7 d |
| Abandoned-cart flow | 0.5% | 8% buy in 7 d | +1.5 pp (+19%); attributed 3.3%, about half incremental | 7 d |
| Browse-abandonment flow | 2% | 1.5% | +0.3 pp (+20%) | 7 d |
| Win-back | 1% | 0.3% buy in 14 d | +0.25 pp; attributed 0.5 to 0.9% | 14 d |
| Replenishment reminder (consumables only) | 0.4% | 6% in 14 d | +0.8 pp (+13%) | 14 d |

## 2. Method

* **Design.** A persistent 10% holdout, 90% treated. The test is two-sided at 5% with 80% power. All
  tests are fixed-horizon (no peeking); an always-valid sequential interval costs somewhat more and
  was not computed.
* **Flow plays.** Users enter a play at a steady weekly rate E. Outcome is binary within a window.
  Required entries `n = (z_a + z_b)^2 [p0(1-p0)/0.10 + p1(1-p1)/0.90] / d^2`, with
  (z_a+z_b)^2 = 7.85. Calendar time is `n / E` plus the outcome window, floored at 1 week (so a
  weekday cycle is always covered). A cell shows "1w" when the answer is 7 days or less.
* **Programme plays** (always-on pushes: all users, same people every week). The outcome is the
  person's count over w weeks, with a gamma-distributed person rate (CV c). The variance of the
  weekly-rate estimate is `lambda/w + c^2 lambda^2`, so it has a floor from between-person
  differences that waiting does not remove. A cell reads "never" when the effect is below that floor
  at this user count. A pre-period covariate (CUPED, with R^2 near the between-person share, about
  0.8 for weekly counts) removes most of the floor: food push 5-segment at 1M goes from "never" to
  8.2 weeks at R^2 0.3 and 2.5 weeks at R^2 0.5.
* **Variants.** Two variants share the treated 90% (45% each); a difference of `d_v` in the outcome
  rate. Shown for `d_v` = 25% and 10% of that play's lift (variants are close; the gap between two
  creatives or send times is smaller than the lift of contacting at all).
* **Five segments.** Five equal segments with lifts 0.4x, 0.7x, 1.0x, 1.3x, 1.6x of the mean (spread
  0.42 of the mean). Omnibus test of "effects are equal" (chi-square, 4 df, 5%, 80% power needs a
  noncentrality of 11.94, computed from the exact Poisson mixture). The "weak" pattern is
  0.7x to 1.3x (a quarter of the squared spread), 4x the sample.
* **Many plays at once.** Each cell is one test. Running 5, 20 or 50 plays and correcting for it
  (Bonferroni) raises the sample needed by 1.5x, 1.9x, 2.2x. A single global holdout shared by all
  plays measures the whole programme; per-play effects are only clean if plays are randomised
  independently of each other.

## 3. Enterprise: weeks until each effect is distinguishable (80% power, 5% alpha, 10% holdout)

"1w" means 7 days or less. "never" means the effect is smaller than the person-to-person noise floor
at this size.

### Table E1. Play lift vs holdout

| Play | 1,000,000 | 10,000,000 | 50,000,000 |
|---|---:|---:|---:|
| Food: always-on push programme, orders/user-week | 1w | 1w | 1w |
| Food: lapsed win-back (push+SMS), order in 14d | 2.5w | 2.1w | 2.0w |
| Food: cart-abandon push, order in 24h | 2.0w | 1.1w | 1.0w |
| Bank: card-spend nudge (push/SMS), txns/user-week | 1w | 1w | 1w |
| Bank: cross-sell offer (WhatsApp/email), take-up in 30d | 16w | 5.5w | 4.5w |
| Telco: recharge reminder SMS, recharge in 7d | 1.5w | 1.0w | 1.0w |
| Telco: data-pack offer SMS (MegaFon-measured +4.95pp) | 2.1w | 2.0w | 2.0w |
| SaaS: onboarding nudge (email/in-app), activation in 14d | 6.1w | 2.4w | 2.1w |
| SaaS: trial-to-paid nudge, upgrade in 30d | 9.0w | 4.8w | 4.4w |

### Table E2. Difference between two variants (variant gap = 25% of the play's lift, then 10%)

25% of lift:

| Play | 1,000,000 | 10,000,000 | 50,000,000 |
|---|---:|---:|---:|
| Food: always-on push programme | 14w | 1w | 1w |
| Food: lapsed win-back | 6.3w | 2.4w | 2.1w |
| Food: cart-abandon push | 7.6w | 1.7w | 1.1w |
| Bank: card-spend nudge | never | 1w | 1w |
| Bank: cross-sell offer | 99w | 14w | 6.2w |
| Telco: recharge reminder | 4.0w | 1.3w | 1.1w |
| Telco: data-pack offer | 2.4w | 2.0w | 2.0w |
| SaaS: onboarding nudge | 30w | 4.8w | 2.6w |
| SaaS: trial-to-paid nudge | 39w | 7.7w | 5.0w |

10% of lift:

| Play | 1,000,000 | 10,000,000 | 50,000,000 |
|---|---:|---:|---:|
| Food: always-on push programme | never | 2.2w | 1w |
| Food: lapsed win-back | 28w | 4.6w | 2.5w |
| Food: cart-abandon push | 42w | 5.1w | 1.8w |
| Bank: card-spend nudge | never | never | 1w |
| Bank: cross-sell offer | >2y (more than 104w) | 63w | 16w |
| Telco: recharge reminder | 20w | 2.9w | 1.4w |
| Telco: data-pack offer | 4.5w | 2.3w | 2.1w |
| SaaS: onboarding nudge | >2y | 19w | 5.4w |
| SaaS: trial-to-paid nudge | >2y | 26w | 8.6w |

Example, discount vs no discount: the food win-back with a discount variant that adds 1.0 pp of
conversion (12% of the treated rate, half of the lift) is detectable in about 3.1 weeks at 1M (2.1 at 10M). That is
conversion only. A decision on profit needs the spend (Hillstrom: 1.9x the sample for the same
effect), and a discount that "wins" on conversion can lose on margin, so the profit test is the one
to run.

### Table E3. Five-segment heterogeneity (omnibus test, effects 0.4x to 1.6x of the mean)

Central pattern:

| Play | 1,000,000 | 10,000,000 | 50,000,000 |
|---|---:|---:|---:|
| Food: always-on push programme | never | 1w | 1w |
| Food: lapsed win-back | 6.6w | 2.5w | 2.1w |
| Food: cart-abandon push | 9.6w | 1.9w | 1.2w |
| Bank: card-spend nudge | never | 1w | 1w |
| Bank: cross-sell offer | >2y | 14w | 6.3w |
| Telco: recharge reminder | 5.0w | 1.4w | 1.1w |
| Telco: data-pack offer | 2.5w | 2.0w | 2.0w |
| SaaS: onboarding nudge | 37w | 5.5w | 2.7w |
| SaaS: trial-to-paid nudge | 44w | 8.3w | 5.1w |

Weak pattern (0.7x to 1.3x): at 1M it is "never" (the two programme plays), more than 2 years (cross-sell, SaaS onboarding, trial-to-paid) or 3.8 to 35 weeks for the other four; at 10M it is 1.5 to 44 weeks, except the bank card nudge, which is "never"; at 50M, 1 to 12 weeks.

Weeks until the smallest segment (0.4x of the mean lift, a fifth of the users) is itself provably
above zero:

| Play | 1,000,000 | 10,000,000 | 50,000,000 |
|---|---:|---:|---:|
| Food: always-on push programme | never | 1.3w | 1w |
| Food: lapsed win-back | 19w | 3.7w | 2.3w |
| Food: cart-abandon push | 33w | 4.2w | 1.6w |
| Bank: card-spend nudge | never | never | 1w |
| Bank: cross-sell offer | >2y | 41w | 12w |
| Telco: recharge reminder | 16w | 2.5w | 1.3w |
| Telco: data-pack offer | 3.6w | 2.2w | 2.0w |
| SaaS: onboarding nudge | >2y | 15w | 4.6w |
| SaaS: trial-to-paid nudge | >2y | 19w | 7.2w |

What these tables mean for the product:

* **Proving a play is cheap at enterprise scale; the calendar is the outcome window.** 30-day
  outcomes (cross-sell, trial-to-paid) cannot be proven faster than 30 days plus accrual. Weekly
  proof reports on 7-day outcomes are realistic.
* **Choosing between close variants is where volume runs out.** At 1M users a 10% variant gap is
  out of reach for most plays; the agent should explore variants only where the gap is plausibly 25%
  or more of the lift, or use many plays pooled.
* **The programme (always-on) plays hit a person-level noise floor**, not a volume floor. A
  pre-period covariate matters more than waiting.
* **Cross-channel stacking is not modelled.** Users in push, SMS, email and WhatsApp plays at once
  share one outcome; per-play effects are only identified if each play is randomised independently.

## 4. Per-person heterogeneity: how many interactions before a per-person policy beats a segment policy

### Model

For person i and interaction t: `y_it = a_i + theta_i * T_it + e_it`, `Var(e) = sigma^2 = p(1-p)`
(Bernoulli outcome), each interaction randomised treated with probability q = 0.9 (a 10% per-send
holdout). Person effects `theta_i ~ N(theta_seg, omega^2)` within a segment, with segment mean known
(it is estimated from millions of users). After m interactions the estimate of theta_i has variance
`sigma^2 / (m q (1-q))`, the posterior mean is shrunk to the segment mean with weight
`B = omega^2 / (omega^2 + sigma^2 / (m q (1-q)))`. The per-person policy treats when the posterior mean
net value is positive; the segment policy treats all or none by the segment mean. The expected gain of
the person policy over the segment policy is `E[(thr - posterior mean)+]` (or the mirror), a closed
form in normal densities, and the oracle (perfect knowledge of theta_i) uses omega instead of
sqrt(B) omega. "Share" is gain divided by oracle gain. When the segment mean sits exactly on the
break-even threshold, share = sqrt(B).

By construction the gain is positive for any m (Bayes shrinkage never loses in expectation), so the
question is how many interactions before it is worth anything. The table gives m for 25% and 50%
of the oracle gain.

### Table P1. Interactions per person needed (m, randomised sends or interactions)

`omega / mean` is the person-to-person spread of the true effect relative to the mean effect. "At
threshold" means the segment's mean net value is zero; "1 SD off" means it is one person-SD from the
break-even.

| Play (outcome noise p, mean lift) | omega / mean | m for 25%, at threshold | m for 50%, at threshold | m for 25%, 1 SD off | m for 50%, 1 SD off |
|---|---:|---:|---:|---:|---:|
| Food push, 24 h order, p 6%, +0.3 pp | 0.5 | 18,568 | 92,840 | 234,989 | 518,127 |
| Food push | 1.0 | 4,642 | 23,210 | 58,747 | 129,532 |
| Food push | 2.0 | 1,160 | 5,802 | 14,687 | 32,383 |
| Telco SMS, 7 d recharge, p 45%, +1.35 pp | 0.5 | 4,024 | 20,119 | 50,924 | 112,281 |
| Telco SMS | 1.0 | 1,006 | 5,030 | 12,731 | 28,070 |
| Telco SMS | 2.0 | 251 | 1,257 | 3,183 | 7,018 |
| Bank cross-sell, 30 d, p 0.6%, +0.15 pp | 0.5 | 7,854 | 39,269 | 99,395 | 219,157 |
| Bank cross-sell | 1.0 | 1,963 | 9,817 | 24,849 | 54,789 |
| Bank cross-sell | 2.0 | 491 | 2,454 | 6,212 | 13,697 |

Share of the oracle gain by interactions per person (food push, omega = mean, at threshold):

| m (interactions per person) | 5 | 20 | 50 | 100 | 260 (one year, 5 a week) | 1,000 | 10,000 | 100,000 |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
| Shrinkage weight B | 0.0001 | 0.0003 | 0.0007 | 0.0014 | 0.0037 | 0.0142 | 0.126 | 0.590 |
| Share of oracle gain | 0.8% | 1.7% | 2.7% | 3.8% | 6.1% | 11.9% | 35% | 77% |

Reading it:

* **A user's own history never gets there.** One year of daily-ish pushes (260 interactions) gives 6%
  of the available per-person gain. Reaching 25% needs 1,000 to 4,600 interactions per person if the person-level spread equals the mean effect and the segment sits at break-even, 4,000 to 19,000 if the spread is half the mean, and 13,000 to 235,000 if the segment is one person-SD from break-even. A telco or bank customer with a few
  messages a month needs decades.
* **The per-person effect can only be learned through shared structure**: features (baseline
  frequency, recency, tenure, device, time of day) pooled across all users, which is the learning
  curve in section 5. The hierarchical model's role is to shrink each person to a feature-based
  prior and treat the leftover person effect as noise.
* **The honest decision rule**: use segment (or feature-model) policies; a per-person policy that uses
  only that person's own interactions should earn its place only through the holdout test (section
  3 shows when that is detectable), because by the table it is below 10% of the oracle for almost
  any realistic interaction count.
* This uses an explicit within-person randomisation of sends; a design that only varies sends across
  people (as holdouts do) identifies even less per person.

## 5. Learning curve for feature-based targeting (numpy T-learner on real randomised data)

### Setup

* **Learner.** T-learner: one ridge logistic regression per arm on standardised features, numpy
  damped Newton (the first, undamped version diverged on Criteo's rare outcomes; all numbers here are
  from the damped fit and the old runs were discarded). Treat when predicted lift exceeds the cost.
  Ridge 10 (fixed in advance); ridge 1000 shown as a sensitivity check. Predicted lift is the
  difference of the two predicted probabilities.
* **Value.** One conversion is worth 1; a contact costs `c = m x ATE`, where ATE is the dataset's
  average lift measured on the whole training pool. m = 1 is the repo's break-even convention: blanket
  send-all and send-none tie, so a gain is a pure targeting gain. m = 0.5 means contact is cheap and
  blanket sending is profitable (the policy must remove the unprofitable contacts). m = 2 means
  blanket sending loses money and the policy must find the profitable pockets.
* **Baseline.** The best blanket policy chosen on the training sample (send-all if the sample's
  average lift exceeds the cost, else send-none).
* **Test.** A fixed random half or fixed hold-out set that never enters training: MegaFon 300,000,
  Criteo 3,000,000, Hillstrom 21,306 (10 random splits). The gain is the paired
  inverse-propensity estimate (the randomisation probability is known), per 1,000 customers, with a
  standard error from the per-row differences.
* **Sizes.** Training rows drawn at random from the training pool, 5 seeds for sizes up to 300k,
  3 at 1M, 2 at 3M, 1 at the full pool (10,979,592 Criteo rows: all 13,979,592 minus the 3,000,000
  test rows). "10% holdout" design resamples the training rows to 90% treated and 10% control, which
  is what a real shop's holdout looks like; "natural" keeps the dataset's own split (Criteo 85/15,
  MegaFon 50/50, Hillstrom 50/50). MegaFon's 10% design stops at 160k because only 150k treated rows
  exist in its training half.
* "sig+" is the share of seeds whose gain is above zero by more than 1.96 standard errors on the
  fixed test set.
* This is the T-learner and the blanket baselines only, not the Autonomy agent.

### Table L1. Criteo (13.98M rows; average lift 0.114 pp, so 1.14 net conversions per 1,000 customers)

10% holdout design, ridge 10:

| Train rows (control rows) | m=1 gain per 1,000 [sig+] | m=0.5 | m=2 | contacted at m=1 |
|---|---:|---:|---:|---:|
| 10,000 (1,000) | +0.72 [100%] | +0.44 [100%] | +1.10 [100%] | 14% |
| 30,000 (3,000) | +0.67 [100%] | +0.37 [100%] | +0.62 [100%] | 6% |
| 100,000 (10,000) | +0.73 [100%] | +0.32 [80%] | +0.68 [100%] | 9% |
| 300,000 (30,000) | +0.79 [100%] | +0.26 [100%] | +0.73 [100%] | 8% |
| 1,000,000 (100,000) | +0.89 [100%] | +0.33 [100%] | +0.80 [100%] | 8% |
| 3,000,000 (300,000) | +0.91 [100%] | +0.37 [100%] | +0.83 [100%] | 8% |

Natural 85/15 design, ridge 10, including the full pool:

| Train rows (control rows) | m=1 | m=0.5 | m=2 | contacted at m=1 |
|---|---:|---:|---:|---:|
| 10,000 (1,510) | +0.54 [100%] | -0.01 [60%] | +0.50 [100%] | 13% |
| 30,000 (4,472) | +0.71 [100%] | +0.31 [100%] | +0.66 [100%] | 10% |
| 100,000 (14,985) | +0.78 [100%] | +0.22 [100%] | +0.71 [100%] | 9% |
| 300,000 (44,980) | +0.77 [100%] | +0.25 [100%] | +0.73 [100%] | 10% |
| 1,000,000 (150,070) | +0.85 [100%] | +0.35 [100%] | +0.82 [100%] | 10% |
| 3,000,000 (450,345) | +0.86 [100%] | +0.35 [100%] | +0.84 [100%] | 9% |
| 10,979,592 (1,647,592) | +0.93 [100%] | +0.36 [100%] | +0.83 [100%] | 9% |

(At the full pool one fit, so no seed spread; the standard error on the fixed test set is 0.07.)
With ridge 1000 the 10% design gives +0.84 at 30k, +0.89 at 100k and +0.87 at 3M: the stronger
shrinkage reaches the plateau about 3x earlier, so the sample size at which targeting "works" depends
on the regularisation.

### Table L2. MegaFon (600k rows; average lift 4.98 pp, 49.8 net conversions per 1,000 customers)

10% holdout design, ridge 10:

| Train rows (control rows) | m=1 gain per 1,000 [sig+] | m=0.5 | m=2 | contacted at m=1 |
|---|---:|---:|---:|---:|
| 10,000 (1,000) | +1.81 [40%] | -6.97 [0%] | -8.17 [0%] | 54% |
| 30,000 (3,000) | +3.74 [100%] | -6.12 [0%] | +1.86 [80%] | 51% |
| 100,000 (10,000) | +5.16 [100%] | -4.70 [0%] | +8.93 [100%] | 53% |
| 160,000 (16,000) | +4.97 [100%] | -3.76 [0%] | +8.66 [100%] | 50% |

Natural 50/50 design: +4.58 at 10k, +5.65 at 30k, +5.78 at 100k, +5.98 at 300k (m=1, all
significant); m=0.5 stays negative at every size (-5.0 to -2.3); m=2 is negative at 10k (-3.2) and turns positive from 30k (+6.3 at 30k, +9.7 at 100k, +11.1 at 300k). With ridge 1000 the 10% design needs 100k to 160k rows to
reach significance (+2.9 [60%], +3.9 [80%]).

### Table L3. Hillstrom (men's e-mail vs none; 42,613 rows; average lift 0.71 pp)

| Train rows (control rows) | m=1 gain per 1,000 [sig+, 10 splits] | m=0.5 | m=2 |
|---|---:|---:|---:|
| 2,500 (1,246) | +0.20 [10%] | -0.28 | -0.75 |
| 5,000 (2,503) | -0.24 [0%] | -0.50 | +0.19 |
| 10,000 (4,999) | -0.58 [0%] | -0.97 | +0.12 |
| 21,307 (10,655) | -0.07 [0%] | -0.68 | -0.24 |

Reading it:

* **Where targeting starts to beat blanket depends on whether the features carry signal and on the
  economics.** At break-even cost (m=1): MegaFon from about 30k training rows with a 10% holdout (3k
  control rows; at 10k the gain is positive but significant in only 40% of seeds); Criteo from
  10k rows. Hillstrom never, up to 21k rows, matching the earlier finding that no rule beat
  "email everyone" there.
* **Criteo's signal is the baseline rate** (the effect tracks the baseline conversion, so a
  response-style ranking works). That is why 1,000 control rows are enough there and why more data
  adds little beyond about 100k.
* **More data keeps helping, slowly.** Criteo at m=1, natural design: 100k rows reach 84% of the
  full-pool gain, 1M 91%, 3M 93%, 11M 100% (+0.93). The remaining gains from 100x data are modest
  (+19%). The extra data is worth more as a way to estimate segment-level and feature-level effects
  with a tight confidence interval than as a way to find a better model with this learner.
* **The cheap-contact regime is the dangerous one.** At m=0.5 the T-learner loses to send-to-all on
  MegaFon at every size up to 300k (-2.3 per 1,000 at 300k, 886 of 1,000 customers still contacted)
  and is at best a small win on Criteo (+0.2 to +0.36 per 1,000 from 30k rows). A targeting policy
  must be gated on a held-out comparison with the blanket policy, as the research notes recommend,
  not trusted by default.
* **Translating rows into weeks.** A 1M-user company that randomises one play on 10% holdout produces
  a 1M-row training set (100k control) within days. At that size both Criteo and MegaFon are on or
  near their plateau. At enterprise scale the time to learn a feature-based targeting policy is not
  the constraint; whether the features carry signal is.

## 6. Pooling across shops: how fast a shared prior helps a new shop

### Model

Shop j has true lift `theta_j ~ N(mu, tau^2)`. Its own estimate after w weeks is `N(theta_j, s_j(w)^2)`
with `s_j(w)^2` from the flow-play variance (section 2). N other shops each have 26 weeks of data
at the same size as this shop, so the pooled mean has variance `(tau^2 + s_0^2) / N`. A new shop's
prior is `N(mu_hat, tau^2 + (tau^2 + s_0^2)/N)`; its posterior standard deviation after w weeks is
`(1/s(w)^2 + 1/(prior variance))^(-1/2)`. Play: the abandoned-cart flow (mean lift 1.5 pp, 0.5% of
profiles entering per week). `kappa = tau / mu` is the between-shop spread of the true effect, the
number we do not know and have to measure; 0.3, 0.6 and 1.0 are shown. Two useful tests: the
posterior of a typical shop (true effect = mu) clears zero at 80% power (posterior sd times 2.8 is at
most mu), and the 95% interval is within plus or minus 50% of the effect.

### Table H1. Weeks until the lift is usable, by shop size, number of other shops with a holdout, and between-shop spread

Cell = weeks until the effect clears zero (80% power) / weeks until it is known within plus or minus
50%. ">520" means more than 10 years.

kappa = 0.3 (shops differ by 30% of the mean effect):

| Other shops | 5,000 profiles | 20,000 | 100,000 |
|---:|---:|---:|---:|
| 0 (no pooling) | >520 / >520 | 290 / >520 | 58 / 114 |
| 5 | >520 / >520 | 196 / 474 | 14 / 69 |
| 20 | >520 / >520 | 67 / 345 | 1 / 46 |
| 100 | 159 / >520 | 1 / 217 | 1 / 35 |

kappa = 0.6:

| Other shops | 5,000 | 20,000 | 100,000 |
|---:|---:|---:|---:|
| 0 | >520 / >520 | 290 / >520 | 58 / 114 |
| 5 | >520 / >520 | 239 / 517 | 43 / 99 |
| 20 | >520 / >520 | 208 / 486 | 40 / 95 |
| 100 | >520 / >520 | 192 / 471 | 38 / 94 |

kappa = 1.0: pooling barely helps (100k shop: 58 weeks alone, 51 weeks with 100 other shops;
5,000-profile shop never).

Reading it:

* **Pooling turns "never" into "weeks" only when shops are alike.** With kappa = 0.3 and 20 other
  shops, a 100k shop's lift is usable in 1 week and a 20k shop's in 67 weeks; with 100 shops a 20k
  shop is usable immediately. A 5k shop needs 100 shops and still waits about 3 years.
* **The floor is the between-shop spread**, not the number of shops: once the pooled mean is
  known, the prior sd cannot shrink below tau. With kappa = 1 the prior is worth almost nothing,
  because a prior that says "the effect is 1.5 pp plus or minus 1.5 pp" is no tighter than the
  data of a 100k shop after a year.
* **What to measure first**: the between-shop spread tau on one play (abandoned cart is the best
  candidate) from the first 10 to 20 shops with holdouts; it decides whether a shared prior is a
  product feature or a footnote.
* The same arithmetic applies inside one large company to its regions, brands or cohorts: pooling
  across 50 cities makes each city's estimate usable weeks earlier if cities share an effect.

## 7. Small shops (secondary): 5k, 20k, 100k, 500k contactable profiles

Same engine, shop assumptions from section 1.3.

### Table S1. Play lift vs holdout

| Play | 5,000 | 20,000 | 100,000 | 500,000 |
|---|---:|---:|---:|---:|
| Campaign email (1 a week, 70% of list), buy in 7d | 6.7y | 88w | 18w | 4.5w |
| Abandoned-cart flow, buy in 7d | >10y | 5.6y | 59w | 13w |
| Browse-abandonment flow, buy in 7d | >10y | 7.0y | 74w | 16w |
| Win-back (lapsed), buy in 14d | >10y | 4.4y | 47w | 11w |
| Replenishment reminder (consumables), buy in 14d | >10y | >10y | 3.8y | 41w |

### Table S2. Variant difference (25% of the lift)

| Play | 5,000 | 20,000 | 100,000 | 500,000 |
|---|---:|---:|---:|---:|
| Campaign email | >10y | >10y | 2.4y | 26w |
| Abandoned-cart flow | >10y | >10y | 8.4y | 88w |
| Browse-abandonment flow | >10y | >10y | >10y | 2.2y |
| Win-back | >10y | >10y | 10.0y | 2.0y |
| Replenishment reminder | >10y | >10y | >10y | 5.4y |

### Table S3. Five-segment heterogeneity (central pattern)

| Play | 5,000 | 20,000 | 100,000 | 500,000 |
|---|---:|---:|---:|---:|
| Campaign email | >10y | >10y | 2.8y | 30w |
| Abandoned-cart flow | >10y | >10y | 9.4y | 99w |
| Browse-abandonment flow | >10y | >10y | >10y | 2.4y |
| Win-back | >10y | >10y | 7.4y | 78w |
| Replenishment reminder | >10y | >10y | >10y | 6.4y |

With the weak pattern every cell below 500k is above 10 years and at 500k it is 2.3 to more than 10
years. Seasonal peaks help a little (Black Friday carts); the table assumes a steady rate.

Hillstrom-sized effects (+0.68 pp on 0.57%, +119%) would need only 12,000 customers with a 10%
holdout; real flows will show a fraction of that, which is why shops under 100k profiles cannot
prove most plays in a year without a shared prior.

## 8. What this means for the product

1. **For enterprise clients the data is enough to prove plays (weeks), to rank close variants only on
   high-volume, short-window plays, and to learn segment- and feature-level targeting.** Per-person
   learning from own interactions is not available at any realistic interaction count; do not
   promise it.
2. **Spend the holdout carefully.** At 1M users 10% (100k) is enough to prove most plays; a 10% holdout at 50M leaves 5M users uncontacted. The weeks in Table E1 are dominated by the outcome window, so a smaller holdout costs little time: at 50M a 2% holdout (1M users) moves cross-sell from 4.5 to 5.4 weeks and the other plays by 0.7 weeks or less (trial-to-paid 4.4 to 4.7); at 10M a 5% holdout adds up to 1 week (cross-sell 5.5 to 6.5). The saving in lost sales is large.
3. **Use pre-period covariates (CUPED) for any programme-level or high-frequency count outcome**:
   it turns "never" cells into weeks.
4. **Gate targeting.** A T-learner beat break-even blanket on MegaFon from 30k rows and on Criteo from
   10k rows, but lost to blanket when contact was cheap on MegaFon at every size. The agent should
   target only where a held-out comparison says so, as the research notes advise.
5. **Plan for correction.** Running 20 plays in parallel costs 1.9x the sample per play.
6. **Measure the real lift distribution early.** The weakest inputs are the assumed lifts in 1.3 and
   the between-shop spread in section 6. The first 10 to 20 shops or the first enterprise holdout
   replace both with data.

## Assumptions and limits

* Lifts for all plays except the MegaFon data-pack offer are assumed, not measured. Published rates
  are attributed (click or open), not incremental, so the assumed lifts are set below them; the
  one holdout study found (Polar) shows no purchase effect at all.
* Outcomes are treated as independent across users and weeks; users who enter the same flow twice, or
  appear in several plays, are correlated (not modelled). Programme plays use a person-level
  gamma-count model; real heavy-user distributions have fatter tails.
* The 5-segment test assumes equal-size segments, a fixed lift spread and no multiple testing across
  segments. Real segments are unequal, which slows detection.
* Calendar floors: 7 days minimum; outcome windows added to accrual time. Seasonality, send-rate
  limits, deliverability changes and channel consent rules are not modelled.
* Learning curve: logistic T-learner, standardised raw features, ridge fixed in advance. A boosted
  learner could do better, especially for MegaFon at m=0.5. Cost and conversion value are set from the
  dataset's own average lift (cost = m x ATE), not from real margins. Criteo and MegaFon are one-shot
  campaigns; Criteo's features are anonymised.
* Per-person model: time-invariant person effects, normal effect distribution, known
  segment mean, binary outcome noise p(1-p), 90% treated per interaction.
* Pooling: normal-normal, equal-size comparable shops, the same play, known between-shop
  spread; the actual spread is unknown.
* Fixed-horizon tests; always-valid sequential intervals will cost somewhat more time.

## Appendix: commands and files

All files are in
`/tmp/claude-0/-home-user-Marketing-ai-/c7787998-074f-51f3-9065-71bef613b34b/scratchpad/power/`
(`SP` below). Run with `python3 -I`.

```
python3 -I SP/prep_data.py            # Hillstrom, MegaFon, 30% Criteo sample, X5 labels -> npz
python3 -I SP/prep_criteo_full.py     # all 13,979,592 Criteo rows -> float32 npy (670 MB)
python3 -I SP/power.py                # Tables E1-E3, S1-S3 (power engine: flow and programme plays, 5-segment omnibus)
python3 -I SP/extras.py               # measured effects, 10% holdout n per dataset, CUPED check, Bonferroni factors
python3 -I SP/model.py                # per-person normal-normal model (Table P1) and pooling (Table H1)
python3 -I SP/run_small.py megafon    # learning curve, MegaFon (natural and 10% holdout)
python3 -I SP/run_small.py hillstrom  # learning curve, Hillstrom (10 splits)
python3 -I SP/run_criteo.py           # learning curve, Criteo up to 10,979,592 training rows (about 12 min, 6 GB peak)
python3 -I SP/lc_tables.py            # prints the learning-curve tables from the result JSON
```

Learner and paired inverse-propensity gain: `SP/lc.py`. Result JSON: `SP/lc_*.json`, console
outputs `SP/power_out.txt`, `SP/model_out.txt`, `SP/extras_out.txt`, `SP/crit_sum.txt`,
`SP/mega_sum.txt`, `SP/hill_sum.txt`. Peak memory stayed under about 6 GB; the 4-CPU machine was
shared. Datasets used: hillstrom, x5_retailhero (labels only), criteo_uplift, megafon; lenta and
starbucks_promo were not opened.

Context documents read: `docs/v2/REAL_DATA_RESULTS.md`, `docs/v2/REAL_BENCH.md` (AFTER and Sealed
sections), `docs/research/v2/learning/02_marketing_ml_production.md`. They agree with the
findings here: blanket sending is the strong default; a ranking has to earn the right to target
out of sample; and Starbucks' sealed result shows the cost of "held-out evidence decides" at
60,000 customers (a +0.013 per customer gain could not be proven with half of them).
