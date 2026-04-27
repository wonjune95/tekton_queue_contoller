"""cache.py 단위 테스트 — 캐시 CRUD, 큐 상태 조회, 에이징, admitted 카운터."""
import datetime
import threading
from unittest.mock import patch, MagicMock
from kubernetes.client.rest import ApiException

import src.cache as cache
import src.config as config
import src.state as state
from tests.conftest import make_pr


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# 1) 타임스탬프 파서
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
class TestParseTimestamp:
    def test_valid(self):
        dt = cache.parse_k8s_timestamp("2025-03-15T10:30:00Z")
        assert dt.year == 2025 and dt.month == 3 and dt.hour == 10

    def test_empty(self):
        dt = cache.parse_k8s_timestamp("")
        assert dt == datetime.datetime.min.replace(tzinfo=datetime.timezone.utc)

    def test_none(self):
        dt = cache.parse_k8s_timestamp(None)
        assert dt == datetime.datetime.min.replace(tzinfo=datetime.timezone.utc)

    def test_invalid_format(self):
        dt = cache.parse_k8s_timestamp("not-a-date")
        assert dt == datetime.datetime.min.replace(tzinfo=datetime.timezone.utc)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# 2) PipelineRun 완료 판정
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
class TestIsPipelinerunFinished:
    def test_has_completion_time(self):
        pr = make_pr(completion_time="2025-01-01T01:00:00Z")
        assert cache.is_pipelinerun_finished(pr) is True

    def test_succeeded_true(self):
        pr = make_pr(conditions=[{"type": "Succeeded", "status": "True"}])
        assert cache.is_pipelinerun_finished(pr) is True

    def test_succeeded_false(self):
        pr = make_pr(conditions=[{"type": "Succeeded", "status": "False"}])
        assert cache.is_pipelinerun_finished(pr) is True

    def test_not_finished(self):
        pr = make_pr()
        assert cache.is_pipelinerun_finished(pr) is False

    def test_unknown_condition(self):
        pr = make_pr(conditions=[{"type": "Succeeded", "status": "Unknown"}])
        assert cache.is_pipelinerun_finished(pr) is False


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# 3) update_cache 이벤트 처리
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
class TestUpdateCache:
    @patch('src.cache.core_api')
    def test_added_event(self, mock_api):
        pr = make_pr(name="pr-add")
        cache.update_cache("ADDED", pr)
        assert "test-cicd/pr-add" in cache.local_cache

    @patch('src.cache.core_api')
    def test_modified_event(self, mock_api):
        pr = make_pr(name="pr-mod")
        cache.update_cache("ADDED", pr)
        pr['status'] = {'completionTime': 'now'}
        cache.update_cache("MODIFIED", pr)
        assert cache.local_cache["test-cicd/pr-mod"]['status']['completionTime'] == 'now'

    @patch('src.cache.core_api')
    def test_deleted_event(self, mock_api):
        pr = make_pr(name="pr-del")
        cache.update_cache("ADDED", pr)
        cache.update_cache("DELETED", pr)
        assert "test-cicd/pr-del" not in cache.local_cache

    @patch('src.cache.core_api')
    def test_delete_nonexistent_no_crash(self, mock_api):
        pr = make_pr(name="ghost")
        cache.update_cache("DELETED", pr)  # 없는 항목 삭제 시 크래시 없음


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# 4) get_queue_status_from_cache 큐 상태 조회
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
class TestGetQueueStatus:
    def test_running_count(self):
        cache.local_cache["test-cicd/r1"] = make_pr(name="r1")  # running (no spec.status)
        cache.local_cache["test-cicd/r2"] = make_pr(name="r2")
        running, pending = cache.get_queue_status_from_cache()
        assert running == 2
        assert pending == []

    def test_pending_managed(self):
        pr = make_pr(name="p1", spec_status="PipelineRunPending",
                     tier=2, managed=True)
        cache.local_cache["test-cicd/p1"] = pr
        running, pending = cache.get_queue_status_from_cache()
        assert running == 0
        assert len(pending) == 1

    def test_pending_unmanaged_excluded(self):
        """managed 라벨이 없는 Pending PR은 대기열에 포함되지 않는다."""
        pr = make_pr(name="p1", spec_status="PipelineRunPending", tier=2, managed=False)
        cache.local_cache["test-cicd/p1"] = pr
        _, pending = cache.get_queue_status_from_cache()
        assert len(pending) == 0

    def test_finished_excluded(self):
        pr = make_pr(name="f1", completion_time="2025-01-01T01:00:00Z")
        cache.local_cache["test-cicd/f1"] = pr
        running, _ = cache.get_queue_status_from_cache()
        assert running == 0

    def test_cancelled_excluded(self):
        pr = make_pr(name="c1", spec_status="Cancelled")
        cache.local_cache["test-cicd/c1"] = pr
        running, _ = cache.get_queue_status_from_cache()
        assert running == 0

    def test_non_target_ns_excluded(self):
        pr = make_pr(namespace="other-ns", name="r1")
        cache.local_cache["other-ns/r1"] = pr
        running, _ = cache.get_queue_status_from_cache()
        assert running == 0

    def test_tier_sort_order(self):
        """낮은 Tier가 먼저 스케줄링된다."""
        # 에이징 효과를 제거하기 위해 방금 생성된 시각 사용
        now_ts = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        for i, tier in enumerate([3, 1, 2]):
            pr = make_pr(name=f"p{i}", spec_status="PipelineRunPending",
                         tier=tier, managed=True, creation_ts=now_ts)
            cache.local_cache[f"test-cicd/p{i}"] = pr
        _, pending = cache.get_queue_status_from_cache()
        result_tiers = [int(p['metadata']['labels'].get('queue.tekton.dev/tier', '3'))
                        for p in pending]
        assert result_tiers == [1, 2, 3]

    def test_fifo_within_same_tier(self):
        """같은 Tier 내에서는 생성 시각 순서(FIFO)."""
        cache.local_cache["test-cicd/old"] = make_pr(
            name="old", spec_status="PipelineRunPending",
            tier=2, managed=True, creation_ts="2025-01-01T00:00:00Z")
        cache.local_cache["test-cicd/new"] = make_pr(
            name="new", spec_status="PipelineRunPending",
            tier=2, managed=True, creation_ts="2025-01-01T01:00:00Z")
        _, pending = cache.get_queue_status_from_cache()
        names = [p['metadata']['name'] for p in pending]
        assert names == ["old", "new"]

    def test_aging_promotes_tier(self):
        """오래 대기한 PR의 effective tier가 낮아져 우선순위가 올라간다."""
        old_ts = "2020-01-01T00:00:00Z"  # 매우 오래 전
        config.crd_config['aging_interval_sec'] = 60
        config.crd_config['aging_min_tier'] = 1
        # Tier 3이지만 aging으로 Tier 1까지 승격
        cache.local_cache["test-cicd/old"] = make_pr(
            name="old", spec_status="PipelineRunPending",
            tier=3, managed=True, creation_ts=old_ts)
        # Tier 2, 최근 생성 → aging 없음
        now_ts = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        cache.local_cache["test-cicd/new"] = make_pr(
            name="new", spec_status="PipelineRunPending",
            tier=2, managed=True, creation_ts=now_ts)
        _, pending = cache.get_queue_status_from_cache()
        # old(effective ~1)가 new(effective 2)보다 먼저
        assert pending[0]['metadata']['name'] == "old"

    def test_aging_min_tier_prevents_tier0(self):
        """에이징으로 agingMinTier(1) 이하로는 내려가지 않는다 → Tier 0 도달 불가."""
        config.crd_config['aging_interval_sec'] = 1
        config.crd_config['aging_min_tier'] = 1
        cache.local_cache["test-cicd/ancient"] = make_pr(
            name="ancient", spec_status="PipelineRunPending",
            tier=3, managed=True, creation_ts="2000-01-01T00:00:00Z")
        cache.local_cache["test-cicd/urgent"] = make_pr(
            name="urgent", spec_status="PipelineRunPending",
            tier=0, managed=True,
            creation_ts=datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"))
        _, pending = cache.get_queue_status_from_cache()
        # Tier 0 (urgent)가 반드시 먼저
        assert pending[0]['metadata']['name'] == "urgent"


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# 5) admitted 카운터 (ConfigMap fallback)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
class TestAdmittedCounter:
    @patch('src.cache.core_api')
    def test_increment_success(self, mock_api):
        cm = MagicMock()
        cm.data = {'admitted': '0'}
        mock_api.read_namespaced_config_map.return_value = cm
        ok, eff = cache._try_increment_global_admitted(5, 10)
        assert ok is True
        assert cm.data['admitted'] == '1'

    @patch('src.cache.core_api')
    def test_increment_at_limit(self, mock_api):
        cm = MagicMock()
        cm.data = {'admitted': '0'}
        mock_api.read_namespaced_config_map.return_value = cm
        ok, _ = cache._try_increment_global_admitted(10, 10)
        assert ok is False

    @patch('src.cache.core_api')
    def test_increment_cm_409_retries(self, mock_api):
        """409 Conflict 시 재시도."""
        cm = MagicMock()
        cm.data = {'admitted': '0'}
        mock_api.read_namespaced_config_map.return_value = cm
        mock_api.replace_namespaced_config_map.side_effect = [
            ApiException(status=409), None
        ]
        ok, _ = cache._try_increment_global_admitted(0, 10)
        assert ok is True

    @patch('src.cache.core_api')
    def test_increment_all_fail_fallback_to_local(self, mock_api):
        """CM 모든 재시도 실패 시 로컬 카운터로 fallback."""
        mock_api.read_namespaced_config_map.side_effect = ApiException(status=500)
        ok, _ = cache._try_increment_global_admitted(0, 10)
        assert ok is True  # local fallback 성공
        assert cache.webhook_admitted_count == 1

    @patch('src.cache.core_api')
    def test_decrement(self, mock_api):
        cm = MagicMock()
        cm.data = {'admitted': '3'}
        mock_api.read_namespaced_config_map.return_value = cm
        cache._decrement_global_admitted()
        assert cm.data['admitted'] == '2'

    @patch('src.cache.core_api')
    def test_decrement_at_zero(self, mock_api):
        cm = MagicMock()
        cm.data = {'admitted': '0'}
        mock_api.read_namespaced_config_map.return_value = cm
        cache._decrement_global_admitted()
        assert cm.data['admitted'] == '0'  # 음수 방지

    @patch('src.cache.core_api')
    def test_reset(self, mock_api):
        cm = MagicMock()
        cm.data = {'admitted': '5'}
        mock_api.read_namespaced_config_map.return_value = cm
        cache.webhook_admitted_count = 10
        cache._reset_global_admitted()
        assert cm.data['admitted'] == '0'
        assert cache.webhook_admitted_count == 0

    @patch('src.cache.core_api')
    def test_get_global_admitted(self, mock_api):
        cm = MagicMock()
        cm.data = {'admitted': '7'}
        mock_api.read_namespaced_config_map.return_value = cm
        assert cache._get_global_admitted() == 7

    @patch('src.cache.core_api')
    def test_get_global_admitted_fallback(self, mock_api):
        mock_api.read_namespaced_config_map.side_effect = Exception("fail")
        cache.webhook_admitted_count = 3
        assert cache._get_global_admitted() == 3
