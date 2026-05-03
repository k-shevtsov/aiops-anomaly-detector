# =============================================================================
# aiops-anomaly-detector — Project Bootstrap
# Usage after reboot: make up
# =============================================================================

SHELL := /bin/bash

GREEN  := \033[0;32m
YELLOW := \033[0;33m
RED    := \033[0;31m
NC     := \033[0m

.PHONY: up down status ollama clusters shared-infra aiops \
        port-forwards smoke-test logs clean help

help:
	@echo ""
	@echo "  make up            — bring up everything after reboot"
	@echo "  make down          — stop all clusters"
	@echo "  make status        — show current state"
	@echo "  make port-forwards — start port-forwards in background"
	@echo "  make smoke-test    — verify all services respond"
	@echo "  make logs          — tail embedding-server logs"
	@echo "  make clean         — delete all k3d clusters"
	@echo ""

# ── Main ─────────────────────────────────────────────────────────────────────
up: ollama clusters shared-infra aiops smoke-test
	@printf "$(GREEN)✅  All services up$(NC)\n\n"
	@echo "  Embedding server : localhost:8001 (run: make port-forwards)"
	@echo "  Ollama API       : localhost:11434"
	@echo ""

# ── Ollama ───────────────────────────────────────────────────────────────────
ollama:
	@printf "$(YELLOW)▶  Checking Ollama...$(NC)\n"
	@systemctl is-active --quiet ollama || sudo systemctl start ollama
	@sleep 2
	@curl -sf http://localhost:11434/v1/models > /dev/null \
		&& printf "$(GREEN)   ✓ Ollama running$(NC)\n" \
		|| (printf "$(RED)   ✗ Ollama failed$(NC)\n" && exit 1)

# ── k3d clusters ─────────────────────────────────────────────────────────────
clusters:
	@printf "$(YELLOW)▶  Starting k3d clusters...$(NC)\n"
	@if k3d cluster list | grep -q "shared-infra"; then \
		k3d cluster start shared-infra 2>/dev/null || true; \
	else \
		k3d cluster create shared-infra \
			--agents 1 \
			--k3s-arg "--disable=traefik@server:0" \
			--wait; \
	fi
	@if k3d cluster list | grep -q "^aiops"; then \
		k3d cluster start aiops 2>/dev/null || true; \
	else \
		printf "$(YELLOW)   aiops cluster not found — skipping$(NC)\n"; \
	fi
	@kubectl config use-context k3d-shared-infra
	@printf "$(GREEN)   ✓ Clusters ready$(NC)\n"

# ── shared-infra ─────────────────────────────────────────────────────────────
shared-infra:
	@printf "$(YELLOW)▶  Setting up shared-infra...$(NC)\n"
	@kubectl config use-context k3d-shared-infra
	@kubectl get namespace shared-infra > /dev/null 2>&1 \
		|| kubectl create namespace shared-infra
	@bash scripts/ollama-service.sh
	@if kubectl get deployment embedding-server-embedding-server \
			-n shared-infra > /dev/null 2>&1; then \
		printf "   embedding-server already deployed\n"; \
	else \
		helm install embedding-server ./shared-infra-charts/charts/embedding-server \
			--namespace shared-infra \
			-f ./shared-infra-charts/charts/embedding-server/values-local.yaml; \
	fi
	@printf "   Waiting for embedding-server..."
	@kubectl rollout status deployment/embedding-server-embedding-server \
		-n shared-infra --timeout=180s > /dev/null
	@printf " $(GREEN)ready$(NC)\n"

# ── aiops cluster ─────────────────────────────────────────────────────────────
aiops:
	@printf "$(YELLOW)▶  Checking aiops cluster...$(NC)\n"
	@if k3d cluster list | grep -q "^aiops"; then \
		kubectl config use-context k3d-aiops; \
		printf "$(GREEN)   ✓ aiops ready$(NC)\n"; \
		kubectl config use-context k3d-shared-infra; \
	else \
		printf "$(YELLOW)   aiops cluster not found — skipping$(NC)\n"; \
	fi

# ── Port-forwards ─────────────────────────────────────────────────────────────
port-forwards:
	@printf "$(YELLOW)▶  Starting port-forwards...$(NC)\n"
	@pkill -f "port-forward.*embedding-server" 2>/dev/null || true
	@pkill -f "port-forward.*prometheus" 2>/dev/null || true
	@sleep 1
	@kubectl config use-context k3d-shared-infra
	@kubectl port-forward -n shared-infra \
		svc/embedding-server-embedding-server 8001:8001 \
		>/tmp/pf-embedding.log 2>&1 &
	@sleep 2
	@curl -sf http://localhost:8001/health > /dev/null \
		&& printf "$(GREEN)   ✓ embedding-server → localhost:8001$(NC)\n" \
		|| printf "$(RED)   ✗ embedding-server port-forward failed$(NC)\n"
	@if k3d cluster list 2>/dev/null | grep -q "^aiops"; then \
		kubectl config use-context k3d-aiops 2>/dev/null; \
		kubectl port-forward -n monitoring svc/prometheus-server 9090:80 \
			>/tmp/pf-prometheus.log 2>&1 & \
		sleep 1; \
		printf "$(GREEN)   ✓ prometheus → localhost:9090$(NC)\n"; \
		kubectl config use-context k3d-shared-infra; \
	fi

# ── Smoke test ────────────────────────────────────────────────────────────────
smoke-test:
	@printf "$(YELLOW)▶  Smoke tests...$(NC)\n"
	@kubectl config use-context k3d-shared-infra
	@curl -sf http://localhost:11434/v1/models > /dev/null \
		&& printf "$(GREEN)   ✓ Ollama (host)$(NC)\n" \
		|| printf "$(RED)   ✗ Ollama (host)$(NC)\n"
	@kubectl run smoke-embed --rm -i --image=curlimages/curl \
		--restart=Never -n shared-infra --timeout=30s -- \
		curl -sf http://embedding-server-embedding-server:8001/health \
		2>/dev/null | grep -q "ok" \
		&& printf "$(GREEN)   ✓ embedding-server (in-cluster)$(NC)\n" \
		|| printf "$(RED)   ✗ embedding-server (in-cluster)$(NC)\n"
	@kubectl run smoke-ollama --rm -i --image=curlimages/curl \
		--restart=Never -n shared-infra --timeout=30s -- \
		curl -sf http://ollama:11434/v1/models \
		2>/dev/null | grep -q "gemma2" \
		&& printf "$(GREEN)   ✓ Ollama (in-cluster)$(NC)\n" \
		|| printf "$(RED)   ✗ Ollama (in-cluster)$(NC)\n"

# ── Status ───────────────────────────────────────────────────────────────────
status:
	@echo ""
	@printf "$(YELLOW)Ollama:$(NC)\n"
	@systemctl is-active ollama \
		&& curl -sf http://localhost:11434/v1/models \
		| python3 -c "import json,sys; [print('  -',m['id']) for m in json.load(sys.stdin)['data']]" \
		|| echo "  not running"
	@echo ""
	@printf "$(YELLOW)k3d clusters:$(NC)\n"
	@k3d cluster list
	@echo ""
	@printf "$(YELLOW)shared-infra pods:$(NC)\n"
	@kubectl config use-context k3d-shared-infra 2>/dev/null \
		&& kubectl get pods -n shared-infra 2>/dev/null \
		|| echo "  cluster not running"
	@echo ""

# ── Down ─────────────────────────────────────────────────────────────────────
down:
	@printf "$(YELLOW)▶  Stopping...$(NC)\n"
	@pkill -f "kubectl port-forward" 2>/dev/null || true
	@k3d cluster stop shared-infra 2>/dev/null || true
	@k3d cluster stop aiops 2>/dev/null || true
	@printf "$(GREEN)   Done$(NC)\n"

# ── Logs ─────────────────────────────────────────────────────────────────────
logs:
	@kubectl config use-context k3d-shared-infra
	@kubectl logs -n shared-infra \
		-l app.kubernetes.io/name=embedding-server -f

# ── Clean ─────────────────────────────────────────────────────────────────────
clean:
	@printf "$(RED)▶  Deleting all k3d clusters...$(NC)\n"
	@k3d cluster delete shared-infra 2>/dev/null || true
	@k3d cluster delete aiops 2>/dev/null || true
	@printf "$(GREEN)   Done$(NC)\n"
