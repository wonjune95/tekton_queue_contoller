# Tekton Global Queue Controller

A lightweight, Python-based Kubernetes controller to enforce **Global Concurrency Limits** for Tekton PipelineRuns across multiple namespaces.

## 개요 (Overview)

Tekton Pipelines는 기본적으로 네임스페이스별 리소스 할당량(ResourceQuota) 외에 **클러스터 전체 단위의 동시 실행 개수(Global Concurrency)** 를 제한하는 기능이 부족합니다.

이 컨트롤러는 다음과 같은 문제를 해결합니다.

1. **과부하 방지:** 클러스터 전체에서 동시에 실행되는 파이프라인 개수를 제한합니다. (예: 최대 2개)
2. **멀티 네임스페이스 지원:** 특정 패턴(예: `*-cicd`)을 가진 여러 네임스페이스를 통합 관리합니다.
3. **Strict Mode (Race Condition 해결):** Tekton이 컨트롤러보다 먼저 파이프라인을 실행시켜버리는 경우, 이를 감지하여 **즉시 삭제 후 대기열로 재등록**합니다.
4. **템플릿 자동 무시:** 기존에 만들어둔 Pending 상태의 템플릿 파이프라인은 건드리지 않고, **새로 실행 요청된 파이프라인만 관리**합니다.

---

## 주요 기능 (Features)

* **Global Limit Enforcement:** CRD(`GlobalLimit`)를 통해 동시 실행 개수를 동적으로 설정 가능.
* **FIFO Queue:** 먼저 생성된 파이프라인이 먼저 실행되는 선입선출 방식.
* **Smart Labeling:** 새로 생성된 파이프라인에만 `queue.tekton.dev/managed` 라벨을 부착하여 관리 (기존 템플릿 파이프라인 영향 없음).
* **Race Condition Handling:** 리소스 제한을 초과하여 생성된 파이프라인이 이미 `Running` 상태가 된 경우, **강제 종료(Delete) 후 Pending 상태로 재생성**하여 순서를 보장.
* **Zero Dependency:** 무거운 프레임워크(Kopf 등) 없이 순수 `kubernetes` Python 클라이언트만 사용하여 가볍고 빠름.

---

## 설치 및 배포 (Installation)

### 1. 사전 요구사항 (Prerequisites)

* Kubernetes Cluster (v1.20+)
* Tekton Pipelines installed

### 2. CRD (Custom Resource Definition) 생성

제한 개수를 설정하기 위한 CRD를 정의합니다.

```yaml
# crd.yaml
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

```

### 3. 제한 개수 설정 (Configuration)

`maxPipelines` 값을 원하는 숫자로 설정합니다. (이름은 반드시 `my-limit`이어야 합니다.)

```yaml
# limit-setting.yaml
apiVersion: tekton.devops/v1
kind: GlobalLimit
metadata:
  name: tekton-queue-limit
spec:
  maxPipelines: 2  # 동시에 2개까지만 실행 허용

```

### 4. 컨트롤러 배포 (Deploy)

RBAC 권한(ClusterRole)과 Deployment를 생성합니다.

```yaml
# deploy.yaml
apiVersion: v1
kind: ServiceAccount
metadata:
  name: queue-controller
  namespace: tekton-pipelines  # 컨트롤러가 설치될 네임스페이스
---
apiVersion: rbac.authorization.k8s.io/v1
kind: ClusterRole
metadata:
  name: queue-controller-cluster-role
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
  name: queue-controller-cluster-binding
roleRef:
  apiGroup: rbac.authorization.k8s.io
  kind: ClusterRole
  name: queue-controller-cluster-role
subjects:
  - kind: ServiceAccount
    name: queue-controller
    namespace: tekton-pipelines
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
      containers:
        - name: manager
          image: docker.io/tekton/tekton-queue-controller:v0.1.0 # 빌드한 이미지 주소
          imagePullPolicy: Always

```

```bash
kubectl apply -f crd.yaml
kubectl apply -f limit-setting.yaml
kubectl apply -f deploy.yaml

```

---

## 동작 방식 (How it works)

1. **감시 (Watcher):**
* `*-cicd` 패턴의 네임스페이스에서 새로운 `PipelineRun` 생성을 실시간 감지합니다.
* 새 파이프라인이 감지되면 `queue.tekton.dev/managed="yes"` 라벨을 부착합니다.
* 기존에 존재하던 `Pending` 상태의 템플릿 파이프라인은 무시합니다.


2. **제한 확인 (Limit Check):**
* 현재 실행 중인(`Running`) 파이프라인 개수를 확인합니다.
* 설정된 `maxPipelines`를 초과하면 해당 파이프라인을 `PipelineRunPending` 상태로 변경합니다.


3. **강제 집행 (Strict Enforcement):**
* 만약 Tekton이 컨트롤러보다 빠르게 파이프라인을 시작(`Started`)시켜버려서 `Pending` 전환이 실패할 경우,
* 컨트롤러는 해당 파이프라인을 **즉시 삭제(Delete)** 하고, 동일한 스펙으로 **재생성(Recreate as Pending)** 하여 대기열 맨 뒤로 보냅니다.


4. **큐 관리 (Manager):**
* 주기적으로 빈자리가 났는지 확인합니다.
* 자리가 나면 대기 중인 파이프라인 중 가장 오래된 것(FIFO)을 실행(`None`)시킵니다.



---

## 빌드 (Build)

직접 이미지를 빌드하려면 다음 명령어를 사용하세요.

```dockerfile
# Dockerfile
FROM python:3.9-slim
RUN pip install kubernetes
COPY controller.py /controller.py
CMD ["python", "-u", "/controller.py"]

```

```bash
docker build -t your-registry/tekton-queue-controller:v0.1.0 .
docker push your-registry/tekton-queue-controller:v0.1.0

```

---

## 📝 라이선스 (License)

MIT License
