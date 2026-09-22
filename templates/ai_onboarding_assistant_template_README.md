# AI Onboarding Assistant — upload template

One row per question. Keep the header row exactly as it is.

## Columns

| Column | Role | Type | Description |
|---|---|---|---|
| `question` | primary key | string | A question a new customer asks, exactly as they would ask it. Asked once, so no two rows repeat one. |
| `expect_refusal` | feature | boolean | True when the documents cannot answer and the assistant must say so rather than guess. |
| `source_doc` | feature | string | The document the answer comes from, used to check that the right passage was retrieved. Blank for a refusal. |
| `reference_answer` | target | text | The answer the documents support, in your own words. Leave blank when the question should be refused. |

## Required

- `question` — the primary key: it identifies each row and is never used as a feature.
- `reference_answer` — the target: required for training, leave blank when scoring.

## Limits

At least 40 rows and 1 positive examples; maximum file size 2048 MB.
