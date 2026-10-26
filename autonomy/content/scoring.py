"""The scoring model: which drafts will sell, for which type of customer (plan-v3 decision 6, step 4).

Trained on our own past sends. One row is a (draft, customer type) cell with its sends and its
purchases. A draft is described by its theme, tone, offer kind, channel, length and a few simple
text features; a type is described by a handful of traits (where its typical member sits on
recency, purchases, spend, order value and offer need, from `autonomy.content.types`). The model is
a regularised logistic regression on binomial counts, fitted with Newton steps in numpy:

    logit(rate) = b0 + draft . b1 + traits . b2 + (draft x traits) . b3 + type intercept

The draft x traits terms are what let it say "short, plain copy suits lapsed deal seekers, longer
copy suits frequent buyers"; a ridge penalty keeps the many small terms from fitting noise.

It is trusted only when it has earned it. A deterministic share of the past drafts (by a hash of the
draft id) is held out; the model is fitted on the rest and asked to rank the held-out drafts. The
score is the AUC *within a type*, because a draft picker only ever compares drafts written for the
same type: over all pairs of held-out drafts in one type, with a purchase on one side and a
non-purchase on the other, how often the draft that was bought gets the higher score (ties count
half). It is compared with the v2 way of using labels, theme labels alone: the purchase rate of the
(type, theme), shrunk towards the theme's rate overall and then towards the shop's rate. The model
is used only when its AUC beats chance by more than `auc_confidence_z` standard errors AND beats the
theme-label AUC by at least `min_auc_gain`. Otherwise `ScoringModel.predict` returns the theme-label
rate, and the report says why. Either way the report states the held-out numbers.

The standard errors come from a bootstrap over held-out *drafts* (`bootstrap_draws` resamples, with
replacement, of whole drafts with all their cells), not over customers: every customer who got a draft
read the same words, so the draft is the unit that carries the luck. The gate asks for two things from
the same resamples: the model's AUC more than `auc_confidence_z` standard errors above 0.5, and its
AUC lead over theme labels both at least `min_auc_gain` and more than `auc_confidence_z` standard
errors above 0. A resampled draft that appears twice counts as two drafts with the same score (a tie),
which pulls the AUC slightly towards 0.5: the bias is in the safe direction.
"""

from __future__ import annotations

import hashlib
import math
import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import NamedTuple

import numpy as np
from numpy.typing import NDArray

from autonomy.content.config import CopyConfig
from autonomy.content.types import CustomerTypes
from autonomy.contracts import (
    Channel,
    ContentItem,
    CustomerState,
    OfferKind,
    OfferOption,
    Theme,
    Tone,
)

Matrix = NDArray[np.float64]

THEMES: tuple[Theme, ...] = tuple(Theme)
TONES: tuple[Tone, ...] = tuple(Tone)
OFFER_KINDS: tuple[OfferKind, ...] = tuple(OfferKind)
CHANNELS: tuple[Channel, ...] = tuple(Channel)

TEXT_NAMES: tuple[str, ...] = (
    "log_chars",
    "words",
    "avg_word_len",
    "sentences",
    "subject_chars",
    "questions",
    "exclamations",
    "slots",
    "slot_first_name",
    "slot_top_category",
    "slot_recommended_product",
    "slot_store_name",
    "you_share",
    "greeting",
)
"""The simple text features, in order. Counted on the template text, slots unfilled."""

FEATURE_NAMES: tuple[str, ...] = (
    *(f"theme:{t.value}" for t in THEMES),
    *(f"tone:{t.value}" for t in TONES),
    *(f"offer:{k.value}" for k in OFFER_KINDS),
    *(f"channel:{c.value}" for c in CHANNELS),
    *TEXT_NAMES,
)
"""Every draft feature, in column order."""

_WORD = re.compile(r"[A-Za-z']+")
_SLOT = re.compile(r"\{\{\s*([A-Za-z_][A-Za-z0-9_]*)\s*\}\}")
_SENTENCE_END = re.compile(r"[.!?]+")
_GREETINGS = ("hi ", "hello", "hey")
_YOU = frozenset({"you", "your", "yours", "you're", "you'll", "you've"})


def draft_features(item: ContentItem, offers: Mapping[str, OfferOption] | None = None) -> NDArray[np.float64]:
    """The feature row of one draft, in the order of `FEATURE_NAMES`."""
    offer = (offers or {}).get(item.offer_id)
    kind = offer.kind if offer is not None else (OfferKind.NONE if item.offer_id == "none" else None)
    body = item.body
    plain = _SLOT.sub(" ", body)
    words = _WORD.findall(plain)
    slots = {m.lower() for m in _SLOT.findall(f"{item.subject or ''} {body}")}
    text = [
        math.log1p(len(body)),
        float(len(words)),
        float(np.mean([len(w) for w in words])) if words else 0.0,
        float(max(1, len(_SENTENCE_END.findall(plain)))),
        float(len(item.subject or "")),
        float(body.count("?")),
        float(body.count("!")),
        float(len(slots)),
        float("first_name" in slots),
        float("top_category" in slots),
        float("recommended_product" in slots),
        float("store_name" in slots),
        sum(w.lower() in _YOU for w in words) / len(words) if words else 0.0,
        float(body.lower().lstrip().startswith(_GREETINGS)),
    ]
    row = [
        *(float(item.theme is t) for t in THEMES),
        *(float(item.tone is t) for t in TONES),
        *(float(kind is k) for k in OFFER_KINDS),
        *(float(item.channel is c) for c in CHANNELS),
        *text,
    ]
    return np.array(row, dtype=np.float64)


@dataclass(frozen=True)
class DraftResult:
    """How one draft did with one customer type: customers sent to, and how many bought."""

    item: ContentItem
    type_id: str
    sends: int
    purchases: int

    def __post_init__(self) -> None:
        if self.sends < 0 or not 0 <= self.purchases <= self.sends:
            raise ValueError(f"purchases {self.purchases} must be between 0 and sends {self.sends}")


class PastSend(NamedTuple):
    """One of our own earlier sends: the draft, the customer's state AS OF the send, and the outcome.

    `state` must be the customer as they were when the message went out (`state.as_of <= sent_at`,
    no events after it). Today's state would already hold the purchase the send caused (one more
    order, recency near zero), so buyers would drift into "recent, frequent buyer" types and the
    outcome would leak into the type the row is counted under. `sent_at` is timezone-aware."""

    item: ContentItem
    state: CustomerState
    sent_at: datetime
    bought: bool


@dataclass(frozen=True)
class Tally:
    """`tally_sends`' result: the cells, and how many rows were left out for a state after the send."""

    cells: list[DraftResult]
    late_states: int


def tally_sends(sends: Iterable[PastSend], types: CustomerTypes) -> Tally:
    """Per-send outcomes -> `DraftResult` cells, with each row placed in this week's types.

    Types are refitted every week, so an old send is placed by its customer's state as of the send
    (see `PastSend`), never by whatever type id it had then. A row whose state is dated after the
    send is skipped and counted in `late_states`: it would leak the outcome into the type."""
    rows: list[PastSend] = []
    late = 0
    for row in sends:
        send = PastSend(*row)
        if send.state.as_of > send.sent_at:
            late += 1
            continue
        rows.append(send)
    placed = types.assign_many([r.state for r in rows])
    cells: dict[tuple[str, str], list[int]] = {}
    items: dict[str, ContentItem] = {}
    for row, type_id in zip(rows, placed, strict=True):
        items.setdefault(row.item.content_id, row.item)
        cell = cells.setdefault((row.item.content_id, type_id), [0, 0])
        cell[0] += 1
        cell[1] += int(row.bought)
    out = [
        DraftResult(item=items[cid], type_id=tid, sends=n, purchases=k)
        for (cid, tid), (n, k) in sorted(cells.items())
    ]
    return Tally(cells=out, late_states=late)


# --- ranking quality ----------------------------------------------------------------------------


def within_type_auc(
    type_ids: Sequence[str], scores: Sequence[float], sends: Sequence[int], purchases: Sequence[int]
) -> tuple[float, float, float, float]:
    """(AUC, pairs weight, purchases, non-purchases) over pairs of different drafts in the same type.

    Each cell is a draft in a type with `purchases` buyers and `sends - purchases` non-buyers, all at
    the draft's one score. A pair is a buyer of one draft and a non-buyer of another draft, in the same
    type; it counts 1 when the buyer's draft scores higher and 0.5 when they tie. Returns an AUC of 0.5
    and zero weight when there are no such pairs."""
    groups: dict[str, list[int]] = {}
    for i, t in enumerate(type_ids):
        groups.setdefault(t, []).append(i)
    s = np.asarray(scores, dtype=np.float64)
    k = np.asarray(purchases, dtype=np.float64)
    neg = np.asarray(sends, dtype=np.float64) - k
    won = total = pos_used = neg_used = 0.0
    for idx in groups.values():
        if len(idx) < 2:
            continue
        a = np.asarray(idx)
        sa, ka, na = s[a], k[a], neg[a]
        pair = ka[:, None] * na[None, :]
        np.fill_diagonal(pair, 0.0)
        greater = (sa[:, None] > sa[None, :]).astype(np.float64)
        tied = (sa[:, None] == sa[None, :]).astype(np.float64)
        won += float((pair * (greater + 0.5 * tied)).sum())
        w = float(pair.sum())
        total += w
        if w > 0.0:
            pos_used += float(ka.sum())
            neg_used += float(na.sum())
    return (won / total if total > 0.0 else 0.5), total, pos_used, neg_used


def bootstrap_auc(
    type_ids: Sequence[str],
    draft_ids: Sequence[str],
    scores_a: Sequence[float],
    scores_b: Sequence[float],
    sends: Sequence[int],
    purchases: Sequence[int],
    *,
    draws: int,
    seed: int,
) -> tuple[float, float]:
    """(standard error of AUC a, standard error of AUC a minus AUC b), resampling whole drafts."""
    by_draft: dict[str, list[int]] = {}
    for i, d in enumerate(draft_ids):
        by_draft.setdefault(d, []).append(i)
    groups = [by_draft[d] for d in sorted(by_draft)]
    rng = np.random.default_rng(seed)
    t = np.asarray(type_ids)
    sa, sb = np.asarray(scores_a, dtype=np.float64), np.asarray(scores_b, dtype=np.float64)
    n, k = np.asarray(sends), np.asarray(purchases)
    aucs: list[float] = []
    diffs: list[float] = []
    for _ in range(draws):
        pick = rng.integers(0, len(groups), size=len(groups))
        idx = np.fromiter((i for g in pick for i in groups[int(g)]), dtype=np.int64)
        a_auc = within_type_auc(t[idx].tolist(), sa[idx].tolist(), n[idx].tolist(), k[idx].tolist())[0]
        b_auc = within_type_auc(t[idx].tolist(), sb[idx].tolist(), n[idx].tolist(), k[idx].tolist())[0]
        aucs.append(a_auc)
        diffs.append(a_auc - b_auc)
    return float(np.std(aucs)), float(np.std(diffs))


# --- the theme-label baseline ---------------------------------------------------------------------


class ThemeBaseline:
    """What v2 knows: the purchase rate of a theme, per type, shrunk towards simpler rates.

    rate(type, theme) = (purchases + m * rate(theme)) / (sends + m), and
    rate(theme) = (purchases + m * rate(shop)) / (sends + m), with m = `strength` sends."""

    def __init__(self, cells: Sequence[DraftResult], strength: float = 100.0) -> None:
        self._m = strength
        total_n = sum(c.sends for c in cells)
        self._shop = (sum(c.purchases for c in cells) + 1.0) / (total_n + 20.0)  # 5% until data says more
        by_theme: dict[Theme, list[int]] = {}
        by_pair: dict[tuple[str, Theme], list[int]] = {}
        for c in cells:
            for table, key in ((by_theme, c.item.theme), (by_pair, (c.type_id, c.item.theme))):
                row = table.setdefault(key, [0, 0])  # type: ignore[arg-type]
                row[0] += c.sends
                row[1] += c.purchases
        self._theme = {t: (k + strength * self._shop) / (n + strength) for t, (n, k) in by_theme.items()}
        self._pair = {key: (n, k) for key, (n, k) in by_pair.items()}

    def theme_rate(self, theme: Theme) -> float:
        return self._theme.get(theme, self._shop)

    def rate(self, type_id: str, theme: Theme) -> float:
        n, k = self._pair.get((type_id, theme), (0, 0))
        return (k + self._m * self.theme_rate(theme)) / (n + self._m)


# --- the logistic model ----------------------------------------------------------------------------


def _sigmoid(x: NDArray[np.float64]) -> NDArray[np.float64]:
    return 1.0 / (1.0 + np.exp(-np.clip(x, -30.0, 30.0)))


class _Logistic:
    """A fitted model: standardisation, type index, traits, and the weight vector."""

    def __init__(
        self,
        cells: Sequence[DraftResult],
        traits: Mapping[str, Sequence[float]],
        offers: Mapping[str, OfferOption],
        ridge: float,
    ) -> None:
        self._offers = offers
        self._traits = {t: np.asarray(v, dtype=np.float64) for t, v in traits.items()}
        self._q = len(next(iter(self._traits.values()))) if self._traits else 0
        unique: dict[str, ContentItem] = {}
        for c in cells:
            unique.setdefault(c.item.content_id, c.item)
        feats = np.array([draft_features(i, offers) for i in unique.values()])
        self._mean = feats.mean(axis=0)
        std = feats.std(axis=0)
        self._std = np.where(std > 1e-9, std, 1.0)
        self._types = sorted({c.type_id for c in cells})
        self._type_col = {t: j for j, t in enumerate(self._types)}
        x = self._design([c.item for c in cells], [c.type_id for c in cells])
        n = np.array([c.sends for c in cells], dtype=np.float64)
        k = np.array([c.purchases for c in cells], dtype=np.float64)
        self.beta = self._newton(x, k, n, ridge)

    def _trait_row(self, type_id: str) -> NDArray[np.float64]:
        return self._traits.get(type_id, np.zeros(self._q))

    def _design(self, items: Sequence[ContentItem], type_ids: Sequence[str]) -> Matrix:
        d = (np.array([draft_features(i, self._offers) for i in items]) - self._mean) / self._std
        t = np.array([self._trait_row(tid) for tid in type_ids]).reshape(len(items), self._q)
        onehot = np.zeros((len(items), len(self._types)))
        for r, tid in enumerate(type_ids):
            col = self._type_col.get(tid)
            if col is not None:
                onehot[r, col] = 1.0
        cross = (d[:, :, None] * t[:, None, :]).reshape(len(items), -1)
        return np.hstack([np.ones((len(items), 1)), d, t, cross, onehot])

    @staticmethod
    def _newton(x: Matrix, k: Matrix, n: Matrix, ridge: float) -> Matrix:
        dim = x.shape[1]
        pen = np.full(dim, ridge)
        pen[0] = 0.0
        shop = float((k.sum() + 0.5) / (n.sum() + 1.0))
        beta = np.zeros(dim)
        beta[0] = math.log(shop / (1.0 - shop))

        def objective(b: Matrix) -> float:
            p = np.clip(_sigmoid(x @ b), 1e-12, 1 - 1e-12)
            return float((k * np.log(p) + (n - k) * np.log1p(-p)).sum() - 0.5 * (pen * b * b).sum())

        best = objective(beta)
        for _ in range(40):
            p = _sigmoid(x @ beta)
            grad = x.T @ (k - n * p) - pen * beta
            hess = (x * (n * p * (1.0 - p))[:, None]).T @ x + np.diag(pen) + 1e-9 * np.eye(dim)
            step = np.linalg.solve(hess, grad)
            scale = 1.0
            for _ in range(12):  # halve the step until the penalised likelihood does not fall
                trial = beta + scale * step
                value = objective(trial)
                if value >= best - 1e-12:
                    break
                scale /= 2.0
            else:
                break
            beta, best = trial, value
            if float(np.abs(scale * step).max()) < 1e-7:
                break
        return beta

    def rates(self, items: Sequence[ContentItem], type_ids: Sequence[str]) -> NDArray[np.float64]:
        return _sigmoid(self._design(items, type_ids) @ self.beta)


# --- the public model ------------------------------------------------------------------------------


@dataclass(frozen=True)
class ScoringReport:
    """What the held-out test said, and whether the model may be used."""

    used: bool
    reason: str
    auc: float | None
    theme_auc: float | None
    auc_se: float | None
    gain_se: float | None
    train_cells: int
    test_cells: int
    train_purchases: int
    test_purchases: int

    def sentence(self) -> str:
        if self.auc is None or self.theme_auc is None:
            return f"Scoring model not used: {self.reason}"
        verdict = "used" if self.used else "not used"
        return (
            f"Scoring model {verdict}: held-out AUC {self.auc:.3f} vs {self.theme_auc:.3f} for theme labels "
            f"alone ({self.test_cells} held-out cells, {self.test_purchases} purchases). {self.reason}"
        )


def _in_holdout(content_id: str, seed: int, share: float) -> bool:
    digest = hashlib.sha256(f"{seed}|{content_id}".encode()).hexdigest()[:12]
    return int(digest, 16) / 16**12 < share


def merge_cells(results: Iterable[DraftResult]) -> list[DraftResult]:
    """One cell per (draft, type): repeated results for the same pair are added together."""
    merged: dict[tuple[str, str], DraftResult] = {}
    for r in results:
        key = (r.item.content_id, r.type_id)
        old = merged.get(key)
        merged[key] = (
            r
            if old is None
            else DraftResult(old.item, old.type_id, old.sends + r.sends, old.purchases + r.purchases)
        )
    return [merged[key] for key in sorted(merged)]


class ScoringModel:
    """Predicts a draft's purchase rate for a type. Falls back to theme labels until it earns its place."""

    def __init__(
        self, config: CopyConfig | None = None, *, offers: Mapping[str, OfferOption] | None = None
    ) -> None:
        self._cfg = config or CopyConfig()
        self._offers: Mapping[str, OfferOption] = offers or {}
        self._baseline: ThemeBaseline | None = None
        self._model: _Logistic | None = None
        self.report: ScoringReport | None = None

    @property
    def usable(self) -> bool:
        """True when the model passed the held-out test and is what `predict` uses."""
        return self._model is not None

    def fit(
        self, results: Iterable[DraftResult], type_traits: Mapping[str, Sequence[float]]
    ) -> ScoringReport:
        """Fit on past sends, test on held-out drafts, and decide whether to use the model."""
        cfg = self._cfg
        cells = [c for c in merge_cells(results) if c.sends > 0]
        self._baseline = ThemeBaseline(cells)
        self._model = None
        test = [c for c in cells if _in_holdout(c.item.content_id, cfg.seed, cfg.holdout_share)]
        test_ids = {c.item.content_id for c in test}
        train = [c for c in cells if c.item.content_id not in test_ids]
        k_train, k_test = sum(c.purchases for c in train), sum(c.purchases for c in test)

        def report(
            used: bool,
            reason: str,
            auc: float | None = None,
            base: float | None = None,
            se: float | None = None,
            gain_se: float | None = None,
        ) -> ScoringReport:
            self.report = ScoringReport(
                used, reason, auc, base, se, gain_se, len(train), len(test), k_train, k_test
            )
            return self.report

        if k_train < cfg.min_train_purchases or k_test < cfg.min_test_purchases:
            return report(
                False,
                f"too little history: {k_train} purchases to train on (need {cfg.min_train_purchases}) and "
                f"{k_test} held out (need {cfg.min_test_purchases}); using theme labels",
            )
        probe = _Logistic(train, type_traits, self._offers, cfg.ridge)
        tid = [c.type_id for c in test]
        sends = [c.sends for c in test]
        buys = [c.purchases for c in test]
        model_scores = probe.rates([c.item for c in test], tid)
        base = ThemeBaseline(train)
        base_scores = [base.rate(c.type_id, c.item.theme) for c in test]
        auc, weight = within_type_auc(tid, model_scores.tolist(), sends, buys)[:2]
        base_auc = within_type_auc(tid, base_scores, sends, buys)[0]
        if weight <= 0.0:
            return report(
                False,
                "no held-out type has two drafts with a purchase and a non-purchase; using theme labels",
            )
        se, gain_se = bootstrap_auc(
            tid,
            [c.item.content_id for c in test],
            model_scores.tolist(),
            base_scores,
            sends,
            buys,
            draws=cfg.bootstrap_draws,
            seed=cfg.seed,
        )
        z = cfg.auc_confidence_z
        if auc <= 0.5 + z * se:
            return report(
                False,
                f"AUC {auc:.3f} is not clearly above chance (needs more than {0.5 + z * se:.3f}); "
                "using theme labels",
                auc,
                base_auc,
                se,
                gain_se,
            )
        gain = auc - base_auc
        if gain < cfg.min_auc_gain or gain <= z * gain_se:
            return report(
                False,
                f"AUC lead over theme labels ({gain:+.3f}) is too small or too uncertain; using theme labels",
                auc,
                base_auc,
                se,
                gain_se,
            )
        self._model = _Logistic(cells, type_traits, self._offers, cfg.ridge)
        return report(
            True, "It beats chance and theme labels on drafts it never saw.", auc, base_auc, se, gain_se
        )

    def predict(self, item: ContentItem, type_id: str) -> float:
        """The predicted purchase rate of `item` for `type_id`; the theme-label rate while not usable."""
        if self._model is not None:
            return float(self._model.rates([item], [type_id])[0])
        if self._baseline is None:
            return 0.0
        return self._baseline.rate(type_id, item.theme)

    def rank(self, items: Sequence[ContentItem], type_id: str) -> list[tuple[ContentItem, float]]:
        """`items` best first with their predicted rates; ties break by content id so it repeats."""
        if self._model is not None and items:
            rates = self._model.rates(list(items), [type_id] * len(items)).tolist()
        else:
            rates = [self.predict(i, type_id) for i in items]
        return sorted(zip(items, rates, strict=True), key=lambda p: (-p[1], p[0].content_id))
