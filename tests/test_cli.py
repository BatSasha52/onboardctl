from __future__ import annotations

from pathlib import Path

import pytest
from typer.testing import CliRunner

from onboardctl import cli
from tests.conftest import EXAMPLES
from tests.fakes import FakeKubectl

runner = CliRunner()


def test_validate_valid_examples() -> None:
    result = runner.invoke(
        cli.app,
        ["validate", str(EXAMPLES / "api-strict.yaml"), str(EXAMPLES / "batch-relaxed.yaml")],
    )
    assert result.exit_code == 0, result.output
    assert "service checkout-api, namespace payments-checkout" in result.output


def test_validate_invalid_example_exits_1_with_field_paths() -> None:
    result = runner.invoke(cli.app, ["validate", str(EXAMPLES / "invalid.yaml")])
    assert result.exit_code == 1
    assert "7 problem(s)" in result.output
    assert "metrics.pathh: unknown field; did you mean 'path'?" in result.output
    assert "example: target: 99.9" in result.output


def test_validate_directory_with_invalid_spec_fails() -> None:
    result = runner.invoke(cli.app, ["validate", str(EXAMPLES)])
    assert result.exit_code == 1


def test_render_writes_manifests_and_rule_files(tmp_path: Path) -> None:
    result = runner.invoke(
        cli.app,
        [
            "render",
            str(EXAMPLES / "api-strict.yaml"),
            "-o",
            str(tmp_path / "out"),
            "--rules-dir",
            str(tmp_path / "rules"),
        ],
    )
    assert result.exit_code == 0, result.output
    assert len(list((tmp_path / "out" / "checkout-api").glob("*.yaml"))) == 8
    assert (tmp_path / "rules" / "checkout-api.rules.yaml").is_file()


def test_render_refuses_invalid_specs(tmp_path: Path) -> None:
    result = runner.invoke(cli.app, ["render", str(EXAMPLES / "invalid.yaml"), "-o", str(tmp_path)])
    assert result.exit_code == 1
    assert not any(tmp_path.iterdir())


def test_verify_needs_specs_or_all() -> None:
    assert runner.invoke(cli.app, ["verify"]).exit_code == 2
    both = runner.invoke(cli.app, ["verify", str(EXAMPLES / "api-strict.yaml"), "--all"])
    assert both.exit_code == 2


def test_drill_refuses_a_non_kind_context(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cli.Settings, "kubectl", lambda self: FakeKubectl([], context="prod-aks"))
    result = runner.invoke(cli.app, ["drill", "--yes"])
    assert result.exit_code == 2
    assert "refusing to drill against 'prod-aks'" in result.output
