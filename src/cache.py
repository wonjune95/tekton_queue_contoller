"""
PipelineRun 인메모리 캐시 모듈.

Watch 인포머가 채우는 PipelineRun 인메모리 캐시를 관리합니다.
"""
import threading
import datetime
import fnmatch

from kubernetes.client.rest import ApiException
from kubernetes import client as k8s_client

from src.config import (
    MANAGED_LABEL_KEY, MANAGED_LABEL_VAL, TIER_LABEL_KEY,
    ENV_LABEL_KEY, CANCEL_STATUSES, DEFAULT_TIER,
    LEASE_NAMESPACE, get_cached_config, DEFAULT_NAMESPACE_PATTERNS,
    log, core_api, effective_tier,
)
from src import metrics as m

# ─── In-memory Cache ─────────────────────────────────────────
local_cache: dict = {}
cache_lock = threading.Lock()



# ─── 유틸리티 ─────────────────────────────────────────────────
def parse_k8s_timestamp(ts_str: str) -> datetime.datetime:
    if not ts_str:
        return datetime.datetime.min.replace(tzinfo=datetime.timezone.utc)
    try:
        return datetime.datetime.strptime(ts_str, "%Y-%m-%dT%H:%M:%SZ").replace(
            tzinfo=datetime.timezone.utc
        )
    except ValueError:
        return datetime.datetime.min.replace(tzinfo=datetime.timezone.utc)


def is_pipelinerun_finished(item: dict) -> bool:
    status = item.get('status', {})
    if status.get('completionTime'):
        return True
    for c in status.get('conditions', []):
        if c.get('type') == 'Succeeded':
            return c.get('status') in ('True', 'False')
    return False


# ─── 캐시 업데이트 ────────────────────────────────────────────
def update_cache(event_type: str, obj: dict):
    ns   = obj['metadata']['namespace']
    name = obj['metadata'].get('name', 'unknown')
    key  = f"{ns}/{name}"

    from src.config import is_target_namespace  # 지연 import (순환 방지)

    with cache_lock:

        if event_type == 'DELETED' and key in local_cache:
            del local_cache[key]
        elif event_type != 'DELETED':
            local_cache[key] = obj



# ─── 큐 상태 조회 ─────────────────────────────────────────────
def get_queue_status_from_cache():
    """캐시에서 running 수와 managed pending 목록을 반환합니다."""
    cfg            = get_cached_config()
    aging_interval = cfg["aging_interval_sec"]
    aging_min      = cfg["aging_min_tier"]
    ns_patterns    = cfg.get("namespace_patterns", DEFAULT_NAMESPACE_PATTERNS)

    running_cnt          = 0
    managed_pending_list = []

    with cache_lock:
        for key, item in local_cache.items():
            ns = item['metadata']['namespace']
            if not any(fnmatch.fnmatch(ns, p) for p in ns_patterns):
                continue
            if is_pipelinerun_finished(item):
                continue
            spec_status = item.get('spec', {}).get('status')
            if spec_status in CANCEL_STATUSES:
                continue
            if spec_status != 'PipelineRunPending':
                running_cnt += 1
            else:
                labels = item['metadata'].get('labels') or {}
                if labels.get(MANAGED_LABEL_KEY) == MANAGED_LABEL_VAL:
                    managed_pending_list.append(item)

    now_utc = datetime.datetime.now(datetime.timezone.utc)

    def _sort_key(item):
        labels    = item['metadata'].get('labels') or {}
        tier_str  = labels.get(TIER_LABEL_KEY, str(DEFAULT_TIER))
        try:
            tier = int(tier_str)
        except ValueError:
            tier = DEFAULT_TIER
        created_at   = parse_k8s_timestamp(item['metadata'].get('creationTimestamp', ''))
        wait_seconds = (now_utc - created_at).total_seconds()
        eff_tier     = effective_tier(tier, wait_seconds, aging_interval, aging_min)
        return (eff_tier, item['metadata'].get('creationTimestamp', ''))

    managed_pending_list.sort(key=_sort_key)
    return running_cnt, managed_pending_list
