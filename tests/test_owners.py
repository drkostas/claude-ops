import pytest

from claude_ops import owners


def test_the_exact_address_beats_the_wide_one():
    rules = {"wide": ["service://x/%"], "narrow": ["service://x/special"]}
    assert owners.most_specific("service://x/special", rules) == "narrow"
    assert owners.most_specific("service://x/other", rules) == "wide"
    assert owners.most_specific("node://y", rules) is None


def test_globs_cross_folders_and_the_longer_one_wins():
    rules = {"tools": ["kernel/tools/*"], "ship": ["kernel/tools/ship*.sh"], "all": ["kernel/*"]}
    assert owners.most_specific("kernel/tools/ship_train.sh", rules, kind="glob") == "ship"
    assert owners.most_specific("kernel/tools/heavy.sh", rules, kind="glob") == "tools"
    assert owners.most_specific("kernel/core/a/b.py", rules, kind="glob") == "all"


def test_a_tie_goes_alphabetically_whatever_the_order():
    assert owners.most_specific("a/b", {"zeta": ["a/*"], "alpha": ["a/?"]}, kind="glob") == "alpha"
    assert owners.most_specific("a/b", {"alpha": ["a/?"], "zeta": ["a/*"]}, kind="glob") == "alpha"


def test_like_underscore_is_one_character():
    assert owners.most_specific("node://pi2", {"pis": ["node://pi_"]}) == "pis"
    assert owners.most_specific("node://pi22", {"pis": ["node://pi_"]}) is None


def test_an_unknown_kind_is_refused():
    with pytest.raises(ValueError):
        owners.most_specific("x", {"a": ["x"]}, kind="regex")
