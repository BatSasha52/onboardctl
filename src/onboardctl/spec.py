"""The service spec: the one YAML file a team writes to onboard a service.

Loading runs three passes, and each reports problems with the path of the
field at fault: YAML parsing, schema validation (pydantic), and checks across
fields that the schema cannot express.
"""

from __future__ import annotations

import difflib
import hashlib
import re
import types
from dataclasses import dataclass, field
from pathlib import Path
from typing import Annotated, Any, Literal, Union, get_args, get_origin

import yaml
from pydantic import AfterValidator, BaseModel, ConfigDict, Field, ValidationError, field_validator
from pydantic.alias_generators import to_camel

from onboardctl import slo

API_VERSION = "onboardctl/v1"
NAME_MAX_LENGTH = 40

# The platform's own namespaces and the Kubernetes system ones.
RESERVED_NAMESPACES = frozenset(
    {"default", "kube-system", "kube-public", "kube-node-lease", "monitoring", "onboardctl-system"}
)

# Added to the routing config when a spec opts in to automatic remediation.
RESPONDER_RECEIVER = "onboardctl-responder"

_DNS_LABEL = re.compile(r"^[a-z0-9]([-a-z0-9]*[a-z0-9])?$")
_METRIC_NAME = re.compile(r"^[a-zA-Z_:][a-zA-Z0-9_:]*$")
_MATCHER = r'[a-zA-Z_][a-zA-Z0-9_]*\s*(?:=~|!~|!=|=)\s*"(?:[^"\\]|\\.)*"'
_MATCHER_LIST = re.compile(rf"^{_MATCHER}(?:\s*,\s*{_MATCHER})*$")
_DURATION = re.compile(r"^([1-9][0-9]*)([sm])$")
_QUANTITY = re.compile(r"^[0-9]+(\.[0-9]+)?(m|k|Ki|M|Mi|G|Gi|T|Ti)?$")
_EMAIL = re.compile(r"^[^@\s]+@[^@\s]+\.[A-Za-z]{2,}$")
_GITHUB_HANDLE = re.compile(r"^@[A-Za-z0-9][A-Za-z0-9-]*(/[A-Za-z0-9][A-Za-z0-9._-]*)?$")
_URL = re.compile(r"^https?://[^\s/?#]+[^\s]*$")
_LABEL_KEY = re.compile(
    r"^([a-z0-9]([-a-z0-9.]*[a-z0-9])?/)?[A-Za-z0-9]([-A-Za-z0-9_.]*[A-Za-z0-9])?$"
)
_LABEL_VALUE = re.compile(r"^([A-Za-z0-9]([-A-Za-z0-9_.]*[A-Za-z0-9])?)?$")


# --- field checks -----------------------------------------------------------


def _dns_label(value: str, max_length: int = 63) -> str:
    if len(value) > max_length:
        raise ValueError(f"must be at most {max_length} characters (got {len(value)})")
    if not _DNS_LABEL.match(value):
        raise ValueError(
            f"{value!r} is not a valid name: use lowercase letters, digits and '-', "
            "starting and ending with a letter or digit"
        )
    return value


def _service_name(value: str) -> str:
    # Leaves room for the suffixes added to object names, such as "-slo-dashboard".
    return _dns_label(value, NAME_MAX_LENGTH)


def _owner(value: str) -> str:
    if not (_EMAIL.match(value) or _GITHUB_HANDLE.match(value)):
        raise ValueError(
            f"{value!r} is neither an email address nor a GitHub handle such as @org/team"
        )
    return value


def _percent_target(value: float) -> float:
    if not 0 < value < 100:
        raise ValueError(
            f"must be greater than 0 and less than 100 (got {value:g}); it is a percentage, "
            "and 100 would leave no error budget"
        )
    return value


def _url(value: str) -> str:
    if not _URL.match(value):
        raise ValueError(f"{value!r} is not an http:// or https:// URL")
    return value


DnsLabel = Annotated[str, AfterValidator(_dns_label)]
Owner = Annotated[str, AfterValidator(_owner)]
Target = Annotated[float, AfterValidator(_percent_target)]
Url = Annotated[str, AfterValidator(_url)]


# --- models -----------------------------------------------------------------


class _Model(BaseModel):
    # Spec keys are camelCase like Kubernetes manifests; unknown keys are
    # errors so that a typo does not silently fall back to a default.
    model_config = ConfigDict(extra="forbid", alias_generator=to_camel, populate_by_name=True)


class MetricsEndpoint(_Model):
    port: str
    path: str = "/metrics"
    interval: str = "30s"
    selector: dict[str, str] = Field(default_factory=dict)

    @field_validator("port", mode="before")
    @classmethod
    def _port_is_not_a_number(cls, value: Any) -> Any:
        if isinstance(value, int) and not isinstance(value, bool):
            raise ValueError(
                f"got the number {value}; a ServiceMonitor selects a Service port by name, "
                "so give the port's name instead"
            )
        return value

    @field_validator("port")
    @classmethod
    def _port_name(cls, value: str) -> str:
        if (
            len(value) > 15
            or not _DNS_LABEL.match(value)
            or "--" in value
            or not re.search("[a-z]", value)
        ):
            raise ValueError(
                f"{value!r} is not a valid Service port name: at most 15 characters of "
                "lowercase letters, digits and '-', with at least one letter"
            )
        return value

    @field_validator("path")
    @classmethod
    def _path(cls, value: str) -> str:
        if not value.startswith("/") or any(c.isspace() for c in value):
            raise ValueError(f"{value!r} must start with '/' and contain no spaces")
        return value

    @field_validator("interval")
    @classmethod
    def _interval(cls, value: str) -> str:
        match = _DURATION.match(value)
        if not match:
            raise ValueError(f"{value!r} is not a duration in seconds or minutes, such as 30s")
        seconds = int(match[1]) * (60 if match[2] == "m" else 1)
        if not 10 <= seconds <= 300:
            raise ValueError(f"must be between 10s and 5m (got {value})")
        return value

    @field_validator("selector")
    @classmethod
    def _selector(cls, value: dict[str, str]) -> dict[str, str]:
        for key, label_value in value.items():
            if not _LABEL_KEY.match(key):
                raise ValueError(f"{key!r} is not a valid Kubernetes label key")
            if len(label_value) > 63 or not _LABEL_VALUE.match(label_value):
                raise ValueError(f"{label_value!r} (for {key}) is not a valid label value")
        return value


class SliMetrics(_Model):
    requests: str = "http_requests_total"
    error_selector: str = 'code=~"5.."'
    latency_histogram: str = "http_request_duration_seconds"

    @field_validator("requests", "latency_histogram")
    @classmethod
    def _metric_name(cls, value: str) -> str:
        if not _METRIC_NAME.match(value):
            raise ValueError(f"{value!r} is not a valid Prometheus metric name")
        return value

    @field_validator("latency_histogram")
    @classmethod
    def _histogram_base_name(cls, value: str) -> str:
        for suffix in ("_bucket", "_count", "_sum"):
            if value.endswith(suffix):
                raise ValueError(
                    f"give the histogram's base name without {suffix!r}; the rules add the suffixes"
                )
        return value

    @field_validator("error_selector")
    @classmethod
    def _matchers(cls, value: str) -> str:
        if not _MATCHER_LIST.match(value.strip()):
            raise ValueError(
                f'{value!r} is not a list of PromQL label matchers such as code=~"5.."'
            )
        return value.strip()


class Availability(_Model):
    target: Target


class Latency(_Model):
    threshold_seconds: float = Field(gt=0, le=60)
    target: Target


class Slo(_Model):
    window: Literal["7d", "28d", "30d"] = "30d"
    availability: Availability
    latency: Latency | None = None
    metrics: SliMetrics = Field(default_factory=SliMetrics)


class Receiver(_Model):
    name: DnsLabel
    webhook_url: Url | None = None


class Route(_Model):
    severity: Literal["critical", "warning"]
    receiver: str


class Alerting(_Model):
    default_receiver: str
    receivers: list[Receiver] = Field(min_length=1)
    routes: list[Route] = Field(default_factory=list)
    runbook_url: Url | None = None


class Dashboard(_Model):
    enabled: bool = True


class Quota(_Model):
    cpu: str = "2"
    memory: str = "4Gi"
    pods: int = Field(default=20, ge=1, le=500)

    @field_validator("cpu", "memory", mode="before")
    @classmethod
    def _number_to_string(cls, value: Any) -> Any:
        # YAML reads `cpu: 2` as an integer; Kubernetes quantities are strings.
        if isinstance(value, int | float) and not isinstance(value, bool):
            return str(value)
        return value

    @field_validator("cpu", "memory")
    @classmethod
    def _quantity(cls, value: str) -> str:
        if not _QUANTITY.match(value):
            raise ValueError(f"{value!r} is not a Kubernetes quantity such as 500m, 2 or 4Gi")
        return value


class Remediation(_Model):
    # The only remediation offered: `kubectl rollout restart` of this
    # Deployment when the fast availability burn-rate alert fires. See
    # responder/README.md for why it is limited to that.
    restart_deployment: DnsLabel


class ServiceSpec(_Model):
    api_version: Literal["onboardctl/v1"]
    name: Annotated[str, AfterValidator(_service_name)]
    team: DnsLabel
    owners: list[Owner] = Field(min_length=1)
    description: str | None = Field(default=None, max_length=200)
    namespace: DnsLabel | None = None
    metrics: MetricsEndpoint
    slo: Slo
    alerting: Alerting
    dashboard: Dashboard = Field(default_factory=Dashboard)
    quota: Quota = Field(default_factory=Quota)
    remediation: Remediation | None = None

    @property
    def target_namespace(self) -> str:
        return self.namespace or self.name

    @property
    def selector(self) -> dict[str, str]:
        return self.metrics.selector or {"app.kubernetes.io/name": self.name}

    def spec_hash(self) -> str:
        """Short, stable fingerprint of the spec, stored on every generated object."""
        canonical = self.model_dump_json(by_alias=True)
        return hashlib.sha256(canonical.encode()).hexdigest()[:16]


# --- problems and loading ---------------------------------------------------


@dataclass(frozen=True)
class Problem:
    path: str  # dotted field path, empty for the whole file
    message: str
    example: str | None = None

    def __str__(self) -> str:
        where = f"{self.path}: " if self.path else ""
        text = f"{where}{self.message}"
        if self.example:
            text += f"\n    example: {self.example}"
        return text


class SpecError(Exception):
    def __init__(self, source: str, problems: list[Problem]) -> None:
        super().__init__(f"{source}: {len(problems)} problem(s)")
        self.source = source
        self.problems = problems


@dataclass(frozen=True)
class LoadedSpec:
    path: Path
    spec: ServiceSpec
    warnings: list[str] = field(default_factory=list)


# Valid input for each field, shown next to the error. List indices in the
# path are written as "*".
EXAMPLES: dict[str, str] = {
    "": "see examples/api-strict.yaml for a complete spec",
    "apiVersion": "apiVersion: onboardctl/v1",
    "name": "name: checkout-api",
    "team": "team: payments",
    "owners": 'owners: ["alice@example.com", "@example-org/payments"]',
    "owners.*": "- alice@example.com   (or a GitHub handle such as @example-org/payments)",
    "description": "description: Public checkout API",
    "namespace": "namespace: payments-checkout",
    "metrics": "metrics: {port: http-metrics, path: /metrics}",
    "metrics.port": "port: http-metrics   (the name of the Service port)",
    "metrics.path": "path: /metrics",
    "metrics.interval": "interval: 30s",
    "metrics.selector": "selector: {app.kubernetes.io/name: checkout-api}",
    "slo": "slo: {availability: {target: 99.9}}",
    "slo.window": "window: 30d   (one of 7d, 28d, 30d)",
    "slo.availability": "availability: {target: 99.9}",
    "slo.availability.target": "target: 99.9",
    "slo.latency": "latency: {thresholdSeconds: 0.25, target: 99}",
    "slo.latency.thresholdSeconds": "thresholdSeconds: 0.25   (must be a histogram bucket bound)",
    "slo.latency.target": "target: 99",
    "slo.metrics.requests": "requests: http_requests_total",
    "slo.metrics.errorSelector": "errorSelector: 'code=~\"5..\"'",
    "slo.metrics.latencyHistogram": "latencyHistogram: http_request_duration_seconds",
    "alerting": "alerting: {defaultReceiver: team-tickets, receivers: [{name: team-tickets}]}",
    "alerting.defaultReceiver": "defaultReceiver: payments-tickets",
    "alerting.receivers": "receivers: [{name: payments-tickets}]",
    "alerting.receivers.*.name": "name: payments-pager",
    "alerting.receivers.*.webhookUrl": "webhookUrl: http://alert-relay.payments.svc:8080/hook",
    "alerting.routes.*.severity": "severity: critical   (critical or warning)",
    "alerting.routes.*.receiver": "receiver: payments-pager   (a name from alerting.receivers)",
    "alerting.runbookUrl": "runbookUrl: https://runbooks.example.com/checkout-api",
    "dashboard.enabled": "enabled: true",
    "quota.cpu": 'cpu: "2"',
    "quota.memory": "memory: 4Gi",
    "quota.pods": "pods: 20",
    "remediation.restartDeployment": "restartDeployment: checkout-api   (a Deployment name)",
}


def _format_path(loc: tuple[str | int, ...]) -> str:
    out = ""
    for part in loc:
        out += f"[{part}]" if isinstance(part, int) else (f".{part}" if out else part)
    return out


def _example_key(loc: tuple[str | int, ...]) -> str:
    return ".".join("*" if isinstance(part, int) else part for part in loc)


def _model_in(annotation: Any) -> type[BaseModel] | None:
    """The pydantic model inside an annotation such as `list[Receiver] | None`."""
    if isinstance(annotation, type) and issubclass(annotation, BaseModel):
        return annotation
    if get_origin(annotation) in (list, Union, types.UnionType, Annotated):
        for arg in get_args(annotation):
            model = _model_in(arg)
            if model is not None:
                return model
    return None


def _fields_at(loc: tuple[str | int, ...]) -> list[str]:
    model: type[BaseModel] | None = ServiceSpec
    for part in loc:
        if isinstance(part, int):
            continue
        if model is None:
            return []
        info = next((f for name, f in model.model_fields.items() if part in (name, f.alias)), None)
        model = _model_in(info.annotation) if info else None
    if model is None:
        return []
    return [f.alias or name for name, f in model.model_fields.items()]


def _problem_from_error(error: Any) -> Problem:
    loc: tuple[str | int, ...] = tuple(error["loc"])
    path = _format_path(loc)
    kind = error["type"]
    if kind == "extra_forbidden":
        valid = _fields_at(loc[:-1])
        close = difflib.get_close_matches(str(loc[-1]), valid, n=1, cutoff=0.6)
        hint = f"; did you mean '{close[0]}'?" if close else ""
        listing = f" Valid fields here: {', '.join(valid)}." if valid else ""
        return Problem(path, f"unknown field{hint}{listing}")
    if kind == "missing":
        message = "required field is missing"
    elif kind == "value_error":
        message = str(error["ctx"]["error"])
    else:
        message = error["msg"][0].lower() + error["msg"][1:]
        value = error.get("input")
        if isinstance(value, str | int | float | bool):
            message += f" (got {value!r})"
    return Problem(path, message, EXAMPLES.get(_example_key(loc)))


class _UniqueKeyLoader(yaml.SafeLoader):
    """SafeLoader that rejects duplicate keys instead of keeping the last one."""

    def construct_mapping(self, node: yaml.MappingNode, deep: bool = False) -> dict[Any, Any]:
        seen: set[Any] = set()
        for key_node, _ in node.value:
            key = self.construct_object(key_node, deep=deep)
            if key in seen:
                raise yaml.constructor.ConstructorError(
                    None, None, f"duplicate key {key!r}", key_node.start_mark
                )
            seen.add(key)
        return super().construct_mapping(node, deep)


def parse_yaml(text: str, source: str) -> dict[str, Any]:
    try:
        data = yaml.load(text, Loader=_UniqueKeyLoader)  # noqa: S506 (safe loader subclass)
    except yaml.MarkedYAMLError as exc:
        mark = exc.problem_mark
        where = f"line {mark.line + 1}, column {mark.column + 1}: " if mark else ""
        raise SpecError(source, [Problem("", f"{where}{exc.problem}", None)]) from exc
    except yaml.YAMLError as exc:
        raise SpecError(source, [Problem("", f"not valid YAML: {exc}")]) from exc
    if not isinstance(data, dict):
        raise SpecError(source, [Problem("", "the file must contain a YAML mapping", EXAMPLES[""])])
    return data


def cross_field_problems(spec: ServiceSpec) -> list[Problem]:
    problems: list[Problem] = []
    alerting = spec.alerting
    names = [r.name for r in alerting.receivers]

    for index, name in enumerate(names):
        if name in names[:index]:
            problems.append(
                Problem(f"alerting.receivers[{index}].name", f"duplicate receiver {name!r}")
            )
        if name == RESPONDER_RECEIVER:
            problems.append(
                Problem(
                    f"alerting.receivers[{index}].name",
                    f"{name!r} is reserved for the platform's remediation responder",
                    EXAMPLES["alerting.receivers.*.name"],
                )
            )

    defined = ", ".join(sorted(set(names)))
    if alerting.default_receiver not in names:
        problems.append(
            Problem(
                "alerting.defaultReceiver",
                f"receiver {alerting.default_receiver!r} is not defined in alerting.receivers "
                f"(defined: {defined})",
                EXAMPLES["alerting.defaultReceiver"],
            )
        )
    seen_severities: set[str] = set()
    for index, route in enumerate(alerting.routes):
        if route.receiver not in names:
            problems.append(
                Problem(
                    f"alerting.routes[{index}].receiver",
                    f"receiver {route.receiver!r} is not defined in alerting.receivers "
                    f"(defined: {defined})",
                    EXAMPLES["alerting.routes.*.receiver"],
                )
            )
        if route.severity in seen_severities:
            problems.append(
                Problem(
                    f"alerting.routes[{index}].severity",
                    f"severity {route.severity!r} is routed more than once; "
                    "only the first route would ever match",
                )
            )
        seen_severities.add(route.severity)

    if spec.target_namespace in RESERVED_NAMESPACES:
        field_name = "namespace" if spec.namespace else "name"
        problems.append(
            Problem(
                field_name,
                f"namespace {spec.target_namespace!r} is reserved for the platform or Kubernetes"
                + ("" if spec.namespace else " (the namespace defaults to the service name)"),
                EXAMPLES["namespace"],
            )
        )
    return problems


def warnings_for(spec: ServiceSpec) -> list[str]:
    """Things that are valid but probably not what the team wants."""
    warnings: list[str] = []
    slis: list[tuple[str, float]] = [("availability", spec.slo.availability.target)]
    if spec.slo.latency:
        slis.append(("latency", spec.slo.latency.target))
    for sli, target in slis:
        for tier, threshold in slo.unreachable_tiers(target, spec.slo.window):
            warnings.append(
                f"slo.{sli}.target: with {target:g}% over {spec.slo.window}, the "
                f"{tier.long_window}/{tier.short_window} burn-rate alert needs an error ratio "
                f"of {slo.fmt(threshold * 100)}%, which can never happen; it will not fire"
            )
    routed = {r.receiver for r in spec.alerting.routes} | {spec.alerting.default_receiver}
    for receiver in spec.alerting.receivers:
        if receiver.name in routed and receiver.webhook_url is None:
            warnings.append(
                f"alerting.receivers: {receiver.name!r} has no webhookUrl, so alerts routed "
                "to it are accepted by Alertmanager but not delivered anywhere"
            )
    return warnings


def parse_spec(data: dict[str, Any], source: str) -> ServiceSpec:
    try:
        spec = ServiceSpec.model_validate(data)
    except ValidationError as exc:
        raise SpecError(source, [_problem_from_error(e) for e in exc.errors()]) from exc
    problems = cross_field_problems(spec)
    if problems:
        raise SpecError(source, problems)
    return spec


def load_spec(path: Path) -> LoadedSpec:
    source = path.as_posix()
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise SpecError(source, [Problem("", f"cannot read file: {exc.strerror}")]) from exc
    spec = parse_spec(parse_yaml(text, source), source)
    return LoadedSpec(path, spec, warnings_for(spec))


def collect_spec_files(paths: list[Path]) -> list[Path]:
    """Expand directories to the YAML files directly inside them, sorted by name."""
    files: list[Path] = []
    for path in paths:
        if path.is_dir():
            files.extend(sorted(p for p in path.iterdir() if p.suffix in (".yaml", ".yml")))
        else:
            files.append(path)
    return files


def duplicate_problems(specs: list[LoadedSpec]) -> list[str]:
    """Names and namespaces must be unique across all specs given together."""
    messages: list[str] = []
    by_name: dict[str, Path] = {}
    by_namespace: dict[str, Path] = {}
    for loaded in specs:
        spec = loaded.spec
        if spec.name in by_name:
            messages.append(
                f"{loaded.path.as_posix()}: service name {spec.name!r} is already used by "
                f"{by_name[spec.name].as_posix()}"
            )
        if spec.target_namespace in by_namespace:
            messages.append(
                f"{loaded.path.as_posix()}: namespace {spec.target_namespace!r} is already used by "
                f"{by_namespace[spec.target_namespace].as_posix()}"
            )
        by_name.setdefault(spec.name, loaded.path)
        by_namespace.setdefault(spec.target_namespace, loaded.path)
    return messages
