# What Autonomy takes as input

Written from reading the code on branch `v2/realdata` (20 Oct 2026), not from the design docs. File
and function names are given so each claim can be checked. "Customer" below means whatever the
business uses as a person id.

## The one-paragraph answer

Autonomy runs on **events**: one row per thing that happened to one customer at one moment
(`autonomy/contracts/events.py`, class `Event`). Everything else (customer state, the decision
loop, learning, proof) is computed from events. A business's files become events through an
**`EventMapping`** (`autonomy/data/mapping.py`): a small JSON/Python object that says which column is
the customer id, which is the time, what type of event each row is, and where the money is. There is
**no "point it at a folder" command**: no CLI or API route takes a data folder and runs the agent on
it. A customer (or we) must write the mapping, call `map_frame` per table, append the events to an
event store, and call `rebuild_states`. The Data Doctor API can profile a CSV and map it to events,
but it does not store them.

## 1. The `Event` (the only real input)

| Field | Type | Required | Meaning |
|---|---|---|---|
| `event_id` | text | yes | Unique. The store drops a second event with the same id. `map_frame` makes it stable from the source name and the row key, so reloading a file adds nothing. |
| `customer_id` | text | yes | The person. Text, so `00418683` keeps its zeros; `17850.0` (an id that went through a float column) is turned back into `17850` (`customer_key`). |
| `type` | one of 19 words | yes | Customer actions: `signup`, `visit`, `view`, `cart`, `purchase`, `refund`. Message lifecycle: `send`, `deliver`, `open`, `click`, `reply`, `bounce`, `unsubscribe`, `complaint`, `block`, `fail`. Consent: `consent_granted`, `consent_revoked`, `preference`. |
| `ts` | date-time **with a time zone** | yes | When it happened. A naive time gets the mapping's `timezone` (default UTC). |
| `source` | text | yes | Where it came from (`csv`, `shopify`, ...). |
| `channel` | `email`, `whatsapp`, `sms`, `push`, `web`, `webhook` | for every message-lifecycle type and consent events | The channel. A `send` without a channel is rejected. |
| `decision_id` | text | no | The decision that caused a message. Ties sends, clicks and purchases together for proof. |
| `value` | number | **yes for `purchase` and `refund`** | Gross amount in the business currency. One currency is assumed; there is no currency field. |
| `margin` | number | no | Gross margin of a purchase. If absent, margin stays 0 in state. |
| `category` | text | no | Product category; feeds `top_categories`. |
| `item_id` | text | no | Product id; needed by the catalogue (refill cycles, co-purchase recommender). |
| `text` | text | no | Reply text (never logged). |
| `attrs` | dict of text/number/bool | no | Extra facts. Some keys are read by name: `country` and `timezone` (on the customer's own events: sets their country and local hours), `offer_id` (on a purchase: counts towards offer sensitivity; on a send: the offer), `quantity` (catalogue), `content_id`, `theme`, `format`, and `logged_probability` (on a past `send`: the probability a randomised campaign had of sending it, used to warm-start learning). |

What the event model cannot hold directly: a **treatment flag without a send** (a control customer is
simply someone with no `send`), a **campaign id** (only `decision_id` / `attrs`), **customer
attributes** such as age, gender, income or tenure (there is no customer-profile table; such columns
can only ride along as `attrs` on some event, and the state fold ignores them), and **multiple
currencies**.

## 2. The `EventMapping` (how a table becomes events)

One mapping per table (`autonomy/data/mapping.py`). Fields, in plain words:

- `customer_id`, `timestamp`: column names (required).
- `timestamp_format`: a Python `strptime` format such as `%m/%d/%Y %H:%M`. Leave empty for ISO 8601
  (`2011-12-09 12:50:00`, `2019-11-24T00:00:34+00:00`). **One format per column**: epoch numbers
  (RetailRocket's milliseconds), `YYYYMMDD` integers that pandas read as numbers, dates split over
  several columns (hotel bookings' year / month name / day), "hours since start" (Starbucks) and
  YYMM codes (KDD Cup 98) cannot be read without first rewriting the column.
- `timezone`: IANA zone for times with no offset (`Europe/London`).
- Event type: either `event_type` (the same type for every row, e.g. an orders file is all
  `purchase`) or `event_type_column` + `event_type_values` (the table's own words to our types,
  case-insensitive, e.g. `{"addtocart": "cart", "transaction": "purchase"}`).
- Money: `value_column`, or `quantity_column` + `unit_price_column` (value = quantity x price).
  `negative_as: "refund"` turns a negative amount into a refund of the positive amount. Text numbers
  with comma thousands (`1,234.50`) are read; a decimal comma (`12,5`) is **refused** (the row is
  reported, not misread).
- `margin_column`, `category_column`, `item_column`, `channel` (constant) or `channel_column`
  (values must be our channel words), `decision_id_column`.
- `attrs`: `{attr_name: column}`; `fixed_attrs`: constants on every event.
- `row_key`: columns that identify a row; a repeat is reported as `duplicate_row` and dropped.
  Without it the key is the row's position, so the same file loaded with rows in another order gets
  different ids.

`map_frame(frame, mapping, source)` returns `(events, problems)`. Bad rows are **collected, not
fatal**: each becomes a `MappingProblem` with a code (`missing_customer`, `bad_timestamp`,
`unknown_event_type`, `bad_number`, `bad_channel`, `duplicate_row`, `invalid_event`, `excluded`). Only
a missing column raises `MappingError`. The frame must already be a pandas DataFrame: reading the
file (CSV separator, encoding, Excel, JSON, parquet, zip) is the caller's job.

## 3. Ready-made loaders (only two datasets)

`autonomy/data/loaders.py` has loaders for exactly the repo's two sample datasets:

- **UCI Online Retail (I)**: `online_retail_log_events(frame, grain="invoice"|"line")`. Expects the
  columns `InvoiceNo, StockCode, Quantity, InvoiceDate, UnitPrice, CustomerID, Country` and the date
  format `%m/%d/%Y %H:%M` (the databricks mirror's). Drops guest rows (no CustomerID) and zero or
  negative prices as `excluded`; cancellations become refunds; time read as Europe/London.
  **Online Retail II** has different headers (`Invoice`, `Price`, `Customer ID`) and ISO dates, so it
  needs renaming plus a different date format before this loader can read it.
- **Hillstrom**: `hillstrom_events(frame)`. Needs a **`customer_id` column**, which the committed
  `library/hillstrom-email/sample.csv` has and the full public file does **not** (an adapter must
  add one, e.g. the row number). It invents an approximate purchase history from `recency` and
  `history`, a signup, e-mail consent for everyone, a `send` with a synthetic `decision_id` for the
  treated two thirds (attr `probability=1/3`), and visit/purchase outcomes 3 and 4 days after the
  send. Code-reading note: the send carries the probability under the attr name `probability`, but
  the warm start reads `logged_probability` (`autonomy/learning/observations.py`), so this history
  is treated as un-propensity-weighted. With three equal arms the weights would be equal anyway, so
  no bias here, but a customer's non-uniform campaign would lose its correction if it followed this
  loader's example.

There is no loader for any other dataset, and none for a generic "orders + customers + products"
export.

## 4. From events to customer state

`autonomy/data/rebuild.py` `rebuild_states(event_store, state_store, StateFolder(), now)` folds every
stored event, in time order, into one `CustomerState` per customer (`autonomy/data/folder.py`):
purchases, revenue, margin, last purchase, top categories, per-channel stats (sends, delivers,
clicks, replies, bounces, complaints, consent, unsubscribed), contacts in the last 7 and 30 days,
active hours in the customer's own time zone, offer sensitivity (share of purchases with an
`offer_id`, shrunk to 0.3), and an RFM segment (`prospect`, `lapsed` > 180 days, `lapsing` > 90 days,
`vip` >= 5 purchases and >= 500 revenue, `new`, `active`). The VIP revenue bar is in the business's
currency and is not rescaled for, say, Russian roubles or Brazilian reais.

What state needs to be useful for the decision loop:

- at least one `purchase` per customer (else everyone is a `prospect`);
- for anyone to be **contactable**, a channel with `address_known` and `consent` true, which only a
  `consent_granted` event sets. **No public dataset has consent events**, so an adapter must invent
  consent (as the Hillstrom loader does) or the agent will contact nobody;
- `send` / `deliver` / `click` events to learn response and fatigue. Pure order logs (Online Retail,
  Olist, CDNOW, Instacart, Dunnhumby, Ta-Feng) have none: they give the agent a purchase history but
  nothing to learn message response from.

## 5. The Data Doctor engine (`api/v2/engines/doctor.py`)

Two stateless HTTP endpoints, both **CSV only**, both **capped at 20 MB** per upload:

- `POST /v2/engines/doctor/profile`: CSV in (multipart `file` or raw `text/csv`), options `snapshot`,
  `primary_key`, `target`, `date_columns`, `delimiter` (so `;` files work if you say so),
  `use_v1`. Reads every cell as text (`read_csv_bytes`: UTF-8 with BOM, falling back to Latin-1).
  Returns a readiness score (100 minus 20 per error and 7 per warning), per-column kind and missing
  share, personal-data findings (email, phone, card, Aadhaar, PAN by value; names, addresses, dates
  of birth by header), key uniqueness, unreadable or future dates, and columns that might leak the
  target. It never returns cell values.
- `POST /v2/engines/doctor/map-events`: CSV plus an `EventMapping` (JSON form field) in; the events,
  the problem rows and counts out. It does **not** store the events anywhere, and it returns all of
  them in the JSON response.

Not available: Excel, JSON lines, parquet, zip/gzip uploads; files over 20 MB (Online Retail II's CSV
is 95 MB, X5's purchases 670 MB gzipped); column-mapping proposals; joining several tables (Olist
needs orders + customers + items + payments joined before any row has both a person id and an
amount); identity linking; drift alerts (the doc says these are not in the API yet).

## 6. The catalogue (`autonomy/catalog`)

Reads only `purchase` events: `purchases_frame(events)` takes `customer_id`, `item_id`, `category`,
day, `value` and `attrs["quantity"]`. `CycleModel` (refill timing) and `CoPurchaseModel`
(complementary items) need **`item_id`** and repeat purchases of the same item or category. Datasets
without an item column (CDNOW, the RCT sets) cannot feed it. `clean_retail_lines` is a cleaning
step written for the Online Retail log's columns.

## 7. Warm start and priors

- **Warm start** (`autonomy/runtime/agent.py` `_warm_start`, `autonomy/learning/observations.py`
  `logged_sends`): past `send` events (with channel, optional `offer_id`, `content_id` or `theme`,
  `format`, and `logged_probability`), their `deliver` and `click` events, and the unsubscribes and
  complaints that followed, handed over before the first decision. Clicks matter: the warm start
  feeds the **click** model (the docstring of `agent.py` says it warm-starts "the click rates, each
  customer's own record and their reading hours"), so a past campaign recorded only as "sent /
  bought" (Hillstrom, X5, Criteo, Starbucks promo) gives it no clicks to start from. Whether the
  purchase-uplift side also uses such history is for the run step to check, not assumed here.
- **Cross-client priors** (`autonomy/priors`): not a data input. They are pooled statistics
  (`ClientStat`) from at least 5 clients, seeded from the Gym or public datasets by `seed_from`.

## 8. Off-policy evaluation (`autonomy/proof`)

Two OPE paths:

- `autonomy/proof/ope.py`: `ips`, `snips`, `doubly_robust`, `evaluate(rewards, logged_prob,
  target_prob, q_logged=, q_target=)`: plain arrays, so any randomised log fits once you have, per
  row, the reward, the logging probability of the logged action and the target policy's probability
  of that same action. `from_decisions` builds this from Autonomy's own `Decision` records.
- `autonomy/proof/replay/` (`datasets.py`, `harness.py`): a `LoggedBandit` (contexts, action index,
  propensity, reward) with readers for **Open Bandit** (`read_open_bandit`, reads the zr-obp CSVs as
  they are), **Hillstrom** (`read_hillstrom`, needs the standard columns, not `customer_id`;
  propensity fixed at 1/3) and **Criteo uplift** (`read_criteo_uplift`, propensity 0.85/0.15 by
  default); `LoggedBandit.split(test_share, seed)` (a random row split: on these three files a row is
  a customer, so it is a split by customer), `fit_segment_policy` and `replay`. For any other RCT
  (Lenta, MegaFon, X5, Starbucks) an adapter must build a `LoggedBandit` itself: treatment column ->
  action, the known assignment share -> propensity, outcome -> reward.
- `autonomy/proof/replay/fetch.py` lists download URLs. As of today `go.criteo.net` and
  `research.zozo.com` are unreachable from this environment; Criteo is reachable as a GitHub release
  asset (`jszymon/uplift_sklearn_data`), Hillstrom through its S3 URL (checksum matches the one in
  `fetch.py`).

## 9. Running the agent

There is no command that takes a data folder. What exists:

- `python -m autonomy.runtime.worker` runs the live loop on whatever is in the database named by
  `AUTONOMY_DATABASE_URL`; `--demo` runs a simulated Gym shop. Getting real data in means writing
  events to that database's event store from Python.
- The API (`python -m api.v2.app`) has no data upload route (`GET /v2/data/status` only reports).
- `POST /v2/engines/decisions/evaluate-batch` decides for up to 1,000 `CustomerState`s per call,
  given briefs and content: so a state built offline can be scored without the runtime.
- `python -m autonomy.catalog.cycle` and `python -m autonomy.catalog.evaluate` print the
  online-retail catalogue validations; `autonomy.gym.run` runs the simulator.

## 10. What a customer could not do without code

1. Load anything other than a single CSV of 20 MB or less through the API (no Excel, JSON, parquet,
   zip; no multi-table join).
2. Store mapped events or start the agent on them without Python (no upload-and-run route or CLI).
3. Read dates written as epoch numbers, integers, split columns or relative hours.
4. Give the agent consent: it has to be stated as `consent_granted` events, which no export we found
   carries in that form.
5. Bring customer attributes (age, region, plan) into state: there is no place for them.
6. Evaluate a randomised campaign other than Hillstrom / Criteo / Open Bandit without building a
   `LoggedBandit` in code.
