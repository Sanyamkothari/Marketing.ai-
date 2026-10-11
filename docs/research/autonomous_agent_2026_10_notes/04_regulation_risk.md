# 04 - Regulation and risk when a marketing agent SENDS autonomously

Research date: 11 October 2026. Analyst view, India first, global second.

**Not legal advice.** This is a regulatory-risk scan for product design. It is not a legal opinion. Before any client goes live with autonomous sending, Indian telecom/data-protection counsel (and EU/US counsel for those markets) must review the final design and contracts.

**How this was researched, and how far to trust it**
- WebFetch and direct HTTPS to official sites were **blocked** in this environment. DNS failed for trai.gov.in and support.google.com, and the egress proxy refused eur-lex.europa.eu and ftc.gov with a 403. So **no primary text was read directly**. Every finding comes from WebSearch result summaries. Where a search surfaced a primary URL (TRAI Gazette PDF, PIB, ASCI PDF, Meta developer docs), it is cited, but its wording was seen only as a search summary.
- The shared web-search budget (200 per turn) ran out before three planned searches: TCCCPR opt-out timing, DPDP Rule 10 (children), and DPDP withdrawal timing. Those points rely on the companion notes or on memory and are marked **[memory, unverified]**.
- Confidence labels: **High** = several independent sources agree, or a primary URL surfaced with matching summary text. **Medium** = one good secondary source, or sources that conflict on details. **Low** = a single vendor or blog source, or an inference.
- This file does not repeat `docs/research/agentic_ai_2026_10_notes/india_regulation_and_market.md` (DPDP timeline, DCA background, AA model, market context). It goes deeper on **autonomy** and on **being the sender**.

---

## 0. Bottom line (what actually changes when the agent presses "send")

1. **Autonomy does not change who the law names as the sender. It changes how much proof you need.** In India the brand stays the TCCCPR *Principal Entity* (PE) and the DPDP *Data Fiduciary*. In the US the advertiser stays the CAN-SPAM "sender". The main new fact is that **no human approved the specific message**. Regulators and platforms will ask the PE to show *ex ante* controls and *ex post* evidence for each send: consent valid at send time, a registered template, a whitelisted URL, quiet hours respected, frequency caps respected. Under DPDP s.6(10) the fiduciary already carries the burden of proving consent. An autonomous sender has to produce that proof per message, by machine, at the moment of sending. *(High, building on the companion notes.)*
2. **Our legal role changes sharply between "agent sends via the client's tools" and "we send ourselves".** Via the client's DLT header, WhatsApp Business Account (WABA) and email domain, we are most likely the client's DPDP processor and a technology vendor. If we send through our own accounts, we become a **registered telemarketer in the DLT chain** (TM-AF/TM-DF), the **WABA owner** answerable to Meta, the **sending domain** whose reputation Gmail, Yahoo and Microsoft score, and a CAN-SPAM co-sender. We would also own the complaint rates that trigger suspensions. *(Medium; role mapping is inference built on cited rules.)*
3. **In India, SMS and WhatsApp autonomy can only be "autonomous selection", not "autonomous authorship".** SMS content must match a pre-registered DLT template with tagged variables and whitelisted URLs. WhatsApp marketing messages must use a Meta-approved template. So the agent can decide who, when, which template and which variable values. New copy needs a human (or PE) registration step. Email is the only channel where the agent can write freely. *(High.)*
4. **Brand-level penalties can follow from one bad autonomous run.** Under TCCCPR (Feb 2025), action starts at **5 complaints in 10 days**. A first violation bars the sender's outgoing telecom resources for **15 days**. Repeat violations mean disconnection across all operators for **1 year** plus blacklisting. On WhatsApp, a template is paused for 3h, then 6h, then **disabled**, and quality ratings fall. On Gmail and Yahoo, a spam rate of 0.3% or more leads to rejection. An autonomous loop that over-sends can therefore cut off the client's *transactional* reach as well. *(High for the numbers; Medium on whether TRAI's resource bar hits registered PEs or mainly unregistered senders.)*
5. **New 2026 rules increase the burden:**
   - TCCCPR Third Amendment, notified **18 Sep 2026**: senders must declare A2P (application-to-person, i.e. automated) calls in advance; AI/ML-based suspected-UCC detection; a ₹0.05/min A2P termination charge.
   - RBI sales-practice ("responsible business conduct") directions, reported final June 2026, effective **1 Jan 2027**: explicit consent before a sale, a ban on dark patterns, refunds for mis-selling.
   - EU AI Act **Art. 50** chatbot disclosure, live since **2 Aug 2026**.
   - Meta's ban on general-purpose AI providers on the WhatsApp Business API since **15 Jan 2026**.
   - ASCI draft AI-labelling guidelines (May 2026).

---

## 1. Who is legally the sender? Three operating modes

Definitions used in this file:
- **Mode S (Suggest):** the agent proposes, and a human at the client approves each campaign or send. This is today's model.
- **Mode C (Client rails):** the agent sends autonomously through the *client's* registered rails: the client's DLT entity/headers/templates, the client's WABA and phone numbers, and the client's ESP account and domain. We operate under the client's credentials and instructions.
- **Mode O (Own rails):** the agent sends through *our* accounts: our DLT telemarketer registration and aggregator, our WABA or BSP account, our ESP sending domain or IP pool, possibly our own headers.

### 1.1 India - TCCCPR / DLT
- **PE (Principal Entity)** = the business on whose behalf commercial communication is sent. The PE registers **headers** (sender IDs) and **content templates** on the DLT (distributed ledger) platform. Every SMS is scrubbed against the PE's registered template and blocked if it fails. *(High)* - [Tribune: TRAI cracks down on misuse of headers/templates](https://www.tribuneindia.com/news/nation/trai-cracks-down-on-unauthorised-telemarketers-directs-telcos-to-stop-misuse-of-headers-message-templates-480421); [Business Standard 2023](https://www.business-standard.com/india-news/complete-verification-of-headers-to-combat-pesky-texts-says-trai-123052500493_1.html)
- **Telemarketers** register on DLT for the aggregator function (TM-AF) or delivery function (TM-DF). A PE must declare its full **PE-to-TM chain** (up to three parties including delivery). Every TM in the chain must be DLT-registered. Messages with an incomplete chain are rejected (traceability applied from 1 Nov 2024 per vendor notices). *(Medium-High)* - [AWS End User Messaging: India sender ID registration](https://docs.aws.amazon.com/sms-voice/latest/userguide/registrations-sms-senderid-india-register.html); [Zoho: register your telemarketer chain on DLT](https://help.zoho.com/portal/en/community/topic/important-compliance-update-ensure-sms-delivery-chain-registration?page=11)
- **Consequence for us:**
  - In Mode C we are not in the DLT chain if we call the client's existing CPaaS (Communications Platform as a Service) through its API. We are a software actor behind the PE.
  - In Mode O, if our infrastructure aggregates or delivers SMS, **we must be a registered telemarketer**. We then sit in every client's chain and are exposed to TRAI's telemarketer sanctions: blacklisting for header/template misuse, which the Third Amendment strengthens. *(Inference from cited rules; Medium)*
- Look-alike headers across PEs are prohibited, so we cannot register a generic "our brand" header to send for many clients. *(Medium)* - [Tribune](https://www.tribuneindia.com/news/nation/trai-cracks-down-on-unauthorised-telemarketers-directs-telcos-to-stop-misuse-of-headers-message-templates-480421)

### 1.2 India - DPDP Act 2023
- **s.8(1):** the Data Fiduciary is responsible for processing done by a processor on its behalf, *irrespective of any agreement to the contrary*. **s.8(2):** a processor may be engaged only under a **valid contract**. *(High; statutory text as reproduced by secondary sites)* - [DPDP Wiki s.8](https://dpdp.myndsolution.com/wiki/act/section-8/); [King Stubb & Kasiva on vendors](https://ksandk.com/corporate/how-should-companies-handle-data-shared-with-vendors-under-the-dpdp-act/); [Mondaq](https://www.mondaq.com/how-should-companies-handle-data-shared-with-vendors-under-the-dpdp-act/1846690)
- Commentators list marketing agencies as typical processors. **An agency that uses client data for its own purposes may become a fiduciary for that use.** *(Medium)* - [treelife](https://treelife.in/?p=15984)
- **Autonomy risk (inference, Low-Medium):** a fiduciary "determines the purpose and means". If our agent decides *whom* to contact, *for what offer*, and *when*, within a broad brief ("grow repeat purchase"), a regulator could see us as co-determining the means, or even the purpose. That points toward joint-fiduciary exposure. Two mitigations:
  - Keep the client's documented purposes and guardrails as the controlling instruction, with the agent choosing only inside them.
  - Never reuse one client's data or model learnings for another client (cross-client training is a "purpose" of our own).
- DPDP has **no GDPR-Art.-22-style right** against automated decisions. The Act defines "automated" as "any digital process capable of operating automatically in response to instructions given or otherwise for the purpose of processing data". Profiling of adults is therefore governed by notice, consent and purpose limitation, not by a special automated-decision regime. *(Medium: definition High; "no ADM right" from memory plus absence in search results)* - [dpdpa.com s.2](https://www.dpdpa.com/dpdpa2023/chapter-1/section2.html)
- **Children:** s.9(3) bars tracking, behavioural monitoring and targeted advertising directed at children (under 18). An autonomous targeting agent must hard-exclude known or likely minors. *(High, from companion notes; Rule 10 detail not re-verified here: search budget exhausted)*

### 1.3 Global analogues
- **CAN-SPAM (US):** both the advertiser and the party that sends can be liable. Hiring an agency does not remove the advertiser's liability. Penalty is up to **US$53,088 per violating email** (Jan 2025 inflation adjustment). Opt-outs must be honoured within **10 business days**, and the opt-out mechanism must work for **30 days** after the send. *(Medium; vendor summaries of the FTC guide, FTC page not reached)* - [Prospeo: CAN-SPAM penalties 2026](https://prospeo.io/s/can-spam-penalties-per-email); [Transcend CAN-SPAM guide](https://transcend.io/blog/can-spam-act)
- **TCPA (US):** an AI-generated voice is an "artificial or prerecorded voice" (FCC Declaratory Ruling, Feb 2024). It needs prior express consent (written, for telemarketing), caller identification and an opt-out. *(High)* - [Hunton](https://huntonak.com/privacy-and-information-security-law/fcc-issues-declaratory-ruling-that-tcpa-applies-to-ai-generated-voice-calls); [Orrick](https://www.orrick.com/en/Insights/2024/02/Did-the-FCC-Really-Ban-AI-Generated-Voice-Calls-No-But-Restrictions-Apply)
- **GDPR:** the controller/processor split works like DPDP's. SCHUFA (CJEU C-634/21, 7 Dec 2023) held that the party producing a score can itself be making an Art. 22 "decision" if a third party's action "draws strongly" on it. So a vendor's automated output can carry Art. 22 duties even if the client formally "decides". *(High)* - [A&O Shearman](https://www.aoshearman.com/en/insights/ao-shearman-on-data/cjeu-rules-that-a-credit-score-constitutes-automated-decision-making-under-the-gdpr); [IAPP](https://iapp.org/news/a/key-takeaways-from-the-cjeus-recent-automated-decision-making-rulings)
- **EU AI Act:** the Art. 50(1) chatbot-disclosure duty sits with the *provider*. Commentators note that rebranding or white-labelling a chatbot under your own name can turn a deployer into a provider (Art. 25). *(Medium)* - [Baker Botts, Sep 2026](https://www.bakerbotts.com/thought-leadership/publications/2026/september/eu-ai-act-article-50-transparency-obligations-go-live); [blckalpaca: Art 50 for agents](https://blckalpaca.at/en/knowledge-base/ai-agents/eu-ai-act-for-ai-agents/art-50-fuer-chatbots-und-agents.md)

---

## 2. India - deeper on the rules that bite an autonomous sender

### 2.1 TRAI TCCCPR 2018 as amended

**Second Amendment (12 Feb 2025)** *(High: TRAI PR 11/2025 surfaced; several secondary sources agree)*
- **Complaint trigger:** action against a sender moves from 10 complaints in 7 days to **5 complaints in 10 days**.
- **Sanctions:**
  - First violation: **outgoing services of all the sender's telecom resources barred for 15 days**.
  - Repeat violations: **all resources, including PRI/SIP trunks, disconnected across all access providers for 1 year**, plus blacklisting.
- **Consumer complaint window:** extended from 3 to **7 days**. Unregistered senders can be reported without a preference registration. Operators must act on complaints about unregistered senders within **5 days** (previously 30).
- **Re-consent cooldown:** a sender **may not seek consent again for 90 days after a customer opts out**.
- **Commencement:** 30 days after the Gazette, with some regulations at 60 days. Which group the penalty clauses fall in was not confirmed.
- Sources: [TRAI PR 11 of 2025](https://cms.trai.gov.in/sites/default/files/2025-02/PR_No.11of2025.pdf); [Fonearena](https://www.fonearena.com/blog/446114/trai-tcccpr-2025-tougher-rules-spam-calls-sms.html); [Storyboard18](https://cms.storyboard18.com/?p=56616); [JSA Prism Feb 2025](https://www.jsalaw.com/wp-content/uploads/2025/02/JSA-Prism-Telecommunications_February-2025_TCCCPR-amendments.Final_.pdf)
- **Scale of enforcement:** about 1,150 entities blacklisted and 18.8 lakh numbers disconnected after the amendments (Mar 2025). *(Medium)* - [Medianama](https://www.medianama.com/2025/03/223-1150-entities-blacklisted-18-8-lakh-numbers-disconnected-after-spam-regulation-amendments/)
- **Template category change:** new "Service Explicit" template registrations were discontinued, and existing ones migrated to **Promotional from 10 Mar 2026**. Messages an agent thinks of as "service nudges" may now be promotional, with promotional consent, DND and time-band rules. *(Low-Medium, single vendor)* - [Fast2SMS](https://www.fast2sms.com/help/?p=16132)
- **Uncertainty:** whether the 15-day bar applies in practice to registered PEs' headers or mainly to senders abusing 10-digit numbers was **not confirmed**. Treat it as a live risk to all of the brand's commercial traffic until counsel confirms.

**Third Amendment Regulations 2026 (notified 18 Sep 2026)** *(High that it was notified: PIB release plus Gazette PDF URL surfaced; Medium on clause detail)*
- Timeline: draft 13 Mar 2026; comments to 19 Apr; counter-comments to 4 May; Open House Discussion 3 Jun; Gazette 18 Sep 2026.
- **Commencement:** 30 days after Gazette publication (**about 18 Oct 2026**, computed). **Regulations 4, 6, 8(a) and 18 start after 60 days (about 17 Nov 2026**, computed). Regulation 4 is the A2P declaration.
- **A2P calls:** every sender must **declare A2P calls in advance** (robocalls, pre-recorded and automated calls) to the originating operator, with CLI ranges, and the declaration is recorded on DLT. **Undeclared A2P calls are treated as UCC.** An AI voice agent making outbound calls is squarely an A2P call.
- **Detection:** AI/ML-based detection of suspected high-probability UCC callers, with KYC re-verification and suspension or disconnection for repeat violations.
- **Templates:** stricter action for header and template misuse, including blacklisting of telemarketers.
- **Complaints:** complaint history is recorded, and an appeal mechanism against complaint outcomes is added.
- **Termination charge:** up to **₹0.05/min** on A2P calls between terminating and originating operators, subject to exemptions. TeamLease says this was retained from the draft; one law-firm piece calls it unverified.
- Sources: [PIB release](https://www.pib.gov.in/PressReleseDetailm.aspx?PRID=2311989&lang=1&reg=3); [Gazette PDF (TRAI, 18 Sep 2026)](https://www.trai.gov.in/sites/default/files/2026-09/Regulation_18092026.pdf); [TRAI PR 119/2026](https://www.trai.gov.in/sites/default/files/2026-09/PR_No119of2026.pdf); [TeamLease RegTech summary](https://teamleaseregtech.com/updates/article/59840/telecom-commercial-communications-customer-preference-third-amendment-/); [AA Plus Consultants analysis](https://www.aaplusconsultants.com/article-tcccpr-third-amendment.html); [ZeeBiz](https://www.zeebiz.com/india/news-trai-tightens-spam-call-rules-new-norms-for-ai-automated-and-robocalls-402451)
- **Design implication:** any voice-agent feature needs a **pre-declared CLI range per client** before about 17 Nov 2026. It must route promotional calls to the **140 series** and service calls to **1600** (BFSI). It must also expect a per-minute termination cost in unit economics.

**DLT content templates, variables and URLs** (background in companion notes; autonomy angle here)
- The template must match. Variables are pre-tagged; per the Nov 2025 direction, untagged or mismatched variables are rejected after a 60-day logging window. URLs, APKs and callback numbers must be whitelisted (since 1 Oct 2024). Templates with more than 3 variables need justification. *(Medium-High, companion notes)*
- **Autonomy angle:** an LLM that "writes" an SMS will fail scrubbing. The agent must (a) select from registered template IDs, (b) fill only tagged variables with values of the declared type, and (c) use only whitelisted links. Registering a new template is a PE action on the DLT portal. In Mode C that stays with the client. In Mode O we could do it as a TM on the PE's behalf, but that makes us answerable for template misuse.

**Consent acquisition (DCA) and "consent templates"**
- Consent-seeking messages go out via the **127xxx** short code. They must state the purpose, the scope and the PE/brand name, and may contain only whitelisted URLs and callbacks. Consents gathered by other means are void once DCA applies to the sector. Users can block all consent-seeking messages. *(Medium-High)* - [TRAI PR 50/2023](https://cms.trai.gov.in/sites/default/files/2024-08/PR_No.50of2023.pdf); [Telecom Review Asia](https://telecomreviewasia.com/news/policy-news/3333-trai-instructs-telecom-operators-to-establish-digital-consent-acquisition-process/)
- **Gap:** the DLT "consent template" registration workflow (fields, approval steps) was **not found**. Verify with the DLT operators' (Jio/Airtel/Vi/BSNL) PE manuals.
- **Autonomy angle:** an agent must **never autonomously send consent-seeking messages** outside DCA. It must also respect the **90-day cooldown** after an opt-out. Re-permissioning journeys are a distinct, human-approved flow.

**Time band**
- Promotional calls and SMS are allowed only **09:00 to 21:00 IST**. Some vendor pages say 10:00. **Medium; verify against the regulation text.** - [Exotel calling-hours FAQ](https://developer.exotel.com/docs/faqs/calling-hours); [MessageCentral India SMS guidelines](https://www.messagecentral.com/en-in/sms-guideline/india)
- BFSI **recovery** calls only 08:00 to 19:00 (RBI). This applies to recovery agents, not to marketing as such. *(Medium)* - [Deccan Herald](https://www.deccanherald.com/business/rbi-directs-loan-recovery-agents-not-to-intimidate-borrowers-no-calling-before-8am-after-7pm-1135618.html)

**Telecommunications Act 2023, s.28**
- Defines "specified message" (any offer or promotion). It empowers rules that require prior consent and Do-Not-Disturb registers, and creates an online grievance mechanism. A press-reported penalty of up to ₹2 lakh is unverified. TCCCPR is the operative instrument for now. *(Medium)* - [lawgratis s.28 text](https://lawgratis.com/bare-acts/the-telecommunications-act-2023?sequence=8); [Onmanorama](https://www.onmanorama.com/news/india/2024/06/23/new-telecom-act-effective-from-june26-nine-sim-per-person.amp.html)

**Opt-out handling SLA:** the TCCCPR time limit for honouring revocation or opt-out was **not verified** (search budget exhausted). **[memory, unverified]**: revocation via the operator's 1909/app is to take effect within a short fixed window, recalled as 24 hours, and every promotional message is expected to carry an opt-out route under the 2025 changes. **Design to "immediate suppression" regardless.**

### 2.2 DPDP Act 2023 and Rules 2025 - autonomy-specific points

Timeline is in the companion notes:
- Rules notified 13 Nov 2025.
- Consent-manager Rule 4 from **13 Nov 2026**.
- Notice, consent, security, breach, rights, children and SDF duties from **13 May 2027**.
- No compression notification found as of Oct 2026. *(Medium-High)* - [MirvoLegal timeline (Sept 2026)](https://www.mirvolegal.com/insights/dpdp-rules-2025-compliance-timeline/); [ConsentOS timeline](https://consentos.in/learn/dpdp-compliance-timeline/); [Cyberaube: CM deadline](https://cyberaube.com/blog/dpdp-consent-manager-deadline-november-2026-data-fiduciary-playbook)

What autonomy adds:
- **Per-send proof of consent (s.6(10)).** Each autonomous send must be traceable to a specific, purpose-tagged, unexpired, unwithdrawn consent. That means a consent ID, the notice version, the channel and the timestamp, captured *at decision time*.
- **Withdrawal (s.6(4), (6)).** Withdrawal must be as easy as giving consent, and processing must stop within a reasonable time. An agent with queued or scheduled sends must re-check consent **at dispatch**, not only at planning.
- **Consent managers (from 13 Nov 2026).** If a client's customers manage consent through a registered consent manager, the agent must accept consent and withdrawal events from that consent manager. This is an interoperability requirement. Standards had not been published at the last check.
- **Purpose limitation.** "Retention offer via WhatsApp" is not the same purpose as "order updates". An agent that swaps channel or purpose on its own to "find a way to reach" a customer is a classic violation pattern.
- **No "legitimate interest" basis.** There is no soft opt-in equivalent in India, unlike EU ePrivacy Art. 13(2).
- **Processor contract (s.8(2)).** Mode C and Mode O both need a DPDP-compliant contract with each client (Section 4).

### 2.3 BFSI overlays (RBI / SEBI / IRDAI)

| Regulator | Instrument (date) | Why it matters to an autonomous sender | Confidence |
|---|---|---|---|
| RBI | Sales-practice / responsible-business-conduct directions: draft 11 Feb 2026; final reported 16 Jun 2026, **effective 1 Jan 2027** | Explicit consent before a product is sold; **dark patterns and forced bundling banned**; refund where mis-selling is established; customer feedback within 30 days of sale; UI/UX standards and audits of digital journeys (draft). An agent optimising conversion with urgency or scarcity copy, or auto-bundling, is exactly the target. | Medium (final: single report; draft: law-firm summaries). [Moneylife](https://www.moneylife.in/article/rbi-cracks-down-on-misselling-banks-barred-from-forced-bundling-dark-patterns-and-aggressive-sales-tactics/80785.html); [Vinod Kothari on draft](https://vinodkothari.com/2026/02/rbis-draft-directions-on-sales-practices/); [FoxMandal](https://foxmandal.in/news/rbi-issues-draft-directions-on-fair-marketing-and-sales-practices/) |
| RBI | FREE-AI framework report (13 Aug 2025) | Recommends a board-approved AI policy (accountability, auditability, consumer protection, **AI disclosures**, liability framework), telling consumers when they are dealing with AI, and AI-specific audit. It is a committee report, not binding directions, but supervisors will expect alignment. | Medium. [S.S. Rana](https://ssrana.in/articles/the-free-ai-framework-regulating-ai-in-financial-sector/); [Lakshmikumaran & Sridharan](https://lakshmisri.com/insights/articles/rbi-framework-responsible-and-ethical-enablement-of-artificial-intelligence-in-financial-sector) |
| RBI | Digital Lending Directions 2025 (8 May 2025) | Express borrower consent for data; tight controls on lending service providers (LSPs). A marketing agent acting for a lender may be treated as part of the LSP perimeter. | Medium (companion notes) |
| TRAI/RBI/SEBI/IRDAI | 1600-series directions (Nov-Dec 2025) | Service/transactional voice calls from BFSI on 1600; promotional calls on 140. | Medium-High (companion notes) |
| SEBI | Intermediary regulation amendments (29 Aug 2024) and circulars (22 Oct 2024; 29 Jan 2025) | Regulated entities may not associate, directly or indirectly, with persons who give unregistered advice or make claims of returns or performance. Agent-generated copy for brokers or AMCs that implies returns is a direct breach risk. Education content must use prices at least 3 months old. | Medium. [Storyboard18](https://www.storyboard18.com/advertising/crackdown-on-finfluencers-sebi-bans-association-of-regulated-entities-with-unregistered-finfluencers-35588.htm); [Mondaq](https://www.mondaq.com/india/securities/1580788/securities-law-update-%7c-clarification-on-intermediaries-and-other-associates-persons-regulated-by-sebi) |
| SEBI | Consultation: Guidelines for Responsible Usage of AI/ML (20 Jun 2025) | Proposed: the regulated entity is accountable for AI outputs, with human oversight, investor disclosure, vendor governance and testing. **Final status not confirmed.** | Low-Medium. [Taxmann](https://www.taxmann.com/post/blog/sebi-proposes-ai-ml-governance-framework-for-securities-markets) |
| IRDAI | Master circular 19 Jun 2024, Ch. I (advertisements), under the Policyholders' Interests Regulations 2024 | Insurers **and intermediaries** need controls over the "content, form and method of dissemination" of ads, a named compliance officer, senior approval of distribution-channel ads, and ASCI compliance. ULIPs may not be sold as investments. **Autonomous generation of new insurance ad copy is incompatible with pre-approval: use pre-approved creative only.** | Medium. [Taxguru](https://taxguru.in/corporate-law/irda-master-circular-insurance-advertisements.html); [Cafemutual](https://cafemutual.com/news/insurance/4002-irdai-tightens-advertising-norms-for-insurers) |

### 2.4 Advertising content: ASCI, CCPA, IT Rules
- **ASCI draft Guidelines for Responsible Labelling of AI in advertising (8 May 2026).** Risk tiers: high, medium (labelling mandatory, e.g. virtual influencers, AI replicas of a real person's likeness or voice), low. **Disclosure does not cure a misleading ad.** Final status not found. *(Medium: ASCI PDF surfaced)* - [ASCI draft PDF](https://www.ascionline.in/wp-content/uploads/2026/05/asci-ai-labelling-guidelines.pdf); [Business Standard](https://www.business-standard.com/industry/news/ai-ads-face-asci-test-as-draft-rules-target-deepfakes-misleading-claims-126051201030_1.html); [Mondaq](https://www.mondaq.com/india/advertising-marketing-branding/1790884/ai-in-advertising-ascis-new-draft-disclosure-guidelines)
- **CCPA Guidelines on Misleading Advertisements and Endorsements, 2022.** They apply to advertisers, **advertising agencies** and endorsers in any medium. Penalties: up to **₹10 lakh**, and **₹50 lakh** for subsequent violations; endorser bans of 1 to 3 years. Agent-generated claims ("lowest price", "guaranteed") that are not substantiated expose both the client and possibly us as the "agency". *(Medium-High)* - [Vikaspedia](https://en.vikaspedia.in/social-welfare/social-awareness/consumer-education/guidelines-for-prevention-of-misleading-advertisements-and-endorsements-for-misleading-advertisements-2022); [Exchange4media](https://www.exchange4media.com/advertising-news/govt-issues-guidelines-for-misleading-ads-to-impose-up-to-rs-50-lakh-fine-for-violation-120756.html)
- **CCPA Dark Patterns Guidelines 2023.** 13 patterns (false urgency, confirm-shaming, nagging, and others). Applies to agent copy and consent UX. *(High, companion notes)*
- **IT Rules amendment on synthetically generated information** (G.S.R. 120(E), notified 10 Feb 2026, **in force 20 Feb 2026**). Intermediaries must label synthetic audio/visual content prominently and embed metadata where feasible. Takedown within 3 hours. The duty falls mainly on platforms, but AI-generated video or voice creatives placed on them will be labelled or held to these rules. *(Medium-High)* - [Hogan Lovells](https://www.hoganlovells.com/en/publications/india-introduces-mandatory-labelling-for-ai-and-3hour-takedown-for-illegal-content); [AMS Shardul update](https://www.amsshardul.com/wp-content/uploads/2026/02/Legislative-Update-IT-Rules-Amendment.pdf)

### 2.5 WhatsApp Business Platform (Meta policy, not law, but enforced by Meta)

**Opt-in**
- The person must have given their number **and** agreed to receive messages from *that named business*. The opt-in must clearly state the business name and comply with local law.
- Since Nov 2024, a general marketing opt-in collected on any channel can suffice for Meta. It does **not** satisfy DPDP purpose or channel consent by itself.
- Some 2026 guides say **white-labelling or hiding behind a third party violates policy**. That matters in Mode O.
- *(Medium; Meta text not reached)* - [Meta opt-in doc mirror](https://support.chatarchitect.com/books/meta-whatsapp/page/get-opt-in-for-whatsapp-developer-documentation/export/pdf); [CM.com](https://www.cm.com/blog/whatsapp-updates-opt-in-and-quality-rating-policy/); [Kanal 2026](https://getkanal.com/blog/whatsapp-opt-in-gdpr-ecommerce)

**Templates**
- Marketing templates need Meta approval (standard review up to about 24h).
- Meta **re-categorises** promotional "utility" templates to marketing (from about 9 Apr 2025; stricter guideline from 1 Jul 2025), and the higher rate then applies. One appeal is allowed within 60 days.
- *(Medium)* - [Exotel template categories](https://developer.exotel.com/docs/whatsapp-support/template-categories); [YCloud](https://www.ycloud.com/blog/whatsapp-api-message-template-category-guidelines-update); [Qontak](https://help-center.qontak.com/hc/en-us/articles/55217812132377)

**Quality enforcement**
- A template rated low quality (RED) is **paused for 3h (1st time), 6h (2nd), then disabled (3rd)**. Blocks and reports drive the rating.
- *(High: Meta developer-docs URL surfaced)* - [Meta: template pausing](https://developers.facebook.com/documentation/business-messaging/whatsapp/templates/template-pausing); [AWS: template status/quality](https://docs.aws.amazon.com/social-messaging/latest/userguide/managing-templates_status.html)

**Per-user marketing caps**
- Meta caps how many marketing templates **a user** receives across *all* businesses. Error **131049** ("healthy ecosystem engagement") is the signal. Thresholds are undisclosed. Do not retry within about 24h. Failed sends are not charged.
- *(Medium, vendor docs consistent)* - [Clickatell](https://clickatell.com/help-center/whatsapp/message-templates/what-are-per-user-marketing-template-limits); [MSG91](https://msg91.com/help/whatsapp/whatsapp-error---this-message-was-not-delivered-to-maintain-healthy-ecosystem-engagement)

**Messaging limits**
- Tiers: 250 / 1K (some docs 2K) / 10K / 100K / unlimited unique users per 24h.
- Since **Oct 2025, limits are shared at the business-portfolio level**, across all numbers in the portfolio.
- *(Medium)* - [respond.io](https://respond.io/help/whatsapp/whatsapp-messaging-limits); [Webex Connect](https://help.webexconnect.io/docs/messaging-limits)

**India pricing**
- Per-message pricing. Marketing about **₹0.8631** per delivered message, plus 18% GST and BSP markup. Utility and authentication about **₹0.115**. Service conversations above **1,000 free/month per number billed at the utility rate from 1 Oct 2026**.
- The shift to per-message pricing on 1 Jul 2025 is **[memory, unverified]**. Check Meta's rate card.
- *(Medium for rates, Low-Medium for the Oct 2026 service change)* - [ChatMaxima India pricing](https://chatmaxima.com/whatsapp-api-pricing/india/); [MyOperator 2026](https://myoperator.com/blog/whatsapp-business-api-pricing-india-2026); [AiSensy](https://m.aisensy.com/blog/whatsapp-per-message-pricing-update-effective-january-1-2026/)

**AI on WhatsApp**
- WhatsApp Business Solution terms (effective **15 Jan 2026**) bar "AI Providers" from using the API where a general-purpose assistant is the primary function. **Business-specific AI** (support, order tracking, marketing conversations for the brand) remains allowed. The EU, Italy and Brazil have opened antitrust probes.
- **Implication:** our agent must present as the **brand's** assistant for the brand's own purposes, never as a general assistant.
- *(Medium-High)* - [TechCrunch](https://techcrunch.com/2025/10/18/whatssapp-changes-its-terms-to-bar-general-purpose-chatbots-from-its-platform); [PPC Land](https://ppc.land/whatsapp-bars-ai-chatbots-as-meta-solidifies-messaging-monopoly/)

**Under TRAI?** WhatsApp is not under DLT/TCCCPR per most sources (companion notes). DPDP consent still applies.

---

## 3. Global rules for later expansion

### 3.1 EU AI Act (Reg. (EU) 2024/1689) as amended by the AI Digital Omnibus
- **Art. 5 prohibitions** (since **2 Feb 2025**; penalties since **2 Aug 2025**). Banned: manipulative, subliminal or deceptive techniques that materially distort behaviour and cause significant harm, and **exploiting vulnerabilities** (age, disability, social or economic situation). Example given: a chatbot exploiting the emotional state of people in debt to sell high-interest loans. Several prohibitions have **no intent requirement**. For an autonomous optimiser, a learned policy that discovers "target people in financial distress with urgency copy" is the risk case. *(High)* - [Lewis Silkin](https://www.lewissilkin.com/insights/2025/02/03/prohibited-practices-and-ai-literacy-requirements-of-eu-ai-act-now-applicable-102jytk); [Orrick on Commission guidelines](https://www.orrick.com/en/insights/2025/04/eu-commission-publishes-guidelines-on-the-prohibited-ai-practices-under-the-ai-act)
- **Art. 50 transparency** (since **2 Aug 2026**, *not* delayed by the Omnibus):
  - **50(1):** people must be told they are interacting with AI unless that is obvious. The Commission's draft guidelines of 8 May 2026 suggest the "obvious" exception does not cover general chatbots or help-desks.
  - **50(2):** machine-readable marking of synthetic content, with a grace period to about **2 Dec 2026** for systems already on the market.
  - Fines up to **€15M or 3%** of turnover.
  - Applies to any conversational marketing agent (WhatsApp or email replies) that serves EU users.
  - *(Medium-High)* - [Baker Botts](https://www.bakerbotts.com/thought-leadership/publications/2026/september/eu-ai-act-article-50-transparency-obligations-go-live); [Gibson Dunn on Omnibus](https://www.gibsondunn.com/eu-ai-act-omnibus-agreement-postponed-high-risk-deadlines-and-other-key-changes/); [Legalithm](https://www.legalithm.com/en/blog/eu-ai-act-what-applies-2-august-2026)
- **Digital Omnibus on AI:** reported as adopted, in the Official Journal and in force **27 Jul 2026** (sometimes cited as Reg. (EU) 2026/1744; unverified). High-risk Annex III duties move to **2 Dec 2027** and Annex I to **2 Aug 2028**. *(Medium: dates differ slightly across sources)* - [Usercentrics](https://usercentrics.com/knowledge-hub/eu-ai-act-high-risk-delay-article-50-transparency-consent/); [Winston Taylor](https://www.winstontaylor.com/insights/ai-act-rules-on-high-risk-ai-delayed-as-ai-digital-omnibus-agreed)
- **High-risk?** Generic marketing personalisation is not in Annex III. **[memory, unverified]**: Annex III covers creditworthiness and credit scoring, and risk assessment and pricing in life and health insurance. A BFSI agent that decides *eligibility or price* (not just messaging) could fall into high-risk scope from Dec 2027.

### 3.2 GDPR Art. 22 (automated individual decisions)
- Prohibits *solely* automated decisions with legal or **similarly significant** effects, unless they are necessary for a contract, authorised by law, or based on explicit consent. A rubber-stamp human review does not count. Fines up to €20M or 4%. *(High)* - [ICO](https://ico.org.uk/for-organisations/uk-gdpr-guidance-and-resources/individual-rights/automated-decision-making-and-profiling/whats-new-under-the-gdpr); [SecurePrivacy](https://secureprivacy.ai/blog/gdpr-article-22-automated-decision-making-guide)
- **Marketing:** WP29/EDPB guidance (WP251) says behavioural advertising is usually *not* significant. It **can be** where it targets vulnerable people (the example is gambling ads to someone in financial difficulty) or where differential pricing effectively bars access. SCHUFA widens who counts as the "decider". *(Medium: commentary on WP251; adopted text not read)* - [Bird & Bird](https://www.twobirds.com/insights/2017/global/article-29-working-party-guidelines-on-automated-decision-making-and-profiling); [Mintz](https://www.mintz.com/insights-center/viewpoints/2017-10-24-key-gdpr-guidance-behavioral-advertising-profiling-and)
- **Design consequence:** messaging-only decisions (who gets which email) are normally outside Art. 22. **Offer eligibility, personalised price or credit-line decisions are not.** Keep those human-reviewed or exclude them from agent autonomy.

### 3.3 ePrivacy and EU consumer law
- The **ePrivacy Regulation was withdrawn (Feb 2025)**. The 2002/58/EC Directive and national laws (e.g. PECR) still govern. **Art. 13** requires prior consent for email/SMS marketing, with a **soft opt-in** for similar products to existing customers if an opt-out was offered at collection and in every message. One report says the CJEU in *Inteligo Media* (C-654/23, Nov 2025) treated free accounts as a "sale" for soft opt-in. *(Medium; Inteligo: Low, single source)* - [Lewis Silkin](https://www.lewissilkin.com/en/insights/2025/02/19/long-awaited-eprivacy-regulation-is-finally-dead-102k0fn); [gblock](https://www.gblock.app/articles/eu-eprivacy-withdrawal-email-tracking)
- **Digital Fairness Act:** a Commission proposal is slated for **Q4 2026**, not yet drafted. Scope: dark patterns, personalised advertising, profiling, and price discrimination by device or location. *(Medium)* - [PinMeTo](https://www.pinmeto.com/news/eu-digital-fairness-act-scope-split-2026/); [Bird & Bird consultation note](https://www.twobirds.com/en/insights/2025/eu-–-consultation-on-the-digital-fairness-act)

### 3.4 US
- **CAN-SPAM:** see section 1.3. No B2B exception.
- **TCPA:**
  - AI voice = artificial voice (Feb 2024).
  - **"Revoke-all" consent-revocation rule:** effective date pushed from 11 Apr 2026 to **31 Jan 2027** (order of 6 Jan 2026). It was then **revised by an FCC order around 9-30 Sep 2026**: revocation may be scoped to the category of informational call (non-marketing only), effective 30 days after Federal Register publication, which is not confirmed.
  - A further notice proposes shorter honour windows and a mandated revoke-all method.
  - *(Medium; sources conflict on adoption date)* - [Burr & Forman](https://www.burr.com/telephone-consumer-protection-act/the-fcc-delays-effective-date-of-tcpa-revoke-all-rule-until-january-31-2027); [Hunton](https://www.hunton.com/insights/legal/fcc-adopts-clarifying-changes-to-the-tcpa-revoke-all-rule-to-be-effective-30-days-after-publication); [Troutman](https://www.troutman.com/insights/fcc-revises-tcpa-revocation-of-consent-rules-that-were-set-to-go-into-effect-in-january/)
  - **[memory, unverified]:** the FCC "one-to-one consent" lead-generation rule was vacated by the 11th Circuit in Jan 2025. Search returned nothing to confirm this.
- **State AI laws:** the Colorado AI Act was delayed to **30 Jun 2026**; 2026 status is not confirmed. California's CPPA ADMT rules were **not verified** (search returned nothing). *(Low)* - [Greenberg Traurig](https://www.gtlaw.com/en/insights/2025/9/colorado-delays-comprehensive-ai-law-with-further-changes-anticipated)

### 3.5 Mailbox-provider sender requirements (contractual, but decisive)
- **Gmail:** bulk senders (≥5,000/day to Gmail) need SPF, DKIM and DMARC (p=none at least, with alignment), RFC 8058 **one-click unsubscribe honoured within 2 days**, and a spam rate below **0.10%** (target) and never **≥0.30%**. Enforcement moved to **permanent rejections from Nov 2025**. *(Medium-High; Google page not reached)* - [Red Sift](https://redsift.com/resources/blog/gmails-enforcement-ramps-up-what-bulk-senders-need-to-know); [GMass](https://www.gmass.co/blog/gmail-bulk-sender-guidelines/)
- **Yahoo:** the same 0.3% complaint ceiling (0.1% warning), List-Unsubscribe with POST recommended, honoured within 2 days, SPF/DKIM/DMARC with From-domain alignment. *(Medium)* - [Resend (citing Yahoo)](https://resend.com/docs/knowledge-base/how-do-i-avoid-yahoos-spam-folder.md); [Bird docs](https://bird.com/docs/knowledge-base/deliverability/gmail-yahoo-requirements)
- **Microsoft Outlook consumer:** for >5,000/day, SPF, DKIM and DMARC are required **from 5 May 2025**. Non-compliant mail goes to Junk, with rejection to follow. *(Medium)* - [Security Boulevard](https://securityboulevard.com/2025/04/microsoft-strengthens-email-sender-rules-key-updates-you-shouldnt-miss/); [Site24x7](https://www.site24x7.com/blog/microsoft-outlook-new-email-authentication-requirements)
- **Apple Mail Privacy Protection (MPP):** the Apple proxy pre-fetches pixels, so **opens are inflated for Apple Mail users and are not a valid signal** for an agent's reward function or "engaged" suppression logic. Use clicks, conversions and replies instead. No reliable 2025-26 inflation figure was found. *(High on mechanism, Low on magnitude)* - [Litmus](https://www.litmus.com/blog/apple-mail-privacy-protection-for-marketers/); [Epsilon](https://epsilon.com/us/insights/blog/apple-latest-plans-to-reshape-the-open-web)

---

## 4. Liability allocation: contracts and insurance

### 4.1 What CEPs (customer engagement platforms) put in their contracts *(Medium; excerpts only)*
- **Braze MSA:** the customer has "sole responsibility for the accuracy and legality of all Customer Data" and obtains end-user consents. - [Braze Main Subscription Agreement](https://www.braze.com/company/legal/terms)
- **MoEngage Acceptable Usage Policy:** the customer must obtain and keep documented consent and may not send unsolicited messages. Content must comply with law and with third-party channel terms (DLT, Meta). - [MoEngage AUP](https://www.moengage.com/acceptable-usage-policy-for-customers/)
- **CleverTap ToS:** the customer is responsible for all activity under its accounts. - [CleverTap ToS](https://clevertap.com/terms-service/)
- **Pattern:** CEPs are tools that move consent, content and sending liability to the customer, backed by an AUP and suspension rights. Indemnity and DPA clauses were not retrieved.
- **The structural problem for us:** an *autonomous agent* is not a passive tool. When the agent picks recipients, timing and content, a "customer is solely responsible" clause is harder to defend commercially, and clients will ask us to stand behind the agent's choices. *(Inference)*

### 4.2 AI-model vendor terms that flow down to us *(Medium)*
- **Anthropic Usage Policy:** "high-risk" consumer-facing uses (legal, financial, employment, among others) require **human-in-the-loop** review and **AI disclosure**. A 2026 update reportedly names loan pricing and claims decisions explicitly; a new version was published 8 Oct 2026, effective 12 Nov 2026 (secondary report).
- **BFSI marketing that touches credit or financial-product eligibility** must keep a qualified human able to override.
- Sources: [Anthropic usage policy update](https://www.anthropic.com/news/usage-policy-update); [ConductAtlas](https://conductatlas.com/platform/anthropic/anthropic-usage-policy/high-risk-use-case-operator-safeguard-requirements)
- Other model vendors were not researched (budget).

### 4.3 Recommended contract stack per mode *(design recommendation, not sourced law)*

**Client agreement**
- **Roles clause.** The client is the PE, data fiduciary and advertiser. We are processor and technology provider. In Mode O, add our role as registered telemarketer and messaging provider.
- **Instruction schedule ("Autonomy Mandate").** A signed, versioned document setting out:
  - purposes
  - channels
  - segments allowed and excluded (minors, vulnerable flags, BFSI-restricted products)
  - template and creative library
  - caps
  - quiet hours
  - offer and discount limits
  - escalation triggers
  - The agent acts only within it. This is the evidence that the client "determined purpose and means" (DPDP) and that sends were authorised.
- **DPA (Data Processing Agreement), DPDP s.8(2):**
  - processing only on documented instructions
  - security equal to the client's s.8(5) duty
  - breach notice to the client fast enough for the client's Board report within 72h
  - sub-processor list (LLM vendor, CPaaS, BSP, ESP)
  - deletion and return
  - help with data-principal rights
  - **no use of client data to train cross-client models** without a separate purpose and consent
- **Consent warranty from the client:** valid DPDP, TCCCPR/DCA and Meta opt-ins for every record it supplies or flags as contactable. Plus **our warranty** that the agent will not send to a record lacking a valid consent flag in the ledger.
- **Indemnities, split by fault:**
  - Client indemnifies for its data, consents, registered templates and offers.
  - We indemnify for agent behaviour outside the Mandate: sending to suppressed or unconsented records, outside quiet hours, above caps, or unapproved content.
  - Cap at a multiple of fees, with a super-cap for data-protection and regulatory fines where insurable.
- **Platform-sanction clause:** who bears the loss when a DLT header or template is blocked, a WABA quality rating drops, or a sending domain is blocklisted. Include a step-down to Mode S on trigger.
- **Audit right:** the client can pull per-send evidence packs. We keep logs for at least 1 year (DPDP log retention per the companion notes) or longer if sector rules require.
- **BFSI addendum:**
  - IRDAI and SEBI ad pre-approval: only pre-approved creatives are eligible for autonomous sends.
  - RBI sales-practice compliance: no dark patterns or bundling, and support for mis-selling refund investigations.
  - AI disclosure per FREE-AI.

**Mode O additions**
- Our own DLT telemarketer registration and chain-binding obligations.
- Meta BSP or Tech Provider terms. Avoid hiding the brand: the WABA display name must be the client.
- Our ESP terms.
- **Suspension and termination rights for us** if a client's lists generate complaints that threaten our shared reputation (shared IPs or domains).

### 4.4 Insurance *(Medium-Low)*
- **Affirmative AI liability cover is emerging.** Armilla's Lloyd's-backed policy launched Apr 2025. Limits were raised to **US$25M (Jan 2026)**, with cover for agent failures, harmful outputs and regulatory defence costs. It has been bundled with Chaucer cyber/Tech E&O since Feb 2026. Traditional insurers are narrowing AI cover. - [FinTech Global](https://fintech.global/2026/01/23/armilla-ai-raises-25m-to-expand-ai-liability-coverage); [CX Today](https://www.cxtoday.com/conversational-ai/first-of-its-kind-insurance-targets-costly-ai-hallucinations-in-customer-service-and-beyond/)
- **Unsolicited-communications exclusions are standard** in tech/media/cyber policies. Courts read "arising out of TCPA/consumer-protection laws" exclusions broadly. You need **express affirmative cover** for unsolicited-communication claims. This is US case law, but the same exclusion wording appears in global forms. - [Wiley](https://wiley.law/alert-3082); [Kelley Drye](https://www.kelleydrye.com/viewpoints/blogs/ad-law-access/seventh-circuit-reminds-insurance-policyholders-to-shop-carefully-for-tcpa-coverage)
- **Recommended programme (inference):**
  - Tech E&O + cyber, with an **affirmative AI endorsement or standalone AI liability policy**.
  - An explicit carve-back for unsolicited-communications and privacy-regulatory claims where available.
  - Regulatory fines cover "where insurable by law". Insurability of DPDP penalties in India is untested.
  - Clients will ask to be named as additional insured.

---

## 5. Operational burdens of being the sender

| Burden | India specifics | Global specifics | Who carries it in Mode C vs O |
|---|---|---|---|
| Registration | DLT PE entity, headers, content templates (tagged variables), consent templates, URL/callback whitelist, PE-TM chain; A2P CLI declaration (Third Amendment, about 17 Nov 2026); 140/1600 series | Domain auth (SPF/DKIM/DMARC); US 10DLC (not researched); Meta business verification | C: client (we prepare drafts). O: we register as TM and operate the chain; the client still owns headers |
| Template approvals | DLT template approval per operator; WhatsApp marketing template review (about 24h) and re-categorisation risk | - | Both. Template lead time limits how fast an agent can test new copy: build a **pre-approved template library** |
| Complaint thresholds | TCCCPR: 5 complaints in 10 days triggers action; 15-day bar, then 1-year disconnection | Gmail/Yahoo spam rate below 0.1%, never ≥0.3%; WhatsApp quality rating, template pausing, per-user caps (131049) | O: we absorb shared-reputation damage across clients. C: the client's assets are at risk; we must throttle |
| Opt-out SLA | TCCCPR: operator-level revocation (exact SLA unverified); 90-day re-consent cooldown; DPDP: stop "within reasonable time"; consent-manager events from Nov 2026 | Gmail/Yahoo: one-click, 2 days; CAN-SPAM: 10 business days; TCPA revoke-all (2027, revised) | Both: target **real-time suppression on every channel** |
| Quiet hours | Promotional 09:00-21:00 IST (verify); recovery 08:00-19:00 (RBI) | TCPA 8am-9pm recipient local time; some US states stricter **[memory, unverified]** | Both |
| Audit / evidence | DPDP s.6(10) burden of proof; log retention at least 1 year; RBI FREE-AI audit; IRDAI ad control records; TRAI complaint history and appeals (Third Amendment) | GDPR accountability (Art. 5(2)); AI Act Art. 50 records; CAN-SPAM per-email basis | Both; in O we are the system of record |
| Breach / incident | DPDP: notify Board without delay, detailed report within 72h (fiduciary duty; processor feeds it) | GDPR 72h | Processor duty to notify the client fast |
| Cost exposure | WhatsApp marketing about ₹0.86/message + GST; A2P call termination charge up to ₹0.05/min | ESP and IP costs | O: we front costs and must cap agent spend |

---

## 6. Obligation x mode table

Legend:
- **Client** = the client must do or own it.
- **Us** = we must do or own it.
- **Shared** = both.
- **Product** = our system must enforce it technically.
- "n/a" = not triggered.

| # | Obligation | (S) We suggest, human approves each campaign | (C) Agent sends via client's tools | (O) We send via our own accounts |
|---|---|---|---|---|
| 1 | DPDP fiduciary duties: notice, purpose, consent, burden of proof (s.5, 6, 6(10)) | Client. We supply consent check in the UI | Client legally. **Product must block non-consented sends and log proof per send** | Client legally. Product as in C. Risk we are seen as co-fiduciary if we pick purposes |
| 2 | DPDP processor contract (s.8(2)) and security (s.8(5) via contract) | Client + us (we process data either way) | Client + us | Client + us, with more sub-processors (CPaaS, BSP, ESP) to list |
| 3 | DPDP children: no targeted ads or behavioural monitoring (s.9(3)) | Client; we warn | Client; **Product hard-excludes known or likely minors** | Same as C |
| 4 | DPDP withdrawal and consent-manager events (Rule 4, from 13 Nov 2026) | Client | Client; **Product re-checks at dispatch, ingests consent-manager events** | Same, and we operate the suppression list |
| 5 | TCCCPR PE registration: headers, content templates, consent templates | Client | Client (we may draft) | Client remains PE; **we** register as TM-AF/DF and join the chain |
| 6 | TCCCPR template scrubbing: tagged variables, whitelisted URLs | Client's CPaaS | Client's CPaaS rejects; **Product pre-validates** | **Us** (as TM we run scrubbing and delivery) |
| 7 | TCCCPR consent/DCA, DND/preference scrubbing, 90-day re-consent cooldown | Client / operator | Operator scrubs; **Product must not trigger consent-seeking outside DCA** | Us as TM (scrubbing); Product as in C |
| 8 | TCCCPR time band (promotional 09:00-21:00) | Client (human schedules) | **Product** | **Product / us** |
| 9 | TCCCPR sanctions (5 complaints in 10 days, 15-day bar, 1-year disconnection) | Client assets | Client assets; **we must cap and kill-switch** | Client assets + **our TM registration** (blacklisting) |
| 10 | Third Amendment A2P call declaration (about 17 Nov 2026), ₹0.05/min | n/a unless voice | Client declares CLI ranges; Product uses only declared CLIs | **Us** if we originate calls; we bear termination cost |
| 11 | RBI sales-practice directions (1 Jan 2027): explicit consent before sale, no dark patterns or bundling, refunds | Client (RE); human reviews copy | Client; **Product: copy linter, no bundling, explicit-consent journey** | Same, plus we are likely an "agent" of the RE under outsourcing rules **[inference]** |
| 12 | RBI FREE-AI: AI disclosure, board AI policy, audit | Client | Client; **Product: AI disclosure, model and decision logs** | Same |
| 13 | IRDAI and SEBI ad pre-approval, no return claims, no unregistered-advice association | Client compliance officer approves | **Only pre-approved creatives**; agent cannot author new claims | Same, and we may count as an "associate" if we generate claims |
| 14 | ASCI codes and AI labelling (draft May 2026); CCPA misleading-ads and dark-pattern rules | Client (+ us as agency if we write copy) | Client + us (agent wrote it); **Product: claim and dark-pattern linter, AI label where needed** | Same; stronger "agency/advertiser" exposure for us |
| 15 | WhatsApp opt-in, approved templates, quality rating, per-user caps, portfolio limits | Client's WABA | Client's WABA; **Product: template-only sends, 131049 back-off, quality monitor** | **Our WABA/BSP**: Meta sanctions hit us across clients; brand-hiding risk |
| 16 | WhatsApp ban on general-purpose AI providers (15 Jan 2026) | n/a | Agent must act as brand-specific assistant | Same; higher scrutiny as Tech Provider |
| 17 | Email: SPF/DKIM/DMARC, one-click unsubscribe ≤2 days, spam rate <0.3% (Gmail/Yahoo/Microsoft) | Client's ESP/domain | Client's domain; **Product throttles on spam-rate telemetry** | **Our domain/IP reputation**; shared-pool contamination |
| 18 | CAN-SPAM (US): identification, address, opt-out ≤10 business days; both advertiser and sender liable | Client | Client + possibly us | Client + **us as sender/initiator** |
| 19 | TCPA (US): consent, AI voice = artificial voice, revocation | Client | Client; Product enforces | Client + us as caller |
| 20 | GDPR Art. 22 (EU): no solely automated significant decisions | Low risk (human approves) | **Higher**: avoid autonomous eligibility or pricing decisions; Art. 22 safeguards if any | Same; SCHUFA means our scoring may itself be the "decision" |
| 21 | ePrivacy Art. 13 (EU): consent or soft opt-in, opt-out in every message | Client | Client; Product enforces | Client + us |
| 22 | EU AI Act Art. 5 (manipulation, exploiting vulnerabilities) | Low (human screen) | **Product: vulnerability exclusions, no exploitative policy learning** | Same; we are clearly the **provider** |
| 23 | EU AI Act Art. 50(1) AI-interaction disclosure (2 Aug 2026) | n/a unless chat | **Provider (us) must design disclosure**; deployer shows it | Us |
| 24 | Audit trail / evidence pack | Approval record | **Per-send immutable decision record** | Same + delivery and complaint logs as system of record |
| 25 | Insurance | Tech E&O | + affirmative AI cover | + unsolicited-communications carve-back; higher limits |

---

## 7. Design requirements for "autonomous within guardrails"

Each item is phrased as a product requirement. **[India]** marks a hard India legal or platform driver.

**A. Mandate and scope**
1. **Signed Autonomy Mandate per client** (versioned): purposes, channels, segments, excluded cohorts, template and creative library, offer limits, caps, quiet hours, budget, escalation rules. The agent cannot act outside it. Every decision records the Mandate version.
2. **Autonomy levels per channel and per use case.** Suggest, then Auto-with-delay (human can veto within N minutes), then Full-auto. Default to Suggest for BFSI promotional copy, voice and anything involving price or eligibility.
3. **Separation of authorship from selection.** For SMS and WhatsApp the agent may only select a pre-registered DLT or Meta-approved template and fill typed variables **[India]**. Free-text generation is allowed only for email (and only from approved claim blocks) or for 24h service-window replies.

**B. Pre-send compliance gate** (fail closed; every check logged with its result)

4. Purpose- and channel-specific **DPDP consent** that is present, unexpired and not withdrawn. Store the consent ID and notice version **[India]**.
5. **TCCCPR consent, DND and preference status** for SMS and voice. Never send consent-seeking messages outside DCA. Enforce the 90-day re-consent cooldown after opt-out **[India]**.
6. **Meta opt-in** naming the brand, for WhatsApp.
7. **Minor and vulnerability exclusion:** known or likely under-18 (DPDP s.9). Financial-distress or vulnerability flags for BFSI and EU (AI Act Art. 5, GDPR WP251).
8. **Template validation:** DLT template ID matched, variable pre-tags and types valid, URLs and callbacks on the whitelist, PE-TM chain complete. WhatsApp template approved and not paused, with the correct category **[India]**.
9. **Content linter** for agent-written or variable text: unsubstantiated claims (CCPA misleading-ads rules); CCPA's 13 dark patterns and the RBI dark-pattern ban (false urgency, confirm-shaming); SEBI return claims; IRDAI ULIP-as-investment; ASCI AI-label triggers. A block routes to a human.
10. **Quiet hours** in the recipient's local time: India promotional 09:00-21:00 (configurable stricter, e.g. 10:00-20:00 by default); TCPA 8am-9pm local; per-client stricter windows.
11. **Frequency caps** per person across *all* channels and *all* campaigns (agent plus human). WhatsApp per-user back-off on 131049, with no retry under 24h. Per-segment fatigue caps.
12. **Rate limits and budget caps:** per channel, per hour and per day; WhatsApp portfolio tier headroom; ESP warm-up curves; spend caps (₹ per day on WhatsApp marketing; ₹0.05/min A2P voice).
13. **Re-check at dispatch time** (not only at planning) for consent, opt-out and suppression. Queued sends are re-validated.

**C. Circuit breakers and human escalation**

14. **Kill switches** driven by telemetry:
    - complaint signals (operator complaint feed if available; target well below TRAI's 5-in-10-days trigger)
    - WhatsApp template quality falling to Yellow or Red, or any pause
    - spam rate ≥0.08-0.1% (Gmail Postmaster)
    - bounce or unsubscribe spikes
    - The breaker auto-pauses the channel and drops to Suggest mode.
15. **Escalate to a human** for:
    - new template or creative needs
    - offers above a discount threshold
    - BFSI product, price or eligibility decisions
    - any GDPR Art. 22-type decision
    - linter blocks
    - first send to a new segment
    - volume above X% of baseline
    - any customer reply showing distress or complaint
16. **Two-person rule** for raising caps, expanding the Mandate, or switching to Full-auto.

**D. Opt-out and rights handling**

17. **Real-time unified suppression:**
    - inputs: unsubscribe (RFC 8058 one-click), STOP replies, WhatsApp opt-out button and block, operator revocation, consent-manager withdrawal events (from 13 Nov 2026), DPDP erasure
    - propagated to every channel in **minutes**, well inside Gmail/Yahoo's 2 days, CAN-SPAM's 10 business days and DPDP's "reasonable time"
    - minimal hashed suppression keys retained so erased people are never re-contacted (counsel to confirm under DPDP)
18. **Grievance routing:** customer complaints from any channel create a ticket for the client's grievance officer (DPDP) and are counted in the breaker metrics.

**E. Transparency and explainability**

19. **AI-interaction disclosure** in conversational channels (EU AI Act Art. 50(1); RBI FREE-AI; Anthropic usage policy for consumer-facing high-risk uses). Label AI-generated audio/visual creatives (ASCI draft; IT Rules synthetic-media labelling).
20. **Per-decision explanation record:** why this person, this message, this time (features and reasons in plain words, model version, policy version, Mandate version, guardrail results). Make it retrievable for a customer complaint, a regulator, or a GDPR Art. 15/22 request.
21. **Brand-specific assistant identity** on WhatsApp, never a general assistant (Meta 15 Jan 2026 terms). The WABA display name is the client.

**F. Audit and evidence**

22. **Immutable, append-only send ledger** (hash-chained), one record per attempted send. Fields:
    - inputs (consent IDs, template ID, variables masked)
    - each gate result
    - decision and reason
    - dispatch outcome (accepted, rejected, scrub failure, 131049)
    - complaint and opt-out follow-ups
    - Retain at least 1 year, configurable longer for BFSI.
23. **Continuous evidence pack per client:** consent coverage, sends blocked by reason, complaint rate trends, quiet-hour and cap violations (should be zero), template inventory and status, model changes, human overrides. Exportable for DPDP, TRAI, RBI and IRDAI review.
24. **Change control:** any change to model, policy, prompt or guardrails is versioned, tested on a hold-out or shadow run, and approved before promotion. A rollback is one click.

**G. Learning safety**

25. **Reward hygiene:**
    - No open-based rewards (Apple MPP inflation). Optimise on clicks, conversions and incremental lift from hold-outs, **penalised** by complaints, opt-outs and blocks.
    - Forbid features that proxy vulnerability (financial distress, age under 18, health) for targeting. This is the EU AI Act Art. 5 and WP251 risk.
26. **No cross-client data or model pooling** without a separate purpose and contract (DPDP processor role).

**H. Mode-O-only requirements**

27. **TM registration and chain operation** on DLT. Per-client isolation of headers, templates, WABAs, sending domains and IP pools, so that one client's complaints cannot sink others. A **client-level kill switch** we control.
28. **Affirmative AI liability insurance** with an unsolicited-communications carve-back, before offering Mode O.

---

## 8. Gaps and what to verify next (primary sources not read)

1. **TCCCPR 2018 consolidated text plus the Feb 2025 and Sep 2026 amendments:**
   - the exact opt-out/revocation SLA
   - whether every promotional message must carry an opt-out
   - the time band (09:00 vs 10:00)
   - whether the 15-day bar applies to registered PEs' headers
   - the final A2P charge and its exemptions
   - Source: the [Gazette PDF](https://www.trai.gov.in/sites/default/files/2026-09/Regulation_18092026.pdf), blocked here.
2. **DLT "consent template" and DCA operational flow** for non-BFSI sectors as of Oct 2026, and whether DCA is live for all sectors.
3. **DPDP Rule 10** (children; verifiable parental consent; Fourth Schedule exemptions) and any MeitY notification after Sept 2026. Search budget was exhausted.
4. **RBI sales-practice directions (June 2026):** official circular number, scope (NBFC/HFC agents, DSAs) and whether the 1 Jan 2027 date holds.
5. **ASCI AI labelling:** has the May 2026 draft been finalised?
6. **SEBI AI/ML guidelines:** final or still a consultation?
7. **Meta:** current Business Messaging Policy text, India rate card (Oct 2026), and the per-message pricing start date.
8. **EU:** the Omnibus OJ reference (2026/1744?), final Art. 50 guidelines, and the *Inteligo Media* judgment.
9. **US:** TCPA September 2026 order and Federal Register date; status of the one-to-one consent rule; California ADMT; Colorado 2026 session outcome.
10. **Insurance:** Indian market availability of affirmative AI cover, and the insurability of DPDP penalties.
11. **Indemnity and DPA language** of Indian CEPs (MoEngage, CleverTap, WebEngage, Netcore) and of AI-agent vendors. Only AUP and ToS excerpts were seen.

---

## 9. Deep-pass request from the coordinator: status

The coordinator asked for (1) primary texts with exact clause numbers, (2) root-cause liability analysis with enforcement precedents, (3) real CEP and AI-vendor DPAs, (4) at least 25 opened sources, and (5) root-cause design conclusions.

**What was possible here:**
- **Opening primary sources was not possible in this session.** Re-tested on 11 Oct 2026:
  - WebFetch to developers.facebook.com and meity.gov.in failed (DNS).
  - curl to pib.gov.in, egazette.gov.in, artificialintelligenceact.eu, ascionline.in, braze.com and moengage.com returned no connection through the proxy.
  - Earlier, trai.gov.in, support.google.com, eur-lex.europa.eu and ftc.gov were blocked the same way.
  - The shared WebSearch budget (200/turn) is used up.
  - So **zero sources were opened directly**. Everything in sections 1-8 comes from about 50 search-result summaries, 100+ URLs surfaced and cited.
- Items (2) and (5) are delivered below as analysis. Statutory definitions quoted from memory are marked **[memory, unverified]** and must be checked against the Gazette text.
- To finish items (1), (3) and (4), re-run with network access to: trai.gov.in, egazette.gov.in, meity.gov.in, developers.facebook.com, business.whatsapp.com, support.google.com, senders.yahooinc.com, eur-lex.europa.eu, ftc.gov, fcc.gov, ascionline.in, rbi.org.in, irdai.gov.in, sebi.gov.in, and the vendor legal pages (braze.com/company/legal, moengage.com, clevertap.com, webengage.com, netcore, anthropic.com/legal, openai.com/policies). Or raise the search budget.

### 9.1 Primary-text targets to verify (clause map)

| Instrument | Clause to pull | Why |
|---|---|---|
| TCCCPR 2018 (as amended to 18 Sep 2026) | Reg. 2 definitions ("Sender", "Principal Entity", "Telemarketer", "Header", "Content Template", "Consent"); Ch. on consent and revocation; Schedule-I (Codes of Practice for entities, consent, content template registration); Reg. on "examination of complaints" and graded action | Who is the sender; whether "sender" turns on *who causes* the message or *whose header* carries it; opt-out SLA; sanction ladder |
| TCCCPR Third Amendment (Gazette 18 Sep 2026) | Reg. 4 (A2P declaration), Reg. 6, Reg. 8(a), Reg. 18 (60-day commencement), termination-charge clause | Voice-agent obligations and start dates |
| TRAI directions | 16 Feb 2023 (headers/templates re-verification); 20 Aug 2024 (URL/APK/callback whitelisting, effective 1 Oct 2024); PE-TM chain/traceability (effective 1 Nov 2024); 18 Nov 2025 (variable pre-tagging); 19 Nov and 16 Dec 2025 (1600 series) | Template-only constraint and chain liability |
| Telecommunications Act 2023 | s.28 (user protection, specified messages), penalty schedule | Statutory backstop above TCCCPR |
| DPDP Act 2023 | s.2(i) Data Fiduciary, s.2(k) Data Processor, s.2(b) "automated", s.5, s.6(1)/(4)/(6)/(10), s.7, s.8(1)/(2)/(5)/(6)/(7), s.9(3), s.33 and Schedule | Role allocation and consent proof |
| DPDP Rules 2025 (G.S.R. 846(E)) | Rules 3, 4, 6, 7, 8, 10, First and Seventh Schedules | Notice content, consent managers, security/logs, children |
| Meta | WhatsApp Business Messaging Policy (opt-in, "Business Name"); Business Solution Terms (AI Providers clause); developer docs on template pausing, per-user marketing limits, messaging limits, pricing | Platform obligations |
| EU AI Act (Reg. 2024/1689) | Art. 3(3)/(4) provider/deployer, Art. 5(1)(a)-(b), Art. 25, Art. 50(1)-(2), Art. 99(4), Annex III point 5; Omnibus amending regulation | Agent as provider; chatbot disclosure |
| GDPR | Art. 4(7)-(8), Art. 22, Art. 26, Art. 28(3), Art. 28(10), Recital 71 | Controller/processor; ADM |
| Google / Yahoo | Gmail "Email sender guidelines" (support.google.com/a/answer/81126) and FAQ; senders.yahooinc.com best practices | Thresholds and unsubscribe SLAs |
| FTC / FCC | 16 CFR 316 (CAN-SPAM: "sender", "initiate", "procure"); 47 CFR 64.1200 (TCPA rules) | US sender definitions |

### 9.2 Root-cause analysis: what makes someone the "sender"

**India - TCCCPR.** **[memory, unverified]** TCCCPR 2018 defines "Sender" broadly as a person or legal entity that sends commercial communication. "Principal Entity" is the business or legal entity that registers headers and content templates and *on whose behalf* communication is sent. Telemarketers are the registered intermediaries that aggregate or deliver.

Three structural facts (from the sources cited in sections 1.1 and 2.1) decide liability far more than who "pressed send":
1. Every SMS is bound to a **header and template registered to one PE**.
2. Every message must travel a **declared PE-TM chain**.
3. Sanctions attach to the **sender's telecom resources and registrations** (headers, templates, SIP/PRI trunks, TM registration).

So under TCCCPR, **the regulatory "sender" is fixed by registration, not by the decision-maker**. Software choosing to send on the PE's registered rails is legally the PE sending. The regulations do not recognise a software "decider" as a separate actor (no source found that does).

The root risk is therefore **attribution without control**: the PE is liable for what our agent decides. That is why the PE will insist on contractual control (the Autonomy Mandate) and technical control (kill switch).

We become a sender-side actor in our own right only if:
- (a) we register as a telemarketer in the chain (Mode O), or
- (b) we register headers or templates in our own name, or
- (c) we use our own numbers or SIP trunks for calls. A2P calls from our CLIs would make us the declaring "Sender" under the Third Amendment's Reg. 4.

**India - DPDP.** **[memory, unverified]** s.2(i): a Data Fiduciary is any person who "alone or in conjunction with other persons determines the purpose and means of processing". s.2(k): a Data Processor processes "on behalf of a Data Fiduciary".

The test is **purpose and means, not who executes**. An autonomous agent choosing recipients and timing is exercising *means* within the client's purposes. That is the risky edge:
- If the client defines purposes and the permitted means envelope (channels, segments, templates, caps) and we only optimise inside it, we stay a processor.
- If we set the objective ("maximise revenue"), choose new purposes (for example, cross-sell into a product line the customer never consented to), or reuse data across clients, we are "in conjunction" determining purpose and means. That makes us a **joint fiduciary**, with direct exposure to DPDP penalties (up to ₹250 crore for security; ₹50 crore residual).
- s.8(1) makes the client liable for us whatever the contract says. The contract therefore allocates *loss* between us. It does not change *regulatory* exposure.

**EU/US analogues confirm the pattern:**
- GDPR uses the same purpose-and-means test (Art. 4(7)). SCHUFA shows that a vendor's automated output can itself be the regulated "decision".
- CAN-SPAM makes both the advertiser and the party who "initiates" liable. **[memory]** "Initiate" covers originating or "procuring" the transmission. A vendor whose software decides and transmits is plausibly an initiator.
- Under the EU AI Act we are the **provider** of the agent in every mode.

### 9.3 Enforcement and precedent (what is actually happening)

- **India - TRAI/DoT:** about **1,150 entities blacklisted and 18.8 lakh numbers disconnected** after the 2024-25 anti-spam measures, as of Mar 2025 ([Medianama](https://www.medianama.com/2025/03/223-1150-entities-blacklisted-18-8-lakh-numbers-disconnected-after-spam-regulation-amendments/)). The Third Amendment adds AI/ML detection of suspected spam callers. Enforcement is **operational and fast**: suspension and disconnection, not litigated fines. For an autonomous sender the realistic worst case is **losing the rails**, not a court judgment.
- **India - DPDP Board:** penalties apply from about 13 May 2027 (companion notes). **No Board penalty orders found.** Pre-DPDP, the SPDI Rules 2011 and IT Act s.43A apply.
- **India - CCPA:** dark-pattern notices to 11 e-commerce platforms (companion notes). Misleading-ad penalties up to ₹10 lakh / ₹50 lakh.
- **US:** FTC v. Verkada, **US$2.95M** CAN-SPAM civil penalty (Aug 2024) ([Prospeo summary](https://prospeo.io/s/can-spam-penalties)).
- **EU [memory, unverified, not searched]:** the Italian Garante has issued large fines over unlawful telemarketing and consent chains, for example against energy and telecom companies (Enel Energia, about €79M in 2024, per memory). The UK ICO regularly fines under PECR for unsolicited marketing texts and calls. These show regulators going after the **brand** for its agents' and partners' sends. Verify before citing.
- **Insurance case law:** courts read TCPA and unsolicited-communication exclusions broadly ([Wiley](https://wiley.law/alert-3082); [Kelley Drye](https://www.kelleydrye.com/viewpoints/blogs/ad-law-access/seventh-circuit-reminds-insurance-policyholders-to-shop-carefully-for-tcpa-coverage)).

### 9.4 How CEPs and AI vendors allocate this risk (what was seen; real DPAs NOT opened)

- **CEPs** (Braze MSA, MoEngage AUP, CleverTap ToS, section 4.1): the customer has sole responsibility for data legality and consent; an AUP bans unsolicited sends; the vendor has suspension rights. These are **tool-provider** postures. They work because a human at the customer configures every campaign.
- **AI vendors** (Anthropic usage policy, section 4.2): flow-down duties of human review and AI disclosure for high-risk consumer-facing uses.
- **Gap:** no CEP clause was found that addresses *autonomous* agent sending. This is an open market norm, and an opportunity to set it with an "Autonomy Mandate" plus fault-based indemnity.

---

## 10. Root-cause conclusions: design choices that keep us out of the "sender" role, or make it safe

The **root cause of sender liability** is the combination of three things:
- (i) whose **registrations and accounts** carry the message (TCCCPR PE, TM chain, WABA, sending domain)
- (ii) who **determines purpose and means** (DPDP and GDPR fiduciary/controller)
- (iii) who **initiates** the transmission (CAN-SPAM, TCPA)

Autonomy does not move (i). It can move (ii) and (iii) toward us if we let the agent set objectives or run on our rails.

**Choices that keep us out of the sender role (default product posture = Mode C):**
1. **Run only on the client's rails.** Use the client's DLT PE entity, headers, templates and PE-TM chain; the client's WABA and display name; the client's ESP account and authenticated domain; the client's declared A2P CLIs. **Never** register headers, templates, WABAs, sending domains or CLIs in our own name for client traffic. Never sit in the DLT TM chain.
2. **The client defines purpose and the means envelope in a signed, versioned Autonomy Mandate.** The agent optimises only *inside* it: which consented customer, which pre-approved template, when within allowed hours, how often within caps. Every decision record cites the Mandate version. This keeps us a **processor** under DPDP s.2(k) and GDPR Art. 28.
3. **No objective-setting by the agent.** The agent cannot create new purposes, channels, segments, offers or templates. It can only *propose* them in Suggest mode for client approval.
4. **No cross-client data reuse or pooled training** without a separate lawful purpose. Reuse is the fastest path to joint-fiduciary status.
5. **Client-held kill switch and audit access.** The client can stop the agent, review evidence and adjust the Mandate at any time. This is evidence of client control.

**Choices that make the sender role safe if we take it (Mode O, or wherever (ii)/(iii) may shift):**
6. **Fail-closed pre-send gate** (section 7 B): consent proof, DND/DCA status, Meta opt-in, minor/vulnerability exclusion, template/variable/URL validation, quiet hours, caps, re-checked at dispatch.
7. **Per-client isolation** of headers, templates, WABAs, domains, IP pools and CLIs, so one client's complaints cannot blacklist our infrastructure or other clients. **Breakers that trip well below** TRAI's 5-complaints-in-10-days trigger, WhatsApp RED/pause, and Gmail's 0.1-0.3%.
8. **Template-only authorship** for SMS and WhatsApp, and an approved-claims library for email. This removes content liability under CCPA, ASCI, IRDAI and SEBI from autonomous generation.
9. **Immutable per-send evidence ledger** plus explanation records. These meet DPDP s.6(10) burden of proof, TRAI complaint and appeal processes, RBI FREE-AI auditability, and EU AI Act/GDPR transparency.
10. **Human escalation** for price, eligibility and credit decisions (GDPR Art. 22, Anthropic high-risk rules, EU AI Act Annex III), for new creatives, and for vulnerable cohorts (EU AI Act Art. 5).
11. **Contract and insurance:**
    - fault-based indemnity (client: data, consent, templates; us: acts outside the Mandate)
    - DPDP s.8(2) DPA
    - platform-sanction clause
    - affirmative AI liability cover with an unsolicited-communications carve-back
12. **Launch sequencing:**
    - Start BFSI in Suggest or Auto-with-veto only (RBI sales-practice directions from 1 Jan 2027; IRDAI and SEBI pre-approval).
    - Treat voice as out of scope until A2P declaration (about 17 Nov 2026) is handled per client.
    - Plan consent-manager integration for 13 Nov 2026 and full DPDP duties for 13 May 2027.

---

*Prepared 11 Oct 2026 from web-search summaries only (direct fetching blocked; search budget exhausted near the end). Sections 9-10 were added after a deep-pass request; primary texts could not be opened in this environment. Not legal advice.*
