"""Check that an onboarded service actually works, not only that kubectl accepted it.

What is expected is derived from manifests (rendered from a spec, read from
a backup, or read back from the cluster), so `verify <spec>` and the DR drill
share the same checks.
"""

from __future__ import annotations

import math
import time
from collections import defaultdict
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, Literal

from onboardctl import labels as lbl
from onboardctl import yamlutil
from onboardctl.cluster import managed_objects
from onboardctl.kube import RESOURCE_FOR_KIND, Kubectl, KubectlError
from onboardctl.prom import AlertmanagerApi, ApiError, PrometheusApi
from onboardctl.render import Manifest

Status = Literal["pass", "fail", "warn"]
KIND_ORDER = list(RESOURCE_FOR_KIND)


@dataclass(frozen=True)
class Expectation:
    service: str
    namespace: str
    objects: tuple[tuple[str, str], ...]  # (kind, name)
    rule_groups: tuple[tuple[str, int], ...]  # (group name, number of rules)
    receivers: tuple[str, ...]  # as Alertmanager names them: namespace/config/receiver
    sli_series: tuple[str, ...]  # recorded SLIs that should have data once traffic flows


@dataclass(frozen=True)
class Check:
    name: str
    status: Status
    detail: str = ""


@dataclass
class ServiceReport:
    service: str
    namespace: str
    checks: list[Check] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return all(c.status != "fail" for c in self.checks)


def expectations_from_objects(objects: list[dict[str, Any]]) -> list[Expectation]:
    by_service: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for obj in objects:
        by_service[obj["metadata"]["labels"][lbl.SERVICE]].append(obj)

    result = []
    for service, objs in sorted(by_service.items()):
        ns_obj = next((o for o in objs if o["kind"] == "Namespace"), None)
        namespace = ns_obj["metadata"]["name"] if ns_obj else objs[0]["metadata"]["namespace"]
        groups: list[tuple[str, int]] = []
        receivers: list[str] = []
        series: list[str] = []
        for obj in objs:
            if obj["kind"] == "PrometheusRule":
                for group in obj["spec"]["groups"]:
                    groups.append((group["name"], len(group["rules"])))
                    series += [
                        f'{r["record"]}{{namespace="{namespace}", job="{service}"}}'
                        for r in group["rules"]
                        if r.get("record", "").endswith(":rate5m")
                    ]
            elif obj["kind"] == "AlertmanagerConfig":
                config = obj["metadata"]["name"]
                receivers += [f"{namespace}/{config}/{r['name']}" for r in obj["spec"]["receivers"]]
        result.append(
            Expectation(
                service=service,
                namespace=namespace,
                objects=tuple(
                    sorted(
                        ((o["kind"], o["metadata"]["name"]) for o in objs),
                        key=lambda item: (KIND_ORDER.index(item[0]), item[1]),
                    )
                ),
                rule_groups=tuple(groups),
                receivers=tuple(receivers),
                sli_series=tuple(series),
            )
        )
    return result


def expectations_from_manifests(manifests: list[Manifest]) -> list[Expectation]:
    objects = [obj for m in manifests for obj in yamlutil.load_all(m.text)]
    return expectations_from_objects(objects)


@dataclass
class _Snapshot:
    """Everything one verification pass needs, fetched once for all services."""

    live: set[tuple[str, str, str]] | None = None  # (namespace, kind, name)
    targets: list[dict[str, Any]] | None = None
    groups: list[dict[str, Any]] | None = None
    receivers: set[str] | None = None
    errors: dict[str, str] = field(default_factory=dict)


def _snapshot(kubectl: Kubectl, prom: PrometheusApi, am: AlertmanagerApi) -> _Snapshot:
    snap = _Snapshot()
    try:
        snap.live = {
            (o["metadata"].get("namespace", ""), o["kind"], o["metadata"]["name"])
            for o in managed_objects(kubectl)
        }
    except KubectlError as exc:
        snap.errors["kubectl"] = str(exc)
    for key, fetch in (
        ("targets", prom.active_targets),
        ("groups", prom.rule_groups),
        ("receivers", am.receivers),
    ):
        try:
            setattr(snap, key, fetch())
        except ApiError as exc:
            snap.errors[key] = str(exc)
    return snap


def _object_checks(exp: Expectation, snap: _Snapshot) -> list[Check]:
    checks = []
    for kind, name in exp.objects:
        label = f"{kind} {name} exists"
        if snap.live is None:
            checks.append(Check(label, "fail", snap.errors["kubectl"]))
            continue
        key = ("" if kind == "Namespace" else exp.namespace, kind, name)
        checks.append(
            Check(label, "pass") if key in snap.live else Check(label, "fail", "not found")
        )
    return checks


def _target_check(exp: Expectation, snap: _Snapshot) -> Check:
    label = "Prometheus scrapes the service"
    if snap.targets is None:
        return Check(label, "fail", snap.errors["targets"])
    mine = [
        t
        for t in snap.targets
        if t["labels"].get("namespace") == exp.namespace and t["labels"].get("job") == exp.service
    ]
    if not mine:
        return Check(
            label,
            "fail",
            f'no active target with job="{exp.service}" in namespace {exp.namespace}; check that '
            "a Service there carries the selector labels and has the named metrics port",
        )
    down = [t for t in mine if t["health"] != "up"]
    if down:
        errors = "; ".join(sorted({t.get("lastError") or t["health"] for t in down}))
        return Check(label, "fail", f"{len(down)}/{len(mine)} targets not up: {errors}")
    return Check(label, "pass", f"{len(mine)}/{len(mine)} targets up")


def _rule_checks(exp: Expectation, snap: _Snapshot) -> list[Check]:
    checks = []
    for name, expected in exp.rule_groups:
        label = f"rule group {name} loaded"
        if snap.groups is None:
            checks.append(Check(label, "fail", snap.errors["groups"]))
            continue
        # The operator writes one rule file per PrometheusRule, named after its namespace.
        group = next(
            (
                g
                for g in snap.groups
                if g["name"] == name and f"/{exp.namespace}-" in g.get("file", "")
            ),
            None,
        )
        if group is None:
            checks.append(Check(label, "fail", "not loaded by Prometheus"))
            continue
        rules = group["rules"]
        unhealthy = [r for r in rules if r.get("health") != "ok"]
        if len(rules) != expected:
            checks.append(Check(label, "fail", f"{len(rules)} rules loaded, expected {expected}"))
        elif unhealthy:
            states = sorted({r.get("lastError") or r.get("health", "unknown") for r in unhealthy})
            checks.append(
                Check(label, "fail", f"{len(unhealthy)} rules not healthy: {'; '.join(states)}")
            )
        else:
            checks.append(Check(label, "pass", f"{expected} rules, all evaluating"))
    return checks


def _receiver_check(exp: Expectation, snap: _Snapshot) -> Check:
    label = "Alertmanager has the routing config"
    if snap.receivers is None:
        return Check(label, "fail", snap.errors["receivers"])
    missing = [r for r in exp.receivers if r not in snap.receivers]
    if missing:
        return Check(label, "fail", "receivers not loaded: " + ", ".join(missing))
    return Check(label, "pass", f"{len(exp.receivers)} receiver(s)")


def _sli_checks(exp: Expectation, prom: PrometheusApi) -> list[Check]:
    # Warnings only: a correctly onboarded service with no traffic has no data.
    checks = []
    for series in exp.sli_series:
        label = f"SLI has data: {series.split('{')[0]}"
        try:
            result = prom.query(series)
        except ApiError as exc:
            checks.append(Check(label, "warn", str(exc)))
            continue
        if not result:
            hint = (
                "; check that slo.latency.thresholdSeconds is one of the histogram's bucket bounds"
                if "latency" in series
                else ""
            )
            checks.append(Check(label, "warn", f"no series yet{hint}"))
        elif math.isnan(float(result[0]["value"][1])):
            checks.append(Check(label, "warn", "no requests in the last 5 minutes"))
        else:
            checks.append(Check(label, "pass", f"error ratio {float(result[0]['value'][1]):.4g}"))
    return checks


def run_checks(
    kubectl: Kubectl,
    prom: PrometheusApi,
    am: AlertmanagerApi,
    expectations: list[Expectation],
    include_sli: bool = True,
) -> list[ServiceReport]:
    snap = _snapshot(kubectl, prom, am)
    reports = []
    for exp in expectations:
        report = ServiceReport(exp.service, exp.namespace)
        report.checks += _object_checks(exp, snap)
        report.checks.append(_target_check(exp, snap))
        report.checks += _rule_checks(exp, snap)
        if exp.receivers:
            report.checks.append(_receiver_check(exp, snap))
        if include_sli and report.ok:
            report.checks += _sli_checks(exp, prom)
        reports.append(report)
    return reports


def verify(
    kubectl: Kubectl,
    prom: PrometheusApi,
    am: AlertmanagerApi,
    expectations: list[Expectation],
    timeout: float,
    interval: float = 5,
    on_retry: Callable[[list[ServiceReport], float], None] | None = None,
) -> list[ServiceReport]:
    """Repeat the checks until they all pass or the timeout runs out.

    Retrying is needed because the operator, the config reloaders and the
    first scrape all take time after an apply.
    """
    start = time.monotonic()
    while True:
        reports = run_checks(kubectl, prom, am, expectations)
        elapsed = time.monotonic() - start
        if all(r.ok for r in reports) or elapsed + interval > timeout:
            return reports
        if on_retry:
            on_retry(reports, elapsed)
        time.sleep(interval)


def format_reports(reports: list[ServiceReport]) -> str:
    marks = {"pass": "PASS", "fail": "FAIL", "warn": "WARN"}
    lines = []
    for report in reports:
        lines.append(f"{report.service} (namespace {report.namespace})")
        for check in report.checks:
            detail = f": {check.detail}" if check.detail else ""
            lines.append(f"  [{marks[check.status]}] {check.name}{detail}")
        failed = sum(c.status == "fail" for c in report.checks)
        warned = sum(c.status == "warn" for c in report.checks)
        verdict = "PASS" if report.ok else "FAIL"
        lines.append(
            f"  result: {verdict} ({len(report.checks) - failed - warned} passed, "
            f"{failed} failed, {warned} warnings)"
        )
    return "\n".join(lines)
