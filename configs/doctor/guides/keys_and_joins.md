# Guide: keys and joins (read when two or more tables must meet, or before trusting an ID column)

## Rules
- Say what each key names (person, account, household, order) first. Joins never lose or double customers silently; the receipt must prove it.
- Exact keys only (R1): never similar names, name plus city, or matching column names. Ask for a shared ID instead.
- Check coverage first: per pair, matched/unmatched shares and two example unmatched IDs in the plan (R1); `describe_repeats` on each key.
- IDs are text (R3); pad zeros only after Q3 gave the length.
- Rounded IDs cannot be joined: say IDS_ROUNDED, ask for a re-export as Text.
- Lookup side must be unique (R22).
- The customer list is the base: each row stays once. Dropping over 2% of a file's rows needs the person's own yes.

## ID hygiene
- Strip `'` and `="00123"`; keep case; strip +91 (or leading 91 on 12 digits) from phones. Write one file's IDs one way in a typed step or query; the match key never rewrites the customer list's IDs.
- Rounded: `1.2345E+11`, 15-digit IDs ending 000.
- Run a query with `expect` (`max_keys_lost_pct`) before proposing a join; IDs missing from the base lose rows: say how many.
- `describe_duplicates` on lookups; a key repeating with different values: ask which copy wins (Q11) or keep the latest row per ID first.
- Codes not found: "Unmapped", never "Other"; count and ask.

## Several IDs
- (R2) Customer ID, phone, email-hash, loyalty number: ask which is main (Q1). Build one crosswalk (master ID, ID type, cleaned value); join each event table through it; report matched share per ID type.
- Two IDs for one person that disagree: ask (Q2); never merge people silently.

## Households
- (R29) Same group on one side of the train/test split; de-duplicate contacts by household; ask Q1 which level is contacted.

## Grains
- Never join a lines table straight onto customers (totals multiply): aggregate per customer first (grain_and_aggregation). Blank customer IDs are guests: counted, left out.
- Traps: a key matching 100% (row number); blank keys matching each other.

## Ask when
- Main ID unclear (Q1), several IDs per person (Q2), ID length or case unclear (Q3), or the second file may be an extract (Q20).
- Nothing joins: say which ID is missing, use `request_data`; no fuzzy match.
