# Positioning decision: replace the client's platform, plug into it, or both?

**Prepared:** 6 October 2026, as the decision Plan J implements (`docs/plans/MARKETING_AI_PLAN_J_LAYER.md` §1)
**Owner:** Minfy — AI/ML team
**Status:** recommendation, awaiting the founders' decision

> **How this was decided.**
> - **Three options were argued**, each by an advocate making its strongest case:
>   - **R:** bundle an open-source CRM and sell a full replacement;
>   - **L:** a pure layer or plugin on the client's existing tools;
>   - **H:** the layer plus a gated "starter activation".
> - **Three judges scored each option independently**, each through one lens: buyer adoption, engineering feasibility, and business defensibility. All three scored R 3/10, L 7/10 and H 8/10.
> - **Evidence:** research on open-source CRM and engagement tools and their licences, integration and marketplace research, the market research in [`MARKET_RESEARCH_2026_10.md`](MARKET_RESEARCH_2026_10.md), and the repository itself. Web access was limited; figures marked *weak* or *background knowledge* need checking before external use.

## The recommendation in one paragraph

Do not bundle an open-source CRM. Do not sell Marketing AI as a full replacement for a client's platform. Build it as a neutral **decide-and-prove layer**. It sits on top of whatever CRM, engagement platform (CEP), messaging provider or warehouse the client already pays for. It decides who to contact and who to leave alone. It hands an approved list to the client's own tool through one standard **Activation Contract**. It then proves the result in rupees against a random holdout that survives the hand-off.

For clients whose tools cannot take in a list, we may later add a small **"starter activation"**. It sends one approved list, once, through the client's own AWS account (SES, End User Messaging) or the client's own WhatsApp or email provider. We build it only if pipeline data shows the need. Multi-step journeys are never part of the product. They are a Minfy services contract.

This is Option H, the hybrid. All three judges ranked it first. Its first two phases are the same as the pure layer, so we commit to those now and decide on the starter using evidence from real prospects. The product serves any B2C business with customer data: retail, BFSI, subscriptions, travel, utilities and telecom. Telecom was only the first example.

## Why not a full replacement

The full-replacement option was well argued. The strongest version was our engine plus the MIT-licensed Dittofeed journey engine plus AWS-rented channels. All three judges still scored it 3/10. These are the reasons.

**1. Buyers will not switch, and the person who blocks adoption hates switching.**
- Campaign managers judge a tool first on whether lists reach the tool they already use (market research, persona 2). Five research areas flagged this.
- Moving to a new engagement platform takes 3–6 months for Braze, 3+ months for WebEngage and about 4 months for Adobe AJO (market research, weak).
- Fewer companies are replacing core platforms. CRM replacement fell from 22.1% to 9.7% in 2025 (MarTech Replacement Survey, weak).
- Even a client with no CEP already works inside an SMS aggregator portal, a WhatsApp provider dashboard or Zoho. A replacement asks them to leave it.
- We have no reference customer yet. Nothing is deployed and all validation is synthetic (README; market research). A pre-revenue vendor asking to replace a client's engagement stack makes the largest trust request possible.

**2. Running channels is a permanent job, and it is the job CEPs charge for.**
- Email: Gmail and Yahoo require a spam rate below 0.3% and one-click unsubscribe honoured within 2 days. SES reviews an account at 5% bounces or 0.1% complaints. Dedicated IPs take up to about 45 days to warm up.
- SMS: India DLT registration of the entity, header and template, plus P/S/T message categories.
- WhatsApp: template approval, quality ratings and per-message fees. Marketing messages cost about INR 0.78–0.88 each (weak).
- Push and in-app need an SDK inside the client's app, and the client has to ship an app release.
- Win-back sends to lapsed customers, which is the worst case for bounces and complaints.
- Formally the client owns sender reputation. In practice the Minfy team would.

**3. The open-source parts do not fit.**
- Every credible open-source CRM is a B2B sales tool: Twenty, SuiteCRM, EspoCRM, Frappe, Krayin and Oro.
- Most are AGPL, GPL or OSL with paid enterprise folders. RudderStack and Airbyte (ELv2) and n8n (Sustainable Use License) forbid resale. erxes has an anti-SaaS clause and Countly a branding lock.
- Odoo's journeys, WhatsApp and AI features are Enterprise-only.
- Mautic strains at 1–2M contacts (forum reports, weak) and published critical advisories in Dec 2025 and May 2026. SuiteCRM published 10 advisories in Jul–Aug 2026.
- Dittofeed is the only permissive fit, but it is pre-1.0 (v0.24.0-alpha) from a tiny vendor. Every client would get its own ClickHouse, Temporal and MinIO or Kafka. The compose file uses `temporalio/auto-setup`, an image Temporal says not to use in production (engineering judge, verified).
- Self-hosting this kind of stack defeated even its own vendors. PostHog dropped self-host support because issues "crop up in every part of the stack". Laudspeaker is unmaintained and Papercups is in maintenance mode.

**4. The competition and the business case work against it.**
- A replacement makes MoEngage, CleverTap, Netcore and Braze competitors. Those platforms already have push and in-app SDKs, which we do not.
- We would lose their marketplaces and referrals, and the proven exit path. Layers get bought: OfferFit by Braze for about $325M, and Aampe by MoEngage in June 2026.
- The best-fit buyers for a replacement pay the least. CleverTap Essentials is about USD 75 a month and MoEngage mid-market is INR 2.5–15 lakh a year (weak).
- The replacement option estimated its own build at 3–5 times the layer: 36–50 engineer-weeks after Phase 0, plus a permanent operations team.
- A small team can write the code, but running channels also means on-call duty, SES review appeals and DLT rejections, every week, for every client.

**5. Its timed opening has closed.** Amazon Pinpoint's engagement features end on 30 Oct 2026, which is 24 days from today. A replacement would not be sellable until about Q2 2027. Pinpoint users will have moved to Amazon Connect, Pushwoosh or OneSignal before then.

**6. It undermines our main selling point.** Our claim is independent proof. If we send the messages and also grade them, a CFO can say "you marked your own homework", which is the exact criticism we aim at other vendors.

**What the replacement option gets right, and we keep:**
- When we send, the send log is exact.
- Holdout enforcement at the moment of sending is possible.
- An outcome-based fee becomes settleable.
- Data residency in the client's account is a strong story.

The starter activation keeps these benefits for the few clients who need them, without becoming a CEP.

## How the recommended model works for a client

**A. A company that already has a CEP or CRM** (MoEngage, CleverTap, Netcore, WebEngage, Braze, Salesforce Marketing Cloud, Klaviyo, HubSpot, Zoho). This is the default path and most of the market.
1. **Weeks 0–3: paid Data Readiness Assessment.**
   - Deploy into the client's AWS account and connect to their warehouse read-only.
   - Run Guided setup and Data Doctor.
   - In parallel, run a hand-off dry run: load a synthetic Activation Contract into their real tool and time it.
   - Agree in writing whose holdout is the source of truth. CleverTap's System Control Group and Braze's Global Control Group can conflict with ours.
   - Name the send-log export in the contract (Braze Currents, MoEngage S3 Exports, CleverTap Event Exports).
2. **Weeks 3–6:** a second person approves the champion model. We score with a persistent holdout and write the Activation Contract to the client's S3 or Redshift. Their tool takes it in natively. The campaign manager filters `mai_treat_flag = 1`, excludes `mai_holdout_flag = 1`, and sends from the tool they already know.
3. **Weeks 6–8:** we read their send log back and publish a reconciliation report. Targets: under 5% of holdout IDs contacted, and at least 70% of the treat list contacted (market research, checkpoint 1).
4. **Weeks 10–14:** the Value Proof Pack. It shows incremental results and net INR with a 95% range, credit as the CEP reports it next to measured credit, and money not spent on "sure things" and sleeping dogs.
5. **Expand:** more lifecycle use cases on the same contract, then audit mode, then an outcome-based fee at renewal.

**B. A company with only a basic CRM, a messaging provider or an email tool** (Zoho, an SMS aggregator, a WhatsApp provider such as AiSensy or Gupshup, SES or SendGrid). The path is the same as A, using the tool they have:
- a file or API recipe for their CRM or provider, or
- a signed "list ready" webhook their own Zapier or n8n can act on.

If none of these can take in a list, this client counts toward the starter gate. Once built, the starter sends one approved batch through **their own** provider account or AWS account. This is still "use what you already have".

**C. A company with nothing.** These clients are not a product target today.
- Before the starter exists: we route them to a CEP partner, a messaging provider, or a Minfy services contract. That contract is an unmodified Dittofeed in their account, or Amazon Connect once it is confirmed in Mumbai. We did not find it listed for Mumbai.
- After the gate passes: the starter, if they accept being the legal sender. That means their own domain, DLT entity and WhatsApp Business Account.
- They start registrations on day 0. SES production access, DLT and WhatsApp approvals each take weeks (background knowledge, not verified).
- When they later buy a CEP, they move to path A. The product they pay for does not change.

## Exactly where the product boundary is

| Area | We build (product) | We integrate with | We never build |
|---|---|---|---|
| Data | Read-only connections, Guided setup, Data Doctor, one row per customer | Client's warehouse or CDP; reverse ETL (Hightouch, Fivetran, RudderStack) | A CDP, identity resolution, a reverse-ETL engine |
| Decide | AutoML, uplift (S/T/X), sleeping-dog suppression, value under a budget, approval, explanations | The client's LLM for copy and summaries | Real-time decisioning engine (for now) |
| Hand-off | Activation Contract, write-scoped destination, per-tool profiles, HMAC webhook, MCP server (later) | CEP ingestion: Braze CDI, MoEngage S3, CleverTap Custom List, Netcore S3, SFMC SFTP | Per-client custom ETL as a product |
| Prove | Persistent holdout, send-log importer, reconciliation, Value Proof Pack, audit mode | CEP send-log exports | Self-graded attribution without a random control |
| Starter (gated) | One-shot sender: SES, End User Messaging SMS/WhatsApp, provider adapter; SendPlan with second approver | Client's AWS account, DLT entity, WhatsApp Business Account, provider | Waits, branches, triggers, follow-up sequences |
| Engagement | – | Client's CEP for journeys, push, in-app, frequency caps | Journey builder, inbox, template/landing-page designer, push SDK |
| Records | Read-only customer view (band, reason, history) inside our app | Client's CRM as the system of record | A CRM, contact management UI |
| Infrastructure | CDK stacks in client's account | AWS services on client's bill | Mail server, SMS gateway, push server, shared IP pool, unofficial WhatsApp APIs |

## Integration strategy

**The Activation Contract.** This is the one output every tool reads. It extends `scores_csv_columns` in `engine/contracts.py`.

- **Manifest:** `schema_version`, `run_id`, `use_case_id`, `model_version_id`, `consent_snapshot_id`, `valid_until`.
- **Row fields:** customer token and channel IDs, band, uplift segment, reason, action, `treat_flag`, `holdout_flag`, `consent_status`, `suppress_reason`, `UPDATED_AT`, and a `PAYLOAD` JSON column.
- **Rules:**
  - About 10 business fields.
  - Changed rows only.
  - Never raw model features.
  - The holdout is exported as a suppression attribute, not just left out.

This fits Netcore's 20-attribute cap and Braze CDI's limit of 250 attributes and 1 MB per row. It is written to one S3 prefix or one database schema through a new write-scoped destination. Source connections stay read-only.

**Why this first.** One table, written into the client's own warehouse, is read natively by about 8 of the 15 platforms researched: Braze CDI, CleverTap warehouse import, MoEngage S3 import, Klaviyo, Iterable Smart Ingest, Salesforce Data Cloud Zero Copy and Adobe Federated Audience Composition. We need no per-platform API code, and we hold no CEP credentials.

Three caveats:
- MoEngage's warehouse import does not list Redshift, so it uses S3 files.
- Klaviyo, Salesforce and Adobe need paid tiers on the client's side.
- No WebEngage file import was confirmed.

**Connector order.** No market-share data for Indian CEPs was found, so this order is a judgement.
1. A file drop to S3 or SFTP, plus the send-log importer.
2. Whatever CEP the first pilot runs.
3. MoEngage and CleverTap, the most common in Indian B2C.
4. Braze, then Salesforce Marketing Cloud (SFTP into a Data Extension, plus Tracking Extract).
5. Netcore for BFSI and e-commerce. Klaviyo for global D2C.
6. HubSpot, Zoho, Iterable, Adobe, Dynamics, Freshworks, Mautic and Odoo only when a paying client needs them.

**How outcomes come back.**
- A `SendLogSource` protocol maps each vendor's export into one schema: `customer_id`, `campaign_id`, `channel`, `event`, `ts`. Sources: Braze Currents, MoEngage S3 Exports, CleverTap Event Exports, Netcore Egress, WebEngage S3, the SFMC Tracking Extract.
- It follows the pattern in `engine/scheduling/outcomes.py`, which already refuses outcome windows that have not matured.
- The measurement code gains contamination, contact rate and complier-adjusted lift.
- Fallback: the CEP's own campaign-report exports.
- Business outcomes (renewal, purchase, repayment) still come from the client's warehouse.

**Listings and distribution.**
- AWS Marketplace INR private offer first. It has been open to India sellers since Nov 2025, at a 3% fee under $1M. Pair it with ACE co-sell.
- CEP marketplaces only after real installs: HubSpot needs 3, Klaviyo 5. MoEngage, CleverTap and Braze Alloys are review-based.
- AppExchange only after at least 2 Salesforce clients. It costs $999 for review plus a 15% revenue share.

**Phases** (effort estimates are ours, not measured):

| Phase | When | What | Effort |
|---|---|---|---|
| 0 | Weeks 0–3 | First real AWS deployment; hand-off dry run on pilot's tool; holdout agreement; send-log clause; survey every prospect's stack (CEP? provider? DLT? WhatsApp Business Account?) | ~1 engineer + Minfy team |
| 1 | Weeks 2–10 | Activation Contract v1 (S3, Redshift/Postgres, SFTP); persistent holdout (replace the per-run salt `seed_from(run_id)` in `engine/stages/actions.py`); send-log importer; reconciliation; HMAC webhook; `auth_mode=local` with separation of duties tested | 10–14 engineer-weeks |
| 2 | Months 3–5 | Pilot's CEP recipe + one of MoEngage, CleverTap, Braze; AWS FTR and Marketplace private offer; audit mode. **Sellable layer v1.** | 3–6 engineer-weeks per CEP |
| 3 | Months 4–7, gated | Starter activation (below) | 1–2 engineers, 2–3 months |
| 4 | Months 5–18 | Salesforce, Netcore, MCP server, marketplace listings, services kit | On demand |

Run alongside these phases: the test planner (N4), value weighting (N7) and the Value Proof Pack (N8).

## If we offer starter activation

**The gate. Build nothing until it passes.** At least 2 of the first 5 qualified prospects must meet all three conditions:
- (a) they have no CEP or CRM that can take in a list;
- (b) they will pay for activation as an add-on;
- (c) they accept being the legal sender, with their own domain, DLT entity and WhatsApp Business Account.

If the gate fails, we ship the pure layer.

**Scope.**
- Channels: email, SMS and WhatsApp.
- Send method: one approved batch per cycle.
- Content: pre-approved templates only.
- Our generated copy may only fill the template's placeholders.

**Components.**
- `SesSender` uses `SendBulkEmail`. SES subscription management handles one-click List-Unsubscribe (verified).
- `EumSmsSender` sends through ap-south-1 or ap-south-2 with the DLT Entity and Template IDs.
- `EumWhatsAppSender` uses End User Messaging Social.
- `BspSender` is a generic HTTP adapter for the client's own provider. In India this is often the cheapest path.
- A `SendPlan` covers the list hash, channel, template, quiet hours, an INR cost estimate split by WhatsApp category, a seed test send, a kill switch, and a second approver. It requires `auth_mode=local`; the default today is `off`.
- Events go to the client's S3, and opt-outs go into the existing consent ledger (`engine/privacy/consent.py`).
- The send IAM role lives in a separate deployment stack that is off by default. A CIO can install the layer with zero send permission.

**One correction is needed first.** Indian DLT sender IDs are one-way, so customers cannot reply STOP (AWS docs, verified).
- The SMS footer "Reply STOP to opt out" in `engine/config.py` (around line 995) is wrong for India.
- SMS needs an opt-out link: either a small public endpoint or the client's existing preference page.
- This work is not yet costed.

**Licences.**
- The product calls only AWS APIs through boto3 (Apache-2.0) and vendor REST APIs. No copyleft code is embedded.
- Dittofeed (MIT) or Mautic (GPL-3.0) run only in services work, unmodified, as separate programs.

**The rule that keeps it from becoming a CRM.** The product sends a message only when all five conditions hold:
1. **Approved list:** from an approved champion, with the holdout drawn before the send and a send plan approved by a second person.
2. **Pre-approved content:** the template is already approved elsewhere, as a DLT SMS template, a Meta WhatsApp template, or an email template with unsubscribe.
3. **One shot, fixed end:** no waits, branches, triggers, real-time sends or follow-ups.
4. **The client's rails and identity:** the client's account or provider key, domain, DLT entity and bill.
5. **Closed loop:** every event lands in the send log, and every opt-out is in the consent ledger before the next list is built.

This rule goes into a decision record. Code review rejects any code that adds waits, branches or triggers. Journey requests go to a services contract or a CEP partner.

**When we stop.** We freeze the starter if any of these happens:
- SES review is triggered on more than 1 client.
- Any client receives a DLT violation notice.
- Sender operations take more than 0.5 person per client per month.
- After 2 quarters, starter clients bring in under 20% of contract value but cause over 40% of the support load.

## Scores from the judges, and what would change the decision

| Option | Buyer and adoption | Engineering feasibility | Business and defensibility | Average |
|---|---|---|---|---|
| R: Full replacement (engine + Dittofeed + AWS channels) | 3 | 3 | 3 | 3.0 |
| L: Pure layer / plugin | 7 | 7 | 7 | 7.0 |
| **H: Layer + gated starter, journeys as services** | **8** | **8** | **8** | **8.0** |

All three judges said the gap between H and L is small. Until the gate passes, H *is* L. They also flagged two claims in H as overstated:
- A client whose IT cannot approve a CEP file import within 3 weeks will not approve send permissions, DLT linkage and WhatsApp templates any faster.
- Buyers will still compare any activation price with provider fees of INR 1,500–4,500 a month.

**Conditions that would change the decision:**
- **Move to pure L (drop the starter):**
  - the first 3 CEP pilots all ingest the contract and supply send logs within 3 weeks; or
  - 2 of the first 5 CFOs discount a readout because "you sent it yourselves"; or
  - a top Indian CEP refuses a partnership because we can send; or
  - Amazon Connect outbound campaigns are confirmed in Mumbai.
- **Move toward R (as a separately staffed later product line, never a bundled CRM):** 3 or more of the first 5 prospects lack any CEP, commit in writing to retire their current tools at CEP-level budgets, and Minfy funds a real operations team first.
- **Re-examine the whole position:**
  - contamination stays above 5%, or contact rate below 70%, on 2 consecutive CEP clients after one corrective cycle; or
  - 3 of 5 CFOs say their CEP's control group is good enough; or
  - uplift fails to beat risk targeting at equal budget; or
  - MoEngage, CleverTap or Netcore ships uplift with interval-based net value in India before our first case study.
- **Strengthen the starter:**
  - 3 of 5 prospects cannot obtain a send-log export; or
  - the packaging test (E5) shows buyers only pay when activation is included.

The biggest unknown is what share of Indian mid-market B2C companies have no CEP. No data was found. Phase 0's stack survey exists to measure it.

## What we tell a buyer, and what we stop saying

**What we say:** "Keep your CRM and engagement tool. Marketing AI decides who to contact and who to leave alone, puts the approved list into the tool you already use, and proves what it earned in rupees against a random control group, all inside your own AWS account."

**What we stop saying:**
- "Complete replacement", "all-in-one platform", or "replace your CRM or CEP".
- "Open-source CRM included."
- "We send your campaigns". The starter is never the lead pitch and never appears in CEP-partner materials.
- "Pinpoint successor". The window has closed.
- "Telecom churn product". We say customer lifecycle for any B2C business.
- "Nobody measures incrementality". Braze and CleverTap ship control groups. We say "independent, uplift-targeted, net value with a 95% range".
- Any result figure not measured on a real client. The demo's 12.7-point effect is planted.