"""Prometheus recording and alerting rules generated from a spec's SLOs."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from decimal import Decimal

from onboardctl import slo
from onboardctl.spec import ServiceSpec


@dataclass(frozen=True)
class SliQueries:
    sli: str  # "availability" or "latency"
    selector: str  # PromQL label matchers for this service's series
    window: str  # SLO window, such as 30d
    target: float
    budget: Decimal
    error_ratio: Callable[[str, str], str]  # (range, separator) -> PromQL

    def record(self, rate_window: str) -> str:
        return f"slo:{self.sli}_error_ratio:rate{rate_window}"

    def ratio(self, rate_window: str) -> str:
        """The recorded error ratio for one range, selected for this service."""
        return f"{self.record(rate_window)}{{{self.selector}}}"

    @property
    def window_error_ratio(self) -> str:
        return self.error_ratio(self.window, " ")


@dataclass(frozen=True)
class RecordingRule:
    record: str
    expr: str


@dataclass(frozen=True)
class AlertRule:
    alert: str
    expr: str
    labels: dict[str, str]
    annotations: dict[str, str]
    for_: str | None = None


@dataclass(frozen=True)
class RuleSet:
    recording_group: str
    alert_group: str
    recording: list[RecordingRule] = field(default_factory=list)
    alerts: list[AlertRule] = field(default_factory=list)


def service_selector(spec: ServiceSpec) -> str:
    # job is set to the service name by the ServiceMonitor's relabeling.
    return f'namespace="{spec.target_namespace}", job="{spec.name}"'


def bucket_bound(seconds: float) -> str:
    """Format a bucket bound the way the Go client writes `le`: 0.25, 1, 10."""
    return slo.fmt(Decimal(repr(seconds)))


def sli_queries(spec: ServiceSpec) -> dict[str, SliQueries]:
    sel = service_selector(spec)
    metrics = spec.slo.metrics
    requests = metrics.requests

    def availability(rate_window: str, sep: str) -> str:
        return sep.join(
            [
                f"sum by (namespace, job) (rate({requests}{{{sel}, {metrics.error_selector}}}"
                f"[{rate_window}]))",
                "/",
                f"sum by (namespace, job) (rate({requests}{{{sel}}}[{rate_window}]))",
            ]
        )

    result = {
        "availability": SliQueries(
            "availability",
            sel,
            spec.slo.window,
            spec.slo.availability.target,
            slo.error_budget(spec.slo.availability.target),
            availability,
        )
    }

    latency = spec.slo.latency
    if latency is not None:
        hist = metrics.latency_histogram
        le = bucket_bound(latency.threshold_seconds)

        def latency_errors(rate_window: str, sep: str) -> str:
            # The share of requests slower than the threshold.
            inner = "  " if sep == "\n" else ""
            return sep.join(
                [
                    "1 - (",
                    f'{inner}sum by (namespace, job) (rate({hist}_bucket{{{sel}, le="{le}"}}'
                    f"[{rate_window}]))",
                    f"{inner}/",
                    f"{inner}sum by (namespace, job) (rate({hist}_count{{{sel}}}[{rate_window}]))",
                    ")",
                ]
            )

        result["latency"] = SliQueries(
            "latency",
            sel,
            spec.slo.window,
            latency.target,
            slo.error_budget(latency.target),
            latency_errors,
        )
    return result


def _describe(spec: ServiceSpec, queries: SliQueries) -> str:
    if queries.sli == "latency" and spec.slo.latency:
        return (
            f"{queries.target:g}% of requests faster than "
            f"{bucket_bound(spec.slo.latency.threshold_seconds)}s"
        )
    return f"{queries.target:g}% of requests succeed"


def _remediation_labels(
    spec: ServiceSpec, queries: SliQueries, tier: slo.BurnTier
) -> dict[str, str]:
    """Labels that route the fast availability page to the remediation responder."""
    if spec.remediation is None or queries.sli != "availability" or tier != slo.TIERS[0]:
        return {}
    return {"remediation": "rollout-restart", "deployment": spec.remediation.restart_deployment}


def build_rules(spec: ServiceSpec) -> RuleSet:
    rules = RuleSet(
        recording_group=f"onboardctl.{spec.name}.recording",
        alert_group=f"onboardctl.{spec.name}.alerts",
    )
    base_labels = {"service": spec.name, "team": spec.team}
    runbook = {"runbook_url": spec.alerting.runbook_url} if spec.alerting.runbook_url else {}

    for queries in sli_queries(spec).values():
        for rate_window in slo.RATE_WINDOWS:
            rules.recording.append(
                RecordingRule(queries.record(rate_window), queries.error_ratio(rate_window, "\n"))
            )
        for tier in slo.TIERS:
            factor = slo.burn_factor(spec.slo.window, tier)
            threshold = f"({slo.fmt(factor)} * {slo.fmt(queries.budget)})"
            rules.alerts.append(
                AlertRule(
                    alert="ErrorBudgetBurn",
                    expr="\n".join(
                        [
                            f"{queries.ratio(tier.long_window)} > {threshold}",
                            "and",
                            f"{queries.ratio(tier.short_window)} > {threshold}",
                        ]
                    ),
                    labels={
                        **base_labels,
                        "severity": tier.severity,
                        "slo": queries.sli,
                        "long_window": tier.long_window,
                        **_remediation_labels(spec, queries, tier),
                    },
                    annotations={
                        "summary": f"{spec.name} is burning its {queries.sli} error budget "
                        f"{slo.fmt(factor)}x faster than sustainable",
                        "description": f"SLO: {_describe(spec, queries)} over {spec.slo.window}. "
                        f"The error ratio over the last {tier.long_window} and "
                        f"{tier.short_window} is above {slo.fmt(factor)} times the budget; "
                        f"at this rate {slo.fmt(tier.budget_fraction * 100)}% of the "
                        f"{spec.slo.window} budget is spent within {tier.long_window}.",
                        **runbook,
                    },
                )
            )

    sel = service_selector(spec)
    rules.alerts.append(
        AlertRule(
            alert="ScrapeTargetMissing",
            expr=f"max by (namespace, job) (up{{{sel}}}) == 0\nor\nabsent(up{{{sel}}})",
            for_="5m",
            labels={**base_labels, "severity": "warning"},
            annotations={
                "summary": f"Prometheus cannot scrape {spec.name}",
                "description": "No healthy scrape target for this service for 5 minutes, so its "
                "SLO rules have no data and its burn-rate alerts cannot fire.",
                **runbook,
            },
        )
    )
    return rules
