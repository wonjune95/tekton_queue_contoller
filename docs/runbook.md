# Runbook — Tekton Queue Controller 운영 절차

## 1. 일반 상태 확인

```bash
# Pod 상태
kubectl get pods -n tekton-pipelines -l app=tekton-queue

# 로그 확인 (최근 100줄)
kubectl logs -n tekton-pipelines -l app=tekton-queue --tail=100

# Liveness / Readiness 직접 확인
kubectl exec -n tekton-pipelines <pod-name> -- \
  wget -qO- --no-check-certificate https://localhost:8443/healthz
kubectl exec -n tekton-pipelines <pod-name> -- \
  wget -qO- --no-check-certificate https://localhost:8443/readyz
```

---

## 2. 장애 대응

### 2-1. Pod CrashLoopBackOff

```bash
# 원인 확인
kubectl describe pod -n tekton-pipelines <pod-name>
kubectl logs -n tekton-pipelines <pod-name> --previous

# 주요 원인
# - TLS 인증서 Secret 누락: kubectl get secret tekton-queue-cacerts -n tekton-pipelines
# - CRD 미설치: kubectl get globallimits.tekton.devops
# - RBAC 권한 부족: kubectl auth can-i patch pipelineruns --as=system:serviceaccount:tekton-pipelines:tekton-queue-controller -n <ns>
```

### 2-2. 큐가 멈춤 (Pending PR들이 Running으로 전환 안 됨)

```bash
# 1. Leader 확인
kubectl get lease tekton-queue-controller-leader -n tekton-pipelines -o yaml
# holderIdentity 필드 확인 → 실제 실행 중인 Pod와 일치해야 함

# 2. admitted ConfigMap 확인
kubectl get configmap tekton-queue-admitted-count -n tekton-pipelines -o yaml
# admitted 값이 비정상적으로 높으면 수동 리셋:
kubectl patch configmap tekton-queue-admitted-count -n tekton-pipelines \
  --type merge -p '{"data":{"admitted":"0"}}'

# 3. 큐 상태 메트릭 확인
kubectl port-forward -n tekton-pipelines svc/tekton-queue-controller 9090:9090
curl http://localhost:9090/metrics | grep tekton_queue
```

### 2-3. Leader 미확보 (모든 Pod가 Follower)

```bash
# Lease 강제 삭제 → 재경쟁 유도
kubectl delete lease tekton-queue-controller-leader -n tekton-pipelines

# Pod 재시작
kubectl rollout restart deployment/tekton-queue-controller -n tekton-pipelines
```

### 2-4. Webhook 응답 실패 (PipelineRun CREATE 요청 타임아웃)

```bash
# Webhook 설정 확인
kubectl get mutatingwebhookconfiguration tekton-queue-mutator -o yaml

# caBundle이 현재 tls.crt와 일치하는지 확인
kubectl get secret tekton-queue-cacerts -n tekton-pipelines \
  -o jsonpath='{.data.tls\.crt}' | base64 -d | openssl x509 -noout -dates

# Webhook 임시 비활성화 (긴급 시)
kubectl delete mutatingwebhookconfiguration tekton-queue-mutator
```

---

## 3. 긴급 배포 (Tier 0 우선순위)

```bash
# Dashboard를 통해 urgent 라벨을 포함한 PipelineRun 생성
kubectl create -f - <<EOF
apiVersion: tekton.dev/v1
kind: PipelineRun
metadata:
  generateName: urgent-deploy-
  namespace: <cicd-namespace>
  labels:
    queue.tekton.dev/urgent: "true"
    type: deploy
    env: prod
spec:
  pipelineRef:
    name: <pipeline-name>
EOF
```

> Tier 0 PR은 쿼터 판정에서 최우선이 아니라 **대기열 정렬에서 최우선**입니다.
> 쿼터가 꽉 찬 경우에는 슬롯이 생길 때 가장 먼저 실행됩니다.

---

## 4. 롤링 업데이트

```bash
# 새 이미지로 교체
kubectl set image deployment/tekton-queue-controller \
  manager=<new-image>:<tag> -n tekton-pipelines

# 업데이트 상태 확인
kubectl rollout status deployment/tekton-queue-controller -n tekton-pipelines

# 문제 발생 시 롤백
kubectl rollout undo deployment/tekton-queue-controller -n tekton-pipelines
```

---

## 5. Prometheus 메트릭 확인

| 메트릭 | 정상 범위 | 이상 신호 |
|--------|-----------|-----------|
| `tekton_queue_running_total` | 0 ~ limit | limit보다 큰 경우 카운터 버그 의심 |
| `tekton_queue_pending_total` | 0 ~ N | 지속적으로 증가 시 Manager 루프 다운 의심 |
| `tekton_queue_kubernetes_api_errors_total` | 0 | 급증 시 K8s API 서버 이상 또는 RBAC 문제 |
| `tekton_queue_webhook_admitted_total` | 증가 | 멈춤 시 Webhook 비활성화 의심 |

```bash
# 메트릭 실시간 확인
kubectl port-forward -n tekton-pipelines svc/tekton-queue-controller 9090:9090
watch -n5 'curl -s http://localhost:9090/metrics | grep tekton_queue'
```
