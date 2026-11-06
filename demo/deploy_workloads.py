"""Deploy demo_app.py as the workload behind each given spec.

Demo and CI use only: onboardctl itself never creates workloads. For each
spec this creates, in the spec's namespace, a Deployment running demo_app.py
and a Service that carries the spec's selector labels and its named metrics
port, which is what the generated ServiceMonitor looks for.

    python demo/deploy_workloads.py services/
    python demo/deploy_workloads.py examples/api-strict.yaml --error-ratio 0.05
    python demo/deploy_workloads.py services/ --delete
"""

from __future__ import annotations

import argparse
import hashlib
import sys
from pathlib import Path
from typing import Any

from onboardctl import yamlutil
from onboardctl.kube import Kubectl
from onboardctl.spec import ServiceSpec, collect_spec_files, load_spec

IMAGE = "python:3.13.15-slim-bookworm"
APP_SOURCE = Path(__file__).with_name("demo_app.py")


def workload(spec: ServiceSpec, error_ratio: float, slow_ratio: float) -> list[dict[str, Any]]:
    namespace = spec.target_namespace
    name = spec.name
    pod_labels = {"app": name, "app.kubernetes.io/part-of": "onboardctl-demo"}
    env = {
        "PORT": "8080",
        "METRICS_PATH": spec.metrics.path,
        "ERROR_RATIO": str(error_ratio),
        "SLOW_RATIO": str(slow_ratio),
        "PYTHONDONTWRITEBYTECODE": "1",
    }
    source = APP_SOURCE.read_text(encoding="utf-8")
    config_map = {
        "apiVersion": "v1",
        "kind": "ConfigMap",
        "metadata": {"name": name, "namespace": namespace, "labels": pod_labels},
        "data": {"demo_app.py": source},
    }
    deployment = {
        "apiVersion": "apps/v1",
        "kind": "Deployment",
        "metadata": {"name": name, "namespace": namespace, "labels": pod_labels},
        "spec": {
            "replicas": 1,
            "selector": {"matchLabels": pod_labels},
            "template": {
                "metadata": {
                    "labels": pod_labels,
                    # Restart the pod when the app source changes.
                    "annotations": {
                        "demo/source-sha256": hashlib.sha256(source.encode()).hexdigest()[:16]
                    },
                },
                "spec": {
                    "securityContext": {"runAsNonRoot": True, "runAsUser": 65534},
                    "containers": [
                        {
                            "name": "app",
                            "image": IMAGE,
                            "command": ["python", "/app/demo_app.py"],
                            "env": [{"name": k, "value": v} for k, v in env.items()],
                            "ports": [{"name": "http", "containerPort": 8080}],
                            "readinessProbe": {"httpGet": {"path": "/healthz", "port": "http"}},
                            "resources": {
                                "requests": {"cpu": "20m", "memory": "32Mi"},
                                "limits": {"memory": "64Mi"},
                            },
                            "securityContext": {
                                "allowPrivilegeEscalation": False,
                                "readOnlyRootFilesystem": True,
                            },
                            "volumeMounts": [{"name": "app", "mountPath": "/app"}],
                        }
                    ],
                    "volumes": [{"name": "app", "configMap": {"name": name}}],
                },
            },
        },
    }
    service = {
        "apiVersion": "v1",
        "kind": "Service",
        "metadata": {"name": name, "namespace": namespace, "labels": spec.selector},
        "spec": {
            "selector": pod_labels,
            "ports": [{"name": spec.metrics.port, "port": 8080, "targetPort": "http"}],
        },
    }
    return [config_map, deployment, service]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("specs", nargs="+", type=Path)
    parser.add_argument("--context")
    parser.add_argument("--error-ratio", type=float, default=0.0005)
    parser.add_argument("--slow-ratio", type=float, default=0.002)
    parser.add_argument("--delete", action="store_true", help="remove the demo workloads")
    args = parser.parse_args()

    kubectl = Kubectl(args.context)
    for path in collect_spec_files(args.specs):
        spec = load_spec(path).spec
        objects = workload(spec, args.error_ratio, args.slow_ratio)
        if args.delete:
            refs = [f"{o['kind'].lower()}/{o['metadata']['name']}" for o in objects]
            print(kubectl.delete(refs, namespace=spec.target_namespace), end="")
            continue
        text = "---\n".join(yamlutil.dump(o) for o in objects)
        print(kubectl.run(["apply", "-f", "-"], stdin=text).stdout, end="")
        kubectl.run(
            [
                "rollout",
                "status",
                f"deployment/{spec.name}",
                "-n",
                spec.target_namespace,
                "--timeout=180s",
            ]
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
