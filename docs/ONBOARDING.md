# Onboarding: turning your tables into a dataset

This guide is for the person at a client who knows what the data means. You do not need to write
SQL, and you will not be asked to. You will be asked, several times, what something in your business
actually is — and those are the questions that decide whether the model is any good.

Everything below describes what the engine does today. The role names, the function names, the
defaults and the check codes are the ones in
[`configs/roles.yaml`](../configs/roles.yaml), [`configs/engine.yaml`](../configs/engine.yaml) and
[`engine/onboarding/`](../engine/onboarding/).

---

## 1. What onboarding is for

You have a customer master: one row per customer, with their plan, their region, what they pay.
You have a billing table: one row per invoice, going back two years. You have a complaints table:
one row per ticket. Maybe a usage log, maybe an activity log.

What you do not have is a column called "churn". Almost nobody does. Churn is not something a
billing system records; it is a sentence somebody in your business says out loud — *"they've gone
quiet, no activity for two months"* — and nobody has ever written it into a table.

Onboarding is the part where you say that sentence to the engine, and the engine turns your tables
into the one thing a model can be trained on:

> **one row per customer per snapshot date, with a column saying what happened next.**

That row carries the customer's attributes as they stand, a set of numbers summarising their recent
behaviour (complaints in the last 90 days, average bill over six months, days since they last did
anything), and an outcome. The identifying columns get the engine's own names — the key is always
`entity_key` and the date is always `snapshot_date` — whatever your files call them.

Nothing about your files has to be changed first. You upload them as they come out of your systems,
with your column names, your date formats and your codes, and the decisions happen in the engine —
where they are saved, so next month is a replay rather than a repeat.

---

## 2. Roles: what each table *is*

The first question about each file is what kind of table it is. Not what you call it — what shape it
has. The engine calls that its **role**, and there are ten:

| Role | What it is |
|---|---|
| `entity` | One row per customer (or order, or asset), with their attributes. |
| `bills` | One row per invoice. |
| `payments` | One row per payment. |
| `complaints` | One row per complaint or support ticket. |
| `usage` | One row per usage record. |
| `campaign_events` | One row per campaign touch — sent, opened, clicked, converted. |
| `orders` | One row per order. |
| `plan_changes` | One row per plan, tariff or package change. |
| `activity` | Any "the customer did something" log — logins, recharges, visits. |
| `other_event` | An event table that fits none of the above. |

There are really only two kinds here. `entity` is the one table that says **who exists**: one row
per customer, and you need exactly one of them. Everything else is an **event log**: one row per
thing that happened, many rows per customer, each row dated.

**Every event table needs exactly two things: a key and a date.** A column that says which customer
the row belongs to, and a column that says when it happened. That is the whole requirement. Every
other column an event role mentions — `amount` on a bill, `severity` on a complaint, `data_mb` on a
usage record — is optional. If you have it, features can be built from it; if you do not, nothing
breaks. A complaints table that is nothing but a customer id and a ticket date is a perfectly good
complaints table, and you can still count complaints in the last 90 days from it.

`other_event` exists so that a table you cannot categorise is never a dead end. It requires the same
key and date and nothing else, and features count rows in it exactly as they do anywhere else.

### How a role gets proposed

You do not pick from a blank list. When a file is uploaded the engine profiles it and scores every
role in the catalogue against four signals:

- **the file name** — does it contain one of the role's name tokens (`invoice`, `ticket`, `cdr`,
  `promo`, and so on);
- **the columns** — how many of that role's typical columns are present, under any of their known
  spellings;
- **the dates** — how much of the best date-looking column actually parses as a date. For an event
  role a strong date column is evidence *for* the role, but only once the name or the columns have
  already pointed at it, so that a timestamp does not vote equally for all nine event roles. For the
  entity role, the *absence* of a dominant date column is the evidence.
- **the shape** — roughly one row per key looks like an entity table; many rows per key looks like
  an event log.

What comes back is a ranked list with a confidence between 0 and 1 and, for each one, the reasons in
plain language — that the file name contained `invoice`, that a named column parses as a date often
enough to be a usable event time, that there are many rows per key. A role that matched nothing at
all is left out rather than padded to the bottom of the list, so everything you are shown is a real
signal.

**The detector proposes; you decide.** A confident proposal is pre-selected, never silently applied,
and changing it is a click. If the detector had nothing to go on it says so, falls back to
`other_event` for a dated table, and hands you the choice.

---

## 3. Mapping: your column names become ours

A model needs to know that your `TNR_MNTHS` and somebody else's `months_active` are the same thing.
That translation is a **mapping**: one line per column, saying *your name → our name → what to do on
the way*.

Each use case declares the shape it wants. The telco churn use case, for instance, wants
`tenure_months`, `signup_date`, `plan_type`, `region`, `monthly_charges`, `marketing_opt_in` and
`last_contacted_at`, and for each one it carries a list of spellings clients actually use —
`tenure_months` also answers to `tenure`, `tnr_mnths`, `months_active`, `customer_age_months`,
`tenure_mths` and `vintage_months`.

The suggester scores your column against each of ours on five things: an exact alias match, how
close the names look, whether the types are compatible, whether the values overlap the vocabulary we
expect, and whether the numbers fall in a plausible range. An exact alias match alone is worth full
marks — if you spelled it exactly one of the ways the use case says you might, you have told us what
it is and nothing else needs a vote.

The key and the date are handled differently, because no alias list can cover them: an entity key is
`CUST_ID` or `ACCT_NO` or `MSISDN` or whatever your billing system called it. Those two are settled
from the *shape* of the data — which column is unique and rarely null, which column parses as a date
— not from its name.

### The confidence pill

Every suggested line carries a percentage. Two thresholds turn it into behaviour, and both are
configurable (`onboarding.mapping` in `configs/engine.yaml`):

- **85% and above** (`auto_accept_confidence`) — shown as already accepted. You do not have to do
  anything. It is still displayed and still reversible; nothing is decided behind your back.
- **between 50% and 85%** — offered as a suggestion for you to confirm or change. A column still
  sitting below 85% when you build is listed in the build report as `MAPPING_LOW_CONFIDENCE`, a
  warning naming both columns and the percentage.
- **below 50%** (`suggest_confidence`) — not offered at all. A guess nobody can act on costs you a
  decision and buys nothing, so the column is simply left unmapped for you to place yourself.

A low pill is not an accusation. It usually means your column name is nothing like ours, which is
extremely common and entirely fine — you confirm it once and it is confirmed forever.

### Value maps

Names are half of it. Your `plan_type` column might hold `PRE`, `PPD` and `POST` where the engine
expects `prepaid` and `postpaid`; your boolean columns might hold `Y` and `N`, or `1` and `0`, or
`allowed` and `blocked`. A **value map** is the per-value half of a mapping: your value on the left,
ours on the right, built from the vocabularies in the use case and from the values your file
actually contains.

A value the vocabulary does not recognise is deliberately *left out of the map* rather than guessed
at. It comes back as `VALUE_UNMAPPED` — a warning listing the values it could not place — and you
decide: add each one to the map, keep it as it is, or blank it out.

### The inverted flag

This one catches people, so it is worth its own paragraph. Many telcos store consent as a
do-not-disturb flag. `DND_FLAG = Y` means *do not contact this customer*. The engine's column is
`marketing_opt_in`, where `true` means *you may contact them*. Those are the same fact stated in
opposite directions, and mapping one onto the other without noticing would invert your entire
consent list — every customer you must not contact becomes one you may.

The suggester recognises a negatively-phrased name against a positively-phrased one and attaches a
**negate** transform: the mapping line reads `DND_FLAG → marketing_opt_in (negate)`, and the values
are flipped on the way in. It is shown on the mapping screen like any other transform, and you can
take it off if your flag does not in fact mean what its name suggests.

The other transforms work the same way and are equally visible: **cast** (read this as a date, a
number, a category), **value map**, **scale** (multiply by a factor — paise to rupees),
**strip** and **lower** (tidy whitespace and case), **lstrip zeros** (drop the leading zeros that
stop `0012345` matching `12345`), **derive** (compute a column from others), and **dedupe** (keep
only the latest row per customer).

### What happens to a column you do not map

Nothing. It is dropped.

The mapped table is built column by column *from the mapping*, so an unmapped column has no path
into the dataset at all. It is not hidden, not carried along, not quietly turned into a feature. The
build report lists what was left out, and your original file is untouched.

This is the right default. A customer master often carries a sales rep's name, an internal batch id,
a free-text note — columns that mean nothing to a model and can quietly leak information about the
outcome. If you want one of them, map it; if you say nothing, it stays out.

---

## 4. Features: what the model actually looks at

A raw event table cannot be fed to a model — a model needs one number per customer, not fifteen
thousand invoice rows. A **feature** is the instruction that turns the second into the first:

> *count the rows in the complaints table for this customer in the 90 days before the snapshot date.*

That is `complaints_90d`. Three parts: a table (a role), a function, and a window.

### What a window means

A window is a number of days, counted **backwards from the snapshot date**. `window_days: 90` means
the last 90 days before the date this row stands at — not the last 90 days of your file, and not
the last 90 days from today. Each row has its own window, because each row has its own snapshot
date.

Some features have no window at all. `days_since_last` looks back as far as it needs to; a ratio
carries the windows in its two halves instead.

### The function library, in plain words

You pick from this list. You never write SQL.

| Function | What it gives you |
|---|---|
| `count` | How many rows there were in the window. |
| `sum` | The total of one column over the window. |
| `mean` | The average of one column. |
| `min` / `max` | The smallest and largest value of one column. |
| `std` | How spread out that column's values were — steady versus erratic. |
| `nunique` | How many *different* values appeared (how many distinct complaint categories, say). |
| `latest` / `first` | The value of a column on the most recent, or the earliest, row in the window. |
| `days_since_last` | Days since the customer's most recent row in this table. |
| `days_since_first` | Days since their earliest row. |
| `exists` | Simply whether anything happened at all. |
| `ratio` | One aggregate divided by another — recent against typical. |
| `derive` | A small arithmetic expression over the customer's own attributes and the snapshot date. |

`count`, `exists` and the `days_since_*` pair count rows, so they need no column. Everything from
`sum` to `first` aggregates a column, so you name it.

Any feature can carry a **filter**: only complaints where `resolved_time` is empty, only payments
where `days_late` is greater than 0. The comparisons available are equals, not-equals, greater and
less than (with or without "or equal"), one-of-a-list, is-empty and is-not-empty.

`ratio` is how you express a *trend* without any statistics. `bill_trend_3m_vs_6m` is the mean bill
over 90 days divided by the mean bill over 180 days: above 1 means bills are rising, below 1 means
falling. `usage_drop_30d_vs_90d` is the same idea pointed at usage, where a number well below 1 is a
customer going quiet.

You are not starting from an empty page. Each use case ships a list of features worth having — the
telco churn one suggests nine, including the two ratios above — and on top of that the engine
generates a standard library for every event role you mapped: `count`, `sum`, `mean`, `latest` and
`days_since_last` at 7, 30, 90, 180 and 365 days. You accept, reject and add. The "add a feature"
form offers 30, 90 and 180 days as its starting windows. (All of this is
`onboarding.features` in `configs/engine.yaml`; a use case can change any of it.)

### Point-in-time: the single most important idea here

Every feature is computed **strictly as of the snapshot date**. A feature for the 31st of March may
count only things that had already happened by the 31st of March.

That sounds obvious, and it is the rule that is broken most often, because breaking it is invisible.
Suppose you build a row dated the 31st of March, and by accident its "complaints in the last 90
days" counts an April complaint too. The model now sees, in the row for March, a fact that nobody
could have known in March. It learns that customers who are about to complain are about to leave —
which is true, and completely useless, because in production, on the 31st of March, that April
complaint has not happened yet.

The damage is that the model looks *excellent*. Every evaluation number goes up. The dataset
contains the answer, so the model finds it, and nothing anywhere in the pipeline says so. Then it
goes live, the future is no longer available, and the predictions are worth nothing. This failure is
common enough to have a name: leakage.

So the engine enforces the boundary in one place and then checks its own work. The clause that
bounds every event join is written once; every generated query is re-read afterwards to confirm the
clause is still in it; and after the dataset has been assembled the whole feature set is computed a
second time against tables containing extra rows dated *after* the last snapshot. If any feature's
value moves, the build fails with `FUTURE_EVENTS_LEAKED`. That error is not a warning and cannot be
acknowledged or waved through — a build that has seen the future is not a build with a caveat.

One honest exception, and the report tells you about it: attributes taken from your customer master
are as they are in the file you uploaded, because a master usually holds today's values with no
history. See `ENTITY_ATTRIBUTES_NOT_TIME_VERSIONED` in section 7.

---

## 5. The outcome: defining what you are predicting

The outcome — the **label** — is the one column in the dataset that is not a measurement of your
data. It is a definition of your business, and you write it.

There are four ways to express one.

**1. From a column you already have** (`column`). You genuinely do have the answer written down: a
`churned` flag, a `Yes`/`No` column, a status field. The engine reads it. This is the simplest case
and it is also the most restricted: one value per customer cannot describe several different dates,
so it goes with single snapshots only (section 7 says what you see if you try it with periodic ones).

**2. Something happened** (`event_presence`). The outcome is true if a matching event occurs in the
window after the snapshot: they bought again, they upgraded, they clicked the offer. You name the
table and the number of days.

**3. Something stopped happening** (`event_absence`). The outcome is true if *no* matching event
occurs in the window. This is churn, and it is the worked example below.

**4. A number crossed a line** (`value_threshold`). The outcome is true when a condition holds of
the events in the window — spend below some figure, a count above some figure. You give the
condition as a small expression, and you say whether it needs to hold for *any* event in the window
(the default) or for *every* one. When it must hold for every event and there were no events at all,
the honest answer is "unknown", and the row is left unlabelled rather than counted as a positive.

### The worked example

The telco churn use case ships this definition:

```yaml
label:
  name: churn_next_60d
  type: event_absence
  role: activity
  horizon_days: 60
  description: No activity of any kind in the 60 days after the snapshot date.
```

Read it as a sentence: *a customer has churned as of a given date if they did nothing at all — no
login, no recharge, no call — in the 60 days that followed.*

Every part of that is yours to change. Sixty days might be ninety in your business. "Activity" might
be "payments", so that churn means no payment rather than no login. You might add a filter, so that
only certain kinds of event count as being alive. Whatever you choose, it is saved as a document and
applied identically to every row, which is what makes two months' results comparable.

### The horizon

`horizon_days` is how far forward the engine looks from each snapshot date. Sixty days means: for a
row dated the 30th of September, look at the sixty days from the 1st of October to the 29th of
November, and ask whether anything happened.

Features look backwards from the snapshot; the label looks forwards. The two never overlap by so
much as a second: an event stamped at exactly the snapshot instant counts as past, not future (this
is `inclusive_snapshot_time`, on by default), so nothing is counted twice and nothing falls between
the two.

Choosing the horizon is a business decision, not a technical one. It should be the time in which you
could still do something useful about the customer. Sixty days is a reasonable answer for telco
churn; seven would predict a blip, and a year would predict something you cannot act on.

### Censoring — the part that surprises people

Here is the situation. Your extract ends on the 28th of February. You have asked for monthly
snapshots and a 60-day horizon. What is the label for the snapshot dated the 31st of January?

Nobody knows. The 60 days after the 31st of January run to the 1st of April, and your file stops on
the 28th of February. For an absence label like churn, "no activity in the next 60 days" comes back
*true* for that row — not because the customer left, but because there is no data yet to say
otherwise.

That is **censoring**: a snapshot whose outcome window has not finished. Left alone, it is one of
the nastiest bugs in this whole business. The last couple of snapshot dates would show close to 100%
churn, the model would learn that "recent" means "churned", every evaluation metric would look
outstanding, and the model would be useless.

So the engine drops them. A snapshot date whose window runs past the end of your data is removed
from the dataset and reported as `LABEL_HORIZON_CENSORED` — a warning, telling you how many dates
went and why. The end of the data is measured across the whole label table, not per customer, and
deliberately so: a customer whose own last event is ancient is exactly the churner you are looking
for, and cutting off per customer would define the answer in terms of itself.

If you find yourself short of snapshots, the fix is one of two things: get an extract that runs at
least a horizon's worth past your last snapshot date, or shorten the horizon. The report says both.

The engine also drops a snapshot where every single row has the same outcome
(`LABEL_DEGENERATE_SNAPSHOT`, a warning). There is nothing there for a model to tell apart.

---

## 6. Snapshots: how many dates to stand at

A snapshot date is the moment the model is asked to stand at and predict from. There are two modes.

**Single** builds one row per customer, at one date — the end of your data, or a date you pick. This
is the right choice when your file is already one row per customer with the answer in it, and it is
always what a *scoring* run does: score everyone as they stand today.

**Periodic** builds one row per customer per date, monthly or weekly. This is the default, and for
training it is almost always what you want.

The reason is simple arithmetic. Twenty thousand customers scored once gives you twenty thousand
rows. Twenty thousand customers at twelve monthly snapshots gives you two hundred and forty
thousand, and — more importantly — each customer appears in several different situations: the month
their bills started rising, the month after they complained, the quiet month before they left. The
model gets to see the *shape of the run-up* rather than a single still photograph. And because
churn in any one month is rare, more dates is often the only way to get enough positive examples for
a model to learn anything at all.

By default the engine takes the last 12 month-ends (`max_snapshots: 12`, `frequency: monthly`). The
first snapshot sits at least 90 days after your data starts (`min_history_days`), so that the
backward-looking windows have something to look at, and the last sits a horizon's worth before your
data ends, so that the forward-looking label has somewhere to look. Both ends are yours to set; a
date you pick that falls outside your own data comes back as `SNAPSHOT_OUTSIDE_DATA_RANGE` rather
than being silently moved.

One more thing happens quietly and is worth knowing about: if your mapping produced a signup date,
rows dated before a customer signed up are not built at all. A customer three months before they
existed is zero or blank on every behavioural feature, which is exactly the shape of a customer
about to leave — train on both and the model learns the shape of a missing row.

Periodic rows also change how the data is split for training. A customer's March and April rows are
two rows of one customer, and April's features overlap March's outcome window, so a split at random
by row would grade the model on customers — and on months — it has already seen. **Use this
dataset** therefore sets Step 2's split to *time-based on `snapshot_date`*: the earliest snapshots
train, the latest test, the way the model will be used. You can still change it under the advanced
settings; if you choose a random split, the engine keeps each customer's rows together instead.

---

## 7. The build, and what its report tells you

Building runs seven stages, and the screen names them as it goes: reading your tables, choosing the
dates to build, building features (one row per table you mapped), working out the outcome,
assembling the dataset, checking the dataset, saving the dataset. You can cancel; it stops at a
stage boundary.

At the end there is a **build report**, and it is the thing to read. It has four tables — one row
per source, one row per snapshot date, one row per feature, and one row per check — plus the
headline: how many rows, how many customers, how long it took, and whether it passed.

**Errors stop the build. Warnings do not.** If anything blocking was found there is no dataset and
no manifest, only the report, because reading the report is your next action either way.

Here is a real report, from the engine's own end-to-end test on four synthetic tables — 500
customers, a customer master, a billing table, a complaints table and an activity log, with the
telco churn definition of churn above:

```
passed: yes    rows: 1,500    customers: 500    errors: 0    warnings: 2

sources    src_customers    entity        500 rows   join coverage   -
           src_bills        bills      13,000 rows   join coverage 100%
           src_complaints   complaints  1,314 rows   join coverage 100%
           src_activity     activity   23,473 rows   join coverage 100%

snapshots  2024-09-30   500 customers   108 positive   21.6%
           2024-10-31   500 customers    95 positive   19.0%
           2024-11-30   500 customers   100 positive   20.0%
           2024-12-31   dropped: the outcome window is not complete yet

warnings   LABEL_HORIZON_CENSORED
             1 of 4 snapshot dates were dropped because the outcome window is not complete yet.
           ENTITY_ATTRIBUTES_NOT_TIME_VERSIONED
             Subscriber attributes are taken as they are today, not as they were at each snapshot.
```

Three of the four requested dates survived; 500 customers times 3 dates is the 1,500 rows. The
fourth was censored exactly as section 5 describes. Your own numbers will be nothing like these —
this is one test fixture, not a benchmark.

### The checks you are most likely to meet

Each check below is quoted the way it is worded on screen, with the numbers filled in from your own
data. The figures in the quotes here stand in for yours; they are there to show the shape of the
sentence.

**`NO_ENTITY_SOURCE` — error.** *"None of the N tables uploaded holds one row per subscriber, so
there is nothing to build a row for."* Every table you gave has been marked as some kind of event
log, so there is no list of who exists and no row to build. **Fix:** upload the customer master, or
change the role of the table that already is one. If one of your event tables happens to have one
row per customer, it is probably the master in disguise — change its role.

**`ENTITY_DUPLICATE_KEYS` — error.** Your customer table has more rows than customers. *"The
subscriber table has 12,000 rows for 10,000 different subscribers, so 2,000 rows repeat an id."*
Usually this is a table with one row per customer *per month*, or an extract that appended rather
than replaced. **Fix:** if there is a date column on it, keep only the latest row per customer —
that is the `dedupe` transform, one click on the mapping screen, and the report tells you which
column it would use. Otherwise consolidate the rows upstream and upload again. Do not ignore it: two
rows for one customer means two different answers to "what plan are they on".

**`JOIN_KEY_COVERAGE_LOW` — warning below 80%, error below 30%.** *"Only 62% of bills rows belong to
a known subscriber."* The keys in an event table do not match the keys in your master. **Fix:** this
is nearly always a formatting difference rather than missing customers — leading zeros on one side
(`0012345` against `12345`), stray spaces, or a difference of case. The engine tries those
transforms itself, and if one of them would clear the bar it says so as a second warning,
`KEY_FORMAT_MISMATCH`, naming the transform to apply on the mapping screen. Apply it and the two
tables join. If the keys genuinely are different populations — a billing extract covering a
region your master does not — then re-pull one of the two so they cover the same customers. Low
coverage is not cosmetic: every unmatched event row is behaviour that silently never reaches a
feature.

**`DATE_FORMAT_AMBIGUOUS` — error.** *"Is 03/04/2024 the 3rd of April or the 4th of March? 8,200
dates in the bills table read differently depending on the answer."* Your date column is written in
a format that reads two ways, and a wrong guess moves those rows by months — quietly wrecking every
window and every label, with nothing to show for it. The engine refuses to guess. **Fix:** set the
date format on the mapping screen — `%d/%m/%Y` for day first, `%m/%d/%Y` for month first. The whole
column is then read that one way, permanently, including next month.

**`LABEL_HORIZON_CENSORED` — warning.** *"1 of 4 snapshot dates were dropped because the outcome
window is not complete yet."* Section 5 explains this in full. It is not a fault and you will see it
on most builds, because the last snapshot is usually within a horizon of the end of the file. **Fix:
nothing, usually.** Look at how many dates went. If it is one or two out of twelve, that is normal
and correct. If it is most of them, either your extract is too short for your horizon, or your
snapshot end date is later than your data actually runs — extend the extract, or shorten the
horizon.

**`ENTITY_ATTRIBUTES_NOT_TIME_VERSIONED` — warning.** *"Subscriber attributes are taken as they are
today, not as they were at each snapshot: the subscriber table has no column mapped to the date a
row became true."* You are building periodic snapshots from a customer master that holds only
current values. So a customer who moved to a cheaper plan last week has that cheaper plan in their
row for every month of last year, as if they had always been on it. **Fix:** if your systems keep
the history — a slowly-changing dimension, an "effective from" date, a dated monthly extract — upload
that version instead and map its date. If they do not, which is the common case, the dataset is
still worth building; just read any feature built from those columns as describing the customer
*today* rather than at each date, and lean on the event-based features, which are correctly dated.
This is a warning precisely because most client masters look like this, and it is reported precisely
because no metric anywhere will show it to you.

Two more worth knowing, both errors, both about the outcome. `LABEL_ROLE_MISSING` means your outcome
is defined from a table you have not uploaded or mapped — upload it, or define the outcome from a
table you do have. And an outcome read from an existing column, combined with periodic snapshots, is
refused outright: it comes back as `ENTITY_ATTRIBUTES_NOT_TIME_VERSIONED` again, this time as an
error, because a single flag per customer cannot say what was true at each of twelve different
dates. Build a single snapshot instead, or define the outcome from events and a horizon.

After the onboarding checks, the full standard data-contract validation runs again on the assembled
dataset, so anything in [`DATA_CONTRACT.md`](DATA_CONTRACT.md) — too few rows, too few positives, a
suspicious column — can also appear in the same report.

---

## 8. Next month

Everything you decided is saved as a **recipe**: which table played which role, every column
mapping, every transform and value map, the features, the outcome definition and the snapshot rule.

Next month you upload the new extracts and replay it. No mapping screen, no confidence pills, no
decisions — the same transforms applied to the same columns, producing the same shape of dataset.
Every transform is a pure function of the data and the saved mapping, with no clock and no
randomness in it, which is exactly what makes an unattended replay safe.

On screen this is **Score new data → Upload this month's tables**. The recipe replayed is the one
the model you are scoring with was trained on — the model knows which dataset it learned from, and
the dataset knows its recipe — so there is nothing to pick. Each new file is matched to one of last
month's: by the same file name, or, failing that, by the role the engine detects for it. Each
matched file gets last month's mapping, and its role, unchanged.

The mapping screen comes back in exactly one case: a file that no longer has a column last month's
mapping read — a renamed export field, a dropped column. Only that file's mapping is reopened, with
the missing column named at the top; point one of the file's own columns at what the old one meant,
or save the mapping without it, and the replay carries on. A table the recipe needs that this
month's upload does not include is named too, and nothing is built until it is uploaded. A new
column that was not there last month is left out, exactly as an unmapped column was the first time.

The recipe is also what makes a result auditable months later. Each built dataset records which
recipe produced it, which mappings it applied and a fingerprint of every source file it read, so
"why was this customer flagged in March" has an answer that does not depend on anybody's memory.

Two sensible habits. First, when your source systems change — a new column, a renamed field, a new
plan code — expect a warning on the next build rather than silence: a new plan code shows up as
`VALUE_UNMAPPED` and you add it to the map. Second, a scoring run always stands at a single date at
the end of the new data and skips the outcome entirely, because there is nothing to look forward to
yet. That is automatic; you never configure a second set of dates, which is what stops the scoring
recipe and the training recipe from drifting apart.
