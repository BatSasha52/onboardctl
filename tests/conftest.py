from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from onboardctl.spec import ServiceSpec, parse_spec

REPO = Path(__file__).resolve().parents[1]
EXAMPLES = REPO / "examples"


def minimal_data() -> dict[str, Any]:
    return {
        "apiVersion": "onboardctl/v1",
        "name": "demo-api",
        "team": "demo",
        "owners": ["demo@example.com"],
        "metrics": {"port": "http-metrics"},
        "slo": {"availability": {"target": 99.9}},
        "alerting": {
            "defaultReceiver": "demo-tickets",
            "receivers": [{"name": "demo-tickets", "webhookUrl": "https://hooks.example.com/x"}],
        },
    }


@pytest.fixture
def data() -> dict[str, Any]:
    return minimal_data()


@pytest.fixture
def spec() -> ServiceSpec:
    return parse_spec(minimal_data(), "test")
