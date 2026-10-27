# Promise map: PITCH.md against the marketer app

This is a check of every concrete promise in [PITCH.md](PITCH.md) against what the app in `ui/v2/` shows a
business owner today. It was made from `ui/v2/*.js`, `ui/v2/screens/*.js`, `ui/v2/mock/*.json`,
`api/v2/routes.py`, `api/v2/deps.py`, the contracts in `autonomy/contracts/`, and the screenshots in
`docs/v2/screenshots/` (taken against the simulated demo on 20 Oct 2026).

**Status** means what a business owner can see:

- **present**: the promise is kept and you can see it.
- **partial**: it is there, but hidden, in jargon, true only in the mock, or missing a piece.
- **missing**: nothing in the app shows it.

**Screen** is the one place the change belongs: shell (the top bar and nav), connect, goal, plan, live, proof,
or onboarding (the "getting started is safe" guidance).

Honesty rules for every change below. Every number comes from the API or from a mock file with the API's
shape. Money always comes with its range, and a range that includes zero reads "not sure yet". Claims about
the product in general (58% more purchases than best-sellers, 3-10% typical) appear only as labelled product
facts ("How we tested this", with a link to the source doc), never as numbers about this business.

## Update: the honesty pass (27 Oct 2026)

The pitch was rewritten to lead with proof, not autonomy ([PITCH.md](PITCH.md)), and the demo was stripped of numbers
it had not earned (docs/v3/ARCHITECTURE_REVIEW.md section 8 "Fix first" 1; docs/v3/CMO_REVIEW.md change 2). What moved
in the table below, in the same row numbers (the audit further down is left as it was):

- **Row 23 is now present.** The server refuses to approve a message the editor did not pass
  (`Services.set_content_status`, 409). The Plan screen offers Approve only on a message the editor passed, and
  the sample API answers 409 the same way. A held draft (the judge went down between its two runs) is no
  longer called "passed". Test: `tests/v2/api/test_content_gate.py`.
- **Row 56 is out of the pitch.** "Paid in results" is dropped until pricing exists; there are 60 promises, not 61.
- **Rows 24, 25, 31, 42** stay partial, with row 25 now quoted with its losses: 34.7% against 22.0% is the best
  case; Ta-Feng (-1.3 points) and Superstore (-2.1 points) lose to best-sellers
  ([REAL_DATA_RESULTS.md](REAL_DATA_RESULTS.md) "Recommender lift").
- **Row 43** ("What it learned"): the sample no longer carries a lesson the engine cannot write, so the card says
  "Nothing new yet" there as it does on the demo. The briefing sample is the Analyst's own template output
  (extra profit, the simple rule, customer health) and no longer has "would buy anyway", "What it is learning" or a
  return-on-spend line.
- **Row 47** (spend): Proof's "Spend so far" and Live's "Budget spent" count different sets. Proof: the agent group
  only, contacts whose results are in, messages and the discounts used. Live: the runtime's budget ledger
  (`SpendLedger`), every message and every matched discount as it happens, for the simple-rule group as well as the
  agent group, which is what the budget limit counts. Each says what it counts, and Proof names Live's number when
  they differ. The Analyst's briefing (drawn on Live) states the proof's spend as the agent group's and no longer
  derives a "budget left" from it, so Live shows one "left" figure, the ledger's.
- **Every screen that shows numbers** says "Simulated shop" when the shop is simulated (the sample files, or a server
  whose only data source is the Gym), Proof first; the top bar says it for the demo server.
- **Follow-ups** (plan): the Plan screen lists a follow-up step only when the server sends it
  (`PlanFacts.follow_ups_run`). No server does: the plain agent sends none, and the v3 journeys run the steps of
  `configs/v3/journeys.yaml`, not the brief's, so every entry point leaves it false, v3 on or off.
- **WhatsApp** (row 9): "Coming next" on the platforms card and on the channels card, never "Ready"; a paused
  WhatsApp still says "Paused" and why. Its description says it is not ready for production (templates not synced
  with Meta, quality signals not read), not that nothing is sent: a set-up sender does send.
- **Rows re-read against the rewritten pitch** (9, 22, 45, 47, 57, 58), and one new row (62, no return multiple).
  Rows 23 ("the server refuses to approve") and 56 (no price list) already covered the other new lines.

## Final status (after the promise work, 20 Oct 2026)

**After the declutter pass (20 Oct 2026).** The screens now show numbers first and move every explanation into a
description that opens on hover, keyboard focus or tap (DESIGN.md section 6). No promise was dropped: each one is on
the page or in the description of the thing it is about, and its words stay in the DOM, where the tests find it.
The "Where" column below says where each moved promise lives now.

Checked against the simulated demo API (`python3 -m api.v2.app`) and the mock files (`?mock=1`), with the
screenshots in [screenshots/](screenshots/README.md) retaken afterwards. Totals before the honesty pass: **49 present, 11 partial,
1 missing** out of 61 (the old pitch); after it, against the rewritten pitch (row 23 present, row 56 dropped, rows 9,
22, 45, 47, 57 and 58 reworded to its lines, row 62 added): **51 present, 10 partial, 0 missing** out of 61. The
screenshots were not retaken for the honesty pass.

| # | Promise (short) | Screen | Status | Where you see it, or why it is not fully kept |
| --- | --- | --- | --- | --- |
| 1 | Five simple screens | shell | present | Nav 1-5 (Connect, Goal, Plan, Live, Proof). Getting started sits outside the numbers; Operator mode is a small muted link (on a phone only with `?operator=1`). |
| 2 | One big Pause button | shell | present | Top-bar Pause stops every running goal (`POST /v2/pause`), says "Stops all sending", and the toast counts the goals; Resume brings back exactly those. Live keeps "Pause this goal". |
| 3 | Shadow / Approve / Autopilot, you stay in control | shell | present | Autonomy badge under the goal switcher on every screen, linking to the Plan dial. |
| 4 | Shadow sends nothing (always visible) | shell | present | Slim note under the top bar while the goal runs in Shadow, with the trial day and its forecast labelled "a forecast, not proof". |
| 5 | It pauses itself, and you hear about it | shell | present | Inbox count matches Live's Inbox; red "Paused itself (n)" badge with the latest alert's words. Alerts have no "resolved" flag, so the count never goes down (the badge is past tense). |
| 6 | Upload files or connect your database | connect | present | "Bring your data in" says exactly what to upload and where (the Data Doctor in the Marketing AI app, linked); "What is connected" shows the API's totals and sources. Uploading itself still happens in v1. |
| 7 | The Data Doctor cleans up messy data | connect | partial | "What the Data Doctor fixed" lists fixes with row counts, but only when its report is linked (`doctor_connected`). The demo has no linked report, so the card says so and shows no counts; "columns mapped" is not reported by the API. |
| 8 | ...and asks before changing anything | connect | partial | "It asks before changing anything." shows everywhere, with how it asks in its description (the card is "Data Doctor questions" when nothing waits); "Waiting for your answer (n)" with rows affected shows only when the report is linked (mock). Answering means going to the v1 Data Doctor: v2 has no endpoint for answers. |
| 9 | Email today; WhatsApp is next; SMS and your tools by hand-off | connect | present | "How messages go out": Sent by Autonomy (Email; WhatsApp is coming next) and By hand-off (SMS, your own tools). Email is Ready / Not set up yet / Paused with the API's reason; WhatsApp is "Coming next" (never "Ready"), with why it is not ready for production in its description, or "Paused" with the API's reason when a safety check paused it. The platforms card lists WhatsApp under "Coming next" too. |
| 10 | Less spam, healthier lists | connect | present | Five health lines (label and number, rate rounded to 2 places); each line's meaning and the goal's safety stop, in the API's words, are the label's description. |
| 11 | Type what you want, in plain words | goal | present | Text box with the pitch's own example; how to say it ("Say who, what you want them to do, and how much to spend.") is the label's (i), and "Every new goal starts in Shadow." holds "Nothing is sent yet" in its description. |
| 12 | "...$5,000 this month" read from the words | goal | present | `POST /v2/goals/read` fills Budget and says "We read $5,000 as your budget and 'this month' as the period, so it runs until 31 Oct 2026". A typed budget is never overwritten. |
| 13 | Or pick an opportunity it found | goal | present | "Or pick one it found for you". |
| 14 | "1,900 customers are drifting away. Want me to handle them?" | goal | present | Big count from `/v2/opportunities/sized` (its "Why: ..." is the count's description), "Want me to handle them?" and "Yes, draft a plan". |
| 15 | The plan in plain English | plan | present | "The plan in plain words" card. |
| 16 | ...who it will contact | plan | present | "Matching customers N · H we never contact" in the limits strip; the full "N customers match this today. H of them are people we never contact ..." is that label's description, and the filters in words are the audience's. |
| 17 | Offers, channels, how often, quiet hours, budget | plan | present | One row each on the plan card. |
| 18 | The actual messages it wrote | plan | present | Message cards with the real text; long WhatsApp template names wrap on a phone. |
| 19 | You approve once | plan | present | "You approve once" dial; passed messages need no approval one by one: a ready message's one choice is a quiet circle-slash icon whose description (hover, focus; its label on touch) is "Don't use this one. Optional: …". |
| 20 | Hard rules it can never break | plan | present | "Rules it can never break": consent, unsubscribes, quiet hours, weekly limit, budget, complaints, no-discount requests, people we never contact, each name with its words and the plan's value in its description. |
| 21 | It pauses itself (the limits) | plan | present | "When it pauses itself" card: the rows grouped under "Pauses the goal" and "Pauses that channel" (each said once), each row's full sentence in its description. |
| 22 | Approved templates in your voice, checked by a strict editor | plan | present | "✓ Checked by the editor" said ONCE, over the group of messages the editor passed on every check, with the checklist (on brand, no false claims, no made-up numbers, unsubscribe line, channel rules) in its description; any other message keeps its own line (one that needs a look comes first, and none that needs a look is ever folded), and the failed check shows on the page; raw notes folded on each message. A message passed on the rules alone (no judge model) shows brand voice and honesty as "Not scored". |
| 23 | Nothing goes out without passing | plan | present | The screen offers Approve only on a message the editor passed, and counts "N passed the editor; M failed and will never be sent". The server enforces it: `POST /v2/content/{id}/status` answers 409 to an approval of a message the editor failed, held or never checked (the Plan screen no longer offers that approval either). A person can still reject or retire any message. |
| 24 | ...which image | plan | partial | Cards draw a thumbnail with alt text when a message has `asset_url`, but no demo or mock message has one, so every real card says "Picture: None, text only". Seen only in the node test. |
| 25 | It knows your products; 58% more than best-sellers | plan | partial | The 58% (34.7% vs 22.0%) appears only in a labelled "How we tested this" box, a fact about the product. This shop's product count shows only in mock mode: the demo's orders name no products, so the card says suggestions are off. The CATALOG.md source is text, not a link, because the app does not serve `docs/`. |
| 26 | A random group is never contacted | plan | present | "10% are people we never contact" (Proof built in), with the method in its description. The word "holdout" is no longer shown (Live's briefing and Proof's API notes are reworded on screen). |
| 27 | A daily briefing | live | present | "Yesterday" card at the top from `GET /v2/today`, the weekly briefing below it. |
| 28 | "Yesterday I contacted 341, left 1,200 alone" | live | present | That sentence with the day's own counts, worded for Shadow and Approve, and two big numbers. |
| 29 | "...and here's why" | live | present | Every left-alone reason with its count and the API's words. |
| 30 | Tap any customer, every decision with a reason | live | present | Customer panel (also "Look up any customer by their id"), each decision with "Why: ...". |
| 31 | "Sent Priya ..." (a person, not an id) | live | partial | First names show when the data has them (mock: "Maya"). The demo's customers have no names, so the feed and panel show ids. |
| 32 | Shadow: you see exactly what it would do | live | present | Each feed row says what really happened: Would send (Shadow), Waiting for your approval, Sent, Delivered, Failed, Not sent. |
| 33 | Approve each batch with one click | live | present | Batches headed in words ("56 customers: 46 by email, 10 by WhatsApp; ..."), Approve and Reject. |
| 34 | Autopilot acts inside your limits | live | present | The top bar's autonomy badge; the level's sentence and "At this level since ..." are the Running pill's description; health against the plan's limits. |
| 35 | Loyal buyers get a reminder, not a coupon | live | present | Panel says the offer habit in words ("Always buys at full price"). |
| 36 | An offer only when it pays for itself | live | partial | "Options it weighed" gives each option's uplift and what it would add or lose after its cost, labelled as estimates. The list comes from `autonomy/decide` and can leave out the option it actually chose. |
| 37 | It targets people it can change | live | partial | Uplift in words ("raises the chance they buy by about 5.2 in 100"). "Would buy anyway" has no reason code of its own (decide does not log the no-message chance), so it is counted under "a message would not change what they do". |
| 38 | It knows when to stay quiet | live | partial | The panel shows "the bar a message had to clear (the cost of bothering them again)". The reason sentences from `autonomy/decide/explain.py` (another stream) still say "the $0.02 cost of another message". |
| 39 | The whole combination for each person | live | present | Feed rows on one line: name, channel and offer, a short reason, what happened; the full reason, the message's words, send time, product and messages this week are the reason's description. |
| 40 | When each person reads their messages | live | present | Panel: "Usually reads messages around ..." with how many reads it is based on (only from 3 or more). |
| 41 | When each customer is due to reorder | live | present | Panel: refill due with its basis. It is the customer's own reorder rhythm, not the catalogue's cycle model. |
| 42 | Complementary products, "the moisturiser she viewed" | live | partial | The feed and panel name the product and possible next items when the data names them (mock). The demo's orders have no product names, so nothing is named there. |
| 43 | It acts, watches, learns | live | partial | "What it learned" card from `today.learned` or the briefing's learning section. On the demo both are empty, so the card says "Nothing new yet". |
| 44 | Every customer, one by one, every day | live | present | "Made a decision for each of N customers yesterday, one by one. Last check ... Next check ..." is the description of Yesterday's "As of" stamp; an overdue check shows on the page. |
| 45 | Next morning: how many were contacted and how many bought, and the extra profit so far with its range or "not sure yet" | live | present | Messages, purchases (labelled every purchase, not only extra) and extra profit with its range, or "Not sure yet". |
| 46 | It pauses itself (seeing it happen) | live | present | Red "This goal paused itself" banner with the alert's words; unsubscribes "pauses at 3x your usual rate" (the Unsubscribes label's description), or the API's "usual rate not known, so this check is not running". |
| 47 | Budget (spend as it runs) | live | present | "Budget spent: $1,840 of $5,000" (sample), the runtime's budget ledger: every message and every matched discount, for the agent and the simple-rule groups, as the budget limit counts it, with what is left. Its description says Proof's spend counts only the agent group's contacts whose results are in. The briefing on the same page states the proof's spend as the agent group's and no "budget left" of its own. |
| 48 | Extra profit against a group never contacted, with a range | proof | present | One sentence, "$X extra profit (likely $low to $high), measured against people we never contact.", then each other figure with its range line. |
| 49 | If it isn't sure yet, it says so | proof | present | Verdict before the numbers: Real extra profit / Not sure yet / Losing money so far / Too early to tell. Too early, the result card shows what it waits for, from the API: the three groups so far, "Each contact counts after N days" and "First from <date>" (the forecast's `since` plus `horizon_days`, only while ahead), and the spend; the method fold then leaves out its group counts. |
| 50 | Real extra profit, not "attributed revenue" | proof | present | How it is measured is the description of "people we never contact", and the "How this is measured" fold at the bottom. |
| 51 | Measured against a simple rule | proof | present | Its own verdict ("Beats the simple rule" ...). The rule is described by example, because the API does not name which rule a goal uses. |
| 52 | Every rupee, or dollar | proof | present | All money in the goal's currency. |
| 53 | Switch to Autopilot when Proof shows real extra profit | proof | present | "Ready for Autopilot" only when the low end is above zero; other cards for Shadow, Approve, losing money, paused and Autopilot without proof. |
| 54 | What it expects to earn before a message goes out | proof | present | "What it expects to earn" from `GET /v2/expected`, chip "Its own forecast, not proof", with its range, under one sentence: "Its forecast covers the N days after each contact it decided on since <date>." |
| 55 | Typically 3-10%; we'll show the real number | proof | present | A "General guidance" pill whose description says "General guidance about AI marketing, not a result from your business", with its source. |
| 56 | ~~Paid in results~~ | proof | dropped from the pitch | There are no pricing terms in the API or the pitch. When a price list exists it must be tied to the low end of the proof range. |
| 57 | Weeks 0 to 3: a read-only audit of your data, then Shadow on your live customers | onboarding | present | `#/start` "Getting started is safe: it watches before it sends.", five steps whose states come from data (the current one's pill says where you are; no "Step n of 5"); Connect is the read-only audit (what is connected, what the Data Doctor found); every new goal starts in Shadow, and Plan marks Shadow "Recommended to start". Shadow runs forward on live decisions; nothing replays your past orders. |
| 58 | Shadow: what it would do, why, and what it expects to earn (a forecast, not proof) | onboarding | present | "Shadow: day N of 14" in the badge, the note and step 3, with what it would have done, its reasons and the forecast labelled "a forecast, not proof". |
| 59 | Then Approve for one goal, small budget | onboarding | present | Step 4 lowers the budget (`PATCH /v2/goals/{id}`, the server's 409 words when raising above an approved plan) and turns on Approve. |
| 60 | Honest status: not yet run with a real business | onboarding | present | "Honest status" card (one line); "How we tested it" is its title's (i). |
| 61 | The ask: your data and one goal | onboarding | present | First run with no goals goes to `#/start` and shows only the ask and steps 1-2; once a goal exists the ask card is gone (it has been met). |
| 62 | No return multiple: the profit shown is already after the costs it could charge | proof | present | No return-on-spend multiple on Proof, in its share text or in the Analyst's briefing (`ProofSummary.roi` is still served). Tests: `tests/v2/promise/proof.test.mjs`, `tests/v2/agents/test_analyst_honest.py`. |

What would turn the partial rows into present ones, and who owns it:

- ~~Server-side guard for the editor (row 23)~~: done, see the update above.
- **Real data with names, products, images and a Data Doctor report** (rows 7, 8, 24, 25, 31, 42): the screens
  already draw them from the API; the simulated demo has none of them. Answering the Doctor's questions in v2
  needs a new endpoint.
- **Decide stream** (rows 36-38): log the chosen option among the bids, a `would_buy_anyway` reason (the
  no-message chance), and reason sentences that call the attention price "the cost of bothering them again".
- **Analyst** (row 43): always write a learning section, so "What it learned" fills on real runs.
- **Pricing terms** (row 56).

## The map before this work (the audit)

The rest of this file is the audit that set the work, kept as it was: "Status now" there is the status
before the changes.

### The audit table

| # | Promise (PITCH.md) | Screen | Status now | Change needed | Data |
| --- | --- | --- | --- | --- | --- |
| 1 | "Five simple screens" | shell | present | Numbered nav 1-5 is there. Keep "Operator mode" out of the marketer's way (show it only with `?operator=1` or tuck it behind a small link). | none |
| 2 | "One big Pause button" | shell | partial | Top-bar Pause stops only the current goal. Make it a clear "Pause all sending" control (pauses every active goal, with a toast saying how many). Keep per-goal Pause on Live. | `POST /v2/goals/{id}/pause` for each active goal (exists; the client can loop over them), or a new `POST /v2/pause` |
| 3 | "You stay in control. Shadow / Approve / Autopilot" | shell | partial | The autonomy level shows only as a chip on Live; the switcher says "(active)". Add a badge next to the goal switcher ("Shadow: sends nothing", "You approve each batch", "Autopilot") that links to the Plan dial. | `Goal.autonomy` (exists) |
| 4 | "Shadow: it decides and shows you, but sends nothing" (always visible) | shell | missing | When the current goal is in Shadow, show a slim note under the top bar: "Shadow mode: Autonomy is deciding, but nothing is being sent." | `Goal.autonomy` (exists) |
| 5 | "It auto-pauses itself if complaints or unsubscribes spike" (you hear about it) | shell | partial | The Inbox count skips "info" alerts and so disagrees with Live's Inbox, and an automatic pause looks like any other alert. Count the same set as Live, and show a red "Paused itself" badge when an alert for this goal has `auto_paused`. | `Alert.severity`, `Alert.auto_paused`, `Alert.goal_id` (exist) |
| 6 | "Upload your customer and order files, or connect your database" | connect | partial | Explained in words, but done in the v1 app through a link. Say exactly where to go and what to upload, and show what is connected once the API reports it. | new `GET /v2/data/status` |
| 7 | "Our Data Doctor cleans up messy data" | connect | partial | One sentence. Against the API the card says "Data status is not available here". Add a "What the Data Doctor fixed" card (customers, orders, columns mapped, rows it could not use, in plain words). | new `GET /v2/data/status` -> `{sources: [{name, kind, customers, orders, updated_at}], fixes: [{what, rows}], problems: [...]}` |
| 8 | "...and asks before changing anything" | connect | missing | Nothing says it asks. Add "Waiting for your answer (n)": each proposed fix with how many rows it touches, answered in the Data Doctor (link) until v2 can answer here. | `GET /v2/data/status` -> `questions: [{question_id, text, rows_affected}]` |
| 9 | "Channels: email and WhatsApp today; SMS and your own tools through a hand-off" | connect | missing | Nothing says which channels are ready. Add a "How messages go out" card: Email and WhatsApp (ready, not set up, or paused, with the reason); SMS and your own tools "by hand-off". | new `GET /v2/channels` -> `[{channel, mode: direct\|handoff, ready, paused, paused_reason}]` |
| 10 | "Less spam, healthier lists" | connect | present | The Sending health card is there. Explain each line in words and round "contacts per customer per week" to 2 places (now "0.2817"). | `GET /v2/health` (exists) |
| 11 | "Type what you want, in plain words" | goal | present | The text box, examples and "Make a plan" are there. | `POST /v2/goals` (exists) |
| 12 | "...'Win back customers who stopped buying, $5,000 this month'" | goal | partial | The budget has to be typed again in its own field, and "this month" is ignored. When the text names an amount and the field is empty, fill it in and say "We read $5,000 as your budget. Change it if that's wrong". Show the end date the server reads. | `POST /v2/goals` with `budget` optional; the server reads the amount and the period from the text and returns `goal.budget` and `goal.ends_at` (field exists) |
| 13 | "Or pick one of the opportunities it found for you" | goal | present | Cards with "Start this goal" are there. | `GET /v2/opportunities` (exists) |
| 14 | "'1,900 customers are drifting away. Want me to handle them?'" | goal | partial | The count appears only inside the API's sentence, and the button says "Start this goal". Show the number of customers big, one sentence on why, and "Want me to handle them?" with a "Yes, draft a plan" button. | new `audience_n` and `reason` on each opportunity (a wrapper `{goal, audience_n, reason}`) |
| 15 | "It shows you its plan in plain English" | plan | present | "The plan in plain words" card is there. | `GET /v2/goals/{id}` brief (exists) |
| 16 | "...who it will contact" | plan | partial | There is a description and filters, but no count, and the filters read like field names ("Last purchase at: more than 60 days ago"). Add "About 1,900 customers match today, of whom 190 are people we never contact", and word the filters for people. | new `audience_n` (and `holdout_n`) in `GET /v2/goals/{id}` |
| 17 | "...which offers it may use, which channels, at most how often, quiet hours and budget" | plan | present | All five are on the plan card, each from the brief's envelope. | `Brief.envelope` (exists) |
| 18 | "It also shows you the actual messages it wrote" | plan | present | Message cards with the real text. On a phone, WhatsApp template names run past the card edge, so they need `overflow-wrap: anywhere`. | `ContentItem` (exists) |
| 19 | "You approve once" | plan | present | "Approve plan" with the autonomy dial. Say on the message cards that the editor has already checked them and that rejecting one is optional, so they don't read as 14 more approvals. | `POST /v2/goals/{id}/approve` (exists) |
| 20 | "Hard rules it can never break: consent, quiet hours, weekly limits, budget, no promotions after a complaint" | plan | partial | Quiet hours, the weekly limit and the budget appear as plan facts. Consent, unsubscribes and the complaint rule are not shown, and nothing says these rules cannot be broken. Add a "Rules it can never break" card listing all of them, each with the plan's value. | new brief envelope hard-rules list: `hard_rules: [{code, words, value}]` in `GET /v2/goals/{id}`, using the codes in `autonomy/decide/rules.py` (`no_consent`, `unsubscribed`, `quiet_hours`, `weekly_cap`, `no_budget`, `open_complaint`) |
| 21 | "It auto-pauses itself if complaints or unsubscribes spike" (the limits) | plan | present | The stop rules are listed in plain sentences. Retitle them "When it pauses itself". | `Envelope.stop_rules` (exists) |
| 22 | "An AI copywriter drafts the messages and a second AI acts as a strict editor: brand voice, no false claims, no made-up numbers, unsubscribe line present, channel rules followed" | plan | partial | "Critic's notes" are shown raw ("judge run 1 [honesty, brand_voice]: honesty 5 ..."), which is jargon. Show a checklist on each message ("Checked by the editor: on-brand ✓, no false claims ✓, no made-up numbers ✓, unsubscribe line ✓, channel rules ✓") and fold the raw notes under "Editor's notes". | critic notes per content item, structured: new `checks: [{check: brand_voice\|honesty\|no_invented_numbers\|unsubscribe\|channel_rules, passed, note}]` on `ContentItem` (written by `autonomy/agents/critic.py`) |
| 23 | "Nothing goes out without passing" | plan | partial | "Only approved messages are ever sent" is there, but a message the editor failed looks the same as one you rejected. Count them: "14 passed the editor; 2 failed and will never be sent". | `ContentItem.status` (exists) plus `checks` (row 22), or a new `reviewed_by: critic\|person` |
| 24 | "...which image" | plan | missing | `asset_url` is never drawn. When a message has an image, show a thumbnail with alt text on its card. | `ContentItem.asset_url` (exists) |
| 25 | "It knows your products ... On real shop data, its suggestions got about 58% more purchases than just recommending best-sellers" | plan | missing | Add a "Products it may suggest" note with a labelled product fact: "How we tested this: on a public real shop's order history (UCI Online Retail), 34.7% of customers went on to buy one of its 5 suggestions, against 22.0% for the 5 best-sellers, about 58% more", linking `docs/v2/CATALOG.md`. Never say it is this shop's result. | the fact: none (from `docs/v2/CATALOG.md`); this shop: optional `products` count in `GET /v2/data/status` |
| 26 | "A small random group of customers is never contacted" | plan | present | "Proof built in: 10% of customers are never contacted". Lead with "people we never contact" wherever the word "holdout" appears (Live briefing, Proof tiles). | `Envelope.holdout_share` (exists) |
| 27 | "A daily briefing" | live | partial | The real API serves a weekly briefing for the whole programme ("as of 19 Oct"); only the mock is daily. The goal on Approve shows programme-wide money against its own budget. Put a "Yesterday" card at the top from a per-goal daily count, with the weekly briefing below it. | new `GET /v2/today?goal_id=` (see API changes) |
| 28 | "'Yesterday I contacted 341 customers, left 1,200 alone'" | live | partial | The feed counts only within the latest 100 decisions ("Left alone (n)"), and "nothing" decisions with no bids drop out of the goal filter. Show two big numbers for yesterday: "Contacted 341 · Left alone 1,200". | `GET /v2/today?goal_id=` -> `contacted`, `left_alone`, counted on the server over all of yesterday's decisions |
| 29 | "'...and here's why'" (who was left alone) | live | missing | Nothing groups the "left alone" decisions by reason. List them: "Contacted twice this week: 640 · Better off without a message: 380 · Open complaint: 1 · People we never contact: 120 · No consent: 59". | `GET /v2/today` -> `left_alone_by_reason: {code: n}` (rule codes plus `not_worth_it`, `would_buy_anyway`, `holdout`) |
| 30 | "Tap any customer and see every decision with a reason" | live | present | The customer panel has a timeline where each decision has its "Why:". Word the facts for people ("chance of this pick 90%", "uplift 0.3%", "Score: churn_risk 0.123" are jargon). | `GET /v2/customers/{id}` (exists) |
| 31 | "'Sent Priya a reminder with no discount ...'" (a person, not an id) | live | partial | The feed and panel show ids (`gym_000988`). Show the first name when it is known, with the id in small type. | new `display_name` (first name only) on the customer detail and on decision rows |
| 32 | "Shadow: ... You see exactly what it would do and why" | live | partial | Reasons begin "Sent a reminder ..." even in Shadow and for decisions still waiting in a batch, so held messages read as sent. Label each item with what really happened: "Would send (Shadow)", "Waiting for your approval", "Scheduled for 19:00", "Sent", "Delivered", "Failed". | new per-decision `delivery: {state: shadow\|held\|queued\|sent\|delivered\|failed, batch_id?}` in `GET /v2/decisions` and the customer timeline (the server joins batches and events by `decision_id`) |
| 33 | "Approve: you approve each batch with one click" | live | present | Approve and Reject on each batch, and "To approve n" in the top bar. The batch summary uses raw ids ("whatsapp (62); offers: none (84), pct10 (34)"), so word them as the Plan does. | new `by_channel: {channel: n}` and `by_offer: {offer_id: n}` on `Batch` (the runtime writes `summary`) |
| 34 | "Autopilot: it acts on its own inside the limits you set" | live | present | The autonomy chip, health against the plan's limits, and the dial's sentence. | `Goal.autonomy`, `GET /v2/health` (exist) |
| 35 | "Loyal buyers get a reminder, not a coupon" | live | partial | Reasons say so, but the panel shows "Offer sensitivity 0.12 (0 buys at full price, 1 needs an offer)". Say "Always buys at full price" or "Usually buys with an offer". | `CustomerState.offer_sensitivity` (exists); better, a new `offer_habit` label from the API so the UI sets no thresholds |
| 36 | "Price-sensitive shoppers get an offer only when it pays for itself" | live | partial | Reasons say "an offer would not add enough to pay for itself". "Options considered" lists bids as "expected net $x, uplift y%". Show the options in words: "10% off: would add -$1.20 after its cost, so not worth it". | decision bids list exposed in the customer timeline: `Decision.bids` with `offer_cost` and `expected_net` (exists) |
| 37 | "Autonomy targets people whose behaviour it can actually change" | live | partial | Uplift appears only as "uplift 0.3%" inside a fold. Say "A message makes a purchase about 0.3 points more likely", and count "would buy anyway" in yesterday's reasons (row 29). | `Bid.uplift` (exists); `left_alone_by_reason.would_buy_anyway` needs its own reason code from decide |
| 38 | "It knows when to stay quiet ... Autonomy weighs that cost every time, so 'do nothing' often wins" | live | partial | Reasons say "more than the $0.02 cost of another message", which reads like a sending price (email costs $0.001). In the panel, show `attention_price` as "the bar a message had to clear (the cost of bothering them again)". The reason wording itself belongs to `autonomy/decide/explain.py`, another stream. | `Decision.attention_price` (exists); reason wording change in decide |
| 39 | "It picks the whole combination for each person. Who, when, which channel, which offer, which message, which image, and how often" | live | partial | The feed shows channel, offer and send time, but not which message, which image, or how many contacts this week. Feed line: "WhatsApp · No offer · 'Made to last' · 19:00 · 2nd message this week". | `Action.content_id` joined to the content subject (exists); `ContentItem.asset_url` (exists); `contacts_7d` on decision rows (new; it is on `CustomerState` today) |
| 40 | "Timing: it learns when each person actually reads their messages" | live | missing | The panel never shows it. Add "Usually reads messages around 7 pm (their time)" next to the send time it chose. | `CustomerState.active_hours`, `hour_counts`, `timezone` (exist); better, a new `best_hour` from the API |
| 41 | "It predicts when each customer is due to reorder" | live | missing | Add "Due for a refill of Vitamin C serum in about 3 days" to the panel. | new `refill: {item_id, item_name, days_until_due, window_open}` on `GET /v2/customers/{id}` (from `autonomy/catalog/cycle.py`) |
| 42 | "...and suggests complementary products" / "mentioning the moisturiser she viewed" | live | missing | `Action.item_id` is never shown. Show "Mentions: Hydrating moisturiser (often bought with your serum)" in the feed and the panel. | `Action.item_id` (exists) plus item names: new `items: {item_id: name}` in decisions and customer responses, and `next_items: [{item_id, name, reason}]` on the customer |
| 43 | "Then it acts, watches what happens, learns" | live | partial | Only the mock briefing has "What it is learning", plus "learning" alerts in the Inbox. Add a "What it learned this week" card. | a briefing section the Analyst always writes (`Briefing.sections`, exists), or `learned: [sentence]` in `GET /v2/today` |
| 44 | "It looks after every customer one by one, every day" / "always-on" | live | partial | Nothing says when it last looked. Header line: "Checked all 1,541 customers today at 09:00. Next check tomorrow at 09:00." | `GET /v2/today` -> `customers_considered`, `last_run_at`, `next_run_at` (the runtime cursor) |
| 45 | "Next morning: 'Yesterday: 341 messages, 24 purchases. Extra profit vs the untouched group so far: $570 (range $310-$830).'" | live | partial | Only the weekly briefing's headline. The "Yesterday" card shows messages, purchases and extra profit so far with its range, or "not sure yet" when the range includes zero. | `GET /v2/today` -> `messages_sent`, `purchases`, `revenue`; profit from `GET /v2/proof?goal_id=` `incremental_profit` (exists) |
| 46 | "It auto-pauses itself if complaints or unsubscribes spike" (seeing it happen) | live | partial | The Inbox has an "Auto-paused" chip, and the Delivered and Complaints tiles show their limits. The Unsubscribes tile has none, because the rule is "3× your usual rate" and the usual rate is not sent. Add "pauses at 3× your usual (usual 0.4%)", and when the goal paused itself, a red header: "Paused itself: complaints went above 0.08%". | new `unsubscribe_baseline` on `HealthStatus`; `Alert.auto_paused`, `Alert.code` (exist) |
| 47 | "Hard rules ... budget" (spend against budget, as it runs) | live | partial | Spend appears only inside the briefing's sentence, which is programme-wide. Add a "Spent $41 of $3,000" tile for this goal. | `GET /v2/today` -> `spend_to_date`, `budget_left` for the goal |
| 48 | "Proof. How much extra profit it made compared with a group it never contacted, with an honest range" | proof | present | Extra profit and revenue against the holdout, each with its 95% range, plus a chart. Lead with the words "people we never contact". | `ProofSummary.incremental_profit`, `incremental_revenue` (exist) |
| 49 | "If it isn't sure yet, it says so" | proof | partial | Only small tile text: "Includes zero: not yet certain". There is no plain verdict, and the value label crosses the zero line. Put a verdict before the numbers: "Real extra profit" (low > 0), "Not sure yet" (range includes 0), "Losing money so far" (high < 0), or "Too early to tell" (no estimate). | `incremental_profit.low` and `.high` (exist; comparing them with zero adds no new number) |
| 50 | "So you see real extra profit, not 'attributed revenue'" | proof | present | The holdout card explains it. Add one "How this is measured" info line. | none |
| 51 | "It also measures itself against a simple rule anyone could set up, so you know the AI is earning its keep" | proof | present | The "Compared with the simple rule" card is there. Add a verdict: "Beats the simple rule" or "Not sure yet". | proof `lift_vs_baseline` (already there) |
| 52 | "It proves every rupee, or dollar" | proof | present | Money is in the goal's currency, and INR is offered on Goal. | `Goal.currency` (exists) |
| 53 | "When the proof page shows real extra profit: switch to Autopilot" | proof | missing | When `incremental_profit.low > 0` and the goal is not on Autopilot, show a "Ready for Autopilot" card linking to the Plan dial. Otherwise: "Stay in Shadow or Approve until the range is above zero." | `incremental_profit.low` (exists), `Goal.autonomy` (exists) |
| 54 | "...you'll see ... what it expects to earn, before a single message goes out" | proof | missing | A Shadow goal sends nothing, so it has no proof, and the expected value of its decisions is never added up. Show "What it expects to earn (its own forecast, not proof): $X (range a to b) over 14 days", labelled as a forecast. | new `GET /v2/expected?goal_id=` -> `{contacts, expected_net: Estimate, expected_spend, horizon_days}`, summed on the server from `Decision.expected_net` and the bids' `uncertainty` |
| 55 | "What to expect: well-run AI marketing typically adds 3-10% in real, measured extra profit. We'll show you the real number, with its range" | proof | missing | Add a labelled product fact below the result: "What to expect: typically 3-10% extra profit. Your real number is the one above." Show this goal's share only if the API sends it. | the fact: none (a product note with its source); optional new `incremental_profit_share: Estimate` on `ProofSummary` |
| 56 | "Your always-on marketing team, paid in results" | proof | missing | No pricing exists in the app. Show nothing until there are pricing terms. If they are added, tie them to the proof's range ("billed on the lower end of the range"), never to a point estimate. | none today (would need billing terms from the API) |
| 57 | "Week 1: connect your data and run in Shadow mode. It decides but sends nothing" | onboarding | missing | The dial explains Shadow, but nothing recommends it. Add a "Getting started safely" path (on Goal and in empty states): 1 Connect your data, 2 Set one goal, 3 Shadow for two weeks, 4 Approve with a small budget, 5 Autopilot when Proof shows real extra profit. Each step's state comes from data. Mark Shadow "Recommended to start" on the Plan dial. | `GET /v2/goals` (`autonomy`, `status`), `GET /v2/data/status`, `GET /v2/proof` (exist or rows 6-8) |
| 58 | "In two weeks of Shadow mode you'll see what it would do, why, and what it expects to earn" | onboarding | missing | Show "Shadow trial: day 5 of 14" on Live and in the shell badge, with what it has seen so far (customers it would contact, and the forecast from row 54). | new `Goal.autonomy_since` (fallback: `Brief.approved_at`, exists); `GET /v2/expected` |
| 59 | "Then: turn on Approve mode for one goal, with a small budget" | onboarding | missing | After the shadow trial, suggest "Try Approve on this goal with a small budget" and let the budget be lowered there. Today a budget cannot be changed after the goal is made. | new `PATCH /v2/goals/{id} {budget}` |
| 60 | "Honest status: it hasn't run with a real business yet; start in Shadow mode, risk-free" | onboarding | missing | Add a plain note on the getting-started card: "Autonomy is new. It has been tested on simulated and historical shop data, not yet live with a real business, so start in Shadow." (The demo's Proof notes already say "Demo data from a simulated shop".) | none (a product fact) |
| 61 | "The ask: give us your customer data and one goal" | onboarding | partial | Connect ends with "Next: say what you want to achieve". On first run with no goals, show exactly those two steps and nothing else. | `GET /v2/goals` (empty), `GET /v2/data/status` |

## API changes needed

Each change is as small as the promise allows. "Exists" means the field is already sent and only the UI has to change.

1. **`GET /v2/today?goal_id=`** (new): one day's work for one goal, counted on the server over all of that
   day's decisions, including "nothing" decisions that have no bids:
   `{date, as_of, customers_considered, contacted, left_alone, left_alone_by_reason: {code: n},
   messages_sent, purchases, revenue, spend_to_date, budget_left, last_run_at, next_run_at, learned?: [str]}`.
   The reason codes are those of `autonomy/decide/rules.py` plus `holdout`, `not_worth_it` and
   `would_buy_anyway`. This covers rows 27-29, 37, 43-45 and 47.
2. **Decision delivery state** on `GET /v2/decisions` rows and in the customer timeline:
   `delivery: {state: shadow|held|queued|sent|delivered|failed, batch_id?}`, joined on the server from
   batches and events by `decision_id` (row 32). The optional `goal_id` on `Decision`, asked for before, also
   fixes the goal filter for bid-less decisions.
3. **The decision bids list in the customer timeline** is already exposed (`Decision.bids` with `offer_cost`,
   `expected_net`, `uplift`). Only the UI changes (rows 36-37).
4. **Customer detail additions** on `GET /v2/customers/{id}`: `display_name` (first name only), `best_hour`
   (from `active_hours`), `offer_habit` (`full_price|sometimes|needs_offer`), `refill: {item_id, item_name,
   days_until_due, window_open}`, `next_items: [{item_id, name, reason}]` (rows 31, 35, 40-42).
5. **Item names** for `Action.item_id`: an `items: {item_id: name}` map in decision and customer responses
   (row 42). Add `display_name` and `contacts_7d` to decision rows for the feed (rows 31, 39).
6. **Hard rules in the brief envelope**: `hard_rules: [{code, words, value}]` in `GET /v2/goals/{id}` (row 20).
7. **Audience size**: `audience_n` and `holdout_n` in `GET /v2/goals/{id}`, and `{goal, audience_n, reason}`
   for each item of `GET /v2/opportunities` (rows 14, 16).
8. **Critic notes per content item, structured**: `ContentItem.checks: [{check, passed, note}]` for brand
   voice, honesty, no invented numbers, unsubscribe line and channel rules, plus who rejected it (rows 22-23).
   `asset_url` already exists (row 24).
9. **Batch breakdown**: `Batch.by_channel: {channel: n}` and `Batch.by_offer: {offer_id: n}`, so the UI words
   them (row 33). The runtime writes `summary`, and that is another stream's file.
10. **Usual unsubscribe rate**: `HealthStatus.unsubscribe_baseline`, the rate the 3× stop rule compares against
    (row 46).
11. **`GET /v2/expected?goal_id=`** (new): the Shadow forecast `{contacts, expected_net: Estimate,
    expected_spend, horizon_days}`, summed on the server from decisions. The UI labels it as a forecast, not
    proof (rows 54, 58).
12. **`Goal.autonomy_since`**: when the current autonomy level began, for "day 5 of 14" (row 58).
13. **`GET /v2/data/status`** (new): `{sources, fixes, problems, questions, products?}` in place of v1's
    `/datasets`, which needs sign-in (rows 6-8, 25, 57).
14. **`GET /v2/channels`** (new): `[{channel, mode: direct|handoff, ready, paused, paused_reason}]` (row 9).
15. **Goal text and budget**: `POST /v2/goals` takes `budget` as optional and reads the amount and the period
    from the text (returning `budget` and `ends_at`). A new `PATCH /v2/goals/{id} {budget}` allows "a small
    budget" later (rows 12, 59).
16. **Proof**: `incremental_profit`, `lift_vs_baseline` and `roi`, each with low and high, are already there.
    The "not sure yet" and "ready for Autopilot" verdicts only compare them with zero (rows 49, 51, 53).
    Optional: `ProofSummary.incremental_profit_share: Estimate` (row 55).
17. **Pause all** needs no new endpoint: the client pauses each active goal. `POST /v2/pause` is optional
    (row 2).
18. **Reason wording** (decide stream, not this one): "the $0.02 cost of another message" should name the
    attention price as the cost of bothering someone again (row 38). Not done here, because
    `autonomy/decide` is being edited by another stream.

## Data for each screen

What the screens call to keep the promises above. The existing endpoints keep their exact shapes (their
tests validate them against the strict contracts), so every addition is a new endpoint, computed in
`api/v2/insights.py` from the real stores (decisions, events, customer state, workspace, batches, the
runtime's spend ledger and schedule) and served by `api/v2/routes.py`. Each `api.js` method falls back to a
mock file in `ui/v2/mock/` with the same shape (tests/v2/promise checks every file against its model), and
each GET resolves to `null` when an older API build has no such route. Every `words` field is a plain sentence
built by code from the counts next to it, so a screen never has to word a reason code itself.

Honesty notes that apply throughout:

- Nothing is estimated here that the system did not already estimate. `GET /v2/expected` adds up the agent's
  own per-decision forecasts and says so (`label: "forecast"`, "Its own forecast, not proof"); decisions with no
  forecast (the simple rule's) are counted in `contacts` but not added (`with_forecast`).
- A number the data does not have stays `null`: no first name in the data, no `display_name`; no catalogue or
  item name in the events, no `name`; no usual unsubscribe rate, `unsubscribe_baseline: null` and the 3x rule says
  "not known yet, so this check is not running" (`judged: false`).
- `would_buy_anyway` is not a reason code yet: telling it apart from "a message would not change what they do"
  (`no_effect`) needs each customer's chance of buying without a message, which decide() does not log.
- `Today.purchases` and `revenue` are every purchase by the customers looked after that day, not extra ones; the
  extra profit (with its range) stays on `GET /v2/proof`.

### Shell (rows 2-5)

| Method (`ui/v2/api.js`) | Endpoint | Fields | Promise |
| --- | --- | --- | --- |
| `pauseAll()` | `POST /v2/pause` | `{paused: [Goal]}`; tells `onGoalChange` about each | 2 |
| `listGoals()` (exists) | `GET /v2/goals` | `autonomy`, `status` | 3, 4 |
| `getPlanFacts(goalId)` | `GET /v2/goals/{id}/plan` | `autonomy_since` (when the plan was approved at this level) | 3, 58 |
| `listAlerts()` (exists) | `GET /v2/alerts` | `severity`, `auto_paused`, `goal_id` | 5 |

### Connect (rows 6-10, 25)

| Method | Endpoint | Fields | Promise |
| --- | --- | --- | --- |
| `getDataReport()` | `GET /v2/data/status` | `customers`, `orders`, `products`, `sources: [{name, kind: file\|shop\|database\|simulation\|other, customers, orders, events, updated_at}]`, `fixes: [{what, rows}]`, `problems: [str]`, `questions: [{question_id, text, rows_affected}]`, `doctor_connected` | 6, 7, 8, 25, 57, 61 |
| `listChannels()` | `GET /v2/channels` | `[{channel, mode: direct\|handoff, ready, paused, paused_reason, words}]` for email, WhatsApp, SMS and your own tools (`webhook`) | 9 |
| `getHealth()` (exists) | `GET /v2/health` | rates | 10 |

`fixes` and `questions` are filled when a Data Doctor report is wired in (`Services.data_doctor`); until then
`doctor_connected` is false and the lists are empty. The older `getDataStatus()` (v1 `/datasets`) stays as it was.

### Goal (rows 11-14)

| Method | Endpoint | Fields | Promise |
| --- | --- | --- | --- |
| `readGoal(text)` | `POST /v2/goals/read` | `{budget, currency, ends_at, period, words}`: "We read $5,000 as your budget. We read 'this month' as the period, so it runs to 31 Oct 2026. Change it if that's wrong." | 12 |
| `createGoal({text, budget?, currency, ends_at?})` | `POST /v2/goals` | `budget` is now optional: without it the server reads the amount and the period from the words (400 if the words name no amount); `goal.ends_at` | 11, 12 |
| `listSizedOpportunities()` | `GET /v2/opportunities/sized` | `[{goal, audience_n, reason}]` | 13, 14 |

### Plan (rows 15-26)

| Method | Endpoint | Fields | Promise |
| --- | --- | --- | --- |
| `getPlanFacts(goalId)` | `GET /v2/goals/{id}/plan` | `audience_n`, `agent_n`, `baseline_n`, `holdout_n` (counted against today's customers), `audience_words` (each filter for people: "Has bought at least once", "Last purchase more than 60 days ago") | 16, 26 |
| | | `hard_rules: [{code, words, value}]`: `no_consent`, `unsubscribed`, `quiet_hours` ("21:00-09:00"), `weekly_cap` (2), `no_budget` (the budget), `open_complaint`, `no_discounts`, `holdout` (0.1); codes of `autonomy/decide/rules.py` | 17, 20 |
| | | `content: [{content_id, status, reviewed_by: critic\|person\|null, passed, checks: [{check: brand_voice\|honesty\|no_invented_numbers\|unsubscribe\|channel_rules, passed, note}], notes}]`, read from the Critic's own notes; `editor_passed`, `editor_failed`, `not_reviewed`, `rejected_by_person` | 19, 22, 23 |
| `getGoal(goalId)` (exists) | `GET /v2/goals/{id}` | `brief.envelope` (offers, channels, caps, quiet hours, budget, `stop_rules`), `content[].asset_url` | 15, 17, 18, 21, 24 |

Row 25's "58% more purchases" is a product fact from `docs/v2/CATALOG.md`, shown only as "How we tested this";
`GET /v2/data/status` `products` says how many items this shop's orders hold.

### Live (rows 27-47)

| Method | Endpoint | Fields | Promise |
| --- | --- | --- | --- |
| `getToday(goalId, {day?})` | `GET /v2/today?goal_id=&day=` | `date` (default: the latest day it decided), `customers_considered`, `contacted`, `contacted_by_group: {agent, baseline}`, `left_alone`, `left_alone_by_reason: {code: n}`, `reasons: [{code, words, n}]`, `messages_sent`, `delivered`, `purchases`, `revenue`, `spend_to_date`, `budget`, `budget_left`, `last_run_at`, `next_run_at`, `run_overdue`, `sends_nothing` (Shadow), `autonomy_since`, `learned: [str]`, `notes` | 27, 28, 29, 37, 43, 44, 45, 47, 58 |
| `listActivity({goalId, customerId, limit})` | `GET /v2/activity` | `[{decision, goal_id, display_name, contacts_7d, delivery: {state: shadow\|held\|approved\|rejected\|queued\|sent\|delivered\|failed\|not_sent, words, batch_id, at}, left_alone: {code, words}, item: {item_id, name}, content: {content_id, channel, theme, subject, template_name, asset_url}}]`; the goal filter keeps bid-less "nothing" decisions (holdout, weekly cap) | 30, 31, 32, 39, 42 |
| `getCustomerInsight(customerId)` | `GET /v2/customers/{id}/insight` | `display_name`, `best_hour`, `best_hour_words` ("Usually reads messages around 7 pm (their time, Asia/Kolkata)", from at least 3 engagements), `offer_habit: full_price\|sometimes\|needs_offer\|unknown` with `offer_habit_words` (counted from their purchases' offers), `refill: {item_id, item_name, days_until_due, window_open, basis}` (their own reorder rhythm), `next_items: [{item_id, name, reason, decision_id}]`, `items: {item_id: name}`, `deliveries: {decision_id: Delivery}`, `left_alone: {decision_id: {code, words}}` | 31, 32, 35, 40, 41, 42 |
| `getCustomer(customerId)` (exists) | `GET /v2/customers/{id}` | `decisions[].bids` (`offer_cost`, `expected_net`, `uplift`), `attention_price` | 36, 37, 38 |
| `getBatchBreakdown(batchId)` | `GET /v2/batches/{id}/breakdown` | `{by_channel, by_offer, offer_labels, words}`: "21 customers: 20 by email, 1 by WhatsApp; 20 with no offer, 1 with 10% off." | 33 |
| `getHealthLimits(goalId)` | `GET /v2/health/limits?goal_id=` | `unsubscribe_baseline`, `rules: [{metric, threshold, action, words, current, baseline, pause_at, judged}]` | 46 |
| `getProof(goalId)` (exists) | `GET /v2/proof?goal_id=` | `incremental_profit` with `low`/`high` | 45 |

Left-alone reason codes (`LEFT_ALONE_WORDS` in `api/v2/insights.py`): `holdout` (people we never contact),
`simple_rule` (the comparison group's rule left them alone), `not_worth_it`, `no_effect`, `testing_rest`,
`nothing_to_offer`, and the rule codes `weekly_cap`, `too_soon`, `open_complaint`, `snoozed`, `no_budget`,
`pacing`, `channel_paused`, `quiet_hours`, `offer_needs_approval`, `no_discounts`, `offer_not_allowed`,
`no_brief`, `unsubscribed`, `no_consent`, `no_address`, `channel_not_allowed`, `other`.

`next_run_at` is the runtime's next daily run after the latest one; `run_overdue` is true when it is due and has
not run yet (the demo API only runs when ticked). Without a runtime it is `null`.

### Proof (rows 48-56)

| Method | Endpoint | Fields | Promise |
| --- | --- | --- | --- |
| `getProof(goalId)` (exists) | `GET /v2/proof` | `incremental_profit`, `incremental_revenue`, `lift_vs_baseline`, `roi` (each `{point, low, high}`); the verdicts compare `low`/`high` with zero | 48, 49, 51, 53 |
| `getExpected(goalId)` | `GET /v2/expected?goal_id=` | `{label: "forecast", since, contacts, with_forecast, customers, expected_net: {point, low, high} \| null, expected_spend, horizon_days, currency, words}` | 54, 58 |

Rows 55 and 56 need no data: "3-10% typical" is a labelled product fact; there are no pricing terms.
`ProofSummary.incremental_profit_share` was optional and is not added.

### Onboarding (rows 57-61)

| Method | Endpoint | Fields | Promise |
| --- | --- | --- | --- |
| `listGoals()`, `getDataReport()`, `getProof()` | as above | step states: data connected, a goal, Shadow, Approve, Autopilot | 57, 61 |
| `getToday(goalId)` / `getPlanFacts(goalId)` | as above | `autonomy_since` for "Shadow trial: day 5 of 14" | 58 |
| `getExpected(goalId)` | as above | what Shadow expects to earn | 58 |
| `updateGoalBudget(goalId, budget)` | `PATCH /v2/goals/{id}` `{budget}` | `{goal, brief}`; lowering is always allowed, a draft plan takes the new budget, raising above an approved plan's budget is a 409 (it needs a new plan) | 59 |

### Mock files

`today.json`, `activity.json`, `customer_insight.json`, `plan.json`, `expected.json`, `health_limits.json`,
`data_status.json`, `channels.json`, `opportunities_sized.json` and `batch_breakdowns.json`. `activity.json`,
`customer_insight.json`, `plan.json`, `batch_breakdowns.json`, `channels.json` and `health_limits.json` were
computed by `api/v2/insights.py` from the existing mock world (decisions, batches, the sample customer, the
plan's content); the others are sample numbers in the real shapes, consistent with `briefing.json` (yesterday:
412 contacted, 1,688 left alone) and `proof.json` (spend $1,840). The sample content now carries the Critic's
notes in its real format and the e-mails their unsubscribe line, and the sample customer's sign-up carries a
first name. In mock mode `readGoal` and a budget-less `createGoal` read the words with a small reader in
`api.js` (`readGoalWords`), and `pauseAll` and `updateGoalBudget` change the in-memory copy only.

### Not done here

- The reason sentence for the attention price (row 38) belongs to `autonomy/decide/explain.py` (another stream).
- `Batch.by_channel`/`by_offer` and `HealthStatus.unsubscribe_baseline` on the contracts themselves: served by
  the breakdown and limits endpoints instead, so the contract shapes stay as they are.
- `Goal.autonomy_since` on the contract: served as `autonomy_since` on `/today` and `/goals/{id}/plan`, from the
  latest plan approval (approving is how the level is set).
- A refill prediction from the catalogue's cycle model (`autonomy/catalog/cycle.py`): the panel's refill is the
  customer's own reorder rhythm, worded with its basis ("Bought it 4 times, about every 40 days").
