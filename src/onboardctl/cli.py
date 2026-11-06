"""Command-line entry point.

Exit codes: 0 success, 1 a check failed (invalid spec, failed verification,
differences found by `diff`), 2 usage error, 3 the environment is not usable
(kubectl missing, cluster or Prometheus unreachable, bad backup).
"""

from __future__ import annotations

import sys
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated, NoReturn

import typer

from onboardctl import cluster, dr, prom
from onboardctl.kube import Kubectl, KubectlError
from onboardctl.render import prometheus_rule_file, render_spec, write_manifests
from onboardctl.spec import (
    LoadedSpec,
    SpecError,
    collect_spec_files,
    duplicate_problems,
    load_spec,
)
from onboardctl.verify import (
    ServiceReport,
    expectations_from_manifests,
    expectations_from_objects,
    format_reports,
    verify,
)

app = typer.Typer(
    add_completion=False,
    no_args_is_help=True,
    pretty_exceptions_enable=False,
    help="Onboard a service to the observability platform from one YAML spec.",
)


@dataclass(frozen=True)
class Settings:
    context: str | None
    verbose: bool
    endpoints: prom.Endpoints

    def kubectl(self) -> Kubectl:
        return Kubectl(self.context, self.verbose)


def _settings(ctx: typer.Context) -> Settings:
    settings: Settings = ctx.obj
    return settings


def _err(message: str) -> None:
    typer.echo(message, err=True)


def _exit(message: str, code: int) -> NoReturn:
    _err(message)
    raise typer.Exit(code)


@contextmanager
def _environment_errors() -> Iterator[None]:
    try:
        yield
    except (KubectlError, cluster.ClusterError, prom.ApiError, dr.BackupError) as exc:
        _exit(f"error: {exc}", 3)


@app.callback()
def _options(
    ctx: typer.Context,
    context: Annotated[
        str | None,
        typer.Option(
            help="kubectl context to use (default: the current context).",
            envvar="ONBOARDCTL_CONTEXT",
        ),
    ] = None,
    monitoring_namespace: Annotated[
        str, typer.Option(help="Namespace of Prometheus and Alertmanager.")
    ] = "monitoring",
    prometheus_service: Annotated[str, typer.Option(help="Prometheus Service name.")] = (
        "kps-prometheus"
    ),
    alertmanager_service: Annotated[str, typer.Option(help="Alertmanager Service name.")] = (
        "kps-alertmanager"
    ),
    prometheus_url: Annotated[
        str | None,
        typer.Option(
            help="Use this Prometheus URL instead of a port-forward.",
            envvar="ONBOARDCTL_PROMETHEUS_URL",
        ),
    ] = None,
    alertmanager_url: Annotated[
        str | None,
        typer.Option(
            help="Use this Alertmanager URL instead of a port-forward.",
            envvar="ONBOARDCTL_ALERTMANAGER_URL",
        ),
    ] = None,
    verbose: Annotated[
        bool, typer.Option("--verbose", "-v", help="Print each kubectl command.")
    ] = False,
) -> None:
    ctx.obj = Settings(
        context,
        verbose,
        prom.Endpoints(
            monitoring_namespace,
            prometheus_service,
            alertmanager_service,
            prometheus_url,
            alertmanager_url,
        ),
    )


SpecPaths = Annotated[
    list[Path], typer.Argument(help="Spec files, or directories of them.", exists=True)
]


def _load(paths: list[Path], quiet: bool = False) -> tuple[list[LoadedSpec], bool]:
    files = collect_spec_files(paths)
    if not files:
        _exit("error: no .yaml spec files found", 1)
    loaded: list[LoadedSpec] = []
    ok = True
    for path in files:
        try:
            spec = load_spec(path)
        except SpecError as exc:
            ok = False
            _err(f"FAIL {exc.source}: {len(exc.problems)} problem(s)")
            for problem in exc.problems:
                _err("  " + str(problem).replace("\n", "\n  "))
            continue
        loaded.append(spec)
        if not quiet:
            service = spec.spec
            typer.echo(
                f"ok   {path.as_posix()} "
                f"(service {service.name}, namespace {service.target_namespace})"
            )
        for warning in spec.warnings:
            _err(f"  warning: {warning}")
    for message in duplicate_problems(loaded):
        ok = False
        _err(f"FAIL {message}")
    return loaded, ok


def _load_or_exit(paths: list[Path]) -> list[LoadedSpec]:
    loaded, ok = _load(paths, quiet=True)
    if not ok:
        _exit("error: fix the spec problems above first", 1)
    return loaded


@app.command()
def validate(specs: SpecPaths) -> None:
    """Check specs against the schema and the platform's rules."""
    _, ok = _load(specs)
    if not ok:
        raise typer.Exit(1)


@app.command()
def render(
    specs: SpecPaths,
    output: Annotated[Path, typer.Option("--output", "-o", help="Output directory.")] = Path(
        "rendered"
    ),
    rules_dir: Annotated[
        Path | None,
        typer.Option(
            help="Also write each service's rules as a plain Prometheus rule file "
            "here, for promtool."
        ),
    ] = None,
) -> None:
    """Generate the Kubernetes manifests for each spec, one directory per service."""
    for loaded in _load_or_exit(specs):
        manifests = render_spec(loaded.spec)
        for path in write_manifests(manifests, output / loaded.spec.name):
            typer.echo(path.as_posix())
        if rules_dir is not None:
            rules_dir.mkdir(parents=True, exist_ok=True)
            rule_path = rules_dir / f"{loaded.spec.name}.rules.yaml"
            rule_path.write_text(prometheus_rule_file(manifests), encoding="utf-8", newline="\n")
            typer.echo(rule_path.as_posix())


@app.command()
def diff(ctx: typer.Context, specs: SpecPaths) -> None:
    """Show what `apply` would change in the cluster. Exits 1 if anything would."""
    loaded = _load_or_exit(specs)
    changed = False
    with _environment_errors():
        kubectl = _settings(ctx).kubectl()
        cluster.preflight(kubectl)
        for item in loaded:
            result = cluster.diff(kubectl, render_spec(item.spec))
            typer.echo(f"== {item.spec.name}: {'changes' if result.changed else 'no changes'}")
            if result.changed:
                typer.echo(result.text.rstrip("\n"))
            changed |= result.changed
    if changed:
        raise typer.Exit(1)


@app.command()
def apply(
    ctx: typer.Context,
    specs: SpecPaths,
    dry_run: Annotated[
        bool,
        typer.Option(
            "--dry-run",
            help="Show the changes and validate them server-side without persisting anything.",
        ),
    ] = False,
    force: Annotated[bool, typer.Option(help="Apply even when the diff is empty.")] = False,
) -> None:
    """Create or update everything a spec describes. Re-running changes nothing."""
    loaded = _load_or_exit(specs)
    with _environment_errors():
        kubectl = _settings(ctx).kubectl()
        cluster.preflight(kubectl)
        for item in loaded:
            result = cluster.apply(kubectl, render_spec(item.spec), dry_run=dry_run, force=force)
            if not result.changed:
                typer.echo(f"{item.spec.name}: no changes")
                continue
            typer.echo(f"{item.spec.name}: {'would change (dry run)' if dry_run else 'applied'}")
            for line in result.output.rstrip("\n").splitlines():
                typer.echo(f"  {line}")


def _print_retry(reports: list[ServiceReport], elapsed: float) -> None:
    failing = sum(c.status == "fail" for r in reports for c in r.checks)
    _err(f"  waiting: {failing} check(s) failing after {elapsed:.0f}s, retrying")


@app.command(name="verify")
def verify_command(
    ctx: typer.Context,
    specs: Annotated[
        list[Path] | None,
        typer.Argument(help="Spec files or directories. Omit with --all.", exists=True),
    ] = None,
    all_services: Annotated[
        bool, typer.Option("--all", help="Verify every onboarded service found in the cluster.")
    ] = False,
    timeout: Annotated[
        float, typer.Option(help="Seconds to keep retrying while checks fail; 0 checks once.")
    ] = 180,
) -> None:
    """Check that onboarded services are scraped, their rules loaded and routing in place."""
    if bool(specs) == all_services:
        _exit("error: give spec paths or --all, not both", 2)
    settings = _settings(ctx)
    with _environment_errors():
        kubectl = settings.kubectl()
        if specs:
            expectations = [
                exp
                for item in _load_or_exit(specs)
                for exp in expectations_from_manifests(render_spec(item.spec))
            ]
        else:
            expectations = expectations_from_objects(cluster.managed_objects(kubectl))
            if not expectations:
                _exit("error: no onboarded services found", 1)
        with prom.connect(kubectl, settings.endpoints) as (prometheus, alertmanager):
            reports = verify(
                kubectl, prometheus, alertmanager, expectations, timeout, on_retry=_print_retry
            )
    typer.echo(format_reports(reports))
    if not all(r.ok for r in reports):
        raise typer.Exit(1)


@app.command(name="list")
def list_command(ctx: typer.Context) -> None:
    """Show onboarded services (objects labeled app.kubernetes.io/managed-by=onboardctl)."""
    with _environment_errors():
        services = cluster.onboarded_services(cluster.managed_objects(_settings(ctx).kubectl()))
    if not services:
        typer.echo("no onboarded services")
        return
    rows = [("SERVICE", "NAMESPACE", "TEAM", "OBJECTS", "SPEC", "OWNERS")]
    for svc in services:
        spec_hash = next(iter(svc.spec_hashes)) if len(svc.spec_hashes) == 1 else "mixed"
        rows.append((svc.name, svc.namespace, svc.team, str(svc.objects), spec_hash, svc.owners))
    widths = [max(len(row[i]) for row in rows) for i in range(len(rows[0]) - 1)]
    for row in rows:
        typer.echo(
            "  ".join(cell.ljust(w) for cell, w in zip(row, widths, strict=False)) + "  " + row[-1]
        )


@app.command()
def offboard(
    ctx: typer.Context,
    name: Annotated[str, typer.Argument(help="Service name.")],
    yes: Annotated[bool, typer.Option("--yes", "-y", help="Do not ask for confirmation.")] = False,
    delete_namespace: Annotated[
        bool,
        typer.Option(help="Delete the namespace even if workloads are still running in it."),
    ] = False,
) -> None:
    """Remove everything onboardctl created for a service."""
    with _environment_errors():
        kubectl = _settings(ctx).kubectl()
        if not yes:
            typer.confirm(
                f"Remove all onboardctl objects for {name!r} in context "
                f"{kubectl.current_context()!r}?",
                abort=True,
            )
        result = cluster.offboard(kubectl, name, delete_namespace)
    for item in result.deleted:
        typer.echo(f"deleted {item}")
    if result.namespace and not result.namespace_deleted:
        typer.echo(
            f"kept namespace {result.namespace} (onboardctl labels removed): it still contains "
            f"{', '.join(result.remaining_workloads)}. Use --delete-namespace to remove it anyway."
        )


@app.command()
def backup(
    ctx: typer.Context,
    output: Annotated[
        Path | None,
        typer.Option("--output", "-o", help="New directory to write (default backups/<time>)."),
    ] = None,
) -> None:
    """Export all onboarded configuration as YAML files plus a checksummed index."""
    directory = output or Path("backups") / dr.timestamp(dr.utc_now())
    with _environment_errors():
        result = dr.backup(_settings(ctx).kubectl(), directory)
    typer.echo(
        f"backed up {result.objects} objects for {len(result.services)} services "
        f"to {directory.as_posix()}"
    )


@app.command()
def restore(
    ctx: typer.Context,
    directory: Annotated[Path, typer.Argument(help="Directory written by `backup`.")],
    dry_run: Annotated[bool, typer.Option("--dry-run", help="Server-side dry run only.")] = False,
) -> None:
    """Re-create onboarded configuration from a backup."""
    with _environment_errors():
        output = dr.restore(_settings(ctx).kubectl(), directory, dry_run=dry_run)
    typer.echo(output.rstrip("\n"))


@app.command()
def drill(
    ctx: typer.Context,
    yes: Annotated[bool, typer.Option("--yes", "-y", help="Do not ask for confirmation.")] = False,
    timeout: Annotated[
        float, typer.Option(help="Seconds to wait for each phase (config dropped, recovery).")
    ] = 300,
    backup_dir: Annotated[
        Path | None, typer.Option(help="Where to write the backup (default backups/drill-<time>).")
    ] = None,
    reports_dir: Annotated[Path, typer.Option(help="Where to write the Markdown report.")] = Path(
        "reports"
    ),
    allow_any_context: Annotated[
        bool, typer.Option(help="Allow running against a context that is not a kind cluster.")
    ] = False,
) -> None:
    """Back up, delete, restore and re-verify all onboarded config, timing the recovery."""
    settings = _settings(ctx)
    with _environment_errors():
        kubectl = settings.kubectl()
        context = kubectl.current_context()
        if not context.startswith("kind-") and not allow_any_context:
            _exit(
                f"error: refusing to drill against {context!r}: the drill deletes configuration. "
                "Use a kind cluster or pass --allow-any-context.",
                2,
            )
        if not yes:
            typer.confirm(
                f"The drill deletes and restores all onboarded config in {context!r}. Continue?",
                abort=True,
            )
        started = dr.utc_now()
        directory = backup_dir or Path("backups") / f"drill-{dr.timestamp(started)}"
        with prom.connect(kubectl, settings.endpoints) as (prometheus, alertmanager):
            result = dr.run_drill(kubectl, prometheus, alertmanager, directory, timeout, _err)

    reports_dir.mkdir(parents=True, exist_ok=True)
    report_path = reports_dir / f"drill-{dr.timestamp(result.started)}.md"
    report_path.write_text(dr.drill_report(result), encoding="utf-8", newline="\n")
    if result.reports:
        typer.echo(format_reports(result.reports))
    if result.recovery_seconds is not None:
        typer.echo(
            f"recovered in {result.recovery_seconds:.1f}s after the restore started "
            f"({result.outage_seconds:.1f}s after deletion)"
        )
    typer.echo(f"drill {result.outcome}" + (f": {result.reason}" if result.reason else ""))
    typer.echo(f"report: {report_path.as_posix()}")
    if result.outcome != "PASS":
        raise typer.Exit(1)


def main() -> None:
    # Windows consoles may default to a legacy code page.
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    app()
