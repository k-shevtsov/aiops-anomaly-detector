#!/usr/bin/env bash
set -euo pipefail

CLUSTER_NAME="aiops"
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "${REPO_ROOT}"

echo "[STEP] Building victim-service Docker image..."
docker build -t victim-service:local ./infra/k8s/victim-service

echo "[STEP] Recreating k3d cluster '${CLUSTER_NAME}'..."
if k3d cluster list | grep -q "^${CLUSTER_NAME}"; then
  k3d cluster delete "${CLUSTER_NAME}" || true
fi

k3d cluster create "${CLUSTER_NAME}" \
  --agents 2 \
  --port "80:80@loadbalancer" \
  --k3s-arg "--disable=traefik@server:0" \
  --wait

echo "[STEP] Importing victim-service image into cluster..."
k3d image import victim-service:local -c "${CLUSTER_NAME}"

echo "[STEP] Creating namespaces..."
kubectl create namespace app        --dry-run=client -o yaml | kubectl apply -f -
kubectl create namespace ai-engine  --dry-run=client -o yaml | kubectl apply -f -
kubectl create namespace monitoring --dry-run=client -o yaml | kubectl apply -f -

echo "[STEP] Installing kube-prometheus-stack..."
helm repo add prometheus-community https://prometheus-community.github.io/helm-charts 2>/dev/null || true
helm repo update
helm upgrade --install monitoring prometheus-community/kube-prometheus-stack \
  --namespace monitoring --create-namespace \
  --wait --timeout 5m

echo "[STEP] Installing ArgoCD..."
helm repo add argo https://argoproj.github.io/argo-helm 2>/dev/null || true
helm repo update
helm upgrade --install argocd argo/argo-cd \
  --namespace argocd --create-namespace

echo "[STEP] Installing Chaos Mesh..."
helm repo add chaos-mesh https://charts.chaos-mesh.org 2>/dev/null || true
helm repo update
helm upgrade --install chaos-mesh chaos-mesh/chaos-mesh \
  --namespace chaos-mesh --create-namespace

echo "[STEP] Applying victim-service manifests..."
kubectl apply -f infra/k8s/victim-service/

echo "[INFO] Waiting for victim-service pods..."
kubectl wait --for=condition=ready pod \
  -l app=victim-service -n app --timeout=180s || {
  echo "[WARN] Pods not Ready after timeout:"
  kubectl get pods -n app
  kubectl describe pod -n app -l app=victim-service || true
  kubectl logs -n app -l app=victim-service --tail=50 || true
  exit 1
}

echo "[STEP] Starting port-forwards..."
./scripts/port-forwards.sh &>/dev/null &

echo "[DONE] Cluster is up. victim-service deployed."
