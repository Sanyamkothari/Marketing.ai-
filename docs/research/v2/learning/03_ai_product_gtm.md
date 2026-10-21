# Building and selling an AI-native marketing agent: lessons for Autonomy's product and go-to-market

21 Oct 2026. Research note for the founders. It covers 15 sources from 2024-2026 on AI agents,
outcome pricing, trust and autonomy, the Klaviyo/Shopify channel, measurement startups and AI
product failures. Each source has its key points, then what it means for Autonomy.

**How this was researched.** This session's network proxy blocked direct page fetches (every host
returned "connect rejected"). So everything below comes from search-engine extracts of the linked
pages, not from reading the pages in full. Text in quotation marks is wording the extracts
attributed to the source. Treat every number as "check before it leaves the team", especially the
third-party estimates (Sacra, review sites).

**Our own context, which changes how to read all of this.** `docs/v2/REAL_DATA_RESULTS.md`
(20 Oct 2026) found that:
- on randomised data the agent **never beat the best simple policy**;
- it **overestimates a message's value by about 10x**: on Hillstrom it expected $4.08 per e-mail
  after cost, against $0.38 measured.

So today the honest product is the measurement and the guardrails, not "the AI beats your team".
Several lessons below push hard in the same direction.

---

## The short version: 10 lessons

1. **Lead with the audit, not the autopilot.** Sell proof first and autonomy second. Every source
   on trust (Lenny's CC/CD, the Knight levels, the 11x story) says to start with high control, and
   our own data says the agent has not yet earned more.
2. **Klaviyo is now our biggest competitor, not only our channel.** Its Marketing Agent (Sep 2025),
   now called Composer, "plans, builds, and launches campaigns, flows, and sign-up forms". Planning
   and writing campaigns are becoming a free feature, as happened to Jasper. What Klaviyo is badly
   placed to sell is a measurement that says its own dashboard's numbers are too high.
3. **Do not price purely on proven extra profit yet.** Outcome pricing works when the outcome is
   countable, immediate and undisputed, like Fin's resolutions. "Extra profit vs a holdout" is none
   of those for a small shop. Use a hybrid: a flat fee, plus a guarantee or a capped bonus.
4. **The proof must be pre-agreed and conservative.** Bill on the lower end of the range, from a
   method signed before the pilot starts. Euclid's "False Profits" warns that both sides must
   "measure the result and agree on it, perpetually".
5. **An audit on its own is a "tourist" product.** a16z's retention work implies a one-off report
   churns by month 3. The audit must turn into an always-on holdout monitor plus fixes, or it is a
   consulting gig.
6. **Emails cannot be undone, so the review comes before the send, and it must not become a rubber
   stamp.** Approve whole batches and policies, add a short cancel window before sends go out, and
   give each decision a one-line reason. Do not ask for daily approvals.
7. **Agencies are both the channel and the people we threaten.** Sequoia's "sell the work" thesis
   points straight at the agency retainer budget. With two founders, though, agencies are the
   fastest way to reach 20 shops. Give agencies the audit as their tool, and do not pitch shops on
   replacing their agency.
8. **Install inside the workflow the shop already has.** MIT NANDA found that tools which don't fit
   how people already work, or don't learn, stall. Sending through the shop's own Klaviyo is right.
   Keep the Autonomy UI to a weekly read.
9. **Overclaiming kills agent startups.** 11x shows the pattern: strong demos, then churn once
   results land, and contracted revenue reported as if it were real. Our pitch's "3-10% extra
   profit" and "paid in results" need to match what the proof page can actually show.
10. **App stores come after pilots, not before.** A Klaviyo listing needs 5 production installs.
    On Shopify, new apps report slow reviews and say activation, not installs, is the bottleneck.
    Pilots are the launch.

---

## The sources

### 1. Intercom Fin: outcome pricing that worked

**Sources:**
[Intercom pricing](https://www.intercom.com/learning-center/intercom-pricing),
[Macha on Fin pricing](https://www.getmacha.com/blog/intercom-fin-pricing),
[Enterprise DNA](https://enterprisedna.co/resources/ai-pulse/ai-pulse-2026-08-02-intercom-s-fin-ai-agent-is-nearing-100m-arr-roughly-half-of/),
[Sacra](https://sacra.com/c/intercom/),
[Mostly Metrics](https://www.mostlymetrics.com/p/how-intercom-reaccelerated-growth-with-outcome-based-pricing)

**What they say:**
- **The price:** "$0.99 per outcome", at most one charge per conversation. Escalations to a human
  are free.
- **The fine print:**
  - Silence counts as a win: a conversation is an "assumed resolution" if the customer doesn't
    reply for 24 hours.
  - There is a monthly floor of 50 outcomes.
  - Lead qualification costs $9.99.
  - One small business worked out its all-in cost at $1.40 per resolution.
- **The results (third-party estimates):** Fin is near $100M ARR, about half of Intercom's revenue.
  Its resolution rate rose from about 25% to 65-70%. Sacra credits outcome pricing with lifting net
  revenue retention from 112% to 146%.

**What this means for Autonomy:**
- **Why it works for Fin:** each outcome is counted within 24 hours, per conversation, and the
  customer can see it. As Fin gets better, Intercom earns more, so the incentives line up.
- **Why we are different:** our outcome is a statistical difference between two groups. It takes
  weeks to measure and, on a small shop, its range often includes zero. We cannot bill per
  decision.
- **What to copy:**
  - Escalations are free (for us: "doing nothing" and held-back sends cost nothing).
  - A small floor fee.
  - A simple unit the customer can predict.
- **Risk:** Fin's "silence counts as a win" rule is exactly the kind of rule that breeds disputes.
  Our version would be crediting ourselves with purchases the holdout shows would have happened
  anyway. Don't.

### 2. Sierra (Bret Taylor): per-outcome pricing as the future of software

**Sources:**
[Cheeky Pint interview](https://cheekypint.substack.com/p/bret-taylor-of-sierra-on-ai-agents),
[Sequoia Training Data](https://sequoiacap.com/podcast/training-data-bret-taylor),
[Sierra pricing review](https://aiagentsquare.com/agents/sierra-ai)

**What they say:**
- "We think outcome-based pricing is the future of software."
- Sierra charges a pre-negotiated rate when the agent resolves the issue on its own, and nothing
  when it escalates to a person.
- Taylor compares this to a salesperson's commission.
- Early on, "the mechanics were still being worked out with early customers".
- There is no public price and no self-serve plan; it is sold to enterprises.

**What this means for Autonomy:**
- The commission framing fits our pitch ("paid in results") and is persuasive to an owner.
- But Sierra negotiates each enterprise deal by hand, with plenty of volume per customer. A
  $2M-a-year Shopify shop gives us neither.
- **Disagreement with the hype:** outcome pricing is right in spirit for us, but only once our
  measurement is trusted and has enough volume. For pilots, use a flat fee with a guarantee.

### 3. Euclid, "False Profits" (Oct 2026): the case against outcome pricing

**Source:** [The Verticalist](https://insights.euclid.vc/p/vertical-ai-outcome-pricing)

**What it says:**
- Outcome-based pricing is "bad advice for most Vertical AI founders".
- Both sides have to "measure the result and agree on it, perpetually".
- Outside a few tried-and-true use cases, it is "rarely achievable today".
- It needs new technology, a new category and a new pricing model to arrive together within a few
  years, which the authors call "unheard of".

**What this means for Autonomy:**
- This is the strongest counterweight to Sierra and Intercom, and it describes our situation
  exactly: our outcome is a disputed counterfactual.
- **Opinion:** our one advantage is that we own the measurement, a randomised holdout with a range.
  That is better than most "outcome" claims, but not enough to make it a billing meter for a
  small shop.
- **Use it as:**
  - the sales argument ("we prove it");
  - a guarantee ("if the lower end of the range is below zero after 60 days, you don't pay");
  - an annual renewal anchor.
- **Don't use it as:** the invoice line.

### 4. Kyle Poyar, State of B2B monetization 2025/2026 (Growth Unhinged)

**Sources:**
[2026 report](https://www.growthunhinged.com/p/the-state-of-b2b-monetization-in-2026),
[What's changing with AI monetization](https://www.growthunhinged.com/p/whats-changing-with-ai-monetization)

**What it says:**
- Hybrid pricing is the most common model at 37%.
- Only "5% named [outcome pricing] their primary model", up from 3% in 2024.
- AI credits are spreading: 29% sell them today.
- "Nobody is happy with their pricing. The biggest complaint: not enough expansion revenue."
- Buyers object that usage pricing is "too complicated and too hard for me to predict".

**What this means for Autonomy:**
- Small and mid-size shop owners hate bills they cannot predict.
- **Recommended shape:**
  - Flat monthly tiers by the number of customers managed. This also handles expansion: as the
    shop grows, it moves up a tier.
  - An optional performance bonus, capped and paid quarterly, only on the lower end of the proven
    range.
- Do not sell credits. Our cost per decision is tiny, so credits would only add confusion.

### 5. Bessemer, AI pricing and monetization playbook (2026)

**Source:** [BVP Atlas](https://bvp.com/atlas/the-ai-pricing-and-monetization-playbook)

**What it says:**
- AI companies are moving from selling access to selling results.
- Price metrics fall into three families: consumption, workflow and outcome.
- AI apps run at about 50-60% gross margin, against 80-90% for traditional SaaS.
- Fin is the model case: the price is the same whether a ticket takes 3 messages or 30.

**What this means for Autonomy:**
- Our costs are mostly LLM copy plus the Critic. That cost is small per customer per week, so
  margin is not our constraint; trust is.
- Pick a "workflow" price metric now: customers under management, or goals running. Move toward
  outcome pricing once proof pages show tight ranges across several shops.

### 6. Sequoia (Julien Bek, Mar 2026), "Services: The New Software"

**Sources:**
[Fortune](https://fortune.com/2026/04/21/services-are-the-new-software-sequoia-venture-capital-julien-bek-ai-native-eye-on-ai/),
[SmarterX summary](https://smarterx.ai/smarterxblog/services-the-new-software-copilot-to-autopilot),
[Roger Wong](https://rogerwong.me/2026/03/services-new-software)

**What it says:**
- **Copilots versus autopilots:** copilots sell tools to people doing the work, and draw on the
  small tool budget. Autopilots sell the finished work, and draw on the much larger "work budget".
- **Start where work is already outsourced.** The buyer already pays an outside firm, so there is
  a budget line to replace.
- **A human stays in the loop:** Bek says "autopilot" was misread. "A human is still there
  monitoring the systems and handling the hardest tasks."

**What this means for Autonomy:**
- Lifecycle email for Shopify shops is already outsourced, to Klaviyo agencies on monthly
  retainers. That is the "work budget" we'd be replacing. It is a much bigger budget than a $99
  app.
- **Disagreement with our plan:** taking that budget makes agencies our competitors, but agencies
  are also the fastest route to shops. The move that fits a two-founder team:
  - **First:** sell to agencies as a copilot (the audit plus proof).
  - **Later:** sell autopilot direct to shops that have no agency.
- Note that Bek's "human still monitoring" matches our exceptions-only inbox. That supports the
  ZERO_TOUCH design rather than "no human ever".

### 7. Levels of Autonomy for AI Agents (Feng, McDonald, Zhang; Knight First Amendment Institute, 2025)

**Sources:**
[Knight Institute](https://knightcolumbia.org/content/levels-of-autonomy-for-ai-agents-1),
[arXiv 2506.12469](https://arxiv.org/abs/2506.12469)

**What it says:**
- Autonomy is a design choice, separate from what the agent can do. Even a highly capable agent
  can run at a low level of autonomy.
- **The five levels**, named by the user's role: operator, collaborator, consultant, approver,
  observer. At the top level, observer, the user has "no capacity to intervene".
- **"Autonomy certificates":** a way to govern what an agent is allowed to do.

**What this means for Autonomy:**
- Our modes map onto the levels like this:

  | Our mode | Level in the paper |
  |---|---|
  | Shadow | consultant: it proposes, the shop acts or doesn't |
  | Approve | approver |
  | Autopilot (with limits and the Pause button) | between approver and observer |

- We should never claim the observer level. The Pause button and the auto-pause are the product.
- The signed mandate (limits, budget, hard rules) is in effect an autonomy certificate. Show it as
  one: a one-page "what Autonomy may do in your shop", versioned and signed. Agencies can show that
  page to their clients.

### 8. Lenny's Newsletter: Reganti and Badam, CC/CD and "why most AI products fail"

**Sources:**
[guest post](https://www.lennysnewsletter.com/p/why-your-ai-product-needs-a-different),
[podcast](https://podcasts.apple.com/us/podcast/why-most-ai-products-fail-lessons-from-50-ai-deployments/id1627920305?i=1000744678894)

**What it says:**
- "AI products are inherently non-deterministic, and you need to constantly negotiate the tradeoff
  between agency and control."
- Start with features that are high-control and low-agency: small, testable and easy to observe.
- Widen autonomy only after reliability is proven in low-stakes settings.

**What this means for Autonomy:**
- Shadow, then Approve, then Autopilot is the right ladder. The step up should be earned per
  goal, not per shop.
- Practical rule: the first goal in Autopilot is the lowest-risk one, a refill reminder with no
  discount. Discounts and win-back offers stay in Approve until that goal's own proof is positive.
- **Our real-data results add a hard constraint:** Autopilot needs its forecasts to be calibrated
  as well as its proof to be positive. Today it is off by about 10x. ZERO_TOUCH's "its forecasts
  matched within Y" gate is essential, not optional.

### 9. Smashing Magazine (Feb 2026) and other human-in-the-loop guides

**Sources:**
[Smashing](https://www.smashingmagazine.com/2026/02/designing-agentic-ai-practical-ux-patterns/),
[NeuralTrust](https://neuraltrust.ai/blog/human-in-the-loop-ai-agents),
[Tcules](https://www.tcules.com/insights/human-in-the-loop-ux-patterns)

**What they say:**
- **Autonomy dial:** "The Autonomy Dial, a form of progressive authorization, allows users to set
  their preferred level of agent independence."
- **Undo:** reversibility is the strongest trust builder. Undo only works if the effect is held
  back; if an action reaches a person immediately, the review must happen before it runs.
- **Approval fatigue:** blanket approvals become rubber stamps.
- **Tiered actions:** tier actions by risk.
- **Explanations:** put a one-line "why" beside each step. Confidence scores alone don't improve
  people's decisions.

**What this means for Autonomy:**
- **A sent email cannot be undone.** So:
  1. Approve at the level of the policy and the batch, never per message.
  2. Hold sends in a short cancel window (for example, a 2-hour queue with "cancel all") during
     the first weeks of Autopilot.
  3. Treat new things (a new offer type, deeper discounts, a new channel) as the high-risk tier
     that always needs approval.
- Our "tap a customer, see the reason" screen is the right explanation design. Do not lead with
  confidence percentages; lead with the plain reason and the range of money.

### 10. Klaviyo Marketing Agent / Composer (Sep 2025 to Jun 2026)

**Sources:**
[Klaviyo newsroom](https://www.klaviyo.com/newsroom/marketing-agent),
[Klaviyo blog](https://www.klaviyo.com/blog/introducing-kai-marketing-agent),
[MarTech](https://martech.org/klaviyo-introduces-marketing-agent-to-automate-campaigns/)

**What they say:**
- Klaviyo's agent "plans, builds, and launches campaigns, flows, and sign-up forms".
- Shops can launch campaigns "in just three clicks" by entering their website URL.
- Automated offer and creative testing was announced as coming later.
- Composer moved to public beta on 30 June 2026.
- Separately, Klaviyo's global holdout groups are only for brands with 400k+ profiles
  ([Klaviyo blog](https://www.klaviyo.com/blog/global-holdout-groups)).
- Our connector research found that Klaviyo's predictive AI needs 500+ ordering customers and 180
  days of history.

**What this means for Autonomy:**
- **This is the biggest risk in the whole plan.** "AI that writes and launches your campaigns" is
  now a free feature inside the platform we send through.
- **Where we can still win:**
  1. **Honest proof:** per-flow holdouts for shops with fewer than 400k profiles. Klaviyo has a
     conflict of interest here, because its attributed-revenue dashboard is its sales pitch.
  2. **Deciding who not to contact:** suppressing sends that cost more than they earn. Klaviyo
     earns more as more messages are sent.
  3. **Margin-aware offers.**
- **Disagreement with the pitch:**
  - Drop "an AI copywriter" as a headline feature. Klaviyo gives that away (and see Jasper below).
  - Do not position as "Klaviyo's AI, but better". Position as "the referee that tells you what
    your Klaviyo actually earns".
- **Exit note:** Braze bought OfferFit and Salesforce-world players buy decisioning, so Klaviyo
  buying or building this is a likely end-state. Keep the integration clean.

### 11. OfferFit to Braze ($325M, 2025) and Hightouch AI Decisioning

**Sources:**
[Braze press release](https://www.braze.com/press-releases/braze-announces-agreement-to-acquire-offerfit),
[MarTech](https://martech.org/braze-picks-up-ai-decisioning-with-offerfit-acquisition/),
[VentureBeat](https://venturebeat.com/business/how-offerfit-uses-self-learning-ai-to-stop-customer-churn),
[Hightouch AI Decisioning](https://hightouch.com/platform/ai-decisioning),
[Hightouch agent docs](https://hightouch.com/docs/ai-decisioning/agents)

**What they say:**
- **OfferFit:**
  - Its motto was "from A/B to AI": reinforcement-learning agents replace manual A/B testing.
  - It was enterprise-focused (telecom, energy, retail) and described as SaaS "with significant
    service components".
  - One customer said earlier A/B tests took "four to six weeks to spin up".
  - Braze paid $325M, and it is now sold as BrazeAI Decisioning Studio.
- **Hightouch:**
  - A holdout setting is built into the agent (10-20%).
  - Vendor case studies: PetSmart +22% incremental bookings in 3 weeks, Whoop +10% cross-sell in
    6 weeks.
  - One customer said they ran more learnings in six weeks than in the prior year of experiments.
  - Competitor GrowthLoop criticises it for limited visibility into its decisions.

**What this means for Autonomy:**
- **The category is real and worth something.** The winners paired "decide here, send there" with
  a built-in holdout.
- **But both did it with enterprise data and service-heavy onboarding.** Small shops have about
  100x less volume per arm. So:
  - Our agent will learn more slowly.
  - The proof ranges will be wide.
  - Pooling priors across shops is the technical bet that makes the small-shop version work.
- **Copy:**
  - Hightouch's always-on holdout.
  - The "speed of learning" pitch (useful to agencies).
- **Avoid:** GrowthLoop's criticism. Visibility of decisions is a selling point for us.

### 12. Aampe ($18M Series A, Dec 2024)

**Sources:**
[SiliconANGLE](https://siliconangle.com/2024/12/10/aampe-raises-18m-scale-consumer-app-personalization-ai-agents/),
[DataRoot Labs interview](https://datarootlabs.com/blog/paul-meinshausen-aampe-interview)

**What they say:**
- Aampe runs one agent per user, which decides "what to show, when, and whether to show anything
  at all".
- It says it has 100M+ agents deployed, selling to consumer apps.
- It plans to "double its employee base" to help customers integrate. In other words, onboarding is
  heavy on people.

**What this means for Autonomy:**
- This is the closest match to our "including doing nothing" idea, so the idea is not unique;
  being for small shops and proving the money is what's different.
- Even well-funded agent companies need people to onboard customers. Budget founder time for each
  pilot (about 3-5 hours of setup and review per shop) and don't promise "zero touch" to the first
  10.

### 13. Jasper's rise and fall (2022-2024)

**Sources:**
[Maginative](https://www.maginative.com/article/jasper-cuts-internal-valuation-as-ai-growth-slows/),
[Voicebot](https://voicebot.ai/2023/07/17/jasper-ai-laying-off-staff-9-months-after-125m-raise/),
[Ben's Bites](https://bensbites.beehiiv.com/p/jasper-cuts-internal-valuation-growth-slows)

**What they say:**
- Jasper was valued at $1.5B in 2022.
- In 2023 it cut staff and cut its revenue forecast by at least 30%.
- ChatGPT gave away similar writing for free, and Jasper retreated to enterprise marketing teams.

**What this means for Autonomy:**
- Anything a general model or the platform (Klaviyo) can do is not a moat. For us that includes
  copywriting, subject lines and "plan a campaign".
- What lasts:
  - a shop's own randomised history (the holdout data compounds and cannot be copied);
  - pooled learning across shops;
  - the trust record (a signed mandate, no incidents).
- Put all marketing weight there.

### 14. a16z, "Retention Is All You Need", and the 11x story

**Sources:**
[a16z](https://a16z.com/ai-retention-benchmarks/),
[TechCrunch on 11x](https://techcrunch.com/2025/03/24/a16z-and-benchmark-backed-11x-has-been-claiming-customers-it-doesnt-have),
[Explorium](https://www.explorium.ai/blog/data-for-gtm/11x-ai-sdr-customer-loss-what-comes-next/)

**What they say:**
- **AI tourists:** "M3 represents your 'true' customer base after AI tourists have churned out."
  An a16z guest also says "retention problems are activation problems in disguise."
- **11x:**
  - It showed logos of companies that weren't customers.
  - ZoomInfo said its trial "performed significantly worse than our SDR employees".
  - An employee claimed 70-80% churn, concentrated in early cohorts. 11x disputes this and cites
    79% retention.
  - It counted contracts with 3-month break clauses at their full annual value.
  - Commentators point to "strong demo performance on curated data, followed by production churn".

**What this means for Autonomy:**
- **Activation:** define it as "the first proof page with a range that excludes zero", not
  "connected". Track the time it takes to get there.
- **Retention risk:** if a shop gets a one-off audit and leaves, that is tourism.
- **Honesty:**
  - Never show a pilot logo or a "3-10%" figure we didn't measure ourselves.
  - Report pilots as pilots, not as revenue.
  - Our README-style honesty ("it hasn't run with a real business yet") is an asset. Keep it in the
    sales material.
- **The 11x risk applies to us directly.** Autonomous sending reaches the shop's own customers.
  One embarrassing email (a wrong discount, or a promotion sent after a complaint) ends a pilot and
  the referral with it. The hard rules and the Critic must be over-tested before the first real
  send.

### 15. MIT NANDA, "The GenAI Divide" (2025)

**Sources:**
[Fortune via Yahoo](https://finance.yahoo.com/news/mit-report-95-generative-ai-105412686.html),
[critique](https://agentmodeai.com/the-mit-genai-pilot-failure-claim/)

**What it says:**
- About 95% of enterprise generative-AI pilots showed no measurable profit-and-loss impact.
- Its diagnosis is a "learning gap": tools that don't keep feedback, don't adapt to the workflow,
  and don't improve.
- Buying from specialised vendors succeeded about 67% of the time, against about 22% for internal
  builds.
- The headline figure is contested: "no measurable P&L impact" is not the same as "the pilot
  failed".

**What this means for Autonomy:**
- This supports our "measure P&L" angle. Most buyers have been burned by AI that couldn't show
  money, and a holdout-based number answers that directly.
- It also supports working inside Klaviyo rather than asking the shop to adopt a new tool.
- "It learns every night from your results" must be visibly true. Show a weekly "what I learned"
  line. That is the learning-gap fix, made visible.

### 16. Haus and incrementality as a wedge

**Sources:**
[Haus GeoLift](https://www.haus.io/geolift),
[Supermetrics podcast with Olivia Kory](https://supermetrics.com/podcasts/incrementality-testing-olivia-kory),
[Darkroom](https://www.darkroomagency.com/observatory/why-geo-experimentation-is-becoming-the-source-of-truth)

**What they say:**
- Haus built a business selling DTC brands one thing: experiments instead of last-click
  attribution. Named customers include Caraway, Jones Road and AG1.
- Its Olivia Kory says they "don't claim to be the one single source of truth"; experiments sit
  next to day-to-day reporting.
- In a 2022 survey, only 4.7% of DTC marketers used incrementality tests as their main measurement.

**What this means for Autonomy:**
- **Proof that "measurement first" can be a wedge**, and that it is the respectable way into
  brands. But the market needs teaching, and Haus sells to brands with big paid-media budgets.
- **For email the pain is different.** Klaviyo's attributed revenue is the agency's report card,
  and many operators already suspect it is inflated (see the agency how-tos at
  [Repeat](https://blog.getrepeat.io/how-to-create-a-holdout-test-in-klaviyo/) and
  [Zettler](https://zettlerdigital.com/blogs/news/how-to-measure-email-sms-incrementality-with-klaviyo)).
- **The audit is credible only if it is a real forward holdout:** 10-20% held out for 30+ days.
  Klaviyo has no native per-flow holdout. That means:
  - the audit takes 4-6 weeks, not a day;
  - the held-out customers cost the shop some revenue during the test.

  Be upfront about both.
- **Copy Haus's tone:** "we complement your Klaviyo dashboard, we don't call it a liar". Agencies
  will listen to that; they will fight "your agency's numbers are fake".

### 17. Klaviyo and Shopify channels

**Sources:**
[Klaviyo app listing requirements](https://developers.klaviyo.com/en/docs/klaviyo_app_listing_requirements),
[Klaviyo technology partner guide](https://www.klaviyo.com/wp-content/uploads/2025/07/Official-Technology-Partner-Program-Guide.pdf),
[Klaviyo agency agreement](https://www.klaviyo.com/legal/partner-terms-conditions),
[Shopify community thread](https://community.shopify.com/t/new-shopify-app-approved-but-struggling-to-get-first-installs-reviews-how-would-you-approach-this/622609),
[first 100 installs](https://discovermybusiness.co/shopify-app-first-100-installs/)

**What they say:**
- **Klaviyo listing:**
  - "Your app must have at least 5 installs with demonstrated Production level API activity", and
    the developer's own accounts don't count.
  - "We expect your app to be live and functional within minutes."
  - The listing needs a benefits page with a demo video.
  - The marketplace has three tiers: Published, Preferred, Premier.
- **Klaviyo agencies:** agencies choose between an Advisor track and a Master track.
- **Shopify:**
  - Founders report months without reviews.
  - Advice is to ask for a review "after a merchant has gotten results".
  - Early installs come from partnerships and community, not ads.
  - Activation, not installs, is the bottleneck.

**What this means for Autonomy:**
- The 5 pilots double as the 5 production installs needed for the Klaviyo listing. Plan it that
  way.
- The Klaviyo marketplace is the better first store: its buyers are already email people, and
  setting up our receiving flow can be automated.
- Shopify's App Store comes later, after the review process (weeks) and once we can ask happy shops
  for reviews at the moment their proof page turns positive.
- **Agencies:** recruit 2-3 Klaviyo agencies with 10-40 shop clients each. Give them a free audit
  for one client, a co-branded report, and a revenue share or white-label option. One agency
  partnership can be worth 20 direct sales calls.

### Briefly: Persado

**Source:** [Gartner reviews](https://gcom.pdo.aws.gartner.com/reviews/product/persado)

Persado has been around since 2012 and sells AI marketing language to the enterprise on quotes and
managed service. I found no evidence that it priced on performance. It confirms the pattern: AI
marketing tools that won enterprise money were service-heavy and sold on custom deals. They were
not self-serve products sold to small businesses.

---

## Recommendations for Autonomy

### Wedge: audit first, autopilot earned

- **What to sell first:** the **Klaviyo Flow Incrementality Audit**, a 4-6 week forward holdout
  on the shop's existing flows. Autonomy runs in Shadow mode alongside it. The output is:
  - which flows earn real extra profit, with ranges;
  - which flows just discount people who would have bought anyway;
  - what Autonomy would have done instead, and what it forecast.
- **Why the audit first:**
  1. It is valuable even if the agent never beats a simple rule (our current state).
  2. It creates the randomised data the agent needs to learn.
  3. It sets up the proof baseline that any later outcome pricing needs.
  4. It is a believable, low-risk first step for an owner or an agency.
- **The trap:** make sure the audit naturally becomes "keep the holdout running and let it fix the
  losing flows". An always-on proof monitor is the retention hook.
- **Disagreement with PITCH.md:**
  - "Paid in results" and "3-10% extra profit" are ahead of the evidence. Lead with "we show you
    what your email really earns".
  - The "Approve mode in week 2" timeline is too fast for real data volumes on small shops.

### Pricing: hybrid, risk-reversed

| Stage | Price | Why |
|---|---|---|
| First 5 pilots | Free, in exchange for data access, a case study and a Klaviyo install | We need the 5 installs and honest results, not revenue |
| Audit, after the pilots | A flat fee (test $500-1,500 one-off) or free for agency partners | Simple to buy; anchored against an agency's monthly retainer |
| Ongoing | Flat monthly fee by customers under management (test $300-1,500 a month), with a guarantee: if the 60-day lower end of the range is below zero, the month is free | The customer can predict it; we carry the risk we say we're sure about |
| Later (with 10+ shops and tight ranges) | Optional performance bonus: x% of the lower end of measured extra profit, capped | Moves toward outcome pricing only once measurement is trusted |

All prices are hypotheses to test in pilot conversations; ask pilots to name them, don't announce
them.

### Trust and autonomy UX

- Earn autonomy **per goal**, gated on two things: the proof's lower end above zero, and the
  forecasts within about 2x of what happened.
- Show the mandate as a signed, versioned "what Autonomy may do" page (an autonomy certificate).
- No undo exists for email. So:
  - approve policies and batches, never single messages;
  - keep a cancel window early on;
  - send anything new or bigger than the mandate to the inbox.
- Give a one-line reason for every decision and show a range of money rather than confidence
  percentages.
- Add a weekly "what I learned" line, so the learning is visible.

### Channels, in order

1. **Founder-led direct sales** to 5 US/UK/Australia Shopify + Klaviyo shops (about $1-20M a year
   in revenue, 5k+ ordering customers, so holdouts have power).
2. **2-3 Klaviyo agencies**, with the audit as their tool.
3. **Klaviyo App Marketplace** listing, after 5 production installs.
4. **Shopify App Store** last.

### Risks, ranked

1. **The agent doesn't beat simple rules** (true today). Mitigation: sell the measurement plus a
   simple-rule fix first. Let the agent replace the rule only where the proof says so.
2. **Klaviyo Composer and native holdouts move down to smaller shops.** Mitigation: speed, pooled
   learning across shops, and the "independent referee" positioning.
3. **Small shops lack the statistical power for tight ranges.** Mitigation: a 5k+ customer minimum
   for pilots, pooled priors, and longer windows. Say "not sure yet" honestly.
4. **Agencies resist an audit that grades their work.** Mitigation: sell through them, and frame
   the result as "where to reinvest".
5. **One bad send ends a pilot.** Mitigation: Shadow first, hard rules, the Critic, a cancel
   window, auto-pause.
6. **Overclaiming, the 11x failure.** Mitigation: publish only measured numbers, with ranges.
