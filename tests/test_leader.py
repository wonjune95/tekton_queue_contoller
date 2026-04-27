"""leader.py 단위 테스트 — Leader Election 시나리오."""
import datetime
from unittest.mock import patch, MagicMock, PropertyMock
from kubernetes.client.rest import ApiException
from kubernetes import client as k8s_client

import src.state as state


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# 헬퍼: Lease 객체 생성
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
def _make_lease(holder="other-pod", renew_time=None, duration=15):
    lease = MagicMock()
    lease.spec.holder_identity = holder
    lease.spec.renew_time = renew_time
    lease.spec.lease_duration_seconds = duration
    return lease


class TestLeaderElection:
    @patch('src.workers.leader.time.sleep', side_effect=InterruptedError)
    @patch('src.workers.leader.k8s_client.CoordinationV1Api')
    @patch('src.workers.leader._reset_global_admitted')
    def test_create_new_lease(self, mock_reset, MockCoord, mock_sleep):
        """Lease가 없으면 새로 생성하고 리더가 된다."""
        from src.workers.leader import leader_election_loop
        coord = MockCoord.return_value
        coord.read_namespaced_lease.side_effect = ApiException(status=404)
        try:
            leader_election_loop()
        except InterruptedError:
            pass
        coord.create_namespaced_lease.assert_called_once()
        assert state.is_leader is True

    @patch('src.workers.leader.time.sleep', side_effect=[None, InterruptedError])
    @patch('src.workers.leader.k8s_client.CoordinationV1Api')
    @patch('src.workers.leader.POD_NAME', 'my-pod')
    def test_renew_lease(self, MockCoord, mock_sleep):
        """자신이 holder이면 갱신한다."""
        from src.workers.leader import leader_election_loop
        coord = MockCoord.return_value
        lease = _make_lease(holder="my-pod",
                           renew_time=datetime.datetime.now(datetime.timezone.utc))
        coord.read_namespaced_lease.return_value = lease
        try:
            leader_election_loop()
        except InterruptedError:
            pass
        coord.replace_namespaced_lease.assert_called()
        assert state.is_leader is True

    @patch('src.workers.leader.time.sleep', side_effect=[None, InterruptedError])
    @patch('src.workers.leader.k8s_client.CoordinationV1Api')
    @patch('src.workers.leader._reset_global_admitted')
    @patch('src.workers.leader.POD_NAME', 'my-pod')
    def test_takeover_expired_lease(self, mock_reset, MockCoord, mock_sleep):
        """Lease 만료 시 탈취하여 리더가 된다."""
        from src.workers.leader import leader_election_loop
        coord = MockCoord.return_value
        expired_time = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(seconds=60)
        lease = _make_lease(holder="dead-pod", renew_time=expired_time, duration=15)
        coord.read_namespaced_lease.return_value = lease
        try:
            leader_election_loop()
        except InterruptedError:
            pass
        assert state.is_leader is True
        mock_reset.assert_called()

    @patch('src.workers.leader.time.sleep', side_effect=[None, InterruptedError])
    @patch('src.workers.leader.k8s_client.CoordinationV1Api')
    @patch('src.workers.leader.POD_NAME', 'my-pod')
    def test_standby_waits(self, MockCoord, mock_sleep):
        """다른 Pod이 활성 리더이면 대기 상태를 유지한다."""
        from src.workers.leader import leader_election_loop
        coord = MockCoord.return_value
        recent = datetime.datetime.now(datetime.timezone.utc)
        lease = _make_lease(holder="other-pod", renew_time=recent, duration=15)
        coord.read_namespaced_lease.return_value = lease
        state.is_leader = True  # 이전에 리더였던 상태
        try:
            leader_election_loop()
        except InterruptedError:
            pass
        assert state.is_leader is False

    @patch('src.workers.leader.time.sleep', side_effect=[None, InterruptedError])
    @patch('src.workers.leader.k8s_client.CoordinationV1Api')
    @patch('src.workers.leader.POD_NAME', 'my-pod')
    def test_409_conflict_on_renewal_keeps_leader(self, MockCoord, mock_sleep):
        """갱신 시 409 Conflict → 리더 상태 유지."""
        from src.workers.leader import leader_election_loop
        coord = MockCoord.return_value
        lease = _make_lease(holder="my-pod",
                           renew_time=datetime.datetime.now(datetime.timezone.utc))
        coord.read_namespaced_lease.return_value = lease
        coord.replace_namespaced_lease.side_effect = ApiException(status=409)
        state.is_leader = True
        try:
            leader_election_loop()
        except InterruptedError:
            pass
        assert state.is_leader is True  # 갱신 충돌이지만 유지

    @patch('src.workers.leader.time.sleep', side_effect=[None, InterruptedError])
    @patch('src.workers.leader.k8s_client.CoordinationV1Api')
    @patch('src.workers.leader.POD_NAME', 'my-pod')
    def test_409_conflict_on_takeover_loses_leader(self, MockCoord, mock_sleep):
        """탈취 시 409 Conflict → 리더 상태 해제."""
        from src.workers.leader import leader_election_loop
        coord = MockCoord.return_value
        expired = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(seconds=60)
        lease = _make_lease(holder="other-pod", renew_time=expired, duration=15)
        coord.read_namespaced_lease.return_value = lease
        coord.replace_namespaced_lease.side_effect = ApiException(status=409)
        state.is_leader = True
        try:
            leader_election_loop()
        except InterruptedError:
            pass
        assert state.is_leader is False

    @patch('src.workers.leader.time.sleep', side_effect=[None, InterruptedError])
    @patch('src.workers.leader.k8s_client.CoordinationV1Api')
    def test_api_500_error_no_crash(self, MockCoord, mock_sleep):
        """500 에러에도 루프가 크래시하지 않는다."""
        from src.workers.leader import leader_election_loop
        coord = MockCoord.return_value
        coord.read_namespaced_lease.side_effect = ApiException(status=500)
        try:
            leader_election_loop()
        except InterruptedError:
            pass
        # 크래시 없이 통과

    @patch('src.workers.leader.time.sleep', side_effect=[None, InterruptedError])
    @patch('src.workers.leader.k8s_client.CoordinationV1Api')
    def test_renew_time_none_treated_as_expired(self, MockCoord, mock_sleep):
        """renewTime이 None이면 만료로 간주하고 탈취한다."""
        from src.workers.leader import leader_election_loop
        coord = MockCoord.return_value
        lease = _make_lease(holder="other-pod", renew_time=None)
        coord.read_namespaced_lease.return_value = lease
        try:
            leader_election_loop()
        except InterruptedError:
            pass
        assert state.is_leader is True
