"""
캐시 및 admitted 카운터 모듈.

PipelineRun 인메모리 캐시와 HA 환경의 cross-pod admitted 카운터를 관리합니다.
(docker/app.py L44~L60, L164~L408 발췌)
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
    log, core_api,
)
from src import metrics as m

# ─── In-memory Cache ─────────────────────────────────────────
local_cache: dict = {}
cache_lock = threading.Lock()

# ─── Webhook admitted 카운터 (ConfigMap primary, per-pod fallback) ─
webhook_admitted_count: int = 0
admitted_lock = threading.Lock()
ADMITTED_CM_NAME = "tekton-queue-admitted-count"


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


# ─── ConfigMap admitted 카운터 함수 ──────────────────────────
def _ensure_admitted_configmap():
    try:
        core_api.create_namespaced_config_map(
            LEASE_NAMESPACE,
            k8s_client.V1ConfigMap(
                metadata=k8s_client.V1ObjectMeta(name=ADMITTED_CM_NAME, namespace=LEASE_NAMESPACE),
                data={'admitted': '0'}
            )
        )
        log(f"[AdmittedCM] ConfigMap {LEASE_NAMESPACE}/{ADMITTED_CM_NAME} 생성 완료")
    except ApiException as e:
        if e.status != 409:
            raise


def _try_increment_global_admitted(running_cnt: int, limit: int, max_retries: int = 5):
    global webhook_admitted_count
    for _ in range(max_retries):
        try:
            cm = core_api.read_namespaced_config_map(ADMITTED_CM_NAME, LEASE_NAMESPACE)
            admitted = int(cm.data.get('admitted', '0'))
            effective_running = running_cnt + admitted
            if effective_running >= limit:
                return False, effective_running
            cm.data['admitted'] = str(admitted + 1)
            core_api.replace_namespaced_config_map(ADMITTED_CM_NAME, LEASE_NAMESPACE, cm)
            return True, effective_running
        except ApiException as e:
            if e.status == 409:
                continue
            elif e.status == 404:
                try:
                    _ensure_admitted_configmap()
                except Exception:
                    break
                continue
            else:
                log(f"[AdmittedCM] ConfigMap 업데이트 실패 (API {e.status}). per-pod fallback 사용.")
                break
        except Exception as e:
            log(f"[AdmittedCM] ConfigMap 업데이트 실패 ({e}). per-pod fallback 사용.")
            break
    with admitted_lock:
        effective_running = running_cnt + webhook_admitted_count
        if effective_running < limit:
            webhook_admitted_count += 1
            return True, effective_running
        return False, effective_running


def _decrement_global_admitted(max_retries: int = 5):
    global webhook_admitted_count
    for _ in range(max_retries):
        try:
            cm = core_api.read_namespaced_config_map(ADMITTED_CM_NAME, LEASE_NAMESPACE)
            admitted = int(cm.data.get('admitted', '0'))
            if admitted <= 0:
                return
            cm.data['admitted'] = str(admitted - 1)
            core_api.replace_namespaced_config_map(ADMITTED_CM_NAME, LEASE_NAMESPACE, cm)
            return
        except ApiException as e:
            if e.status == 409:
                continue
            elif e.status == 404:
                return
            else:
                log(f"[AdmittedCM] ConfigMap 감소 실패 (API {e.status}). per-pod fallback 사용.")
                break
        except Exception as e:
            log(f"[AdmittedCM] ConfigMap 감소 실패 ({e}). per-pod fallback 사용.")
            break
    with admitted_lock:
        webhook_admitted_count = max(0, webhook_admitted_count - 1)


def _reset_global_admitted():
    global webhook_admitted_count
    for _ in range(5):
        try:
            try:
                cm = core_api.read_namespaced_config_map(ADMITTED_CM_NAME, LEASE_NAMESPACE)
                cm.data['admitted'] = '0'
                core_api.replace_namespaced_config_map(ADMITTED_CM_NAME, LEASE_NAMESPACE, cm)
            except ApiException as e:
                if e.status == 404:
                    _ensure_admitted_configmap()
                elif e.status == 409:
                    continue
                else:
                    raise
            with admitted_lock:
                webhook_admitted_count = 0
            return
        except Exception as e:
            log(f"[AdmittedCM] ConfigMap 초기화 실패 ({e}). per-pod fallback 사용.")
    with admitted_lock:
        webhook_admitted_count = 0


def _get_global_admitted() -> int:
    try:
        cm = core_api.read_namespaced_config_map(ADMITTED_CM_NAME, LEASE_NAMESPACE)
        return int(cm.data.get('admitted', '0'))
    except Exception:
        with admitted_lock:
            return webhook_admitted_count


# ─── 캐시 업데이트 ────────────────────────────────────────────
def update_cache(event_type: str, obj: dict):
    ns   = obj['metadata']['namespace']
    name = obj['metadata'].get('name', 'unknown')
    key  = f"{ns}/{name}"

    from src.config import is_target_namespace  # 지연 import (순환 방지)

    with cache_lock:
        existing = local_cache.get(key)
        is_phantom_replacement = (
            existing is not None and
            existing.get('metadata', {}).get('resourceVersion') == '__admitted__'
        )
        is_new_addition = key not in local_cache

        if event_type == 'DELETED' and key in local_cache:
            del local_cache[key]
        elif event_type != 'DELETED':
            local_cache[key] = obj

    if is_target_namespace(ns):
        if event_type in ('ADDED', 'MODIFIED') and (is_new_addition or is_phantom_replacement):
            spec_status = obj.get('spec', {}).get('status')
            if spec_status != 'PipelineRunPending':
                _decrement_global_admitted()
        elif event_type == 'DELETED' and is_phantom_replacement:
            _decrement_global_admitted()


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
        aging_bonus  = int(wait_seconds // aging_interval) if aging_interval > 0 else 0
        effective_tier = min(tier, max(aging_min, tier - aging_bonus))
        return (effective_tier, item['metadata'].get('creationTimestamp', ''))

    managed_pending_list.sort(key=_sort_key)
    return running_cnt, managed_pending_list
