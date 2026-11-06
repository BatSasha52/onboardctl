"""Rendering tests, including golden files.

The golden files under tests/golden/ are the exact expected output for each
example spec. After an intended change to the templates, regenerate them with

    UPDATE_GOLDEN=1 pytest tests/test_render.py

and review the diff like any other code change.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

import pytest
import yaml

from onboardctl import labels as lbl
from onboardctl.dashboard import build_dashboard
from onboardctl.render import prometheus_rule_file, render_spec, write_manifests, yaml_str
from onboardctl.spec import ServiceSpec, load_spec, parse_spec
from tests.conftest import EXAMPLES, REPO

GOLDEN = REPO / "tests" / "golden"
VALID_EXAMPLES = ["api-strict", "batch-relaxed"]


@pytest.mark.parametrize("example", VALID_EXAMPLES)
def test_rendered_output_matches_golden_files(example: str, tmp_path: Path) -> None:
    manifests = render_spec(load_spec(EXAMPLES / f"{example}.yaml").spec)
    expected_dir = GOLDEN / example
    if os.environ.get("UPDATE_GOLDEN"):
        write_manifests(manifests, expected_dir)
    written = write_manifests(manifests, tmp_path)
    assert sorted(p.name for p in written) == sorted(p.name for p in expected_dir.glob("*.yaml"))
    for path in written:
        expected = (expected_dir / path.name).read_bytes()
        assert path.read_bytes() == expected, f"{example}/{path.name} differs from golden file"


def test_rendering_is_deterministic(spec: ServiceSpec) -> None:
    assert render_spec(spec) == render_spec(spec)


@pytest.mark.parametrize("example", VALID_EXAMPLES)
def test_every_object_is_labeled_and_parses(example: str) -> None:
    loaded = load_spec(EXAMPLES / f"{example}.yaml").spec
    for manifest in render_spec(loaded):
        obj = yaml.safe_load(manifest.text)
        meta = obj["metadata"]
        assert meta["labels"][lbl.MANAGED_BY] == "onboardctl"
        assert meta["labels"][lbl.SERVICE] == loaded.name
        assert meta["annotations"][lbl.SPEC_HASH] == loaded.spec_hash()
        if obj["kind"] != "Namespace":
            assert meta["namespace"] == loaded.target_namespace


def _objects(spec: ServiceSpec) -> dict[str, dict[str, Any]]:
    return {m.kind: yaml.safe_load(m.text) for m in render_spec(spec)}


def test_dashboard_toggle(data: dict[str, Any]) -> None:
    assert "ConfigMap" in _objects(parse_spec(data, "t"))
    data["dashboard"] = {"enabled": False}
    assert "ConfigMap" not in _objects(parse_spec(data, "t"))


def test_burn_rate_thresholds_come_from_the_target(data: dict[str, Any]) -> None:
    data["slo"]["availability"]["target"] = 99.5
    rules = _objects(parse_spec(data, "t"))["PrometheusRule"]["spec"]["groups"][1]["rules"]
    burn = [r for r in rules if r["alert"] == "ErrorBudgetBurn"]
    assert [r["labels"]["long_window"] for r in burn] == ["1h", "6h", "3d"]
    assert [r["labels"]["severity"] for r in burn] == ["critical", "critical", "warning"]
    assert "> (14.4 * 0.005)" in burn[0]["expr"]
    assert "> (6 * 0.005)" in burn[1]["expr"]


def test_latency_rules_use_the_threshold_as_bucket(data: dict[str, Any]) -> None:
    data["slo"]["latency"] = {"thresholdSeconds": 1.0, "target": 99}
    rules = _objects(parse_spec(data, "t"))["PrometheusRule"]["spec"]["groups"][0]["rules"]
    latency = [r for r in rules if r["record"].startswith("slo:latency")]
    assert len(latency) == 5
    # Formatted like the Go client: "1", not "1.0".
    assert 'le="1"' in latency[0]["expr"]


def test_service_monitor_relabels_job_to_service_name(spec: ServiceSpec) -> None:
    endpoint = _objects(spec)["ServiceMonitor"]["spec"]["endpoints"][0]
    assert endpoint["relabelings"] == [
        {"action": "replace", "targetLabel": "job", "replacement": "demo-api"}
    ]


def test_routes_and_receivers(data: dict[str, Any]) -> None:
    data["alerting"]["receivers"].append({"name": "demo-pager"})
    data["alerting"]["routes"] = [{"severity": "critical", "receiver": "demo-pager"}]
    config = _objects(parse_spec(data, "t"))["AlertmanagerConfig"]["spec"]
    assert config["route"]["receiver"] == "demo-tickets"
    assert config["route"]["routes"][0]["receiver"] == "demo-pager"
    assert [r["name"] for r in config["receivers"]] == ["demo-tickets", "demo-pager"]
    assert "webhookConfigs" not in config["receivers"][1]


def test_dashboard_json(spec: ServiceSpec) -> None:
    config_map = _objects(spec)["ConfigMap"]
    dashboard = json.loads(config_map["data"]["demo-api-slo.json"])
    assert dashboard == build_dashboard(spec)
    assert len(dashboard["uid"]) <= 40
    assert config_map["metadata"]["labels"]["grafana_dashboard"] == "1"
    ids = [p["id"] for p in dashboard["panels"]]
    assert len(ids) == len(set(ids))


def test_prometheus_rule_file_has_only_groups(spec: ServiceSpec) -> None:
    data = yaml.safe_load(prometheus_rule_file(render_spec(spec)))
    assert list(data) == ["groups"]
    assert [g["name"] for g in data["groups"]] == [
        "onboardctl.demo-api.recording",
        "onboardctl.demo-api.alerts",
    ]


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("plain", "plain"),
        ("1h", '"1h"'),
        ("yes", '"yes"'),
        ("a b", '"a b"'),
        ("@org/t", '"@org/t"'),
    ],
)
def test_yaml_str_quotes_only_when_needed(value: str, expected: str) -> None:
    assert yaml_str(value) == expected
    assert yaml.safe_load(f"k: {yaml_str(value)}") == {"k": value}


def test_remediation_adds_scoped_rbac_and_a_responder_route(data: dict[str, Any]) -> None:
    assert "Role" not in _objects(parse_spec(data, "t"))
    data["remediation"] = {"restartDeployment": "demo-api"}
    objects = _objects(parse_spec(data, "t"))
    rule = objects["Role"]["rules"][0]
    assert rule["resourceNames"] == ["demo-api"]
    assert rule["verbs"] == ["get", "patch"]
    subject = objects["RoleBinding"]["subjects"][0]
    assert (subject["name"], subject["namespace"]) == ("onboardctl-responder", "onboardctl-system")
    route = objects["AlertmanagerConfig"]["spec"]["route"]["routes"][0]
    assert route["receiver"] == "onboardctl-responder"
    assert route["continue"] is True
    alerts = objects["PrometheusRule"]["spec"]["groups"][1]["rules"]
    labeled = [a for a in alerts if a["labels"].get("remediation") == "rollout-restart"]
    # Only the fast availability page, never the slower or latency tiers.
    assert [(a["labels"]["slo"], a["labels"]["long_window"]) for a in labeled] == [
        ("availability", "1h")
    ]
    assert labeled[0]["labels"]["deployment"] == "demo-api"
