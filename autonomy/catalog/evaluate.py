"""Offline evaluation of the co-purchase recommender, on the real online-retail log and on planted data.

The question is the honest one: if we had recommended k products to each customer on the day of
the split, how many of them went on to buy one of those products in the next 90 days, without being
nudged? And is that better than simply showing everyone the best sellers?

- **Time-based split.** The model learns only from purchases at or before `cutoff`; the test is
  what each customer bought in `(cutoff, cutoff + horizon]`. The default split is the online-retail
  sample's own snapshot, 2011-09-10, with its 90-day outcome window, so the numbers line up with
  `library/online-retail/sample.csv`.
- **What counts as a hit.** Only *new* items: products the customer had not bought before the
  cutoff. Re-buying what you already own is the refill model's job, and every method leaves owned
  items out, so none of them can score by repeating history.
- **Metrics.** hit-rate@k is the share of customers who bought at least one of their k
  recommendations; recall@k is, per customer, the share of their new items that were among the k,
  averaged over customers.
- **Baseline.** Popularity: the k items bought by the most distinct customers in the 90 days before
  the cutoff, leaving out what the customer owns. It is the fallback the recommender itself uses, so
  beating it means co-purchase adds something real.

Why the raw log and not `sample.csv`: the committed sample is one row per lapsed customer, a set of
aggregates with no product lines, so no recommender can be scored on it. The product lines are in
the raw log that `library/online-retail/fetch.py` downloads (git-ignored). We use the sample for its
audience: the 1,463 lapsed customers a win-back campaign would target are scored as their own row.

Run it with `python -m autonomy.catalog.evaluate` (add `--log PATH` if the log lives elsewhere); it
rewrites the "Recommendations" section of docs/v2/CATALOG.md and leaves every other section alone.
"""

from __future__ import annotations

import argparse
import time
from collections.abc import Collection, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

import numpy as np
import pandas as pd

from autonomy.catalog.products import clean_retail_lines
from autonomy.catalog.recommend import BASKET_COLUMNS, CoPurchaseConfig, CoPurchaseModel
from autonomy.data.loaders import (
    ONLINE_RETAIL_LOG,
    ONLINE_RETAIL_SAMPLE,
    read_online_retail_log,
    read_online_retail_sample,
)
from autonomy.data.mapping import customer_key

__all__ = [
    "DEFAULT_CUTOFF",
    "DEFAULT_HORIZON",
    "DOC_PATH",
    "SECTION",
    "MethodScore",
    "evaluate",
    "main",
    "render_section",
    "retail_purchases",
    "synthetic_planted_pairs",
    "write_section",
]

DEFAULT_CUTOFF = datetime(2011, 9, 10, tzinfo=UTC)
"""The online-retail sample's snapshot date: train on what came before, test on the 90 days after."""
DEFAULT_HORIZON = timedelta(days=90)
DOC_PATH = Path(__file__).resolve().parents[2] / "docs" / "v2" / "CATALOG.md"
SECTION = "Recommendations"
POPULARITY = "popularity (baseline)"


@dataclass(frozen=True)
class MethodScore:
    """One method at one k on one population."""

    method: str
    k: int
    hit_rate: float
    recall: float
    customers: int


def retail_purchases(log: pd.DataFrame) -> pd.DataFrame:
    """The raw online-retail log as the recommender's basket table (`BASKET_COLUMNS`).

    The cleaning is `autonomy.catalog.products.clean_retail_lines`, the same one the cycle
    validation uses. The basket is the invoice and times are Unix seconds (UTC); one item twice on
    one invoice is one row. The log has no categories, so category diversity is idle here.
    """
    lines = clean_retail_lines(log)
    seconds = lines["ts"].dt.tz_localize(None).to_numpy(dtype="datetime64[s]").astype(np.int64)
    frame = pd.DataFrame(
        {
            "customer_id": lines["customer_id"].to_numpy(),
            "item_id": lines["item_id"].to_numpy(),
            "t": seconds,
            "category": None,
            "basket_id": lines["invoice"].to_numpy(),
        }
    )
    return frame.drop_duplicates(["customer_id", "item_id", "basket_id"]).reset_index(drop=True)


def synthetic_planted_pairs(
    seed: int = 7,
    *,
    customers: int = 600,
    pairs: int = 12,
    noise_items: int = 40,
    cutoff: datetime = DEFAULT_CUTOFF,
) -> pd.DataFrame:
    """Purchases with a known answer: item `A<i>` is bought with `B<i>`, on top of popular noise.

    Every customer has one planted pair. Before the cutoff, two in three buy both halves within ten
    days of each other (the pattern to learn); the rest buy only `A<i>` and then buy `B<i>` in the
    test window (the hit to find). Everyone also buys popular noise items (Zipf-like), before and
    after the cutoff, which is exactly what a popularity baseline will recommend.
    """
    rng = np.random.default_rng(seed)
    start = int(cutoff.timestamp()) - 180 * 86400
    day = 86400
    weights = 1.0 / np.arange(1, noise_items + 1)
    weights /= weights.sum()
    rows: list[tuple[str, str, int, str, str]] = []
    for n in range(customers):
        cid = f"c{n:05d}"
        p = int(rng.integers(pairs))
        a, b = f"A{p:02d}", f"B{p:02d}"
        t_a = start + int(rng.integers(0, 150)) * day
        rows.append((cid, a, t_a, "planted_a", f"{cid}-a"))
        if rng.random() < 2 / 3:
            rows.append((cid, b, t_a + int(rng.integers(0, 10)) * day, "planted_b", f"{cid}-b"))
        else:
            t_b = int(cutoff.timestamp()) + int(rng.integers(1, 60)) * day
            rows.append((cid, b, t_b, "planted_b", f"{cid}-b"))
        for when, count in (("train", 3), ("test", 1)):
            for j, item in enumerate(rng.choice(noise_items, size=count, replace=False, p=weights)):
                base = start if when == "train" else int(cutoff.timestamp())
                t = base + int(rng.integers(1, 150 if when == "train" else 60)) * day
                rows.append((cid, f"N{int(item):02d}", t, "noise", f"{cid}-{when}{j}"))
    return pd.DataFrame(rows, columns=list(BASKET_COLUMNS))


def _columns(frame: pd.DataFrame) -> zip[tuple[str, str, int]]:
    customers = [str(v) for v in frame["customer_id"].to_numpy()]
    items = [str(v) for v in frame["item_id"].to_numpy()]
    times = [int(v) for v in frame["t"].to_numpy(dtype=np.int64)]
    return zip(customers, items, times, strict=True)


def _last_bought(train: pd.DataFrame) -> dict[str, dict[str, int]]:
    """{customer: {item: Unix seconds it was last bought}}."""
    out: dict[str, dict[str, int]] = {}
    for cid, item, t in _columns(train):
        mine = out.setdefault(cid, {})
        mine[item] = max(t, mine.get(item, t))
    return out


def evaluate(
    purchases: pd.DataFrame,
    *,
    cutoff: datetime = DEFAULT_CUTOFF,
    horizon: timedelta = DEFAULT_HORIZON,
    ks: Sequence[int] = (5, 10, 20),
    configs: Mapping[str, CoPurchaseConfig] | None = None,
    population: Collection[str] | None = None,
) -> list[MethodScore]:
    """Hit-rate@k and recall@k for each co-purchase config and the popularity baseline.

    The population is every customer who bought before the cutoff and bought at least one new item
    in the test window; `population` narrows it further (to the win-back audience, say).
    """
    methods = dict(configs or {"co-purchase (cosine)": CoPurchaseConfig()})
    t = purchases["t"].to_numpy(dtype=np.int64)
    lo, hi = int(cutoff.timestamp()), int((cutoff + horizon).timestamp())
    train = purchases[t <= lo]
    test = purchases[(t > lo) & (t <= hi)]
    owned = _last_bought(train)
    bought_after: dict[str, set[str]] = {}
    for cid, item, _ in _columns(test):
        bought_after.setdefault(cid, set()).add(item)
    targets: dict[str, set[str]] = {}
    for cid, items in bought_after.items():
        if cid not in owned or (population is not None and cid not in population):
            continue
        new = items - set(owned[cid])
        if new:
            targets[cid] = new
    order = sorted(targets)
    top_k = max(ks)

    lists: dict[str, list[list[str]]] = {}
    baseline_model: CoPurchaseModel | None = None
    for name, config in methods.items():
        model = CoPurchaseModel.fit(train, as_of=cutoff, config=config)
        baseline_model = baseline_model or model
        lists[name] = [
            [r.item_id for r in model.recommend_items(owned[c], k=top_k, now=cutoff)] for c in order
        ]
    if baseline_model is None:
        baseline_model = CoPurchaseModel.fit(train, as_of=cutoff)
    lists[POPULARITY] = [baseline_model.popular(top_k, exclude=owned[c]) for c in order]

    scores: list[MethodScore] = []
    for name, recs in lists.items():
        for k in ks:
            hits = [len(set(r[:k]) & targets[c]) for r, c in zip(recs, order, strict=True)]
            n = len(order)
            hit_rate = sum(h > 0 for h in hits) / n if n else 0.0
            recall = sum(h / len(targets[c]) for h, c in zip(hits, order, strict=True)) / n if n else 0.0
            scores.append(MethodScore(name, k, hit_rate, recall, n))
    return scores


def _table(scores: Sequence[MethodScore]) -> str:
    lines = ["| Method | k | hit-rate@k | recall@k | customers |", "|---|---:|---:|---:|---:|"]
    for s in scores:
        lines.append(f"| {s.method} | {s.k} | {s.hit_rate:.1%} | {s.recall:.1%} | {s.customers:,} |")
    return "\n".join(lines)


def render_section(
    results: Mapping[str, Sequence[MethodScore]],
    *,
    source: str,
    notes: Sequence[str] = (),
) -> str:
    """The "Recommendations" section of docs/v2/CATALOG.md, from evaluation results per population."""
    parts = [
        f"## {SECTION}",
        "",
        "Complementary products come from item-item co-purchase (`autonomy/catalog/recommend.py`): two "
        "products are related when the same customers buy them on one shopping trip (one basket, or "
        "baskets within 30 days of the trip's first one). Similarity is cosine over trip counts; each "
        "owned item votes for its neighbours, weighted by how recently it was bought, and the "
        'strongest voter is the reason ("Often bought with your Vitamin C serum"). Popular items in '
        "the customer's own categories fill any gap, always ranked below co-purchase picks, and a "
        "repeat category halves the next item's score so the list stays varied. `next_best_item` "
        "chooses a refill of the same item when its refill window is open (it reads the refill model's "
        "`CycleEstimate`: `key`, `level`, `window_open`, `days_until_due`), and a complementary item "
        "when it is not; its `item_id` is what goes in `Action.item_id`. No randomness, no clock, no LLM.",
        "",
        f"**Evaluation.** {source} Time-based split: the model learns from purchases on or before "
        f"{DEFAULT_CUTOFF.date().isoformat()} and is tested on the {DEFAULT_HORIZON.days} days after. "
        "A hit is a recommended product the customer had never bought before and then bought in the "
        "test window, unprompted. Everyone's owned items are excluded, the baseline included. The "
        "baseline recommends the products with the most distinct buyers in the 90 days before the "
        "split. Reproduce with `python library/online-retail/fetch.py` (downloads the log) and then "
        "`python -m autonomy.catalog.evaluate`.",
        "",
    ]
    for population, scores in results.items():
        parts += [f"**{population}**", "", _table(scores), ""]
    if notes:
        parts += ["**Notes**", ""] + [f"- {n}" for n in notes] + [""]
    return "\n".join(parts).rstrip() + "\n"


def write_section(path: Path, heading: str, body: str) -> None:
    """Replace the `## heading` section of `path` with `body` (which starts with that heading).

    Every other section is kept as it is. A missing file is created, a missing section is appended.
    """
    text = path.read_text(encoding="utf-8") if path.exists() else "# Catalogue: refills and recommendations\n"
    lines = text.splitlines(keepends=True)
    start = next((i for i, line in enumerate(lines) if line.rstrip() == f"## {heading}"), None)
    block = body if body.endswith("\n") else body + "\n"
    if start is None:
        new = text.rstrip("\n") + "\n\n" + block
    else:
        end = next((j for j in range(start + 1, len(lines)) if lines[j].startswith("## ")), len(lines))
        tail = "".join(lines[end:])
        new = "".join(lines[:start]) + block + ("\n" + tail if tail else "")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(new, encoding="utf-8")


def main(argv: Sequence[str] | None = None) -> int:
    """Evaluate on the raw online-retail log and write the results into docs/v2/CATALOG.md."""
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0] if __doc__ else None)
    parser.add_argument("--log", type=Path, default=ONLINE_RETAIL_LOG, help="the raw online-retail log")
    parser.add_argument("--sample", type=Path, default=ONLINE_RETAIL_SAMPLE, help="the win-back sample")
    parser.add_argument("--doc", type=Path, default=DOC_PATH)
    parser.add_argument("--no-write", action="store_true", help="print the section instead of writing it")
    args = parser.parse_args(argv)
    if not args.log.exists():
        parser.error(f"{args.log} not found: run `python library/online-retail/fetch.py` first")

    purchases = retail_purchases(read_online_retail_log(args.log))
    sample = read_online_retail_sample(args.sample)
    lapsed = {str(customer_key(v)) for v in sample["customer_id"]}
    configs = {
        "co-purchase (cosine)": CoPurchaseConfig(),
        "co-purchase (lift)": CoPurchaseConfig(metric="lift"),
        "co-purchase, same basket only": CoPurchaseConfig(window=timedelta(0)),
    }
    everyone = evaluate(purchases, configs=configs)
    winback = evaluate(purchases, configs={"co-purchase (cosine)": CoPurchaseConfig()}, population=lapsed)
    synthetic = evaluate(
        synthetic_planted_pairs(), ks=(1, 5), configs={"co-purchase (cosine)": CoPurchaseConfig()}
    )

    train = purchases[purchases["t"].to_numpy(dtype=np.int64) <= int(DEFAULT_CUTOFF.timestamp())]
    # Timing is for the report only; nothing it measures feeds a recommendation.
    started = time.perf_counter()
    model = CoPurchaseModel.fit(train, as_of=DEFAULT_CUTOFF)
    fitted = time.perf_counter()
    owned = _last_bought(train)
    for cid in sorted(owned):
        model.recommend_items(owned[cid], k=10, now=DEFAULT_CUTOFF)
    scored = time.perf_counter()
    source = (
        f"Online Retail log (UCI, 2010-12-01 to 2011-12-09): {len(purchases):,} product lines after "
        f"cleaning, {purchases['customer_id'].nunique():,} customers, {purchases['item_id'].nunique():,} "
        f"products; the model before the split has {model.n_trips:,} trips and "
        f"{len(model.neighbours.item):,} item-to-item links."
    )
    notes = [
        f"Speed, on one CPU: fitting on {len(train):,} lines took {fitted - started:.1f} s, and scoring "
        f"all {len(owned):,} customers (10 items each) took {scored - fitted:.1f} s, about "
        f"{len(owned) / max(scored - fitted, 1e-9):,.0f} customers a second.",
        "Defaults (30-day trip window, cosine, 90-day half-life, 50 neighbours) were set before looking "
        "at this test window; of the variants compared, a 365-day half-life scored about one point "
        "higher at k=5, too small a gain on one split to change the default. Lift favours rare items "
        "and loses to plain popularity, which is why cosine is the default.",
        "`library/online-retail/sample.csv` has no product lines (one aggregated row per lapsed "
        "customer), so the recommender is scored on the raw log it was built from; the sample supplies "
        "the win-back audience above.",
        "Non-product lines (POST, DOT, M, BANK CHARGES, ...) and cancellations are excluded; the log has "
        "no categories, so the diversity rule is idle on this data (it is covered by the unit tests).",
        "Synthetic check (planted pairs on top of popular noise, seed 7): "
        + "; ".join(f"{s.method} hit-rate@{s.k} {s.hit_rate:.0%}" for s in synthetic)
        + ".",
    ]
    section = render_section(
        {
            "All customers with a new product in the test window": everyone,
            "Win-back audience (sample.csv's lapsed customers)": winback,
        },
        source=source,
        notes=notes,
    )
    if args.no_write:
        print(section)
    else:
        write_section(args.doc, SECTION, section)
        print(f"wrote the {SECTION} section of {args.doc}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
