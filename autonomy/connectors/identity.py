"""One person, one customer id: link Klaviyo profiles to the Shopify customers they belong to.

Shopify keys a customer by its numeric customer id, and every Shopify-keyed decision, purchase and
proof number uses that id. Klaviyo's events name a profile by the connection's `identity` setting
(`auto`: the profile's `external_id`, else its lower-cased email, else its phone). Klaviyo's Shopify
integration is reported **not** to put the Shopify id in `external_id`, so without a link the same
person would be two customers: one with the orders, one with the consent and the engagement. A
Klaviyo delivery would then never meet the decision it delivered.

**How a link is made.** The linker is the engine's `record_hook`
(`ConnectorEngine(record_hook=IdentityLinker(store))`). It reads, from every new record version:

* Shopify customers: the customer id, plus the email address and phone number when the connection
  reads them (`contact=read`, which needs Shopify's protected customer data level 2; see
  `autonomy.connectors.shopify.api.CONTACT_CONFIG`).
* Klaviyo profiles (the consent stream) and the profiles attached to engagement and browsing
  events: the key Klaviyo's events use, the profile's email, phone and `external_id`.

Emails are normalised (trimmed, lower-cased) and phones kept only in international form (`+` and 8
to 15 digits); both are stored as SHA-256 hashes, never in the clear. A Klaviyo key is linked to a
Shopify customer id when **exactly one** Shopify customer matches it, by `external_id`, email or
phone. Two candidates (an email two Shopify customers share, or an email pointing at one customer
and a phone at another) are a **conflict**: the profile stays separate and the conflict is counted.
A Shopify customer id is never linked to another one, so two Shopify customers are never merged.
The links are recomputed from the stored facts whenever either side changes, so a changed address
moves or drops a link instead of keeping a stale one.

**How a link is used.** Events are never rewritten (their ids are stable). `AliasedEvents` and
`AliasedStates` wrap the runtime's stores and translate at read time: an event stored under a
linked Klaviyo key reads as the Shopify customer's, a state stored under one is hidden, and
`for_customer(<shopify id>)` returns the events of the id and of every key linked to it. A new or
changed link asks the runtime to rebuild states (`on_change`). `address_book` gives the Klaviyo
hand-off the email (or phone) of the linked profile, so the message reaches the profile that has
the consent and the engagement comes back to the right decision.
"""

from __future__ import annotations

import hashlib
import re
import threading
from collections.abc import Callable, Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Final, Literal

from sqlalchemy import Column, Index, Integer, MetaData, String, Table, and_, delete, func, or_, select
from sqlalchemy.engine import Engine

from autonomy.connectors.base import Connection, Stream
from autonomy.contracts import Channel, CustomerState, Event
from autonomy.interfaces import EventStore, StateStore
from autonomy.store.schema import UtcDateTime
from autonomy.store.sql import upsert

__all__ = [
    "KLAVIYO",
    "SHOPIFY",
    "AliasedEvents",
    "AliasedStates",
    "IdentityFact",
    "IdentityLinker",
    "IdentityMap",
    "IdentityStore",
    "IdentitySummary",
    "address_book",
    "contact_hash",
    "create_identity_tables",
    "identity_eraser",
    "identity_metadata",
    "normalize_email",
    "normalize_phone",
]

SHOPIFY: Final[str] = "shopify"
KLAVIYO: Final[str] = "klaviyo"
LINKED: Final[str] = "linked"
CONFLICT: Final[str] = "conflict"

LinkMethod = Literal["external_id", "email", "phone", "email+phone"]
ChangeHook = Callable[[str, datetime], None]
"""(business_id, now): links changed for this business; the runtime should rebuild states."""

_EMAIL: Final[re.Pattern[str]] = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
_PHONE_JUNK: Final[re.Pattern[str]] = re.compile(r"[\s().\-]")


# --- normalising ---------------------------------------------------------------------------------


def normalize_email(value: object) -> str | None:
    """`  Ann@Example.COM ` -> `ann@example.com`; None for anything that is not an address."""
    if not isinstance(value, str):
        return None
    text = value.strip().lower()
    return text if _EMAIL.match(text) else None


def normalize_phone(value: object) -> str | None:
    """An international number (`+44 20 7946 0958` -> `+442079460958`); None otherwise.

    A number without its country code is never guessed at: the same digits are different people
    in different countries."""
    if not isinstance(value, str):
        return None
    text = _PHONE_JUNK.sub("", value.strip())
    if not text.startswith("+"):
        return None
    digits = text[1:]
    if not digits.isdigit() or not 8 <= len(digits) <= 15:
        return None
    return "+" + digits


def contact_hash(kind: Literal["email", "phone"], value: str) -> str:
    """The stored form of a normalised address: SHA-256, prefixed by its kind."""
    return hashlib.sha256(f"{kind}:{value}".encode()).hexdigest()


# --- what is stored ------------------------------------------------------------------------------


@dataclass(frozen=True)
class IdentityFact:
    """What one source says about one of its customers (addresses hashed)."""

    business_id: str
    source: str
    key: str
    """The customer id this source's events use (Shopify: the customer id; Klaviyo: its event key)."""
    email_hash: str | None = None
    phone_hash: str | None = None
    external_id: str | None = None


@dataclass(frozen=True)
class IdentitySummary:
    """For the Data Doctor: how well the two sources meet."""

    shopify_customers: int
    klaviyo_profiles: int
    linked: int
    conflicts: int
    same_id: int
    """Klaviyo profiles whose event key already is a Shopify customer id (no link needed)."""

    @property
    def unlinked(self) -> int:
        return max(0, self.klaviyo_profiles - self.linked - self.conflicts - self.same_id)


identity_metadata = MetaData()

fact_table = Table(
    "identity_fact",
    identity_metadata,
    Column("business_id", String(200), primary_key=True),
    Column("source", String(40), primary_key=True),
    Column("source_key", String(400), primary_key=True),
    Column("email_hash", String(64), nullable=True),
    Column("phone_hash", String(64), nullable=True),
    Column("external_id", String(400), nullable=True),
    Column("updated_at", UtcDateTime(), nullable=False),
    Index("ix_identity_fact_email", "business_id", "email_hash"),
    Index("ix_identity_fact_phone", "business_id", "phone_hash"),
    Index("ix_identity_fact_external", "business_id", "external_id"),
)

link_table = Table(
    "identity_link",
    identity_metadata,
    Column("business_id", String(200), primary_key=True),
    Column("alias", String(400), primary_key=True),
    Column("customer_id", String(400), nullable=True),
    Column("status", String(20), nullable=False),
    Column("method", String(20), nullable=True),
    Column("candidates", Integer(), nullable=False),
    Column("updated_at", UtcDateTime(), nullable=False),
    Index("ix_identity_link_customer", "business_id", "customer_id"),
)


def create_identity_tables(engine: Engine) -> None:
    """Create the identity tables if missing (safe to call twice)."""
    identity_metadata.create_all(engine)


class IdentityStore:
    """Facts per source and the links made from them, in SQL (Postgres or SQLite)."""

    def __init__(self, engine: Engine) -> None:
        self.engine = engine

    # -- facts ------------------------------------------------------------------------------------

    def put_facts(self, facts: Sequence[IdentityFact], now: datetime) -> None:
        """Store facts. A field a record does not carry keeps the value stored before.

        Records differ in what they carry: a Shopify customer read without `contact=read` has no
        email, though its REST webhook may have had one, and a profile attached to a Klaviyo event
        may lack the phone its consent record has. Overwriting with "unknown" would drop and remake
        links (and rebuild state) on every such record; a new address still replaces the old one."""
        latest: dict[tuple[str, str, str], IdentityFact] = {}
        for f in facts:
            held = latest.get((f.business_id, f.source, f.key))
            latest[(f.business_id, f.source, f.key)] = f if held is None else _merged(held, f)
        by_source: dict[tuple[str, str], list[str]] = {}
        for business_id, source, key in latest:
            by_source.setdefault((business_id, source), []).append(key)
        for (business_id, source), keys in by_source.items():
            for key, stored in self.facts(business_id, source, keys).items():
                latest[(business_id, source, key)] = _merged(stored, latest[(business_id, source, key)])
        rows = [
            {
                "business_id": f.business_id,
                "source": f.source,
                "source_key": f.key,
                "email_hash": f.email_hash,
                "phone_hash": f.phone_hash,
                "external_id": f.external_id,
                "updated_at": now,
            }
            for f in latest.values()
        ]
        with self.engine.begin() as conn:
            upsert(conn, fact_table, rows, keep=())

    def facts(self, business_id: str, source: str, keys: Iterable[str]) -> dict[str, IdentityFact]:
        wanted = sorted(set(keys))
        out: dict[str, IdentityFact] = {}
        t = fact_table
        with self.engine.connect() as conn:
            for chunk in _chunks(wanted, 500):
                rows = conn.execute(
                    select(t).where(
                        t.c.business_id == business_id, t.c.source == source, t.c.source_key.in_(chunk)
                    )
                )
                for r in rows.mappings():
                    out[str(r["source_key"])] = _fact(r)
        return out

    def matching(
        self,
        business_id: str,
        source: str,
        *,
        email_hashes: Iterable[str] = (),
        phone_hashes: Iterable[str] = (),
        external_ids: Iterable[str] = (),
    ) -> list[IdentityFact]:
        """Facts of `source` that share any of the given hashes or external ids."""
        emails, phones, externals = (
            sorted(set(email_hashes)),
            sorted(set(phone_hashes)),
            sorted(set(external_ids)),
        )
        t = fact_table
        clauses = []
        if emails:
            clauses.append(t.c.email_hash.in_(emails))
        if phones:
            clauses.append(t.c.phone_hash.in_(phones))
        if externals:
            clauses.append(t.c.external_id.in_(externals))
        if not clauses:
            return []
        with self.engine.connect() as conn:
            rows = conn.execute(
                select(t).where(t.c.business_id == business_id, t.c.source == source, or_(*clauses))
            )
            return [_fact(r) for r in rows.mappings()]

    def keys(self, business_id: str, source: str, keys: Iterable[str]) -> set[str]:
        """Which of `keys` are known customer keys of `source`."""
        return set(self.facts(business_id, source, keys))

    # -- links ------------------------------------------------------------------------------------

    def link_rows(self, business_id: str, aliases: Iterable[str]) -> dict[str, tuple[str | None, str]]:
        """alias -> (customer id or None, status), for the aliases that have a row."""
        wanted = sorted(set(aliases))
        out: dict[str, tuple[str | None, str]] = {}
        t = link_table
        with self.engine.connect() as conn:
            for chunk in _chunks(wanted, 500):
                rows = conn.execute(select(t).where(t.c.business_id == business_id, t.c.alias.in_(chunk)))
                for r in rows.mappings():
                    out[str(r["alias"])] = (_opt(r["customer_id"]), str(r["status"]))
        return out

    def aliases_of(self, business_id: str, customer_ids: Iterable[str]) -> set[str]:
        wanted = sorted(set(customer_ids))
        t = link_table
        found: set[str] = set()
        with self.engine.connect() as conn:
            for chunk in _chunks(wanted, 500):
                rows = conn.execute(
                    select(t.c.alias).where(
                        t.c.business_id == business_id, t.c.status == LINKED, t.c.customer_id.in_(chunk)
                    )
                )
                found.update(str(a) for (a,) in rows)
        return found

    def set_links(self, business_id: str, rows: Sequence[Mapping[str, Any]], now: datetime) -> None:
        """Write link rows (`alias`, `customer_id`, `status`, `method`, `candidates`); `status=None` deletes."""
        keep = [
            dict(r) | {"business_id": business_id, "updated_at": now} for r in rows if r["status"] is not None
        ]
        drop = [str(r["alias"]) for r in rows if r["status"] is None]
        t = link_table
        with self.engine.begin() as conn:
            if drop:
                for chunk in _chunks(sorted(set(drop)), 500):
                    conn.execute(delete(t).where(t.c.business_id == business_id, t.c.alias.in_(chunk)))
            upsert(conn, t, keep, keep=())

    def links(self, business_id: str) -> dict[str, str]:
        """Every live link of the business: alias -> Shopify customer id."""
        t = link_table
        with self.engine.connect() as conn:
            rows = conn.execute(
                select(t.c.alias, t.c.customer_id).where(t.c.business_id == business_id, t.c.status == LINKED)
            )
            return {str(a): str(c) for a, c in rows if c is not None}

    def klaviyo_keys_that_are_customers(self, business_id: str) -> set[str]:
        """Klaviyo event keys that already are Shopify customer ids (profiles keyed by `external_id`)."""
        k, s = fact_table.alias("k"), fact_table.alias("s")
        with self.engine.connect() as conn:
            rows = conn.execute(
                select(k.c.source_key)
                .join(
                    s,
                    and_(
                        s.c.business_id == k.c.business_id,
                        s.c.source == SHOPIFY,
                        s.c.source_key == k.c.source_key,
                    ),
                )
                .where(k.c.business_id == business_id, k.c.source == KLAVIYO)
            )
            return {str(key) for (key,) in rows}

    def summary(self, business_id: str) -> IdentitySummary:
        f, t = fact_table, link_table
        with self.engine.connect() as conn:

            def count(source: str) -> int:
                q = (
                    select(func.count())
                    .select_from(f)
                    .where(f.c.business_id == business_id, f.c.source == source)
                )
                return int(conn.execute(q).scalar_one())

            def links(status: str) -> int:
                q = (
                    select(func.count())
                    .select_from(t)
                    .where(t.c.business_id == business_id, t.c.status == status)
                )
                return int(conn.execute(q).scalar_one())

            shopify, klaviyo, linked, conflicts = (
                count(SHOPIFY),
                count(KLAVIYO),
                links(LINKED),
                links(CONFLICT),
            )
        same = len(self.klaviyo_keys_that_are_customers(business_id))
        return IdentitySummary(shopify, klaviyo, linked, conflicts, same)


# --- linking -------------------------------------------------------------------------------------


@dataclass
class LinkChanges:
    linked: int = 0
    unlinked: int = 0
    conflicts: int = 0

    @property
    def any(self) -> bool:
        return bool(self.linked or self.unlinked or self.conflicts)


class IdentityLinker:
    """The engine's `record_hook`: record identity facts, then (re)link what they touch."""

    def __init__(
        self,
        store: IdentityStore,
        *,
        on_change: ChangeHook | None = None,
        maps: Sequence[IdentityMap] = (),
    ) -> None:
        self.store = store
        self.on_change = on_change
        self.maps = list(maps)
        self.conflicts_seen = 0

    def __call__(
        self, connection: Connection, stream: Stream, records: Sequence[Mapping[str, Any]], now: datetime
    ) -> None:
        facts = facts_from(connection, stream, records)
        if facts:
            self.record(facts, now)

    def record(self, facts: Sequence[IdentityFact], now: datetime) -> LinkChanges:
        """Store facts and relink every Klaviyo key they can affect; return what changed."""
        changes = LinkChanges()
        by_business: dict[str, list[IdentityFact]] = {}
        for fact in facts:
            by_business.setdefault(fact.business_id, []).append(fact)
        for business_id, mine in sorted(by_business.items()):
            shopify = [f for f in mine if f.source == SHOPIFY]
            klaviyo = [f for f in mine if f.source == KLAVIYO]
            # Shopify keys whose address changed may leave links pointing at them: relink those too.
            touched_customers = {f.key for f in shopify}
            self.store.put_facts(mine, now)
            # Link from what is stored (a record without an address keeps the one known before).
            to_check: dict[str, IdentityFact] = self.store.facts(
                business_id, KLAVIYO, [f.key for f in klaviyo]
            )
            if shopify:
                shopify = list(self.store.facts(business_id, SHOPIFY, touched_customers).values())
                for f in self.store.matching(
                    business_id,
                    KLAVIYO,
                    email_hashes=[h for s in shopify if (h := s.email_hash)],
                    phone_hashes=[h for s in shopify if (h := s.phone_hash)],
                    external_ids=touched_customers,
                ):
                    to_check.setdefault(f.key, f)
                before = self.store.aliases_of(business_id, touched_customers)
                if before:
                    for key, f in self.store.facts(business_id, KLAVIYO, before).items():
                        to_check.setdefault(key, f)
                # A Klaviyo key equal to a Shopify id that just appeared stops being an alias.
                for key, f in self.store.facts(business_id, KLAVIYO, touched_customers).items():
                    to_check.setdefault(key, f)
            step = self._relink(business_id, list(to_check.values()), now)
            changes.linked += step.linked
            changes.unlinked += step.unlinked
            changes.conflicts += step.conflicts
            if step.any:
                for m in self.maps:
                    if m.business_id == business_id:
                        m.invalidate()
                if self.on_change is not None:
                    self.on_change(business_id, now)
        return changes

    def _relink(self, business_id: str, klaviyo: Sequence[IdentityFact], now: datetime) -> LinkChanges:
        changes = LinkChanges()
        if not klaviyo:
            return changes
        keys = [f.key for f in klaviyo]
        customers = self.store.keys(business_id, SHOPIFY, keys + [e for f in klaviyo if (e := f.external_id)])
        by_email: dict[str, set[str]] = {}
        by_phone: dict[str, set[str]] = {}
        emails: dict[str, str | None] = {}
        for f in self.store.matching(
            business_id,
            SHOPIFY,
            email_hashes=[h for f in klaviyo if (h := f.email_hash)],
            phone_hashes=[h for f in klaviyo if (h := f.phone_hash)],
        ):
            if f.email_hash:
                by_email.setdefault(f.email_hash, set()).add(f.key)
            if f.phone_hash:
                by_phone.setdefault(f.phone_hash, set()).add(f.key)
            emails[f.key] = f.email_hash
        current = self.store.link_rows(business_id, keys)
        rows: list[dict[str, Any]] = []
        for fact in klaviyo:
            wanted = _decide(fact, customers, by_email, by_phone, emails)
            had = current.get(fact.key)
            now_row = (wanted["customer_id"], wanted["status"]) if wanted["status"] is not None else None
            if had == now_row or (had is None and now_row is None):
                continue
            rows.append(wanted | {"alias": fact.key})
            if wanted["status"] == LINKED:
                changes.linked += 1
            elif wanted["status"] == CONFLICT:
                changes.conflicts += 1
                self.conflicts_seen += 1
            if had is not None and had[1] == LINKED and wanted["status"] != LINKED:
                changes.unlinked += 1
        if rows:
            self.store.set_links(business_id, rows, now)
        return changes


def _decide(
    fact: IdentityFact,
    customers: set[str],
    by_email: Mapping[str, set[str]],
    by_phone: Mapping[str, set[str]],
    emails: Mapping[str, str | None] | None = None,
) -> dict[str, Any]:
    """The link row for one Klaviyo key: linked to exactly one Shopify customer, a conflict, or none.

    A phone number alone does not link a profile to a customer whose known email is another one: a
    household often shares a number, and two different emails say two different people. That is
    counted as a conflict and kept separate. `emails` is each matching Shopify customer's email hash."""
    none: dict[str, Any] = {"customer_id": None, "status": None, "method": None, "candidates": 0}
    if fact.key in customers:
        return none  # already a Shopify customer id: it is the key itself, never an alias of another
    found: dict[str, set[str]] = {}
    ambiguous = False
    if fact.external_id and fact.external_id in customers:
        found.setdefault(fact.external_id, set()).add("external_id")
    for kind, table, value in (("email", by_email, fact.email_hash), ("phone", by_phone, fact.phone_hash)):
        if value is None:
            continue
        ids = table.get(value, set())
        if len(ids) > 1:
            ambiguous = True  # two Shopify customers share this address: never pick one
        for cid in ids:
            found.setdefault(cid, set()).add(kind)
    if ambiguous or len(found) > 1:
        return {"customer_id": None, "status": CONFLICT, "method": None, "candidates": len(found)}
    if not found:
        return none
    ((customer_id, how),) = found.items()
    theirs = (emails or {}).get(customer_id)
    if how == {"phone"} and fact.email_hash and theirs and theirs != fact.email_hash:
        return {"customer_id": None, "status": CONFLICT, "method": None, "candidates": 1}
    method: LinkMethod
    if "external_id" in how:
        method = "external_id"
    elif how == {"email", "phone"}:
        method = "email+phone"
    else:
        method = "email" if "email" in how else "phone"
    return {"customer_id": customer_id, "status": LINKED, "method": method, "candidates": 1}


def facts_from(
    connection: Connection, stream: Stream, records: Sequence[Mapping[str, Any]]
) -> list[IdentityFact]:
    """Identity facts in one page of records (Shopify customers; Klaviyo profiles and event profiles)."""
    business = connection.business_id
    out: list[IdentityFact] = []
    if connection.kind == SHOPIFY and stream is Stream.CUSTOMERS:
        for r in records:
            cid = _opt(r.get("id"))
            if cid is None:
                continue
            email, phone = normalize_email(r.get("email")), normalize_phone(r.get("phone"))
            out.append(
                IdentityFact(
                    business_id=business,
                    source=SHOPIFY,
                    key=cid,
                    email_hash=contact_hash("email", email) if email else None,
                    phone_hash=contact_hash("phone", phone) if phone else None,
                )
            )
    elif connection.kind == KLAVIYO and stream in (Stream.CONSENT, Stream.ENGAGEMENT, Stream.EVENTS):
        from autonomy.connectors.klaviyo.mapping import SETUP_PROFILE, customer_for

        identity = connection.config.get("identity", "auto")
        for r in records:
            profile = r if stream is Stream.CONSENT else _mapping(r.get("_profile"))
            profile_id = _opt(profile.get("id"))
            attrs = _mapping(profile.get("attributes")) if stream is Stream.CONSENT else profile
            if not attrs and stream is not Stream.CONSENT:
                continue  # an event without its profile says nothing about who it is
            key = customer_for(profile_id, attrs, identity)
            external = _opt(attrs.get("external_id"))
            if key is None or external == SETUP_PROFILE:
                continue
            email, phone = normalize_email(attrs.get("email")), normalize_phone(attrs.get("phone_number"))
            out.append(
                IdentityFact(
                    business_id=business,
                    source=KLAVIYO,
                    key=key,
                    email_hash=contact_hash("email", email) if email else None,
                    phone_hash=contact_hash("phone", phone) if phone else None,
                    external_id=external,
                )
            )
    return out


# --- reading through the links -------------------------------------------------------------------


class IdentityMap:
    """The business's links in memory: alias -> Shopify id, and back. Reloaded after `invalidate()`."""

    def __init__(self, store: IdentityStore, business_id: str) -> None:
        self.store = store
        self.business_id = business_id
        self._lock = threading.Lock()
        self._to: dict[str, str] | None = None
        self._from: dict[str, tuple[str, ...]] = {}
        self._reachable: frozenset[str] = frozenset()

    def invalidate(self) -> None:
        with self._lock:
            self._to = None

    def refresh(self, now: datetime | None = None) -> None:  # noqa: ARG002 - a tick hook's shape
        """Reload from the store (another process may have linked since)."""
        self.invalidate()
        self._load()

    def _load(self) -> dict[str, str]:
        with self._lock:
            if self._to is not None:
                return self._to
        to = self.store.links(self.business_id)
        back: dict[str, list[str]] = {}
        for alias, cid in to.items():
            back.setdefault(cid, []).append(alias)
        reachable = frozenset(self.store.klaviyo_keys_that_are_customers(self.business_id))
        with self._lock:
            self._to = to
            self._from = {cid: tuple(sorted(a)) for cid, a in back.items()}
            self._reachable = reachable
            return to

    def canonical(self, customer_id: str) -> str:
        return self._load().get(customer_id, customer_id)

    def aliases(self, customer_id: str) -> tuple[str, ...]:
        self._load()
        return self._from.get(customer_id, ())

    def is_alias(self, customer_id: str) -> bool:
        return customer_id in self._load()

    def known_to_klaviyo(self, customer_id: str) -> bool:
        """A Klaviyo profile is keyed by this Shopify id itself (its `external_id`)."""
        self._load()
        return customer_id in self._reachable


class AliasedEvents:
    """An `EventStore` that reads every linked Klaviyo key as its Shopify customer. Writes pass through."""

    def __init__(self, inner: EventStore, identity: IdentityMap) -> None:
        self.inner = inner
        self.identity = identity

    def __getattr__(self, name: str) -> Any:  # other store methods (counts, deletes) pass through
        return getattr(self.inner, name)

    def _remap(self, event: Event) -> Event:
        cid = self.identity.canonical(event.customer_id)
        return event if cid == event.customer_id else event.model_copy(update={"customer_id": cid})

    def append(self, events: Iterable[Event]) -> int:
        return self.inner.append(events)

    def for_customer(
        self, customer_id: str, *, since: datetime | None = None, until: datetime | None = None
    ) -> list[Event]:
        cid = self.identity.canonical(customer_id)
        ids = (cid, *self.identity.aliases(cid))
        found: list[Event] = []
        for one in ids:
            found.extend(self.inner.for_customer(one, since=since, until=until))
        if len(ids) > 1:
            found.sort(key=lambda e: (e.ts, e.event_id))
        return [self._remap(e) for e in found]

    def scan(self, *, since: datetime | None = None, until: datetime | None = None) -> Iterator[Event]:
        for event in self.inner.scan(since=since, until=until):
            yield self._remap(event)

    def for_decision(self, decision_id: str) -> list[Event]:
        return [self._remap(e) for e in self.inner.for_decision(decision_id)]


class AliasedStates:
    """A `StateStore` that hides states left under a key that is now linked to a Shopify customer.

    The state store has no delete, so a profile decided on before its link was found keeps an old
    state row; hiding it means no sweep ever reaches the same person twice."""

    def __init__(self, inner: StateStore, identity: IdentityMap) -> None:
        self.inner = inner
        self.identity = identity

    def __getattr__(self, name: str) -> Any:
        return getattr(self.inner, name)

    def get(self, customer_id: str) -> CustomerState | None:
        return self.inner.get(self.identity.canonical(customer_id))

    def put(self, states: Iterable[CustomerState]) -> None:
        self.inner.put(s for s in states if not self.identity.is_alias(s.customer_id))

    def scan(self) -> Iterator[CustomerState]:
        for state in self.inner.scan():
            if not self.identity.is_alias(state.customer_id):
                yield state

    def count(self) -> int:
        """Customers, not rows: a hidden state left under a linked key is not a second customer.

        Health's contacts per customer and the customer counts read this, so counting the hidden
        rows would understate how often each person is contacted."""
        total = self.inner.count()
        if total == 0:
            return 0
        engine = getattr(self.inner, "_engine", None)
        if engine is not None and engine is self.identity.store.engine:
            from autonomy.store.schema import customer_state_table

            s, t = customer_state_table, link_table
            with engine.connect() as conn:
                hidden = int(
                    conn.execute(
                        select(func.count())
                        .select_from(s.join(t, s.c.customer_id == t.c.alias))
                        .where(t.c.business_id == self.identity.business_id, t.c.status == LINKED)
                    ).scalar_one()
                )
            return max(0, total - hidden)
        return sum(1 for _ in self.scan())


def address_book(
    identity: IdentityMap, fallback: Callable[[str, Channel], str | None] | None = None
) -> Callable[[str, Channel], str | None]:
    """Where a hand-off to Klaviyo goes, for the dispatcher's `AddressBook`.

    Email: the linked profile's email, else the linked profile's own `external_id` (Klaviyo finds a
    profile by it). SMS: the linked profile's phone, else its `external_id`. With no link, a customer
    whose Klaviyo profile is keyed by the Shopify id itself is named by that id; anyone else has no
    known address and is skipped (`no_address`) rather than handed to Klaviyo as a new, empty
    profile that can never be delivered to. Other channels ask `fallback`."""

    def book(customer_id: str, channel: Channel) -> str | None:
        cid = identity.canonical(customer_id)
        if channel not in (Channel.EMAIL, Channel.SMS):
            return fallback(cid, channel) if fallback is not None else None
        aliases = identity.aliases(cid)
        wanted = normalize_email if channel is Channel.EMAIL else normalize_phone
        direct = sorted(a for a in aliases if wanted(a))
        if direct:
            return direct[0]
        external = sorted(
            a
            for a in aliases
            if not normalize_email(a) and not normalize_phone(a) and not a.startswith("klaviyo:")
        )
        if external:
            return external[0]
        if wanted(cid) or identity.known_to_klaviyo(cid):
            return cid
        return fallback(cid, channel) if fallback is not None else None

    return book


def identity_eraser(store: IdentityStore, db: Engine, business_id: str) -> Callable[[Sequence[str]], int]:
    """A Shopify privacy desk eraser: a redacted customer's linked Klaviyo keys go too.

    `customers/redact` names the Shopify customer; the events, state and decisions stored under the
    Klaviyo keys linked to them are the same person's, so they are deleted with the identity facts
    and links. Returns how many rows went."""
    from autonomy.store.schema import customer_state_table, decision_table, event_table

    def erase(customer_ids: Sequence[str]) -> int:
        ids = sorted({c for c in customer_ids if c})
        if not ids:
            return 0
        aliases = sorted(store.aliases_of(business_id, ids))
        removed = 0
        with db.begin() as conn:
            for chunk in _chunks(aliases, 500):
                for table in (event_table, customer_state_table, decision_table):
                    removed += conn.execute(delete(table).where(table.c.customer_id.in_(chunk))).rowcount
        f, t = fact_table, link_table
        with store.engine.begin() as conn:
            for chunk in _chunks(sorted(set(ids) | set(aliases)), 500):
                removed += conn.execute(
                    delete(f).where(f.c.business_id == business_id, f.c.source_key.in_(chunk))
                ).rowcount
                removed += conn.execute(
                    delete(t).where(
                        t.c.business_id == business_id, or_(t.c.alias.in_(chunk), t.c.customer_id.in_(chunk))
                    )
                ).rowcount
        return removed

    return erase


# --- helpers -------------------------------------------------------------------------------------


def _fact(r: Mapping[Any, Any]) -> IdentityFact:
    return IdentityFact(
        business_id=str(r["business_id"]),
        source=str(r["source"]),
        key=str(r["source_key"]),
        email_hash=_opt(r["email_hash"]),
        phone_hash=_opt(r["phone_hash"]),
        external_id=_opt(r["external_id"]),
    )


def _merged(old: IdentityFact, new: IdentityFact) -> IdentityFact:
    """`new`, with each field it does not know taken from `old`."""
    return IdentityFact(
        business_id=new.business_id,
        source=new.source,
        key=new.key,
        email_hash=new.email_hash or old.email_hash,
        phone_hash=new.phone_hash or old.phone_hash,
        external_id=new.external_id or old.external_id,
    )


def _opt(value: object) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _mapping(value: object) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _chunks(items: Sequence[str], size: int) -> Iterator[Sequence[str]]:
    for start in range(0, len(items), size):
        yield items[start : start + size]
