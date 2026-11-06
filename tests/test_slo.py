from __future__ import annotations

from decimal import Decimal

import pytest

from onboardctl import slo


@pytest.mark.parametrize(
    ("window", "expected"),
    [
        ("30d", ["14.4", "6", "1"]),
        ("28d", ["13.44", "5.6", "0.9333"]),
        ("7d", ["3.36", "1.4", "0.2333"]),
    ],
)
def test_burn_factors(window: str, expected: list[str]) -> None:
    assert [slo.fmt(slo.burn_factor(window, tier)) for tier in slo.TIERS] == expected


@pytest.mark.parametrize(
    ("target", "budget"), [(99.9, "0.001"), (99.5, "0.005"), (99, "0.01"), (95, "0.05")]
)
def test_error_budget_is_exact(target: float, budget: str) -> None:
    assert slo.error_budget(target) == Decimal(budget)


def test_unreachable_tiers() -> None:
    # 14.4 x 10% budget = 144% error ratio: impossible. 6 x 10% = 60% is fine.
    unreachable = slo.unreachable_tiers(90, "30d")
    assert [t.long_window for t, _ in unreachable] == ["1h"]
    assert slo.unreachable_tiers(99.9, "30d") == []


def test_duration_hours() -> None:
    assert slo.duration_hours("5m") * 12 == 1
    assert slo.duration_hours("3d") == 72
