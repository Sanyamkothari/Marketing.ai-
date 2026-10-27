# Order-history import: a shop's CSV into the agent's event store

Branch `v4/f-import`. Closes ARCHITECTURE_REVIEW finding D5 (row 19 and the "Phase 0" row): the Data Doctor's order-log
path ([`docs/v3/ORDER_LOG.md`](../v3/ORDER_LOG.md)) builds a v1 AutoML table and never writes an event, so a shop that is
not on Shopify or Klaviyo started with an empty memory. This is the front door for those shops.

Code: `autonomy/data/order_import.py` (reader, events, report, command line) and `api/v2/order_import.py` (the upload route).
Tests: `tests/v4/import/` (small fixtures of both shapes under `tests/v4/import/fixtures/`).

## Use

```
python -m autonomy.data.order_import orders.csv --shop my-shop --dry-run
python -m autonomy.data.order_import orders.csv --shop my-shop --timezone Asia/Kolkata --currency INR
```

Options: `--timezone` (IANA zone for times with no offset, default UTC), `--currency`, `--date-order day_first|month_first`,
`--exported-at` (ISO 8601 with offset; default the file's modification time), `--database-url` (default
`AUTONOMY_DATABASE_URL`, else `sqlite:///autonomy.db`).
Without `--dry-run` it appends the new events and folds them into customer state. A dry run opens no database unless
`--database-url` is given; with one it reads the store to say how many events would be new.

```
curl -X POST 'http://localhost:8000/v2/import/orders?shop=my-shop&dry_run=true' \
     -H 'content-type: text/csv' --data-binary @orders.csv
```

The body is the raw CSV (as `POST /v2/webhooks/{channel}` takes its raw body), so the light v2 CI job needs no multipart
parser. `shop` defaults to the server's business id; `exported_at` defaults to the time the upload is received
(`Services.clock`). 400 for a file that is not a known shape or an `exported_at` before its latest order, 413 above 64 MB. The route
is not in `docs/v2/openapi.json` (`IN_SCHEMA` is false, as for `/v2/images`), so that shared file is untouched.

The report (printed, or the JSON answer) holds: rows read and used, orders, customers, refunds and their total, gross
revenue, skipped rows by reason, how many customers are contactable, events built, and (given a store) how many were new
and how many already held. It carries counts only, never an id or an address. `orders`, `refunds`, `refunded_amount` and
`revenue` are what the file holds: a Shopify refund that grew since an earlier import writes only the increase, and
`events_new` says how many events were actually written. `contactable` is the file's yes answers less those whose stored
opt-out stands (`consent_blocked`).

## What is read

| Shape | Columns (case, spaces and punctuation ignored) |
|---|---|
| UCI Online Retail | `InvoiceNo, StockCode, Quantity, InvoiceDate, UnitPrice, CustomerID` (+ `Description`, `Country`; the order, date and customer names also read `OrderID`, `OrderDate`, `Email` and similar) |
| Shopify orders export | `Name, Email, Created at, Total, Discount Code, Financial Status, Refunded Amount` (+ `Currency`, `Accepts Marketing`, `Cancelled at`, `Lineitem ...`); a `Customer ID` column is used in preference to `Email` |

The detector's column rules are those of `engine/doctor/order_log.py` (an ID-like name for the shopper, a date column that
parses, a repeating order number), written with the standard library. The engine module was not imported: it needs pandas
and the Data Doctor's session objects, and the v2 CI job installs neither. The customer-id rule is
`autonomy.data.mapping.customer_key` (`12346.0` is `12346`, `007` stays `007`, placeholders and scientific notation are
refused, an e-mail is lower-cased). Delimiters `,` `;` and tab are sniffed; a file that is not UTF-8 is read as Windows-1252
(the UCI file) and the report says so.

## What is written

| Event | When | Notes |
|---|---|---|
| `PURCHASE` | one per order | value = sum of the lines (UCI) or `Total` (Shopify); attrs `order_id`, `lines`, `quantity`, `discount_codes`, `offer_id` (first code), `country`, `currency`, `financial_status`, `import_shop`; `item_id` = the biggest line. One per order, not per line, so `CustomerState.purchases` counts orders as the Shopify connector's does |
| `REFUND` (Shopify `Refunded Amount`) | one per order | names `order_id`, `refund_id="refunded"`; never more than the order's total |
| `REFUND` (void) | a `voided` or `expired` order | the unpaid rest of the order, `refund_id="void"`, at `Cancelled at` when the file has it (the connector's convention) |
| `REFUND` (UCI return) | one per returned line (negative quantity, or an invoice beginning `C`) | positive value; attr `cancellation_id`; **no `order_id`**: a UCI return does not say which invoice it returns, so `autonomy.proof.rewards` matches it to the customer's latest earlier purchase with exactly that amount left, else the latest with money left (docs/v3/PROOF_PROFIT.md section 1). A return by a customer with no purchase in the file is skipped (`return_without_purchase`) unless the store already holds an earlier purchase of theirs, so lifetime revenue never goes negative from an import |
| `CONSENT_GRANTED` (email) | only Shopify `Accepts Marketing` of yes/true | see below |

Nothing else: no `SIGNUP` (a first order is not a signup date), no revocation for a "no" unless the store holds a grant.

## Honesty rules

- **No invented consent.** UCI has no consent column, so nobody in a UCI import is contactable. For Shopify only an explicit yes
  on the customer's latest order grants email consent; a no, a blank or a missing column grants nothing. The report's
  `contactable` is the count of those yes answers in the file. An address is still needed to send: the customer id is the
  e-mail for Shopify exports and the shop's own id for UCI.
- **A later no is honoured.** Given a store, a customer the file now says declines marketing, whose latest stored e-mail
  consent is a grant, gets a `CONSENT_REVOKED`. A customer who never consented gets nothing.
- **An export never brings back an opt-out.** Given a store, a yes is not written when the customer holds an
  `UNSUBSCRIBE` or `COMPLAINT`, a `CONSENT_REVOKED` from any other source, or an earlier import's own revoke at or after
  this export's `exported_at`. The state folder folds events in the order they arrive, so without this an old export
  would undo a newer unsubscribe. Such answers are counted in `consent_blocked` and not in `contactable`. A yes is also
  not written again when the latest held consent is already a grant. A grant's id includes `exported_at`, so a customer
  who said no in one export and yes in a later one is granted again.
- **Consent and refund times are not in a Shopify export.** Both are stamped at `exported_at`, never at the order, and the
  refund carries `time_known=false`. A Shopify file with any such refund or any marketing answer is refused without
  `exported_at`, and so is an `exported_at` earlier than the file's latest order: either would date a refund or a consent
  before it happened. The command line defaults it to the file's modification time, the API route to the time the upload
  is received.
- **Ids are text.** Order numbers (`#1001`, `536365`) and customer ids are never parsed as numbers.
- **Time zones.** A time with an offset keeps it; one without is read in `--timezone` and counted (`naive_times`, with a
  note when that zone is the UTC default). Events are stored in UTC; a non-UTC zone is also put on the event as
  `attrs["timezone"]`, as `map_frame` does. A slash date that could be day first or month first is refused, with the option
  that settles it, unless a part above 12 in the file's own dates settles it.
- **Money.** Amounts are plain or comma-grouped (`1,234.50`); anything else is skipped as `bad_amount` rather than guessed.
  A Shopify file with several currencies imports only one (`--currency`, else the most common) and counts the rest as
  `other_currency`; amounts are never added across currencies.
- **Skipped rows, each with a reason** (`SKIP_REASONS`): `blank_row`, `missing_order_id`, `missing_customer` (a guest
  checkout, never merged into one customer), `placeholder_customer`, `bad_customer_id`, `bad_timestamp`, `bad_amount`,
  `non_positive_price`, `zero_quantity`, `order_customer_conflict`, `other_currency`, `non_product_line` (UCI stock codes
  that are not goods: `POST`, `DOT`, `C2`, `D`, `M`, `S`, `B`, `BANK CHARGES`, `AMAZONFEE`, `CRUK`) and
  `return_without_purchase`. For Shopify the unit is the row, and an order whose customer is blank skips all its rows.

## Idempotency (DECISIONS B-8)

Every event id is `stable_id("evt", "order_import", shop, kind, order id, line)`: a content hash, never a row position. The
event store drops a repeated id, so importing the same or an overlapping export adds nothing twice, a re-export that lists
orders in another order adds only the new ones, and the same file for another shop is a different set of events.

- A purchase's "line" is the whole order, so an order that gains lines in a later export keeps its first value. This is
  the one place the keying is coarser than B-8's "line": a PURCHASE per line would make `purchases` count lines, not orders.
- A UCI return's line is (invoice, stock code, quantity, price) plus an occurrence counter, so two identical returned
  lines stay two events.
- A Shopify refund's id includes the refund total. Given a store, the event written is the increase over the import
  refunds the store already holds for that order, so a refund that grew from 300 to 500 adds one event of 200, and
  importing the 500 export again adds nothing. Without a store (a bare dry run) it cannot know the increase and reports
  the full amount.

## Measured

Every figure is from a command run on this branch, on this machine.

| Command | Result |
|---|---|
| `pytest -o addopts="" tests/v4/import` | 52 passed in 3.76 s (45 in `test_order_import.py`, 7 in `test_order_import_api.py`; the API file needs FastAPI and httpx and skips without them) |
| the same plus `tests/v2/backbone/test_loaders.py test_folder.py test_sql_stores.py`, `tests/v2/realfix/test_mapping_safe.py`, `tests/v2/api/test_api.py`, `tests/unit/doctor/test_order_log.py test_order_log_parity.py`, `-n 2` | 316 passed, 22 skipped in 17.37 s (the skips are the Postgres variants, which need `MARKETING_AI_TEST_DATABASE_URL`) |
| `python -m autonomy.data.order_import tests/v4/import/fixtures/uci_orders.csv --shop uk --dry-run --currency GBP --timezone Europe/London` | 9 rows, 5 used, 3 orders, 3 customers, 1 refund totalling 8.50, revenue 100.82 GBP, 0 contactable, 4 skipped (1 each of `missing_customer`, `placeholder_customer`, `non_positive_price`, `non_product_line`), 4 events |
| `python -m autonomy.data.order_import tests/v4/import/fixtures/shopify_orders.csv --shop in --dry-run --exported-at 2024-04-01T00:00:00+00:00` | 6 rows, 5 used, 4 orders, 3 customers, 2 refunds totalling 800.00, revenue 3,200.00 INR, 1 contactable, 1 skipped (`missing_customer`), 7 events |
| `ruff check`, `black --check`, `mypy --strict` on the changed `autonomy/`, `api/v2/` files and the new tests | clean |

## Not done, and pending CI

- Not run on a full export: the real UCI file is not downloadable here, and the small sample under `library/` is
  aggregated per customer, not an order log. The fixtures are hand-written; the expected numbers in the tests are worked
  out by hand in their comments. Pending CI or a real file:
  `python -m autonomy.data.order_import library/online-retail/data/online-retail-dataset.csv --shop uci --dry-run --timezone Europe/London --currency GBP`
  (after `library/online-retail/fetch.py`).
- `.github/workflows/v2.yml` runs `tests/v2 tests/v3` only; `tests/v4` runs in `ci.yml` (testpaths) but is not yet in the
  v2 job's paths, lint or test lines. The integrator adds `tests/v4` there.
- The route is not in `docs/v2/openapi.json` (see above). Publish it with `python -m api.v2.app --openapi` after setting
  `IN_SCHEMA = True` if it should be.
- A Shopify export's `Email` is the customer id, which differs from the numeric id the Shopify connector uses. A shop that
  later connects Shopify needs the identity linker (`autonomy/connectors/identity.py`) to join the two; this import does not.
- The route sits behind the v2 operator bearer-token guard (`_Guard` in `api/v2/app.py`; `/v2/import/` is not in
  `_PUBLIC_PREFIXES`), like the other write routes.
- An export's "no" revokes a held grant even when that grant (from Klaviyo, say) is newer than the export. This errs
  towards not sending; a shop that re-collects consent elsewhere after an export should not re-import that older export.
