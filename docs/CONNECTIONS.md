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
| AI service (Amazon Bedrock) | `ai_service` | built in | set up on its own screen, `#/generative/connection` |

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

## API

`GET /connections/kinds`, `GET|POST /connections`, `GET|PUT|DELETE /connections/{id}`,
`POST /connections/{id}/test`, `GET /connections/{id}/browse?path=`, `POST /connections/{id}/preview`,
`POST /connections/{id}/import`. Reads are Viewer; everything that changes a connection or talks to the
service is Analyst. See `docs/API.md` for the bodies.
