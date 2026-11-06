from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from onboardctl.spec import (
    LoadedSpec,
    SpecError,
    duplicate_problems,
    load_spec,
    parse_spec,
    parse_yaml,
    warnings_for,
)
from tests.conftest import EXAMPLES, REPO


def problems(data: dict[str, Any]) -> dict[str, str]:
    with pytest.raises(SpecError) as info:
        parse_spec(data, "test")
    return {p.path: p.message for p in info.value.problems}


@pytest.mark.parametrize(
    "path",
    [
        EXAMPLES / "api-strict.yaml",
        EXAMPLES / "batch-relaxed.yaml",
        *sorted((REPO / "services").glob("*.yaml")),
    ],
    ids=lambda p: p.name,
)
def test_shipped_specs_are_valid_without_warnings(path: Path) -> None:
    loaded = load_spec(path)
    assert loaded.warnings == []


def test_invalid_example_reports_every_problem_with_its_path() -> None:
    with pytest.raises(SpecError) as info:
        load_spec(EXAMPLES / "invalid.yaml")
    paths = [p.path for p in info.value.problems]
    assert paths == [
        "name",
        "owners[0]",
        "metrics.port",
        "metrics.pathh",
        "slo.availability.target",
        "slo.latency.target",
        "alerting.receivers[0].webhookUrl",
    ]
    assert all(p.example or p.path == "metrics.pathh" for p in info.value.problems)


def test_unknown_field_suggests_the_closest_name(data: dict[str, Any]) -> None:
    data["slo"]["availability"] = {"tagret": 99.9}
    found = problems(data)
    assert "did you mean 'target'?" in found["slo.availability.tagret"]
    assert "required field is missing" in found["slo.availability.target"]


def test_port_number_is_rejected_with_an_explanation(data: dict[str, Any]) -> None:
    data["metrics"]["port"] = 8080
    assert "by name" in problems(data)["metrics.port"]


@pytest.mark.parametrize("target", [0, 100, 150, -1])
def test_target_must_be_strictly_between_0_and_100(data: dict[str, Any], target: float) -> None:
    data["slo"]["availability"]["target"] = target
    assert "greater than 0 and less than 100" in problems(data)["slo.availability.target"]


@pytest.mark.parametrize(
    ("name", "ok"),
    [("checkout-api", True), ("a", True), ("Checkout", False), ("-api", False), ("x" * 41, False)],
)
def test_service_name_rules(data: dict[str, Any], name: str, ok: bool) -> None:
    data["name"] = name
    if ok:
        assert parse_spec(data, "test").name == name
    else:
        assert "name" in problems(data)


def test_namespace_defaults_to_name_and_reserved_names_are_refused(data: dict[str, Any]) -> None:
    assert parse_spec(data, "test").target_namespace == "demo-api"
    data["namespace"] = "kube-system"
    assert "reserved" in problems(data)["namespace"]
    del data["namespace"]
    data["name"] = "monitoring"
    assert "defaults to the service name" in problems(data)["name"]


def test_routes_must_reference_defined_receivers(data: dict[str, Any]) -> None:
    data["alerting"]["defaultReceiver"] = "nobody"
    data["alerting"]["routes"] = [
        {"severity": "critical", "receiver": "pager"},
        {"severity": "critical", "receiver": "demo-tickets"},
    ]
    found = problems(data)
    assert "'nobody' is not defined" in found["alerting.defaultReceiver"]
    assert "'pager' is not defined" in found["alerting.routes[0].receiver"]
    assert "more than once" in found["alerting.routes[1].severity"]


def test_error_selector_must_be_label_matchers(data: dict[str, Any]) -> None:
    data["slo"]["metrics"] = {"errorSelector": "code=5xx"}
    assert "label matchers" in problems(data)["slo.metrics.errorSelector"]
    data["slo"]["metrics"] = {"errorSelector": 'code=~"5..", path!="/healthz"'}
    parse_spec(data, "test")


def test_histogram_base_name_without_suffix(data: dict[str, Any]) -> None:
    data["slo"]["metrics"] = {"latencyHistogram": "http_request_duration_seconds_bucket"}
    assert "base name" in problems(data)["slo.metrics.latencyHistogram"]


def test_numeric_quota_values_become_quantities(data: dict[str, Any]) -> None:
    data["quota"] = {"cpu": 2, "memory": "512Mi"}
    assert parse_spec(data, "test").quota.cpu == "2"
    data["quota"] = {"memory": "lots"}
    assert "quantity" in problems(data)["quota.memory"]


def test_duplicate_yaml_keys_are_an_error() -> None:
    with pytest.raises(SpecError) as info:
        parse_yaml("name: a\nteam: b\nname: c\n", "test")
    assert "line 3" in info.value.problems[0].message
    assert "duplicate key 'name'" in info.value.problems[0].message


def test_yaml_must_be_a_mapping() -> None:
    with pytest.raises(SpecError):
        parse_yaml("- just\n- a list\n", "test")


def test_unreachable_burn_rate_tier_is_a_warning(data: dict[str, Any]) -> None:
    data["slo"]["availability"]["target"] = 90
    warnings = warnings_for(parse_spec(data, "test"))
    assert any("1h/5m burn-rate alert" in w and "can never happen" in w for w in warnings)


def test_receiver_without_webhook_is_a_warning(data: dict[str, Any]) -> None:
    del data["alerting"]["receivers"][0]["webhookUrl"]
    warnings = warnings_for(parse_spec(data, "test"))
    assert any("not delivered" in w for w in warnings)


def test_spec_hash_is_stable_and_tracks_content(data: dict[str, Any]) -> None:
    first = parse_spec(data, "test").spec_hash()
    assert parse_spec(data, "test").spec_hash() == first
    data["slo"]["availability"]["target"] = 99.5
    assert parse_spec(data, "test").spec_hash() != first


def test_duplicate_names_across_specs(data: dict[str, Any]) -> None:
    one = LoadedSpec(Path("a.yaml"), parse_spec(data, "a"))
    other_data = dict(data, namespace="elsewhere")
    two = LoadedSpec(Path("b.yaml"), parse_spec(other_data, "b"))
    messages = duplicate_problems([one, two])
    assert len(messages) == 1
    assert "service name 'demo-api' is already used" in messages[0]


def test_responder_receiver_name_is_reserved(data: dict[str, Any]) -> None:
    data["alerting"]["receivers"].append({"name": "onboardctl-responder"})
    assert "reserved" in problems(data)["alerting.receivers[1].name"]
