# v3 generated images

Decision 5 of [`plan-v3.md`](../../plan-v3.md), with decision V-2 of [`DECISIONS.md`](DECISIONS.md): a provider
interface with a fake implementation now; the real image model is chosen when phase 4 starts. Generation is
**off by default** (`ImageConfig.enabled = False`). This page covers what is built, how to use it and what is not.
It builds on [`docs/v2/FORMATS.md`](../v2/FORMATS.md): a generated image enters the asset library through the
same approval path as one the brand uploaded (`status=approved` plus `approved_by`), but as its own kind,
`offer_banner`, because the picture states a price and an offer. The Creative and the Critic never use an
`offer_banner` as a general image (see "Kept out of the general pool" below).

## The flow

```
GenerationJob (product x customer type)
   -> generate_image: ImageProvider makes a background; code writes the price and offer text  (autonomy/assets/generate.py)
   -> ImageDesk.submit: VisionChecker looks at it                                          (autonomy/assets/check.py)
         bad  -> REFUSED, with reasons, goes no further
         good -> PENDING, in a batch of at most 20                                         (autonomy/assets/approval.py)
   -> a person approves the batch, or rejects single images                                (api/v2/images.py)
         approved -> checked again, then Asset(kind=offer_banner, status=approved, approved_by=...) in the AssetLibrary
   -> ImageDesk.update_catalogue: a price change retires the approved banner and refuses the pending one
```

A job is **one product for one customer type**. `GenerationJob` has no customer field and rejects unknown keys, so
a per-person image cannot be asked for. Each person's hero product still comes from the recommender; the image for
that product and that person's type already exists.

## The provider interface

`ImageProvider` has one method, `generate(ImageRequest) -> ProviderImage`. The request holds the product id, name
and category, the catalogue photo bytes and a `StyleBrief` (category style, season, theme, brand palette). The
answer holds the image bytes, type, size, the provider's id and the product it says it drew.

`FakeImageProvider` is the only implementation. It returns a valid PNG gradient in the brand's main colour,
the same bytes for the same request, with no network and no randomness. It can be told to plant mistakes
(`Flaw.WRONG_PRODUCT`, `BROKEN_LOGO`, `GARBLED_TEXT`, `OFF_BRAND_COLOUR`) per product, and leaves notes about them
inside the PNG so the fake vision model can read them back. A note Latin-1 cannot hold (a Devanagari item id) is
written as a UTF-8 `iTXt` chunk. A real provider is a second class with the same `generate` method; it must not
draw prices or words.

The image id is built from the job (product, customer type, offer, style), the exact texts drawn, the provider id
and a SHA-256 of the picture the provider returned. A catalogue price change therefore names a new image, and a
provider whose output varies gives a new id on each run.

## Prices and offers are drawn by code

`expected_texts(product, offer)` is the only source of text on the image. It uses the catalogue price and `Decimal`
arithmetic (the sale price rounds half up to the cent), so the numbers are exact:

| Role | Example (price 1,299.50, 20% off, INR) |
|---|---|
| `name` | `Runner 0` |
| `offer` | `20% off` |
| `was_price` (struck through) | `₹1,299.50` |
| `price` | `₹1,039.60` |

The list price is rounded to the cent first. No price on file, or a price that rounds to 0.00, means no price
is shown. A discount on a product without a price, or one whose sale price does not come out below the list price
once rounded (20% off 0.01), raises an error rather than print a number nobody can stand behind. The alt text is
built from the same texts.

`compose_overlay` places those texts as an `OverlaySpec` (positions as fractions of the image, so it scales).
A line wider than the image's margins is shrunk (to no less than 3.5% of the height), and the offer moves to a
second line when it would run into a long name. The width of a line is an estimate (characters x font size x 0.6),
not a measurement; `layout_problems` uses the same estimate, and the rule check refuses a line that runs off the
image or over another. A name is never cut, so one too long to fit is refused.
`render_svg(image)` draws the background with the overlay on top as one SVG, the same string every time, with the
text XML-escaped and nothing else changed. The approval screen uses it for thumbnails
(`GET /v2/images/{image_id}/preview.svg`). Nothing here needs a picture library.

## The vision check

`VisionChecker.check(image, context) -> CheckResult`; a result with any reason is a refusal, and `result.words` is
the reasons in one line.

| Checker | Needs a model | What it refuses |
|---|---|---|
| `RuleChecker` | no | The file is not a PNG or JPEG, or its label disagrees with its first bytes (`bad_format`); the header is damaged (`not_readable`); the declared size is not the real size (`dimension_mismatch`); narrower than 600 px or shorter than 300 px (`too_small`); over 5 MB (`too_large`); blank alt text; product not in the catalogue (`unknown_product`); the image was made for another product or filed under another category (`product_mismatch`); the offer's currency is not the shop's, when the desk is given one (`currency_mismatch`); the overlay text differs from `expected_texts` (`overlay_text_mismatch`), lacks a text (`overlay_text_missing`) or carries one the offer does not call for (`overlay_text_unexpected`); a line runs off the image or over another (`overlay_layout`) |
| `FakeModelChecker` | fake | Another product shown, a broken logo, text the picture drew itself (garbled), colours far from the brand palette; and, failing closed, a picture the observer could not see or whose product or colours it could not tell (`not_seen`) |
| `CompositeChecker` | | Runs several and merges their reasons |

`default_checker()` is the rules plus the fake model. The fake model reads the notes the fake provider left in the
PNG; a real vision model replaces the observer. A file without those notes (a JPEG, a PNG from any other provider,
a PNG whose notes were stripped) is refused with `not_seen`: plugging a real provider into the default checker
refuses every image until a real observer is plugged in too, rather than passing pictures nobody looked at. The
size, format and text limits are in `ImageConfig`. The shop's currency comes from `ImageDesk(..., currency=...)`;
pass `BrandKit.currency`. Without it the currency is not checked.

## Batch approval

`ImageDesk` (`autonomy/assets/approval.py`) is the whole rule set; the routes are thin over it.

- `submit(image)` checks the image. Refused images are kept with their reasons (`GET /v2/images/refused`) and are
  in no batch. Passing images are `pending` and join the open batch, which holds at most 20 (`ImageConfig.batch_size`
  cannot be set above 20). A batch closes when it is approved, so a later image starts a new one. Submitting the
  same image again while it is pending or approved returns the same record. A refused or rejected image may be
  submitted again: it is checked afresh and starts over, and the old record is kept in the queue's `history`.
- `approve_batch(batch_id, approved_by)` checks every image still pending in the batch again, then approves the
  ones that still pass; one that no longer passes becomes `refused` with the new reasons. Images already rejected
  stay rejected. A blank approver is refused; a batch with nothing pending is a conflict.
- `reject(image_id, rejected_by, reason)` rejects one pending image.
- `update_catalogue(catalogue)` holds every pending and approved image against a new catalogue. An approved image
  whose text no longer matches (its price changed) becomes `retired`, and its library asset is put back with
  `status=retired`, so no lookup returns it; a pending one becomes `refused`. Regenerating the job then makes a new
  image with the new price.
- `approved_for(item_id, customer_type, offer)` returns the approved banners for exactly that product, customer
  type and offer.

Only approval writes to the library. It writes an `Asset` with `kind=offer_banner`, `status=approved`,
`approved_by`, the product's `item_id` and `category`, the style's theme, alt text, size, type and byte count.
Pending, rejected and refused images are never written to the library, not even as drafts, so no lookup (`find`,
`candidates`) can return them. `created_at` comes from the clock the desk was given.

### Kept out of the general pool

A generated banner has a price and an offer drawn on it for one product and one customer type. The Creative picks
assets by format and theme only, so an ordinary `image` asset would be a candidate for any cell, including a
no-offer cell or a 10%-off one, and the image would then make a claim the message's offer label does not. So the
asset's kind is `offer_banner`, which no format takes (`FORMAT_MEDIA` in `autonomy/channels/render.py` lists only
`image`, `carousel_card` and `video`):

- the Creative's `assets_for` and `carousel_sets` never return one, so no library variant uses it;
- the Critic refuses an item that carries one (`a text_image item needs image assets`);
- `validate()` in `autonomy/assets/support.py` reports `wrong_kind` for it on every channel.

`tests/v3/images/test_downstream.py` holds the library with an approved `20% off` banner and checks all three.
Sending a banner only with a message making the same offer, to the same customer type, is not built: a sender
would look it up with `ImageDesk.approved_for` and match the cell's offer. Until that exists, approved banners
are reviewed and stored but not sent.

| Route (`api/v2/images.py`) | What it does |
|---|---|
| `GET /v2/images/batches?open=true` | Batches and their counts; `open=true` keeps those with a pending image |
| `GET /v2/images/batches/{batch_id}` | The batch's images: texts drawn, checks, status, preview link |
| `POST /v2/images/batches/{batch_id}/approve` (no body) | Approve all still pending |
| `POST /v2/images/{image_id}/reject` `{reason}` | Reject one |
| `GET /v2/images/{image_id}/preview.svg` | The banner drawn |
| `GET /v2/images/refused` | Refused images with reasons |

Who approved or rejected is the server's identity, never a name from the request: `ImageServices.approver`,
which defaults to `APPROVER` (`"marketer"`), the same name brief approval records (`api/v2/deps.py`). The API has
no user accounts yet, so this records the role, not the person; an auth layer would set `approver` per request.
Calling `ImageDesk` directly still takes a name.

Errors: 404 unknown batch or image, 409 wrong state, 422 bad body or blank approver, 503 when images are not
attached.

## Turning it on

Off by default in three places: `ImageConfig.enabled` is false (`generate_image` raises `ImagesDisabledError`),
`create_app(..., images=None)` attaches nothing so the routes answer 503, and the routes are not in
`docs/v2/openapi.json` (`IN_SCHEMA = False` in `api/v2/images.py`). To turn it on, a caller builds an `ImageDesk`
over the brand's `AssetLibrary` and passes `ImageServices(desk)` to `create_app`. Approved banners are then
stored but, as said above, not yet sent.

## What is not built

- **No real image model.** Only the fake exists, so nothing here says how good a real model's backgrounds are.
- **No raster output.** Email clients do not show SVG. The approved asset's `url` comes from a `url_for` callable
  given to the desk; something at send time must turn the SVG into a PNG or JPEG at that URL (for example a
  rasteriser behind the CDN). The asset's `size_bytes` is the background's size, not the final file's. Pillow was
  not used, to keep the light CI job light.
- **No offer matching at send time** (see "Kept out of the general pool").
- **The queue is in memory.** A restart loses pending images. A database-backed queue is a second class with the
  same methods as `InMemoryImageQueue`.
- **No scheduler.** Nothing yet decides which product x customer-type pairs to generate, or calls `generate_image`;
  that belongs to the Analyst once the feature is switched on.
- **The vision model is fake.** The pass test of decision 5 is "at least 95% of planted bad images caught". The test
  `test_the_default_checker_catches_at_least_95_percent_of_a_fixed_eval_set` measures that for the rules plus the
  fake model on a fixed set of planted images (every rule plant on three products, every model flaw on four
  products). That set is the same plants the parametrized tests already require to be caught, and the fake model
  reads back flags the fake provider wrote, so the figure is 100% by construction: it adds no evidence beyond those
  tests. `test_held_out_plants_the_parametrized_tests_do_not_name` adds six plants the parametrized tests do not
  name (notes stripped, a stale price, a wrong currency, an overlay with no text, an off-palette PNG with no notes,
  a 640x64 strip); all six are refused. None of this measures a real model, and the real figure must be measured
  again when one is chosen.
- **The live test** (generated image against the brand's own image against text only, read against the holdout) is
  not started.
- Currency symbols are known for USD, INR, EUR and GBP; other codes print as `CHF 12.50`. Digits use western
  grouping (`1,299.50`), not the Indian lakh style.

## How it was checked

All run in the worktree on branch `v3/images`:

- `python -m pytest tests/v3/images -o addopts="" -q`: 89 passed in 2.77 s (after the review fixes; 61 before).
- `python -m pytest tests/v2/formats/test_assets.py tests/v2/formats/test_format_content.py -o addopts="" -q`
  (they cover `autonomy/assets/library.py` and `support.py`, which gained the `offer_banner` kind): 49 passed.
- `ruff check`, `black --check` and `mypy --strict --python-version 3.11 --ignore-missing-imports --follow-imports=silent`
  on every file added or changed: clean.
- `python -m pytest tests/v2/api/test_api.py -k openapi`: passed (the published schema is unchanged because the routes
  are out of the schema).

The full v2 suite was not run here (it runs in CI after integration).
