"""
Webhook 모듈.

Flask 앱과 /mutate, /healthz, /readyz 라우트를 정의합니다.
"""
import json
import base64
import fnmatch
import datetime
import time

from flask import Flask, request, jsonify, g

from src.config import (
    TIER_LABEL_KEY, MANAGED_LABEL_KEY, MANAGED_LABEL_VAL,
    ENV_LABEL_KEY, get_cached_config, is_target_namespace,
    determine_tier, POD_NAME, log,
)
from src.cache import (
    local_cache, cache_lock,
    get_queue_status_from_cache, _try_increment_global_admitted,
    _get_global_admitted, parse_k8s_timestamp,
)
from src import metrics as m
from src import state

app = Flask(__name__)


# ─── /mutate 지연 계측 (§3.4 제어 계층 부하: p50/p99) ──────────
@app.before_request
def _webhook_timer_start():
    g._webhook_start = time.perf_counter()


@app.after_request
def _webhook_timer_observe(response):
    if request.path == '/mutate':
        start = getattr(g, '_webhook_start', None)
        if start is not None:
            m.METRIC_WEBHOOK_LATENCY.observe(time.perf_counter() - start)
    return response


# ─── Health 엔드포인트 ─────────────────────────────────────────
@app.route('/healthz', methods=['GET'])
def healthz():
    with state.leader_lock:
        leader_status = state.is_leader
    return jsonify({"status": "ok", "leader": leader_status, "pod": POD_NAME}), 200


@app.route('/readyz', methods=['GET'])
def readyz():
    if not state.initial_sync_done:
        return jsonify({"status": "not_ready", "reason": "initial sync not complete"}), 503
    with cache_lock:
        cache_size = len(local_cache)
    with state.leader_lock:
        leader_status = state.is_leader
    return jsonify({
        "status": "ready",
        "cached_resources": cache_size,
        "leader": leader_status,
        "pod": POD_NAME,
    }), 200


# ─── Mutating Admission Webhook ───────────────────────────────
@app.route('/mutate', methods=['POST'])
def mutate_pipelinerun():
    request_info = request.get_json(silent=True)
    if not request_info:
        log("[경고] Webhook 요청 파싱 실패. 통과 처리.")
        return _allow("")

    uid       = request_info.get("request", {}).get("uid", "")
    req_obj   = request_info.get("request", {}).get("object", {})
    metadata  = req_obj.get("metadata") or {}
    namespace = metadata.get("namespace", "")
    labels    = metadata.get("labels") or {}
    username  = request_info.get("request", {}).get("userInfo", {}).get("username", "")

    # 대상 네임스페이스가 아니면 그냥 통과
    if not is_target_namespace(namespace):
        return _allow(uid)

    cfg               = get_cached_config()
    tier_val          = determine_tier(labels, cfg["tier_rules"])
    pr_name           = metadata.get('name') or metadata.get('generateName', 'unknown') + "(gen)"
    ptype             = labels.get('type', '?')

    is_managed = any(fnmatch.fnmatch(username, p) for p in cfg["managed_sa_patterns"])

    # 관리 대상 외 출처 → Pending 설정 (스케줄링 제외)
    if not is_managed:
        m.METRIC_WEBHOOK_HELD.labels(tier=str(tier_val)).inc()
        log(f"[Webhook 보류] {namespace}/{pr_name} ({ptype}, Tier {tier_val}) "
            f"-> 관리 대상 외 출처 ({username}). Pending 설정 (스케줄링 제외).")
        patch = [{"op": "add", "path": "/spec/status", "value": "PipelineRunPending"}]
        patch += _label_patch(metadata.get("labels"), {TIER_LABEL_KEY: str(tier_val)})
        return _admission_patch_response(uid, patch)

    # 캐시 동기화 미완료 방어
    if not state.initial_sync_done:
        log(f"[경고] 캐시 미동기화 상태에서 Dashboard Webhook 요청 수신 ({namespace}). 통과 처리.")
        return _allow(uid)

    limit        = cfg["max_pipelines"]
    running_cnt, _ = get_queue_status_from_cache()
    env_val      = labels.get(ENV_LABEL_KEY, '?')
    is_urgent    = labels.get('queue.tekton.dev/urgent', '') == 'true'
    match_info   = "urgent" if is_urgent else f"env:{env_val}"

    should_admit, effective_running = _try_increment_global_admitted(running_cnt, limit)

    # 쿼터 초과 → Pending + managed 라벨 (대기열)
    if not should_admit:
        m.METRIC_WEBHOOK_QUEUED.labels(tier=str(tier_val)).inc()
        log(f"[Webhook 차단] {namespace}/{pr_name} ({ptype}/{match_info}, Tier {tier_val}) "
            f"-> 쿼터 초과(Running:{effective_running} >= Limit:{limit}). 대기열로 보냅니다.")
        patch = [{"op": "add", "path": "/spec/status", "value": "PipelineRunPending"}]
        patch += _label_patch(metadata.get("labels"),
                              {TIER_LABEL_KEY: str(tier_val), MANAGED_LABEL_KEY: MANAGED_LABEL_VAL})
        return _admission_patch_response(uid, patch)

    # generateName PR은 이름이 확정되지 않아 phantom entry 삽입 스킵
    pr_real_name = metadata.get('name')
    if pr_real_name:
        pr_key = f"{namespace}/{pr_real_name}"
        phantom_labels = dict(labels)
        phantom_labels[TIER_LABEL_KEY] = str(tier_val)
        phantom_entry = {
            'metadata': {
                'namespace': namespace,
                'name': pr_real_name,
                'labels': phantom_labels,
                'creationTimestamp': datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                'resourceVersion': '__admitted__',
            },
            'spec': {'status': None},
            'status': {},
        }
        with cache_lock:
            if pr_key not in local_cache:
                local_cache[pr_key] = phantom_entry

    m.METRIC_WEBHOOK_ADMITTED.labels(tier=str(tier_val)).inc()
    log(f"[Webhook 통과] {namespace}/{pr_name} ({ptype}/{match_info}, Tier {tier_val}) "
        f"-> 즉시 실행 허용 (Running:{effective_running + 1}/{limit})")

    patch = _label_patch(metadata.get("labels"), {TIER_LABEL_KEY: str(tier_val)})
    return _admission_patch_response(uid, patch)


def _allow(uid: str):
    """패치 없이 통과시키는 AdmissionReview 응답."""
    return jsonify({
        "apiVersion": "admission.k8s.io/v1",
        "kind": "AdmissionReview",
        "response": {"uid": uid, "allowed": True},
    })


def _label_patch(existing_labels: dict, new_labels: dict) -> list:
    """라벨 추가용 JSONPatch ops 생성.
    labels 맵이 없으면 통째로 add, 있으면 키별 escaped path로 add."""
    if not existing_labels:
        return [{"op": "add", "path": "/metadata/labels", "value": dict(new_labels)}]
    return [
        {"op": "add", "path": f"/metadata/labels/{k.replace('/', '~1')}", "value": v}
        for k, v in new_labels.items()
    ]


def _admission_patch_response(uid: str, patch: list):
    patch_b64 = base64.b64encode(json.dumps(patch).encode('utf-8')).decode('utf-8')
    return jsonify({
        "apiVersion": "admission.k8s.io/v1",
        "kind": "AdmissionReview",
        "response": {
            "uid": uid, "allowed": True,
            "patchType": "JSONPatch", "patch": patch_b64
        }
    })
