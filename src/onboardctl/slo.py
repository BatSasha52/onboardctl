"""Error budget and burn-rate arithmetic.

The alerting scheme is the multi-window, multi-burn-rate approach from the
Google SRE Workbook ("Alerting on SLOs"). Each tier fires when the error
ratio over both a long and a short window is high enough that, if it
continued, the given fraction of the whole error budget would be gone within
the long window. The short window makes the alert stop soon after the
problem stops.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

WINDOW_HOURS: dict[str, int] = {"7d": 7 * 24, "28d": 28 * 24, "30d": 30 * 24}


@dataclass(frozen=True)
class BurnTier:
    severity: str
    long_window: str
    short_window: str
    budget_fraction: Decimal  # share of the whole budget spent within long_window


TIERS: tuple[BurnTier, ...] = (
    BurnTier("critical", "1h", "5m", Decimal("0.02")),
    BurnTier("critical", "6h", "30m", Decimal("0.05")),
    BurnTier("warning", "3d", "6h", Decimal("0.10")),
)

# Every range the recording rules have to provide, shortest first.
RATE_WINDOWS: tuple[str, ...] = ("5m", "30m", "1h", "6h", "3d")

_UNIT_HOURS = {"m": Decimal(1) / 60, "h": Decimal(1), "d": Decimal(24)}


def duration_hours(duration: str) -> Decimal:
    return Decimal(duration[:-1]) * _UNIT_HOURS[duration[-1]]


def error_budget(target_percent: float) -> Decimal:
    """Allowed error ratio for a target given in percent: 99.9 -> 0.001."""
    # repr() gives the shortest decimal form of the float, so 99.9 becomes
    # exactly 99.9 rather than 99.900000000000005684...
    return 1 - Decimal(repr(target_percent)) / 100


def burn_factor(slo_window: str, tier: BurnTier) -> Decimal:
    """How many times faster than "exactly on budget" the tier fires at."""
    factor = tier.budget_fraction * WINDOW_HOURS[slo_window] / duration_hours(tier.long_window)
    return factor.quantize(Decimal("0.0001")).normalize()


def fmt(value: Decimal) -> str:
    """Plain decimal notation without trailing zeros: 14.40 -> 14.4, 1E+1 -> 10."""
    return format(value.normalize(), "f")


def unreachable_tiers(target_percent: float, slo_window: str) -> list[tuple[BurnTier, Decimal]]:
    """Tiers whose threshold is an error ratio of 100% or more, so they can never fire."""
    budget = error_budget(target_percent)
    result = []
    for tier in TIERS:
        threshold = burn_factor(slo_window, tier) * budget
        if threshold >= 1:
            result.append((tier, threshold))
    return result
