<div align="center">

# 🚦 Tekton Global Queue Controller

**다수의 네임스페이스에 걸친 Tekton PipelineRun의 전역 동시 실행 수를 통제하고, 우선순위 기반으로 스케줄링하는 Kubernetes Admission Webhook 컨트롤러**

[![Tests](https://img.shields.io/badge/tests-168%20passed-2ea44f?logo=pytest&logoColor=white)](#13-검증-verification)
[![Integration](https://img.shields.io/badge/integration%20S1~S4-passed-2ea44f?logo=kubernetes&logoColor=white)](#13-검증-verification)
[![Python](https://img.shields.io/badge/python-3.11-3776AB?logo=python&logoColor=white)](docker/Dockerfile)
[![Kubernetes](https://img.shields.io/badge/kubernetes-1.20%2B-326CE5?logo=kubernetes&logoColor=white)](#71-사전-요구사항)
[![Tekton](https://img.shields.io/badge/tekton-pipelines-FD495C?logo=tekton&logoColor=white)](https://tekton.dev)
[![HA](https://img.shields.io/badge/HA-leader%20election-blueviolet?logo=apachekafka&logoColor=white)](#6-고가용성-ha-구성)
[![License](https://img.shields.io/badge/license-MIT-yellow)](LICENSE)

</div>

---

## ⚡ 한눈에 (TL;DR)

Tekton Pipelines에는 **클러스터 전역 동시 실행 제한이 없어**, 대량 배포가 몰리면 자원 경쟁으로 OOM·노드 장애가 발생합니다. 이 컨트롤러는 API Server 인입 단계(Admission Webhook)에서 이를 선제 통제합니다.

- **외부 락 스토리지 0** — Redis·ZooKeeper 없이 K8s 네이티브 프리미티브(Lease 기반 Leader Election)만으로 전역 쿼터 정합성을 확보. 인가 결정은 Leader의 매니저 루프 한 곳에서만 일어납니다
- **비파괴적 제어 + 우선순위** — 쿼터 초과 PR을 삭제/재생성 없이 `PipelineRunPending` 주입으로 보류하고, 티어 + 에이징으로 스케줄링해 기아(starvation)를 방지
- **HA + 자가 치유** — Lease 기반 Leader Election으로 ~15초 내 Failover, 카운터 누수는 Manager OR-게이트가 30초 내 자동 보정(결과적 정합성)
- **검증됨** — 168개 단위 테스트 통과 + 실클러스터 시나리오에서 동시 실행이 한도를 초과하지 않음(직렬·동시 도착 모두)

> 왜 Go가 아니라 Python인지는 [§3 핵심 설계 결정](#-핵심-설계-결정-key-design-decisions)에서 다룹니다.

---

## 🚀 Quick Start

Kind 로컬 클러스터에 Tekton과 컨트롤러를 배포하고, 버스트 시나리오로 큐 동작을 곧바로 확인할 수 있습니다. (`kind`, `kubectl`, `docker` 필요)

```bash
make kind-setup   # Kind 클러스터 생성 + Tekton + 컨트롤러 자동 배포
make kind-test    # PipelineRun 24개 버스트 생성 → 큐 적체·소진 관찰
```

> 프로덕션 배포(TLS 인증서·HA 구성)는 [§7 설치 및 배포](#7-설치-및-배포)를, 전체 동작 원리는 [§2 워크플로우](#2-워크플로우)를 참고하세요.

---

## 📑 목차

| | 섹션 | | 섹션 |
|---|---|---|---|
| 1 | [개요](#1-개요) | 8 | [설정 참조](#8-설정-참조) |
| 2 | [워크플로우](#2-워크플로우) | 9 | [모니터링 (Prometheus)](#9-모니터링-prometheus) |
| 3 | [아키텍처](#3-아키텍처) | 10 | [Docker 이미지 빌드](#10-docker-이미지-빌드) |
| 4 | [우선순위 스케줄링](#4-우선순위-스케줄링) | 11 | [프로젝트 구조](#11-프로젝트-구조) |
| 5 | [네임스페이스 설정](#5-네임스페이스-설정) | 12 | [인가 경로](#12-인가-경로-admission-path) |
| 6 | [고가용성 (HA)](#6-고가용성-ha-구성) | 13 | [검증 (Verification)](#13-검증-verification) |
| 7 | [설치 및 배포](#7-설치-및-배포) | 14 | [설계 한계 및 향후 과제](#14-설계-한계-및-향후-과제) |

---

## 1. 개요

Tekton Pipelines는 클러스터 전체 단위의 동시 실행 개수를 제한하는 기능을 포함하지 않습니다. 이로 인해 대규모 배포 요청이 동시에 발생하면 자원 경쟁으로 인한 OOM 및 노드 장애가 발생할 수 있습니다.

본 컨트롤러는 **Kubernetes MutatingAdmissionWebhook**을 활용하여 API Server 인입 단계에서 파이프라인 생성 요청을 선제적으로 통제합니다.

| 기능 | 설명 |
|------|------|
| **출처 기반 실행 제어** | Tekton Dashboard에서 생성된 PR만 큐를 통해 실행. 배스천 등 외부 출처는 Pending으로 보류 |
| **전역 동시 실행 제한** | 생성 요청을 `PipelineRunPending`으로 보류하고, 매니저가 빈 슬롯만큼만 실행시켜 자원 고갈 원천 차단 |
| **우선순위 스케줄링** | CRD 기반 티어 분류 + 에이징(Aging) 메커니즘으로 기아(Starvation) 방지 |
| **멀티 네임스페이스** | 복수 패턴 기반 네임스페이스 필터링 (fnmatch 문법) |
| **고가용성 (HA)** | Kubernetes Lease 기반 Leader Election으로 다중 Pod 구성 지원 |
| **API Server 부하 최소화** | SharedInformer 패턴의 로컬 인메모리 캐시 기반 Admission 판정 |
| **모니터링** | Prometheus 메트릭 노출 |

---

## 2. 워크플로우

### 2.0. 전체 흐름 한눈에 보기

PipelineRun 생성부터 실행까지의 전체 경로 — ① Admission Webhook(쿼터 판정) → ② Controller Pod(캐시·스케줄링·자가 치유) → ③ HA(Leader Election)를 한 장으로 정리한 그림입니다. 인가 결정이 어디서 일어나는지는 [§12](#12-인가-경로-admission-path)에서 다룹니다.

<div align="center">
  <img src="docs/images/workflow.png" alt="Tekton Queue Controller 전체 워크플로우" width="900"/>
</div>

> 📄 편집 가능한 원본: [`docs/images/workflow.mmd`](docs/images/workflow.mmd) · 벡터: [`docs/images/workflow.svg`](docs/images/workflow.svg)

### 2.1. 출처 기반 실행 제어

PipelineRun 생성 시 Webhook이 `request.userInfo.username`을 확인하여 출처를 구분합니다.

```mermaid
flowchart TD
    A([PipelineRun CREATE 요청]) --> B{대상 네임스페이스?}
    B -- No --> P([그냥 통과<br/>allowed=true])
    B -- Yes --> C{출처 = Dashboard SA?}
    C -- "No (배스천/CI 등)" --> H[/spec.status = PipelineRunPending<br/>managed 라벨 없음 → 스케줄링 제외/]
    C -- Yes --> D{캐시 초기 동기화 완료?}
    D -- No --> P
    D -- Yes --> Q[/Tier 라벨 + managed 라벨<br/>spec.status = PipelineRunPending<br/>대기열 진입/]
    Q -.->|매니저 루프가 인가| R([빈 슬롯만큼 실행 시작])

    classDef admit fill:#2ea44f,stroke:#1a7f37,color:#fff;
    classDef hold fill:#d29922,stroke:#9a6700,color:#fff;
    class R,P admit;
    class H,Q hold;
```

- **Dashboard** (`tekton-dashboard` SA): 쿼터와 무관하게 **항상 대기열에 진입**하고, 실행 시작은 매니저 루프가 결정합니다.
- **그 외 출처**: 항상 `PipelineRunPending` 설정, 매니저가 절대 스케줄링하지 않음

> **왜 웹훅이 직접 인가하지 않는가**
>
> 웹훅은 요청마다 독립적으로 실행됩니다. 슬롯 확인과 실행 허용을 웹훅에서 처리하면,
> 동시에 들어온 요청들이 각자 자기 시점의 상태를 보고 판정하기 때문에 순간적으로 상한을 넘길 수 있습니다.
> 실측에서 동시 12요청 시 상한 30에 대해 최대 48까지 올라갔습니다(직렬 도착에서는 재현되지 않음).
>
> 그래서 **인가 결정은 매니저 루프 한 곳에서만** 수행합니다. 결정이 단일 스레드에서 직렬로 일어나므로
> 경쟁이 성립하지 않으며, 매니저는 한 사이클에서 실행시킨 건수를 세어 남은 슬롯을 차감합니다.
> 대가로 슬롯이 비어 있어도 다음 사이클까지 대기합니다(최대 약 5초).

### 2.2. 대기열 스케줄링 흐름

```mermaid
flowchart TD
    L([Manager Loop<br/>5초 주기 · Leader Pod 전용]) --> CFG[GlobalLimit CRD 로드<br/>Limit / Aging / TierRules / NS Patterns]
    CFG --> ST[캐시 상태 조회<br/>Running 수 + Managed Pending 목록]
    ST --> AVAIL{"available_slots &gt; 0<br/>&amp;&amp; pending 존재?"}
    AVAIL -- No --> WAIT[대기 5초 후 재확인]
    AVAIL -- Yes --> SORT[Pending 정렬<br/>effective_tier ASC → creationTimestamp FIFO + Aging]
    SORT --> RUN([빈 슬롯만큼 spec.status=null 패치<br/>→ 실행 시작])
    WAIT --> L
    RUN --> L

    classDef run fill:#2ea44f,stroke:#1a7f37,color:#fff;
    class RUN run;
```

### 2.3. HA (Leader Election) 워크플로우

```mermaid
flowchart LR
    subgraph Cluster["Kubernetes Cluster"]
        subgraph PodA["Pod A (Leader)"]
            A1["✅ Webhook"]
            A2["✅ Watcher"]
            A3["✅ Manager"]
            A4["✅ LeaderElection"]
        end
        subgraph PodB["Pod B (Standby)"]
            B1["✅ Webhook"]
            B2["✅ Watcher"]
            B3["⏸️ Manager"]
            B4["✅ LeaderElection"]
        end
        Lease[("🔑 Lease 리소스 (etcd)<br/>holder: pod-a<br/>renew: 2s · duration: 15s")]
        A4 -- "갱신 (renew)" --> Lease
        B4 -. "감시 (watch)" .-> Lease
    end

    classDef leader fill:#2ea44f,stroke:#1a7f37,color:#fff;
    classDef standby fill:#6e7781,stroke:#424a53,color:#fff;
    class A3 leader;
    class B3 standby;
```

**Failover 시나리오**

```mermaid
sequenceDiagram
    participant A as Pod A (Leader)
    participant L as Lease (etcd)
    participant B as Pod B (Standby)
    A--xL: ❌ 장애 발생 → Lease 갱신 중단
    loop 2초 간격
        B->>L: Lease 확인
    end
    Note over L: renewTime +15s 경과<br/>→ Lease 만료 판정
    B->>L: Lease 탈취 (takeover)
    L-->>B: holder = pod-b
    Note over B: 🟢 Leader 승격<br/>Manager 스케줄링 루프 자동 시작
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
| **출처 구분** | `request.userInfo.username` 기반 Dashboard SA 패턴 매칭 | 배스천/CI 등 외부 생성 PR 자동 보류 |
| **제어 시점** | K8s API Server 인입 시점 (Admission Phase) | 불필요한 이벤트 전파 방지 |
| **초과 쿼터 처리** | JSONPatch를 통한 Pending 상태 + 티어 라벨 주입 | 강제 삭제/재생성 로직 제거 |
| **우선순위 분류** | CRD `tierRules`에 의한 label/env 기반 자동 티어 부여 | 운영 정책 변경 시 코드 수정 불필요 |
| **기아 방지** | 대기 시간 기반 에이징으로 effective tier 자동 승격 | 낮은 우선순위 파이프라인의 무기한 대기 방지 |
| **대기열 정합성** | `creationTimestamp` 기준 정렬 (FIFO) | Pod 재시작 시 순서 보장 |
| **취소/중지 처리** | `Cancelled`, `CancelledRunFinally`, `StoppedRunFinally` 상태 감지 | 취소된 파이프라인의 슬롯 즉시 반환 |
| **Race Condition 방어** | 인가 결정을 매니저 루프 한 곳으로 단일화 (Leader 전용) | 동시 CREATE 시에도 상한 초과 없음 |
| **고가용성** | Kubernetes Lease 기반 Leader Election | Leader 장애 시 ~15초 내 자동 Failover |

### 💡 핵심 설계 결정 (Key Design Decisions)

외부 의존성을 늘리지 않고 **Kubernetes 네이티브 프리미티브만으로** 분산 정합성·고가용성을 달성한 것이 이 컨트롤러의 핵심 설계입니다.

| 문제 | 선택한 설계 | 대안 대비 이점 |
|------|-------------|----------------|
| 다중 Pod 간 전역 쿼터 정합성 | 인가 결정을 매니저 루프 한 곳으로 단일화 (Leader만 수행) | 분산 락·외부 스토리지 불필요 → 인프라 의존성 0 ([§2.1](#21-요청-처리-흐름)) |
| 쿼터 초과 PR 처리 | Admission 단계에서 `PipelineRunPending` 주입 | PR 강제 삭제/재생성 없는 비파괴적 제어 |
| 고가용성 | K8s Lease 기반 Leader Election | ZooKeeper 등 외부 코디네이터 직접 운영 불필요 |

> **왜 Go(controller-runtime)가 아니라 Python인가?**
> 이 컨트롤러의 정합성·HA 보장은 프레임워크가 아니라 **K8s 네이티브 프리미티브**(ConfigMap `resourceVersion` 낙관적 락, Lease)에 있습니다 — 즉 correctness가 언어에 독립적입니다. 덕분에 controller-runtime의 무게 없이, leader/manager/watcher가 하나의 인메모리 캐시를 공유하는 **단일 경량 프로세스**로 단순하게 유지됩니다. 이 공유 캐시 모델 때문에 멀티프로세스는 정합성을 깨뜨리므로, Gunicorn을 `workers=1` + `threads=8` 스레드 동시성으로 **의도적으로 고정**했습니다([§10.1](#101-dockerfile-구조)).

### 🔒 보안 가드레일

| 계층 | 조치 |
|------|------|
| **전송** | Webhook은 TLS 전용(8443/HTTPS), 자체 서명 인증서 + `caBundle` 검증 |
| **실행** | 컨테이너 non-root 실행(uid 1001), `python:3.11-slim` 최소 베이스 |
| **네트워크** | `install/networkpolicy.yaml`로 ingress/egress 제한 |
| **권한** | 컨트롤러 SA는 필요한 리소스에 대한 최소 RBAC만 보유 (상세: [`docs/security.md`](docs/security.md)) |

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

- **승격 주기:** `agingIntervalSec` (기본 180초)마다 effective tier가 1 감소
- **승격 하한:** `agingMinTier` (기본 1) 이하로는 내려가지 않음
- **Tier 0 보호:** 에이징으로 Tier 0(긴급)에 도달할 수 없으므로, 수동 긴급 배포의 최우선 지위가 항상 보장됨

```mermaid
flowchart LR
    A["대기 PR<br/>orig_tier, 대기시간 W"] --> B["aging_bonus =<br/>⌊ W / agingIntervalSec ⌋"]
    B --> C["effective_tier =<br/>max(agingMinTier, orig_tier − bonus)"]
    C --> D{"effective_tier<br/>최종값"}
    D --> E["정렬 키 =<br/>(effective_tier ASC,<br/>creationTimestamp FIFO)"]

    G["🛡️ Tier 0 보호벽<br/>agingMinTier = 1 가드<br/>→ 승격이 Tier 0을 침범 불가"] -. 보장 .-> C

    classDef guard fill:#cf222e,stroke:#a40e26,color:#fff;
    class G guard;
```

**예시** (`agingIntervalSec=180`, `agingMinTier=1`): Tier 3 PR이 9분(540초) 대기하면 `bonus=3` → `effective_tier = max(1, 3−3) = 1` 로 승격되어 운영(Tier 1)과 동등하게 경쟁하지만, **Tier 0(긴급)은 절대 넘볼 수 없습니다.**

### 4.3. 긴급 배포

PipelineRun 생성 시 아래 라벨을 추가하면 Tier 0으로 분류됩니다.

```yaml
metadata:
  labels:
    queue.tekton.dev/urgent: "true"
```

---

## 5. 네임스페이스 설정

GlobalLimit CRD의 `spec.namespacePatterns`에서 설정합니다. `fnmatch` 문법(`*`, `?`, `[seq]`)을 지원하며, 재배포 없이 런타임에 변경 가능합니다.

```yaml
apiVersion: tekton.devops/v1
kind: GlobalLimit
metadata:
  name: tekton-queue-limit
spec:
  namespacePatterns:
    - "*-cicd"
    - "production-*"
  maxPipelines: 10
```

```bash
kubectl patch globallimit tekton-queue-limit --type=merge \
  -p '{"spec":{"namespacePatterns":["*-cicd","newapp-*"]}}'
```

| 패턴 | 매칭 | 불일치 |
|------|------|--------|
| `*-cicd` | `myapp-cicd`, `test-cicd` | `myapp-deploy` |
| `tekton-*` | `tekton-pipelines` | `my-tekton` |
| `prod-*` | `prod-api`, `prod-web` | `staging-api` |

---

## 6. 고가용성 (HA) 구성

Kubernetes Lease 기반 Leader Election으로 **다중 Pod(replicas ≥ 2)** 구성을 지원합니다.

| 파라미터 | 값 | 설명 |
|---------|-----|------|
| Lease Duration | 15초 | Lease 유효 기간 |
| Retry Period | 2초 | Lease 체크/갱신 주기 |
| 장애 복구 시간 | ~15초 | Leader 장애 시 Standby 승격까지 최대 시간 |

```bash
kubectl exec -n tekton-pipelines <pod-name> -- curl -sk https://localhost:8443/healthz
# {"leader": true, "pod": "tekton-queue-controller-xxx", "status": "ok"}
```

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

# 2. GlobalLimit 설정
kubectl apply -f install/limit-setting.yaml

# 3. Controller 배포
#    ⚠️ deploy.yaml의 caBundle을 실제 값으로 교체하세요:
#    cat tls.crt | base64 | tr -d '\n'
kubectl apply -f install/deploy.yaml
```

### 7.4. 배포 확인

```bash
kubectl get pods -n tekton-pipelines -l app=tekton-queue
kubectl get globallimits
kubectl exec -n tekton-pipelines <pod-name> -- curl -sk https://localhost:8443/healthz
```

---

## 8. 설정 참조

### 8.1. GlobalLimit CRD 필드

| 필드 | 타입 | 필수 | 기본값 | 설명 |
|------|------|------|--------|------|
| `spec.namespacePatterns` | `string[]` | ❌ | `["*-cicd"]` | 관리 대상 네임스페이스 패턴 목록 |
| `spec.maxPipelines` | `integer` | ✅ | - | 동시 실행 가능한 최대 파이프라인 수 |
| `spec.agingIntervalSec` | `integer` | ❌ | 180 | 에이징 승격 주기 (초) |
| `spec.agingMinTier` | `integer` | ❌ | 1 | 에이징으로 도달 가능한 최소 Tier |
| `spec.tierRules` | `object[]` | ❌ | 기본 규칙 | 티어 분류 규칙 배열 |
| `spec.managedSAPatterns` | `string[]` | ❌ | env var 또는 `["system:serviceaccount:tekton-pipelines:tekton-dashboard"]` | 큐가 관리하는 SA 패턴 목록 (fnmatch 문법) |

### 8.2. 환경변수

| 환경변수 | 기본값 | 설명 |
|---------|--------|------|
| `POD_NAME` | `controller-{PID}` | Pod 이름 (Leader Election용, Downward API로 주입) |
| `POD_NAMESPACE` | `tekton-pipelines` | Pod 네임스페이스 (Lease 생성 위치) |
| `LEASE_NAME` | `tekton-queue-controller-leader` | Leader Election Lease 리소스 이름 |
| `MANAGED_SA_PATTERNS` | `system:serviceaccount:tekton-pipelines:tekton-dashboard` | 큐가 관리하는 SA 패턴 (기본값 단일 패턴, CRD에서 배열로 확장 가능) |

### 8.3. 엔드포인트

| 경로 | 포트 | 설명 |
|------|------|------|
| `/mutate` | 8443 (HTTPS) | Admission Webhook 엔드포인트 |
| `/healthz` | 8443 (HTTPS) | Liveness Probe (leader 상태 포함) |
| `/readyz` | 8443 (HTTPS) | Readiness Probe (초기 동기화 상태) |
| `/metrics` | 9090 (HTTP) | Prometheus 메트릭 |

---

## 9. 모니터링 (Prometheus)

| 메트릭 | 타입 | 라벨 | 설명 |
|--------|------|------|------|
| `tekton_queue_limit` | Gauge | - | 글로벌 동시 실행 허용량 |
| `tekton_queue_running_total` | Gauge | - | 현재 실행 중인 파이프라인 수 |
| `tekton_queue_pending_total` | Gauge | `tier` | 대기열 파이프라인 수 (Tier별) |
| `tekton_queue_webhook_admitted_total` | Counter | `tier` | (구버전 지표) 웹훅이 직접 인가하던 시절의 허용 횟수. 현재는 증가하지 않음 |
| `tekton_queue_webhook_queued_total` | Counter | `tier` | Dashboard PR 쿼터 초과 대기열 진입 횟수 |
| `tekton_queue_webhook_held_total` | Counter | `tier` | Dashboard 외 출처 PR 보류 횟수 |
| `tekton_queue_scheduled_total` | Counter | `tier` | Manager 스케줄링 횟수 |
| `tekton_queue_kubernetes_api_errors_total` | Counter | `operation` | K8s API 에러 횟수 |

```yaml
scrape_configs:
  - job_name: tekton-queue-controller
    static_configs:
      - targets:
        - tekton-queue-controller.tekton-pipelines.svc.cluster.local:9090
```

---

## 10. Docker 이미지 빌드

### 10.1. Dockerfile 구조

이미지는 `docker/Dockerfile`에 정의되어 있으며, **Python 3.11-slim** 기반으로 빌드됩니다. WSGI 서버로 **Gunicorn**을 사용합니다.

```
python:3.11-slim (Base)
  └─ /app
       ├── requirements.txt    ← pip 의존성 설치
       ├── src/                 ← 비즈니스 로직 모듈
       └── gunicorn.conf.py     ← Gunicorn 설정 (진입점: post_fork에서 스레드 기동)
```

| 레이어 | 설명 |
|--------|------|
| `COPY requirements.txt` → `pip install` | 의존성만 먼저 설치하여 Docker 캐시 최적화 |
| `COPY src/`, `COPY gunicorn.conf.py` | 소스 코드 및 서버 설정 복사 |
| `useradd -u 1001 appuser` | 보안을 위한 비루트 사용자 실행 |
| `EXPOSE 8443 / 9090` | Webhook(HTTPS) 및 Prometheus 메트릭 포트 |
| `CMD gunicorn -c gunicorn.conf.py src.webhook:app` | Gunicorn으로 Flask 앱 구동 |

**Gunicorn 설정 (`docker/gunicorn.conf.py`):**

| 항목 | 값 | 이유 |
|------|-----|------|
| `workers` | 1 | leader/manager/watcher 스레드가 in-memory 캐시를 공유하므로 멀티 프로세스 불가 |
| `worker_class` | gthread | 스레드 기반 동시 요청 처리 (Flask `threaded=True`와 동일한 모델) |
| `threads` | 8 | 동시 Webhook 요청 처리 |
| `timeout` | 120 | K8s Webhook 타임아웃(최대 30초)보다 길게 설정하여 worker 불필요 재시작 방지 |

### 10.2. 이미지 빌드 (수동)

프로젝트 루트 디렉터리에서 실행합니다.

```bash
# 기본 빌드
docker build -t tekton-queue-controller:local -f docker/Dockerfile .
```

빌드가 완료되면 이미지를 확인합니다.

```bash
docker images | grep tekton-queue-controller
# tekton-queue-controller   local   abc123def456   10 seconds ago   185MB
```

<details>
<summary><b>10.3. 이미지 태깅 &amp; 레지스트리 Push</b> (표준 Docker 절차 — 펼치기)</summary>

```bash
# 버전 / 레지스트리 태그 지정
docker tag tekton-queue-controller:local <REGISTRY_HOST>/tekton-queue-controller:v1.0.0

# 로그인 후 푸시
docker login <REGISTRY_HOST>
docker push <REGISTRY_HOST>/tekton-queue-controller:v1.0.0
```

> **참고:** `install/deploy.yaml`의 `spec.containers[].image` 값을 푸시한 이미지 경로로 변경해야 합니다.

</details>

### 10.4. Kind 로컬 클러스터에 로드

프라이빗 레지스트리 없이 로컬 Kind 클러스터에서 테스트하려면 이미지를 직접 로드합니다.

```bash
# Kind 클러스터에 이미지 로드
kind load docker-image tekton-queue-controller:local --name tekton-test
```

### 10.5. Makefile 단축 명령

위 과정을 Makefile로 간편하게 실행할 수 있습니다.

```bash
make build       # Docker 이미지 빌드
make load        # 빌드 + Kind 클러스터 로드
make deploy      # K8s 리소스 배포 (install/ 디렉터리)
make all         # 빌드 + 로드 + 배포 (전체 워크플로우)
make kind-setup  # Kind 클러스터 생성 + Tekton + 컨트롤러 자동 배포
make kind-test   # Kind 클러스터에서 S3 Burst 재현 (24개 PipelineRun)
make test        # pytest 단위 테스트
make simulate    # S1~S4 스케줄링 시뮬레이션 (K8s 불필요)
make lint        # flake8 코드 검사
make clean       # 이미지 삭제
```

Makefile 변수를 오버라이드하여 이미지 이름과 태그를 변경할 수 있습니다.

```bash
# 커스텀 이미지 이름/태그로 빌드
make build IMAGE_NAME=harbor.example.com/devops/tekton-queue-controller IMAGE_TAG=v1.0.0

# 다른 Kind 클러스터에 로드
make load CLUSTER_NAME=my-cluster
```

---

## 11. 프로젝트 구조

```
tekton_queue_controller/
├── Makefile                # 빌드 및 배포, 자동화 명령어
├── docker/
│   ├── Dockerfile          # 컨테이너 이미지 빌드 스크립트 (Python 3.11+)
│   ├── gunicorn.conf.py    # Gunicorn 프로덕션 서버 설정 (진입점)
│   └── requirements.txt    # Python 의존성 (Flask, Gunicorn, Kubernetes 등)
├── src/                    # 비즈니스 로직(Backend 모듈)
│   ├── __init__.py
│   ├── state.py            # Global 공유 상태 자원
│   ├── config.py           # CRD 설정 로드 및 환경변수
│   ├── cache.py            # 인메모리 PR 캐시 (Watch 인포머가 채움)
│   ├── metrics.py          # Prometheus 메트릭 정의
│   ├── webhook.py          # Flask 기반 Mutating Webhook API
│   └── workers/            # 백그라운드 Worker 스레드
│       ├── __init__.py
│       ├── leader.py       # Kubernetes Lease 기반 리더 선출
│       ├── manager.py      # 티어 기반 대기열 평가 및 스케줄링
│       └── watcher.py      # PipelineRun Informer/Watcher 동기화
├── docs/                   # 아키텍처 및 SRE 운영 문서
│   ├── architecture.md     # 시스템 아키텍처 및 상세 컴포넌트 구조
│   ├── security.md         # 네트워크 정책 및 권한 가이드
│   ├── runbook.md          # 롤백 및 장애 대응 매뉴얼
│   └── images/             # 다이어그램 (workflow.mmd/svg/png)
├── install/                # K8s 매니페스트
│   ├── crd.yaml
│   ├── deploy.yaml
│   ├── limit-setting.yaml
│   ├── networkpolicy.yaml
│   └── secret.yaml
└── grafana-dashboard/
    └── grafana-dashboard.json
```

---

## 12. 인가 경로 (Admission Path)

인가 결정은 **매니저 루프 한 곳에서만** 일어납니다. 웹훅은 판정하지 않습니다.

```mermaid
flowchart LR
    W["Webhook (모든 Pod)"] -- "PipelineRunPending + managed 라벨" --> Q[(대기열)]
    Q --> M["Manager (Leader Pod 전용)<br/>5초 주기"]
    M -- "available_slots = limit - running" --> S([빈 슬롯만큼 실행 시작])
    S -. "한 사이클에서 실행시킨 건수만큼<br/>남은 슬롯 차감" .-> M

    classDef run fill:#2ea44f,stroke:#1a7f37,color:#fff;
    class S run;
```

### 12.1. 왜 단일 지점인가

웹훅은 요청마다 독립적으로 실행됩니다. 슬롯 확인과 실행 허용을 웹훅에서 처리하면 동시에 들어온 요청들이 각자 자기 시점의 상태를 보고 판정하므로 순간적으로 상한을 넘길 수 있습니다. 실측에서 동시 12요청 시 상한 30에 대해 최대 48까지 올라갔습니다(직렬 도착에서는 재현되지 않음).

인가를 단일 스레드로 모으면 경쟁 자체가 성립하지 않습니다. 매니저는 한 사이클에서 실행시킨 건수를 세어 남은 슬롯을 차감하므로 사이클 내 초과도 없습니다.

### 12.2. 이전 구조에서 쓰던 admitted 카운터

이전에는 웹훅이 직접 인가했고, "통과시켰지만 아직 Watcher 캐시에 running 으로 반영되기 전"인 인플라이트 수를 ConfigMap 카운터로 메웠습니다. 그러나 카운터가 감소하는 시점과 그 건이 running 수에 반영되는 시점이 어긋나는 창이 남아 상한이 깨졌고, ConfigMap 낙관적 락 경합으로 웹훅 p99 지연이 6~9 ms 에서 925~5,000 ms 로 증가했습니다.

인가가 매니저로 단일화되면서 인플라이트 구간 자체가 사라졌으므로 카운터는 제거했습니다. `tekton_queue_webhook_admitted_total` 지표는 기존 대시보드 호환을 위해 정의만 남아 있으며 더 이상 증가하지 않습니다.

## 13. 검증 (Verification)

### 13.1. 로컬 개발 & 단위 테스트

클러스터 없이 소스에서 바로 테스트를 재현할 수 있습니다.

```bash
python -m venv .venv && source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -r docker/requirements.txt pytest flake8

make test        # = python -m pytest tests/ -v
make lint        # = flake8 src/ tests/ --max-line-length=120
make simulate    # S1~S4 스케줄링 로직 시뮬레이션 (K8s 불필요)
```

**158 passed** — Webhook 동시성·Leader Election·Watcher 재동기화·Tier 분류·self-healing OR-게이트 등 포함.

### 13.2. 통합 테스트 (실 클러스터, S1~S4)

`docker-desktop` Kubernetes에 Tekton + 컨트롤러를 실제 배포하여 4개 시나리오를 검증했습니다. **모든 시나리오에서 동시 실행 수가 `maxPipelines` 한도를 단 1건도 초과하지 않았습니다.**

| 시나리오 | 설명 | 완료 | 최대 동시 (peak / L_max) | 결과 |
|----------|------|------|--------------------------|------|
| **S1** Baseline | 정상 부하, Tier 1:2:3 = 1:2:7 | 10/10 | 5 / 5 | ✅ 슬롯 한도 준수 |
| **S2** Priority | Tier3×5 선점 후 Tier1×3 진입 | 8/8 | 3 / 3 | ✅ 큐 우선순위 정렬(비선점) |
| **S3** Burst | L_max×3 동시 생성 (15개) | 15/15 | 5 / 5 | ✅ 버스트 흡수 |
| **S4** Adversarial | λ(0.25/s) > μ(0.15/s) 포화 | 12/12 | 3 / 3 | ✅ 적체 후 완전 소진 |

> **S2 우선순위 참고:** 컨트롤러는 비선점형이므로 t=0에 빈 슬롯을 먼저 점유한 Tier3는 끝까지 실행됩니다. 우선순위는 **대기열에서 경쟁한 건**에만 적용되며, 실측 결과 대기 24s의 Tier1이 대기 63~74s의 Tier3보다 먼저 스케줄되어 정책이 정확히 동작함을 확인했습니다.

```bash
# 통합 시나리오 재현 (기본 컨텍스트: docker-desktop, KUBE_CONTEXT로 변경 가능)
python -m tests.integration_scenarios
```

---

## 14. 설계 한계 및 향후 과제

- **비선점형 설계:** 실행 중인 낮은 티어 파이프라인의 선점은 지원하지 않습니다. 우선순위 정렬은 대기열 진입 이후 Manager에서만 적용됩니다.
- **CRD 변경 반영 지연:** GlobalLimit CRD 변경 시 최대 5초의 반영 지연이 있습니다.
- **HA Failover 지연:** Leader 장애 시 최대 ~15초의 스케줄링 공백이 발생합니다. 이 동안 Webhook은 모든 Pod에서 정상 처리됩니다.
- **관리 SA 패턴 변경 시 재배포 필요:** `MANAGED_SA_PATTERNS` 환경변수 변경은 Pod 재시작이 필요합니다. SA 패턴 추가는 `spec.managedSAPatterns` 배열에 항목을 추가하면 재배포 없이 반영됩니다.
- **self-healing 관측성:** 자가 치유 발동은 현재 **로그로만** 기록됩니다. SRE 알람 연동을 위한 전용 Prometheus 카운터(예: `tekton_queue_self_healing_total`) 노출은 향후 과제입니다.

---

## 라이선스

[MIT](LICENSE) © 2026 Wongjun Jang
