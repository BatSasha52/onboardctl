"""Label and annotation keys that onboardctl puts on every object it manages.

These labels are how `list`, `offboard`, `backup` and `verify` find the
objects later, so changing them is a breaking change.
"""

from __future__ import annotations

MANAGED_BY = "app.kubernetes.io/managed-by"
MANAGER = "onboardctl"
SERVICE = "onboardctl/service"
TEAM = "onboardctl/team"

SPEC_HASH = "onboardctl/spec-hash"
OWNERS = "onboardctl/owners"
DESCRIPTION = "onboardctl/description"

MANAGED_SELECTOR = f"{MANAGED_BY}={MANAGER}"


def common_labels(service: str, team: str) -> dict[str, str]:
    return {MANAGED_BY: MANAGER, SERVICE: service, TEAM: team}


def service_selector(service: str) -> str:
    return f"{MANAGED_SELECTOR},{SERVICE}={service}"
