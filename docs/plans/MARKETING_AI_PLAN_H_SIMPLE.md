# Marketing AI — Plan H: One simple product

**Companion to:** Plan G (`MARKETING_AI_PLAN_G_AGENTS.md`), `PARALLEL_WORK_PROTOCOL.md`
**Owner:** Minfy — AI/ML team
**Decision range:** DEC-1100 … 1199
**Milestones:** M80 – M84
**Status:** approved in conversation on 2026-09-28; building

> The platform grew one phase at a time and now shows every capability as its own page: industries,
> clients, "Build data", reports, campaigns, model health, schedules, uplift, privacy, admin. A
> marketer does not understand half of it. This plan **keeps the engine and hides the complexity**:
> a normal user sees four pages and one three-step flow. Nothing that works is deleted; rarely used
> tools move under Settings or into the flow where they belong.

## 1. Decisions taken with the product owner

| # | Question | Answer |
|---|---|---|
| H1 | Industries with different lifecycles? | **No.** One generic journey for every business. |
| H2 | Client chooser? | **No.** The product works for one company; the default client stays hidden underneath. |
| H3 | Sample data in the product? | **No.** It starts empty. Test data lives in tests only; the demo seed is a developer command. |
| H4 | Uplift, privacy, schedules, model health as pages? | **Hide or fold into the flow.** Uplift becomes a step of every use case that acts on customers. |
| H5 | Sign-in and roles? | **Single-user tool for now.** Sign-in stays off; admin screens are hidden. |
| H6 | Which cloud services? | **As many as are easy:** see §4. |

## 2. What a person sees

```
🏠 Home    🔌 Connections    📊 Results    ⚙️ Settings
```

- **Home** — one generic journey, four goals, each with its use cases:
  - **Win customers:** Targeted Advertisement, Vehicle Policy Cross-sell, Term Deposit Conversion
  - **Keep them paying:** Payment Propensity, Card Default Propensity
  - **Stop them leaving:** RCA (churn with reasons), Telco Customer Churn
  - **Win them back:** Win-back Campaign, Retail Win-back
  - **Run smoothly** (operations, shown last): Order Fulfillment, Fault Prediction
  - No industry picker, no client picker, no demo numbers.
- **Connections** — every cloud service in one place: set up, test, see status (§4).
- **Results** — every run, newest first, with its use case, status and outcome; campaign results
  and reports are opened from a run, not from the menu.
- **Settings** — AI service, privacy, schedules, advanced tools (uplift workbench, data request kit,
  raw-table builder), about. Nothing a new user needs.
- **Approvals** — a badge on Results when a model waits for approval, not a menu item.

## 3. The use-case flow: three steps (and a fourth after a campaign)

```
1 Choose data   → upload a file, or pick a table / file from a connection
2 Guided setup  → the helper checks and fixes the data and recommends settings; you approve
3 Run & results → scores, reasons, actions; model health and the schedule are tabs here
4 Measure       → after the campaign, see who it actually changed (uplift), when outcomes are in
```

Guided setup opens first; Manual setup stays one click away for experts.

**Step 4 is uplift, folded into the process.** Uplift answers "did contacting this customer change
what they did?", which needs a campaign with a held-back control group. Every use case that sends
an action already holds back a control group (`actions.control_group_fraction`), so every such use
case can measure its campaign: ads, cross-sell, payment reminders, retention and win-back alike.
Operational use cases (order fulfilment, fault prediction) do not contact customers and get no
step 4. The separate Uplift page moves to Settings → Advanced.

## 4. Connections

| Service | How | Tier |
|---|---|---|
| Amazon S3, and S3-compatible stores (Google Cloud Storage interoperability, MinIO, Cloudflare R2) via an endpoint URL | `boto3` (installed) | built in |
| PostgreSQL, and Amazon Redshift (PostgreSQL protocol) | `psycopg` (installed) | built in |
| MySQL / MariaDB | `pymysql` (small, pure Python; pinned) | built in |
| AI service (Amazon Bedrock) | existing AWS connection screen, moved here | built in |
| Snowflake, Google BigQuery, Azure Blob Storage | their official SDKs as optional extras; the card says "needs the add-on" when not installed | optional |

Each connection:
- **Set up**: a small form per service (host, port, database / bucket, region, user, secret).
- **Secrets never leave the server**: stored encrypted at rest (Fernet, key from a settings secret),
  never returned by the API, masked in the audit log; read-only credentials are asked for.
- **Test connection**: named steps with a result each — reach the server, sign in, list, read a
  sample, check it is read-only where the service can say — and a plain fix for each failure.
- **Use**: in step 1 of a use case, pick a connection, then a table (databases) or a CSV / Parquet
  object (stores); the platform previews it and imports it as an ordinary upload, so Guided setup,
  runs, recipes and scoring work unchanged. Scoring files can come from a connection the same way.

## 5. Milestones

- **M80 — Connections (engine, API, screen).** Connection store with encrypted secrets; connector
  interface (`test`, `browse`, `preview`, `import_to_upload`); S3, PostgreSQL/Redshift, MySQL, and
  optional Snowflake / BigQuery / Azure Blob; routes; the Connections page; "from a connection" in
  step 1 of Guided and Manual setup. Tests: moto for S3, SQLite-backed fakes or a skip-with-reason
  Postgres/MySQL, contract tests for the optional ones.
- **M81 — One generic journey, one company, empty start.** A `generic` journey file becomes the
  only one shown; the industry picker and client chooser are hidden; no demo data unless a developer
  seeds it; sample values gone from screens.
- **M82 — Four-page navigation.** Home / Connections / Results / Settings; Results merges run
  history, campaign results and reports; model health and schedules become tabs of a use case;
  approvals become a badge; privacy, admin, AI service, uplift workbench, data kit and raw-table
  builder move under Settings; Guided setup opens first.
- **M83 — Measure a campaign (uplift in the flow).** Step 4 on every use case with actions: when a
  scored run's campaign outcomes are uploaded, show incremental conversions and train an uplift model
  from the held-back group, using the existing uplift engine.
- **M84 — Checks and docs.** Every screen test updated; screenshots of the new flow; README "Start
  here" rewritten for a marketer; full suite green.

## 6. Guard rails

- The engine, its artefacts and its APIs keep working; hidden screens stay reachable by URL.
- No secret is ever sent to the browser, logged or written unencrypted.
- A connection imports a snapshot; the platform never writes to a client's systems.
