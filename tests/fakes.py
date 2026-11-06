"""Stand-ins for kubectl, Prometheus and Alertmanager in unit tests."""

from __future__ import annotations

import copy
from typing import Any

from onboardctl import yamlutil
from onboardctl.prom import ApiError
from onboardctl.render import render_spec
from onboardctl.spec import ServiceSpec


def live_objects(spec: ServiceSpec) -> list[dict[str, Any]]:
    """Rendered objects with the kind of fields the API server adds."""
    objects = []
    for index, manifest in enumerate(render_spec(spec)):
        obj = yamlutil.load_all(manifest.text)[0]
        obj["metadata"].update(
            uid=f"uid-{index}",
            resourceVersion=str(1000 + index),
            creationTimestamp="2026-01-01T00:00:00Z",
            managedFields=[{"manager": "onboardctl"}],
        )
        obj["status"] = {}
        objects.append(obj)
    return objects


class FakeKubectl:
    def __init__(self, items: list[dict[str, Any]], context: str = "kind-test") -> None:
        self.items = items
        self.context = context
        self.applied: list[str] = []
        self.deleted: list[tuple[list[str], str | None]] = []

    def get_items(
        self, resources: str, selector: str | None = None, namespace: str | None = None
    ) -> list[dict[str, Any]]:
        return copy.deepcopy(self.items)

    def current_context(self) -> str:
        return self.context

    def apply(self, manifests: str, dry_run: bool = False) -> str:
        self.applied.append(manifests)
        docs = yamlutil.load_all(manifests)
        return "".join(
            f"{d['kind'].lower()}/{d['metadata']['name']} serverside-applied\n" for d in docs
        )

    def delete(self, refs: list[str], namespace: str | None = None, wait: bool = True) -> str:
        self.deleted.append((refs, namespace))
        return ""


def healthy_prometheus(spec: ServiceSpec) -> FakePrometheus:
    namespace, name = spec.target_namespace, spec.name
    recording = 10 if spec.slo.latency else 5
    alerts = 7 if spec.slo.latency else 4
    rule_file = f"/etc/prometheus/rules/prometheus-kps-rulefiles-0/{namespace}-{name}-slo-uid.yaml"
    return FakePrometheus(
        targets=[{"labels": {"namespace": namespace, "job": name}, "health": "up"}],
        groups=[
            {
                "name": f"onboardctl.{name}.recording",
                "file": rule_file,
                "rules": [{"health": "ok"}] * recording,
            },
            {
                "name": f"onboardctl.{name}.alerts",
                "file": rule_file,
                "rules": [{"health": "ok"}] * alerts,
            },
        ],
        series={"": [{"value": [0, "0.001"]}]},
    )


class FakePrometheus:
    def __init__(
        self,
        targets: list[dict[str, Any]],
        groups: list[dict[str, Any]],
        series: dict[str, list[dict[str, Any]]] | None = None,
        fail: bool = False,
    ) -> None:
        self.targets = targets
        self.groups = groups
        self.series = series or {}
        self.fail = fail

    def _check(self) -> None:
        if self.fail:
            raise ApiError("connection refused")

    def active_targets(self) -> list[dict[str, Any]]:
        self._check()
        return self.targets

    def rule_groups(self) -> list[dict[str, Any]]:
        self._check()
        return self.groups

    def query(self, promql: str) -> list[dict[str, Any]]:
        self._check()
        return self.series.get(promql, self.series.get("", []))


class FakeAlertmanager:
    def __init__(self, receivers: set[str]) -> None:
        self._receivers = receivers

    def receivers(self) -> set[str]:
        return self._receivers
