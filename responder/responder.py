"""Alertmanager webhook receiver that performs one remediation: a rollout restart.

It acts only on the fast availability burn-rate alert (ErrorBudgetBurn with
slo=availability, long_window=1h) of services whose spec opted in, which
onboardctl marks with remediation=rollout-restart and a deployment label.
Before acting it asks Alertmanager whether that alert is really firing, so a
forged webhook call cannot trigger a restart, and it restarts a Deployment at
most once per cooldown. The cooldown is read from the Deployment itself (the
annotation `kubectl rollout restart` sets), so it survives restarts of this
process and also counts restarts made by hand.

Every decision is written to stdout as one JSON line: the audit log.

Standard library only, so it runs from a ConfigMap on a stock python image.
See README.md in this directory for the reasoning behind these limits.
"""

from __future__ import annotations

import json
import os
import re
import ssl
import sys
import threading
import urllib.parse
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, TextIO

ALERTNAME = "ErrorBudgetBurn"
REQUIRED_LABELS = {"remediation": "rollout-restart", "slo": "availability", "long_window": "1h"}
RESTARTED_AT = "kubectl.kubernetes.io/restartedAt"
MAX_BODY = 1024 * 1024
_NAME = re.compile(r"^[a-z0-9]([-a-z0-9]*[a-z0-9])?$")


@dataclass(frozen=True)
class Target:
    namespace: str
    deployment: str


def utc_now() -> datetime:
    return datetime.now(UTC).replace(microsecond=0)


def eligible(alert: dict[str, Any]) -> tuple[Target | None, str]:
    """Whether this alert is one the responder may act on, and why not if not."""
    labels = alert.get("labels") or {}
    if alert.get("status") != "firing":
        return None, f"status is {alert.get('status')!r}, not firing"
    if labels.get("alertname") != ALERTNAME:
        return None, f"alert {labels.get('alertname')!r} is not handled"
    for key, value in REQUIRED_LABELS.items():
        if labels.get(key) != value:
            return None, f"label {key}={labels.get(key)!r}, needs {value!r}"
    namespace, deployment = labels.get("namespace", ""), labels.get("deployment", "")
    if not (_NAME.match(namespace) and _NAME.match(deployment)):
        return None, "missing or invalid namespace or deployment label"
    return Target(namespace, deployment), "eligible"


def cooldown_left(last_restart: str | None, now: datetime, cooldown: float) -> float:
    if not last_restart:
        return 0.0
    try:
        when = datetime.fromisoformat(last_restart.replace("Z", "+00:00"))
    except ValueError:
        return 0.0
    return max(0.0, cooldown - (now - when).total_seconds())


class KubeApi:
    """Just enough of the Kubernetes API, authenticated as the pod's service account."""

    SA_DIR = "/var/run/secrets/kubernetes.io/serviceaccount"

    def __init__(self) -> None:
        host = os.environ["KUBERNETES_SERVICE_HOST"]
        port = os.environ["KUBERNETES_SERVICE_PORT"]
        self.base = f"https://{host}:{port}"
        self.context = ssl.create_default_context(cafile=f"{self.SA_DIR}/ca.crt")

    def _request(self, method: str, path: str, body: Any = None, content_type: str = "") -> Any:
        # Re-read every call: the kubelet rotates projected tokens.
        with open(f"{self.SA_DIR}/token", encoding="utf-8") as f:
            token = f.read().strip()
        headers = {"Authorization": f"Bearer {token}", "Accept": "application/json"}
        data = None
        if body is not None:
            data = json.dumps(body).encode()
            headers["Content-Type"] = content_type
        request = urllib.request.Request(self.base + path, data, headers, method=method)
        with urllib.request.urlopen(request, context=self.context, timeout=10) as response:
            return json.load(response)

    def last_restart(self, target: Target) -> str | None:
        path = f"/apis/apps/v1/namespaces/{target.namespace}/deployments/{target.deployment}"
        deployment = self._request("GET", path)
        annotations = deployment["spec"]["template"]["metadata"].get("annotations") or {}
        value: str | None = annotations.get(RESTARTED_AT)
        return value

    def restart(self, target: Target, at: datetime) -> None:
        """The same patch `kubectl rollout restart` sends."""
        path = f"/apis/apps/v1/namespaces/{target.namespace}/deployments/{target.deployment}"
        patch = {
            "spec": {"template": {"metadata": {"annotations": {RESTARTED_AT: at.isoformat()}}}}
        }
        self._request("PATCH", path, patch, "application/strategic-merge-patch+json")


def alertmanager_confirms(base_url: str) -> Callable[[Target], bool]:
    def firing(target: Target) -> bool:
        filters = [
            f'alertname="{ALERTNAME}"',
            f'namespace="{target.namespace}"',
            f'deployment="{target.deployment}"',
            *(f'{k}="{v}"' for k, v in REQUIRED_LABELS.items()),
        ]
        query = urllib.parse.urlencode(
            [("active", "true"), ("silenced", "false"), ("inhibited", "false")]
            + [("filter", f) for f in filters]
        )
        with urllib.request.urlopen(f"{base_url}/api/v2/alerts?{query}", timeout=10) as response:
            return bool(json.load(response))

    return firing


class Responder:
    def __init__(
        self,
        kube: Any,
        is_firing: Callable[[Target], bool],
        cooldown: float,
        dry_run: bool,
        clock: Callable[[], datetime] = utc_now,
        audit_log: TextIO = sys.stdout,
    ) -> None:
        self.kube = kube
        self.is_firing = is_firing
        self.cooldown = cooldown
        self.dry_run = dry_run
        self.clock = clock
        self.audit_log = audit_log
        # One decision at a time, so two notifications for the same
        # Deployment cannot both pass the cooldown check.
        self.lock = threading.Lock()

    def handle(self, alert: dict[str, Any]) -> dict[str, Any]:
        with self.lock:
            record = self._decide(alert)
        self.audit_log.write(json.dumps(record, sort_keys=True) + "\n")
        self.audit_log.flush()
        return record

    def _decide(self, alert: dict[str, Any]) -> dict[str, Any]:
        now = self.clock()
        labels = alert.get("labels") or {}
        record: dict[str, Any] = {
            "time": now.isoformat(),
            "alertname": labels.get("alertname"),
            "fingerprint": alert.get("fingerprint"),
            "namespace": labels.get("namespace"),
            "deployment": labels.get("deployment"),
            "dry_run": self.dry_run,
        }
        target, reason = eligible(alert)
        if target is None:
            return {**record, "action": "ignored", "reason": reason}
        try:
            if not self.is_firing(target):
                return {
                    **record,
                    "action": "ignored",
                    "reason": "Alertmanager does not report this alert as firing",
                }
            last = self.kube.last_restart(target)
            left = cooldown_left(last, now, self.cooldown)
            if left > 0:
                return {
                    **record,
                    "action": "rate_limited",
                    "reason": f"last restart at {last}, {left:.0f}s of cooldown left",
                }
            if self.dry_run:
                return {**record, "action": "would_restart", "reason": "dry run"}
            self.kube.restart(target, now)
            return {**record, "action": "restarted", "reason": "fast availability burn"}
        except Exception as exc:  # logged, never raised into Alertmanager's retry loop
            return {**record, "action": "error", "reason": f"{type(exc).__name__}: {exc}"}


def make_handler(responder: Responder) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802
            self._reply(200 if self.path == "/healthz" else 404, {"ok": self.path == "/healthz"})

        def do_POST(self) -> None:  # noqa: N802
            if self.path != "/alerts":
                self._reply(404, {"error": "not found"})
                return
            length = int(self.headers.get("Content-Length") or 0)
            if length > MAX_BODY:
                self._reply(413, {"error": "payload too large"})
                return
            try:
                payload = json.loads(self.rfile.read(length))
                alerts = payload["alerts"]
            except (ValueError, KeyError, TypeError):
                self._reply(400, {"error": "expected an Alertmanager webhook payload"})
                return
            actions = [responder.handle(alert)["action"] for alert in alerts]
            self._reply(200, {"actions": actions})

        def _reply(self, status: int, body: dict[str, Any]) -> None:
            data = json.dumps(body).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def log_message(self, format: str, *args: object) -> None:  # noqa: A002
            pass  # the audit log is the record that matters

    return Handler


def main() -> None:
    responder = Responder(
        kube=KubeApi(),
        is_firing=alertmanager_confirms(os.environ["ALERTMANAGER_URL"].rstrip("/")),
        cooldown=float(os.environ.get("COOLDOWN_SECONDS", "1800")),
        dry_run=os.environ.get("DRY_RUN", "false").lower() == "true",
    )
    port = int(os.environ.get("PORT", "8080"))
    ThreadingHTTPServer(("", port), make_handler(responder)).serve_forever()


if __name__ == "__main__":
    main()
