#!/bin/bash
set -e

# Colors
GREEN="\033[0;32m"
YELLOW="\033[1;33m"
BLUE="\033[0;34m"
NC="\033[0m"

CLUSTER_NAME="aiops"

echo -e "${GREEN}[STEP] Starting cluster teardown...${NC}"

# ---------------------------------------------------------
# Kill all port-forward processes
# ---------------------------------------------------------
echo -e "${BLUE}[INFO] Killing port-forward processes...${NC}"
pkill -f "kubectl port-forward" 2>/dev/null || true

# ---------------------------------------------------------
# Uninstall Helm releases
# ---------------------------------------------------------
echo -e "${BLUE}[INFO] Uninstalling Helm releases...${NC}"
helm uninstall monitoring -n monitoring --wait 2>/dev/null || true
helm uninstall argocd -n argocd --wait 2>/dev/null || true
helm uninstall chaos-mesh -n chaos-mesh --wait 2>/dev/null || true

# ---------------------------------------------------------
# Delete namespaces
# ---------------------------------------------------------
echo -e "${BLUE}[INFO] Deleting namespaces...${NC}"
kubectl delete namespace app --ignore-not-found=true --timeout=60s || true
kubectl delete namespace monitoring --ignore-not-found=true --timeout=60s || true
kubectl delete namespace argocd --ignore-not-found=true --timeout=60s || true
kubectl delete namespace chaos-mesh --ignore-not-found=true --timeout=60s || true

# ---------------------------------------------------------
# Remove ArgoCD CRDs (force)
# ---------------------------------------------------------
echo -e "${BLUE}[INFO] Removing ArgoCD CRDs...${NC}"

ARGO_CRDS=(
  applications.argoproj.io
  applicationsets.argoproj.io
  appprojects.argoproj.io
)

for crd in "${ARGO_CRDS[@]}"; do
  kubectl delete crd "$crd" --ignore-not-found=true || true
done

# ---------------------------------------------------------
# Remove Prometheus CRDs
# ---------------------------------------------------------
echo -e "${BLUE}[INFO] Removing Prometheus CRDs...${NC}"

PROM_CRDS=(
  alertmanagerconfigs.monitoring.coreos.com
  alertmanagers.monitoring.coreos.com
  podmonitors.monitoring.coreos.com
  probes.monitoring.coreos.com
  prometheuses.monitoring.coreos.com
  prometheusrules.monitoring.coreos.com
  servicemonitors.monitoring.coreos.com
  thanosrulers.monitoring.coreos.com
)

for crd in "${PROM_CRDS[@]}"; do
  kubectl delete crd "$crd" --ignore-not-found=true || true
done

# ---------------------------------------------------------
# Remove Chaos Mesh CRDs
# ---------------------------------------------------------
echo -e "${BLUE}[INFO] Removing Chaos Mesh CRDs...${NC}"

CHAOS_CRDS=(
  podchaos.chaos-mesh.org
  iochaos.chaos-mesh.org
  networkchaos.chaos-mesh.org
  stresschaos.chaos-mesh.org
  timechaos.chaos-mesh.org
)

for crd in "${CHAOS_CRDS[@]}"; do
  kubectl delete crd "$crd" --ignore-not-found=true || true
done

# ---------------------------------------------------------
# Remove stuck finalizers
# ---------------------------------------------------------
echo -e "${BLUE}[INFO] Removing stuck CRD finalizers...${NC}"

for crd in $(kubectl get crd -o name 2>/dev/null); do
  kubectl patch "$crd" -p '{"metadata":{"finalizers":[]}}' --type=merge 2>/dev/null || true
done

# ---------------------------------------------------------
# Remove stuck webhooks
# ---------------------------------------------------------
echo -e "${BLUE}[INFO] Removing leftover webhooks...${NC}"

kubectl delete validatingwebhookconfiguration --all 2>/dev/null || true
kubectl delete mutatingwebhookconfiguration --all 2>/dev/null || true

# ---------------------------------------------------------
# Delete k3d cluster
# ---------------------------------------------------------
echo -e "${YELLOW}[ACTION] Deleting k3d cluster '${CLUSTER_NAME}'...${NC}"
k3d cluster delete $CLUSTER_NAME --wait || true

echo -e "${GREEN}[DONE] Cluster teardown complete.${NC}"

