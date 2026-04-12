# Tekton Global Queue Controller

다수의 네임스페이스에 걸쳐 실행되는 Tekton PipelineRun의 **전역 동시 실행 개수(Global Concurrency Limits)**를 통제하고, **우선순위 기반 스케줄링**을 제공하는 Kubernetes Mutating Admission Webhook 컨트롤러입니다.

---

## 1. 개요

Tekton Pipelines는 클러스터 전체 단위의 동시 실행 개수를 제한하는 기능을 포함하지 않습니다. 이로 인해 대규모 배포 요청이 동시에 발생하면 자원 경쟁으로 인한 OOM 및 노드 장애가 발생할 수 있습니다.

본 컨트롤러는 **Kubernetes MutatingAdmissionWebhook**을 활용하여 API Server 인입 단계에서 파이프라인 생성 요청을 선제적으로 통제하며, 다음의 기능을 제공합니다.

| 기능 | 설명 |
|------|------|
| **사전 차단** | 쿼터 초과 시 `PipelineRunPending` 상태를 즉시 주입하여 자원 고갈 원천 차단 |
| **우선순위 스케줄링** | CRD 기반 티어 분류 + 에이징(Aging) 메커니즘으로 기아(Starvation) 방지 |
| **멀티 네임스페이스** | 복수 패턴 기반 네임스페이스 필터링 (환경변수 / CRD 동적 설정) |
| **고가용성 (HA)** | Kubernetes Lease 기반 Leader Election으로 다중 Pod 구성 지원 |
| **API Server 부하 최소화** | SharedInformer 패턴의 로컬 인메모리 캐시 기반 Admission 판정 |
| **결함 허용성** | etcd의 `creationTimestamp` 기준 정렬로 Pod 재시작 시에도 순서 보장 |
| **모니터링** | Prometheus 메트릭 노출 + Grafana 대시보드 제공 |

---

## 2. 워크플로우

### 2.1. 전체 동작 흐름

```
 개발자/CI 트리거                  K8s API Server                  Queue Controller
       │                              │                                │
       │  PipelineRun CREATE 요청      │                                │
       │─────────────────────────────▶│                                │
       │                              │   /mutate Webhook 호출          │
       │                              │───────────────────────────────▶│
       │                              │                                │
       │                              │                    ┌───────────┤
       │                              │                    │ 캐시 조회   │
       │                              │                    │ 쿼터 확인   │
       │                              │                    │ 티어 분류   │
       │                              │                    └───────────┤
       │                              │                                │
       │                              │   ┌────────────────────────────┤
       │                              │   │                            │
       │                              │   │ [쿼터 여유]                 │
       │                              │   │  → 즉시 실행 허용            │
       │                              │   │  → tier 라벨 부여            │
       │                              │   │                            │
       │                              │   │ [쿼터 초과]                 │
       │                              │   │  → PipelineRunPending 주입  │
       │                              │   │  → tier + managed 라벨 부여  │
       │                              │   └────────────────────────────┤
       │                              │                                │
       │                              │◀──────── JSONPatch 응답 ────────│
       │◀─────── PipelineRun 생성 ────│                                │
       │                              │                                │
```

### 2.2. 대기열 스케줄링 흐름

```
   Manager Loop (5초 주기, Leader Pod에서만 실행)
       │
       ▼
  ┌─────────────┐
  │ CRD 설정 로드 │ ─── GlobalLimit CRD에서 Limit/Aging/TierRules/NamespacePatterns 읽기
  └──────┬──────┘
         ▼
  ┌─────────────┐
  │ 캐시에서 상태  │ ─── Running 카운트 + Managed Pending 목록 조회
  │   조회       │
  └──────┬──────┘
         ▼
  ┌─────────────┐     NO
  │ Running     │──────────────────▶ (대기 5초 후 재확인)
  │  < Limit ?  │
  └──────┬──────┘
         │ YES
         ▼
  ┌──────────────────┐
  │ Pending 목록 정렬  │ ─── effective_tier(ASC) → creationTimestamp(FIFO)
  │  + Aging 적용     │
  └──────┬───────────┘
         ▼
  ┌─────────────┐
  │ 빈 슬롯만큼   │ ─── spec.status = null 패치 → PipelineRun 실행 시작
  │ 순서대로 실행  │
  └─────────────┘
```

### 2.3. HA (Leader Election) 워크플로우

```
  ┌──────────────────────────────────────────────────────┐
  │                  Kubernetes Cluster                   │
  │                                                      │
  │  ┌─────────────────┐     ┌─────────────────┐         │
  │  │   Pod A (Leader) │     │ Pod B (Standby)  │         │
  │  │                 │     │                 │         │
  │  │  [Webhook]  ✅  │     │  [Webhook]  ✅  │         │
  │  │  [Watcher]  ✅  │     │  [Watcher]  ✅  │         │
  │  │  [Manager]  ✅  │     │  [Manager]  ⏸   │         │
  │  │  [LeaderEl] ✅  │     │  [LeaderEl] ✅  │         │
  │  │       │         │     │       │         │         │
  │  └───────┼─────────┘     └───────┼─────────┘         │
  │          │                       │                   │
  │          ▼                       ▼                   │
  │    ┌──────────────────────────────────┐               │
  │    │        Lease 리소스 (etcd)        │               │
  │    │  holder: pod-a                  │               │
  │    │  renewTime: 2s 간격 갱신         │               │
  │    │  leaseDuration: 15s             │               │
  │    └──────────────────────────────────┘               │
  └──────────────────────────────────────────────────────┘

  [Failover 시나리오]
  1. Pod A 장애 발생 → Lease 갱신 중단
  2. Pod B가 2초 간격으로 Lease를 확인
  3. renewTime으로부터 15초 경과 → Lease 만료 판정
  4. Pod B가 Lease를 탈취하여 Leader로 승격
  5. Manager 스케줄링 루프 자동 시작
  6. webhook_admitted_count 초기화
```

| 역할 | Leader Pod | Standby Pod |
|------|-----------|-------------|
| Webhook `/mutate` | ✅ 처리 | ✅ 처리 (Service 라운드로빈) |
| Watcher 캐시 동기화 | ✅ 실행 | ✅ 실행 (독립적) |
| Manager 스케줄링 | ✅ **실행** | ❌ **대기** |
| Leader Election | ✅ Lease 갱신 | ✅ Lease 감시 |

---

## 3. 아키텍처

| 설계 항목 | 구현 방식 | 기대 효과 |
|-----------|-----------|-----------|
| **제어 시점** | K8s API Server 인입 시점 (Admission Phase) | 불필요한 이벤트 전파 방지 |
| **초과 쿼터 처리** | JSONPatch를 통한 Pending 상태 + 티어 라벨 주입 | 강제 삭제/재생성 로직 제거 |
| **우선순위 분류** | CRD `tierRules`에 의한 label/env 기반 자동 티어 부여 | 운영 정책 변경 시 코드 수정 불필요 |
| **기아 방지** | 대기 시간 기반 에이징으로 effective tier 자동 승격 | 낮은 우선순위 파이프라인의 무기한 대기 방지 |
| **대기열 정합성** | `creationTimestamp` 기준 정렬 (FIFO) | Pod 재시작 시 순서 보장 |
| **취소/중지 처리** | `Cancelled`, `CancelledRunFinally`, `StoppedRunFinally` 상태 감지 | 취소된 파이프라인의 슬롯 즉시 반환 |
| **Race Condition 방어** | `webhook_admitted_count`로 Webhook-Watcher 간 정합성 유지 | 동시 CREATE 시 쿼터 초과 방지 |
| **고가용성** | Kubernetes Lease 기반 Leader Election | Leader 장애 시 ~15초 내 자동 Failover |
| **네임스페이스 설정** | CRD `namespacePatterns` 동적 설정 (복수 패턴 지원) | 재배포 없이 대상 네임스페이스 변경 가능 |
| **모니터링** | Prometheus `/metrics` 엔드포인트 자동 노출 | Grafana 대시보드로 실시간 시각화 |

---

## 4. 우선순위 스케줄링

### 4.1. 티어 분류 체계

GlobalLimit CRD의 `tierRules`를 통해 PipelineRun의 우선순위를 자동 분류합니다. 규칙은 순서대로 매칭되며, 먼저 매칭된 규칙이 적용됩니다.

| 매칭 순서 | matchType | 매칭 대상 | 예시 | Tier |
|-----------|-----------|-----------|------|------|
| 1순위 | `label` | `metadata.labels`의 지정 키 | `queue.tekton.dev/urgent: "true"` | 0 (긴급) |
| 2순위 | `env` | `metadata.labels.env` | `prod` | 1 (운영) |
| 3순위 | `env` | `metadata.labels.env` | `stg` | 2 (검증) |
| 기본값 | `env` | `metadata.labels.env` | `*` (나머지) | 3 (개발) |

### 4.2. 에이징 (Aging) 메커니즘

대기열에서 장시간 대기하는 파이프라인의 effective tier를 자동으로 승격시켜 기아 현상을 방지합니다.

- **승격 주기:** `agingIntervalSec` (기본 300초)마다 effective tier가 1 감소
- **승격 하한:** `agingMinTier` (기본 1) 이하로는 내려가지 않음
- **Tier 0 보호:** 에이징으로 Tier 0(긴급)에 도달할 수 없으므로, 수동 긴급 배포의 최우선 지위가 항상 보장됨

### 4.3. 긴급 배포 사용 방법

PipelineRun을 수동으로 생성할 때 아래 라벨을 추가하면 Tier 0으로 분류됩니다.

```yaml
metadata:
  labels:
    queue.tekton.dev/urgent: "true"
```

---

## 5. 네임스페이스 설정

컨트롤러가 관리할 네임스페이스를 GlobalLimit CRD의 `spec.namespacePatterns`에서 설정합니다. 복수 패턴을 지원하며, `fnmatch` 문법(`*`, `?`, `[seq]`)을 사용합니다.

CRD에 미설정 또는 조회 실패 시 기본값 `["*-cicd"]`가 사용됩니다.

### 5.1. 설정 방법

GlobalLimit CRD의 `spec.namespacePatterns`에 배열로 지정합니다. **재배포 없이 런타임에 변경 가능**합니다.

```yaml
apiVersion: tekton.devops/v1
kind: GlobalLimit
metadata:
  name: tekton-queue-limit
spec:
  namespacePatterns:
    - "*-cicd"
    - "production-*"
    - "staging-*"
  maxPipelines: 10
  # ...
```

```bash
# 런타임 변경 예시
kubectl patch globallimit tekton-queue-limit --type=merge \
  -p '{"spec":{"namespacePatterns":["*-cicd","newapp-*"]}}'
```

### 5.2. 매칭 예시

| 패턴 | 매칭되는 네임스페이스 | 매칭 안되는 네임스페이스 |
|------|---------------------|----------------------|
| `*-cicd` | `myapp-cicd`, `test-cicd` | `myapp-deploy`, `default` |
| `tekton-*` | `tekton-pipelines`, `tekton-builds` | `my-tekton` |
| `my-ns` | `my-ns` (정확 매칭) | `my-ns-extra` |
| `prod-*` | `prod-api`, `prod-web` | `staging-api` |

---

## 6. 고가용성 (HA) 구성

### 6.1. 개요

Kubernetes Lease 기반 Leader Election으로 **다중 Pod(replicas ≥ 2)** 구성을 지원합니다. Leader Pod만 Manager 스케줄링 루프를 실행하고, Standby Pod는 Leader가 장애 시 자동으로 승격됩니다.

### 6.2. Leader Election 파라미터

| 파라미터 | 값 | 설명 |
|---------|-----|------|
| Lease Duration | 15초 | Lease 유효 기간 |
| Retry Period | 2초 | Lease 체크/갱신 주기 |
| 장애 복구 시간 | ~15초 | Leader 장애 시 Standby 승격까지 최대 시간 |

### 6.3. Leader 상태 확인

```bash
# 특정 Pod의 Leader 상태 확인
kubectl exec -n tekton-pipelines <pod-name> -- curl -sk https://localhost:8443/healthz
# 응답 예시:
# {"leader": true, "pod": "tekton-queue-controller-7f8b9c4d5-abc12", "status": "ok"}
```

### 6.4. Deployment 설정

```yaml
spec:
  replicas: 2  # HA 기본 구성 (2~3 권장)
```

---

## 7. 설치 및 배포

### 7.1. 사전 요구사항

- Kubernetes Cluster (v1.20+)
- Tekton Pipelines 설치 완료
- OpenSSL (웹훅용 TLS 인증서 생성)

### 7.2. TLS 인증서 및 Secret 생성

```bash
cat > csr.conf <<EOF
[req]
req_extensions = v3_req
distinguished_name = req_distinguished_name
[req_distinguished_name]
[ v3_req ]
basicConstraints = CA:FALSE
keyUsage = nonRepudiation, digitalSignature, keyEncipherment
extendedKeyUsage = serverAuth
subjectAltName = @alt_names
[alt_names]
DNS.1 = tekton-queue-controller
DNS.2 = tekton-queue-controller.tekton-pipelines
DNS.3 = tekton-queue-controller.tekton-pipelines.svc
EOF

openssl genrsa -out tls.key 2048
openssl req -new -key tls.key -out tls.csr \
  -subj "/CN=tekton-queue-controller.tekton-pipelines.svc" \
  -config csr.conf
openssl x509 -req -in tls.csr -signkey tls.key -out tls.crt \
  -days 3650 -extensions v3_req -extfile csr.conf

kubectl create secret tls tekton-queue-cacerts \
  --cert=tls.crt --key=tls.key -n tekton-pipelines
```

### 7.3. 배포 순서

```bash
# 1. CRD 등록
kubectl apply -f install/crd.yaml

# 2. GlobalLimit 설정 (네임스페이스 패턴, 동시 실행 제한, 티어 규칙)
kubectl apply -f install/limit-setting.yaml

# 3. Controller 배포 (RBAC, Service, Webhook, Deployment)
#    ⚠️ deploy.yaml의 caBundle을 실제 값으로 교체하세요:
#    cat tls.crt | base64 | tr -d '\n'
kubectl apply -f install/deploy.yaml
```

### 7.4. 배포 확인

```bash
# Pod 상태 확인 (HA면 2개 Pod가 Running이어야 함)
kubectl get pods -n tekton-pipelines -l app=tekton-queue

# GlobalLimit 설정 확인
kubectl get globallimits

# Leader 확인
kubectl exec -n tekton-pipelines <pod-name> -- curl -sk https://localhost:8443/healthz
```

---

## 8. 설정 참조

### 8.1. GlobalLimit CRD 필드

| 필드 | 타입 | 필수 | 기본값 | 설명 |
|------|------|------|--------|------|
| `spec.namespacePatterns` | `string[]` | ❌ | `["*-cicd"]` | 관리 대상 네임스페이스 패턴 목록 |
| `spec.maxPipelines` | `integer` | ✅ | - | 동시 실행 가능한 최대 파이프라인 수 |
| `spec.agingIntervalSec` | `integer` | ❌ | 300 | 에이징 승격 주기 (초) |
| `spec.agingMinTier` | `integer` | ❌ | 1 | 에이징으로 도달 가능한 최소 Tier |
| `spec.tierRules` | `object[]` | ❌ | 기본 규칙 | 티어 분류 규칙 배열 |

### 8.2. 환경변수

| 환경변수 | 기본값 | 설명 |
|---------|--------|------|
| `POD_NAME` | `controller-{PID}` | Pod 이름 (Leader Election용, Downward API로 주입) |
| `POD_NAMESPACE` | `tekton-pipelines` | Pod 네임스페이스 (Lease 생성 위치) |
| `LEASE_NAME` | `tekton-queue-controller-leader` | Leader Election Lease 리소스 이름 |

### 8.3. 엔드포인트

| 경로 | 포트 | 설명 |
|------|------|------|
| `/mutate` | 8443 (HTTPS) | Admission Webhook 엔드포인트 |
| `/healthz` | 8443 (HTTPS) | Liveness Probe (leader 상태 포함) |
| `/readyz` | 8443 (HTTPS) | Readiness Probe (초기 동기화 상태) |
| `/metrics` | 9090 (HTTP) | Prometheus 메트릭 |

---

## 9. 모니터링 (Prometheus & Grafana)

### 9.1. 메트릭 목록

| 메트릭 | 타입 | 라벨 | 설명 |
|--------|------|------|------|
| `tekton_queue_limit` | Gauge | - | 글로벌 동시 실행 허용량 |
| `tekton_queue_running_total` | Gauge | - | 현재 실행 중인 파이프라인 수 |
| `tekton_queue_pending_total` | Gauge | `tier` | 대기열 파이프라인 수 (Tier별) |
| `tekton_queue_webhook_admitted_total` | Counter | `tier` | Webhook 즉시 실행 허용 횟수 |
| `tekton_queue_webhook_queued_total` | Counter | `tier` | Webhook 대기열 전환 횟수 |
| `tekton_queue_scheduled_total` | Counter | `tier` | Manager 스케줄링 횟수 |
| `tekton_queue_kubernetes_api_errors_total` | Counter | `operation` | K8s API 에러 횟수 |

### 9.2. Prometheus Scrape 설정

```yaml
scrape_configs:
  - job_name: tekton-queue-controller
    static_configs:
      - targets:
        - tekton-queue-controller.tekton-pipelines.svc.cluster.local:9090
```

### 9.3. Grafana 대시보드

`grafana-dashbaord/grafana-dashboard.json`을 Grafana의 "Import" 메뉴로 등록하면 큐 상태를 실시간으로 시각화할 수 있습니다.

---

## 10. 빌드 가이드

```bash
cd docker

# 이미지 빌드
docker build -t your-registry/tekton-queue-controller:v0.3.0 .

# 이미지 푸시
docker push your-registry/tekton-queue-controller:v0.3.0
```

---

## 11. 프로젝트 구조

```
tekton_queue_controller/
├── docker/
│   ├── Dockerfile          # 컨테이너 이미지 빌드 파일
│   ├── app.py              # 메인 컨트롤러 소스 코드
│   └── requirements.txt    # Python 의존성
├── install/
│   ├── crd.yaml            # GlobalLimit CRD 스키마
│   ├── deploy.yaml         # RBAC, Service, Webhook, Deployment
│   ├── limit-setting.yaml  # GlobalLimit 설정 예시
│   └── secret.yaml         # TLS Secret 템플릿
├── grafana-dashbaord/
│   └── grafana-dashboard.json  # Grafana 대시보드
└── README.md
```

---

## 12. 설계 한계 및 향후 과제

- **비선점형 설계:** Webhook 단계에서는 슬롯 가용 여부만 판단하며 우선순위를 고려하지 않습니다. 우선순위 정렬은 대기열 진입 이후 Manager에서 적용됩니다. 실행 중인 낮은 티어 파이프라인의 선점은 지원하지 않습니다.
- **Webhook-Manager 경합:** 빌드 완료 후 트리거되는 배포 PipelineRun이 Webhook 경로로 슬롯을 먼저 차지하여, 대기열의 높은 우선순위 파이프라인보다 먼저 실행될 수 있습니다.
- **`webhook_admitted_count` 정합성:** Webhook 통과 후 PipelineRun 생성 자체가 실패하면 카운터가 일시적으로 높게 유지됩니다. 전체 재동기화 시 리셋되어 최종 정합성이 보장됩니다.
- **CRD 변경 반영 지연:** GlobalLimit CRD 변경 시 최대 5초의 반영 지연이 있습니다.
- **HA Failover 지연:** Leader 장애 시 최대 ~15초의 스케줄링 공백이 발생합니다. 이 동안 Webhook은 모든 Pod에서 정상 처리됩니다.