"""
설정 모듈.

모든 상수, 환경변수 로드, CRD 기반 설정 로드,
Kubernetes 클라이언트 초기화를 담당합니다.
(docker/app.py L1~L74, L128~L161 발췌)
"""
import os
import fnmatch
import threading
import logging

from kubernetes import client, config
from kubernetes.client.rest import ApiException

# ─── 기본 상수 ────────────────────────────────────────────────
DEFAULT_NAMESPACE_PATTERNS = ["*-cicd"]
DEFAULT_LIMIT = 10
DEFAULT_AGING_INTERVAL_SEC = 180
DEFAULT_AGING_MIN_TIER = 1
DEFAULT_TIER = 3
DEFAULT_TIER_RULES = [
    {"tier": 0, "matchType": "label", "labelKey": "queue.tekton.dev/urgent",
     "pattern": "true", "description": "긴급 배포 (수동 실행)"},
    {"tier": 1, "matchType": "env", "pattern": "prod", "description": "운영 배포"},
    {"tier": 2, "matchType": "env", "pattern": "stg",  "description": "검증 배포"},
    {"tier": 3, "matchType": "env", "pattern": "*",    "description": "개발 (기본값)"},
]

MANAGED_LABEL_KEY = "queue.tekton.dev/managed"
MANAGED_LABEL_VAL = "yes"
TIER_LABEL_KEY    = "queue.tekton.dev/tier"
ENV_LABEL_KEY     = "env"

CANCEL_STATUSES = frozenset({
    'Cancelled',
    'CancelledRunFinally',
    'StoppedRunFinally',
})

# Dashboard SA만 큐를 통한 실행을 허용합니다.
DASHBOARD_SA_PATTERN = os.environ.get(
    "DASHBOARD_SA_PATTERN",
    "system:serviceaccount:tekton-pipelines:tekton-dashboard"
)

# ─── Leader Election 상수 ─────────────────────────────────────
LEASE_NAME             = os.environ.get("LEASE_NAME", "tekton-queue-controller-leader")
LEASE_NAMESPACE        = os.environ.get("POD_NAMESPACE", "tekton-pipelines")
POD_NAME               = os.environ.get("POD_NAME", f"controller-{os.getpid()}")
LEASE_DURATION_SEC     = 15
LEASE_RETRY_PERIOD_SEC = 2

# ─── CRD 기반 런타임 설정 (load_crd_config()로 갱신) ──────────
crd_config: dict = {
    "max_pipelines":       DEFAULT_LIMIT,
    "aging_interval_sec":  DEFAULT_AGING_INTERVAL_SEC,
    "aging_min_tier":      DEFAULT_AGING_MIN_TIER,
    "tier_rules":          DEFAULT_TIER_RULES,
    "namespace_patterns":  list(DEFAULT_NAMESPACE_PATTERNS),
    "dashboard_sa_pattern": DASHBOARD_SA_PATTERN,
}
crd_config_lock = threading.Lock()

# ─── Kubernetes 클라이언트 ────────────────────────────────────
try:
    config.load_incluster_config()
except config.ConfigException:
    config.load_kube_config()

api      = client.CustomObjectsApi()
core_api = client.CoreV1Api()

logging.getLogger('werkzeug').setLevel(logging.ERROR)


# ─── 유틸리티 함수 ───────────────────────────────────────────
def load_crd_config() -> int:
    """GlobalLimit CRD를 읽어 crd_config를 갱신하고 max_pipelines를 반환합니다."""
    from src import metrics as m  # 순환 import 방지를 위해 지연 import
    try:
        obj  = api.get_cluster_custom_object('tekton.devops', 'v1', 'globallimits', 'tekton-queue-limit')
        spec = obj.get('spec', {})
        ns_patterns = spec.get('namespacePatterns')
        resolved_patterns = (
            ns_patterns if (ns_patterns and isinstance(ns_patterns, list) and len(ns_patterns) > 0)
            else list(DEFAULT_NAMESPACE_PATTERNS)
        )
        new_cfg = {
            "max_pipelines":       int(spec.get('maxPipelines', DEFAULT_LIMIT)),
            "aging_interval_sec":  int(spec.get('agingIntervalSec', DEFAULT_AGING_INTERVAL_SEC)),
            "aging_min_tier":      int(spec.get('agingMinTier', DEFAULT_AGING_MIN_TIER)),
            "tier_rules":          spec.get('tierRules') or DEFAULT_TIER_RULES,
            "namespace_patterns":  resolved_patterns,
            "dashboard_sa_pattern": spec.get('dashboardSAPattern') or DASHBOARD_SA_PATTERN,
        }
        with crd_config_lock:
            crd_config.update(new_cfg)
        return new_cfg["max_pipelines"]
    except ApiException as e:
        m.METRIC_API_ERRORS.labels(operation='get_crd').inc()
        _log(f"[경고] GlobalLimit CRD 조회 실패 (API 에러 {e.status}): {e.reason}. 기본값 사용.")
        return DEFAULT_LIMIT
    except Exception as e:
        _log(f"[경고] GlobalLimit CRD 조회 실패: {e}. 기본값 사용.")
        return DEFAULT_LIMIT


def get_cached_config() -> dict:
    """현재 crd_config의 복사본을 반환합니다 (thread-safe)."""
    with crd_config_lock:
        return dict(crd_config)


def is_target_namespace(namespace: str) -> bool:
    """namespace가 큐 관리 대상 패턴에 해당하는지 확인합니다."""
    cfg      = get_cached_config()
    patterns = cfg.get("namespace_patterns", DEFAULT_NAMESPACE_PATTERNS)
    return any(fnmatch.fnmatch(namespace, p) for p in patterns)


def determine_tier(labels: dict, tier_rules: list) -> int:
    """라벨 맵과 Tier 규칙 목록을 받아 해당 Tier 번호를 반환합니다."""
    for rule in tier_rules:
        match_type = rule.get('matchType', 'env')
        pattern    = rule.get('pattern', '')
        if match_type == 'label':
            label_val = labels.get(rule.get('labelKey', ''), '')
            if label_val and fnmatch.fnmatch(label_val, pattern):
                return int(rule.get('tier', DEFAULT_TIER))
        elif match_type == 'env':
            env_val = labels.get(ENV_LABEL_KEY, '')
            if fnmatch.fnmatch(env_val, pattern):
                return int(rule.get('tier', DEFAULT_TIER))
    return DEFAULT_TIER


# ─── 로깅 ─────────────────────────────────────────────────────
import datetime

def _log(msg: str) -> None:
    """타임스탬프가 포함된 구조화 로그를 출력합니다."""
    now  = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{now}] {msg}"
    try:
        print(line, flush=True)
    except UnicodeEncodeError:
        print(line.encode('utf-8', errors='replace').decode('utf-8'), flush=True)


# 외부 모듈에서 공통으로 사용할 log 함수
log = _log
