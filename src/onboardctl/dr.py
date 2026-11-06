"""Backup, restore and disaster-recovery drills for onboarded configuration.

What is covered is the configuration onboardctl manages (namespaces, quotas,
ServiceMonitors, rules, routing, dashboards). Metric history in the
Prometheus TSDB is not; see "Disaster recovery" in the README.
"""

from __future__ import annotations

import hashlib
import json
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from onboardctl import labels as lbl
from onboardctl import yamlutil
from onboardctl.cluster import managed_objects
from onboardctl.kube import RESOURCE_FOR_KIND, Kubectl, ref
from onboardctl.prom import AlertmanagerApi, ApiError, PrometheusApi
from onboardctl.verify import (
    Expectation,
    ServiceReport,
    expectations_from_objects,
    format_reports,
    verify,
)

INDEX_FILE = "backup.json"
BACKUP_FORMAT = 1

# Namespaces first, since everything else lives in one.
APPLY_ORDER = tuple(RESOURCE_FOR_KIND)


class BackupError(RuntimeError):
    pass


def utc_now() -> datetime:
    return datetime.now(UTC).replace(microsecond=0)


def timestamp(moment: datetime) -> str:
    return moment.strftime("%Y%m%dT%H%M%SZ")


def clean(obj: dict[str, Any]) -> dict[str, Any]:
    """Keep only what is needed to re-create the object.

    Server-populated fields (uid, resourceVersion, managedFields, status...)
    would make every backup differ and some would be rejected on create.
    """
    meta = obj["metadata"]
    labels = {k: v for k, v in meta.get("labels", {}).items() if k != "kubernetes.io/metadata.name"}
    kept_meta: dict[str, Any] = {"name": meta["name"], "labels": labels}
    if "namespace" in meta:
        kept_meta["namespace"] = meta["namespace"]
    annotations = {
        k: v
        for k, v in meta.get("annotations", {}).items()
        if k != "kubectl.kubernetes.io/last-applied-configuration"
    }
    if annotations:
        kept_meta["annotations"] = annotations
    # Keep every other top-level field: kinds keep their content in different
    # places (spec, data, rules, roleRef, subjects). Only status is server-owned.
    result: dict[str, Any] = {
        key: value for key, value in obj.items() if key not in ("metadata", "status")
    }
    result["metadata"] = kept_meta
    # A Namespace's spec only holds the finalizer the API server adds itself.
    if obj["kind"] == "Namespace":
        result.pop("spec", None)
    return result


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


@dataclass(frozen=True)
class BackupResult:
    directory: Path
    objects: int
    services: list[str]


def backup(kubectl: Kubectl, directory: Path) -> BackupResult:
    if directory.exists() and any(directory.iterdir()):
        raise BackupError(f"{directory} is not empty; choose a new directory")
    objects = sorted(
        (clean(o) for o in managed_objects(kubectl)),
        key=lambda o: (o["metadata"]["labels"][lbl.SERVICE], APPLY_ORDER.index(o["kind"])),
    )
    if not objects:
        raise BackupError("no onboarded objects found; nothing to back up")

    entries = []
    for obj in objects:
        service = obj["metadata"]["labels"][lbl.SERVICE]
        relative = f"{service}/{obj['kind'].lower()}-{obj['metadata']['name']}.yaml"
        data = yamlutil.dump(obj).encode()
        path = directory / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        entries.append(
            {
                "file": relative,
                "kind": obj["kind"],
                "name": obj["metadata"]["name"],
                "namespace": obj["metadata"].get("namespace"),
                "service": service,
                "sha256": _sha256(data),
            }
        )
    index = {
        "format": BACKUP_FORMAT,
        "created": utc_now().isoformat(),
        "context": kubectl.current_context(),
        "objects": entries,
    }
    # Written last: a backup interrupted halfway has no index, and restore
    # refuses a directory without one.
    (directory / INDEX_FILE).write_text(
        json.dumps(index, indent=2, sort_keys=True) + "\n", encoding="utf-8", newline="\n"
    )
    services = sorted({e["service"] for e in entries})
    return BackupResult(directory, len(entries), services)


def read_backup(directory: Path) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Load a backup, checking that every file is present and unmodified."""
    index_path = directory / INDEX_FILE
    if not index_path.is_file():
        raise BackupError(f"{index_path} not found; not a complete onboardctl backup")
    index = json.loads(index_path.read_text(encoding="utf-8"))
    if index.get("format") != BACKUP_FORMAT:
        raise BackupError(f"unsupported backup format {index.get('format')!r}")
    objects = []
    for entry in index["objects"]:
        path = directory / entry["file"]
        if not path.is_file():
            raise BackupError(f"{entry['file']} is listed in {INDEX_FILE} but missing")
        data = path.read_bytes()
        if _sha256(data) != entry["sha256"]:
            raise BackupError(f"{entry['file']} does not match its checksum in {INDEX_FILE}")
        objects.append(yamlutil.load_all(data.decode())[0])
    return index, objects


def restore(kubectl: Kubectl, directory: Path, dry_run: bool = False) -> str:
    _, objects = read_backup(directory)
    namespaces = [o for o in objects if o["kind"] == "Namespace"]
    others = sorted(
        (o for o in objects if o["kind"] != "Namespace"),
        key=lambda o: APPLY_ORDER.index(o["kind"]),
    )
    output = ""
    for batch in (namespaces, others):
        if batch:
            output += kubectl.apply("---\n".join(yamlutil.dump(o) for o in batch), dry_run=dry_run)
    return output


# --- drill ------------------------------------------------------------------


@dataclass
class Step:
    name: str
    started: datetime
    seconds: float = 0.0
    ok: bool = True
    detail: str = ""


@dataclass
class DrillResult:
    context: str
    started: datetime
    backup_dir: Path
    finished: datetime | None = None
    services: list[str] = field(default_factory=list)
    objects: int = 0
    steps: list[Step] = field(default_factory=list)
    reports: list[ServiceReport] = field(default_factory=list)
    recovery_seconds: float | None = None
    outage_seconds: float | None = None
    outcome: str = "ABORTED"
    reason: str = ""


class _Abort(Exception):
    pass


@contextmanager
def _step(result: DrillResult, name: str, log: Callable[[str], None]) -> Iterator[Step]:
    step = Step(name, utc_now())
    result.steps.append(step)
    log(f"drill: {name}")
    start = time.monotonic()
    try:
        yield step
    except Exception as exc:
        step.ok = False
        step.detail = step.detail or str(exc)
        raise
    finally:
        step.seconds = time.monotonic() - start


def _config_gone(prom: PrometheusApi, expectations: list[Expectation]) -> bool:
    names = {name for exp in expectations for name, _ in exp.rule_groups}
    jobs = {(exp.namespace, exp.service) for exp in expectations}
    groups_left = any(g["name"] in names for g in prom.rule_groups())
    targets_left = any(
        (t["labels"].get("namespace"), t["labels"].get("job")) in jobs
        for t in prom.active_targets()
    )
    return not groups_left and not targets_left


def run_drill(
    kubectl: Kubectl,
    prom: PrometheusApi,
    am: AlertmanagerApi,
    backup_dir: Path,
    timeout: float,
    log: Callable[[str], None],
) -> DrillResult:
    """Back up, delete, restore and re-verify all onboarded configuration.

    Namespaces are not deleted: that would delete the teams' workloads too,
    which onboardctl neither owns nor backs up.
    """
    result = DrillResult(kubectl.current_context(), utc_now(), backup_dir)
    try:
        _drill(kubectl, prom, am, backup_dir, timeout, log, result)
    except _Abort as exc:
        result.reason = str(exc)
    except Exception as exc:  # recorded in the report rather than lost
        result.outcome = "FAIL"
        result.reason = f"{type(exc).__name__}: {exc}"
    result.finished = utc_now()
    return result


def _drill(
    kubectl: Kubectl,
    prom: PrometheusApi,
    am: AlertmanagerApi,
    backup_dir: Path,
    timeout: float,
    log: Callable[[str], None],
    result: DrillResult,
) -> None:
    with _step(result, "baseline verify", log) as step:
        objects = managed_objects(kubectl)
        if not objects:
            raise _Abort("nothing is onboarded; there is nothing to drill")
        expectations = expectations_from_objects(objects)
        result.services = [e.service for e in expectations]
        baseline = verify(kubectl, prom, am, expectations, timeout=min(timeout, 60))
        if not all(r.ok for r in baseline):
            step.ok = False
            step.detail = "baseline failed"
            result.reports = baseline
            raise _Abort("the baseline verification failed; fix it before drilling")
        step.detail = f"{len(expectations)} services healthy"

    with _step(result, "backup", log) as step:
        backed_up = backup(kubectl, backup_dir)
        result.objects = backed_up.objects
        step.detail = f"{backed_up.objects} objects to {backup_dir.as_posix()}"

    outage_start = time.monotonic()
    with _step(result, "delete onboarded config (namespaces kept)", log) as step:
        by_namespace: dict[str, list[str]] = {}
        for obj in objects:
            if obj["kind"] != "Namespace":
                by_namespace.setdefault(obj["metadata"]["namespace"], []).append(ref(obj))
        for namespace, refs in sorted(by_namespace.items()):
            kubectl.delete(refs, namespace=namespace)
        step.detail = f"{sum(len(r) for r in by_namespace.values())} objects deleted"

    with _step(result, "wait until Prometheus has dropped the config", log) as step:
        deadline = time.monotonic() + timeout
        while True:
            try:
                if _config_gone(prom, expectations):
                    break
            except ApiError:
                pass
            if time.monotonic() > deadline:
                step.ok = False
                step.detail = "rules or targets still present"
                raise _Abort(
                    f"Prometheus still had the deleted config after {timeout:.0f}s, so a "
                    "recovery time would be meaningless"
                )
            time.sleep(5)
        step.detail = "rule groups and scrape targets gone"

    restore_start = time.monotonic()
    with _step(result, "restore from backup", log) as step:
        output = restore(kubectl, backup_dir)
        step.detail = f"{len(output.splitlines())} objects applied"

    with _step(result, "verify after restore", log) as step:
        result.reports = verify(
            kubectl,
            prom,
            am,
            expectations,
            timeout=timeout,
            on_retry=lambda reports, elapsed: log(
                f"drill: waiting for recovery ({elapsed:.0f}s, "
                f"{sum(c.status == 'fail' for r in reports for c in r.checks)} checks failing)"
            ),
        )
        recovered = all(r.ok for r in result.reports)
        step.ok = recovered
        step.detail = "all checks pass" if recovered else "checks still failing at timeout"

    now = time.monotonic()
    if recovered:
        result.recovery_seconds = now - restore_start
        result.outage_seconds = now - outage_start
        result.outcome = "PASS"
    else:
        result.outcome = "FAIL"
        result.reason = f"not recovered within {timeout:.0f}s of the restore"


def drill_report(result: DrillResult) -> str:
    def fmt_time(moment: datetime | None) -> str:
        return moment.strftime("%Y-%m-%d %H:%M:%S UTC") if moment else "-"

    lines = [
        f"# DR drill {fmt_time(result.started)}",
        "",
        f"- Outcome: **{result.outcome}**" + (f" ({result.reason})" if result.reason else ""),
        f"- Cluster context: `{result.context}`",
        f"- Started: {fmt_time(result.started)}",
        f"- Finished: {fmt_time(result.finished)}",
        f"- Services: {', '.join(result.services) or '-'}",
        f"- Objects backed up: {result.objects}",
        f"- Backup directory: `{result.backup_dir.as_posix()}`",
        "",
        "## Recovery time",
        "",
    ]
    if result.recovery_seconds is not None and result.outage_seconds is not None:
        lines += [
            f"- From start of restore to all checks passing: **{result.recovery_seconds:.1f} s**",
            f"- From deletion to all checks passing: {result.outage_seconds:.1f} s",
        ]
    else:
        lines.append("- Not measured: the drill did not complete a recovery.")
    lines += [
        "",
        "## Steps",
        "",
        "| Step | Started | Duration | Result | Detail |",
        "| --- | --- | --- | --- | --- |",
    ]
    for step in result.steps:
        lines.append(
            f"| {step.name} | {step.started.strftime('%H:%M:%S')} | {step.seconds:.1f} s | "
            f"{'ok' if step.ok else 'failed'} | {step.detail.replace('|', '/')} |"
        )
    lines += ["", "## Final verification", ""]
    if result.reports:
        lines += ["```", format_reports(result.reports), "```"]
    else:
        lines.append("Not run.")
    lines += [
        "",
        "## Scope",
        "",
        "This drill deletes and restores the configuration onboardctl manages: "
        "ResourceQuotas, ServiceMonitors, PrometheusRules, AlertmanagerConfigs and dashboard "
        "ConfigMaps. Namespaces and the workloads in them are left in place. Metric history "
        "stored by Prometheus is not part of the backup and is not restored.",
        "",
    ]
    return "\n".join(lines)
