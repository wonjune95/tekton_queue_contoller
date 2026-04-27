"""watcher.py 단위 테스트 — Watch 동기화 로직."""
import json
from unittest.mock import patch, MagicMock
from kubernetes.client.rest import ApiException

import src.state as state
import src.cache as cache


class TestWatcherSync:
    @patch('src.workers.watcher.watch')
    @patch('src.workers.watcher.time.sleep', side_effect=InterruptedError)
    @patch('src.workers.watcher.api')
    @patch('src.workers.watcher._reset_global_admitted')
    def test_initial_sync_populates_cache(self, mock_reset, mock_api, mock_sleep, mock_watch):
        """초기 동기화 시 전체 PR을 캐시에 로드한다."""
        from src.workers.watcher import watcher_loop
        raw_resp = MagicMock()
        raw_resp.data = json.dumps({
            'metadata': {'resourceVersion': '100'},
            'items': [
                {'metadata': {'namespace': 'ns1', 'name': 'pr1',
                              'resourceVersion': '10'}},
                {'metadata': {'namespace': 'ns2', 'name': 'pr2',
                              'resourceVersion': '11'}},
            ]
        }).encode()
        mock_api.list_cluster_custom_object.return_value = raw_resp
        # Watch stream에서 바로 에러를 던져서 루프 탈출
        w_instance = MagicMock()
        mock_watch.Watch.return_value = w_instance
        w_instance.stream.side_effect = Exception("test exit")
        try:
            watcher_loop()
        except InterruptedError:
            pass
        assert 'ns1/pr1' in cache.local_cache
        assert 'ns2/pr2' in cache.local_cache
        assert state.initial_sync_done is True

    @patch('src.workers.watcher.watch')
    @patch('src.workers.watcher.time.sleep', side_effect=InterruptedError)
    @patch('src.workers.watcher.api')
    @patch('src.workers.watcher._reset_global_admitted')
    def test_initial_sync_clears_old_cache(self, mock_reset, mock_api, mock_sleep, mock_watch):
        """재동기화 시 기존 캐시를 클리어한다."""
        from src.workers.watcher import watcher_loop
        cache.local_cache["stale/entry"] = {"old": True}
        raw_resp = MagicMock()
        raw_resp.data = json.dumps({
            'metadata': {'resourceVersion': '200'},
            'items': []
        }).encode()
        mock_api.list_cluster_custom_object.return_value = raw_resp
        w_instance = MagicMock()
        mock_watch.Watch.return_value = w_instance
        w_instance.stream.side_effect = Exception("test exit")
        try:
            watcher_loop()
        except InterruptedError:
            pass
        assert "stale/entry" not in cache.local_cache

    @patch('src.workers.watcher.watch')
    @patch('src.workers.watcher.time.sleep', side_effect=[None, InterruptedError])
    @patch('src.workers.watcher.api')
    @patch('src.workers.watcher._reset_global_admitted')
    def test_410_gone_triggers_resync(self, mock_reset, mock_api, mock_sleep, mock_watch):
        """410 Gone 에러 시 전체 재동기화를 트리거한다."""
        from src.workers.watcher import watcher_loop
        call_count = [0]
        def fake_list(*args, **kwargs):
            call_count[0] += 1
            if call_count[0] >= 2:
                raise InterruptedError("stop")
            resp = MagicMock()
            resp.data = json.dumps({
                'metadata': {'resourceVersion': '100'},
                'items': []
            }).encode()
            return resp
        mock_api.list_cluster_custom_object.side_effect = fake_list
        w_instance = MagicMock()
        mock_watch.Watch.return_value = w_instance
        w_instance.stream.side_effect = ApiException(status=410)
        try:
            watcher_loop()
        except InterruptedError:
            pass
        assert call_count[0] >= 2

    @patch('src.workers.watcher.watch')
    @patch('src.workers.watcher.time.sleep', side_effect=InterruptedError)
    @patch('src.workers.watcher.api')
    def test_api_error_no_crash(self, mock_api, mock_sleep, mock_watch):
        """API 에러에도 크래시하지 않는다."""
        from src.workers.watcher import watcher_loop
        mock_api.list_cluster_custom_object.side_effect = ApiException(status=500)
        try:
            watcher_loop()
        except InterruptedError:
            pass
