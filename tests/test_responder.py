from __future__ import annotations

import io
import json
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from responder import Responder, Target, cooldown_left, eligible

NOW = datetime(2026, 11, 6, 12, 0, tzinfo=UTC)


def alert(**overrides: Any) -> dict[str, Any]:
    labels = {
        "alertname": "ErrorBudgetBurn",
        "namespace": "payments-checkout",
        "deployment": "checkout-api",
        "remediation": "rollout-restart",
        "slo": "availability",
        "long_window": "1h",
        "severity": "critical",
    }
    labels.update(overrides.pop("labels", {}))
    return {"status": "firing", "labels": labels, "fingerprint": "abc", **overrides}


class FakeKube:
    def __init__(self, last: str | None = None, fail: bool = False) -> None:
        self.last = last
        self.fail = fail
        self.restarts: list[tuple[Target, datetime]] = []

    def last_restart(self, target: Target) -> str | None:
        if self.fail:
            raise PermissionError("deployments.apps is forbidden")
        return self.last

    def restart(self, target: Target, at: datetime) -> None:
        self.restarts.append((target, at))


def responder(kube: FakeKube, firing: bool = True, dry_run: bool = False) -> Responder:
    return Responder(
        kube,
        lambda _: firing,
        cooldown=1800,
        dry_run=dry_run,
        clock=lambda: NOW,
        audit_log=io.StringIO(),
    )


def test_eligible_alert() -> None:
    assert eligible(alert()) == (Target("payments-checkout", "checkout-api"), "eligible")


@pytest.mark.parametrize(
    ("change", "reason"),
    [
        ({"status": "resolved"}, "not firing"),
        ({"labels": {"alertname": "ScrapeTargetMissing"}}, "is not handled"),
        ({"labels": {"slo": "latency"}}, "label slo='latency'"),
        ({"labels": {"long_window": "6h"}}, "label long_window='6h'"),
        ({"labels": {"remediation": "none"}}, "label remediation='none'"),
        ({"labels": {"deployment": "../../secrets"}}, "invalid namespace or deployment"),
    ],
)
def test_ineligible_alerts(change: dict[str, Any], reason: str) -> None:
    target, why = eligible(alert(**change))
    assert target is None
    assert reason in why


def test_restarts_and_writes_an_audit_record() -> None:
    kube = FakeKube()
    r = responder(kube)
    record = r.handle(alert())
    assert record["action"] == "restarted"
    assert kube.restarts == [(Target("payments-checkout", "checkout-api"), NOW)]
    logged = json.loads(r.audit_log.getvalue())
    assert logged == record


def test_cooldown_prevents_a_second_restart() -> None:
    kube = FakeKube(last=(NOW - timedelta(minutes=10)).isoformat())
    record = responder(kube).handle(alert())
    assert record["action"] == "rate_limited"
    assert "1200s of cooldown left" in record["reason"]
    assert kube.restarts == []


def test_restart_allowed_after_cooldown() -> None:
    kube = FakeKube(last=(NOW - timedelta(minutes=31)).isoformat().replace("+00:00", "Z"))
    assert responder(kube).handle(alert())["action"] == "restarted"


def test_not_firing_in_alertmanager_is_ignored() -> None:
    kube = FakeKube()
    record = responder(kube, firing=False).handle(alert())
    assert record["action"] == "ignored"
    assert kube.restarts == []


def test_dry_run_does_not_restart() -> None:
    kube = FakeKube()
    assert responder(kube, dry_run=True).handle(alert())["action"] == "would_restart"
    assert kube.restarts == []


def test_api_errors_are_recorded_not_raised() -> None:
    record = responder(FakeKube(fail=True)).handle(alert())
    assert record["action"] == "error"
    assert "forbidden" in record["reason"]


def test_cooldown_left_ignores_garbage() -> None:
    assert cooldown_left(None, NOW, 60) == 0
    assert cooldown_left("yesterday", NOW, 60) == 0
    assert cooldown_left(NOW.isoformat(), NOW, 60) == 60
