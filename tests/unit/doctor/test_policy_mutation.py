"""The mutation test kills every mutant of every data rule (Plan O M233, DEC-1803; the design §5.4).

After Plan K's `engine.agentic.evals.mutation_test`: a rule that can be removed without any fixture's decision
changing is untested, so it does not count. Two kinds of mutant are killed here:

* a rule removed from the engine (`DATA_RULES` without it): some fixture's decision changes;
* a rule left out of one rule set that names it: some fixture of that kind changes - so every rule a rule set
  names matters there, and a rule set cannot carry a rule that could never refuse anything.
"""

from __future__ import annotations

from engine.doctor.policy import DATA_RULE_NAMES, DATA_RULES, evaluate, rule_mutants, rule_set_mutants
from tests.unit.doctor.policy_cases import fixtures, shipped


def test_every_data_rule_has_a_mutant_and_every_mutant_is_killed() -> None:
    assert set(DATA_RULES) == set(DATA_RULE_NAMES)
    caught = rule_mutants(fixtures())
    survivors = sorted(rule for rule, killed_by in caught.items() if not killed_by)
    assert survivors == [], f"no fixture notices these rules missing: {survivors}"


def test_every_rule_of_every_rule_set_matters_there() -> None:
    caught = rule_set_mutants(fixtures(), shipped())
    assert len(caught) == sum(len(rules.rules) for rules in shipped().rule_sets.values())
    survivors = sorted(f"{kind}:{rule}" for (kind, rule), killed_by in caught.items() if not killed_by)
    assert survivors == [], f"these rules could be left out of their rule set unnoticed: {survivors}"


def test_each_one_rule_fixture_is_refused_by_that_rule_alone() -> None:
    """The violators break exactly one rule, so the mutants above are killed for the right reason."""
    for label, subject, ctx in fixtures():
        _kind, rule = label.split(":", 1)
        refusing = {d.rule for d in evaluate(subject, ctx) if not d.allowed}
        if rule == "allowed":
            assert refusing == set(), label
        else:
            assert refusing == {rule}, (label, refusing)
