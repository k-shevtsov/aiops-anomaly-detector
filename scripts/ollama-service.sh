#!/bin/bash
# Recreate Ollama Service+Endpoints pointing to host IP
set -e

HOST_IP=$(docker network inspect k3d-shared-infra | python3 -c "
import json, sys
nets = json.load(sys.stdin)
for net in nets:
    for cfg in net.get('IPAM', {}).get('Config', []):
        gw = cfg.get('Gateway')
        if gw:
            print(gw)
            break
")

echo "Host IP: $HOST_IP"

kubectl delete service ollama -n shared-infra --ignore-not-found > /dev/null 2>&1
kubectl delete endpoints ollama -n shared-infra --ignore-not-found > /dev/null 2>&1

kubectl apply -f - << EOF
apiVersion: v1
kind: Service
metadata:
  name: ollama
  namespace: shared-infra
spec:
  ports:
    - port: 11434
      targetPort: 11434
---
apiVersion: v1
kind: Endpoints
metadata:
  name: ollama
  namespace: shared-infra
subsets:
  - addresses:
      - ip: ${HOST_IP}
    ports:
      - port: 11434
EOF

echo "Ollama service → ${HOST_IP}:11434"
