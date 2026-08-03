"""watcher.py 단위 테스트 — Watch 동기화 로직."""
import json
from unittest.mock import patch, MagicMock
from kubernetes.client.rest import ApiException

import src.state as state
import src.cache as cache
from tests.conftest import make_pr


class TestWatcherSync:
    @patch('src.workers.watcher.watch')
    @patch('src.workers.watcher.time.sleep', side_effect=InterruptedError)
    @patch('src.workers.watcher.api')
    def test_initial_sync_populates_cache(self, mock_api, mock_sleep, mock_watch):
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
    def test_initial_sync_clears_old_cache(self, mock_api, mock_sleep, mock_watch):
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
    def test_410_gone_triggers_resync(self, mock_api, mock_sleep, mock_watch):
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


class TestWatcherStreamEvents:
    @patch('src.workers.watcher.watch')
    @patch('src.workers.watcher.time.sleep', side_effect=InterruptedError)
    @patch('src.workers.watcher.api')
    def test_stream_added_event_updates_cache(self, mock_api, mock_sleep, mock_watch):
        """Watch 스트림 ADDED 이벤트가 캐시에 반영된다."""
        from src.workers.watcher import watcher_loop
        raw_resp = MagicMock()
        raw_resp.data = json.dumps({
            'metadata': {'resourceVersion': '100'},
            'items': []
        }).encode()
        mock_api.list_cluster_custom_object.return_value = raw_resp
        new_pr = {
            'metadata': {'namespace': 'ns1', 'name': 'new-pr', 'resourceVersion': '101',
                         'labels': {}, 'creationTimestamp': '2025-01-01T00:00:00Z'},
            'spec': {}, 'status': {},
        }
        w_instance = MagicMock()
        mock_watch.Watch.return_value = w_instance
        # 첫 번째 stream 호출: 이벤트 반환 / 두 번째: 예외로 루프 탈출
        w_instance.stream.side_effect = [
            [{'type': 'ADDED', 'object': new_pr}],
            InterruptedError("done"),
        ]
        try:
            watcher_loop()
        except InterruptedError:
            pass
        assert 'ns1/new-pr' in cache.local_cache

    @patch('src.workers.watcher.watch')
    @patch('src.workers.watcher.time.sleep', side_effect=InterruptedError)
    @patch('src.workers.watcher.api')
    def test_stream_deleted_event_removes_from_cache(self, mock_api, mock_sleep, mock_watch):
        """Watch 스트림 DELETED 이벤트가 캐시에서 항목을 제거한다."""
        from src.workers.watcher import watcher_loop
        # 초기 동기화에 포함시켜 캐시에 올려 놓음
        raw_resp = MagicMock()
        raw_resp.data = json.dumps({
            'metadata': {'resourceVersion': '100'},
            'items': [{
                'metadata': {'namespace': 'ns1', 'name': 'to-delete',
                             'resourceVersion': '99', 'labels': {},
                             'creationTimestamp': '2025-01-01T00:00:00Z'},
                'spec': {}, 'status': {},
            }]
        }).encode()
        mock_api.list_cluster_custom_object.return_value = raw_resp
        del_pr = {
            'metadata': {'namespace': 'ns1', 'name': 'to-delete', 'resourceVersion': '101',
                         'labels': {}, 'creationTimestamp': '2025-01-01T00:00:00Z'},
            'spec': {}, 'status': {},
        }
        w_instance = MagicMock()
        mock_watch.Watch.return_value = w_instance
        w_instance.stream.side_effect = [
            [{'type': 'DELETED', 'object': del_pr}],
            InterruptedError("done"),
        ]
        try:
            watcher_loop()
        except InterruptedError:
            pass
        assert 'ns1/to-delete' not in cache.local_cache

    @patch('src.workers.watcher.watch')
    @patch('src.workers.watcher.time.sleep', side_effect=[None, InterruptedError])
    @patch('src.workers.watcher.api')
    def test_generic_exception_preserves_resource_version(self, mock_api, mock_sleep, mock_watch):
        """스트림 중 일반 예외 발생 시 resource_version을 보존하여 전체 재동기화를 하지 않는다."""
        from src.workers.watcher import watcher_loop
        resp = MagicMock()
        resp.data = json.dumps({'metadata': {'resourceVersion': '100'}, 'items': []}).encode()
        mock_api.list_cluster_custom_object.return_value = resp

        w_instance = MagicMock()
        mock_watch.Watch.return_value = w_instance
        # 첫 번째 stream: 일반 예외 / 두 번째: InterruptedError로 탈출
        w_instance.stream.side_effect = [Exception("stream broken"), InterruptedError("done")]
        try:
            watcher_loop()
        except InterruptedError:
            pass
        # 전체 재동기화(list)가 최초 1회만 호출 → resource_version 보존 확인
        assert mock_api.list_cluster_custom_object.call_count == 1
    @patch('src.workers.watcher.watch')
    @patch('src.workers.watcher.time.sleep', side_effect=[None, InterruptedError])
    @patch('src.workers.watcher.api')
    def test_non_410_api_error_preserves_resource_version(self, mock_api, mock_sleep, mock_watch):
        """410 이외의 API 에러는 resource_version을 보존하고 재동기화를 트리거하지 않는다."""
        from src.workers.watcher import watcher_loop
        resp = MagicMock()
        resp.data = json.dumps({'metadata': {'resourceVersion': '100'}, 'items': []}).encode()
        mock_api.list_cluster_custom_object.return_value = resp
        w_instance = MagicMock()
        mock_watch.Watch.return_value = w_instance
        # 첫 번째: 500 에러(sleep), 두 번째: InterruptedError로 탈출
        w_instance.stream.side_effect = [ApiException(status=500), InterruptedError("done")]
        try:
            watcher_loop()
        except InterruptedError:
            pass
        # 초기 동기화 1회만 호출, 재동기화 없음
        assert mock_api.list_cluster_custom_object.call_count == 1

    @patch('src.workers.watcher.watch')
    @patch('src.workers.watcher.time.sleep', side_effect=InterruptedError)
    @patch('src.workers.watcher.api')
    def test_initial_sync_done_stays_true_on_resync(self, mock_api, mock_sleep, mock_watch):
        """이미 동기화 완료된 상태에서 재동기화 시 initial_sync_done이 True를 유지한다."""
        from src.workers.watcher import watcher_loop
        state.initial_sync_done = True
        raw_resp = MagicMock()
        raw_resp.data = json.dumps({'metadata': {'resourceVersion': '200'}, 'items': []}).encode()
        mock_api.list_cluster_custom_object.return_value = raw_resp
        w_instance = MagicMock()
        mock_watch.Watch.return_value = w_instance
        w_instance.stream.side_effect = Exception("test exit")
        try:
            watcher_loop()
        except InterruptedError:
            pass
        assert state.initial_sync_done is True
