"""
Webhook 모듈.

Flask 앱과 /mutate, /healthz, /readyz 라우트를 정의합니다.
(docker/app.py L92, L410~L592 발췌)
"""
import json
import base64
import fnmatch
import datetime

from flask import Flask, request, jsonify

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
        return jsonify({
            "apiVersion": "admission.k8s.io/v1",
            "kind": "AdmissionReview",
            "response": {"uid": "", "allowed": True}
        })

    uid       = request_info.get("request", {}).get("uid", "")
    req_obj   = request_info.get("request", {}).get("object", {})
    metadata  = req_obj.get("metadata") or {}
    namespace = metadata.get("namespace", "")
    labels    = metadata.get("labels") or {}
    username  = request_info.get("request", {}).get("userInfo", {}).get("username", "")

    # 대상 네임스페이스가 아니면 그냥 통과
    if not is_target_namespace(namespace):
        return jsonify({
            "apiVersion": "admission.k8s.io/v1",
            "kind": "AdmissionReview",
            "response": {"uid": uid, "allowed": True}
        })

    cfg               = get_cached_config()
    tier_val          = determine_tier(labels, cfg["tier_rules"])
    tier_label_escaped    = TIER_LABEL_KEY.replace("/", "~1")
    managed_label_escaped = MANAGED_LABEL_KEY.replace("/", "~1")
    pr_name           = metadata.get('name') or metadata.get('generateName', 'unknown') + "(gen)"
    ptype             = labels.get('type', '?')

    is_from_dashboard = fnmatch.fnmatch(username, cfg["dashboard_sa_pattern"])

    # Dashboard 외 출처 → Pending 설정 (스케줄링 제외)
    if not is_from_dashboard:
        m.METRIC_WEBHOOK_HELD.labels(tier=str(tier_val)).inc()
        log(f"[Webhook 보류] {namespace}/{pr_name} ({ptype}, Tier {tier_val}) "
            f"-> Dashboard 외 출처 ({username}). Pending 설정 (스케줄링 제외).")
        patch = [{"op": "add", "path": "/spec/status", "value": "PipelineRunPending"}]
        if not metadata.get("labels"):
            patch.append({"op": "add", "path": "/metadata/labels",
                          "value": {TIER_LABEL_KEY: str(tier_val)}})
        else:
            patch.append({"op": "add",
                          "path": f"/metadata/labels/{tier_label_escaped}",
                          "value": str(tier_val)})
        return _admission_patch_response(uid, patch)

    # 캐시 동기화 미완료 방어
    if not state.initial_sync_done:
        log(f"[경고] 캐시 미동기화 상태에서 Dashboard Webhook 요청 수신 ({namespace}). 통과 처리.")
        return jsonify({
            "apiVersion": "admission.k8s.io/v1",
            "kind": "AdmissionReview",
            "response": {"uid": uid, "allowed": True}
        })

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
        if not metadata.get("labels"):
            patch.append({"op": "add", "path": "/metadata/labels",
                          "value": {TIER_LABEL_KEY: str(tier_val),
                                    MANAGED_LABEL_KEY: MANAGED_LABEL_VAL}})
        else:
            patch.append({"op": "add",
                          "path": f"/metadata/labels/{tier_label_escaped}",
                          "value": str(tier_val)})
            patch.append({"op": "add",
                          "path": f"/metadata/labels/{managed_label_escaped}",
                          "value": MANAGED_LABEL_VAL})
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

    patch = []
    if not metadata.get("labels"):
        patch.append({"op": "add", "path": "/metadata/labels",
                      "value": {TIER_LABEL_KEY: str(tier_val)}})
    else:
        patch.append({"op": "add",
                      "path": f"/metadata/labels/{tier_label_escaped}",
                      "value": str(tier_val)})
    return _admission_patch_response(uid, patch)


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
