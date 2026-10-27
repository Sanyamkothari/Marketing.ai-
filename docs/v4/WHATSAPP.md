# WhatsApp readiness for India (v4)

This closes the WhatsApp gaps in `docs/v3/ARCHITECTURE_REVIEW.md` (rows 2, 3 and 20, and fix 6) and
in `docs/v3/CMO_REVIEW.md` (worry 6: "WhatsApp 131049 cap handling"). Everything below runs against
fakes. No Meta account was used, so section "Needs a real Meta account" lists what is still unverified.

Settings live in two files, so tuning or adding a language needs no code:

- `configs/v4/whatsapp.yaml`: the YELLOW budget factor, whether RED pauses, what each Meta error code means
  (label, counts as failure or not, hours the customer is held), and which template categories are usable.
- `configs/v4/optout_phrases.yaml`: opt-out phrases, one list per language.

## 1. WhatsApp is valued from purchases, never from clicks

**The problem.** The Cloud API reports sent, delivered, read and failed, never a click. The click model
counted every WhatsApp send as a clickable send that never got a click, so WhatsApp's predicted value
fell toward zero (the review's probe: 0.05 to 0.0005 after 2,000 sends). Only a channel with 200+
historical sends and no click was switched to the per-message unit (`NO_CLICK_MIN_SENDS`).

**What changed** (`autonomy/runtime/agent.py`, small block next to `NO_CLICK_MIN_SENDS`):

- `NEVER_CLICK_CHANNELS = {"whatsapp"}`. `ClickResponseModel.no_click` starts with it, and `observe_click`
  never removes it. WhatsApp's uplift is "extra purchases per delivered message", the same unit SMS
  already used, measured against the holdout's purchases in the window (`LiftPerClick` with `kind=SEND`).
- A CLICK event on a WhatsApp decision (for example from an outside link tracker) is ignored by the lift
  estimate, the click model and the reading-hour model.
- Read receipts (`OPEN`) are not used at all. A read says the message was seen, not that it moved anyone,
  and a weak signal that is not validated would only add a second thing to get wrong. If a read signal is
  wanted later it should be tested against holdout purchases first.
- Email and SMS are unchanged: their `no_click` set still starts empty and still fills from history.

**Effect on the Gym.** The Gym plants clicks on every channel, so WhatsApp runs there now ignore those
clicks and value WhatsApp per message. The size of that change on Gym results is pending CI:
`python -m autonomy.gym.run --scenario <scenario> --policy agent --seeds 3 --customers 3000` on the
`v4/f-whatsapp` branch against its base commit (the commands in `.github/workflows/gym.yml`).

Tests: `tests/v4/whatsapp/test_no_click.py`.

## 2. Template sync, and a send naming a non-approved template is refused

- `WhatsAppCloudAdapter.list_templates()` reads `GET /{waba_id}/message_templates` (fields `name, language,
  category, status, components`, 100 per page) and follows `paging.next`, but only to a URL on the Graph
  API, so the access token is never sent elsewhere. `sync_templates(now)` replaces the adapter's
  `TemplateCatalogue` with the result. New constructor argument: `waba_id`.
- Usable means status `APPROVED` and category `MARKETING` or `UTILITY`. `AUTHENTICATION` templates are for
  one-time codes and are never sent.
- `TemplateCatalogue.drafts(now)` turns the usable templates into `DRAFT` content items (stable ids, so a
  re-sync upserts). They keep Meta's `{{1}}` placeholders and name no slots: a person maps each parameter to a
  customer field, and the Critic reviews the approved body, before an item can be approved and sent.
- **Refused before dispatch, at two points.** `WhatsAppCloudAdapter.send` returns an unaccepted receipt
  `template_not_approved: ...` before making any request. The dispatcher's `WhatsAppGuard` skips the
  decision with reason `template_not_approved`, so nothing is handed to the adapter and no FAIL is written
  (a refusal of our own is not a delivery failure). A catalogue that was never synced refuses nothing
  (Meta stays the only judge), so existing setups behave as before until a sync runs.
- `message_template_status_update` webhooks update the catalogue at once: PAUSED, DISABLED, REJECTED and
  PENDING stop a template; APPROVED and REINSTATED allow it again. FLAGGED and IN_APPEAL change nothing.

Wiring left to the runtime: call `sync_templates` at start-up and on a schedule (hourly is plenty), and
build the dispatcher with `guards={Channel.WHATSAPP: WhatsAppGuard(adapter.templates, adapter.health,
language=adapter.language)}`. Nothing in `service.py` or `api/v2/deps.py` was edited (other streams edit them).

Tests: `tests/v4/whatsapp/test_templates.py`.

## 3. Quality rating, messaging tier and Meta's error codes

**Quality and tier** (`autonomy/channels/whatsapp_health.py`):

- `phone_number_quality_update` webhooks and `get_phone_status()` (`GET /{phone_number_id}?fields=
  quality_rating,messaging_limit_tier`) update `WhatsAppHealth`. A number never heard about is GREEN.
- YELLOW halves the weekly WhatsApp budget: the dispatcher guard allows `max(1, int(cap x 0.5))` WhatsApp
  sends per customer per rolling 7 days, where `cap` is the strictest `max_contacts_per_week` of the goal's
  briefs. Over the budget the decision is skipped with `quality_budget`. This is enforced at dispatch; the
  policy still plans with the full cap, so on YELLOW some WhatsApp decisions are made and then skipped
  rather than planned around. Making the policy plan with the halved cap is a follow-up.
- RED pauses WhatsApp for the shop: every WhatsApp decision is skipped with `quality_paused` while the
  rating is RED, email and SMS go on. An `Alert` (`whatsapp.quality_red`, critical, `auto_paused`) is raised once
  per turn to RED and handed out by `adapter.drain_alerts()`. A person resumes by calling
  `health.reset()` after the cause is fixed (or the next report says otherwise).
- The webhook also emits an account-level event (customer `whatsapp_account`, `attrs.quality_rating`), which
  the existing `whatsapp_quality` stop rule in `breakers.py` already reads, so a brief that carries that
  rule pauses the channel through the breaker path too.

**Error codes** (`configs/v4/whatsapp.yaml`; from a status webhook or from the send response):

| Code | Meaning | Recorded as | Customer on WhatsApp | Breaker |
|---|---|---|---|---|
| 131049 | Meta withheld a marketing message to keep the person's inbox healthy | FAIL, `not_delivered = "not delivered: Meta limit"` | not retried for 24 h | not counted |
| 131026 | The person cannot receive it (not on WhatsApp, old app, terms not accepted) | FAIL, "not delivered: recipient cannot receive WhatsApp" | not retried for 168 h | not counted |
| 131047 | Free-form message outside the 24-hour window: our bug, a template was needed | FAIL, "not delivered: needs a template ..." | no hold | counted |
| 131050 | The person stopped marketing messages from us | BLOCK (unchanged) | consent withdrawn | as before |

"Not counted" means the FAIL carries `breaker_ignore = true`. Both breakers skip it: `channels/breakers.py`
(`delivery_rate_below` leaves the message out of the rate) and the runtime `Breaker.record` in
`runtime/agent.py`. The hold is a `hold_until` attr on the FAIL; the guard skips a WhatsApp decision for that
customer with `provider_hold` until then. Email to the same person is not held.

The 168 h hold for 131026 and the 0.5 factor are my defaults, not Meta's numbers; tune them in the config.

Tests: `tests/v4/whatsapp/test_quality_limits.py`.

## 4. Opt-outs in Indian languages

- `autonomy/channels/optout.py` loads `configs/v4/optout_phrases.yaml`. A reply opts out when, after
  lower-casing, Unicode normalisation and dropping punctuation, it is one of the phrases or contains one as
  whole words ("please band karo ab"). The whole message is also compared with its spaces removed, so
  "bandkaro" matches. Words are cut on letters, combining marks and digits, so Devanagari vowel signs stay
  inside their word.
- Listed: STOP, unsubscribe, band karo, band kar do, mat bhejo, nahi chahiye, बंद करो, मत भेजो, नहीं चाहिए,
  रोको, plus spelling variants (band kardo, nahin chahiye, बंद कर दो, ...), opt out, dnd and roko.
  A phrase inside a longer sentence counts, the same call the English net already makes: a missed opt-out
  is a legal problem, a wrong one costs one customer's messages. "chahiye" or "band" alone do not match.
- **Immediately, in the webhook.** For an inbound WhatsApp text or quick-reply button that matches, the adapter
  returns the REPLY plus an UNSUBSCRIBE and a CONSENT_REVOKED event (reason `reply_optout_instant`, stamped
  with the reply's time, `attrs.address` = the sender). `DispatchGateway.handle_webhook` already suppresses
  the address and revokes its consent proofs for an UNSUBSCRIBE, so the number is blocked and the consent
  record shows `revoked_at` before the webhook response returns, without waiting for the Conversation agent.
- The Conversation agent's hard keyword net (`stop_keyword`) reads the same lexicon, so a reply that reaches
  it by another route also resolves to "stop" and unsubscribes every channel as before.
- An existing test expected the "Stop promotions" quick-reply fixture to yield one REPLY; it now yields the
  REPLY, UNSUBSCRIBE and CONSENT_REVOKED (`tests/v2/channels/test_providers.py`).
- A new language: add a key under `languages:` in the YAML. `tests/v4/whatsapp/test_optout.py` proves this with
  a temporary file.

Tests: `tests/v4/whatsapp/test_optout.py`.

## What was run

All with the shared venv, from the worktree root.

- `python -m pytest tests/v4 -o addopts="" -q`: 78 passed (the four files above).
- Tests covering the touched modules: `tests/v4`, `tests/v2/channels`, `tests/v2/dispatch_pro`,
  `tests/v2/agents/test_analyst_conversation.py`, `tests/v2/realfix/test_brain_calibration.py`,
  `tests/v2/agent_brain/test_per_person.py`, `tests/v2/contact_less/test_restraint.py`,
  `tests/v2/wire_agent/test_items_formats.py`, `tests/v3/currency/test_currency.py`,
  `tests/v2/runtime/test_service.py`, `tests/v2/agent_gym/test_agent_in_gym.py`,
  `tests/v2/dogs_storm/test_storm_reasons.py`, `tests/v3/frequency/test_plans.py` with `-m "not slow"`:
  478 passed.
- `ruff check`, `black --check` on the changed files; `mypy --strict` on the changed `autonomy` files: clean.
- `tests/v4` was added to the v2 workflow (`.github/workflows/v2.yml`: branch filter, paths, ruff, black, pytest).

Not run here (CI): the whole `tests/v2` and `tests/v3` suites, and the Gym comparison described in section 1.

## Needs a real Meta account to verify

These behaviours follow Meta's public documentation as I know it, not an observed account:

1. **Template list shape.** Field names, the `paging.next` link and the `limit` parameter of
   `GET /{waba_id}/message_templates`; whether the access token needs `whatsapp_business_management`.
2. **Template language.** Sends use the adapter's one `language` (as before); a template approved only in
   another language is treated as not approved. Multi-language shops need a language on the `Message`.
3. **`phone_number_quality_update` payload.** The code reads `event` (FLAGGED, UNFLAGGED, UPGRADE, DOWNGRADE,
   ONBOARDING), `current_limit`, and an explicit `quality_rating` or `current_quality_rating` if present. If
   the payload has no rating, FLAGGED is taken as RED (Meta's low-quality state) and UNFLAGGED as YELLOW until
   `get_phone_status` reads the real rating. Whether FLAGGED is exactly RED, and whether Meta sends a rating
   in this webhook at all, is unverified. Call `get_phone_status` on a schedule as the backstop.
4. **`GET /{phone_number_id}?fields=quality_rating,messaging_limit_tier`** and the value spellings (GREEN,
   YELLOW, RED, NA; TIER_250 ... TIER_UNLIMITED).
5. **131049, 131026, 131047.** That each arrives as a `failed` status with `errors[0].code`, and not only
   (or also) synchronously in the send response; the code handles both. The 24 h hold matches the spec,
   but whether Meta's own retry window is shorter or longer is not known. 131047 appears only if a
   free-form message is sent, which this code never does.
6. **Quick-reply buttons.** Whether the "Stop promotions" button's `button.text` and `payload` arrive as in the
   fixture on a live number, and whether Meta's own block action also surfaces as 131050 for the next send.
7. **Opt-out spellings.** The phrase list is a starting set. Real replies from a pilot will show the
   spellings missing (Tamil, Bengali, Marathi, Telugu and so on are not included); add them in the YAML.
8. **Rating to budget mapping.** The 0.5 factor for YELLOW and RED pausing are product rules from the brief; how
   quickly a number recovers from YELLOW and whether pausing on RED is too cautious needs a pilot.
9. **Alerts.** `drain_alerts()` is not yet called from the runtime tick, so the RED alert reaches the inbox only
   once the service is wired to hand it over (the dispatcher pause itself works without it).
