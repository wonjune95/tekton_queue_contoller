"""
테스트 공통 설정 (conftest.py).

src/config.py가 모듈 import 시점에 K8s 클라이언트를 초기화하므로,
테스트 모듈이 import되기 전에 kubernetes 설정 로드를 모킹합니다.
"""
from unittest.mock import MagicMock

# ── K8s 설정 로드 모킹 (모듈 레벨, 가장 먼저 실행) ───────────
import kubernetes.config as _k8s_cfg

_k8s_cfg.load_incluster_config = MagicMock(
    side_effect=_k8s_cfg.ConfigException("test env")
)
_k8s_cfg.load_kube_config = MagicMock()

# 이제 src 모듈을 안전하게 import할 수 있습니다
import pytest
import src.state as state
import src.cache as cache
import src.config as config


@pytest.fixture(autouse=True)
def _reset_global_state():
    """각 테스트 전후에 전역 상태를 초기화합니다."""
    state.is_leader = False
    state.initial_sync_done = False
    cache.local_cache.clear()
    cache.webhook_admitted_count = 0
    config.crd_config.update({
        "max_pipelines":       config.DEFAULT_LIMIT,
        "aging_interval_sec":  config.DEFAULT_AGING_INTERVAL_SEC,
        "aging_min_tier":      config.DEFAULT_AGING_MIN_TIER,
        "tier_rules":          config.DEFAULT_TIER_RULES,
        "namespace_patterns":  list(config.DEFAULT_NAMESPACE_PATTERNS),
        "dashboard_sa_pattern": config.DASHBOARD_SA_PATTERN,
    })
    yield
    state.is_leader = False
    state.initial_sync_done = False
    cache.local_cache.clear()
    cache.webhook_admitted_count = 0


@pytest.fixture
def flask_client():
    """Flask 테스트 클라이언트를 제공합니다."""
    from src.webhook import app
    app.config['TESTING'] = True
    with app.test_client() as client:
        yield client


def make_pr(namespace="test-cicd", name="pr-001", labels=None,
            spec_status=None, tier=None, managed=False,
            creation_ts="2025-01-01T00:00:00Z",
            completion_time=None, conditions=None):
    """테스트용 PipelineRun 객체를 생성하는 헬퍼."""
    if labels is None:
        labels = {}
    if tier is not None:
        labels[config.TIER_LABEL_KEY] = str(tier)
    if managed:
        labels[config.MANAGED_LABEL_KEY] = config.MANAGED_LABEL_VAL
    pr = {
        'metadata': {
            'namespace': namespace,
            'name': name,
            'labels': labels,
            'creationTimestamp': creation_ts,
            'resourceVersion': '100',
        },
        'spec': {},
        'status': {},
    }
    if spec_status:
        pr['spec']['status'] = spec_status
    if completion_time:
        pr['status']['completionTime'] = completion_time
    if conditions:
        pr['status']['conditions'] = conditions
    return pr


def make_admission_request(namespace="test-cicd", name="pr-test",
                           labels=None, username="system:serviceaccount:tekton-pipelines:tekton-dashboard",
                           generate_name=None):
    """테스트용 AdmissionReview 요청을 생성하는 헬퍼."""
    metadata = {"namespace": namespace, "labels": labels or {}}
    if name:
        metadata["name"] = name
    if generate_name:
        metadata["generateName"] = generate_name
    return {
        "apiVersion": "admission.k8s.io/v1",
        "kind": "AdmissionReview",
        "request": {
            "uid": "test-uid-12345",
            "object": {
                "metadata": metadata,
                "spec": {},
            },
            "userInfo": {"username": username},
        }
    }
