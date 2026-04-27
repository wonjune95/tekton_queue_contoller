"""
Manager(스케줄링) 루프 모듈.

5초 주기로 대기열을 확인하고, 가용 슬롯만큼 Pending PR을 Running으로 전환합니다.
리더 Pod에서만 실행됩니다.
(docker/app.py L677~L748 발췌)
"""
import time
import datetime

from kubernetes.client.rest import ApiException

from src.config import (
    TIER_LABEL_KEY, ENV_LABEL_KEY, DEFAULT_TIER,
    load_crd_config, get_cached_config, log, api,
)
from src.cache import (
    get_queue_status_from_cache, _get_global_admitted,
    local_cache, cache_lock, parse_k8s_timestamp,
)
from src import metrics as m
from src import state


def print_dashboard(limit: int, running_cnt: int, pending_list: list, cfg: dict):
    bar_length    = 20
    filled_length = min(int(bar_length * running_cnt // limit) if limit > 0 else 0, bar_length)
    bar           = '█' * filled_length + '-' * (bar_length - filled_length)
    aging_interval = cfg["aging_interval_sec"]
    aging_min      = cfg["aging_min_tier"]
    log("=" * 60)
    log(f"[스케줄링 현황] Limit: {limit} | Aging: {aging_interval}s | MinTier: {aging_min}")
    log(f"실행 중 (Running) : {running_cnt:2d} / {limit:2d} |{bar}|")
    log(f"대기 중 (Pending) : {len(pending_list):2d} 개")
    if pending_list:
        now_utc = datetime.datetime.now(datetime.timezone.utc)
        log("-" * 60)
        log("   [대기열 순번 Top 5 (Priority & FIFO + Aging)]")
        for idx, item in enumerate(pending_list[:5]):
            ns         = item['metadata']['namespace']
            name       = item['metadata'].get('name') or item['metadata'].get('generateName', '') + "(gen)"
            labels     = item['metadata'].get('labels') or {}
            orig_tier  = labels.get(TIER_LABEL_KEY, str(DEFAULT_TIER))
            created_at = parse_k8s_timestamp(item['metadata'].get('creationTimestamp', ''))
            wait_secs  = (now_utc - created_at).total_seconds()
            aging_bonus = int(wait_secs // aging_interval) if aging_interval > 0 else 0
            wait_disp  = f"{int(wait_secs)}s" if wait_secs < 120 else f"{int(wait_secs//60)}m"
            ptype      = labels.get('type', '?')
            env_val    = labels.get(ENV_LABEL_KEY, '?')
            try:
                tier_int       = int(orig_tier)
                effective_tier = min(tier_int, max(aging_min, tier_int - aging_bonus))
            except ValueError:
                effective_tier = aging_min
            log(f"   {idx+1}. [Tier {orig_tier}->{effective_tier}] "
                f"{ns}/{name} ({ptype}/{env_val}, 대기: {wait_disp})")
    log("=" * 60)


def manager_loop():
    log("[Manager] 스레드 시작 (스케줄링 주기: 5초)")
    last_log_time = 0

    while True:
        try:
            with state.leader_lock:
                currently_leader = state.is_leader
            if not currently_leader:
                time.sleep(5)
                continue

            limit          = load_crd_config()
            cfg            = get_cached_config()
            running, pending = get_queue_status_from_cache()

            if pending or abs(time.time() - last_log_time) > 60:
                print_dashboard(limit, running, pending, cfg)
                last_log_time = time.time()

            m.METRIC_QUEUE_LIMIT.set(limit)
            m.METRIC_QUEUE_RUNNING.set(running)
            m.METRIC_QUEUE_PENDING.clear()
            pending_by_tier = {}
            for target in pending:
                t_labels = target['metadata'].get('labels') or {}
                tier_val = t_labels.get(TIER_LABEL_KEY, str(DEFAULT_TIER))
                pending_by_tier[tier_val] = pending_by_tier.get(tier_val, 0) + 1
            for t_val, count in pending_by_tier.items():
                m.METRIC_QUEUE_PENDING.labels(tier=str(t_val)).set(count)

            effective_running = running + _get_global_admitted()
            available_slots   = limit - effective_running

            if available_slots > 0 and pending:
                scheduled = 0
                for target in pending:
                    if scheduled >= available_slots:
                        break
                    t_name   = target['metadata']['name']
                    t_ns     = target['metadata']['namespace']
                    t_labels = target['metadata'].get('labels') or {}
                    tier_val = t_labels.get(TIER_LABEL_KEY, str(DEFAULT_TIER))
                    ptype    = t_labels.get('type', '?')
                    env_val  = t_labels.get(ENV_LABEL_KEY, '?')
                    created_at = parse_k8s_timestamp(target['metadata'].get('creationTimestamp', ''))
                    wait_secs  = (datetime.datetime.now(datetime.timezone.utc) - created_at).total_seconds()
                    try:
                        api.patch_namespaced_custom_object(
                            'tekton.dev', 'v1', t_ns, 'pipelineruns', t_name,
                            {'spec': {'status': None}}
                        )
                        m.METRIC_SCHEDULED.labels(tier=str(tier_val)).inc()
                        log(f"[스케줄링 완료] {t_ns}/{t_name} ({ptype}/{env_val}, "
                            f"Tier {tier_val}, 대기시간: {int(wait_secs)}s) -> 실행 시작")
                        running   += 1
                        scheduled += 1
                        with cache_lock:
                            key = f"{t_ns}/{t_name}"
                            if key in local_cache:
                                local_cache[key]['spec']['status'] = None
                    except ApiException as e:
                        m.METRIC_API_ERRORS.labels(operation='patch_pipelinerun').inc()
                        log(f"[에러] 실행 패치 실패 ({t_ns}/{t_name}): API 에러 {e.status} - {e.reason}")
                    except Exception as e:
                        log(f"[에러] 실행 패치 실패 ({t_ns}/{t_name}): {e}")
        except Exception as e:
            log(f"[에러] Manager 루프 에러: {e}")
        time.sleep(5)
