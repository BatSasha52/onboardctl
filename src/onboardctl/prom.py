"""Reaching the Prometheus and Alertmanager HTTP APIs.

By default onboardctl starts `kubectl port-forward` to the in-cluster
Service on a free local port for the duration of the command, so nothing
has to be exposed outside the cluster. Set ONBOARDCTL_PROMETHEUS_URL and
ONBOARDCTL_ALERTMANAGER_URL (or the matching flags) to use existing
endpoints instead.
"""

from __future__ import annotations

import queue
import re
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any

import requests

from onboardctl.kube import Kubectl


class ApiError(RuntimeError):
    pass


@dataclass(frozen=True)
class Endpoints:
    """Where to find Prometheus and Alertmanager."""

    namespace: str = "monitoring"
    prometheus_service: str = "kps-prometheus"
    alertmanager_service: str = "kps-alertmanager"
    prometheus_url: str | None = None
    alertmanager_url: str | None = None


_FORWARDING = re.compile(r"Forwarding from 127\.0\.0\.1:(\d+)")


@contextmanager
def port_forward(
    kubectl: Kubectl, namespace: str, service: str, remote_port: int, timeout: float = 20
) -> Iterator[str]:
    """Yield a local base URL that forwards to the Service until the block ends."""
    # ":<port>" lets kubectl choose a free local port, which it prints.
    proc = kubectl.popen(["port-forward", "-n", namespace, f"svc/{service}", f":{remote_port}"])
    lines: queue.Queue[str] = queue.Queue()

    def reader() -> None:
        assert proc.stdout is not None
        for line in proc.stdout:
            lines.put(line)

    threading.Thread(target=reader, daemon=True).start()
    output = []
    try:
        while True:
            try:
                line = lines.get(timeout=timeout)
            except queue.Empty:
                raise ApiError(f"timed out waiting for port-forward to svc/{service}") from None
            output.append(line.strip())
            match = _FORWARDING.search(line)
            if match:
                break
            if proc.poll() is not None:
                raise ApiError(f"port-forward to svc/{service} failed: {' '.join(output)}")
        yield f"http://127.0.0.1:{match[1]}"
    finally:
        proc.terminate()
        proc.wait(timeout=10)


@contextmanager
def connect(
    kubectl: Kubectl, endpoints: Endpoints
) -> Iterator[tuple[PrometheusApi, AlertmanagerApi]]:
    with (
        _endpoint(
            kubectl,
            endpoints.prometheus_url,
            endpoints.namespace,
            endpoints.prometheus_service,
            9090,
        ) as prom_url,
        _endpoint(
            kubectl,
            endpoints.alertmanager_url,
            endpoints.namespace,
            endpoints.alertmanager_service,
            9093,
        ) as am_url,
    ):
        yield PrometheusApi(prom_url), AlertmanagerApi(am_url)


@contextmanager
def _endpoint(
    kubectl: Kubectl, url: str | None, namespace: str, service: str, port: int
) -> Iterator[str]:
    if url:
        yield url.rstrip("/")
    else:
        with port_forward(kubectl, namespace, service, port) as forwarded:
            yield forwarded


class _Api:
    def __init__(self, base_url: str, timeout: float = 10) -> None:
        self.base_url = base_url
        self.timeout = timeout
        self.session = requests.Session()

    def _get(self, path: str, params: dict[str, str] | None = None) -> Any:
        url = self.base_url + path
        try:
            response = self.session.get(url, params=params, timeout=self.timeout)
            response.raise_for_status()
            return response.json()
        except (requests.RequestException, ValueError) as exc:
            raise ApiError(f"GET {path} failed: {exc}") from exc


class PrometheusApi(_Api):
    def _data(self, path: str, params: dict[str, str] | None = None) -> Any:
        body = self._get(path, params)
        if body.get("status") != "success":
            raise ApiError(f"GET {path}: {body.get('error', 'unexpected response')}")
        return body["data"]

    def active_targets(self) -> list[dict[str, Any]]:
        targets: list[dict[str, Any]] = self._data("/api/v1/targets", {"state": "active"})[
            "activeTargets"
        ]
        return targets

    def rule_groups(self) -> list[dict[str, Any]]:
        groups: list[dict[str, Any]] = self._data("/api/v1/rules")["groups"]
        return groups

    def query(self, promql: str) -> list[dict[str, Any]]:
        result: list[dict[str, Any]] = self._data("/api/v1/query", {"query": promql})["result"]
        return result


class AlertmanagerApi(_Api):
    def receivers(self) -> set[str]:
        return {r["name"] for r in self._get("/api/v2/receivers")}
