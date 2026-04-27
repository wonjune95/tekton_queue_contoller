"""
Tekton Queue Controller — 진입점 (Entry Point)

기존 단일 파일(app.py)에서 src/ 모듈로 분리한 뒤,
이 파일은 초기화와 스레드 시작만 담당하는 slim wrapper로 동작합니다.

각 기능의 구현은 아래 모듈을 참고하세요.
  - src/config.py      : 상수·CRD 설정·K8s 클라이언트
  - src/metrics.py     : Prometheus 메트릭
  - src/state.py       : 공유 전역 상태 (is_leader, initial_sync_done)
  - src/cache.py       : 인메모리 캐시·admitted 카운터
  - src/webhook.py     : Flask /mutate /healthz /readyz
  - src/workers/leader.py  : Leader Election 루프
  - src/workers/manager.py : 스케줄링 루프
  - src/workers/watcher.py : K8s Watch 동기화 루프
"""
import threading

from prometheus_client import start_http_server

from src.config import (
    load_crd_config, get_cached_config,
    POD_NAME, LEASE_NAME, LEASE_NAMESPACE,
    LEASE_DURATION_SEC, CANCEL_STATUSES, log,
)
from src.webhook import app
from src.workers.leader  import leader_election_loop
from src.workers.manager import manager_loop
from src.workers.watcher import watcher_loop


if __name__ == "__main__":
    log("Tekton Queue Controller 기동 준비 중...")
    log(f"  Pod: {POD_NAME}")
    initial_limit = load_crd_config()
    cfg = get_cached_config()
    log(f"  네임스페이스 패턴: {cfg['namespace_patterns']}")
    log(f"  Limit: {cfg['max_pipelines']}")
    log(f"  Aging: {cfg['aging_interval_sec']}초당 Tier 1 승격 (최소 Tier {cfg['aging_min_tier']})")
    log("  Tier Rules:")
    for rule in cfg['tier_rules']:
        mt    = rule.get('matchType', 'env')
        extra = f", labelKey: {rule['labelKey']}" if mt == 'label' else ""
        log(f"    Tier {rule['tier']} [{mt}{extra}] "
            f"{rule['pattern']} ({rule.get('description', '')})")
    log(f"  취소 상태 목록: {sorted(CANCEL_STATUSES)}")
    log(f"  Dashboard SA 패턴: {cfg['dashboard_sa_pattern']}")
    log(f"  Leader Election: Lease={LEASE_NAMESPACE}/{LEASE_NAME}, "
        f"Duration={LEASE_DURATION_SEC}s, RetryPeriod=2s")

    # Prometheus 메트릭 서버 (포트 9090)
    start_http_server(9090)
    log("Prometheus metrics server started on port 9090")

    # 백그라운드 스레드 시작
    threading.Thread(target=leader_election_loop, daemon=True).start()
    threading.Thread(target=manager_loop,         daemon=True).start()
    threading.Thread(target=watcher_loop,         daemon=True).start()

    # Flask HTTPS 서버 (포트 8443)
    app.run(
        host='0.0.0.0',
        port=8443,
        ssl_context=('/certs/tls.crt', '/certs/tls.key'),
        threaded=True,
    )
