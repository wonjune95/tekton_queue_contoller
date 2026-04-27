# Tekton Queue Controller — Architecture

## 1. 개요

Tekton Queue Controller는 Kubernetes MutatingAdmissionWebhook + 백그라운드 스케줄러로 구성된 컨트롤러입니다.  
Dashboard를 통해 생성되는 PipelineRun의 동시 실행 수를 제한하고, 우선순위(Tier) 기반으로 대기열을 관리합니다.

---

## 2. 컴포넌트 다이어그램

```
┌─────────────────────────────────────────────────────────────┐
│                  Kubernetes Cluster                          │
│                                                             │
│  ┌───────────┐   CREATE PR   ┌────────────────────────────┐ │
│  │ Dashboard │──────────────▶│  MutatingAdmissionWebhook  │ │
│  └───────────┘               │  /mutate  (Flask HTTPS)    │ │
│                              └────────────┬───────────────┘ │
│                                           │ JSONPatch        │
│                                           ▼                  │
│  ┌──────────────────────────────────────────────────────┐   │
│  │               tekton-queue-controller Pod             │   │
│  │                                                      │   │
│  │  ┌──────────┐  ┌──────────┐  ┌────────────────────┐ │   │
│  │  │  Watcher │  │ Manager  │  │  Leader Election   │ │   │
│  │  │  Thread  │  │  Thread  │  │    Thread (Lease)  │ │   │
│  │  └────┬─────┘  └────┬─────┘  └────────────────────┘ │   │
│  │       │              │                                │   │
│  │       ▼              ▼                                │   │
│  │  ┌─────────────────────────┐                         │   │
│  │  │      In-Memory Cache    │                         │   │
│  │  │  (local_cache + lock)   │                         │   │
│  │  └─────────────────────────┘                         │   │
│  │                                                      │   │
│  │  ┌──────────────────────────┐                        │   │
│  │  │  Prometheus Metrics :9090│                        │   │
│  │  └──────────────────────────┘                        │   │
│  └──────────────────────────────────────────────────────┘   │
│                        │                                    │
│           ┌────────────┼───────────┐                        │
│           ▼            ▼           ▼                        │
│      PipelineRun   ConfigMap    Lease                       │
│      (patch)     (admitted cnt) (leader)                    │
└─────────────────────────────────────────────────────────────┘
```

---

## 3. Webhook 처리 흐름

```
PipelineRun CREATE 요청
        │
        ▼
대상 네임스페이스인가? ──No──▶ 그냥 통과 (allowed=True)
        │ Yes
        ▼
Dashboard SA 출처인가? ──No──▶ Pending 설정 (managed 라벨 없음, 스케줄링 제외)
        │ Yes
        ▼
캐시 초기 동기화 완료? ──No──▶ 그냥 통과 (캐시 미준비 방어)
        │ Yes
        ▼
쿼터(running + admitted) < limit? ──No──▶ Pending + managed 라벨 추가 (대기열)
        │ Yes
        ▼
ConfigMap admitted+1 (원자적)
        │
        ▼
Tier 라벨 추가 → 즉시 실행 허용
```

---

## 4. Tier (우선순위) 결정 로직

| Tier | 조건 | 설명 |
|------|------|------|
| 0 | `queue.tekton.dev/urgent: "true"` 라벨 | 긴급 배포 |
| 1 | `env` 라벨이 `prod` 패턴 매칭 | 운영 배포 |
| 2 | `env` 라벨이 `stg` 패턴 매칭 | 검증 배포 |
| 3 | 그 외 (기본값) | 개발 |

**Aging**: 대기 시간이 `aging_interval_sec` 초마다 Tier 1씩 승격 (최소 `aging_min_tier`까지).

---

## 5. Leader Election (HA)

- K8s `coordination.k8s.io/v1` Lease 리소스를 사용
- Lease 이름: `tekton-queue-controller-leader` (기본값)
- `LEASE_DURATION_SEC=15`, `LEASE_RETRY_PERIOD_SEC=2`
- **리더만** Manager 루프(스케줄링)를 실행함
- 리더 교체 시 `_reset_global_admitted()` 호출로 ConfigMap 카운터 초기화

---

## 6. 모듈 의존 관계

```
config.py ◀─── metrics.py
    │
    ├──────▶ cache.py ◀─── state.py
    │            │
    │            ▼
    └──────▶ webhook.py
                 │
        workers/*.py ◀── state.py
```

| 모듈 | 의존 |
|------|------|
| `config.py` | `kubernetes` 클라이언트, 환경 변수 |
| `metrics.py` | `prometheus_client` |
| `state.py` | `threading` |
| `cache.py` | `config.py`, `metrics.py`, `state.py` |
| `webhook.py` | `flask`, `cache.py`, `config.py`, `metrics.py`, `state.py` |
| `workers/leader.py` | `config.py`, `metrics.py`, `state.py`, `cache.py` |
| `workers/manager.py` | `config.py`, `metrics.py`, `cache.py`, `state.py` |
| `workers/watcher.py` | `config.py`, `metrics.py`, `cache.py`, `state.py` |

---

## 7. 포트 및 엔드포인트

| 포트 | 프로토콜 | 용도 |
|------|----------|------|
| 8443 | HTTPS | Webhook `/mutate`, `/healthz`, `/readyz` |
| 9090 | HTTP | Prometheus `/metrics` |

---

## 8. Prometheus 메트릭 목록

| 메트릭 | 종류 | 설명 |
|--------|------|------|
| `tekton_queue_limit` | Gauge | 설정된 최대 동시 실행 수 |
| `tekton_queue_running_total` | Gauge | 현재 실행 중인 수 |
| `tekton_queue_pending_total` | Gauge | Tier별 대기 중인 수 |
| `tekton_queue_webhook_admitted_total` | Counter | Webhook에서 즉시 허용된 수 |
| `tekton_queue_webhook_queued_total` | Counter | 쿼터 초과로 대기열 이동된 수 |
| `tekton_queue_webhook_held_total` | Counter | Dashboard 외 출처로 보류된 수 |
| `tekton_queue_scheduled_total` | Counter | Manager가 스케줄링한 수 |
| `tekton_queue_kubernetes_api_errors_total` | Counter | K8s API 에러 횟수 |
