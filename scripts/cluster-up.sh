#!/bin/bash
set -e

CLUSTER_NAME="aiops"
echo "🚀 Starting aiops cluster..."

k3d cluster delete $CLUSTER_NAME 2>/dev/null || true
k3d cluster create $CLUSTER_NAME \
  --port "80:80@loadbalancer" \
  --agents 2

kubectl create namespace app 2>/dev/null || true
k3d image import victim-service:local -c $CLUSTER_NAME

helm upgrade --install monitoring prometheus-community/kube-prometheus-stack \
  --namespace monitoring --create-namespace \
  --values infra/helm-values/prometheus-values.yaml \
  --timeout 8m || true

helm upgrade --install argocd argo/argo-cd \
  --namespace argocd --create-namespace \
  --set server.service.type=ClusterIP \
  --timeout 8m || true

helm upgrade --install chaos-mesh chaos-mesh/chaos-mesh \
  --namespace chaos-mesh --create-namespace \
  --set chaosDaemon.runtime=containerd \
  --set chaosDaemon.socketPath=/run/k3s/containerd/containerd.sock \
  --timeout 8m || true

kubectl apply -f infra/k8s/victim-service/

echo "⏳ Waiting for pods..."
kubectl wait --for=condition=Ready pod \
  -l app=victim-service -n app --timeout=120s

echo "✅ Done! Now run:"
echo "  kubectl port-forward svc/monitoring-kube-prometheus-prometheus -n monitoring 9090:9090 &"
echo "  kubectl port-forward svc/victim-service -n app 8000:80 &"
