"""
Manager(스케줄링) 루프 모듈.

5초 주기로 대기열을 확인하고, 가용 슬롯만큼 Pending PR을 Running으로 전환합니다.
리더 Pod에서만 실행됩니다.
"""
import time
import datetime

from kubernetes.client.rest import ApiException

from src.config import (
    TIER_LABEL_KEY, ENV_LABEL_KEY, DEFAULT_TIER,
    load_crd_config, get_cached_config, log, api, effective_tier,
)
from src.cache import (
    get_queue_status_from_cache,
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
            wait_disp  = f"{int(wait_secs)}s" if wait_secs < 120 else f"{int(wait_secs//60)}m"
            ptype      = labels.get('type', '?')
            env_val    = labels.get(ENV_LABEL_KEY, '?')
            try:
                eff_tier = effective_tier(int(orig_tier), wait_secs, aging_interval, aging_min)
            except ValueError:
                eff_tier = aging_min
            log(f"   {idx+1}. [Tier {orig_tier}->{eff_tier}] "
                f"{ns}/{name} ({ptype}/{env_val}, 대기: {wait_disp})")
    log("=" * 60)


# ── 유효 Tier 승격 이벤트 로그 (W_max 직접 검증 + 역전 기전 그림 소스) ──
# PR별 마지막 관측 유효 Tier. 매 사이클 재계산해 감소(=승격) 시점을 기록한다.
_last_eff_tier: dict = {}


def detect_and_log_promotions(pending: list, cfg: dict, now=None) -> None:
    """대기 중 PR의 유효 Tier 전이를 감지해 로그·메트릭으로 남긴다.

    유효 Tier가 낮아지는 것이 '승격'(우선순위 상승)이다. enqueue~승격 시각으로
    W_max 상한을 실측하고, 승격 타임라인이 Tier 간 역전 기전 그림의 소스가 된다.
    now 주입 가능(테스트용).
    """
    if now is None:
        now = datetime.datetime.now(datetime.timezone.utc)
    aging_interval = cfg["aging_interval_sec"]
    aging_min      = cfg["aging_min_tier"]
    current_keys = set()

    for item in pending:
        ns   = item['metadata']['namespace']
        name = item['metadata'].get('name') or item['metadata'].get('generateName', '') + "(gen)"
        key  = f"{ns}/{name}"
        current_keys.add(key)
        labels = item['metadata'].get('labels') or {}
        try:
            orig = int(labels.get(TIER_LABEL_KEY, DEFAULT_TIER))
        except (ValueError, TypeError):
            orig = DEFAULT_TIER
        created = parse_k8s_timestamp(item['metadata'].get('creationTimestamp', ''))
        wait    = (now - created).total_seconds()
        eff     = effective_tier(orig, wait, aging_interval, aging_min)

        prev = _last_eff_tier.get(key)
        if prev is None:
            _last_eff_tier[key] = eff
        elif eff < prev:  # 승격(유효 Tier 감소)
            env_val = labels.get(ENV_LABEL_KEY, '?')
            log(f"[승격] {ns}/{name} Tier {prev}->{eff} (대기 {int(wait)}s, env:{env_val})")
            m.METRIC_PROMOTION.labels(from_tier=str(prev), to_tier=str(eff)).inc()
            _last_eff_tier[key] = eff

    # 더 이상 대기열에 없는(스케줄/완료/삭제된) PR 정리 — 누수 방지
    for k in list(_last_eff_tier.keys()):
        if k not in current_keys:
            del _last_eff_tier[k]




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

            # 유효 Tier 승격 이벤트 기록 (매 사이클)
            detect_and_log_promotions(pending, cfg)

            # 로그 폭주 방지: pending이 있어도 30초마다(유휴 시 60초마다)만 대시보드 출력
            elapsed = time.time() - last_log_time
            if (pending and elapsed > 30) or elapsed > 60:
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

            # 인가는 이 루프에서만 일어난다(단일 스레드). 따라서 슬롯은 캐시의 running 수만
            # 보면 되며, «인가했지만 아직 running 으로 안 잡힌» 인플라이트를 따로 셀 필요가 없다.
            # 아래 루프가 실행시킨 건수만큼 running 을 증가시켜 사이클 내 초과도 방지한다.
            available_slots = limit - running

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
                        continue
                    except Exception as e:
                        log(f"[에러] 실행 패치 실패 ({t_ns}/{t_name}): {e}")
                        continue
        except Exception as e:
            log(f"[에러] Manager 루프 에러: {e}")
        time.sleep(5)
