# Tekton Global Queue Controller

다수의 네임스페이스에 걸쳐 실행되는 Tekton PipelineRun의 **전역 동시 실행 개수(Global Concurrency Limits)**를 통제하기 위한 경량화된 Flask 기반의 Kubernetes Mutating Admission Webhook 및 컨트롤러입니다.

## 1. 개요 (Overview)

Tekton Pipelines는 기본적으로 네임스페이스별 리소스 할당량(ResourceQuota) 기능만 제공할 뿐, **클러스터 전체 단위의 동시 실행 개수를 제한하는 기능이 부족**합니다.

본 컨트롤러는 **Kubernetes MutatingAdmissionWebhook**을 활용하여 API Server 인입 단계에서 파이프라인 생성 요청을 선제적으로 통제함으로써 다음의 문제들을 해결합니다.

1. **사전 차단 (Proactive Control):** 쿼터를 초과한 실행 요청이 etcd에 기록되기 전에 미리 `PipelineRunPending` 상태를 주입하여 I/O 리소스 낭비를 원천 차단합니다.
2. **멀티 네임스페이스 지원:** 특정 패턴(예: `*-cicd`)을 가진 여러 네임스페이스를 단일 컨트롤러에서 통합 관리합니다.
3. **API Server 부하 최소화:** O(N)의 API Server 호출 대신 O(1)의 로컬 인메모리 캐시(`SharedInformer` 패턴)를 조회하여 인가(Admission) 여부를 1ms 이내로 결정합니다.
4. **결함 허용성 (Fault Tolerance):** 대기열의 순서를 컨트롤러 메모리에 의존하지 않고, etcd에 기록된 `creationTimestamp`를 기준으로 정렬(FIFO)하여 Controller Pod 재시작 시에도 순서가 붕괴되지 않습니다.

---

## 2. 연동 대시보드 (Custom Dashboard)

본 컨트롤러가 관리하는 Global Queue 상태를 웹 UI에서 직관적으로 모니터링하고 제어하려면, 맞춤형으로 제작된 아래의 오픈소스 대시보드를 함께 사용하는 것을 권장합니다.
<img width="1901" height="908" alt="스크린샷 2026-03-02 190526" src="https://github.com/user-attachments/assets/b2d062ae-370f-4a41-b1d6-be36ed03a076" />

🔗 **[wonjune95/tekton_ui_custom_dashboard](https://github.com/wonjune95/tekton_ui_custom_dashboard)**

*해당 대시보드가 K8s 클러스터 내의 `GlobalLimit` CRD 자원에 접근할 수 있도록, 설치 과정(`deploy.yaml`)에 필수 RBAC 권한이 포함되어 있습니다.*

---

## 3. 아키텍처 설계 특징 (Architectural Features)

기존 컨트롤러 패턴(생성된 리소스를 사후에 감지하여 강제 삭제 후 재생성하는 방식)이 유발하던 API Server와 etcd의 부하 문제를 해결하기 위해 다음과 같이 설계되었습니다.

| 설계 항목 | 구현 방식 및 특징 | 기대 효과 |
| --- | --- | --- |
| **제어 시점** | K8s API Server 인입 시점 (`Admission Phase`) | 시스템 부하를 유발하는 불필요한 이벤트 전파 방지 |
| **초과 쿼터 처리** | 인입 즉시 `JSONPatch`를 통해 Pending 상태 주입 | 파이프라인 강제 삭제 및 재생성 로직 제거 |
| **etcd Write 부하** | 1개 파이프라인 요청당 단 1회의 트랜잭션(생성)만 발생 | 기존 방식 대비 I/O 연산량 대폭 절감 |
| **대기열 정합성** | K8s Native Timestamp (`creationTimestamp`) 기준 정렬 | 단일 장애점(SPOF) 제거 및 안정적인 FIFO 큐 보장 |

---

## 4. 설치 및 배포 (Installation)

### 4.1. 사전 요구사항

* Kubernetes Cluster (v1.20+)
* Tekton Pipelines 설치 완료
* OpenSSL (웹훅용 TLS 인증서 생성 목적)

### 4.2. TLS 인증서 및 Secret 생성 (필수)

Webhook은 K8s API Server와 반드시 HTTPS로 통신해야 하므로 자체 서명 인증서를 생성하여 K8s Secret으로 배포해야 합니다.

```bash
# 1. 인증서 생성 (컨트롤러가 배포될 tekton-pipelines 네임스페이스에 맞게 SAN 설정)
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
openssl req -new -key tls.key -out tls.csr -subj "/CN=tekton-queue-webhook-service.tekton-pipelines.svc" -config csr.conf
openssl x509 -req -in tls.csr -signkey tls.key -out tls.crt -days 3650 -extensions v3_req -extfile csr.conf

# 2. Secret 배포
kubectl create secret tls tekton-queue-cacerts --cert=tls.crt --key=tls.key -n tekton-pipelines

```

### 4.3. CRD 및 GlobalLimit 설정

전역 제한 개수를 설정하기 위한 CRD와 실제 Limit 값을 정의합니다.

```yaml
# crd-and-limit.yaml
apiVersion: apiextensions.k8s.io/v1
kind: CustomResourceDefinition
metadata:
  name: globallimits.tekton.devops
spec:
  group: tekton.devops
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
              properties:
                maxPipelines:
                  type: integer
---
apiVersion: tekton.devops/v1
kind: GlobalLimit
metadata:
  name: tekton-queue-limit
spec:
  maxPipelines: 4  # 동시에 허용할 최대 파이프라인 실행 개수

```

### 4.4. 통합 권한 및 컨트롤러 배포 (Deploy)

대시보드 뷰어 권한, 컨트롤러 권한, Service, Deployment, 그리고 `MutatingWebhookConfiguration`이 모두 포함된 통합 배포 파일입니다.

> **주의:** 아래 YAML의 `caBundle` 필드는 예시 값입니다. 배포 전 반드시 `cat tls.crt | base64 | tr -d '\n'` 명령어로 추출한 실제 값을 입력하세요.

```yaml
# deploy.yaml
# 1. Dashboard Viewer RBAC
apiVersion: rbac.authorization.k8s.io/v1
kind: ClusterRole
metadata:
  name: tekton-dashboard-globallimit-viewer
rules:
- apiGroups:
  - "tekton.devops"
  resources:
  - "globallimits"
  verbs:
  - "get"
  - "list"
  - "watch"
---
apiVersion: rbac.authorization.k8s.io/v1
kind: ClusterRoleBinding
metadata:
  name: tekton-dashboard-globallimit-binding
subjects:
- kind: ServiceAccount
  name: tekton-dashboard           # 설치된 대시보드의 실제 ServiceAccount
  namespace: tekton-pipelines      # 대시보드가 설치된 네임스페이스
roleRef:
  kind: ClusterRole
  name: tekton-dashboard-globallimit-viewer
  apiGroup: rbac.authorization.k8s.io

# 2. Controller RBAC & ServiceAccount
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

# 3. Webhook Service & Configuration
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
      caBundle: "<BASE64_ENCODED_CA_CERT_HERE>" # 주의: 실제 Base64 인코딩 값으로 교체하세요!
    rules:
      - operations: ["CREATE"]
        apiGroups: ["tekton.dev"]
        apiVersions: ["v1", "v1beta1"]
        resources: ["pipelineruns"]
    admissionReviewVersions: ["v1", "v1beta1"]
    sideEffects: None
    timeoutSeconds: 5

# 4. Controller Deployment
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
          image: docker.io/tekton-queue-controller:v0.1.0 # 빌드한 이미지 주소로 변경
          imagePullPolicy: Always
          securityContext:
            allowPrivilegeEscalation: false
            capabilities:
              drop:
                - ALL
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

## 5. 내부 동작 흐름 (How it works)

1. **사전 통제 (Webhook Endpoint):** 파이프라인 생성 API 호출 시 K8s API Server가 컨트롤러의 `/mutate` 엔드포인트를 호출합니다. 컨트롤러는 로컬 캐시를 확인하여 현재 실행 중인 파이프라인이 쿼터를 초과했을 경우, `PipelineRunPending` 상태와 큐 관리용 라벨을 JSONPatch로 즉각 주입합니다.
2. **상태 동기화 (Watcher Thread):** 백그라운드 스레드에서 K8s API의 `Watch` 스트림을 활용해 클러스터 내 파이프라인의 상태 변화를 로컬 인메모리 캐시(`local_cache`)에 실시간으로 반영합니다.
3. **큐 스케줄링 (Manager Thread):** 5초 주기로 구동되며, 기존 실행 중이던 파이프라인이 종료되어 쿼터에 여유 슬롯이 생기면, 대기열(etcd `creationTimestamp` 기준 오름차순 정렬)에서 다음 파이프라인의 `status`를 `None`으로 패치하여 실행을 재개시킵니다.

---

## 6. 빌드 가이드 (Build)

```dockerfile
# Dockerfile
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
docker build -t your-registry/tekton-queue-controller:v0.1.0 .
docker push your-registry/tekton-queue-controller:v0.1.0

```
