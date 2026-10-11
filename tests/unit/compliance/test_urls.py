"""Links in a message and the client's URL whitelist (Plan K M134, DEC-1405 (d))."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError

from engine.compliance.config import load_url_whitelist
from engine.compliance.errors import ComplianceError
from engine.compliance.urls import UrlWhitelist, find_urls

LIST = UrlWhitelist(
    urls=("shop.example.in", "*.deals.in", "https://example.in/offers", "cdn.example.in:8443")
)


def test_links_are_found_without_closing_punctuation() -> None:
    text = "Go to https://x.in/a. Or WWW.y.in/b, or (http://z.in/c) - not ftp://w.in or x.in alone"
    assert find_urls(text) == ("https://x.in/a", "WWW.y.in/b", "http://z.in/c")
    assert find_urls("no links here") == ()


@pytest.mark.parametrize(
    ("url", "allowed"),
    [
        ("https://shop.example.in/diwali?x=1", True),
        ("http://SHOP.example.in", True),
        ("www.shop.example.in/x", False),  # another host: www.shop is not shop
        ("https://evil.shop.example.in", False),
        ("https://a.deals.in/x", True),
        ("www.b.c.deals.in", True),
        ("https://deals.in", False),  # `*.` means a sub-domain only
        ("https://example.in/offers", True),
        ("https://example.in/offers/diwali", True),
        ("https://example.in/offers2", False),  # a prefix ends at a path boundary
        ("http://example.in/offers", False),  # a prefix keeps its scheme
        ("www.example.in/offers", False),  # no scheme: host entries only
        ("https://cdn.example.in:8443/a.png", True),
        ("https://cdn.example.in/a.png", False),
        ("https://bit.ly/x9", False),
    ],
)
def test_an_entry_is_a_host_a_sub_domain_host_or_a_prefix(url: str, allowed: bool) -> None:
    assert LIST.allows(url) is allowed


def test_the_channels_a_list_covers() -> None:
    assert LIST.applies_to("email") and LIST.applies_to("sms")
    sms_only = UrlWhitelist(channels=(" SMS",), urls=("x.in",))
    assert sms_only.channels == ("sms",)
    assert sms_only.applies_to("sms") and not sms_only.applies_to("email")


@pytest.mark.parametrize(
    "document",
    [
        {"urls": []},
        {"urls": ["not a host"]},
        {"urls": ["https://"]},
        {"urls": ["x.in"], "channels": ["e-mail"]},
        {"urls": ["x.in"], "invented": 1},
    ],
)
def test_a_list_that_is_not_valid_is_refused(document: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        UrlWhitelist.model_validate(document)


def test_the_list_is_read_from_its_file_else_the_region_block_else_not_at_all(tmp_path: Path) -> None:
    assert load_url_whitelist(tmp_path) == (None, None)
    region = {"url_whitelist": {"urls": ["region.example.in"]}}
    from_region, source = load_url_whitelist(tmp_path, region)
    assert from_region is not None and from_region.urls == ("region.example.in",)
    assert source == "region file: url_whitelist"
    (tmp_path / "compliance").mkdir()
    (tmp_path / "compliance" / "url_whitelist.yaml").write_text(
        yaml.safe_dump({"channels": ["sms"], "urls": ["file.example.in"]}), encoding="utf-8"
    )
    from_file, source = load_url_whitelist(tmp_path, region)
    assert from_file is not None and from_file.urls == ("file.example.in",)
    assert source == "compliance/url_whitelist.yaml"
    (tmp_path / "compliance" / "url_whitelist.yaml").write_text(
        "urls: [42 is not a host]\n", encoding="utf-8"
    )
    with pytest.raises(ComplianceError) as caught:
        load_url_whitelist(tmp_path)
    assert caught.value.code == "CONFIG_INVALID"


def test_bare_hosts_are_links_too_but_not_e_mail_addresses_or_words() -> None:
    """The first review: a link with no scheme (`deals-x.in/a`) was never found, so never checked."""
    from engine.compliance.urls import find_links

    text = (
        "Shop at deals-x.in/a, or bit.ly/x9. Visit https://shop.example.in/d and WWW.y.in/b. "
        "Mail help@acme.in; Rs.500 off, e.g. now, 4.5 stars, file v2.0 - not shop.example.in2 - ftp://w.in"
    )
    assert find_links(text) == (
        "deals-x.in/a",
        "bit.ly/x9",
        "https://shop.example.in/d",
        "WWW.y.in/b",
        "w.in",
    )
    assert find_urls(text) == ("https://shop.example.in/d", "WWW.y.in/b"), "find_urls keeps its meaning"


@pytest.mark.parametrize(
    ("url", "allowed"),
    [
        ("shop.example.in/diwali", True),  # a host entry: any link to exactly this host, scheme or not
        ("SHOP.example.in", True),
        ("x.deals.in/a", True),
        ("deals-x.in/a", False),
        ("cdn.example.in:8443/a.png", True),
        ("example.in/offers", False),  # no scheme: host entries only, never a prefix
    ],
)
def test_a_bare_host_is_matched_against_the_host_entries(url: str, allowed: bool) -> None:
    assert LIST.allows(url) is allowed


def test_finding_links_stays_linear_on_long_dotted_and_undotted_words() -> None:
    import time

    from engine.compliance.urls import find_links

    for text in ("a." * 50_000 + "1", "a" * 100_000 + ".", ("x-" * 50_000) + "@y"):
        start = time.perf_counter()
        find_links(text)
        assert time.perf_counter() - start < 1.0
