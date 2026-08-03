# Tekton Queue Controller — Makefile
# 사용법: make <target>

IMAGE_NAME   ?= tekton-queue-controller
IMAGE_TAG    ?= local
CLUSTER_NAME ?= tekton-test
# GKE 푸시용: 예) asia-northeast3-docker.pkg.dev/<PROJECT>/tekton-queue  (make push 시 지정)
REGISTRY     ?=
IMAGE_REF     = $(if $(REGISTRY),$(REGISTRY)/$(IMAGE_NAME):$(IMAGE_TAG),$(IMAGE_NAME):$(IMAGE_TAG))

.PHONY: build load push deploy test simulate kind-setup kind-test lint clean help

## Docker 이미지 빌드 (로컬)
build:
	docker build -t $(IMAGE_NAME):$(IMAGE_TAG) -f docker/Dockerfile .

## Kind 클러스터에 이미지 로드
load: build
	kind load docker-image $(IMAGE_NAME):$(IMAGE_TAG) --name $(CLUSTER_NAME)

## 레지스트리로 빌드·푸시 (GKE용). REGISTRY·IMAGE_TAG 지정 필수.
##   예: make push REGISTRY=asia-northeast3-docker.pkg.dev/PROJECT/tekton-queue IMAGE_TAG=v0.2.0
push:
	@test -n "$(REGISTRY)" || { echo "REGISTRY 를 지정하세요 (예: make push REGISTRY=<REGION>-docker.pkg.dev/<PROJECT>/tekton-queue IMAGE_TAG=v0.2.0)"; exit 1; }
	docker build -t $(IMAGE_REF) -f docker/Dockerfile .
	docker push $(IMAGE_REF)
	@echo ">>> 푸시 완료: $(IMAGE_REF)"
	@echo ">>> deploy.yaml 이미지 교체 후 배포하세요 (CLAUDE.md '자주 쓰는 명령' 참조)."

## Kubernetes 리소스 배포 (install/ 디렉터리 기준)
deploy:
	kubectl apply -f install/crd.yaml
	kubectl apply -f install/limit-setting.yaml
	kubectl apply -f install/secret.yaml
	kubectl apply -f install/deploy.yaml

## 전체 배포 (빌드 + 로드 + 배포)
all: load deploy

## pytest 단위 테스트
test:
	python -m pytest tests/ -v

## S1~S4 스케줄링 시뮬레이션 (K8s 불필요)
simulate:
	python -m tests.simulate_scenarios

## Kind 로컬 클러스터 구성 (Tekton + 컨트롤러 배포)
kind-setup:
	bash scripts/kind_setup.sh

## Kind 클러스터에서 S3 Burst 시나리오 재현 (24개 PipelineRun 생성)
kind-test:
	@echo ">>> S3 Burst: 24개 PipelineRun 생성 (test-cicd 네임스페이스)"
	@kubectl create namespace test-cicd 2>/dev/null || true
	@for i in $$(seq 1 24); do \
		kubectl create -n test-cicd -f - <<EOF \
		  2>/dev/null & \
apiVersion: tekton.dev/v1 \
kind: PipelineRun \
metadata: \
  generateName: burst-pr- \
  labels: \
    env: dev \
spec: \
  pipelineSpec: \
    tasks: \
    - name: sleep \
      taskSpec: \
        steps: \
        - name: sleep \
          image: alpine \
          command: [sh, -c, "sleep 30"] \
EOF \
	done; wait
	@echo ">>> 생성 완료. 상태 확인:"
	@kubectl get pipelineruns -n test-cicd | head -30

## 코드 lint (flake8)
lint:
	flake8 src/ tests/ --max-line-length=120

## 빌드 캐시 정리
clean:
	docker rmi $(IMAGE_NAME):$(IMAGE_TAG) 2>/dev/null || true

## 도움말
help:
	@echo "사용 가능한 타겟:"
	@echo "  build   - Docker 이미지 빌드"
	@echo "  load    - Kind 클러스터에 이미지 로드"
	@echo "  deploy  - K8s 리소스 배포"
	@echo "  all     - 빌드 + 로드 + 배포"
	@echo "  test    - pytest 단위 테스트"
	@echo "  lint    - flake8 코드 검사"
	@echo "  clean   - 이미지 삭제"
