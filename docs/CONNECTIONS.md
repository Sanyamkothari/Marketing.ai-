# Connections (Plan H, M80)

**Owner:** Plan H (`engine/connections/`, `api/routes/connections.py`, `ui/modules/connections/`,
`tests/**/connections/**`). **Plan:** `docs/plans/MARKETING_AI_PLAN_H_SIMPLE.md` §4.

## In plain words

The **Connections** page (`#/connections`) lists every place a company's data lives. Set one up once,
test it, and from then on step 1 of any use case can **pick a table or a file from it** instead of
uploading one. Marketing AI only **reads**: it imports a snapshot as an ordinary upload and never
writes to, creates or deletes anything in the connected system.

| Service | Kind | Tier | Needs |
|---|---|---|---|
| Amazon S3 | `s3` | built in | a bucket; keys optional (blank uses this computer's AWS sign-in) |
| Google Cloud Storage (HMAC keys), Cloudflare R2, MinIO and other S3-compatible stores | `s3_compatible` | built in | storage address, bucket, access key ID and secret |
| PostgreSQL (also RDS, Aurora, Cloud SQL) | `postgres` | built in with the `aws` extra (psycopg) | server, database, user, password |
| Amazon Redshift | `redshift` | built in with the `aws` extra (psycopg) | server, database, user, password |
| MySQL / MariaDB | `mysql` | built in (pymysql) | server, database, user, password |
| Snowflake | `snowflake` | optional: `pip install 'marketing-ai[snowflake]'` | account, warehouse, database, user, password |
| Google BigQuery | `bigquery` | optional: `pip install 'marketing-ai[bigquery]'` | project, service-account key (JSON) |
| Azure Blob Storage | `azure_blob` | optional: `pip install 'marketing-ai[azure]'` | account, container, SAS token (or account key) |
| AI service (two: Product AI and Deliverable AI) | `ai_service` | built in | set up on its own screens, `#/connections/ai/product` and `#/connections/ai/deliverable` (see "The AI service" below) |

A service whose add-on is not installed is still listed; its card says **Needs the add-on** and names
the command.

## Testing a connection

**Save and test** runs five named steps and shows each one's result with a plain fix:

1. **Reach the service** - the server answers on its address and port.
2. **Sign in** - the user and password (or keys) are accepted.
3. **List what is there** - schemas and tables, or CSV / Parquet files.
4. **Read a sample** - one row, or the first bytes of one file.
5. **Check it is read-only** - where the service can say: PostgreSQL / Redshift table privileges,
   MySQL grants, an S3 IAM policy simulation (or a bucket anyone can write to), an Azure SAS token's
   permissions. A user who could write is a *caution*, not a failure; the fix is always "use a user
   that can only read".

Every network call has a timeout: at most 10 seconds to connect or sign in, 60 seconds per read.

## Secrets

- A connection's passwords and keys are encrypted with **Fernet** (`cryptography`) and kept in
  `connections/<id>.json` in the artefact store beside its non-secret settings. They are never
  returned by the API (a connection lists only the *names* of the secrets it has saved), never logged,
  and never part of an audit event.
- The key is **`MARKETING_AI_CONNECTIONS_KEY`**: 32 url-safe base64 bytes, as printed by
  `python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"`, or a
  generated secret of at least 32 letters and digits, from which the key is derived. It is a secret
  field: never shown in the settings summary and never shipped to a training job.
- **On an AWS deployment** the database stack generates it once (`marketing-ai/<env>/connections-key`,
  48 letters and digits, retained, never rotated) and the application secret carries it, as it
  carries the privacy salt (DEC-1120). Rotating it would leave every saved password unreadable.
- **On a laptop** (a local data folder) without the variable, a key is generated the first time a
  secret is saved and kept at `<data dir>/connections_key` with 0600 permissions; the log says so once.
  Back it up with the data folder: without it the saved passwords cannot be read.
- **On `env=prod`** (or with S3 artefact storage) saving a secret without the variable is refused, naming
  the variable.
- **A changed key** is recognised (each connection records the key's fingerprint, never the key):
  testing says the saved password cannot be read and to set the old key back or enter it again.

## Using a connection

Step 1 of Guided setup and of Manual setup: **Pick from a connection** → choose the connection → open
folders or schemas → pick a CSV / Parquet file or a table → see its first 20 rows (personal details
masked) → **Import**. Both use the same picker (`ui/modules/connections/picker.js`): Guided setup draws
it itself; Manual setup gets it through the *upload-source* seam (`registerUploadSource` in
`ui/modules/router.js`, registered by `ui/modules/connections/source.js`), which offers "or Pick from a
connection" beside the file upload once at least one usable connection exists, and otherwise a short
line linking to the Connections page. The imported upload then fills Manual Step 1 exactly as a file
upload does (ID and outcome detected, Run posts its `upload_id`).
The import is `POST /connections/{id}/import`, which streams the data into the upload store and ends in
the same code as `POST /uploads` (`api.routes.uploads.finish_upload`), so the helper, the checks, runs,
recipes and scoring treat it exactly as an uploaded file. A table is imported as CSV, bounded by the use
case's `max_file_size_mb`; `connection_source.json` beside the upload records where it came from.

Only one query ever reads a table: `SELECT * FROM <schema>.<table> LIMIT n`, with both names quoted by
the database's own identifier rules (psycopg `sql.Identifier`, MySQL back-ticks, Snowflake double
quotes) and accepted only when the database lists them. BigQuery uses its table API, with no SQL.

## The monthly loop reads from connections (Plan J M107, DEC-1317)

So the value arrives every month without anyone downloading and uploading files, three things can be
read from a saved connection, always read-only and never written back (`engine/measurement/pull.py`):

* **A client's table, kept bound** - `POST /clients/{id}/sources/from-connection {connection_id,
  selection, role?}` adds a source exactly as the file upload `POST /clients/{id}/sources` does (same
  limits, same profile, same role), with a `binding` saying where it came from. `selection` is one file
  (`path`), **the newest CSV or Parquet file under a folder** (`prefix`: newest by the store's own
  last-modified time) or a table (`schema_name` and `table`). Before every scheduled build of a recipe
  that would read a bound source, the table is read again by that binding - for a folder, whatever file is
  newest now - within the per-source row limit, and the build uses it (`docs/ONBOARDING.md` section 8).
  A build that reads only uploaded files reads no connection. Nothing is uploaded.
* **A campaign's outcomes** - `POST /campaigns/{id}/outcomes {connection_id, selection, date_column,
  date_from, date_to, outcome_column?}` reads only the rows dated inside the window and keeps them as an
  ordinary upload with `pull_source.json` beside it (the connection, the table or file, the window, how
  many rows, and whether the database or Marketing AI applied the window). A `measure` schedule does the
  same on its own once the campaign's outcome window has closed.
* **Consent** - `POST /privacy/consent/imports/from-connection {connection_id, selection, client_id?,
  partial?}` (Admin) imports a consent table or file all or nothing, exactly as the file import.

**The query rule, amended (DEC-1317 amends DEC-1105).** A pull from a SQL database (PostgreSQL, Redshift,
MySQL, Snowflake) adds at most **one** condition to the one query: a date window on a column the request
declared - `SELECT * FROM <schema>.<table> WHERE <column> >= '<from>' AND <column> < '<day after to>'
LIMIT n`. The column must be one of the columns the table itself reports (read through the same query
with `LIMIT 0`) and is quoted by the same identifier rule as the names; the two dates are written from
date values, never from typed text. There is no free SQL: a request with any other field is refused,
and a name the database did not list never reaches SQL. A file in a store, or a BigQuery table, is read
whole within the size limit and the same window is applied after reading; a row whose date cannot be read
is left out and counted.

## The AI service: two settings, Product AI and Deliverable AI

Marketing AI talks to a language model for two different jobs, so there are **two settings**, each
with its own provider, model, key and test (DEC-1140):

| Setting | What it powers | Who it is for |
|---|---|---|
| **Product AI** | The Guided-setup chat helper. It reads column names and masked samples of *your* file. | Your team's own tool. You choose it and you pay for it. |
| **Deliverable AI** | What the customer receives: the Onboarding Assistant (document questions), root-cause summaries, win-back campaign copy, and the judge and guardrail checks over them. | The customer's. They may want their own account, their own bill, or a provider that is not a third party. |

**Why separate?** *Billing* (your helper's tokens are not the customer's), *privacy* (the helper sees
your file's structure; the deliverable sees the customer's documents and campaign data, and each
screen tells you exactly who receives those prompts), and *choice* (a customer can insist on Amazon
Bedrock in their own account while your team uses whatever suits it). Neither is the AutoML models that
train and score - those never call a language model.

**The two settings are separate.** Each is connected on its own; neither uses the other's service or key. With nothing connected for a slot, the
features that need it say so (`409 AI_NOT_CONNECTED`, naming the slot) instead of answering with made-up
text; Guided setup's rules-based suggestions and questions keep working, and only the chat box is off.

**Providers:** Amazon Bedrock (no key; the AWS sign-in of this computer or role, region and model
names), OpenAI, Claude (Anthropic), OpenRouter, Hugging Face, and *Other / local* for any
OpenAI-compatible server (Ollama, vLLM, LiteLLM, Together, Groq, an Azure-compatible gateway; the key
is optional). Model names shown are examples only (`configs/ai_service.yaml`); the field is editable and
**Load models from this service** asks the service for its own list.

**Keys** are treated exactly like a connection's secrets: Fernet-encrypted with
`MARKETING_AI_CONNECTIONS_KEY` (same key, same fingerprint, same "enter it again" handling), stored in
`ai_service/product.json` and `ai_service/deliverable.json` in the artefact store, never returned by the
API (a slot says only `has_key: true`), never logged and never in an error. A saved key is kept when you
save again with the key box blank, but only for the same provider **and** the same address; changing
either asks for the key again, so a saved key can never be sent somewhere new. If the encryption key was
changed, the slot says so and the calls say "enter the key again" - they are *not* quietly sent to some
other provider.

**Addresses** must be `https://` (or `http://` for a server on this computer such as Ollama), with no
user name or password, and never a link-local address (the cloud metadata address included). A
deployed Marketing AI additionally refuses loopback and private addresses: a name is resolved and every
address it gives is checked before a test or a call, and redirects are never followed.

**Who sees the prompts.** Every provider except Amazon Bedrock is a third party: the screen says
"Your prompts (including column names and masked samples from Guided setup) are sent to <provider>."
for Product AI, and names the customer's documents and campaign data for Deliverable AI. The Guided-setup
data gate (`agent.ai_data_access`) is fed the Product AI's effective provider.

**Documents without an embedding model.** Claude has no embeddings, and any service can be saved without
one. The document assistant then matches by keywords (`keyword-hash-v1`): real retrieval by shared
words, no model involved, said so on the screen.

**For operators.** `MARKETING_AI_ALLOW_FAKE_AI=1` (default off) lets the deterministic *test model*
answer when nothing is connected. It exists for the test suite and developer checks; a real deployment
must never set it. A use case whose own file says `generative.llm.backend: bedrock` keeps working as
before whenever nothing is saved (its source shows as `config`). On a deployment without
`MARKETING_AI_CONNECTIONS_KEY` the screens are read-only and say so (`editable: false`).

## API

`GET /connections/kinds`, `GET|POST /connections`, `GET|PUT|DELETE /connections/{id}`,
`POST /connections/{id}/test`, `GET /connections/{id}/browse?path=`, `POST /connections/{id}/preview`,
`POST /connections/{id}/import`. Reads are Viewer; everything that changes a connection or talks to the
service is Analyst. See `docs/API.md` for the bodies.

The AI service: `GET /ai-service` (both slots and the six providers), then per slot (`product` or
`deliverable`; any other name is 404) `PUT /ai-service/{slot}` (saves; no network call; a blank `api_key`
keeps the saved key for the same provider and address), `POST /ai-service/{slot}/test` (one 16-token
completion; a failed call is `200` with `ok: false` and a plain `fix`), `POST /ai-service/{slot}/models`
(the service's model names, at most 200; on failure an empty list and a `note`) and
`DELETE /ai-service/{slot}`. Reads are Viewer; the rest is Analyst. These routes read their own body
(16 KiB at most), so a validation error names fields and never repeats a value, and every response is
`Cache-Control: no-store`. At most 4 calls to a service run at once, then `429 AI_BUSY`.
