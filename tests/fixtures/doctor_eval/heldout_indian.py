"""Twenty held-out Indian customer exports for Plan O M248 (the design §12; Plan O §9.1 P3, P5).

**Independence.** This generator was written without reading Data Doctor's lexicons or rules (the Hinglish header
words, the Indian number and yes/no vocabularies, the ID patterns), and it imports nothing from the product: only
the standard library and `openpyxl` (for the workbooks). A test parses this file and fails if it ever imports
`engine`, `api`, `scripts` or another fixture (`test_the_heldout_generator_imports_nothing_from_the_product`), and
another fails if any product or tuning module imports it. The spellings below are the way such exports are
written in practice - romanised Hindi headers, Devanagari headers, abbreviations, lakh and crore amounts, a
trailing `/-`, `haan/nahi`, GSTINs, PIN codes, `+91` mobiles - chosen by this file's author, not looked up.

**Never used for tuning.** The benchmark reports these files in their own section; no rule, threshold or lexicon
may be changed because of them. Until the author the approvers name (Plan O §10, question 2) delivers their own
files, these are the stand-in, and the benchmark reads a folder of real ones (`--heldout-dir`) with no change.

Each file is the customer list of a Targeted Advertisement campaign (who bought within 30 days of seeing an ad).
:func:`heldout_files` returns the bytes and the truth the harness needs to measure harm: the ID column, the
outcome column and each row's outcome, the header as written, the number of data rows.
"""

from __future__ import annotations

import csv
import io
import random
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Any, Final

__all__ = ["HELDOUT_COUNT", "HeldoutFile", "heldout_files"]

HELDOUT_COUNT: Final[int] = 20
USE_CASE: Final[str] = "targeted-advertisement"
XLSX: Final[str] = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"

# Header words, as clerks in Indian firms write them (the author's own choices).
_ID_HEADERS: Final[tuple[str, ...]] = (
    "Grahak ID",
    "grahak_sankhya",
    "Cust No.",
    "ग्राहक आईडी",
    "Customer Code",
    "khata_no",
    "Party Code",
    "ग्राहक क्रमांक",
)
_NAME_HEADERS: Final[tuple[str, ...]] = ("Naam", "Customer Name", "ग्राहक का नाम", "Party Name")
_CITY_HEADERS: Final[tuple[str, ...]] = ("Shahar", "City", "शहर", "Location")
_PIN_HEADERS: Final[tuple[str, ...]] = ("Pin Code", "PIN", "pincode", "पिन कोड")
_MOBILE_HEADERS: Final[tuple[str, ...]] = ("Mobile No", "Phone", "मोबाइल", "Contact No.")
_SPEND_HEADERS: Final[tuple[str, ...]] = (
    "Kul Kharch",
    "Total Spend (Rs)",
    "कुल खर्च",
    "Amt",
    "Saal ka kharcha",
)
_VISITS_HEADERS: Final[tuple[str, ...]] = (
    "Visits (7 din)",
    "Hafte ki visits",
    "visits_7d",
    "पिछले 7 दिन विज़िट",
)
_TENURE_HEADERS: Final[tuple[str, ...]] = ("Kitne mahine se", "Tenure (months)", "Purana grahak (mahine)")
_ACTIVE_HEADERS: Final[tuple[str, ...]] = ("Sakriya", "Active?", "सक्रिय", "Chalu hai")
_CONSENT_HEADERS: Final[tuple[str, ...]] = ("SMS ki anumati", "Marketing OK", "विज्ञापन सहमति", "DND hata")
_JOINED_HEADERS: Final[tuple[str, ...]] = ("Judne ki tareekh", "Join Date", "जुड़ने की तारीख", "Reg. Dt")
_GST_HEADERS: Final[tuple[str, ...]] = ("GSTIN", "GST No.", "जीएसटी नंबर")
_PLAN_HEADERS: Final[tuple[str, ...]] = ("Yojana", "Plan", "प्लान", "Scheme")
_OUTCOME_HEADERS: Final[tuple[str, ...]] = (
    "Kharida (30 din)",
    "Bought in 30 days",
    "खरीदा",
    "Purchase Hua",
    "converted",
    "Kharidari ki",
)
_CITIES: Final[tuple[str, ...]] = (
    "Mumbai",
    "Pune",
    "Nagpur",
    "Indore",
    "Lucknow",
    "Patna",
    "Jaipur",
    "Surat",
    "Kochi",
    "Bhubaneswar",
    "Bengaluru",
    "Hyderabad",
)
_FIRST: Final[tuple[str, ...]] = (
    "Ramesh",
    "Sunita",
    "Aarav",
    "Priya",
    "Imran",
    "Kavya",
    "Harpreet",
    "Deepak",
)
_LAST: Final[tuple[str, ...]] = ("Sharma", "Patil", "Iyer", "Khan", "Das", "Reddy", "Gill", "Verma")
_PLANS: Final[tuple[tuple[str, ...], ...]] = (
    ("Basic", "Gold", "Platinum"),
    ("Silver", "Gold", "Diamond"),
    ("मूल", "स्वर्ण", "प्लैटिनम"),
)
_YES_NO: Final[tuple[tuple[str, str], ...]] = (
    ("haan", "nahi"),
    ("Haan", "Nahin"),
    ("हाँ", "नहीं"),
    ("Y", "N"),
    ("1", "0"),
    ("Yes", "No"),
)
_GST_STATES: Final[tuple[str, ...]] = ("27", "29", "09", "24", "33", "07")
_ALNUM: Final[str] = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ"


@dataclass(frozen=True)
class _Spec:
    """How one held-out export is written."""

    number: int
    fmt: str
    rows: int
    titles: int
    footer: bool
    money: str
    yes_no: int
    outcome_yes_no: int
    date_style: str
    with_gstin: bool
    with_name: bool


@dataclass(frozen=True)
class HeldoutFile:
    """One held-out export and the truth the harness measures against."""

    name: str
    use_case: str
    file_name: str
    content: bytes
    content_type: str
    key_column: str
    outcome_column: str
    outcome_positive: str
    outcome_negative: str
    header: tuple[str, ...]
    data_rows: int
    ids: tuple[str, ...]
    outcomes: tuple[int, ...] = field(repr=False)
    """Each data row's outcome, 1 or 0, in row order (aligned with `ids`)."""
    personal_values: tuple[str, ...] = field(default=(), repr=False)
    """A few planted personal values (names, mobiles, GSTINs): none may reach a prompt."""


def _specs() -> tuple[_Spec, ...]:
    out: list[_Spec] = []
    for number in range(HELDOUT_COUNT):
        out.append(
            _Spec(
                number=number + 1,
                fmt="xlsx" if number % 3 == 1 else "csv",
                rows=1_200 + 150 * (number % 7),
                titles=(0, 1, 2, 3)[number % 4],
                footer=number % 2 == 1,
                money=("lakh", "plain_commas", "rupee_slash", "crore_mix", "k_suffix")[number % 5],
                yes_no=number % len(_YES_NO),
                outcome_yes_no=(4, 0, 3, 4, 2, 5, 4)[number % 7],
                date_style=("dmy_slash", "dmy_dash", "iso", "d_mon_y")[number % 4],
                with_gstin=number % 4 in (0, 3),
                with_name=number % 5 != 2,
            )
        )
    return tuple(out)


def _money(value: float, style: str, rng: random.Random) -> str:
    rupees = round(value)
    if style == "lakh" and rupees >= 100_000:
        return f"₹{rupees / 100_000:.1f} L"
    if style == "crore_mix" and rupees >= 1_000_000:
        return f"{rupees / 10_000_000:.2f} Cr"
    if style == "k_suffix" and rupees >= 1_000:
        return f"{rupees // 1_000}K"
    if style in ("plain_commas", "rupee_slash", "lakh", "crore_mix"):
        text = _indian_commas(rupees)
        return f"Rs {text}/-" if style == "rupee_slash" or rng.random() < 0.1 else text
    return str(rupees)


def _indian_commas(value: int) -> str:
    digits = str(value)
    if len(digits) <= 3:
        return digits
    head, tail = digits[:-3], digits[-3:]
    groups: list[str] = []
    while len(head) > 2:
        groups.insert(0, head[-2:])
        head = head[:-2]
    if head:
        groups.insert(0, head)
    return ",".join(groups) + "," + tail


def _date(day: date, style: str) -> str:
    if style == "dmy_slash":
        return day.strftime("%d/%m/%Y")
    if style == "dmy_dash":
        return day.strftime("%d-%m-%Y")
    if style == "d_mon_y":
        return day.strftime("%d %b %Y")
    return day.isoformat()


def _gstin(rng: random.Random) -> str:
    """A GSTIN-shaped value; its check character is random, so some are valid and most are not."""
    letters = "".join(rng.choice("ABCDEFGHIJKLMNOPQRSTUVWXYZ") for _ in range(5))
    digits = "".join(rng.choice("0123456789") for _ in range(4))
    return f"{rng.choice(_GST_STATES)}{letters}{digits}{rng.choice('ABCDEFGHIJKLMNOPQRSTUVWXYZ')}1Z{rng.choice(_ALNUM)}"


def _mobile(rng: random.Random) -> str:
    number = f"{rng.choice('6789')}{rng.randrange(10**8, 10**9)}"
    return rng.choice((f"+91 {number[:5]} {number[5:]}", f"0{number}", number, f"+91-{number}"))


def _one(spec: _Spec) -> HeldoutFile:
    rng = random.Random(9_000 + spec.number)
    pick = spec.number - 1
    headers = {
        "id": _ID_HEADERS[pick % len(_ID_HEADERS)],
        "name": _NAME_HEADERS[pick % len(_NAME_HEADERS)],
        "city": _CITY_HEADERS[pick % len(_CITY_HEADERS)],
        "pin": _PIN_HEADERS[pick % len(_PIN_HEADERS)],
        "mobile": _MOBILE_HEADERS[pick % len(_MOBILE_HEADERS)],
        "spend": _SPEND_HEADERS[pick % len(_SPEND_HEADERS)],
        "visits": _VISITS_HEADERS[pick % len(_VISITS_HEADERS)],
        "tenure": _TENURE_HEADERS[pick % len(_TENURE_HEADERS)],
        "plan": _PLAN_HEADERS[pick % len(_PLAN_HEADERS)],
        "active": _ACTIVE_HEADERS[pick % len(_ACTIVE_HEADERS)],
        "consent": _CONSENT_HEADERS[pick % len(_CONSENT_HEADERS)],
        "joined": _JOINED_HEADERS[pick % len(_JOINED_HEADERS)],
        "gstin": _GST_HEADERS[pick % len(_GST_HEADERS)],
        "outcome": _OUTCOME_HEADERS[pick % len(_OUTCOME_HEADERS)],
    }
    order = ["id", "name", "city", "pin", "mobile", "joined", "tenure", "visits", "spend", "plan", "active"]
    order += ["gstin"] if spec.with_gstin else []
    order += ["consent", "outcome"]
    if not spec.with_name:
        order.remove("name")
    yes, no = _YES_NO[spec.yes_no]
    out_yes, out_no = _YES_NO[spec.outcome_yes_no]
    plans = _PLANS[pick % len(_PLANS)]
    prefix = ("G", "C", "KH", "P", "")[pick % 5]
    rows: list[dict[str, Any]] = []
    ids: list[str] = []
    outcomes: list[int] = []
    personal: list[str] = []
    start = date(2023, 4, 1)
    for index in range(spec.rows):
        visits = rng.randrange(0, 15)
        tenure = rng.randrange(1, 96)
        plan = rng.randrange(len(plans))
        active = rng.random() < 0.7
        spend = rng.lognormvariate(11.0 + 0.4 * plan, 0.9)
        score = -2.4 + 0.18 * visits + 0.5 * plan + (0.6 if active else -0.4) - 0.004 * tenure
        bought = 1 if rng.random() < 1 / (1 + 2.718281828 ** (-score)) else 0
        customer = f"{prefix}{100_000 + index * 3 + spec.number:06d}"
        name = f"{rng.choice(_FIRST)} {rng.choice(_LAST)}"
        mobile = _mobile(rng)
        gst = _gstin(rng) if rng.random() < 0.35 else ""
        if index < 3:
            personal += [name, mobile] + ([gst] if gst and spec.with_gstin else [])
        rows.append(
            {
                "id": customer,
                "name": name,
                "city": rng.choice(_CITIES),
                "pin": f"{rng.randrange(110_001, 855_999):06d}",
                "mobile": mobile,
                "joined": _date(start + timedelta(days=rng.randrange(0, 900)), spec.date_style),
                "tenure": tenure,
                "visits": visits,
                "spend": _money(spend, spec.money, rng),
                "plan": plans[plan],
                "active": yes if active else no,
                "gstin": gst,
                "consent": yes if rng.random() < 0.85 else no,
                "outcome": out_yes if bought else out_no,
            }
        )
        ids.append(customer)
        outcomes.append(bought)
    header = tuple(headers[key] for key in order)
    width = len(header)
    grid: list[list[Any]] = []
    titles = ("Grahak suchi - vigyapan abhiyan", "Report nikala: 01-10-2026", "Sirf aantarik upyog ke liye")
    for title in titles[: spec.titles]:
        grid.append([title] + [None] * (width - 1))
    grid.append(list(header))
    grid += [[row[key] for key in order] for row in rows]
    if spec.footer:
        grid.append(["Kul"] + [None] * (width - 1))
        grid.append(["--- report samaapt ---"] + [None] * (width - 1))
    name = f"in_{spec.number:02d}"
    if spec.fmt == "xlsx":
        from openpyxl import Workbook  # type: ignore[import-untyped]

        book = Workbook()
        sheet = book.active
        sheet.title = "Grahak"
        for line in grid:
            sheet.append(line)
        buffer = io.BytesIO()
        book.save(buffer)
        content, kind, file_name = buffer.getvalue(), XLSX, f"{name}.xlsx"
    else:
        text = io.StringIO()
        writer = csv.writer(text, lineterminator="\n")
        for line in grid:
            writer.writerow(["" if value is None else value for value in line])
        content, kind, file_name = text.getvalue().encode("utf-8"), "text/csv", f"{name}.csv"
    return HeldoutFile(
        name=name,
        use_case=USE_CASE,
        file_name=file_name,
        content=content,
        content_type=kind,
        key_column=headers["id"],
        outcome_column=headers["outcome"],
        outcome_positive=out_yes,
        outcome_negative=out_no,
        header=header,
        data_rows=spec.rows,
        ids=tuple(ids),
        outcomes=tuple(outcomes),
        personal_values=tuple(personal),
    )


def heldout_files() -> tuple[HeldoutFile, ...]:
    """The twenty held-out exports: the same rows in every run (a workbook's file properties carry a save time)."""
    return tuple(_one(spec) for spec in _specs())
