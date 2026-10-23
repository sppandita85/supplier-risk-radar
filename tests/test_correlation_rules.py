"""Unit tests for the rule-based correlation engine (no database)."""
from radar import correlate
from radar.config import load_rules

from .conftest import sig

RULES = load_rules()["rules"]


def test_burst_needs_three_signals_in_window():
    matched, *_ = correlate.rule_burst([sig(1, 6, 3), sig(2, 3, 3), sig(3, 0, 3)], RULES["burst"])
    assert matched
    matched, _, used, reason = correlate.rule_burst([sig(1, 20, 3), sig(2, 0, 3)], RULES["burst"])
    assert not matched and "only 1 signals" in reason


def test_multi_category_adds_bonus_per_extra_category():
    sigs = [sig(1, 2, 3, "operational"), sig(2, 1, 3, "financial"), sig(3, 0, 3, "compliance")]
    matched, score, _, _ = correlate.rule_multi_category(sigs, RULES["multi_category"])
    assert matched
    assert score == correlate._pts(sigs) + 2 * 2


def test_escalation_requires_rising_severity():
    rising = [sig(1, 6, 2), sig(2, 4, 3), sig(3, 2, 4)]
    assert correlate.rule_escalation(rising, RULES["escalation"])[0]
    falling = [sig(1, 6, 4), sig(2, 4, 3), sig(3, 2, 2)]
    matched, _, _, reason = correlate.rule_escalation(falling, RULES["escalation"])
    assert not matched and reason == "no rising severity trend"


def test_critical_event_needs_high_confidence():
    assert correlate.rule_critical_event([sig(1, 0, 5, confidence=0.9)], RULES["critical_event"])[0]
    assert not correlate.rule_critical_event([sig(1, 0, 5, confidence=0.5)], RULES["critical_event"])[0]


def test_episodes_split_on_gap():
    eps = correlate._episodes([sig(1, 40, 3), sig(2, 38, 3), sig(3, 5, 3)])
    assert [len(e) for e in eps] == [2, 1]


def test_alert_severity_bands():
    assert correlate._alert_severity(12, "burst") == "medium"
    assert correlate._alert_severity(15, "burst") == "high"
    assert correlate._alert_severity(25, "burst") == "critical"
    assert correlate._alert_severity(10, "critical_event") == "critical"


def test_criticality_weight():
    assert correlate._weight(5) > correlate._weight(1)
