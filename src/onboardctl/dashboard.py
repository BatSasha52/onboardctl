"""Grafana dashboard for one service, built as data and serialised to JSON."""

from __future__ import annotations

import hashlib
import json
from typing import Any

from onboardctl import slo
from onboardctl.rules import SliQueries, sli_queries
from onboardctl.spec import ServiceSpec

# The Prometheus data source that kube-prometheus-stack provisions in Grafana.
DATASOURCE = {"type": "prometheus", "uid": "prometheus"}


def dashboard_uid(spec: ServiceSpec) -> str:
    # Grafana limits UIDs to 40 characters, so derive one rather than using the name.
    digest = hashlib.sha256(f"{spec.target_namespace}/{spec.name}".encode()).hexdigest()
    return f"onboardctl-{digest[:16]}"


def _panel(
    panel_id: int,
    title: str,
    kind: str,
    grid: tuple[int, int, int, int],
    targets: list[tuple[str, str]],
    unit: str,
    description: str = "",
) -> dict[str, Any]:
    x, y, w, h = grid
    return {
        "id": panel_id,
        "title": title,
        "description": description,
        "type": kind,
        "datasource": DATASOURCE,
        "gridPos": {"x": x, "y": y, "w": w, "h": h},
        "fieldConfig": {"defaults": {"unit": unit}, "overrides": []},
        "targets": [
            {"refId": chr(ord("A") + i), "expr": expr, "legendFormat": legend}
            for i, (expr, legend) in enumerate(targets)
        ],
    }


def _sli_row(
    first_id: int, y: int, label: str, queries: SliQueries, target: float
) -> list[dict[str, Any]]:
    budget = slo.fmt(queries.budget)
    return [
        _panel(
            first_id,
            f"{label} over the SLO window (target {target:g}%)",
            "stat",
            (0, y, 6, 6),
            [(f"1 - ({queries.window_error_ratio})", label)],
            "percentunit",
        ),
        _panel(
            first_id + 1,
            f"{label} error budget remaining",
            "stat",
            (6, y, 6, 6),
            [(f"1 - ({queries.window_error_ratio}) / {budget}", "remaining")],
            "percentunit",
            "1 means no budget used, 0 means the budget is spent, below 0 means the SLO is missed.",
        ),
        _panel(
            first_id + 2,
            f"{label} burn rate",
            "timeseries",
            (12, y, 12, 6),
            [(f"{queries.ratio(w)} / {budget}", w) for w in ("5m", "1h", "6h")],
            "none",
            "1 means the budget lasts exactly the SLO window. The fast page alert fires above "
            f"{slo.fmt(slo.burn_factor(queries.window, slo.TIERS[0]))}.",
        ),
    ]


def build_dashboard(spec: ServiceSpec) -> dict[str, Any]:
    queries = sli_queries(spec)
    selector = queries["availability"].selector
    requests = spec.slo.metrics.requests
    panels = _sli_row(1, 0, "Availability", queries["availability"], spec.slo.availability.target)
    y = 6
    if spec.slo.latency and "latency" in queries:
        panels += _sli_row(4, y, "Latency", queries["latency"], spec.slo.latency.target)
        y += 6
    panels += [
        _panel(
            10,
            "Requests per second by status code",
            "timeseries",
            (0, y, 16, 7),
            [(f"sum by (code) (rate({requests}{{{selector}}}[5m]))", "{{code}}")],
            "reqps",
        ),
        _panel(
            11,
            "Scrape targets up",
            "stat",
            (16, y, 8, 7),
            [(f"sum(up{{{selector}}})", "up")],
            "none",
        ),
    ]
    return {
        "uid": dashboard_uid(spec),
        "title": f"{spec.name} SLOs",
        "tags": ["onboardctl", spec.team],
        "timezone": "utc",
        "schemaVersion": 39,
        "refresh": "30s",
        "time": {"from": "now-6h", "to": "now"},
        "panels": panels,
    }


def dashboard_json(spec: ServiceSpec) -> str:
    return json.dumps(build_dashboard(spec), indent=2, sort_keys=True)
