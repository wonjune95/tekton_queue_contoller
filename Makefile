# Tekton Queue Controller — Makefile
# 사용법: make <target>

IMAGE_NAME   ?= tekton-queue-controller
IMAGE_TAG    ?= local
CLUSTER_NAME ?= tekton-test

.PHONY: build load deploy test lint clean help

## Docker 이미지 빌드
build:
	docker build -t $(IMAGE_NAME):$(IMAGE_TAG) -f docker/Dockerfile .

## Kind 클러스터에 이미지 로드
load: build
	kind load docker-image $(IMAGE_NAME):$(IMAGE_TAG) --name $(CLUSTER_NAME)

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
