# Mandates: the governance plane

An autonomous marketer needs one place that says what it may never do. In v2 that place is the **mandate**: a versioned, signed statement of the hard limits on autonomous action for one business. No goal, brief, bid or batch can go past it. The code is in `autonomy/mandate/` (`models.py`, `signing.py`, `enforce.py`, and `catalogue.py` for the Action Catalogue); the HTTP routes are in `api/v2/engines/mandates.py`.

## What a mandate is

`Mandate` (frozen pydantic, unknown keys rejected) holds:

| Field | Meaning |
|---|---|
| `mandate_id`, `business_id`, `goal_ids`, `version` | Which business and goals it governs, and which version this is (from 1) |
| `budget_ceiling`, `currency` | Total spend allowed over the validity window |
| `max_daily_spend` | Spend allowed per UTC calendar day (optional; never above the ceiling) |
| `margin_floor_pct` | An action's expected margin after the offer's cost, as a share of revenue, must stay at or above this (0.2 = 20%) |
| `quiet_hours` | `start`, `end` (local hours; may wrap midnight; equal means none) and a fallback `timezone` for customers whose own zone is blank or invalid |
| `frequency_cap` | `max_touches` in any `window_days` days (1 to 30, because customer state keeps 30 days of send times) |
| `channels` | Channels the agent may use (at least one) |
| `approved_template_ids` | Content ids that may be sent. An action without a content id is refused |
| `approved_offer_ids` | Offer ids that may be used. "No offer" is always allowed |
| `cohort_exclusions` | `AudienceFilter`s; a customer who matches **any one** is never contacted (same field and op rules as brief audiences, `autonomy/decide/audience.py`) |
| `valid_from`, `valid_until` | The validity window, `[from, until)` |
| `signers` | The signatures collected so far |
| `status` | `draft`, `pending_signatures`, `active`, `expired`, `revoked` |

All times are normalised to UTC when the model is built, so the same moment written in two time zones hashes the same.

A mandate's **body** is every field except `signers` and `status`. Changing any limit means a new body, and that needs a new version and new signatures. Revoking or expiring a mandate changes only its status, so its old signatures stay checkable.

## Signing

`body_hash(mandate)` is the SHA-256 of the body as canonical JSON (keys sorted, no whitespace, UTF-8, tuples as lists, times in UTC ISO 8601). A signer signs `signature_message(...)`: the body hash plus the person, role, key id, algorithm and time. A signature therefore cannot be moved to another mandate, another version, another role or another person.

Two algorithms are supported:

- **Ed25519** (`Ed25519Signer`), when the `cryptography` package can be imported. Verifiers hold only public keys, so the server that checks a signature cannot forge one. This is the target.
- **HMAC-SHA256** (`HmacSigner`), with the standard library only and one secret per signer. The light v2 CI environment does not install `cryptography`, so this is what runs there. Its weakness is that whoever verifies holds the secret and could sign too.

**Upgrade path from HMAC to Ed25519.** `cryptography` is already pinned for v1. Give each signer an Ed25519 key, held on their own device or in a KMS. Register the public key with `KeyRing.add_ed25519`, and new signatures will carry `algorithm="ed25519"`. Nothing else changes: the signature record, the signed message and the ledger all stay the same, and old HMAC signatures stay checkable for as long as their secrets are kept. If `cryptography` is missing, an Ed25519 signature counts as not verified. It never counts as verified.

**Keys belong to people.** A `KeyRing` binds each key id to one person and to the roles that key may sign in. A signature verifies only when all of these hold:

- the key is known;
- the signature's `person_id` is the key's owner;
- the signature's role is one the key may use;
- the signature bytes check out.

Without this binding, one person holding two keys could pass the two-person rule alone.

## Who has to sign: the two-person rule

`SignaturePolicy` sets who must sign:

- a **marketing lead** signs every mandate (`always_required`);
- when `budget_ceiling` is above `finance_threshold` (10,000 by default), a **CFO or finance** signer signs as well;
- each required role is filled by a **different person**, so one person holding both the marketing-lead and CFO roles cannot fill both;
- at least `min_distinct_signers` different people sign (2 by default). Even a small mandate needs a second pair of eyes, for example an `approver`.

`policy.unmet(mandate)` returns the unmet codes (`missing_role:marketing_lead`, `missing_role:finance`, `two_person_rule`). `verify_mandate(mandate, policy, keys)` first checks the cryptography (`body_hash_mismatch`, `bad_signature`), then applies the policy to the good signatures only. An empty list means the mandate is authorised.

`with_signature(mandate, sig, policy=..., keys=...)` adds one verified signature. The mandate becomes `pending_signatures` until the policy is met, and `active` once it is. It refuses:

- a signature that does not verify against this body;
- a second signature from the same person in the same role;
- any signature on a revoked or expired mandate.

## The ledger

`MandateLedger` is append-only and hash-chained. It follows the approach of v1's `engine/agentic/ledger.py`, reimplemented with the standard library so the v2 core stays light. Each entry stores a full snapshot of the mandate at that moment (created, submitted, signed, activated, revoked, expired, superseded):

    entry_hash = sha256( prev_hash || canonical({seq, kind, actor, at, mandate_id, version, body_hash, mandate}) )

The first entry links to 64 zeros. `append` refuses a changed body that does not carry a higher version. `verify_chain(keys=None)` walks the chain and reports the first bad entry with one of these codes:

| Code | What went wrong |
|---|---|
| `seq_gap` | An entry was dropped or the sequence was reordered |
| `chain_broken` | `prev_hash` does not match the previous entry's hash, for example after a rewritten history |
| `hash_mismatch` | A field of the entry was edited |
| `snapshot_mismatch` / `body_hash` | The stored snapshot disagrees with the entry |
| `version_regressed` | A body changed without a higher version |
| `bad_signature` | With `keys`, some signature in some snapshot does not verify |

`ledger.head` is the latest entry hash. A client can keep it as a receipt: if the history is later rewritten, even with every hash recomputed, the receipt no longer matches.

## Enforcement

`check_action(mandate, action, costs, state, now, *, spend=None, pending_touches=0)` is pure maths. It takes no LLM call, and time is passed in. It returns **every** violation, not just the first, so a person reviewing a blocked action sees all the reasons at once:

| Code | Rule |
|---|---|
| `mandate_not_active` | Status is not `active`, or the mandate is past `valid_until` (`effective_status`) |
| `mandate_outside_validity` | `now` or the action's `send_at` falls outside `[valid_from, valid_until)` |
| `mandate_goal_not_covered` | The action's goal is not in `goal_ids` |
| `mandate_channel_not_allowed` | The channel is not in `channels` |
| `mandate_template_not_approved` | The content id is missing or not approved |
| `mandate_offer_not_approved` | The offer is neither "none" nor approved |
| `mandate_quiet_hours` | `send_at` falls in quiet hours in the customer's own time zone (the mandate's zone is the fallback) |
| `mandate_frequency_cap` | The touches in the window ending at `send_at`, plus `pending_touches`, plus this one, exceed `max_touches`. The count comes from `recent_contacts`. When `contacts_7d` / `contacts_30d` report more sends than that list explains, the unexplained ones are counted whenever the window overlaps their span, so missing send times never let a customer through |
| `mandate_margin_floor` | `(margin - offer cost on a converting order) / revenue` is below the floor. If revenue or margin is unknown, the action passes only when it has no offer cost |
| `mandate_budget_ceiling` | `spend.total` plus this action's cost exceeds the ceiling |
| `mandate_daily_spend` | `spend.today` plus this action's cost exceeds `max_daily_spend` |
| `mandate_cohort_excluded` | The customer matches a cohort exclusion |

`costs` is an `ActionCosts` (offer cost, channel cost, and the revenue and margin if the action converts) or a decide `Bid`. `ActionCosts.offer_cost` is what counts against spend; `offer_cost_if_converted` is the offer's cost on an order that converts, which is what the margin floor uses (it defaults to `offer_cost`).

`check_bid(mandate, bid, state, now, offers=brief.envelope.offers)` is the same check for a bid. It uses the bid's `value_if_converted` as the margin and the customer's average order value as the revenue. A bid's `offer_cost` is the *expected* cost (cost per redemption times the chance of redemption), which is right for spend but far too small for the margin, because the customer who converts redeems the offer. So the margin is checked with the offer's full `cost_per_redemption` from `offers`, and a bid whose offer is not described there is reported as `mandate_margin_floor`. `check_action_against_mandate` is another name for `check_action`.

**Pacing.** `TokenBucket.for_mandate(mandate, now)` paces spend under `max_daily_spend`. The bucket holds a `burst_share` of the daily cap (a quarter by default) and refills the rest evenly over 24 hours. As a result, **no rolling 24 hours spends more than the daily cap**. The bucket is immutable: `try_spend(amount, now)` returns `(allowed, new_bucket)`. `wait_seconds` says when an amount can next be spent, or returns `None` if it never can. If `now` is earlier than the last update, the bucket adds nothing.

## How decide and the runtime will use it

Integration happens in those streams. This package only provides the functions.

1. **Envelope.** `mandate_to_envelope(mandate, offers, base=brief.envelope, spent=...)` gives the `Envelope` that the existing decide rules already read, so they keep working unchanged:
   - channels are the mandate's, and only those the base also allows (none in common is an error);
   - offers are the mandate's approved ones that the base also lists, with the base's description (so its `needs_approval` stands); "no offer" is always kept;
   - the budget is the ceiling minus spend so far, or the base's budget if that is smaller;
   - the daily spend is the smaller of the mandate's and the base's;
   - the quiet hours cover both the mandate's and the base's window. An envelope holds one window, so two windows that do not touch become the smallest single window covering both (quieter than either, never louder);
   - the weekly cap is never looser than the frequency cap in any 7 days, nor than the base's.

   So the result is never looser than either the mandate or the brief's envelope. The base still supplies what a mandate does not govern: the exploration floor, the holdout and baseline shares, and the stop rules. The envelope is a coarse projection. It has no margin floor, template list or cohort exclusions, and no exact frequency window.
2. **Decide.** After its own hard rules, `decide()` calls `check_bid` on each candidate bid and adds the codes to `Decision.blocked_by`. The `mandate_` prefix keeps these codes apart from decide's own codes.
3. **Dispatch.** The dispatcher calls `check_action` once more just before sending, with the spend so far and the pending touches, and draws the action's cost from the mandate's `TokenBucket`. Anything that fails is held rather than sent.
4. **Lifecycle.** The runtime records every change in the `MandateLedger`: created, each signature, activated, revoked, expired, and a new version that supersedes the old one. It acts only on a mandate for which `verify_mandate(...) == []` and `effective_status(now) == active`.

## The Action Catalogue

A mandate says *how much* the agent may do. The catalogue (`autonomy/mandate/catalogue.py`) says *with what*: the offers it may make (`OfferOption`), each channel's price (`ChannelRate`: channel, unit cost, currency, provider) and the provider templates that were approved (`CatalogueTemplate`). Both have to hold for an action to go out.

The catalogue is never edited in place. `ActionCatalogue.publish(content, created_by, now)` writes version N+1, records what changed (`diff_from_previous`: each offer, rate or template added, removed or changed, with the changed fields) and chains each version to the one before by hash, using the same canonical JSON as mandate hashes. `verify_history()` lists any version that was changed or broken out of the chain. A publish that changes nothing, or that predates the version before it, is refused. Two stores: `InMemoryCatalogueStore`, and `JsonDirCatalogueStore` (one file per version, written once and never overwritten).

`check_action_against_catalogue(version, action, currency=mandate.currency)` returns these codes, separate from the `mandate_` ones:

| Code | Rule |
|---|---|
| `catalogue_template_unknown` | The action's content id is not a template in this version |
| `catalogue_template_channel_mismatch` | The template is approved for a different channel |
| `catalogue_offer_unknown` | The offer is neither "none" nor in this version |
| `catalogue_channel_unpriced` | The channel has no rate |
| `catalogue_currency_mismatch` | The channel has rates, but none in the mandate's currency |

`catalogue_costs(version, action, currency=...)` gives the listed (offer cost, channel cost). Rates in another currency are never mixed into a mandate's spend.

## HTTP routes (`/mandates`)

The router holds no rules of its own: it parses, calls `autonomy.mandate` and wraps the answer. State lives in a `MandateRegistry` (signature policy, per-currency policy overrides, key ring, catalogue, ledger, clock), injected through `get_mandate_registry` so tests can swap it. Each body may carry `now`; otherwise the registry's clock supplies it.

| Route | What it does |
|---|---|
| `POST /mandates` | Draft a mandate (201). Recorded in the ledger. Templates and offers are checked against the pinned (or latest) catalogue |
| `POST /mandates/{id}/sign` | Add one signature: either carried (`signature`, `key_id`, `signed_at`, `algorithm`) or made with a server-held key. 403 if it does not verify or the signer is not listed on the draft, 409 on a repeat. Signing alone never activates a mandate |
| `POST /mandates/{id}/certify` | 201 and `active` when `verify_mandate` passes; otherwise 409 with the unmet codes and an `awaiting` list of the listed signers who can fill each one |
| `GET /mandates/{id}` | The mandate, its body hash, unmet requirements, expected signers, catalogue version and ledger head |
| `GET /mandates/{id}/ledger` | The history and whether the chain verifies |
| `POST /mandates/{id}/check-action` | Every `mandate_` violation from `check_action` plus the `catalogue_` codes. Costs left out are priced from the catalogue |
| `POST /mandates/{id}/revoke` | Revoke; afterwards every check fails with `mandate_not_active` and certify returns 409 |
| `GET /mandates/catalogue?version=`, `POST /mandates/catalogue`, `GET /mandates/catalogue/diff?a=&b=`, `GET /mandates/catalogue/history` | Read, publish (201; 409 when nothing changed; 422 on duplicate ids), diff and list catalogue versions. `catalogue` is refused as a mandate id |

Input rules for `POST /mandates`:

- **Budget.** Give exactly one of `budget_ceiling_inr` (INR) or `budget_ceiling`, either a number with `currency` or `{amount, currency}`. A bare number with no currency is refused (422), not guessed: 500,000 is a very different ceiling in INR and in USD. A `currency` that disagrees with `budget_ceiling.currency` is refused too.
- **`margin_floor_pct` is a percentage here** (18 means 18%); the model stores it as a share (0.18).
- **Quiet hours** take `21` or `"21:00"`; `"21:30"` is refused rather than rounded.
- **Frequency cap** takes `max_touches` or its alias `max_per_customer`; both with different values is refused.
- Channels default to the approved templates' channels. The validity window defaults to 30 days from `now`.

**Calling check-action with an offer.** When `costs` is left out, an offer is priced at its full `cost_per_redemption`, and the margin floor fails any action with an offer cost but no known revenue or margin. Pass `costs` with `revenue_if_converted` and `margin_if_converted` for an offer action, or it will come back `mandate_margin_floor`.

**Finance threshold and currency.** The default `SignaturePolicy` threshold (10,000) does not know about currency. The registry takes a policy per currency; production should set an INR one.

**Not yet supported.** Amending a mandate (a new version with a `superseded` entry): `POST /mandates` with an existing id returns 409. A revoke reason: the ledger entry has no field for one, and the mandate's `note` is signed content, so writing it there would break every signature. The registry is in memory.
