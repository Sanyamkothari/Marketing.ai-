"""Regenerate `configs/aws_prices.yaml` from the public AWS Price List for SageMaker.

The table exists so that a run can report what its compute cost. Plan section 13.3 forbids writing a
number nobody measured, and a *list price* is not a measurement of anybody's bill - it is a published
fact about a rate card. The distinction is kept everywhere: this script records the offer version,
the publication date and the URL it read, `engine/aws/prices.py` copies that provenance into the
`CostEstimate.basis` sentence, and an instance type that is not in the table produces `None` rather
than a guess (DEC-333).

Unlike `gen_templates` and `gen_api_docs`, this generator is **not** wired into `make generate` or
`make check-generated`: its input is a third party's rate card, and a price change at AWS must not
turn a green build red. It is run by hand (`make prices`) and its output is reviewed in the diff.
"""

from __future__ import annotations

import argparse
import csv
import io
import sys
import urllib.request
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Final

COMMAND: Final[str] = "python -m scripts.fetch_aws_prices"
DEFAULT_OUT: Final[Path] = Path("configs/aws_prices.yaml")
DEFAULT_REGION: Final[str] = "ap-south-1"

PRICE_LIST_URL: Final[str] = (
    "https://pricing.us-east-1.amazonaws.com/offers/v1.0/aws/AmazonSageMaker/current/{region}/index.csv"
)
"""The public, unauthenticated offer file. One file per service per region."""

HEADER_PREAMBLE_LINES: Final[int] = 5
"""The offer CSV opens with five metadata rows before the real header (FormatVersion, Disclaimer, ...)."""

COMPONENTS: Final[Mapping[str, str]] = {"Training": "training", "Processing": "processing"}
"""The only two SageMaker components this product runs; the value is the key used in the YAML."""

TIMEOUT_SECONDS: Final[float] = 60.0


class PriceFetchError(Exception):
    """The offer file could not be read or did not contain what this script needs."""


@dataclass(frozen=True, slots=True)
class Offer:
    """One region's rate card, reduced to the rows this product can bill against."""

    region: str
    source_url: str
    offer_version: str
    publication_date: datetime
    rates: Mapping[str, Mapping[str, float]]
    """component -> instance type -> USD per hour."""


def fetch_csv(region: str, *, url_template: str = PRICE_LIST_URL) -> str:
    """The offer CSV for `region`, as text.

    Reads the URL with the standard library so the script has no dependency the engine does not
    already have; `https_proxy` and `SSL_CERT_FILE` are honoured by `urllib` for the same reason.
    """
    url = url_template.format(region=region)
    try:
        with urllib.request.urlopen(url, timeout=TIMEOUT_SECONDS) as response:  # a fixed https URL
            return str(response.read().decode("utf-8"))
    except OSError as exc:
        raise PriceFetchError(f"Could not read the AWS price list at {url}: {type(exc).__name__}.") from exc


def parse_offer(csv_text: str, *, region: str, source_url: str) -> Offer:
    """The on-demand hourly rates for Training and Processing ML instances in `region`.

    Only `TermType == OnDemand` rows whose `Unit` is `Hrs` and whose `Component` is one of
    `COMPONENTS` are kept: those are the two job types `SageMakerJobRunner` creates. A duplicate
    instance type within one component is a contradiction in the source and is refused rather than
    silently resolved, because picking one of two prices is exactly the kind of invented number
    plan section 13.3 rules out.
    """
    lines = csv_text.splitlines()
    if len(lines) <= HEADER_PREAMBLE_LINES:
        raise PriceFetchError("The AWS price list was empty or truncated.")
    preamble = dict(row for row in csv.reader(lines[:HEADER_PREAMBLE_LINES]) if len(row) == 2)
    raw_published = preamble.get("Publication Date")
    offer_version = preamble.get("Version")
    if raw_published is None or offer_version is None:
        raise PriceFetchError("The AWS price list carried no Publication Date or Version row.")
    published = datetime.fromisoformat(raw_published.replace("Z", "+00:00"))

    rates: dict[str, dict[str, float]] = {value: {} for value in COMPONENTS.values()}
    for row in csv.DictReader(io.StringIO("\n".join(lines[HEADER_PREAMBLE_LINES:]))):
        component = COMPONENTS.get(row.get("Component") or "")
        if component is None or row.get("TermType") != "OnDemand" or row.get("Unit") != "Hrs":
            continue
        instance_type = (row.get("Instance Name") or "").strip()
        raw_price = (row.get("PricePerUnit") or "").strip()
        if not instance_type or not raw_price:
            continue
        price = float(raw_price)
        previous = rates[component].get(instance_type)
        if previous is not None and previous != price:
            raise PriceFetchError(
                f"The price list gives {instance_type} two different {component} prices "
                f"({previous} and {price}); this script will not choose between them."
            )
        rates[component][instance_type] = price
    for component, found in rates.items():
        if not found:
            raise PriceFetchError(f"The price list carried no {component} rates for {region}.")
    return Offer(
        region=region,
        source_url=source_url,
        offer_version=offer_version,
        publication_date=published,
        rates={component: dict(sorted(found.items())) for component, found in rates.items()},
    )


def render(offers: Sequence[Offer], *, fetched_at: date) -> str:
    """The `configs/aws_prices.yaml` document, rendered by hand so its comments and order are stable."""
    out: list[str] = [
        f"# GENERATED by `{COMMAND}` - do not edit by hand, and do not add a rate you have not sourced.",
        "#",
        "# These are AWS PUBLISHED LIST PRICES, read from the public price list at the URL below on the",
        "# date below. They are NOT a measurement of anybody's bill: they ignore savings plans, free",
        "# tier, spot, taxes and any negotiated discount, and they go stale the moment AWS changes them.",
        "# `engine/aws/prices.py` carries this provenance into every `CostEstimate.basis` it writes, and",
        "# an instance type that is missing here produces a null `estimated_usd`, never a zero and never",
        "# a guess (plan section 13.3, DEC-333).",
        "",
        f"fetched_at: {fetched_at.isoformat()}",
        "currency: USD",
        "unit: hour",
        "regions:",
    ]
    for offer in offers:
        out.extend(
            [
                f"  {offer.region}:",
                f"    source_url: {offer.source_url}",
                f"    offer_version: '{offer.offer_version}'",
                f"    publication_date: '{offer.publication_date.isoformat()}'",
                "    components:",
            ]
        )
        for component, found in sorted(offer.rates.items()):
            out.append(f"      {component}:")
            out.extend(f"        {instance_type}: {price}" for instance_type, price in found.items())
    out.append("")
    return "\n".join(out)


def build(regions: Iterable[str], *, fetched_at: date, url_template: str = PRICE_LIST_URL) -> str:
    """Fetch every region and render the document."""
    offers = [
        parse_offer(
            fetch_csv(region, url_template=url_template),
            region=region,
            source_url=url_template.format(region=region),
        )
        for region in regions
    ]
    return render(offers, fetched_at=fetched_at)


def main(argv: Sequence[str] | None = None) -> int:
    """`--region` (repeatable), `--out` and `--fetched-at`; returns the process exit code."""
    parser = argparse.ArgumentParser(
        prog=COMMAND, description="Regenerate configs/aws_prices.yaml from the AWS price list."
    )
    parser.add_argument("--region", action="append", default=None, help=f"default: {DEFAULT_REGION}")
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT, help=f"default: {DEFAULT_OUT}")
    parser.add_argument(
        "--fetched-at",
        type=date.fromisoformat,
        default=None,
        help="the date recorded in the file; defaults to today in UTC",
    )
    args = parser.parse_args(argv)
    regions: list[str] = args.region or [DEFAULT_REGION]
    fetched_at: date = args.fetched_at or datetime.now().astimezone().date()
    try:
        document = build(regions, fetched_at=fetched_at)
    except PriceFetchError as exc:
        print(f"{COMMAND}: {exc}", file=sys.stderr)
        return 1
    out: Path = args.out
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(document, encoding="utf-8")
    print(f"{COMMAND}: wrote {out}")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
