# v2 Formats: choosing what a message looks like, safely

Decision 5 of the plan is **format**: should this person get plain text, text with a product
image, a carousel, or a video? This page covers how we do that without ever showing a customer
something nobody at the brand has seen. The code lives in `autonomy/assets/`.

## 1. Safe first: approved assets now, generated media later

| Stage | What the agent may use | Gate |
|---|---|---|
| **Now (v2.0)** | Text, and images, carousel cards and videos **the brand uploaded and approved** | `Asset.status == approved` and an `approved_by`; `validate()` must find no error |
| **Later** | Generated images (a product on a new background, a seasonal banner) | Each image goes through the Critic, then a person approves it, then it enters the library as an ordinary approved asset |
| **Much later** | Generated video | Same path as images, once images have a clean record |

Why in this order:

- **A bad image does more harm than a bad sentence.** The Critic can read text for false claims
  and tone; it cannot yet judge an image reliably (a wrong product, an odd hand, a logo misused).
- **The brand already owns good assets.** Product shots and campaign images exist; using them
  well is most of the value of format at no new risk.
- **One path in.** Generated media will not get a side door. It will land in the same library,
  with the same `status` and `approved_by`, so everything downstream (lookups, validation,
  rendering, the audit trail) stays as it is.

The agent **never** generates an image or a video in the current code, and nothing in the
per-customer loop calls an LLM to pick one.

## 2. The asset library

An `Asset` (`autonomy/assets/library.py`) is one image, video or carousel card:

| Field | Why it is there |
|---|---|
| `asset_id` | Stable id; also the file name in a JSON directory, so it is limited to safe characters |
| `kind` | `image`, `video` or `carousel_card` |
| `url` | Where the channel fetches it; must be https |
| `alt_text` | **Required.** Read aloud to screen-reader users and shown when images are blocked |
| `width`, `height`, `size_bytes`, `mime_type`, `duration_s` | For channel limits and shape guidance (`duration_s` is for videos only) |
| `item_id`, `category`, `themes` | How the agent finds a fitting asset: the product, its category, the message theme |
| `group_id`, `position` | Carousel cards: which set and in what order |
| `status`, `approved_by` | `approved`, `draft` or `retired`; an approved asset must name who approved it |
| `created_at` | Passed in, never read from the clock |

Two implementations share the `AssetLibrary` protocol (`put`, `get`, `find`):

- `InMemoryAssetLibrary` for the Gym and tests.
- `JsonDirAssetLibrary`: one `<asset_id>.json` per asset. A brand's library can be reviewed in a
  pull request, edited by hand, and is re-read on every lookup. Broken files fail loudly.

Rules that hold for both:

- `find()` returns **approved assets only** unless the caller passes `approved_only=False`.
- `get()` returns any status, so a past decision's asset can still be shown in the audit view.
- Results are sorted by `asset_id`, so the same library always gives the same answer.
- Assets are retired, not deleted, so old decisions still point at something real.

Helpers: `candidates(library, kind=..., item_id=..., category=..., theme=...)` lists fitting
approved assets **most specific first** (the product itself, then its category, then the theme).
The broader tiers never offer a picture of a different product, or one filed under a different
category: a message about one shoe never shows another. An empty list means "no fitting asset":
send text. `carousel_set(library, group_id)` returns a
carousel's approved cards in card order.

## 3. The support matrix

What each channel can carry (`autonomy/assets/support.py`, `SUPPORT`). Hard limits are errors
(the provider rejects the file, or the send would break a rule); guidance is a warning (it
sends, but may look poor).

| Channel | text | text_image | carousel | video |
|---|---|---|---|---|
| **Email** | yes | JPEG/PNG/GIF; guidance: under 1 MB, at least 600 px wide | yes, as **stacked images** (2 to 10), same guidance per image | no |
| **WhatsApp** (template) | yes | header image: JPEG/PNG, **max 5 MB**; guidance 1.91:1 | **only with the `carousel_template` flag**: 2 to 10 cards, JPEG/PNG max 5 MB, **all one shape**; guidance 1:1 or 1.91:1 | header video: MP4/3GPP, **max 16 MB**; guidance 16:9, under 60 s |
| **SMS** | yes | no | no | no |
| **Push** | yes | JPEG/PNG, **max 1 MB** (Firebase); guidance 2:1 | no | no |
| **Web** | yes | JPEG/PNG/GIF/WebP; guidance under 1 MB | no | no |
| **Webhook** | yes | yes | yes | yes (the client's tool renders it; we pass the URLs) |

Product-level formats map onto these four: a *product image* is `text_image` with the item's
own asset; a *comparison* is a `carousel` (or one prepared comparison image); *review / social
proof* is the `social_proof` theme in text or with an approved image; *hybrid* is `text_image`.

### The API

- `formats_for(channel, carousel_template=False)`: the formats a channel can carry, text first.
- `supports(channel, fmt, carousel_template=False)`, `spec_for(channel, fmt)`.
- `validate(asset, channel, fmt=None, carousel_template=False) -> list[Problem]`: every problem,
  as data (`code`, `severity`, `message`, `asset_id`). `fmt` defaults to the asset's natural
  format (image: text_image, video: video, card: carousel).
- `validate_carousel(cards, channel, carousel_template=False)`: each card, plus card count and,
  on WhatsApp, matching shapes.
- `blocking(problems)` and `is_sendable(...)`: only the errors.

What `validate` checks, in plain words:

| Check | Severity |
|---|---|
| The channel cannot carry the format, or it needs the carousel flag | error |
| The format carries no asset (text), or the asset is the wrong kind | error |
| Not approved (draft or retired) | error |
| Alt text blank | error |
| URL not https | error |
| File type not accepted (JPEG/PNG only on WhatsApp and push) | error; unknown type is a warning |
| Over a hard size limit | error; over guidance or unknown size is a warning |
| Video over a hard length limit | error; over guidance or unknown length is a warning |
| Aspect ratio far (more than 10%) from the channel's guidance, or narrower than suggested | warning |
| Carousel with fewer than 2 or more than 10 cards; WhatsApp cards of different shapes | error |

The Creative uses `formats_for` and `candidates` to label content items with a format and an
asset; the Critic runs `validate` and rejects items with any error; the renderer checks again
at send time, because an asset can be retired between writing and sending.

## 4. How the agent will learn format

Format becomes **one more option the agent chooses**, like channel and offer, and learned the
same way (the decide stream wires this in later):

1. **The option set is safe by construction.** For a customer and channel, the arms are
   `formats_for(channel)` filtered to formats that have at least one sendable approved asset
   (`candidates` + `is_sendable`). Text is always an arm, so there is always a fallback.
2. **A bandit over labels.** `ContentItem.format` and `Action.format` / `Action.asset_id` are
   logged with every decision, so Thompson sampling can learn format by segment and channel,
   and what is learned for one goal carries over to others, just as theme and tone do.
3. **Costs count.** A rich format may cost more (a WhatsApp media template, design time), so it
   wins only when its extra uplift pays for it, the same net-value rule as channels.
4. **Every choice has a probability**, so the proof stream can measure what format caused.
5. **Assets are not the arm.** We learn "text_image works for lapsed shoe buyers", not "asset
   47 works"; within a format the most specific approved asset is used. That keeps learning
   fast with few customers and lets the brand swap images without losing what was learned.

## 5. What is not here yet

- Generated images or video (section 1).
- MMS, rich SMS and in-email video.
- Automatic image inspection (reading real dimensions and size from the file). For now they
  come from the upload; when missing, `validate` warns rather than guesses.
