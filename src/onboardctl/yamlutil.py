"""YAML output that stays readable in Git diffs."""

from __future__ import annotations

from typing import Any

import yaml


class _Dumper(yaml.SafeDumper):
    pass


def _represent_str(dumper: yaml.SafeDumper, value: str) -> yaml.ScalarNode:
    # Multi-line strings (PromQL, dashboard JSON) as literal blocks instead of
    # one long quoted line full of \n escapes.
    if "\n" in value:
        return dumper.represent_scalar("tag:yaml.org,2002:str", value, style="|")
    return dumper.represent_scalar("tag:yaml.org,2002:str", value)


_Dumper.add_representer(str, _represent_str)


def dump(data: Any) -> str:
    return yaml.dump(data, Dumper=_Dumper, sort_keys=True, default_flow_style=False, width=1000)


def load_all(text: str) -> list[Any]:
    return [doc for doc in yaml.safe_load_all(text) if doc is not None]
