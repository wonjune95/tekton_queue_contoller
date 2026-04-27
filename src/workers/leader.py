"""
Leader Election 루프 모듈.

K8s Lease 리소스를 사용해 HA 환경에서 단일 리더를 선출합니다.
리더만 Manager 루프(스케줄링)를 실행합니다.
(docker/app.py L595~L674 발췌)
"""
import time
import datetime

from kubernetes import client as k8s_client
from kubernetes.client.rest import ApiException

from src.config import (
    LEASE_NAME, LEASE_NAMESPACE, POD_NAME,
    LEASE_DURATION_SEC, LEASE_RETRY_PERIOD_SEC, log,
)
from src.cache import _reset_global_admitted
from src import metrics as m
from src import state


def leader_election_loop():
    coord_api = k8s_client.CoordinationV1Api()
    log(f"[LeaderElection] 시작 (Pod: {POD_NAME}, Lease: {LEASE_NAMESPACE}/{LEASE_NAME})")

    while True:
        _is_renewal_attempt = False
        try:
            now = datetime.datetime.now(datetime.timezone.utc)
            try:
                lease = coord_api.read_namespaced_lease(LEASE_NAME, LEASE_NAMESPACE)
            except ApiException as e:
                if e.status == 404:
                    lease_body = k8s_client.V1Lease(
                        metadata=k8s_client.V1ObjectMeta(name=LEASE_NAME, namespace=LEASE_NAMESPACE),
                        spec=k8s_client.V1LeaseSpec(
                            holder_identity=POD_NAME,
                            lease_duration_seconds=LEASE_DURATION_SEC,
                            acquire_time=now,
                            renew_time=now,
                        ),
                    )
                    coord_api.create_namespaced_lease(LEASE_NAMESPACE, lease_body)
                    with state.leader_lock:
                        if not state.is_leader:
                            state.is_leader = True
                            log("[LeaderElection] Leader 획득 (신규 Lease 생성)")
                    time.sleep(LEASE_RETRY_PERIOD_SEC)
                    continue
                else:
                    raise

            holder      = lease.spec.holder_identity
            renew_time  = lease.spec.renew_time
            duration    = lease.spec.lease_duration_seconds or LEASE_DURATION_SEC

            if holder == POD_NAME:
                _is_renewal_attempt = True
                lease.spec.renew_time = now
                coord_api.replace_namespaced_lease(LEASE_NAME, LEASE_NAMESPACE, lease)
                with state.leader_lock:
                    if not state.is_leader:
                        state.is_leader = True
                        log("[LeaderElection] Leader 재획득 (갱신 성공)")
            elif renew_time is None or (now - renew_time).total_seconds() > duration:
                lease.spec.holder_identity        = POD_NAME
                lease.spec.acquire_time           = now
                lease.spec.renew_time             = now
                lease.spec.lease_duration_seconds = LEASE_DURATION_SEC
                coord_api.replace_namespaced_lease(LEASE_NAME, LEASE_NAMESPACE, lease)
                with state.leader_lock:
                    was_leader      = state.is_leader
                    state.is_leader = True
                if not was_leader:
                    _reset_global_admitted()
                    log(f"[LeaderElection] Leader 승격 (이전 Leader: {holder}, Lease 만료)")
            else:
                with state.leader_lock:
                    if state.is_leader:
                        state.is_leader = False
                        log(f"[LeaderElection] Leader 해제 (현재 Leader: {holder})")

        except ApiException as e:
            m.METRIC_API_ERRORS.labels(operation='leader_election').inc()
            if e.status == 409:
                if _is_renewal_attempt:
                    log("[LeaderElection] Lease 갱신 충돌 (409). is_leader 유지 후 재시도.")
                else:
                    log("[LeaderElection] Lease 탈취 충돌 (409). Leader 상태 해제.")
                    with state.leader_lock:
                        if state.is_leader:
                            state.is_leader = False
            else:
                log(f"[LeaderElection] API 에러 ({e.status}): {e.reason}")
        except Exception as e:
            log(f"[LeaderElection] 에러: {e}")

        time.sleep(LEASE_RETRY_PERIOD_SEC)
