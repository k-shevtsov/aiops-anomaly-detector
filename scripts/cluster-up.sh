#!/bin/bash
set -e
trap 'echo -e "\033[0;31m[ERROR]\033[0m Script failed. Press Enter to exit..."; read' ERR

# ---------------------------------------------------------
# Colors for readable output
# ---------------------------------------------------------
GREEN="\033[0;32m"
YELLOW="\033[1;33m"
BLUE="\033[0;34m"
NC="\033[0m" # no color

# ---------------------------------------------------------
# Determine repository root (one level above scripts/)
# ---------------------------------------------------------
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

echo -e "${BLUE}[INFO] Script directory: $SCRIPT_DIR${NC}"
echo -e "${BLUE}[INFO] Repository root: $REPO_ROOT${NC}"

# Paths to values and manifests
VALUES_PATH="$REPO_ROOT/infra/helm-values/prometheus-values.yaml"
VICTIM_MANIFESTS="$REPO_ROOT/infra/k8s/victim-service/"

CLUSTER_NAME="aiops"

echo -e "${GREEN}[STEP] Starting cluster setup...${NC}"

# ---------------------------------------------------------
# Local Helm chart storage (offline installation)
# ---------------------------------------------------------
LOCAL_HELM_DIR="$HOME/helm-local"

echo -e "${BLUE}[INFO] Ensuring local Helm chart directory exists at $LOCAL_HELM_DIR...${NC}"
mkdir -p "$LOCAL_HELM_DIR"

# Download charts once (offline mode afterwards)
if ! ls "$LOCAL_HELM_DIR"/kube-prometheus-stack-*.tgz >/dev/null 2>&1; then
  echo -e "${BLUE}[INFO] Downloading kube-prometheus-stack chart...${NC}"
  helm pull prometheus-community/kube-prometheus-stack -d "$LOCAL_HELM_DIR"
fi

if ! ls "$LOCAL_HELM_DIR"/argo-cd-*.tgz >/dev/null 2>&1; then
  echo -e "${BLUE}[INFO] Downloading argo-cd chart...${NC}"
  helm pull argo/argo-cd -d "$LOCAL_HELM_DIR"
fi

if ! ls "$LOCAL_HELM_DIR"/chaos-mesh-*.tgz >/dev/null 2>&1; then
  echo -e "${BLUE}[INFO] Downloading chaos-mesh chart...${NC}"
  helm pull chaos-mesh/chaos-mesh -d "$LOCAL_HELM_DIR"
fi

# ---------------------------------------------------------
# Recreate k3d cluster
# ---------------------------------------------------------
echo -e "${YELLOW}[ACTION] Recreating k3d cluster '${CLUSTER_NAME}'...${NC}"
k3d cluster delete $CLUSTER_NAME 2>/dev/null || true

k3d cluster create $CLUSTER_NAME \
  --port "80:80@loadbalancer" \
  --agents 2

# ---------------------------------------------------------
# Create namespace and import local Docker image
# ---------------------------------------------------------
echo -e "${BLUE}[INFO] Creating namespace 'app'...${NC}"
kubectl create namespace app 2>/dev/null || true

echo -e "${BLUE}[INFO] Importing local Docker image into cluster...${NC}"
k3d image import victim-service:local -c $CLUSTER_NAME

# ---------------------------------------------------------
# Install kube-prometheus-stack (offline)
# ---------------------------------------------------------
echo -e "${GREEN}[STEP] Installing monitoring stack (kube-prometheus-stack)...${NC}"
helm upgrade --install monitoring "$LOCAL_HELM_DIR"/kube-prometheus-stack-*.tgz \
  --namespace monitoring --create-namespace \
  --values "$VALUES_PATH" \
  --timeout 12m

# ---------------------------------------------------------
# Install ArgoCD (minimal version)
#
# Why minimal:
# - Dex is disabled (no SSO)
# - Redis is disabled (lighter footprint)
# - Notifications disabled
# - Faster installation, fewer CRDs, fewer webhooks
#
# Why single release:
# - ArgoCD Helm chart does NOT support CRD-only release
#   (CRD-only mode still creates resources and breaks ownership)
# - Therefore we install everything in one Helm release
#   with extended timeout to allow CRD registration
# ---------------------------------------------------------

echo -e "${GREEN}[STEP] Installing ArgoCD (minimal configuration)...${NC}"

helm upgrade --install argocd "$LOCAL_HELM_DIR"/argo-cd-*.tgz \
  --namespace argocd --create-namespace \
  --set server.service.type=ClusterIP \
  --set dex.enabled=false \
  --set redis.enabled=false \
  --set notifications.enabled=false \
  --set controller.enableStatefulSet=false \
  --timeout 15m

# ---------------------------------------------------------
# Install Chaos Mesh (offline)
# ---------------------------------------------------------
echo -e "${GREEN}[STEP] Installing Chaos Mesh...${NC}"
helm upgrade --install chaos-mesh "$LOCAL_HELM_DIR"/chaos-mesh-*.tgz \
  --namespace chaos-mesh --create-namespace \
  --set chaosDaemon.runtime=containerd \
  --set chaosDaemon.socketPath=/run/k3s/containerd/containerd.sock \
  --timeout 8m

# ---------------------------------------------------------
# Deploy victim-service
# ---------------------------------------------------------
echo -e "${GREEN}[STEP] Applying victim-service manifests...${NC}"
kubectl apply -f "$VICTIM_MANIFESTS"

echo -e "${BLUE}[INFO] Waiting for victim-service pods to become Ready...${NC}"
kubectl wait --for=condition=Ready pod \
  -l app=victim-service -n app --timeout=120s

# ---------------------------------------------------------
# Final message
# ---------------------------------------------------------
echo -e "${GREEN}[DONE] Cluster is ready.${NC}"
echo -e "${BLUE}To access services, run:${NC}"
echo "  kubectl port-forward svc/monitoring-kube-prometheus-prometheus -n monitoring 9090:9090 &"
echo "  kubectl port-forward svc/victim-service -n app 8000:80 &"

