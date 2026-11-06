#!/usr/bin/env bash
# Create or delete a local kind cluster running a pinned kube-prometheus-stack.
#
#   scripts/dev-cluster.sh up      create the cluster (if missing) and install the stack
#   scripts/dev-cluster.sh down    delete the cluster
#   scripts/dev-cluster.sh status  show nodes and monitoring pods
#
# Works from WSL2 and from Git Bash on Windows. Requires docker, kind,
# kubectl and helm on PATH.
set -euo pipefail

CLUSTER_NAME="${CLUSTER_NAME:-onboardctl}"
MON_NS="monitoring"
KPS_RELEASE="kps"
KPS_CHART="oci://ghcr.io/prometheus-community/charts/kube-prometheus-stack"
KPS_CHART_VERSION="92.21.1"

# Under Git Bash, kind and helm are Windows executables and need C:/... paths.
# `pwd -W` prints that form there and fails harmlessly everywhere else.
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && { pwd -W 2>/dev/null || pwd; })"

log() { printf '[%s] %s\n' "$(date -u +%H:%M:%S)" "$*" >&2; }
die() { log "error: $*"; exit 1; }

require() {
  local tool
  for tool in "$@"; do
    command -v "$tool" >/dev/null 2>&1 || die "$tool not found on PATH"
  done
}

cluster_exists() {
  # kind.exe prints CRLF line endings under Git Bash.
  kind get clusters 2>/dev/null | tr -d '\r' | grep -qx "$CLUSTER_NAME"
}

up() {
  require docker kind kubectl helm
  docker info >/dev/null 2>&1 || die "docker is not running"

  if cluster_exists; then
    log "kind cluster '$CLUSTER_NAME' already exists"
  else
    log "creating kind cluster '$CLUSTER_NAME'"
    kind create cluster --name "$CLUSTER_NAME" --config "$REPO_ROOT/cluster/kind.yaml" --wait 120s
  fi
  kubectl config use-context "kind-$CLUSTER_NAME" >/dev/null

  log "installing kube-prometheus-stack $KPS_CHART_VERSION (the first run takes a few minutes)"
  helm upgrade --install "$KPS_RELEASE" "$KPS_CHART" \
    --version "$KPS_CHART_VERSION" \
    --namespace "$MON_NS" --create-namespace \
    --values "$REPO_ROOT/cluster/kube-prometheus-stack-values.yaml" \
    --wait --timeout 15m

  # helm --wait covers Deployments; Prometheus and Alertmanager are
  # StatefulSets created later by the operator.
  log "waiting for Prometheus and Alertmanager"
  wait_for_pod "app.kubernetes.io/name=prometheus"
  wait_for_pod "app.kubernetes.io/name=alertmanager"
  log "ready. kubectl context: kind-$CLUSTER_NAME"
}

wait_for_pod() {
  local selector="$1"
  for _ in $(seq 1 60); do
    if kubectl get pods -n "$MON_NS" -l "$selector" -o name 2>/dev/null | grep -q .; then
      kubectl wait pods -n "$MON_NS" -l "$selector" --for=condition=Ready --timeout=300s >/dev/null
      return 0
    fi
    sleep 5
  done
  die "no pod matching $selector appeared in namespace $MON_NS"
}

down() {
  require kind
  if cluster_exists; then
    kind delete cluster --name "$CLUSTER_NAME"
  else
    log "kind cluster '$CLUSTER_NAME' does not exist"
  fi
}

status() {
  require kubectl
  kubectl --context "kind-$CLUSTER_NAME" get nodes
  kubectl --context "kind-$CLUSTER_NAME" get pods -n "$MON_NS"
}

case "${1:-up}" in
  up) up ;;
  down) down ;;
  status) status ;;
  *) die "usage: $0 [up|down|status]" ;;
esac
