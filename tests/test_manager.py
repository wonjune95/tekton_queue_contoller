"""manager.py 단위 테스트 — 스케줄링 루프 로직."""
import time
from unittest.mock import patch, MagicMock, call
from kubernetes.client.rest import ApiException

import src.state as state
import src.cache as cache
import src.config as config
from src.workers.manager import manager_loop
from tests.conftest import make_pr


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# 1) 리더 체크
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
class TestManagerLeaderCheck:
    @patch('src.workers.manager.time.sleep', side_effect=InterruptedError)
    @patch('src.workers.manager.load_crd_config')
    def test_skips_when_not_leader(self, mock_crd, mock_sleep):
        """리더가 아니면 스케줄링하지 않는다."""
        state.is_leader = False
        try:
            manager_loop()
        except InterruptedError:
            pass
        mock_crd.assert_not_called()

    @patch('src.workers.manager.time.sleep', side_effect=[None, InterruptedError])
    @patch('src.workers.manager.load_crd_config', return_value=10)
    @patch('src.cache.core_api')
    def test_runs_when_leader(self, mock_api, mock_crd, mock_sleep):
        from src.workers.manager import manager_loop as ml
        state.is_leader = True
        cm = MagicMock()
        cm.data = {'admitted': '0'}
        mock_api.read_namespaced_config_map.return_value = cm
        try:
            ml()
        except InterruptedError:
            pass
        mock_crd.assert_called()


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# 2) 스케줄링 로직
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
class TestManagerScheduling:
    @patch('src.workers.manager.time.sleep', side_effect=[None, InterruptedError])
    @patch('src.workers.manager.load_crd_config', return_value=10)
    @patch('src.workers.manager.api')
    @patch('src.cache.core_api')
    def test_schedules_pending(self, mock_cache_api, mock_api, mock_crd, mock_sleep):
        """가용 슬롯이 있으면 Pending PR을 스케줄링한다."""
        state.is_leader = True
        cm = MagicMock()
        cm.data = {'admitted': '0'}
        mock_cache_api.read_namespaced_config_map.return_value = cm
        cache.local_cache["test-cicd/p1"] = make_pr(
            name="p1", spec_status="PipelineRunPending",
            tier=2, managed=True)
        try:
            manager_loop()
        except InterruptedError:
            pass
        mock_api.patch_namespaced_custom_object.assert_called_once_with(
            'tekton.dev', 'v1', 'test-cicd', 'pipelineruns', 'p1',
            {'spec': {'status': None}}
        )

    @patch('src.workers.manager.time.sleep', side_effect=[None, InterruptedError])
    @patch('src.workers.manager.load_crd_config', return_value=1)
    @patch('src.workers.manager.api')
    @patch('src.cache.core_api')
    def test_respects_limit(self, mock_cache_api, mock_api, mock_crd, mock_sleep):
        """쿼터가 꽉 차면 스케줄링하지 않는다."""
        state.is_leader = True
        cm = MagicMock()
        cm.data = {'admitted': '0'}
        mock_cache_api.read_namespaced_config_map.return_value = cm
        cache.local_cache["test-cicd/r1"] = make_pr(name="r1")
        cache.local_cache["test-cicd/p1"] = make_pr(
            name="p1", spec_status="PipelineRunPending",
            tier=2, managed=True)
        try:
            manager_loop()
        except InterruptedError:
            pass
        mock_api.patch_namespaced_custom_object.assert_not_called()

    @patch('src.workers.manager.time.sleep', side_effect=[None, InterruptedError])
    @patch('src.workers.manager.load_crd_config', return_value=10)
    @patch('src.workers.manager.api')
    @patch('src.cache.core_api')
    def test_patch_failure_continues(self, mock_cache_api, mock_api, mock_crd, mock_sleep):
        """패치 실패해도 Manager 루프가 크래시하지 않는다."""
        state.is_leader = True
        cm = MagicMock()
        cm.data = {'admitted': '0'}
        mock_cache_api.read_namespaced_config_map.return_value = cm
        mock_api.patch_namespaced_custom_object.side_effect = ApiException(status=500)
        cache.local_cache["test-cicd/p1"] = make_pr(
            name="p1", spec_status="PipelineRunPending",
            tier=2, managed=True)
        try:
            manager_loop()
        except InterruptedError:
            pass
        # 크래시 없이 완료

    @patch('src.workers.manager.time.sleep', side_effect=[None, InterruptedError])
    @patch('src.workers.manager.load_crd_config', return_value=2)
    @patch('src.workers.manager.api')
    @patch('src.cache.core_api')
    def test_schedules_only_available_slots(self, mock_cache_api, mock_api, mock_crd, mock_sleep):
        """슬롯 수만큼만 스케줄링한다 (초과 방지)."""
        state.is_leader = True
        cm = MagicMock()
        cm.data = {'admitted': '0'}
        mock_cache_api.read_namespaced_config_map.return_value = cm
        cache.local_cache["test-cicd/r1"] = make_pr(name="r1")
        for i in range(5):
            cache.local_cache[f"test-cicd/p{i}"] = make_pr(
                name=f"p{i}", spec_status="PipelineRunPending",
                tier=2, managed=True)
        try:
            manager_loop()
        except InterruptedError:
            pass
        assert mock_api.patch_namespaced_custom_object.call_count == 1
