# Tekton Global Queue Controller

다수의 네임스페이스에 걸쳐 실행되는 Tekton PipelineRun의 **전역 동시 실행 개수(Global Concurrency Limits)**를 통제하고, **우선순위 기반 스케줄링**을 제공하는 Kubernetes Mutating Admission Webhook 컨트롤러입니다.

## 1. 개요 (Overview)

Tekton Pipelines는 클러스터 전체 단위의 동시 실행 개수를 제한하는 기능을 포함하지 않습니다. 이로 인해 대규모 배포 요청이 동시에 발생하면 자원 경쟁으로 인한 OOM 및 노드 장애가 발생할 수 있습니다.

본 컨트롤러는 **Kubernetes MutatingAdmissionWebhook**을 활용하여 API Server 인입 단계에서 파이프라인 생성 요청을 선제적으로 통제하며, 다음의 기능을 제공합니다.

1. **사전 차단 (Proactive Control):** 쿼터를 초과한 실행 요청에 `PipelineRunPending` 상태를 즉시 주입하여 자원 고갈을 원천 차단합니다.
2. **우선순위 스케줄링:** CRD 기반의 티어 분류 규칙(label/env 매칭)을 통해 긴급 배포를 우선 처리하고, 에이징(Aging) 메커니즘으로 기아(Starvation) 현상을 방지합니다.
3. **멀티 네임스페이스 지원:** 특정 패턴(예: `*-cicd`)을 가진 여러 네임스페이스를 단일 컨트롤러에서 통합 관리합니다.
4. **API Server 부하 최소화:** `SharedInformer` 패턴의 로컬 인메모리 캐시를 조회하여 Admission 판정을 수행합니다.
5. **결함 허용성 (Fault Tolerance):** 대기열 순서를 etcd의 `creationTimestamp` 기준으로 정렬하여, Controller Pod 재시작 시에도 순서가 보장됩니다.

---

## 2. 연동 대시보드 (Custom Dashboard)

본 컨트롤러가 관리하는 Global Queue 상태를 웹 UI에서 모니터링하고 제어하려면, 아래의 대시보드를 함께 사용하는 것을 권장합니다.

<img width="1901" height="908" alt="스크린샷 2026-03-02 190526" src="https://github.com/user-attachments/assets/b2d062ae-370f-4a41-b1d6-be36ed03a076" />

🔗 **[wonjune95/tekton_ui_custom_dashboard](https://github.com/wonjune95/tekton_ui_custom_dashboard)**

---

## 3. 아키텍처 설계 특징 (Architectural Features)

| 설계 항목 | 구현 방식 | 기대 효과 |
| --- | --- | --- |
| **제어 시점** | K8s API Server 인입 시점 (Admission Phase) | 불필요한 이벤트 전파 방지 |
| **초과 쿼터 처리** | `JSONPatch`를 통한 Pending 상태 + 티어 라벨 주입 | 강제 삭제/재생성 로직 제거 |
| **우선순위 분류** | CRD `tierRules`에 의한 label/env 기반 자동 티어 부여 | 운영 정책 변경 시 코드 수정 불필요 |
| **기아 방지** | 대기 시간 기반 에이징으로 effective tier 자동 승격 | 낮은 우선순위 파이프라인의 무기한 대기 방지 |
| **대기열 정합성** | `creationTimestamp` 기준 정렬 (FIFO) | Pod 재시작 시 순서 보장 |
| **취소/중지 처리** | `Cancelled`, `CancelledRunFinally`, `StoppedRunFinally` 상태 감지 | 취소된 파이프라인의 슬롯 즉시 반환 |
| **Race Condition 방어** | `webhook_admitted_count`로 Webhook-Watcher 간 정합성 유지 | 동시 CREATE 시 쿼터 초과 방지 |

---

## 4. 우선순위 스케줄링 (Priority Scheduling)

### 4.1. 티어 분류 체계

GlobalLimit CRD의 `tierRules`를 통해 PipelineRun의 우선순위를 자동 분류합니다. 규칙은 순서대로 매칭되며, 먼저 매칭된 규칙이 적용됩니다.

| 매칭 순서 | matchType | 매칭 대상 | 예시 | Tier |
| --- | --- | --- | --- | --- |
| 1순위 | `label` | `metadata.labels`의 지정 키 | `queue.tekton.dev/urgent: "true"` | 0 (긴급) |
| 2순위 | `env` | `metadata.labels.env` | `prod` | 1 (운영) |
| 3순위 | `env` | `metadata.labels.env` | `stg` | 2 (검증) |
| 기본값 | `env` | `metadata.labels.env` | `*` (나머지) | 3 (개발) |

### 4.2. 에이징 (Aging) 메커니즘

대기열에서 장시간 대기하는 파이프라인의 effective tier를 자동으로 승격시켜 기아 현상을 방지합니다.

- **승격 주기:** `agingIntervalSec` (기본 180초)마다 effective tier가 1 감소
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

## 5. 설치 및 배포 (Installation)

### 5.1. 사전 요구사항

* Kubernetes Cluster (v1.20+)
* Tekton Pipelines 설치 완료
* OpenSSL (웹훅용 TLS 인증서 생성)

### 5.2. TLS 인증서 및 Secret 생성

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
DNS.1 = tekton-queue-webhook-service
DNS.2 = tekton-queue-webhook-service.tekton-pipelines
DNS.3 = tekton-queue-webhook-service.tekton-pipelines.svc
EOF

openssl genrsa -out tls.key 2048
openssl req -new -key tls.key -out tls.csr \
  -subj "/CN=tekton-queue-webhook-service.tekton-pipelines.svc" \
  -config csr.conf
openssl x509 -req -in tls.csr -signkey tls.key -out tls.crt \
  -days 3650 -extensions v3_req -extfile csr.conf

kubectl create secret tls tekton-queue-cacerts \
  --cert=tls.crt --key=tls.key -n tekton-pipelines
```

### 5.3. CRD 및 GlobalLimit 설정

```yaml
# globallimit-crd.yaml
apiVersion: apiextensions.k8s.io/v1
kind: CustomResourceDefinition
metadata:
  name: globallimits.tekton.devops
spec:
  group: tekton.devops
  names:
    kind: GlobalLimit
    plural: globallimits
    singular: globallimit
    shortNames:
      - gl
  scope: Cluster
  versions:
    - name: v1
      served: true
      storage: true
      schema:
        openAPIV3Schema:
          type: object
          properties:
            spec:
              type: object
              required:
                - maxPipelines
              properties:
                maxPipelines:
                  type: integer
                  description: "동시 실행 가능한 최대 파이프라인 수"
                agingIntervalSec:
                  type: integer
                  default: 180
                  description: "에이징 승격 주기 (초)"
                agingMinTier:
                  type: integer
                  default: 1
                  description: "에이징으로 도달 가능한 최소 Tier (0은 긴급 전용)"
                tierRules:
                  type: array
                  description: "티어 분류 규칙 (순서대로 매칭)"
                  items:
                    type: object
                    required:
                      - tier
                      - matchType
                      - pattern
                    properties:
                      tier:
                        type: integer
                      matchType:
                        type: string
                        enum: [label, env]
                      labelKey:
                        type: string
                      pattern:
                        type: string
                      description:
                        type: string
      additionalPrinterColumns:
        - name: MaxPipelines
          type: integer
          jsonPath: .spec.maxPipelines
        - name: AgingInterval
          type: integer
          jsonPath: .spec.agingIntervalSec
        - name: MinTier
          type: integer
          jsonPath: .spec.agingMinTier
---
apiVersion: tekton.devops/v1
kind: GlobalLimit
metadata:
  name: tekton-queue-limit
spec:
  maxPipelines: 10
  agingIntervalSec: 180
  agingMinTier: 1
  tierRules:
    - tier: 0
      matchType: label
      labelKey: "queue.tekton.dev/urgent"
      pattern: "true"
      description: "긴급 배포 (수동 실행)"
    - tier: 1
      matchType: env
      pattern: "prod"
      description: "운영 배포"
    - tier: 2
      matchType: env
      pattern: "stg"
      description: "검증 배포"
    - tier: 3
      matchType: env
      pattern: "*"
      description: "개발 (기본값)"
```

```bash
kubectl apply -f globallimit-crd.yaml
```

### 5.4. 통합 배포 (Deploy)

> **주의:** `caBundle` 필드는 `cat tls.crt | base64 | tr -d '\n'`으로 추출한 실제 값을 입력하세요.

```yaml
# deploy.yaml

# --- Dashboard Viewer RBAC ---
apiVersion: rbac.authorization.k8s.io/v1
kind: ClusterRole
metadata:
  name: tekton-dashboard-globallimit-viewer
rules:
- apiGroups: ["tekton.devops"]
  resources: ["globallimits"]
  verbs: ["get", "list", "watch"]
---
apiVersion: rbac.authorization.k8s.io/v1
kind: ClusterRoleBinding
metadata:
  name: tekton-dashboard-globallimit-binding
subjects:
- kind: ServiceAccount
  name: tekton-dashboard
  namespace: tekton-pipelines
roleRef:
  kind: ClusterRole
  name: tekton-dashboard-globallimit-viewer
  apiGroup: rbac.authorization.k8s.io

# --- Controller RBAC ---
---
apiVersion: v1
kind: ServiceAccount
metadata:
  name: queue-controller
  namespace: tekton-pipelines
---
apiVersion: rbac.authorization.k8s.io/v1
kind: ClusterRole
metadata:
  name: queue-controller-role
rules:
  - apiGroups: ["tekton.dev"]
    resources: ["pipelineruns", "pipelineruns/status"]
    verbs: ["list", "watch", "get", "patch", "update", "delete", "create"]
  - apiGroups: ["tekton.devops"]
    resources: ["globallimits"]
    verbs: ["list", "watch", "get"]
  - apiGroups: [""]
    resources: ["events", "namespaces"]
    verbs: ["create", "list", "watch"]
  - apiGroups: ["apiextensions.k8s.io"]
    resources: ["customresourcedefinitions"]
    verbs: ["list", "watch", "get"]
---
apiVersion: rbac.authorization.k8s.io/v1
kind: ClusterRoleBinding
metadata:
  name: queue-controller-binding
roleRef:
  apiGroup: rbac.authorization.k8s.io
  kind: ClusterRole
  name: queue-controller-role
subjects:
  - kind: ServiceAccount
    name: queue-controller
    namespace: tekton-pipelines

# --- Webhook Service & Configuration ---
---
apiVersion: v1
kind: Service
metadata:
  name: tekton-queue-webhook-service
  namespace: tekton-pipelines
spec:
  ports:
    - port: 443
      targetPort: 8443
  selector:
    app: tekton-queue
---
apiVersion: admissionregistration.k8s.io/v1
kind: MutatingWebhookConfiguration
metadata:
  name: tekton-queue-mutator
webhooks:
  - name: queue.mutator.tekton.dev
    clientConfig:
      service:
        name: tekton-queue-webhook-service
        namespace: tekton-pipelines
        path: "/mutate"
      caBundle: "<BASE64_ENCODED_CA_CERT_HERE>"
    rules:
      - operations: ["CREATE"]
        apiGroups: ["tekton.dev"]
        apiVersions: ["v1", "v1beta1"]
        resources: ["pipelineruns"]
    admissionReviewVersions: ["v1", "v1beta1"]
    sideEffects: None
    timeoutSeconds: 5

# --- Controller Deployment ---
---
apiVersion: apps/v1
kind: Deployment
metadata:
  name: tekton-queue-controller
  namespace: tekton-pipelines
spec:
  replicas: 1
  selector:
    matchLabels:
      app: tekton-queue
  template:
    metadata:
      labels:
        app: tekton-queue
    spec:
      serviceAccountName: queue-controller
      securityContext:
        runAsNonRoot: true
        runAsUser: 1001
        fsGroup: 1001
      containers:
        - name: manager
          image: docker.io/tekton-queue-controller:v0.2.0
          imagePullPolicy: Always
          securityContext:
            allowPrivilegeEscalation: false
            capabilities:
              drop: ["ALL"]
            seccompProfile:
              type: RuntimeDefault
          resources:
            requests:
              memory: "128Mi"
              cpu: "100m"
            limits:
              memory: "256Mi"
              cpu: "500m"
          ports:
            - containerPort: 8443
          livenessProbe:
            httpGet:
              path: /healthz
              port: 8443
              scheme: HTTPS
            initialDelaySeconds: 10
            periodSeconds: 15
          readinessProbe:
            httpGet:
              path: /readyz
              port: 8443
              scheme: HTTPS
            initialDelaySeconds: 5
            periodSeconds: 10
          volumeMounts:
            - name: cert-volume
              mountPath: "/certs"
              readOnly: true
      volumes:
        - name: cert-volume
          secret:
            secretName: tekton-queue-cacerts
```

---

## 6. 내부 동작 흐름 (How it works)

1. **사전 통제 (Webhook):** PipelineRun 생성 요청 시 K8s API Server가 `/mutate` 엔드포인트를 호출합니다. 컨트롤러는 로컬 캐시를 확인하여 쿼터 초과 시 `PipelineRunPending` 상태, 관리 라벨(`queue.tekton.dev/managed`), 티어 라벨(`queue.tekton.dev/tier`)을 JSONPatch로 주입합니다. 티어는 CRD의 `tierRules`에 의해 자동 분류됩니다.

2. **상태 동기화 (Watcher):** 백그라운드 스레드에서 K8s Watch 스트림으로 PipelineRun 상태 변화를 로컬 캐시에 실시간 반영합니다. `resourceVersion` 만료(410 Gone) 시 전체 재동기화를 수행하며, 일시적 API 에러 시에는 기존 위치에서 재연결을 시도합니다.

3. **큐 스케줄링 (Manager):** 5초 주기로 구동되며, 빈 슬롯이 발생하면 대기열에서 우선순위 순으로 파이프라인을 꺼내 실행합니다. 정렬 기준은 effective tier(오름차순) → creationTimestamp(FIFO)이며, 에이징에 의해 장기 대기 파이프라인의 effective tier가 자동 승격됩니다.

4. **취소/중지 처리:** `spec.status`가 `Cancelled`, `CancelledRunFinally`, `StoppedRunFinally`인 PipelineRun은 running/pending 카운트에서 즉시 제외되어 슬롯이 반환됩니다.

---

## 7. 설계 한계 및 향후 과제

- **비선점형 설계:** Webhook 단계에서는 슬롯 가용 여부만 판단하며 우선순위를 고려하지 않습니다. 우선순위 정렬은 대기열 진입 이후 Manager에서 적용됩니다. 실행 중인 낮은 티어 파이프라인의 선점은 지원하지 않습니다.
- **Webhook-Manager 경합:** 빌드 완료 후 트리거되는 배포 PipelineRun이 Webhook 경로로 슬롯을 먼저 차지하여, 대기열의 높은 우선순위 파이프라인보다 먼저 실행될 수 있습니다.
- **`webhook_admitted_count` 정합성:** Webhook 통과 후 PipelineRun 생성 자체가 실패하면 카운터가 일시적으로 높게 유지됩니다. 전체 재동기화 시 리셋되어 최종 정합성이 보장됩니다.
- **CRD 변경 반영 지연:** GlobalLimit CRD 변경 시 최대 5초의 반영 지연이 있습니다.

---

## 8. 빌드 가이드 (Build)

```dockerfile
FROM python:3.9-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY app.py .
USER 1001
EXPOSE 8443
CMD ["python", "app.py"]
```

**requirements.txt:**

```text
Flask==3.0.0
kubernetes==28.1.0
```

```bash
docker build -t your-registry/tekton-queue-controller:v0.2.0 .
docker push your-registry/tekton-queue-controller:v0.2.0
```