# Cross-branch requests

Changes one branch needs and another branch owns. A request is **not** a patch: the branch that
needs the change describes the smallest version of it, says what it is blocked on, and moves on.

Opened by the `library/` branch (public dataset library). Numbering follows that branch's decision
range: **CBR-4xx** pairs with **DEC-4xx** in `docs/DECISIONS.md`.

| Id | Owner | Blocking? | One line |
|---|---|---|---|
| [CBR-401](#cbr-401) | `tests/` | **yes** | Two assertions forbid a second industry or a seventh use case |
| [CBR-402](#cbr-402) | `engine/` | no | `prepare` and `ingest` disagree about what is PII; ISO dates get redacted |
| [CBR-403](#cbr-403) | `engine/` | no | Template column names cannot contain dots, so real files must be renamed |
| [CBR-404](#cbr-404) | `engine/` | no | `threshold.mode: auto` can settle on "everything is positive" |

---

## CBR-401

**Two test assertions forbid adding any industry or use case to `configs/`.**

*Owner:* whoever owns `tests/unit/test_config_loading.py`.
*Blocking:* **yes** — it is why the library's four industries and four use cases are not in
`configs/`.

The library's whole point is to show that a second, third and fourth industry need configuration
and nothing else. Adding `configs/industries/banking.yaml` does exactly that, and it turns the test
suite red:

```
tests/unit/test_config_loading.py::test_industries_list_and_telecom_loads
    assert list_industries() == ("telecom",)          # line 105
```

Adding `configs/use_cases/bank_term_deposit.yaml` turns a second one red, because it asserts that
the telecom industry file lists *every* shipped use case:

```
tests/unit/test_config_loading.py::test_industry_available_entries_have_files_and_matching_stage_names
    assert sorted(available) == sorted(list_use_case_ids())   # line 136
```

A third would follow: `tests/unit/test_generated_files.py::test_templates_are_up_to_date` fails
until `templates/` carries the generated pair for each new use case, and `templates/` is not the
library branch's to write either.

**Reproduced**, on a clean checkout:

```
$ cat > configs/industries/__probe.yaml <<'YAML'
schema_version: 1
id: probe
name: Probe
stages:
  - {id: awareness, name: Awareness, ai_type: predictive,
     use_cases: [{id: targeted-advertisement, status: available}]}
YAML
$ python -m pytest tests/unit/test_config_loading.py -k industries_list
FAILED test_industries_list_and_telecom_loads - Left contains one more item: 'telecom'
```

**Smallest change.** Make both assertions about the telecom industry rather than about the whole
config directory:

* `assert "telecom" in list_industries()` instead of `== ("telecom",)`;
* in `test_industry_available_entries_have_files_and_matching_stage_names`, assert that every id
  telecom lists as available has a file and a matching stage name — which the loop above the
  assertion already checks — and drop the `== sorted(list_use_case_ids())` line, or narrow it to
  the seven ids telecom actually owns.

Nothing in `engine/` has to change: `load_industry` already validates each file against its own
root, and `list_industries` already globs. The restriction is only in the tests.

**Until then.** The library ships its configs in `library/configs/`, a second config root the
engine already supports (`MARKETING_AI_CONFIG_DIR`, or `root=` on every loader, DEC-038). Its
`engine.yaml` is a symlink to the real one, so there is no second copy of the defaults to drift.
Moving them into `configs/` afterwards is `git mv` plus `make generate`; see DEC-400.

---

## CBR-402

**`prepare` and `ingest` implement PII detection twice, and they disagree.**

*Owner:* whoever owns `engine/stages/prepare.py`.
*Blocking:* no — the library reports what happened and carries on.

`engine/stages/validate.py:390` says:

> PII detection has exactly **one** definition in the engine - `engine.stages.ingest.detect_pii`

It does not. `engine/stages/prepare.py:511` has a second one, `_detect_pii`, with its own
`_PII_VALUE_PATTERNS`, and the two differ in three ways that matter:

| | `ingest.detect_pii` | `prepare._detect_pii` |
|---|---|---|
| Type gate | skips FLOAT, BOOLEAN, DATE and DATETIME columns (`_PII_TYPES`, line 824) | none — examines every column |
| Match | `pattern.fullmatch` | `pattern.search` |
| Threshold | per-detector `min_value_match_rate` **and** `min_distinct_ratio` | a flat 50 % of sampled values |

**What it did to a real file.** The phone-number pattern is
`\+?\d[\d\s().-]{7,17}\d`, and an ISO date matches it:

```
>>> re.compile(r'\+?\d[\d\s().-]{7,17}\d').fullmatch('2011-09-10')
<re.Match object; span=(0, 10), match='2011-09-10'>
```

On `library/online-retail`, whose `snapshot_date` is `2011-09-10` in every row, the run produced:

* `validation.json` — **one** finding, `CONSTANT_COLUMN` on `snapshot_date`. No `PII_DETECTED`,
  because ingest's type gate correctly skips a DATE column.
* `prepare.json` — `pii_columns: ["snapshot_date"]` and a `redact` transform replacing its values
  with `[REDACTED]`.

So the engine told the user one thing and did another. Here the column was constant and headed for
the bin anyway, which is why this is not blocking. On a file with several snapshot dates — the
shape `split.type: time_based` exists for — a live date column would be silently redacted with
nothing in the validation report to say so.

**Smallest change.** Delete `prepare._detect_pii` and `_PII_VALUE_PATTERNS` and call
`ingest.detect_pii` per column, which is what the validate docstring already claims happens. If the
two must stay separate, give `prepare._detect_pii` ingest's type gate — the one line
`if inferred not in _PII_TYPES: return ()` — so a date column is never examined for phone numbers.

**Run to reproduce:** `library/.runs/online-retail/`, produced by
`python -m library.run_engine --dataset online-retail --use-case retail-win-back
--config-root library/configs --csv library/online-retail/data/prepared.csv
--primary-key customer_id --target reactivated_90d`.

---

## CBR-403

**A template column name cannot contain a dot, so published files have to be renamed.**

*Owner:* whoever owns `engine/config.py`.
*Blocking:* no — `fetch.py` renames them and `mapping.yaml` records it.

`TemplateColumn.name` is `Annotated[str, Field(..., pattern=r"^[A-Za-z_][A-Za-z0-9_]*$")]`
(`engine/config.py:829`). Two of the six public datasets carry dotted column names, and one of them
is a target:

| Dataset | Published name | Renamed to |
|---|---|---|
| UCI Bank Marketing | `emp.var.rate`, `cons.price.idx`, `cons.conf.idx`, `nr.employed` | `emp_var_rate`, `cons_price_idx`, `cons_conf_idx`, `nr_employed` |
| UCI Default of Credit Card Clients | `default.payment.next.month` | `default_payment_next_month` |

A dotted name is legal in a CSV header, legal in pandas and legal in AutoGluon. The engine accepts
it as an ordinary feature; it is only the **template** that refuses it, and `_check_template` then
rejects the whole use case with `TEMPLATE_TARGET_MISMATCH` when the target is one of them. The
workaround is to leave `template.columns` empty, which costs the use case its generated template
and its `TARGET_MISSING` hint.

This is a small friction, and renaming in preparation is arguably the right answer anyway — it is
what `mapping.yaml` is for. Filed because nothing currently *says* so: a user who uploads
`bank-additional-full.csv` with a matching use case discovers the restriction as a config-load
error.

**Smallest change.** Either widen the pattern to allow interior dots
(`^[A-Za-z_][A-Za-z0-9_.]*$`) — the name is only used as a CSV header and a dictionary key — or
add one line to `docs/DATA_CONTRACT.md` §7 saying template column names are identifiers, so a file
with dotted headers needs a mapping step. The second is cheaper and probably better.

---

## CBR-404

**`evaluation.threshold.mode: auto` can settle on a threshold that calls every row positive.**

*Owner:* whoever owns `engine/stages/evaluate.py`.
*Blocking:* no.

`auto` maximises F1 on the validation split. On a weak model over a base rate near 50 % that
maximum really can be at "predict everything positive", and on `library/online-retail` it was:

```
threshold        0.3148   (mode: auto)
recall           1.0
specificity      0.0
precision        0.4110   == the test-split positive rate
accuracy         0.4110
```

Nothing is mis-computed — F1 genuinely is highest there — and the score ranking, the bands and the
decile chart are unaffected, because those read the score and not the threshold. But the Model page
would report 100 % recall, which reads as a triumph and means the model made no decision at all.

**Smallest change.** In the `auto` search, skip candidate thresholds whose confusion matrix has no
predicted negatives (or no predicted positives), and fall back to the best remaining one; if none
qualifies, fall back to 0.5 and say so in `threshold_detail`, which already exists to carry exactly
that sentence.

**Run to reproduce:** `library/.runs/online-retail/`, same command as CBR-402.
