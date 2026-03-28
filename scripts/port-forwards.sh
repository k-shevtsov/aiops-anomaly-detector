#!/bin/bash
echo "🔌 Starting port-forwards..."

pkill -f "port-forward" 2>/dev/null || true
sleep 1

kubectl port-forward svc/monitoring-kube-prometheus-prometheus \
  -n monitoring 9090:9090 &
kubectl port-forward svc/victim-service -n app 8000:80 &
kubectl port-forward svc/monitoring-grafana -n monitoring 3000:80 &

echo "✅ Port-forwards running:"
echo "  Prometheus: http://localhost:9090"
echo "  Grafana:    http://localhost:3000 (admin/admin)"
echo "  Victim:     http://localhost:8000"
