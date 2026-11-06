# Azure reference module (not deployed)

`modules/aks-observability` describes what the local kind cluster stands in
for on Azure: an AKS cluster whose container logs and control plane logs go
to a Log Analytics workspace. `examples/basic` shows how it would be called.

**This is a validated reference module. It has never been applied to an Azure
subscription, by CI or by hand.** CI runs only `terraform fmt -check`,
`terraform init -backend=false`, `terraform validate` and `tflint` with the
azurerm ruleset (see `.github/workflows/terraform.yml`). Those checks catch
syntax errors, wrong argument names and types, and known invalid values;
they cannot catch quota limits, region availability, policy denials or
anything else only Azure would report at apply time.

## What it creates

| Resource | Notes |
| --- | --- |
| Resource group | Holds everything below. |
| Log Analytics workspace | 30-day retention and a daily ingestion cap by default, as a cost guard. |
| AKS cluster | Entra ID auth with Azure RBAC, local accounts disabled, workload identity and OIDC issuer enabled, Azure CNI overlay with Cilium (which enforces NetworkPolicy), Container Insights agent sending to the workspace. |
| System node pool | Only critical add-ons, spread over zones. |
| Workload node pool | Autoscaled; where kube-prometheus-stack and the teams' services would run. |
| Diagnostic setting | Sends API server, audit, controller manager and guard logs to the workspace. |

The module does not install kube-prometheus-stack or onboardctl's
configuration; on a real platform that would be done by a GitOps tool
(for example Flux or Argo CD) after the cluster exists. Long-term metric
storage (Thanos or Azure Monitor managed Prometheus) is also out of scope
here; see "Disaster recovery" in the top-level README.

## Running the checks locally

```bash
cd terraform
terraform fmt -check -recursive
(cd modules/aks-observability && terraform init -backend=false && terraform validate)
(cd examples/basic && terraform init -backend=false && terraform validate)
tflint --init && tflint --recursive
```

`init` downloads the azurerm provider (about 100 MB) but needs no Azure
credentials.
