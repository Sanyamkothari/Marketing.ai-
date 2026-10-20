"""What counts as a yes (Q24): the person's answer decides the outcome the model learns from.

The outcome is meaning, so the AI only offers; the person's click records (`engine.doctor.visit.answer`). This module
builds the offers, with counts our code measured, and refuses what cannot be a safe outcome. Two cases, both small:

* **a column the file has** ("converted_30d = 1"): the outcome column and the values that mean yes, as an
  `OutcomeDefinition` of kind `column_values` (the prescription's outcome, M239). Every other value is a no. The
  click is its approval.
* **a rule over another file or a threshold** ("a sale is an order over 500 within 30 days of contact"): ONE
  `sql_query` step the agent proposed (the same sandbox, guards and `expect` as any query) builds a 0/1 column on
  the customer table; that column is the outcome. The query is run again here to count the customers and the yes
  rate, and to check it: one row per customer, only 0 and 1, both present, an as-of date, and the leak rule of the
  query step - the rows dated after the as-of date are the outcome window, so every other column must not move when
  they are taken out (`future_rows_moved`; the outcome column alone is let off, and only for a query that says
  `reads_future`). The click accepts the pending query step with the answer.

The sentence the Ready card shows (`says`) is the person's rule in plain words and the count: "a sale = an order over
500 within 30 days: 312 of 4,000 customers". Nothing here reaches the AI service.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import TYPE_CHECKING, Any, Final

import pandas as pd

from engine.agent.contracts import ProposalState
from engine.agent.sql_step import (
    THIS_TABLE,
    SqlStepError,
    future_rows_moved,
    resolve_as_of_rule,
    run_sql,
    sql_params,
)
from engine.doctor.case import payload_digest
from engine.doctor.outcome_definition import (
    OutcomeError,
    propose_value_map,
    value_text,
)
from engine.doctor.prescription import slot_name
from engine.doctor.visit_contracts import OptionEffect

if TYPE_CHECKING:
    from engine.doctor.case import CaseItem
    from engine.doctor.diagnose import LoadedFile

__all__ = [
    "OUTCOME_ASK_CODE",
    "OUTCOME_KIND",
    "OutcomeOffer",
    "column_offer",
    "other_changes",
    "outcome_query",
    "outcome_says",
    "query_item",
    "rule_tables",
    "rules_payload",
    "sql_offer",
    "step_digest",
]

OUTCOME_ASK_CODE: Final[str] = "Q24_WHAT_COUNTS"
OUTCOME_KIND: Final[str] = "outcome_column"
"""The item kind the answer adds: the same the rules' OUTCOME_COLUMN answer adds, so the prescription, the readiness
check and the run's start read it without change."""


@dataclass(frozen=True)
class OutcomeOffer:
    """One answer of the question: what the person reads, and what the click records."""

    label: str
    says: str
    effect: OptionEffect
    effect_text: str = ""
    """What the click does, in a plain sentence (under the answer)."""


def _plain(rule: str) -> str:
    return " ".join(rule.split()).rstrip(".").strip()


def _counts(rows: int, yes: int, no: int) -> dict[str, Any]:
    return {"rows": rows, "yes": yes, "no": no, "yes_rate": round(yes / (yes + no), 4) if yes + no else None}


def _says(rule: str, rows: int, yes: int) -> str:
    return f"{_plain(rule)}: {yes:,} of {rows:,} customers"


def _label(says: str, yes: int, rows: int) -> str:
    return f"{says} ({yes / rows:.1%})" if rows else says


def outcome_says(item: CaseItem) -> str | None:
    """The plain-words sentence of a ticked outcome the person's answer recorded, or None."""
    says = item.payload.get("says")
    return says if isinstance(says, str) and says else None


# ---------------------------------------------------------------------------
# A column the file has
# ---------------------------------------------------------------------------
def column_offer(
    frame: pd.DataFrame,
    column: str,
    yes: Sequence[Any],
    *,
    upload_id: str,
    rule: str = "",
) -> OutcomeOffer:
    """ "`column` = `yes` is a yes, every other value a no", with the counts. `OutcomeError` when a yes value is not
    in the column, when no value is left for no, or when the column has too many values to be a yes/no."""
    if column not in frame.columns:
        raise OutcomeError("OUTCOME_CANNOT_MEASURE", f"There is no column '{column}' in this file.")
    texts = frame[column].astype(object).map(value_text)
    seen = {str(v): str(v) for v in texts.dropna().unique()}
    folded = {v.casefold(): v for v in seen}
    wanted: list[str] = []
    for given in yes:
        key = value_text(given)
        found = seen.get(key or "") or folded.get((key or "").casefold())
        if found is None:
            raise OutcomeError(
                "OUTCOME_UNDECIDED",
                f"'{column}' has no value '{str(given)[:40]}'; name a value as the file has it.",
            )
        if found not in wanted:
            wanted.append(found)
    if not wanted:
        raise OutcomeError("OUTCOME_UNDECIDED", "Name the value or values of the column that mean yes.")
    answers: dict[str, Any] = {v: ("yes" if v in wanted else "no") for v in seen}
    if all(m == "yes" for m in answers.values()):
        raise OutcomeError(
            "OUTCOME_UNDECIDED", f"Every value of '{column}' would be a yes; a no is needed too."
        )
    found_map = propose_value_map(frame, column, given=answers)
    definition, counts = found_map.definition, found_map.counts
    if definition is None or counts is None:  # pragma: no cover - every value was given a meaning above
        raise OutcomeError("OUTCOME_UNDECIDED", f"'{column}' cannot be read as a yes and a no.")
    shown = " or ".join(f"{v}" for v in wanted)
    words = _plain(rule) or f"“{column}” = {shown} is a yes"
    says = _says(words, counts.yes + counts.no, counts.yes)
    payload: dict[str, Any] = {
        "upload_id": upload_id,
        "column": column,
        "definition": definition.model_dump(mode="json"),
        "says": says,
        "counts": _counts(counts.yes + counts.no, counts.yes, counts.no),
    }
    return OutcomeOffer(
        label=_label(says, counts.yes, counts.yes + counts.no),
        says=says,
        effect=OptionEffect(option_id="c0", kind=OUTCOME_KIND, title=says, payload=payload),
        effect_text=f"Your click makes “{column}” the outcome the model learns, with these values as the yes.",
    )


def rules_payload(frame: pd.DataFrame, column: str, positive_label: Any = None) -> dict[str, Any]:
    """What a chip of the rules' own outcome question records beyond the column: the counts and the sentence the
    Ready card shows. Never a value map: the chip's outcome is the column, as it always was, so training does not
    start to drop rows or stop on a new value. Nothing extra when the values cannot be read as yes and no."""
    try:
        found = propose_value_map(frame, column, positive_label=positive_label)
    except OutcomeError:
        return {}
    if found.definition is None or found.counts is None:
        return {}
    counts = found.counts
    yes = [m.value for m in found.definition.map if m.meaning == "yes"]
    says = _says(f"“{column}” = {' or '.join(yes)} is a yes", counts.yes + counts.no, counts.yes)
    return {"says": says, "counts": _counts(counts.yes + counts.no, counts.yes, counts.no)}


# ---------------------------------------------------------------------------
# A rule over another file: one query step
# ---------------------------------------------------------------------------
def rule_tables(files: Sequence[LoadedFile]) -> dict[str, pd.DataFrame]:
    """Each file as a query names it (`customers.csv` is `customers`, read as `t_customers`), as the query tool does."""
    out: dict[str, pd.DataFrame] = {}
    for file in files:
        base = slot_name(Path(str(file.file_name or "").strip() or file.upload_id).stem)
        name, n = base, 2
        while name in out:
            name, n = f"{base}_{n}", n + 1
        out[name] = file.frame
    return out


def sql_offer(
    step: Mapping[str, Any],
    column: str,
    files: Sequence[LoadedFile],
    customers: LoadedFile,
    key: str,
    *,
    rule: str,
    items: Sequence[CaseItem] = (),
) -> OutcomeOffer:
    """The offer of a query step (its recipe-step dump) whose table has the 0/1 `column` on `customers`.

    Run again here on the whole files the case holds. Refused (`OutcomeError`) unless: the query states an as-of
    date; its table has one row per customer, the ID and the column; the column is only 0 and 1 with both present;
    the rows dated after the as-of date change nothing but that column; and the case has no other ticked build or
    query (this query reads the files by name and its table is the one the model learns from).
    """
    if step.get("kind") != "sql_query":
        raise OutcomeError("OUTCOME_CANNOT_MEASURE", "The outcome of a rule is built by one query step.")
    name = (step.get("params") or {}).get("output_name")
    if other_changes(items, str(name)):
        raise OutcomeError(
            "OUTCOME_QUERY_ALONE",
            "This case already has a ticked join, stack or query. The outcome query makes the table the model "
            "learns from, so it cannot sit beside them: untick them first, or use a column the file has.",
        )
    try:
        params = sql_params(step.get("params") or {})
        tables = rule_tables(files)
        read: dict[str, pd.DataFrame] = {}
        for name in params.inputs:
            frame = customers.frame if name == THIS_TABLE else tables.get(name)
            if frame is None:
                raise OutcomeError(
                    "OUTCOME_CANNOT_MEASURE", f"The query reads '{name}', which is not a file here."
                )
            read[name] = frame
        used: date | None = params.as_of
        rule_of = params.as_of_rule
        if rule_of is not None:
            source = read.get(rule_of.table)
            if source is None or rule_of.column not in source.columns:
                raise OutcomeError(
                    "OUTCOME_CANNOT_MEASURE", "The query's as-of date reads a column it does not have."
                )
            used = resolve_as_of_rule(rule_of, source[rule_of.column])
        if used is None:
            raise OutcomeError(
                "OUTCOME_NEEDS_AS_OF",
                "State the date the outcome is measured from (as_of or as_of_rule, used as $as_of in the query): "
                "the orders after it are the outcome, the rows before it are all the model may learn from.",
            )
        run = run_sql(params.sql, read, output_name=params.output_name, as_of=used)
        table = run.frame
        moved = future_rows_moved(
            params.sql,
            read,
            output_name=params.output_name,
            as_of=used,
            ignore=(column,) if params.reads_future else (),
        )
    except SqlStepError as exc:
        raise OutcomeError(exc.code, exc.message) from exc
    if moved:
        raise OutcomeError(
            "FUTURE_EVENTS_LEAKED",
            f"The query's table changed in {moved:,} rows when the rows dated after {used} were left out of its "
            "inputs: it uses what was not known on that date for more than the outcome. Only the outcome column may "
            "read the window after the as-of date (and the query must say reads_future); every other column must "
            "come from before it.",
        )
    for needed in (key, column):
        if needed not in table.columns:
            raise OutcomeError("OUTCOME_CANNOT_MEASURE", f"The query's table has no column '{needed}'.")
    if len(table) != len(customers.frame) or table[key].duplicated().any():
        raise OutcomeError(
            "OUTCOME_CANNOT_MEASURE",
            f"The query's table must have one row for each of the {len(customers.frame):,} customers "
            f"({len(table):,} rows now).",
        )
    values = pd.to_numeric(table[column], errors="coerce")
    if values.isna().any() or not values.isin((0, 1)).all():
        raise OutcomeError(
            "OUTCOME_CANNOT_MEASURE", f"'{column}' must be 1 for a yes and 0 for a no on every row."
        )
    yes, no = int((values == 1).sum()), int((values == 0).sum())
    if not yes or not no:
        raise OutcomeError(
            "TARGET_CONSTANT",
            f"Every customer would be a {'yes' if yes else 'no'}; there is nothing to tell apart.",
        )
    alone = _alone_decides(table, column, key, values)
    if alone is not None:
        raise OutcomeError(
            "OUTCOME_FROM_A_FEATURE",
            f"'{alone}' on its own tells every yes from every no: the outcome is built from it, so the model would "
            "only learn the rule back. Leave that column out of the query's table.",
        )
    says = _says(rule or params.description, len(table), yes)
    payload: dict[str, Any] = {
        "upload_id": customers.upload_id,
        "column": column,
        "says": says,
        "counts": _counts(len(table), yes, no),
        "outcome_sql": params.output_name,
        "step_digest": step_digest(step),
    }
    return OutcomeOffer(
        label=_label(says, yes, len(table)),
        says=says,
        effect=OptionEffect(option_id="c0", kind=OUTCOME_KIND, title=says, payload=payload),
        effect_text=(
            f"Your click adds the query that builds “{column}” (1 is a yes) on your customer table, and makes it "
            "the outcome the model learns."
        ),
    )


def _alone_decides(table: pd.DataFrame, column: str, key: str, outcome: pd.Series[Any]) -> str | None:
    """A number column that separates every yes from every no by itself (a rank AUC of 1 either way): the rule
    was written on it, and leaving it in the table lets the model read the answer off it. Read on the rows the
    column has a value for (when it has gaps, only with 20 rows or more and both a yes and a no among them).
    """
    for name in table.columns:
        if name in (key, column):
            continue
        numbers = pd.to_numeric(table[name], errors="coerce")
        known = numbers.notna()
        if numbers[known].nunique() < 2 or (not known.all() and int(known.sum()) < 20):
            continue
        yes = (outcome == 1)[known]
        n1, n0 = int(yes.sum()), int((~yes).sum())
        if not n1 or not n0:
            continue
        ranks = numbers[known].rank()
        auc = (float(ranks[yes].sum()) - n1 * (n1 + 1) / 2) / (n1 * n0)
        if auc >= 0.999 or auc <= 0.001:
            return str(name)
    return None


def step_digest(step: Mapping[str, Any]) -> str:
    """A digest of the whole step (the query, its inputs, name, as-of date and rule, `reads_future`, description):
    what the person is shown is this exact step, and the click ticks only a step with this digest."""
    import json

    return payload_digest(json.loads(json.dumps(step, default=str)))


def _step_of(item: CaseItem) -> dict[str, Any] | None:
    step = item.payload.get("step")
    return step if isinstance(step, dict) and step.get("kind") == "sql_query" else None


def _output_name(step: Mapping[str, Any]) -> object:
    params = step.get("params")
    return params.get("output_name") if isinstance(params, dict) else None


def query_item(items: Sequence[CaseItem], output_name: str, digest: str | None = None) -> CaseItem | None:
    """The newest query-step item of the case that makes the table `output_name` (and, with `digest`, is exactly
    the step with that digest)."""
    found: CaseItem | None = None
    for item in items:
        step = _step_of(item)
        if (
            step is not None
            and _output_name(step) == output_name
            and (digest is None or step_digest(step) == digest)
        ):
            found = item
    return found


def outcome_query(items: Sequence[CaseItem], outcome: CaseItem) -> CaseItem | None:
    """The query-step item an outcome item (Q24's rule over orders) ticked, or None when it is a column's outcome."""
    name, digest = outcome.payload.get("outcome_sql"), outcome.payload.get("step_digest")
    if not isinstance(name, str) or not isinstance(digest, str):
        return None
    return query_item(items, name, digest)


def other_changes(items: Sequence[CaseItem], output_name: str) -> bool:
    """Whether a ticked join, stack or query other than the query `output_name` is in `items`."""
    for item in items:
        if item.state is not ProposalState.ACCEPTED:
            continue
        step = _step_of(item)
        if step is not None and _output_name(step) != output_name:
            return True
        if isinstance(item.payload.get("build_record"), dict):
            return True
    return False
