"""Cluster operations: diff, apply, list and offboard onboarded services."""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from typing import Any

from onboardctl import labels as lbl
from onboardctl.kube import MANAGED_RESOURCES, Kubectl, KubectlError, ref
from onboardctl.render import Manifest, join_manifests

REQUIRED_CRDS = {"servicemonitors", "prometheusrules", "alertmanagerconfigs"}

# Objects that mean a team still runs something in the namespace.
WORKLOAD_RESOURCES = "deployments,statefulsets,daemonsets,jobs,cronjobs,pods"


class ClusterError(RuntimeError):
    pass


def preflight(kubectl: Kubectl) -> None:
    """Fail early, with a hint, if the cluster lacks the Prometheus Operator CRDs."""
    try:
        found = {name.split(".")[0] for name in kubectl.api_resources("monitoring.coreos.com")}
    except KubectlError as exc:
        raise ClusterError(f"cannot reach the cluster: {exc}") from exc
    missing = REQUIRED_CRDS - found
    if missing:
        raise ClusterError(
            f"the cluster has no {', '.join(sorted(missing))} resources. Is the Prometheus "
            "Operator (kube-prometheus-stack) installed? For a local cluster run "
            "scripts/dev-cluster.sh up"
        )


@dataclass(frozen=True)
class DiffResult:
    changed: bool
    text: str
    namespace_exists: bool


def diff(kubectl: Kubectl, manifests: list[Manifest]) -> DiffResult:
    namespace = next(m for m in manifests if m.kind == "Namespace").name
    if not kubectl.exists("namespace", namespace):
        # A server-side diff of objects in a namespace that does not exist
        # yet fails, so describe the creation instead.
        lines = [f"namespace {namespace} does not exist yet; apply would create:"]
        lines += [f"  + {m.kind}/{m.name}" for m in manifests]
        return DiffResult(True, "\n".join(lines) + "\n", namespace_exists=False)
    text = kubectl.diff(join_manifests(manifests))
    return DiffResult(text is not None, text or "", namespace_exists=True)


@dataclass(frozen=True)
class ApplyResult:
    changed: bool
    output: str


def apply(
    kubectl: Kubectl, manifests: list[Manifest], dry_run: bool = False, force: bool = False
) -> ApplyResult:
    """Apply only when something differs, so a re-run touches nothing."""
    current = diff(kubectl, manifests)
    if not current.changed and not force:
        return ApplyResult(False, "")
    if dry_run:
        if not current.namespace_exists:
            return ApplyResult(True, current.text)
        return ApplyResult(True, current.text + kubectl.apply(join_manifests(manifests), True))
    return ApplyResult(True, kubectl.apply(join_manifests(manifests)))


def managed_objects(kubectl: Kubectl, service: str | None = None) -> list[dict[str, Any]]:
    selector = lbl.service_selector(service) if service else lbl.MANAGED_SELECTOR
    return kubectl.get_items(MANAGED_RESOURCES, selector=selector)


@dataclass(frozen=True)
class OnboardedService:
    name: str
    namespace: str
    team: str
    owners: str
    objects: int
    spec_hashes: frozenset[str]


def onboarded_services(objects: list[dict[str, Any]]) -> list[OnboardedService]:
    groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for obj in objects:
        groups[obj["metadata"]["labels"][lbl.SERVICE]].append(obj)
    services = []
    for name, objs in sorted(groups.items()):
        ns_obj = next((o for o in objs if o["kind"] == "Namespace"), None)
        namespace = ns_obj["metadata"]["name"] if ns_obj else objs[0]["metadata"].get("namespace")
        annotations = (ns_obj or objs[0])["metadata"].get("annotations", {})
        services.append(
            OnboardedService(
                name=name,
                namespace=namespace or "",
                team=objs[0]["metadata"]["labels"].get(lbl.TEAM, ""),
                owners=annotations.get(lbl.OWNERS, ""),
                objects=len(objs),
                spec_hashes=frozenset(
                    o["metadata"].get("annotations", {}).get(lbl.SPEC_HASH, "") for o in objs
                ),
            )
        )
    return services


@dataclass(frozen=True)
class OffboardResult:
    deleted: list[str]
    namespace: str | None
    namespace_deleted: bool
    remaining_workloads: list[str]


def offboard(kubectl: Kubectl, service: str, delete_namespace: bool = False) -> OffboardResult:
    objects = managed_objects(kubectl, service)
    if not objects:
        raise ClusterError(f"no objects labeled {lbl.service_selector(service)} found")
    namespace_obj = next((o for o in objects if o["kind"] == "Namespace"), None)
    deleted = []
    by_namespace: dict[str, list[str]] = defaultdict(list)
    for obj in objects:
        if obj["kind"] != "Namespace":
            by_namespace[obj["metadata"]["namespace"]].append(ref(obj))
    for namespace, refs in sorted(by_namespace.items()):
        kubectl.delete(refs, namespace=namespace)
        deleted += [f"{namespace}/{r}" for r in refs]

    if namespace_obj is None:
        return OffboardResult(deleted, None, False, [])
    namespace = namespace_obj["metadata"]["name"]
    # Deleting the namespace deletes everything in it. Only do that on
    # request when the team still has workloads there.
    workloads = [
        f"{o['kind']}/{o['metadata']['name']}"
        for o in kubectl.get_items(WORKLOAD_RESOURCES, namespace=namespace)
    ]
    if workloads and not delete_namespace:
        # Keep the namespace but stop managing it, so that `list` and
        # `backup` no longer treat the service as onboarded.
        kubectl.run(
            ["label", "namespace", namespace]
            + [f"{key}-" for key in (lbl.MANAGED_BY, lbl.SERVICE, lbl.TEAM)]
        )
        kubectl.run(
            ["annotate", "namespace", namespace]
            + [f"{key}-" for key in (lbl.SPEC_HASH, lbl.OWNERS, lbl.DESCRIPTION)]
        )
        return OffboardResult(deleted, namespace, False, workloads)
    kubectl.delete([f"namespace/{namespace}"])
    deleted.append(f"namespace/{namespace}")
    return OffboardResult(deleted, namespace, True, workloads)
