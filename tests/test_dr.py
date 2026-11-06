from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from onboardctl import dr, yamlutil
from onboardctl.spec import ServiceSpec, parse_spec
from tests.fakes import FakeAlertmanager, FakeKubectl, healthy_prometheus, live_objects


def test_clean_keeps_only_what_recreates_the_object(spec: ServiceSpec) -> None:
    obj = live_objects(spec)[2]  # ServiceMonitor
    obj["metadata"]["annotations"]["kubectl.kubernetes.io/last-applied-configuration"] = "{}"
    cleaned = dr.clean(obj)
    assert set(cleaned) == {"apiVersion", "kind", "metadata", "spec"}
    assert set(cleaned["metadata"]) == {"name", "namespace", "labels", "annotations"}
    assert (
        "kubectl.kubernetes.io/last-applied-configuration" not in cleaned["metadata"]["annotations"]
    )


def test_clean_drops_namespace_spec_and_automatic_label(spec: ServiceSpec) -> None:
    namespace = live_objects(spec)[0]
    namespace["spec"] = {"finalizers": ["kubernetes"]}
    namespace["metadata"]["labels"]["kubernetes.io/metadata.name"] = "demo-api"
    cleaned = dr.clean(namespace)
    assert "spec" not in cleaned
    assert "kubernetes.io/metadata.name" not in cleaned["metadata"]["labels"]


def test_backup_round_trip(spec: ServiceSpec, tmp_path: Path) -> None:
    kubectl = FakeKubectl(live_objects(spec))
    result = dr.backup(kubectl, tmp_path / "b")
    assert result.objects == 6
    assert result.services == ["demo-api"]
    index, objects = dr.read_backup(tmp_path / "b")
    assert index["context"] == "kind-test"
    kinds = [o["kind"] for o in objects]
    assert kinds == [k for k in dr.APPLY_ORDER if k in kinds]
    expected = {o["kind"]: dr.clean(o) for o in live_objects(spec)}
    assert objects == [expected[o["kind"]] for o in objects]


def test_backup_refuses_a_non_empty_directory(spec: ServiceSpec, tmp_path: Path) -> None:
    (tmp_path / "old.txt").write_text("x")
    with pytest.raises(dr.BackupError, match="not empty"):
        dr.backup(FakeKubectl(live_objects(spec)), tmp_path)


def test_backup_with_nothing_onboarded_is_an_error(tmp_path: Path) -> None:
    with pytest.raises(dr.BackupError, match="nothing to back up"):
        dr.backup(FakeKubectl([]), tmp_path / "b")


def test_tampered_file_is_detected(spec: ServiceSpec, tmp_path: Path) -> None:
    dr.backup(FakeKubectl(live_objects(spec)), tmp_path)
    rule = next(tmp_path.glob("demo-api/prometheusrule-*.yaml"))
    rule.write_text(rule.read_text().replace("14.4", "144"))
    with pytest.raises(dr.BackupError, match="checksum"):
        dr.read_backup(tmp_path)


def test_incomplete_backup_is_refused(spec: ServiceSpec, tmp_path: Path) -> None:
    dr.backup(FakeKubectl(live_objects(spec)), tmp_path)
    (tmp_path / dr.INDEX_FILE).unlink()
    with pytest.raises(dr.BackupError, match="not a complete"):
        dr.read_backup(tmp_path)
    dr.backup(FakeKubectl(live_objects(spec)), tmp_path / "again")
    next((tmp_path / "again").glob("demo-api/servicemonitor-*.yaml")).unlink()
    with pytest.raises(dr.BackupError, match="missing"):
        dr.read_backup(tmp_path / "again")


def test_restore_applies_namespaces_first(spec: ServiceSpec, tmp_path: Path) -> None:
    dr.backup(FakeKubectl(live_objects(spec)), tmp_path)
    target = FakeKubectl([])
    output = dr.restore(target, tmp_path)
    assert len(target.applied) == 2
    assert [d["kind"] for d in yamlutil.load_all(target.applied[0])] == ["Namespace"]
    assert len(yamlutil.load_all(target.applied[1])) == 5
    assert len(output.splitlines()) == 6


def test_backup_index_is_stable_json(spec: ServiceSpec, tmp_path: Path) -> None:
    dr.backup(FakeKubectl(live_objects(spec)), tmp_path)
    index = json.loads((tmp_path / dr.INDEX_FILE).read_text())
    assert [e["file"] for e in index["objects"]][0] == "demo-api/namespace-demo-api.yaml"


def test_drill_aborts_when_the_baseline_is_unhealthy(spec: ServiceSpec, tmp_path: Path) -> None:
    prom = healthy_prometheus(spec)
    prom.targets = []
    result = dr.run_drill(
        FakeKubectl(live_objects(spec)),
        prom,
        FakeAlertmanager(set()),
        tmp_path / "b",
        timeout=0,
        log=lambda _: None,
    )
    assert result.outcome == "ABORTED"
    assert "baseline" in result.reason
    assert not (tmp_path / "b").exists()
    report = dr.drill_report(result)
    assert "Outcome: **ABORTED**" in report
    assert "Not measured" in report


def test_backup_keeps_rbac_content(data: dict[str, Any], tmp_path: Path) -> None:
    # Role and RoleBinding keep their content outside `spec`; losing it made
    # a real drill fail with "unsupported role reference kind".
    data["remediation"] = {"restartDeployment": "demo-api"}
    spec = parse_spec(data, "t")
    dr.backup(FakeKubectl(live_objects(spec)), tmp_path)
    _, objects = dr.read_backup(tmp_path)
    by_kind = {o["kind"]: o for o in objects}
    assert by_kind["Role"]["rules"][0]["resourceNames"] == ["demo-api"]
    assert by_kind["RoleBinding"]["roleRef"]["kind"] == "Role"
    assert by_kind["RoleBinding"]["subjects"][0]["name"] == "onboardctl-responder"
    assert all("status" not in o for o in objects)
