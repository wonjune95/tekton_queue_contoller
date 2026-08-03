#!/usr/bin/env bash
# Kind 로컬 클러스터 구성 스크립트
# 실행: bash scripts/kind_setup.sh
# 사전 요구: kind, kubectl, docker, openssl

set -euo pipefail

CLUSTER_NAME="tekton-test"
IMAGE_NAME="tekton-queue-controller:local"
NAMESPACE="tekton-pipelines"
SVC_NAME="tekton-queue-controller"

# ── 1. Kind 클러스터 생성 ─────────────────────────────────────────
echo ">>> [1/6] Kind 클러스터 생성 ($CLUSTER_NAME)"
if kind get clusters | grep -q "^${CLUSTER_NAME}$"; then
  echo "    이미 존재하는 클러스터. 건너뜁니다."
else
  kind create cluster --name "$CLUSTER_NAME"
fi

# ── 2. Tekton Pipelines 설치 ──────────────────────────────────────
echo ">>> [2/6] Tekton Pipelines 설치"
# ※ GCS 버킷 경로는 404, `latest` 는 낡은 v1.6.0 을 가리킨다. GitHub 릴리스 자산 + 버전 고정을 쓸 것.
kubectl apply --filename \
  "https://github.com/tektoncd/pipeline/releases/download/${TEKTON_VERSION:-v1.9.2}/release.yaml"

echo "    Tekton Controller 준비 대기 중..."
kubectl wait --for=condition=available --timeout=120s \
  deployment/tekton-pipelines-controller -n tekton-pipelines
echo "    Tekton 설치 완료."

# ── 3. TLS 인증서 생성 (Webhook용 Self-signed) ────────────────────
echo ">>> [3/6] Webhook TLS 인증서 생성"
TLS_DIR="$(mktemp -d)"
SVC_FQDN="${SVC_NAME}.${NAMESPACE}.svc"

cat > "${TLS_DIR}/csr.conf" <<EOF
[req]
req_extensions = v3_req
distinguished_name = req_distinguished_name
[req_distinguished_name]
[v3_req]
basicConstraints = CA:FALSE
keyUsage = nonRepudiation, digitalSignature, keyEncipherment
subjectAltName = @alt_names
[alt_names]
DNS.1 = ${SVC_FQDN}
DNS.2 = ${SVC_FQDN}.cluster.local
EOF

# Git Bash(MSYS/Cygwin)는 "/CN=..." 를 Windows 경로로 변환한다 → 슬래시 하나 더 붙여 이스케이프.
case "$(uname -s)" in
  MINGW*|MSYS*|CYGWIN*) SUBJ="//CN=${SVC_FQDN}" ;;
  *)                    SUBJ="/CN=${SVC_FQDN}"  ;;
esac

openssl genrsa -out "${TLS_DIR}/ca.key" 2048 2>/dev/null
openssl req -new -x509 -days 365 -key "${TLS_DIR}/ca.key" \
  -subj "$SUBJ" -out "${TLS_DIR}/ca.crt"

openssl genrsa -out "${TLS_DIR}/tls.key" 2048 2>/dev/null
openssl req -new -key "${TLS_DIR}/tls.key" \
  -subj "$SUBJ" -out "${TLS_DIR}/tls.csr"
openssl x509 -req -days 365 \
  -in  "${TLS_DIR}/tls.csr" \
  -CA  "${TLS_DIR}/ca.crt" -CAkey "${TLS_DIR}/ca.key" -CAcreateserial \
  -extensions v3_req -extfile "${TLS_DIR}/csr.conf" \
  -out "${TLS_DIR}/tls.crt" 2>/dev/null

CA_BUNDLE=$(base64 < "${TLS_DIR}/ca.crt" | tr -d '\n')
echo "    인증서 생성 완료."

# ── 4. Secret + CRD + GlobalLimit 배포 ───────────────────────────
echo ">>> [4/6] Kubernetes 리소스 배포"

kubectl apply -f install/crd.yaml

# Secret을 임시 YAML로 생성 (tls.crt / tls.key 교체)
kubectl create secret tls tekton-queue-cacerts \
  --cert="${TLS_DIR}/tls.crt" \
  --key="${TLS_DIR}/tls.key"  \
  -n "$NAMESPACE" \
  --dry-run=client -o yaml | kubectl apply -f -

kubectl apply -f install/limit-setting.yaml

# ── 5. 컨트롤러 이미지 빌드 및 Kind 로드 ─────────────────────────
echo ">>> [5/6] 컨트롤러 이미지 빌드 및 Kind 로드"
docker build -t "$IMAGE_NAME" -f docker/Dockerfile . --quiet
kind load docker-image "$IMAGE_NAME" --name "$CLUSTER_NAME"
echo "    이미지 로드 완료."

# ── 6. Deployment / Service / Webhook 배포 ────────────────────────
echo ">>> [6/6] 컨트롤러 배포 (caBundle 자동 주입)"

# deploy-local.yaml을 deploy.yaml 기반으로 생성:
#   - image를 local 이미지로 교체
#   - imagePullPolicy를 IfNotPresent으로 교체
#   - caBundle 플레이스홀더를 실제 값으로 교체
sed \
  -e "s|docker.io/tekton-queue-controller:v0.1.0|${IMAGE_NAME}|g" \
  -e "s|imagePullPolicy: Always|imagePullPolicy: IfNotPresent|g" \
  -e "s|<BASE64_ENCODED_CA_CERT_HERE>|${CA_BUNDLE}|g" \
  install/deploy.yaml | kubectl apply -f -

echo "    컨트롤러 배포 완료."
echo ""
echo "    Pod 준비 대기 중..."
kubectl wait --for=condition=available --timeout=60s \
  deployment/tekton-queue-controller -n "$NAMESPACE" || true

# ── 완료 안내 ─────────────────────────────────────────────────────
echo ""
echo "══════════════════════════════════════════════════════════"
echo "  Kind 클러스터 구성 완료"
echo "  다음 명령으로 상태를 확인하세요:"
echo ""
echo "  kubectl get pods -n tekton-pipelines"
echo "  kubectl logs -n tekton-pipelines -l app=tekton-queue --tail=30"
echo ""
echo "  테스트 PipelineRun 생성 (S3 Burst 재현):"
echo "  make kind-test"
echo "══════════════════════════════════════════════════════════"

rm -rf "$TLS_DIR"
