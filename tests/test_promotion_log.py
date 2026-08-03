"""
유효 Tier 승격 이벤트 로그 테스트 (W_max 검증 + 역전 그림 소스).
detect_and_log_promotions 는 now 주입이 가능해 시간 진행을 결정론적으로 재현한다.
"""
import datetime
from prometheus_client import REGISTRY

import src.workers.manager as manager

UTC = datetime.timezone.utc
BASE = datetime.datetime(2025, 1, 1, 0, 0, 0, tzinfo=UTC)
CFG = {"aging_interval_sec": 300, "aging_min_tier": 1}


def _pending_pr(name="pr-1", tier=3, ns="test-cicd",
                created="2025-01-01T00:00:00Z"):
    return {
        "metadata": {
            "namespace": ns, "name": name,
            "labels": {"queue.tekton.dev/tier": str(tier), "env": "dev"},
            "creationTimestamp": created,
        },
        "spec": {"status": "PipelineRunPending"},
        "status": {},
    }


def _promo_count(frm, to):
    return REGISTRY.get_sample_value(
        "tekton_queue_promotion_total",
        {"from_tier": str(frm), "to_tier": str(to)})


def test_promotion_progression():
    manager._last_eff_tier.clear()
    pr = [_pending_pr()]

    base_32 = _promo_count(3, 2) or 0.0
    base_21 = _promo_count(2, 1) or 0.0

    # t+10s: 아직 승격 없음(eff=3), 초기 관측만
    manager.detect_and_log_promotions(pr, CFG, now=BASE + datetime.timedelta(seconds=10))
    assert (_promo_count(3, 2) or 0.0) == base_32
    assert manager._last_eff_tier["test-cicd/pr-1"] == 3

    # t+310s: eff 3->2 승격
    manager.detect_and_log_promotions(pr, CFG, now=BASE + datetime.timedelta(seconds=310))
    assert (_promo_count(3, 2) or 0.0) == base_32 + 1
    assert manager._last_eff_tier["test-cicd/pr-1"] == 2

    # t+610s: eff 2->1 승격 (W_max=600s 상한에서 최고 우선순위 도달)
    manager.detect_and_log_promotions(pr, CFG, now=BASE + datetime.timedelta(seconds=610))
    assert (_promo_count(2, 1) or 0.0) == base_21 + 1
    assert manager._last_eff_tier["test-cicd/pr-1"] == 1


def test_prunes_departed_pr():
    manager._last_eff_tier.clear()
    pr = [_pending_pr(name="pr-x")]
    manager.detect_and_log_promotions(pr, CFG, now=BASE + datetime.timedelta(seconds=10))
    assert "test-cicd/pr-x" in manager._last_eff_tier
    # 대기열에서 사라짐(스케줄/완료) → 정리되어야 함
    manager.detect_and_log_promotions([], CFG, now=BASE + datetime.timedelta(seconds=20))
    assert "test-cicd/pr-x" not in manager._last_eff_tier


def test_no_promotion_logged_on_first_sight():
    manager._last_eff_tier.clear()
    # 이미 오래 대기한 PR을 처음 관측해도(초기값 설정) 승격 로그는 남기지 않는다
    base_31 = _promo_count(3, 1) or 0.0
    pr = [_pending_pr(name="pr-old")]
    manager.detect_and_log_promotions(pr, CFG, now=BASE + datetime.timedelta(seconds=10000))
    assert manager._last_eff_tier["test-cicd/pr-old"] == 1  # 곧바로 eff=1
    assert (_promo_count(3, 1) or 0.0) == base_31  # 전이 로그 없음
