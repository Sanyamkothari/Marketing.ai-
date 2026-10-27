# WhatsApp readiness for India (v4)

This closes the WhatsApp gaps in `docs/v3/ARCHITECTURE_REVIEW.md` (rows 2, 3 and 20, and fix 6) and
in `docs/v3/CMO_REVIEW.md` (worry 6: "WhatsApp 131049 cap handling"). Everything below runs against
fakes. No Meta account was used, so section "Needs a real Meta account" lists what is still unverified.

Settings live in two files, so tuning or adding a language needs no code:

- `configs/v4/whatsapp.yaml`: the YELLOW budget factor, whether RED pauses, what each Meta error code means
  (label, counts as failure or not, hours the customer is held), and which template categories are usable.
- `configs/v4/optout_phrases.yaml`: opt-out phrases per language, how each language matches (`contains`
  or `whole_message`), and the filler words a whole-message match may carry.

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
  estimate, the click model and the reading-hour model (live, and in the warm start from history).
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
- Usable means status `APPROVED` and category `MARKETING` or `UTILITY` (`templates.usable_status` and
  `templates.usable_categories` in `configs/v4/whatsapp.yaml`; the adapter's catalogue reads them).
  `AUTHENTICATION` templates are for one-time codes and are never sent.
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

**Wiring** (`autonomy/channels/whatsapp_guard.py`):

- `whatsapp_guards(adapters)` builds the guard from a `WhatsAppCloudAdapter` (sharing its catalogue and
  health). `DispatchGateway.attach_dispatcher` uses it for the gateway's WhatsApp providers, and
  `build_runtime` (`autonomy/runtime/settings.py`) uses it for the direct (no gateway) dispatcher. With no
  WhatsApp Cloud adapter the guard map is empty and nothing changes.
- `WhatsAppUpkeep` is a `before_tick` hook that `build_runtime` adds when a WhatsApp Cloud adapter is
  configured: every 6 hours it calls `sync_templates` (only with `AUTONOMY_WHATSAPP_WABA_ID` set) and
  `get_phone_status`, and every tick it hands `drain_alerts()` to the runtime's alert store. A failed call
  becomes the runtime's `runtime.hook_error` alert (once a day) and is retried after 6 hours.
- Not wired: `api/v2/deps.py` (the demo, simulated adapters) and `autonomy/runtime/demo.py`.
- Template status webhooks update the catalogue of the process that takes the webhook (the API). A runtime
  in another process sees the change at its next sync, so up to 6 hours late; the send-time refusal by
  Meta stays the backstop in between.

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
- RED pauses WhatsApp for the shop: every WhatsApp decision is skipped with `quality_paused`, email and
  SMS go on. The pause is a latch: a later GREEN or YELLOW report (including UNFLAGGED) updates the rating
  but does not lift it; only `health.reset(at)` does, when a person resumes WhatsApp after fixing the cause.
  An `Alert` (`whatsapp.quality_red`, critical, `auto_paused`) is raised once per turn to RED, handed out
  by `adapter.drain_alerts()` and put in the alert store by `WhatsAppUpkeep`.
- The webhook also emits an account-level event (customer `whatsapp_account`, `attrs.quality_rating`,
  `attrs.account_level = true`). The guard reads the latest such event from the event log before each
  check, so a RED report taken by the API process pauses the runtime process's sender too, and survives a
  restart. A RED report from before the last `reset(at)` does not pause again. The reset is held in
  memory, so after a restart a stored RED report (with no newer report) pauses again until a person
  resumes: the safe direction. The existing `whatsapp_quality` stop rule in `breakers.py` reads the same
  event. `autonomy/data/rebuild.py` skips `account_level` events, so no customer state is made for
  `whatsapp_account`.
- A quality webhook naming another number of the same business account is ignored when the adapter knows
  its own number (`display_phone_number`, given or read by `get_phone_status`). Before the first
  `get_phone_status`, with no number given, every report on the account is applied.
- The default stop rule (`contracts/goals.py`) pauses WhatsApp on RED (threshold 2). YELLOW halves the weekly
  budget instead of pausing (changed at integration; it was threshold 1, which paused on YELLOW).

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
customer with `provider_hold` until then. Email to the same person is not held. Holds are read back over
the longer of a week and the longest `hold_hours` in the config; a `hold_until` that does not parse is
treated as no hold (it never stops the batch).

The 168 h hold for 131026 and the 0.5 factor are my defaults, not Meta's numbers; tune them in the config.

Tests: `tests/v4/whatsapp/test_quality_limits.py`.

## 4. Opt-outs in Indian languages

- `autonomy/channels/optout.py` loads `configs/v4/optout_phrases.yaml`. Text is compared after
  lower-casing, Unicode normalisation and dropping punctuation. Words are cut on letters, combining marks
  and digits, so Devanagari vowel signs stay inside their word. The whole message is also compared with its
  spaces removed, so "bandkaro" matches.
- Each language says how it matches. English is `contains` (the phrase anywhere as whole words, the call
  the Conversation agent's English net already makes). Hinglish and Hindi are `whole_message`: the reply
  must be the phrase once filler words are dropped ("please", "mujhe", "ye", "messages", "ab", "कृपया",
  "अब", ...; the `filler_words` list). So "please band karo ab" and "mujhe ye messages nahi chahiye" opt
  out, but "Red nahi chahiye, blue wala bhejo", "price mat bhejo, photo bhejo" and "abhi nahi chahiye, next
  month" (a "not now") do not: they go to the classifier as before. These phrases are ordinary words that
  also describe products, so matching them inside a sentence would unsubscribe customers who were ordering.
- Listed: STOP, unsubscribe, band karo, band kar do, mat bhejo, nahi chahiye, बंद करो, मत भेजो, नहीं चाहिए,
  रोको, plus spelling variants (band kardo, nahin chahiye, बंद कर दो, ...), opt out, dnd and roko.
  "chahiye" or "band" alone do not match.
- **Immediately, in the webhook.** For an inbound WhatsApp text or quick-reply button that matches, the adapter
  returns the REPLY plus an UNSUBSCRIBE and a CONSENT_REVOKED event (reason `reply_optout_instant`, stamped
  with the reply's time, `attrs.address` = the sender). `DispatchGateway.handle_webhook` already suppresses
  the address and revokes its consent proofs for an UNSUBSCRIBE, so the number is blocked and the consent
  record shows `revoked_at` before the webhook response returns, without waiting for the Conversation agent.
- **One UNSUBSCRIBE per channel.** The adapter marks the REPLY with `opted_out = <phrase>`. The
  Conversation agent reads that as stop and writes UNSUBSCRIBE events only for the customer's other
  channels (by default it stops every channel), not a second one for WhatsApp, so the unsubscribe
  breakers count one per opt-out.
- The Conversation agent's keyword net (`stop_keyword(text, channel)`) reads the multilingual lexicon only
  for WhatsApp replies. Email and SMS replies keep the English net alone, as before this stream.
- An existing test expected the "Stop promotions" quick-reply fixture to yield one REPLY; it now yields the
  REPLY, UNSUBSCRIBE and CONSENT_REVOKED (`tests/v2/channels/test_providers.py`).
- A new language: add a key under `languages:` in the YAML. `tests/v4/whatsapp/test_optout.py` proves this with
  a temporary file.

Tests: `tests/v4/whatsapp/test_optout.py`.

## What was run

All with the shared venv, from the worktree root.

After the review fixes (wiring, opt-out matching, one UNSUBSCRIBE per channel, RED latch, holds, config
categories, account-level events; `tests/v4/whatsapp/test_wiring.py` added):

- `python -m pytest tests/v4 -o addopts="" -q -p no:cacheprovider -m "not slow"`: 99 passed.
- `python -m pytest tests/v2/channels tests/v2/dispatch_pro tests/v2/agents/test_analyst_conversation.py
  tests/v2/backbone/test_folder.py tests/v2/failure/test_runtime_wiring.py
  tests/v2/zero_touch/test_wiring_production.py -o addopts="" -q -p no:cacheprovider -m "not slow" -n 2`:
  312 passed.
- `ruff check`, `black --check` and `mypy --strict` on the changed files: clean.

Before the review fixes:

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
9. **Alerts.** `WhatsAppUpkeep` puts the RED alert in the alert store of the process whose adapter saw the
   report. A RED webhook taken by the API process raises its alert in that process, which reaches the store
   only if the API process also runs the upkeep step; the runtime process pauses from the stored event and
   raises its own alert when its guard applies that event.
10. **Quality webhook number.** That `display_phone_number` in `phone_number_quality_update` is the same
    number `GET /{phone_number_id}?fields=display_phone_number` returns (compared as digits).


## Change after the Gym runs (27 Oct)

Valuing WhatsApp from purchases only, for every shop, cost the agent 39% of its value in the Gym's
`whatsapp_fatigue` and 16% in `baseline_shop` (docs/v4/RESULTS.md). It is now an option:
`build_agent_policy(never_click_channels=UNTRACKED_WHATSAPP)`, for shops whose WhatsApp links carry no click
tracking. By default WhatsApp counts tracked clicks like email. A WhatsApp number that reports none is found
by the existing rule: 200 delivered messages without a click, and the channel is then valued per message.
