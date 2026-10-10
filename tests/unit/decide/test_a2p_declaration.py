"""Plan K M131 (DEC-1323) part A: an automated call must be declared to the operator (TRAI Third Amendment).

The rule is data, not code: `configs/regions/in.yaml` carries `call_requires_true: [a2p_declared]` (and
`ivr_requires_true`), the second family beside `<channel>_requires`. An action sent on that channel must
have the field exactly `true`; null and false are both refused with `ACTION_A2P_UNDECLARED`. Python names
no region and no channel.
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from engine.config import ConfigError, config_root
from engine.decide.catalogue import CATALOGUE_FILENAME, ActionItem, load_catalogue

REGION_ONLY_SMS = "sms_requires:\n  - dlt_template_id\n"


def _root(tmp_path: Path, catalogue: str, *, region_text: str | None = None) -> Path:
    """A config root whose catalogue is `catalogue` and whose regions are the shipped ones (or `in.yaml` as given)."""
    shutil.copytree(config_root() / "regions", tmp_path / "regions")
    if region_text is not None:
        (tmp_path / "regions" / "in.yaml").write_text(region_text, encoding="utf-8")
    (tmp_path / "decide").mkdir()
    (tmp_path / CATALOGUE_FILENAME).write_text(catalogue, encoding="utf-8")
    return tmp_path


def _call_action(*lines: str, channel: str = "call") -> str:
    body = "".join(f"    {line}\n" for line in lines)
    return (
        "region: IN\nactions:\n"
        f"  - action_id: reminder_call\n    label: Reminder call\n    channels: [{channel}]\n{body}"
    )


def test_the_shipped_india_file_asks_for_a_declaration_on_call_and_ivr() -> None:
    text = (config_root() / "regions" / "in.yaml").read_text(encoding="utf-8")
    assert "call_requires_true:\n  - a2p_declared" in text
    assert "ivr_requires_true:\n  - a2p_declared" in text
    assert "telecaller" in text, "the file says a human-dialled channel must use another name"


@pytest.mark.parametrize("channel", ["call", "ivr"])
def test_an_undeclared_automated_call_action_is_refused(tmp_path: Path, channel: str) -> None:
    root = _root(tmp_path, _call_action(channel=channel))
    with pytest.raises(ConfigError) as caught:
        load_catalogue(root=root)
    error = caught.value
    assert error.code == "ACTION_A2P_UNDECLARED"
    assert error.path == "actions[0].a2p_declared"
    for word in ("reminder_call", channel, "'IN'", "a2p_declared"):
        assert word in error.message, word


@pytest.mark.parametrize("channel", ["call", "ivr"])
def test_a_call_action_declared_false_is_refused_the_same_way(tmp_path: Path, channel: str) -> None:
    root = _root(tmp_path, _call_action("a2p_declared: false", channel=channel))
    with pytest.raises(ConfigError) as caught:
        load_catalogue(root=root)
    assert caught.value.code == "ACTION_A2P_UNDECLARED"
    assert "false" in caught.value.message


@pytest.mark.parametrize("channel", ["call", "ivr"])
def test_a_declared_call_action_loads(tmp_path: Path, channel: str) -> None:
    root = _root(tmp_path, _call_action("a2p_declared: true", channel=channel))
    action = load_catalogue(root=root).actions[0]
    assert action.a2p_declared is True and action.channels == (channel,)


def test_the_position_of_the_offending_action_is_in_the_path(tmp_path: Path) -> None:
    text = (
        "region: IN\nactions:\n"
        "  - {action_id: first_email, label: First, channels: [email]}\n"
        "  - {action_id: second_call, label: Second, channels: [email, call]}\n"
    )
    with pytest.raises(ConfigError) as caught:
        load_catalogue(root=_root(tmp_path, text))
    assert (caught.value.code, caught.value.path) == ("ACTION_A2P_UNDECLARED", "actions[1].a2p_declared")


def test_an_action_on_another_channel_is_unaffected(tmp_path: Path) -> None:
    text = (
        "region: IN\nactions:\n"
        "  - {action_id: offer_email, label: Offer by email, channels: [email]}\n"
        "  - {action_id: offer_telecaller, label: Offer by a person, channels: [telecaller]}\n"
        '  - {action_id: offer_sms, label: Offer by SMS, channels: [sms], dlt_template_id: "11070001", message_category: promotional}\n'
    )
    catalogue = load_catalogue(root=_root(tmp_path, text))
    assert [action.a2p_declared for action in catalogue.actions] == [None, None, None]


def test_a_region_file_without_the_new_rule_does_not_ask_for_a_declaration(tmp_path: Path) -> None:
    root = _root(tmp_path, _call_action(), region_text=REGION_ONLY_SMS)
    assert load_catalogue(root=root).actions[0].a2p_declared is None


def test_no_region_means_no_rule(tmp_path: Path) -> None:
    text = "actions:\n  - {action_id: reminder_call, label: Reminder call, channels: [call]}\n"
    assert load_catalogue(root=_root(tmp_path, text)).actions[0].a2p_declared is None


def test_the_rule_is_not_specific_to_the_fields_it_ships_with(tmp_path: Path) -> None:
    """Any listed field must be exactly true; one that is not the A2P field is `CATALOGUE_INVALID`."""
    root = _root(tmp_path, _call_action(), region_text="whatsapp_requires_true:\n  - a2p_declared\n")
    assert load_catalogue(root=root).actions[0].channels == ("call",), "other channels are not asked"
    text = "region: IN\nactions:\n  - {action_id: wa, label: WhatsApp offer, channels: [whatsapp], priority: 1}\n"
    with pytest.raises(ConfigError) as caught:
        load_catalogue(
            root=_root(tmp_path / "second", text, region_text="whatsapp_requires_true:\n  - eligibility\n")
        )
    assert caught.value.code == "CATALOGUE_INVALID"
    assert caught.value.path == "actions[0].eligibility"


@pytest.mark.parametrize("written", ['"true"', '"yes"', '"1"', "1", "0", "maybe"])
def test_only_a_real_boolean_is_a_declaration(tmp_path: Path, written: str) -> None:
    """The field is a strict boolean: a quoted "true", a number or a word is refused, never read as declared."""
    root = _root(tmp_path, _call_action(f"a2p_declared: {written}"))
    with pytest.raises(ConfigError):
        load_catalogue(root=root)
    with pytest.raises(ValueError):
        ActionItem.model_validate(
            {"action_id": "a", "label": "A", "channels": ["call"], "a2p_declared": written.strip('"')}
        )


def test_the_shipped_example_keeps_loading_and_shows_the_call_action_commented_out(tmp_path: Path) -> None:
    example = (config_root() / "decide" / "catalogue.example.yaml").read_text(encoding="utf-8")
    assert "# - action_id: example_offer_call" in example
    assert "#   a2p_declared: true" in example
    filled = example.replace("<your DLT template id>", "1107000000000000001")
    root = _root(tmp_path, filled)
    assert len(load_catalogue(root=root).actions) == 2
    # Un-commenting the example call action gives a catalogue the India file accepts.
    active = filled.replace("  # - action_id: example_offer_call", "  - action_id: example_offer_call")
    for field in ("label", "channels", "offer_cost", "contact_cost", "a2p_declared"):
        active = active.replace(f"  #   {field}:", f"    {field}:")
    root2 = _root(tmp_path / "again", active)
    assert load_catalogue(root=root2).actions_by_id["example_offer_call"].a2p_declared is True
