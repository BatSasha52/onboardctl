"""A thin wrapper around the kubectl binary.

onboardctl shells out to kubectl rather than using a Kubernetes client
library: every operation is a kubectl command an engineer can rerun by hand,
and it uses the same kubeconfig, contexts and auth plugins as the rest of
their tooling. `--verbose` prints each command.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

FIELD_MANAGER = "onboardctl"

# The objects onboardctl creates, as kubectl resource names.
RESOURCE_FOR_KIND: dict[str, str] = {
    "Namespace": "namespaces",
    "ResourceQuota": "resourcequotas",
    "ServiceMonitor": "servicemonitors.monitoring.coreos.com",
    "PrometheusRule": "prometheusrules.monitoring.coreos.com",
    "AlertmanagerConfig": "alertmanagerconfigs.monitoring.coreos.com",
    "ConfigMap": "configmaps",
    "Role": "roles.rbac.authorization.k8s.io",
    "RoleBinding": "rolebindings.rbac.authorization.k8s.io",
}
MANAGED_RESOURCES = ",".join(RESOURCE_FOR_KIND.values())


class KubectlError(RuntimeError):
    pass


def _git_diff_dir() -> str | None:
    """Directory of the diff.exe that ships with Git for Windows, if any."""
    git = shutil.which("git")
    if not git:
        return None
    for root in Path(git).resolve().parents[:3]:
        candidate = root / "usr" / "bin" / "diff.exe"
        if candidate.is_file():
            return str(candidate.parent)
    return None


class Kubectl:
    def __init__(self, context: str | None = None, verbose: bool = False) -> None:
        binary = shutil.which("kubectl")
        if binary is None:
            raise KubectlError("kubectl not found on PATH")
        self.binary = binary
        self.context = context
        self.verbose = verbose
        self.env = dict(os.environ)
        # `kubectl diff` runs an external `diff`. Windows has none on PATH
        # outside Git Bash, so fall back to the one bundled with Git.
        if shutil.which("diff") is None and "KUBECTL_EXTERNAL_DIFF" not in self.env:
            extra = _git_diff_dir()
            if extra:
                self.env["PATH"] = self.env.get("PATH", "") + os.pathsep + extra

    def command(self, args: list[str]) -> list[str]:
        context = ["--context", self.context] if self.context else []
        return [self.binary, *context, *args]

    def run(
        self, args: list[str], stdin: str | None = None, ok_codes: tuple[int, ...] = (0,)
    ) -> subprocess.CompletedProcess[str]:
        cmd = self.command(args)
        if self.verbose:
            print("+ kubectl " + " ".join(cmd[1:]), file=sys.stderr)
        proc = subprocess.run(
            cmd, input=stdin, capture_output=True, text=True, encoding="utf-8", env=self.env
        )
        if proc.returncode not in ok_codes:
            detail = (proc.stderr or proc.stdout).strip()
            raise KubectlError(
                f"kubectl {' '.join(args)} failed (exit {proc.returncode}): {detail}"
            )
        return proc

    def popen(self, args: list[str]) -> subprocess.Popen[str]:
        cmd = self.command(args)
        if self.verbose:
            print("+ kubectl " + " ".join(cmd[1:]), file=sys.stderr)
        return subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            env=self.env,
        )

    def current_context(self) -> str:
        if self.context:
            return self.context
        return self.run(["config", "current-context"]).stdout.strip()

    def apply(self, manifests: str, dry_run: bool = False) -> str:
        # Server-side apply with one field manager: kubectl and the API server
        # work out what changed, and re-applying the same input is a no-op.
        # --force-conflicts because the spec in Git is the source of truth;
        # a field someone edited by hand is drift and is taken back.
        args = [
            "apply",
            "--server-side",
            f"--field-manager={FIELD_MANAGER}",
            "--force-conflicts",
            "-f",
            "-",
        ]
        if dry_run:
            args.append("--dry-run=server")
        return self.run(args, stdin=manifests).stdout

    def diff(self, manifests: str) -> str | None:
        """Unified diff of what `apply` would change, or None if nothing would."""
        args = [
            "diff",
            "--server-side",
            f"--field-manager={FIELD_MANAGER}",
            "--force-conflicts",
            "-f",
            "-",
        ]
        # kubectl diff exits 1 when there are differences, >1 on errors.
        proc = self.run(args, stdin=manifests, ok_codes=(0, 1))
        return proc.stdout if proc.returncode == 1 else None

    def get_items(
        self,
        resources: str,
        selector: str | None = None,
        namespace: str | None = None,
    ) -> list[dict[str, Any]]:
        args = ["get", resources, "-o", "json"]
        args += ["-n", namespace] if namespace else ["--all-namespaces"]
        if selector:
            args += ["-l", selector]
        items: list[dict[str, Any]] = json.loads(self.run(args).stdout).get("items", [])
        return items

    def exists(self, resource: str, name: str, namespace: str | None = None) -> bool:
        args = ["get", resource, name, "--ignore-not-found", "-o", "name"]
        if namespace:
            args += ["-n", namespace]
        return bool(self.run(args).stdout.strip())

    def delete(self, refs: list[str], namespace: str | None = None, wait: bool = True) -> str:
        args = ["delete", *refs, "--ignore-not-found", f"--wait={str(wait).lower()}"]
        if namespace:
            args += ["-n", namespace]
        return self.run(args).stdout

    def api_resources(self, group: str) -> set[str]:
        out = self.run(["api-resources", f"--api-group={group}", "-o", "name"]).stdout
        return {line.strip() for line in out.splitlines() if line.strip()}


def ref(obj: dict[str, Any]) -> str:
    """kubectl reference such as servicemonitors.monitoring.coreos.com/checkout-api."""
    return f"{RESOURCE_FOR_KIND[obj['kind']]}/{obj['metadata']['name']}"
