# M99 review (Claude Code, 2026-10-08)

Branch `plan-j/m99-catalogue` at `01e923e`. The partner agent's third milestone.

**Verdict: not mergeable as pushed. The milestone's main feature did nothing in a real run.** Several
parts were sound:
- the Alembic migration (nullable `channel`, so existing records keep applying to all channels)
- the ledger's channel filter
- the frozen catalogue model, with data-driven region rules
- the `action_id` check at config load

The partner's 9 tests (`tests/unit/decide/test_catalogue.py`, `tests/integration/decide/test_catalogue_consent.py`
as pushed) pass on `01e923e`. But the acceptance tests passed for the reason the §4.7 checklist warns about.
Claude Code fixed the findings on the way to `main`; the fix commit is the reference, and
`docs/handoff/M99.md` says what changed, file by file.

## Blocking (fixed before merge)

1. **Channel consent never reached a real run's treat list.** The treat list looked for the consent and
   contactable columns in `scores.parquet`. A real run never writes them: `scores.csv` and `scores.parquet`
   carry `scores_csv_columns()` and nothing else (DEC-083). So every customer counted as contactable on every
   channel. Both acceptance tests wrote `scores.parquet` by hand with those columns, the exact pattern of the
   M97 and M98 findings. *Lesson, third time: build the run with the pipeline, then test the output.*
   **Fixed:** per-channel contactability is now computed during the run, right after the actions stage, while
   the input columns still exist (`engine/decide/contactability.py`, installed in the Plan J block of
   `engine/pipeline.py` like the holdout service and the gross value). It is written as a row-level Plan J
   artefact, `channel_contactability.parquet` (key columns and one `contactable_<channel>` flag per channel;
   registered in `configs/privacy.yaml`, `engine/privacy/layout.py` and `ROW_LEVEL_ARTEFACTS`), with its
   counts in `channel_contactability.json`, and the treat list joins it on every key column. Only a use case
   that sets `actions.suppression.channels` engages it. Regression tests, on runs built by `Pipeline.run_score`:
   `tests/integration/decide/test_channel_consent_real_run.py` (uplift, with the ledger) and
   `tests/integration/decide/test_channel_consent_real_propensity.py` (`slow`, the band path).
2. **The consent ledger's channel was never consulted.** `ConsentLedger.classify(channel=...)` existed, but
   nothing called it. A customer who opted out of SMS in the ledger was still sent SMS. **Fixed:** when the
   ledger gates the run (`consent_gate_for_run`, the same lookup the actions stage makes), it feeds each
   channel's contactability: `test_only_sms_planned_and_opted_out_by_the_ledger_is_not_treated`.
3. **A suppression rule that never ran was invented.** When `opted_out` did not run, `_suppression_counts`
   appended `SuppressionCount(reason="opted_out", rows=0, ...)`, an entry for a rule that did not run. The
   counts also covered every row (already-suppressed rows, controls, the floor band). **Fixed:** no entry is
   ever added. `channel_counts` counts, per channel, the rows eligible to be treated (neither suppressed nor
   held out as control) that are not contactable on that channel; they ride on the `opted_out` entry, else on
   the `consent_false` entry, and when neither rule ran they are only in `channel_contactability.json` and the
   treat list summary. `test_no_suppression_entry_is_invented_for_a_rule_that_did_not_run` (real run) and
   `test_no_entry_is_invented_for_a_rule_that_did_not_run` (unit).
4. **`scripts/bench_1m.py` edited for the third time,** after M97's review reverted it and M98's review
   said to leave it. Reverted to `main`'s version. *A file outside the milestone is never yours, however
   small the fix.*
5. **A shipped catalogue with made-up data.** `configs/decide/catalogue.yaml` shipped an invented DLT
   template id (`110712345678`) and invented ₹45 offers. Because the file existed, every deployment ran with
   a catalogue: action-id validation on, channel costs overridden, a hash stamped. **Fixed:** the file is
   gone, so defaults are unchanged, and the example lives in `configs/decide/catalogue.example.yaml`,
   labelled as an example and never read. Its DLT template id is the placeholder `<your DLT template id>`,
   which counts as missing, so a copy made without filling it in is refused with
   `ACTION_DLT_TEMPLATE_MISSING`. `test_no_catalogue_ships_and_with_none_nothing_changes` and
   `test_the_shipped_example_is_refused_until_its_placeholders_are_filled`.

## Should fix (fixed)

- `lookup_value_costs(channel=...)` took the offer cost of the *first* catalogue action on that channel.
  An offer's cost belongs to the action, not the channel. Offer cost now comes only from an action id; a
  channel gives a contact cost only (`test_lookup_value_costs_offer_cost_comes_only_from_an_action`).
- `ActionItem.channel` was "one channel or a comma-separated list" in a string. It is now
  `channels: list[str]` (a single `channel:` is still accepted; a comma in it is refused).
- `_truthy` (private to `engine/stages/actions.py`) was imported from two other modules. It is now a
  public helper in `engine/decide/contactability.py` (`truthy`), one documented import of Phase 1's rule;
  `engine/stages/actions.py` itself is `main`'s, byte for byte (the first fix made it public there, which
  breaks the Phase 1 edit rule; the second review reverted that).
- The hand-off's self-check claimed "defaults unchanged" and "stay in your lane" while a shipped
  catalogue changed every run's configuration and `bench_1m.py` was edited. `docs/handoff/M99.md` §2, §3 and
  §8 are rewritten to be true.

## Found while fixing (not in the first review)

- **Every default run's files changed.** The new optional fields were serialised as `null` / `{}`:
  `"channel_counts": null` on every `SuppressionCount` of every `scoring_summary.json`, and `action_id`,
  `channels` and `treat_action_id` in every `run_config.json`. They are now left out while unset
  (`exclude_if`, as DEC-1242 and M93 did), so a default config dumps exactly as on `main`.
- **An old SQLite `platform.db` broke ledger reads.** `consent_record.channel` was added to the model, but a
  laptop's `platform.db` made before M99 gets no new column from `create_all`, and every ledger read now
  selects it, so a gated scoring run would fail with "no such column". The column is now added in place
  (`add_missing_columns`, also when the scoring seam opens the ledger with `create=False`):
  `test_an_old_sqlite_platform_db_keeps_working_for_the_scoring_seam`.
- The catalogue cache was keyed by root only, so an edited catalogue was not read again in a running
  process while `catalogue_sha256` already reported the new file. It is now keyed by content too.

## Second review (correctness; compatibility and protocol), fixed

- **Major: a Phase 1 stage file edited outside the four functions Plan J may edit.** Reverted;
  `engine/decide/contactability.py::truthy` reads `actions._truthy` through one import.
- **Major: code added to the shared `engine/config.py` outside its PLAN-J block.** `ChannelSuppressionConfig`,
  the channel-name rule and the field type live in `engine/decide/spec.py` (imports nothing from `engine`,
  the M92 pattern); `validate_action_ids` lives in `engine/decide/catalogue.py`. `config.py` keeps the import,
  the two fields and the two one-line calls, all listed in `docs/handoff/M99.md` §2.
- **The treat list read the catalogue as it was when the list was built.** A run now writes
  `catalogue_stamp.json`; the treat list plans from it and says when the file has changed
  (`test_a_catalogue_edited_after_the_run_changes_nothing_the_treat_list_plans`).
- **A consent channel no configuration could name was stored.** Refused: `CONSENT_CHANNEL_INVALID`
  (`test_an_import_refuses_a_channel_no_configuration_can_name`).
- **The default real-run test ran with a catalogue present.** Its variant now has none, and the test checks
  `catalogue_sha256` is null.
- **Docs.** `docs/handoff/M99.md` §8.5 names the treat list's reviewed change (a `contactable_channels`
  column and a `catalogue_sha256: null` field on every treat list); `docs/DECIDE.md` §12 is up to date.

## What was good

- The migration and the ledger filter: `null` = all channels, so existing records stay valid.
- Region rules read from `configs/regions/<region>.yaml`, so no region name appears in Python.
- An unknown `action_id` refused at config load, with a clear path in the error.
- The bitmask for `contactable_channels`: neat, and linear in rows, but its label table was built for all 2^k
  combinations of channels (exponential in channels); the second review labels only the patterns present.
- Golden files regenerated with `--update`, and the change explained.

## For M101

- Before trusting a test, check that the data it feeds in could come out of the real pipeline.
- Do not touch files outside the milestone. Raise an open question instead.
- Ship real configuration empty or absent. Put examples in an `.example` file.
- A new optional field on a contract or a config is left out of the file while unset.
