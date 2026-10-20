# Zero-touch setup: connect once, then a weekly read

**Why this matters most:** most customers will reach Autonomy through a connector (Shopify +
Klaviyo first). If connecting gives useful, proven results with almost no work, that is the
product. Every item below is judged by one question: does it cut the customer's time or raise the
money they see?

**Target:**
- About 15 minutes from "Connect Shopify" to a running Shadow plan.
- About 5 minutes a week after that.
- No daily approvals.

Starts after connectors wave 1 (`v2/connectors`) is merged.

## Success measures (checked on the demo shop and the real-data sets)

| Measure | Target |
|---|---|
| Time from connect to first proposed goals with a money size | under 10 minutes after backfill |
| Clicks from connect to Shadow running | 5 or fewer |
| Decisions that need a person once running | 0 inside the limits; only the exceptions below |
| Inbox items per week on a healthy shop | 0-2 |
| Every proposed goal, limit and brand rule | traceable to the shop's own data (no invented numbers) |

## What to build, in order of impact

1. **Goal proposals sized in money, from their own history.** After the backfill, scan for the
   opportunities that pay most often:
   - refill due
   - first-to-second purchase
   - win-back
   - VIP at risk
   - browse/cart (when events exist)

   Each comes with audience size, expected monthly value with a range (from the shop's own repeat
   rates and the priors), and one sentence of why. One click accepts it. The best one is
   preselected.
2. **Limits learned from their history, confirmed once.** These come from their own data:
   - **How often they message:** their past send cadence (from Klaviyo).
   - **Discount limits:** their usual discount depth and margins (from Shopify orders and
     discounts).
   - **Quiet hours:** their customers' time zones.
   - **Budget:** from current spend.

   Show "what we found -> what we will use". It becomes the signed mandate in one click.
3. **Brand kit read from their tools.** We read these, never invent them:
   - Tone and phrasing, from their Klaviyo templates and past campaigns.
   - Product names and images, from Shopify.
   - Logo and colours, from the shop theme or their email templates.
   - Discount style.

   The Critic checks every message against it. One approval of a sample of 3 messages covers the
   brand voice.
4. **Exceptions-only inbox.** Only these:
   - safety stop tripped
   - proof shows it is losing
   - reconnect needed
   - something new or bigger than the mandate (offer type, discount depth, channel, budget)
   - a customer complaint that needs a person

   Everything else goes in the weekly briefing, not the inbox. Each item has one recommended action
   and one button.
5. **Shadow -> Autopilot in one click.**
   - After 1-2 weeks of Shadow (it decides, sends nothing), show "it would have made about $X
     (range), and its forecasts matched what happened within Y". Then "Turn on Autopilot?".
   - Option: pre-authorise automatic promotion when the proof bar is met.
   - The automatic version must also stop itself: it drops back to Approve if proof turns
     negative.
6. **Data Doctor as a quiet watchdog.** For connected data it watches for these, and stays silent
   unless the data should not be trusted:
   - missing days
   - row-count drops
   - schema changes
   - consent coverage
   - duplicate customers across Shopify and Klaviyo (link by email or phone)
7. **Guided setup only where unavoidable.** Klaviyo is fully automatic. Other tools get a
   step-by-step check that verifies itself by sending a test event.

## Not in scope

Any approval that the mandate already covers. Daily reports. Asking the customer to map fields.
