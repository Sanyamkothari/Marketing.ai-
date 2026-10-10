# Agent infrastructure 2025-2026 for a governed "self-proving campaign agent" and a "trust / verification layer" for other companies' marketing agents

Research date: 2026-10-10. Method: WebSearch snippets plus one successful WebFetch (MCP blog). Most vendor/LinkedIn-style sources are secondary; items marked (UNVERIFIED) rest on a single secondary source or conflict between sources. Direct fetch of linuxfoundation.org failed (DNS), so the A2A press-release facts come via search snippets only.

## 1. Model Context Protocol (MCP): spec status, auth, governance, adoption, registry

### Takeaway
MCP is now the de facto tool-access standard, governed neutrally under the Linux Foundation's Agentic AI Foundation (AAIF) since Dec 2025, with spec versions 2025-06-18, 2025-11-25 and a major stateless-core revision on 2026-07-28. OAuth 2.1-style resource-server authorization is built in, so exposing "Marketing AI" as a remote MCP server is technically well supported.

### Cited Findings
- Spec 2025-06-18 added elicitation (servers can ask the user for more info mid-interaction, usable as a human-in-the-loop hook), structured tool output, classification of MCP servers as OAuth Resource Servers with protected-resource metadata, a requirement that clients use Resource Indicators (RFC 8707), and removed JSON-RPC batching — [MCP changelog 2025-06-18](https://modelcontextprotocol.io/specification/2025-06-18/changelog)
- Spec 2025-11-25: a third-party reference repo says it was released 25 Nov 2025 (one-year anniversary); authorization remains OPTIONAL and covers HTTP transports (stdio takes credentials from environment) — [official auth page 2025-11-25](https://modelcontextprotocol.io/specification/2025-11-25/basic/authorization); date via [mirror/reference repo](https://glama.ai/mcp/servers/@chrishayuk/chuk-mcp-server-reference/blob/9ddab97f9b578b38cce2687dadabf9c33da69271/specs/2025-11-25/README.md) (secondary)
- 2025-11-25 auth changes per mirror: OpenID Connect Discovery, incremental scope consent via WWW-Authenticate, OAuth Client ID Metadata Documents as recommended client registration, RFC 9728 Protected Resource Metadata discovery — [mirror](https://glama.ai/mcp/servers/@chrishayuk/chuk-mcp-server-reference/blob/9ddab97f9b578b38cce2687dadabf9c33da69271/specs/2025-11-25/README.md); Keycloak's table lists RFC 8707 Resource Indicators as MUST, Dynamic Client Registration (RFC 7591) as MAY, Client ID Metadata Document as SHOULD (Keycloak notes its CIMD support is experimental) — [Keycloak MCP guide](https://www.keycloak.org/securing-apps/mcp-authz-server)
- Spec 2026-07-28 released 28 July 2026, called by maintainers "the largest revision of the protocol since launch": stateless core (drops `initialize` handshake and protocol-level session so servers scale on ordinary HTTP, round-robin load balancing), Multi Round-Trip Requests, extensions MCP Apps (server-rendered UI) and Tasks (long-running work), authorization revised to align with OAuth/OpenID Connect deployments, formal deprecation policy. An "Enterprise-Managed Authorization" extension post (18 June 2026) is described as stable (not confirmed part of the 07-28 release) — [MCP blog](https://blog.modelcontextprotocol.io/tags/mcp/) (fetched)
- A 2026 MCP roadmap post exists (priorities reported by secondary sources: long-running agents, agent authentication, handling large numbers of tools; one source dates roadmap to 22 Aug 2026) — [official roadmap post](https://blog.modelcontextprotocol.io/posts/2026-mcp-roadmap/) (not opened); [Gigazine summary](https://gigazine.net/gsc_news/en/20260824-mcp-roadmap/) (secondary)
- Governance: Anthropic donated MCP to the Agentic AI Foundation (AAIF), a directed fund under the Linux Foundation, on 9 Dec 2025; AAIF co-founded by Anthropic, Block and OpenAI, supported by Google, Microsoft, AWS, Cloudflare, Bloomberg; founding projects MCP, Block's goose, OpenAI's AGENTS.md. Governing Board handles budget/membership/new projects; projects keep technical control; MCP maintainer structure unchanged — [Anthropic announcement](https://anthropic.com/news/donating-the-model-context-protocol-and-establishing-of-the-agentic-ai-foundation); [MCP blog](https://blog.modelcontextprotocol.io/posts/2025-12-09-mcp-joins-agentic-ai-foundation/); [SD Times](https://sdtimes.com/ai/linux-foundation-forms-agentic-ai-foundation-to-be-new-home-for-mcp-goose-and-agents-md/)
- Adoption claims at donation: more than 10,000 active public servers; adopted by ChatGPT, Cursor, Gemini, Microsoft Copilot, VS Code — [Anthropic announcement](https://anthropic.com/news/donating-the-model-context-protocol-and-establishing-of-the-agentic-ai-foundation) (as summarised by search)
- OpenAI added MCP to its Agents SDK first (Altman announcement, March 2025), then Responses API / ChatGPT; Microsoft announced native MCP support in Windows and a private registry with code-signing/identity verification; Google's Gemini API MCP support was initially limited to SDK-level tool enumeration (2025-era; may have expanded) — [Benzinga](https://in.benzinga.com/25/03/44554189/openai-and-microsoft-back-mcp-standard-in-major-shift-toward-interoperable-ai-agents-that-can-think-click-and-navigate-the-web-but-google-ceo-sundar-pichai-appears-to-be-mulling-over-it-in-shakespearean-fashion-to-mcp-or-not-to-mcp); [Martin Alderson](https://martinalderson.com/posts/mcp-support-across-ai-apis/)
- Official MCP Registry announced in preview 8 Sept 2025 at registry.modelcontextprotocol.io, maintained by a registry working group; no 2026 registry milestones found — [MCP blog](https://blog.modelcontextprotocol.io/posts/2025-09-08-mcp-registry-preview/)
- Salesforce: June 2025 "MCP support across Salesforce" (native MCP client in Agentforce); hosted MCP servers beta from Oct 2025 (SObject, Apex, Flows, prompt templates; OAuth 2.0 + PKCE). Salesforce's own Agentforce Vibes doc still labels hosted servers "Beta" (dev/sandbox/scratch orgs); a third-party article claims GA in April 2026 (conflict, UNVERIFIED) — [Salesforce dev blog](https://developer.salesforce.com/blogs/2025/06/introducing-mcp-support-across-salesforce); [hosted MCP beta](https://developer.salesforce.com/blogs/2025/10/salesforce-hosted-mcp-servers-are-in-beta-today); [Agentforce Vibes docs](https://developer.salesforce.com/docs/platform/einstein-for-devs/guide/hostedmcp.html)
- Microsoft: Entra Agent ID gives each Copilot Studio/Foundry agent its own identity; Foundry agent identity can authenticate to MCP and A2A tools; Microsoft Agent 365 is the control plane to discover/manage agents. Timeline for mandatory Agent ID conflicts across sources (May vs July 2026) — [Microsoft Learn](https://learn.microsoft.com/microsoft-copilot-studio/admin-use-entra-agent-identities); [Entra agent ID governance](https://learn.microsoft.com/en-us/entra/id-governance/agent-id-governance-overview)

### Inferences
- A remote, OAuth-protected, Streamable-HTTP MCP server for Marketing AI is aligned with the spec direction; the 2026-07-28 stateless core fits a FastAPI service behind a load balancer, and the Tasks extension fits long-running measurement jobs ("did it work?" takes days).
- Elicitation (2025-06-18) gives a standard channel for approval prompts, but approval should still be enforced server-side, not left to client UX.
- Because spec versions move quickly (3 revisions in ~13 months), pin an SDK version and plan for periodic upgrades.

### Gaps
- Official Google/Microsoft/OpenAI statements of current MCP support (2026) not retrieved; only 2025-era sources.
- Whether the Agentforce hosted MCP servers are GA is unresolved.
- Exact text of the 2026-07-28 authorization changes was not read.

## 2. A2A, Agent Communication Protocol, payments/commerce protocols, and marketing-vendor support

### Takeaway
A2A (agent-to-agent) is Linux Foundation-governed, reached v1.0 in March 2026, and was reportedly moved under AAIF alongside MCP in Aug 2026. Among marketing platforms, MCP (not A2A) is where concrete interfaces exist today; mostly read-only or scoped, and no marketing vendor was found exposing A2A.

### Cited Findings
- Google donated A2A to the Linux Foundation in June 2025; Linux Foundation press release (9 Apr 2026) reports 150+ supporting organizations, integration across Google, Microsoft and AWS platforms and enterprise production use — [Linux Foundation press release](https://linuxfoundation.org/press/a2a-protocol-surpasses-150-organizations-lands-in-major-cloud-platforms-and-sees-enterprise-production-use-in-first-year) (via search snippet; page fetch failed); [Google Open Source Blog](https://opensource.googleblog.com/2026/04/a-year-of-open-collaboration-celebrating-the-anniversary-of-a2a.html)
- A2A v1.0 frozen 12 March 2026 with signed Agent Cards, multitenancy, version negotiation and multiple protocol bindings (JSON-RPC, gRPC, HTTP+JSON) — [dailyaiworld](https://dailyaiworld.com/blogs/a2a-10-joins-agentic-ai-foundation-internet-agents) (secondary blog; UNVERIFIED against spec)
- Reported 17-18 Aug 2026: Google moved A2A into the AAIF; AAIF grew from <40 members to >250; sources say it is a reshuffle inside LF rather than fresh donation — [Techzine](https://www.techzine.eu/news/devops/143659/google-transfers-a2a-to-the-agentic-ai-foundation/); [Digital Today](https://www.digitaltoday.co.kr/en/view/94171/a2a-agent-to-agent-protocol-to-be-managed-alongside-mcp-under-aaif); [AI2Work](https://ai2.work/blog/google-s-a2a-protocol-joins-the-agentic-ai-foundation-with-mcp) (no official LF/Google release seen)
- Commerce/payments: ACP (Agentic Commerce Protocol, OpenAI + Stripe, Apache 2.0) covers discovery/cart/checkout; AP2 (Agent Payments Protocol, Google) provides cryptographically signed "mandates" for payment authorization, positioned as an A2A extension, with a reported FIDO Alliance donation milestone; Universal Commerce Protocol (UCP, Google-linked) covers the purchase lifecycle. Sources disagree on ACP's current form (OpenAI reportedly pivoting from in-chat checkout) — [Descope guide](https://www.descope.com/blog/post/developer-guide-agentic-commerce); [Crossmint comparison](https://crossmint.com/learn/agentic-payments-protocols-compared); [AP2 site](https://ap2-protocol.org/index.md) (vendor/secondary; verify against primary repos)
- Relevance note: AP2's "signed mandate" idea (user-authorised, verifiable intent) is a design analogue for signed campaign approvals.
- Agent Communication Protocol (IBM's ACP): not researched in this pass; search results used "ACP" for the commerce protocol and a framework article mentions "ACP" without detail — [morphllm](https://www.morphllm.com/ai-agent-framework) (no usable detail).
- Salesforce Agentforce Marketing (formerly Marketing Cloud Next) emphasises configurable guardrails, approval paths and Data Cloud context; no search result mentioned A2A support — [Salesforce](https://www.salesforce.com/marketing/ai/); [Futurum](https://futurumgroup.com/insights/will-salesforce-marketing-cloud-next-deliver-real-agentic-marketing/)
- HubSpot: hosted remote MCP at mcp.hubspot.com, Streamable HTTP, OAuth with PKCE, read/write CRM objects (contacts, companies, deals); separate local developer MCP — [HubSpot docs](https://developers.hubspot.com/docs/build-with-ai/remote-mcp-server)
- Klaviyo: official MCP server (remote recommended, OAuth); custom connections can disable write tools and restrict API scopes — [Klaviyo developer docs](https://developers.klaviyo.com/en/docs/klaviyo_mcp_server)
- Braze: official MCP server in beta, gives AI tools access to non-PII Braze data for analysis; a community project says the official server is read-only (single unofficial source, UNVERIFIED) — [Braze docs](https://braze.com/docs/developer_guide/mcp_server/); [fireMCP (unofficial)](https://glama.ai/mcp/servers/saarkia/fireMCP)
- MoEngage: official hosted MCP server, nine read-only tools for campaign search/analytics, custom-connector status, limited to data centres DC01-DC04 per one doc page; third-party StackOne offers a 40-tool MoEngage MCP including create/update/delete — [MoEngage docs](https://moengage.com/docs/user-guide/ai-and-intelligence/merlin-ai/moengage-mcp-server); [MoEngage blog](https://www.moengage.com/blog/introducing-moengage-mcp-server/); [StackOne](https://www.stackone.com/connectors/moengage/mcp/)
- Netcore: announced an MCP server in May 2025, pitched as decisioning + execution across WhatsApp, Email, SMS, RCS — [Netcore](https://netcorecloud.com/?p=103291)
- CleverTap: only a community "clevertap-mcp" found; no official CleverTap MCP confirmed — [Glama](https://glama.ai/mcp/servers/hsvwhbsmyk) (UNVERIFIED)
- Amplitude: official remote MCP (US mcp.amplitude.com/mcp, EU endpoint) for querying/editing charts, dashboards, experiments, cohorts; not an ingestion endpoint — [Amplitude docs](https://amplitude.com/docs/amplitude-ai/amplitude-mcp). Mixpanel: an MCP server is available in this very environment's tool list (Mixpanel connector) — evidence only from the session tool list, not a web source.
- Segment: searches returned nothing on a Segment MCP server (gap).
- Measurement vendors: Recast has an official MCP exposing its MMM reports (about 20 reports, Claude plugin) — [Recast](https://getrecast.com/mcp-is-here/); [Recast docs](https://docs.getrecast.com/docs/the-recast-mcp). No MCP/agent offering found for Haus or Measured (absence in search, not proof of absence).
- Ad verification: DoubleVerify launched DV Neura on 17 June 2026, with MCP-based access to DV data (Claude first; Gemini/Copilot "expected"), an Insight Agent, and an Activation Agent that executes approved campaign changes within advertiser-defined guardrails, slated for Q3 2026 (forward-looking) — [DoubleVerify newsroom](https://doubleverify.com/company/newsroom/doubleverify-introduces-dv-neura-the-dynamic-ai-engine-powering-the-future-of-media-quality-and-effectiveness); [PPC Land](https://ppc.land/doubleverify-launches-dv-neura-its-ai-engine-for-agentic-ad-campaigns/). No IAS equivalent found.

### Inferences
- Marketing platform MCP servers are the plug-in surface for (b): an external agent (Claude/ChatGPT/custom) already connects to Braze/Klaviyo/HubSpot/MoEngage via MCP and could add a "Marketing AI" MCP server beside them. They are largely read-only/analytics-oriented today, so our agent's "act" step would more likely go through each platform's REST API (or a scoped write MCP such as Klaviyo's) with human approval.
- Vendors push their own in-platform agents (Agentforce, DV Neura), which creates demand for a neutral check that is not the vendor's own agent; no vendor was seen offering such a neutral check on causal value.
- A2A is better suited if we want to be an agent others delegate to ("verification agent" with an Agent Card); MCP fits "tool call" semantics better. Support both is optional; start with MCP.

### Gaps
- Agentforce A2A support not found; Salesforce hosted MCP GA status unresolved.
- IBM/BeeAI Agent Communication Protocol status (reportedly merged into A2A in 2025) not verified.
- No data on Segment/Twilio, CleverTap official, Netcore API detail, Adobe, Iterable, Customer.io, Optimizely.
- Whether any vendor markets A2A for marketing use cases: none found.

## 3. Agent governance and guardrails (HITL, policy-as-code, audit, standards, guardian agents, observability)

### Takeaway
The ecosystem is converging on: per-agent identity, policy enforced at a gateway (default deny), human approval for consequential actions, full traces, and a "guardian agent" runtime-enforcement category. Regulation timelines are shifting (EU AI Act high-risk dates deferred), but NIST has begun agent-specific standards work.

### Cited Findings
- Gartner (11 June 2025): guardian agents will capture 10-15% of the agentic AI market by 2030; they can monitor, or block/redirect actions; poll of 147 CIOs found 24% had deployed a few agents and 4% more than a dozen — [Gartner press release](https://www.gartner.com/en/newsroom/press-releases/2025-06-11-gartner-predicts-that-guardian-agents-will-capture-10-15-percent-of-the-agentic-ai-market-by-2030)
- Gartner Market Guide for Guardian Agents (Feb 2026): three capabilities (visibility/traceability, continuous assurance/evaluation, runtime inspection/enforcement); prediction that through 2028 at least 80% of unauthorised AI agent transactions will come from internal policy violations rather than attacks — [Holistic AI summary](https://www.holisticai.com/blog/gartner-guardian-agent-vendor-guide); [Metomic](https://www.metomic.io/resource-centre/guardian-agents-explained/) (vendor summaries of paywalled report; UNVERIFIED wording)
- Gartner: over 40% of agentic AI projects will be cancelled by end of 2027 (cost, unclear value, inadequate risk controls); only ~130 of thousands of "agentic" vendors are genuine ("agent washing") — [Outlook Business](https://www.outlookbusiness.com/artificial-intelligence/over-40-of-agentic-ai-projects-will-be-scrapped-by-2027-says-gartner); [W.Media](https://w.media/?p=20821) (secondary coverage of June 2025 release)
- AWS Bedrock AgentCore Policy (Cedar, enforced at AgentCore Gateway, intercepts each agent-to-tool call, natural-language-to-Cedar authoring) GA 3 Mar 2026; AgentCore Evaluations GA 31 Mar 2026; both previewed 2 Dec 2025; Guardrails integration at Policy announced 17 Jun 2026 (third-party report) — [AWS What's New (preview)](https://aws.amazon.com/about-aws/whats-new/2025/12/amazon-bedrock-agentcore-policy-evaluations-preview); [AWS What's New (GA)](https://aws.amazon.com/de/about-aws/whats-new/2026/03/policy-amazon-bedrock-agentcore-generally-available/); [Classmethod](https://dev.classmethod.jp/en/articles/20260617-amazon-bedrock-agentcore-policy-guardrails/)
- OWASP Top 10 for Agentic Applications (ASI01-ASI10) published 9 Dec 2025 by 100+ experts; a separate OWASP LLM Top 10 2026 edition reported early Aug 2026 — [OWASP GenAI](https://genai.owasp.org/resource/owasp-top-10-for-agentic-applications-for-2026); [CSA note](https://labs.cloudsecurityalliance.org/research/csa-research-note-owasp-genai-top10-2026-agent-control-stand/)
- NIST CAISI "AI Agent Standards Initiative" launched Feb 2026: industry-led standards, open-source protocols, research on agent authentication/identity; NCCoE concept paper on agent identity/authorization; no final standard yet as of sources — [NIST CAISI](https://www.nist.gov/caisi/ai-agent-standards-initiative); [CASRAI guide](https://casrai.org/guides/nist-ai-agent-standards-initiative). Claim of a Q4 2026 "AI Agent Interoperability Profile" is UNVERIFIED ([meta-intelligence](https://meta-intelligence.tech/en/insight-nist-agent-standards))
- EU AI Act: provisional Digital Omnibus agreement defers Annex III high-risk obligations from 2 Aug 2026 to 2 Dec 2027 and Annex I to 2 Aug 2028; effective only on formal adoption/publication (status as of Oct 2026 not verified); transparency, GPAI and prohibited-practice rules not delayed per one source — [Gibson Dunn](https://www.gibsondunn.com/eu-ai-act-omnibus-agreement-postponed-high-risk-deadlines/); [OneTrust](https://www.onetrust.com/blog/eu-digital-omnibus-proposes-delay-of-ai-compliance-deadlines/)
- Observability: OpenTelemetry GenAI semantic conventions (experimental) are the vendor-neutral layer; Arize OpenInference/Phoenix, Langfuse (MIT, self-hostable), LangSmith are common; one source reports consolidation (e.g. Cisco/Galileo) (UNVERIFIED) — [morphllm](https://www.morphllm.com/ai-observability); [Last9](https://last9.io/blog/best-ai-observability-tools-2026)
- Tamper-evident action receipts: early projects (VERITAS, Stardive, Cinchor Proof, Zerker Labs/SRI) and IETF individual drafts on post-action evidence records/execution outcome attestation; none is an adopted standard; Fortune (June 2026) argues policy control plane plus external attestation are complementary — [Manifund](https://manifund.org/projects/veritas-verifiable-accountability-for-autonomous-ai-agents); [SRI](https://www.sri.com/press/story/sri-spinout-zerker-labs-is-building-trust-and-verification-products-for-the-agentic-economy/); [IETF draft](https://datatracker.ietf.org/doc/draft-morrow-sogomonian-exec-outcome-attest/00/bibtex/); [Fortune](https://www.fortune.com/2026/06/23/ai-agents-verifiable-execution-enterprise-trust-najwa-aaraj-tii/) (self-reported/early-stage)
- ISO/IEC 42001 and NIST AI RMF applicability to marketing agents: NOT retrieved in this pass (see Gaps).

### Inferences
- Governance primitives to copy: (1) Cedar/OPA-style default-deny policy at the tool-call boundary, (2) per-agent identity and scoped tokens, (3) approval gate for write actions, (4) budget/frequency/consent hard limits enforced in our service not the prompt, (5) append-only hash-chained decision log (each decision, hold-out reservation, approval, evidence label).
- An evidence-labelled audit trail plus randomised hold-out is a stronger artefact than the generic "execution receipt" being built by early-stage firms: those prove what the agent did; we can prove whether it worked.
- EU AI Act deferral reduces near-term compliance pull for high-risk; consent/GDPR and marketing regulations (outside this research) remain binding regardless.

### Gaps
- ISO/IEC 42001 and NIST AI RMF specifics not searched; EU AI Act classification of marketing personalisation agents (likely limited-risk/transparency, not high-risk) not verified.
- Final adoption status of the Digital Omnibus not confirmed.
- No quantitative data on HITL approval fatigue or budget-limit patterns from primary sources.

## 4. Security risks and mitigations (prompt injection, tool poisoning)

### Takeaway
MCP tool poisoning and prompt injection are measured, real risks, with a steady stream of CVEs; mitigations are layered (gateway policy, least privilege, pinned/audited tool definitions, human confirmation), not a single fix. For a server we publish, the main duties are strict schemas, least-privilege scopes, treating all inputs from customer data as untrusted, and returning no instructions in tool outputs.

### Cited Findings
- MCPTox benchmark (arXiv, Aug 2025): 353 adversarial tools from 45 live MCP servers across 20 models; average attack success rate 36.5%; more capable models often more susceptible — via [Practical DevSecOps](https://www.practical-devsecops.com/mcp-security-vulnerabilities/) and [CSA note](https://labs.cloudsecurityalliance.org/research/csa-research-note-mcp-tool-poisoning-auto-execution-20260701/) (secondary summaries; arXiv paper itself not opened)
- CSA research note (1 July 2026): tool description field is an unsanitised attack surface; IDEs/coding agents (Cursor, Claude Code, Gemini CLI) auto-execute project-defined MCP servers with developer privileges — [CSA](https://labs.cloudsecurityalliance.org/research/csa-research-note-mcp-tool-poisoning-auto-execution-20260701/)
- Incidents (secondary sources, details vary): GitHub MCP prompt injection via public issues leaking private repo data (May 2025); Asana cross-tenant exposure (June 2025); Anthropic MCP Inspector RCE CVE-2025-49596; mcp-remote CVE-2025-6514 (CVSS 9.6); Cursor CVE-2025-54135/54136; Anthropic filesystem server CVE-2025-53109/53110 — [UpGuard](https://www.upguard.com/blog/mcp-security-incidents); [Zealynx breach index](https://www.zealynx.io/blogs/mcp-breach-index-2025-2026) (check NVD for exact scores)
- Mitigations proposed: gateway-level enforcement, input/output filtering, sandboxing, pinning/auditing tool definitions — [TrueFoundry](https://truefoundry.com/blog/blog-mcp-tool-poisoning-gateway-defense) (vendor; some mitigations not independently confirmed). AWS added Guardrails (prompt-injection/sensitive-info blocking) into AgentCore Policy at the gateway — [Classmethod](https://dev.classmethod.jp/en/articles/20260617-amazon-bedrock-agentcore-policy-guardrails/)
- CRMArena-Pro (Salesforce, accepted TMLR 2026): near-zero inherent confidentiality awareness in evaluated LLM agents; prompting improved this at a cost to task completion — [arXiv](https://arxiv.org/html/2505.18878v1); [The Decoder](https://the-decoder.com/salesforces-crm-benchmark-finds-ai-agents-struggle-in-real-world-business-scenarios/)

### Inferences
- Customer free-text fields (names, notes, support tickets) are an indirect-injection channel into a campaign agent; the planner should never receive raw free text with write-capable tools in the same context, or should pass it through a quarantined step.
- A trust-layer MCP server that returns structured, schema-validated JSON (no free-form prose from third parties) reduces exposure for callers.

### Gaps
- No 2026 primary advisories fetched; CVE scores are from aggregator blogs.
- No marketing-specific injection incident found.

## 5. Precedents for verification / trust layers; is anyone a neutral judge of marketing agents?

### Takeaway
Independent measurement (incrementality, MMM) and ad verification are established categories, and several are now exposing data via MCP/agents, but I found no company positioned as a neutral judge of marketing-agent decisions (causal value, consent, evidence label). The gap appears open but evidence is absence-of-results, not confirmed.

### Cited Findings
- Haus markets AI-powered geo-experiment incrementality ("Causal Intelligence", Jan 2026 blog); no agent/MCP offering found — [Haus blog](https://www.haus.io/blog-tags/causal-intelligence)
- Recast MCP exposes MMM reports to LLM assistants (read-only reporting) — [Recast](https://getrecast.com/mcp-is-here/)
- DoubleVerify (ad verification) moved into agent-era via MCP data access and a guardrail-bound Activation Agent (see Section 2); this shows verification vendors are becoming agent-callable, but DV's agent is also an actor, so it is not a neutral judge — [DoubleVerify](https://doubleverify.com/company/newsroom/doubleverify-introduces-dv-neura-the-dynamic-ai-engine-powering-the-future-of-media-quality-and-effectiveness)
- Gartner frames guardian agents as part of AI TRiSM runtime inspection/enforcement; these target safety/policy, not causal effectiveness — [Holistic AI](https://www.holisticai.com/blog/gartner-guardian-agent-vendor-guide)
- Independent audit/attestation startups for agent actions exist at early stage (see Section 3) — [Manifund](https://manifund.org/projects/veritas-verifiable-accountability-for-autonomous-ai-agents); [SRI](https://www.sri.com/press/story/sri-spinout-zerker-labs-is-building-trust-and-verification-products-for-the-agentic-economy/)

### Inferences
- Positioning: "incrementality-as-an-API for agents" combining pre-action check (is it worth it, consented, within hold-out) and post-action proof. Closest precedents are incrementality vendors (post-hoc, human-facing) and guardian agents (policy, not causal). Neither covers both.
- Trust risk: a verifier operated by a campaign-agent vendor has a conflict of interest (marking own homework). Neutrality arguments (separate service, signed outputs, customer-owned hold-out seed) matter; the ad-verification analogy supports a "third party" posture.

### Gaps
- Measured, INCRMNTAL, Mutinex, Lifesight, Northbeam agentic/MCP offerings not found (only Haus and Recast searched effectively).
- No search for Gartner's AI TRiSM market guide text itself; only secondary mentions.
- Did not verify existence of "agent evaluation as a service" startups beyond early-stage mentions.

## 6. Frameworks for building our own agent (Python/FastAPI on AWS)

### Takeaway
Credible options all exist; for a Python/FastAPI AWS stack, LangGraph (explicit graph with human-in-the-loop interrupts) and the Claude Agent SDK / OpenAI Agents SDK as lighter orchestrators are reasonable, with Bedrock AgentCore as an AWS-native runtime/policy/evaluation plane. Evidence on maturity is from vendor-adjacent comparison blogs, so treat fit judgments as provisional.

### Cited Findings
- 2026 comparisons describe: Claude Agent SDK as Anthropic-first, autonomous-operator style (locked to Claude models, though Bedrock keeps traffic in your cloud); OpenAI Agents SDK as coordinator/sub-agent handoffs; LangGraph as directed graph with explicit control flow, error routing and human-intervention points, favoured for complex stateful production agents; Google ADK as enterprise multi-agent orchestration tied to Google Cloud — [Composio](https://composio.dev/blog/claude-agents-sdk-vs-openai-agents-sdk-vs-google-adk); [Requesty](https://www.requesty.ai/blog/best-ai-agent-sdks-compared-2026-langchain-crewai-openai-anthropic-google); [Tigera](https://www.tigera.io/?p=68978)
- Bedrock AgentCore provides runtime, gateway, Policy (Cedar) and Evaluations, with Policy GA 3 Mar 2026 and Evaluations GA 31 Mar 2026 — [AWS What's New](https://aws.amazon.com/about-aws/whats-new/2025/12/amazon-bedrock-agentcore-policy-evaluations-preview); [AWS News Blog](https://aws.amazon.com/blogs/aws/amazon-bedrock-agentcore-adds-quality-evaluations-and-policy-controls-for-deploying-trusted-ai-agents/)
- OpenAI Agents SDK has had MCP support since March 2025 — [Benzinga](https://in.benzinga.com/25/03/44554189/openai-and-microsoft-back-mcp-standard-in-major-shift-toward-interoperable-ai-agents-that-can-think-click-and-navigate-the-web-but-google-ceo-sundar-pichai-appears-to-be-mulling-over-it-in-shakespearean-fashion-to-mcp-or-not-to-mcp)
- Microsoft's equivalents (Agent Framework, Entra Agent ID, Agent 365) exist but are Azure/M365-centred — [Microsoft Learn](https://learn.microsoft.com/en-us/entra/id-governance/agent-id-governance-overview)

### Inferences
- Because Marketing AI's core (scoring, hold-outs, consent, measurement) is deterministic Python, the LLM should be a thin planner that calls these as tools and never computes the stats; a graph/state-machine (LangGraph-style) maps naturally onto plan -> approval interrupt -> act -> measure -> learn with durable state in the existing DB.
- The same tool functions can be mounted twice: as the agent's internal tools and as the external MCP server (e.g. via the official MCP Python SDK/FastMCP mounted in FastAPI) — design them once.
- AWS-native deployment is eased by AgentCore (policy/evaluations) but adds lock-in; keep policy logic portable (Cedar/OPA).

### Gaps
- No primary-source maturity data (release cadence, breaking changes) for each SDK; no AgentCore MCP-specific doc read; Claude Agent SDK's Python/FastAPI deployment guidance not checked.
- No head-to-head benchmarks of frameworks on a marketing task.

## 7. Evidence of agent reliability in production; case for constrained, approval-gated autonomy

### Takeaway
Benchmarks consistently show large gaps between single-attempt success and repeated-attempt reliability, and lower success in multi-turn business tasks; analysts forecast heavy project attrition. This supports narrow tools, deterministic cores and human approval for consequential actions.

### Cited Findings
- tau-bench: GPT-4o about 61% pass@1 but about 25% pass^8 on retail tasks (original paper numbers as reported); pass^k = probability all k trials succeed — [Automation Anywhere explainer](https://www.automationanywhere.com/company/blog/ai-agent-benchmarks); [beancount.io](https://beancount.io/bean-labs/research-logs/2026/06/12/tau-bench-tool-agent-user-interaction-real-world-domains) (secondary; the original paper not opened). Naming of successor benchmarks (tau2 vs tau3) is inconsistent across sources — [BenchLM](https://benchlm.ai/benchmarks/tau-bench)
- Vendor-reported 2026 tau-bench numbers: 74.5% pass^1 and 60.3% pass^4 (Automation Anywhere agent) vs leaderboard leader 70.2%/56.2%; setups vary across submitters so scores are not comparable — [Automation Anywhere white paper](https://www.automationanywhere.com/sites/default/files/internal-assets/context-intelligence-white-paper_en.pdf); [Steel leaderboard](https://leaderboard.steel.dev/leaderboards/tau-bench/) (vendor claim)
- CRMArena-Pro (Salesforce): leading agents about 58% single-turn success, about 35% multi-turn; nearly half of sampled multi-turn failures were because the model never asked for needed information; workflow execution was stronger (about 83% for Gemini 2.5 Pro) — [arXiv](https://arxiv.org/html/2505.18878v1); [The Decoder](https://the-decoder.com/salesforces-crm-benchmark-finds-ai-agents-struggle-in-real-world-business-scenarios/) (models tested were 2025-era)
- Gartner: more than 40% of agentic AI projects cancelled by end of 2027 — [Outlook Business](https://www.outlookbusiness.com/artificial-intelligence/over-40-of-agentic-ai-projects-will-be-scrapped-by-2027-says-gartner)
- Gartner: through 2028, 80% of unauthorised agent transactions from internal policy violations rather than malicious attacks — [Holistic AI](https://www.holisticai.com/blog/gartner-guardian-agent-vendor-guide) (secondary)
- DoubleVerify's Activation Agent is explicitly limited to "approved campaign changes within advertiser-defined guardrails" — an industry example of constrained autonomy — [DoubleVerify](https://doubleverify.com/company/newsroom/doubleverify-introduces-dv-neura-the-dynamic-ai-engine-powering-the-future-of-media-quality-and-effectiveness)

### Inferences
- Reliability numbers are for 2025-era models in generic domains; they are directional, not predictions for a campaign planner with a narrow tool set. The pass^k framing is useful for our own evaluation: run each planning scenario k times and require consistent decisions.
- Constrained autonomy is consistent with how incumbents ship (guardrails, approval paths, Q3 "within guardrails"). A "self-proving" agent that auto-measures against hold-out gives a feedback signal most agents lack.

### Gaps
- No marketing-specific production failure-rate data found.
- Latest-model (2026) agent benchmark results not retrieved; tau-bench successor naming unresolved.
- Primary sources (the papers, Gartner releases) were not opened for several figures.

---
## Source list (all URLs referenced)
- https://modelcontextprotocol.io/specification/2025-06-18/changelog
- https://modelcontextprotocol.io/specification/2025-11-25/basic/authorization
- https://blog.modelcontextprotocol.io/tags/mcp/ (fetched)
- https://blog.modelcontextprotocol.io/posts/2026-mcp-roadmap/ (not opened)
- https://blog.modelcontextprotocol.io/posts/2025-09-08-mcp-registry-preview/
- https://blog.modelcontextprotocol.io/posts/2025-12-09-mcp-joins-agentic-ai-foundation/
- https://anthropic.com/news/donating-the-model-context-protocol-and-establishing-of-the-agentic-ai-foundation
- https://linuxfoundation.org/press/a2a-protocol-surpasses-150-organizations-lands-in-major-cloud-platforms-and-sees-enterprise-production-use-in-first-year
- https://www.gartner.com/en/newsroom/press-releases/2025-06-11-gartner-predicts-that-guardian-agents-will-capture-10-15-percent-of-the-agentic-ai-market-by-2030
- https://developers.hubspot.com/docs/build-with-ai/remote-mcp-server
- https://developers.klaviyo.com/en/docs/klaviyo_mcp_server
- https://braze.com/docs/developer_guide/mcp_server/
- https://moengage.com/docs/user-guide/ai-and-intelligence/merlin-ai/moengage-mcp-server
- https://amplitude.com/docs/amplitude-ai/amplitude-mcp
- https://getrecast.com/mcp-is-here/
- https://developer.salesforce.com/blogs/2025/06/introducing-mcp-support-across-salesforce
- https://aws.amazon.com/about-aws/whats-new/2025/12/amazon-bedrock-agentcore-policy-evaluations-preview
- https://www.nist.gov/caisi/ai-agent-standards-initiative
- https://www.gibsondunn.com/eu-ai-act-omnibus-agreement-postponed-high-risk-deadlines/
- https://arxiv.org/html/2505.18878v1
- https://doubleverify.com/company/newsroom/doubleverify-introduces-dv-neura-the-dynamic-ai-engine-powering-the-future-of-media-quality-and-effectiveness
- https://genai.owasp.org/resource/owasp-top-10-for-agentic-applications-for-2026
- https://labs.cloudsecurityalliance.org/research/csa-research-note-mcp-tool-poisoning-auto-execution-20260701/
- (other secondary/blog URLs are cited inline above)
