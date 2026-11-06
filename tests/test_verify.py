from __future__ import annotations

from typing import Any

from onboardctl.render import render_spec
from onboardctl.spec import ServiceSpec
from onboardctl.verify import (
    Expectation,
    ServiceReport,
    expectations_from_manifests,
    expectations_from_objects,
    format_reports,
    run_checks,
    verify,
)
from tests.fakes import (
    FakeAlertmanager,
    FakeKubectl,
    FakePrometheus,
    healthy_prometheus,
    live_objects,
)

RECEIVERS = {"demo-api/demo-api-routing/demo-tickets"}


def _checks(
    spec: ServiceSpec, prom: FakePrometheus, receivers: set[str] = RECEIVERS
) -> ServiceReport:
    kubectl = FakeKubectl(live_objects(spec))
    expectations = expectations_from_manifests(render_spec(spec))
    [report] = run_checks(kubectl, prom, FakeAlertmanager(receivers), expectations)
    return report


def _status(report: ServiceReport, prefix: str) -> tuple[str, str]:
    check = next(c for c in report.checks if c.name.startswith(prefix))
    return check.status, check.detail


def test_expectations_from_rendered_manifests(spec: ServiceSpec) -> None:
    [exp] = expectations_from_manifests(render_spec(spec))
    assert exp == Expectation(
        service="demo-api",
        namespace="demo-api",
        objects=(
            ("Namespace", "demo-api"),
            ("ResourceQuota", "demo-api-quota"),
            ("ServiceMonitor", "demo-api"),
            ("PrometheusRule", "demo-api-slo"),
            ("AlertmanagerConfig", "demo-api-routing"),
            ("ConfigMap", "demo-api-slo-dashboard"),
        ),
        rule_groups=(("onboardctl.demo-api.recording", 5), ("onboardctl.demo-api.alerts", 4)),
        receivers=("demo-api/demo-api-routing/demo-tickets",),
        sli_series=('slo:availability_error_ratio:rate5m{namespace="demo-api", job="demo-api"}',),
    )


def test_expectations_are_the_same_from_live_objects(spec: ServiceSpec) -> None:
    assert expectations_from_objects(live_objects(spec)) == expectations_from_manifests(
        render_spec(spec)
    )


def test_healthy_service_passes(spec: ServiceSpec) -> None:
    report = _checks(spec, healthy_prometheus(spec))
    assert report.ok
    assert all(c.status == "pass" for c in report.checks)
    assert "result: PASS" in format_reports([report])


def test_missing_object_fails(spec: ServiceSpec) -> None:
    kubectl = FakeKubectl([o for o in live_objects(spec) if o["kind"] != "ServiceMonitor"])
    expectations = expectations_from_manifests(render_spec(spec))
    [report] = run_checks(
        kubectl, healthy_prometheus(spec), FakeAlertmanager(RECEIVERS), expectations
    )
    assert _status(report, "ServiceMonitor") == ("fail", "not found")


def test_target_down_reports_the_scrape_error(spec: ServiceSpec) -> None:
    prom = healthy_prometheus(spec)
    prom.targets[0].update(health="down", lastError="connection refused")
    status, detail = _status(_checks(spec, prom), "Prometheus scrapes")
    assert status == "fail"
    assert "connection refused" in detail


def test_no_target_explains_what_to_check(spec: ServiceSpec) -> None:
    prom = healthy_prometheus(spec)
    prom.targets = []
    status, detail = _status(_checks(spec, prom), "Prometheus scrapes")
    assert status == "fail"
    assert "selector labels" in detail


def test_rules_from_another_namespace_do_not_count(spec: ServiceSpec) -> None:
    prom = healthy_prometheus(spec)
    for group in prom.groups:
        group["file"] = "/etc/prometheus/rules/x/other-ns-demo-api-slo-uid.yaml"
    status, _ = _status(_checks(spec, prom), "rule group onboardctl.demo-api.recording")
    assert status == "fail"


def test_unevaluated_rules_fail(spec: ServiceSpec) -> None:
    prom = healthy_prometheus(spec)
    prom.groups[1]["rules"] = [{"health": "unknown"}] * 4
    status, detail = _status(_checks(spec, prom), "rule group onboardctl.demo-api.alerts")
    assert (status, detail) == ("fail", "4 rules not healthy: unknown")


def test_missing_receiver_fails(spec: ServiceSpec) -> None:
    status, detail = _status(_checks(spec, healthy_prometheus(spec), set()), "Alertmanager has")
    assert status == "fail"
    assert "demo-api/demo-api-routing/demo-tickets" in detail


def test_sli_without_traffic_is_only_a_warning(spec: ServiceSpec) -> None:
    prom = healthy_prometheus(spec)
    prom.series = {"": [{"value": [0, "NaN"]}]}
    report = _checks(spec, prom)
    assert report.ok
    assert _status(report, "SLI has data") == ("warn", "no requests in the last 5 minutes")


def test_unreachable_prometheus_is_a_failure_not_a_crash(spec: ServiceSpec) -> None:
    prom = healthy_prometheus(spec)
    prom.fail = True
    report = _checks(spec, prom)
    assert not report.ok
    assert _status(report, "Prometheus scrapes") == ("fail", "connection refused")


def test_verify_retries_until_healthy(spec: ServiceSpec) -> None:
    prom = healthy_prometheus(spec)
    healthy_groups = prom.groups
    prom.groups = []
    attempts: list[Any] = []

    def on_retry(reports: list[ServiceReport], elapsed: float) -> None:
        attempts.append(reports)
        prom.groups = healthy_groups

    kubectl = FakeKubectl(live_objects(spec))
    expectations = expectations_from_manifests(render_spec(spec))
    reports = verify(
        kubectl,
        prom,
        FakeAlertmanager(RECEIVERS),
        expectations,
        timeout=10,
        interval=0,
        on_retry=on_retry,
    )
    assert len(attempts) == 1
    assert all(r.ok for r in reports)
