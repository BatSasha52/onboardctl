"""Turn a spec into Kubernetes manifests using the Jinja2 templates."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import jinja2
import yaml

from onboardctl import labels as lbl
from onboardctl import yamlutil
from onboardctl.dashboard import dashboard_json
from onboardctl.rules import build_rules
from onboardctl.spec import RESPONDER_RECEIVER, ServiceSpec


@dataclass(frozen=True)
class Manifest:
    filename: str
    kind: str
    name: str
    namespace: str | None
    text: str


@dataclass(frozen=True)
class ObjectNames:
    quota: str
    service_monitor: str
    prometheus_rule: str
    alertmanager_config: str
    dashboard: str
    responder_role: str


def object_names(spec: ServiceSpec) -> ObjectNames:
    return ObjectNames(
        quota=f"{spec.name}-quota",
        service_monitor=spec.name,
        prometheus_rule=f"{spec.name}-slo",
        alertmanager_config=f"{spec.name}-routing",
        dashboard=f"{spec.name}-slo-dashboard",
        responder_role=f"{spec.name}-responder",
    )


# Strings that YAML would read as something other than a string, or that
# need quoting for another reason, are written as JSON strings (valid YAML).
_PLAIN = re.compile(r"^[A-Za-z_/][A-Za-z0-9._/-]*$")
_YAML_KEYWORDS = {"true", "false", "yes", "no", "on", "off", "null", "y", "n", "~"}


def yaml_str(value: Any) -> str:
    text = str(value)
    if _PLAIN.match(text) and text.lower() not in _YAML_KEYWORDS:
        return text
    return json.dumps(text)


def _environment() -> jinja2.Environment:
    env = jinja2.Environment(
        loader=jinja2.PackageLoader("onboardctl", "templates"),
        undefined=jinja2.StrictUndefined,
        trim_blocks=True,
        lstrip_blocks=True,
        keep_trailing_newline=True,
        autoescape=False,  # YAML output, not HTML
    )
    env.filters["yaml_str"] = yaml_str
    return env


_ENV = _environment()

# (template, kind) in apply order: the Namespace must exist before anything in it.
_TEMPLATES = (
    ("namespace.yaml.j2", "Namespace"),
    ("resourcequota.yaml.j2", "ResourceQuota"),
    ("servicemonitor.yaml.j2", "ServiceMonitor"),
    ("prometheusrule.yaml.j2", "PrometheusRule"),
    ("alertmanagerconfig.yaml.j2", "AlertmanagerConfig"),
    ("dashboard.yaml.j2", "ConfigMap"),
    ("role.yaml.j2", "Role"),
    ("rolebinding.yaml.j2", "RoleBinding"),
)

# Where the remediation responder runs (see responder/).
RESPONDER_NAMESPACE = "onboardctl-system"
RESPONDER_SERVICE_ACCOUNT = "onboardctl-responder"
RESPONDER_URL = "http://onboardctl-responder.onboardctl-system.svc:8080/alerts"


def _context(spec: ServiceSpec) -> dict[str, Any]:
    labels = lbl.common_labels(spec.name, spec.team)
    annotations = {lbl.SPEC_HASH: spec.spec_hash()}
    namespace_annotations = {**annotations, lbl.OWNERS: ",".join(spec.owners)}
    if spec.description:
        namespace_annotations[lbl.DESCRIPTION] = spec.description
    return {
        "name": spec.name,
        "namespace": spec.target_namespace,
        "names": object_names(spec),
        "labels": labels,
        "annotations": annotations,
        # Baseline Pod Security is the platform default for team namespaces.
        "namespace_labels": {**labels, "pod-security.kubernetes.io/enforce": "baseline"},
        "namespace_annotations": namespace_annotations,
        "dashboard_labels": {**labels, "grafana_dashboard": "1"},
        "selector": spec.selector,
        "metrics": spec.metrics,
        "quota": spec.quota,
        "alerting": spec.alerting,
        "rules": build_rules(spec),
        "dashboard_json": dashboard_json(spec) if spec.dashboard.enabled else "",
        "remediation": spec.remediation,
        "responder": {
            "receiver": RESPONDER_RECEIVER,
            "namespace": RESPONDER_NAMESPACE,
            "service_account": RESPONDER_SERVICE_ACCOUNT,
            "url": RESPONDER_URL,
        },
    }


def render_spec(spec: ServiceSpec) -> list[Manifest]:
    context = _context(spec)
    manifests = []
    for index, (template, kind) in enumerate(_TEMPLATES):
        if kind == "ConfigMap" and not spec.dashboard.enabled:
            continue
        if kind in ("Role", "RoleBinding") and spec.remediation is None:
            continue
        text = _ENV.get_template(template).render(**context)
        obj = yaml.safe_load(text)  # a template bug should fail here, not in kubectl
        if obj.get("kind") != kind:
            raise RuntimeError(f"{template} rendered kind {obj.get('kind')!r}, expected {kind}")
        manifests.append(
            Manifest(
                filename=f"{index:02d}-{kind.lower()}.yaml",
                kind=kind,
                name=obj["metadata"]["name"],
                namespace=obj["metadata"].get("namespace"),
                text=text,
            )
        )
    return manifests


def join_manifests(manifests: list[Manifest]) -> str:
    return "---\n".join(m.text for m in manifests)


def write_manifests(manifests: list[Manifest], directory: Path) -> list[Path]:
    """Write one file per object, replacing whatever an earlier render left there."""
    directory.mkdir(parents=True, exist_ok=True)
    for old in directory.glob("*.yaml"):
        old.unlink()
    paths = []
    for manifest in manifests:
        path = directory / manifest.filename
        path.write_text(manifest.text, encoding="utf-8", newline="\n")
        paths.append(path)
    return paths


def prometheus_rule_file(manifests: list[Manifest]) -> str:
    """The PrometheusRule's groups as a plain Prometheus rule file, for promtool."""
    rule = next(m for m in manifests if m.kind == "PrometheusRule")
    groups = yaml.safe_load(rule.text)["spec"]["groups"]
    return yamlutil.dump({"groups": groups})
