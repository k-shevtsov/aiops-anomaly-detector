# aiops-anomaly-detector

AIOps Self-Healing Kubernetes with Isolation Forest and LLM explanations.

## Stack
- k3d, ArgoCD, Prometheus, Grafana, Chaos Mesh
- scikit-learn (Isolation Forest), Claude API
- Kubernetes Python SDK, Telegram, GitHub Issues, Helm

## Local Development Notes

### After laptop reboot
k3d does not survive reboots. Always recreate the cluster:
```bash
k3d cluster delete aiops
k3d cluster create aiops --port "80:80@loadbalancer" --agents 2
kubectl create namespace app
k3d image import victim-service:local -c aiops
helm upgrade --install monitoring prometheus-community/kube-prometheus-stack \
  --namespace monitoring --create-namespace \
  --values infra/helm-values/prometheus-values.yaml
helm upgrade --install argocd argo/argo-cd \
  --namespace argocd --create-namespace \
  --set server.service.type=ClusterIP
helm upgrade --install chaos-mesh chaos-mesh/chaos-mesh \
  --namespace chaos-mesh --create-namespace \
  --set chaosDaemon.runtime=containerd \
  --set chaosDaemon.socketPath=/run/k3s/containerd/containerd.sock
kubectl apply -f infra/k8s/victim-service/
```

### helm --wait timeout
helm times out but pods continue starting. Always verify with:
```bash
kubectl get pods -n <namespace>
```
