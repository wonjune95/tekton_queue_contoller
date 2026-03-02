import time
import threading
import fnmatch
import copy
import json
import base64
import datetime
import logging
from flask import Flask, request, jsonify
from kubernetes import client, config, watch
from kubernetes.client.rest import ApiException

# =========================================================
# [설정]
NAMESPACE_PATTERN = "*-cicd"
DEFAULT_LIMIT = 10
MANAGED_LABEL_KEY = "queue.tekton.dev/managed"
MANAGED_LABEL_VAL = "yes"
# =========================================================

local_cache = {}
cache_lock = threading.Lock()

try:
    config.load_incluster_config()
except:
    config.load_kube_config()

api = client.CustomObjectsApi()
app = Flask(__name__)

logging.getLogger('werkzeug').setLevel(logging.ERROR)

# ---------------------------------------------------------
# [유틸리티 및 로깅 로직]
# ---------------------------------------------------------
def log(msg):
    now = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{now}] {msg}", flush=True)

def print_dashboard(limit, running_cnt, pending_list):
    bar_length = 20
    filled_length = int(bar_length * running_cnt // limit) if limit > 0 else 0
    filled_length = min(filled_length, bar_length)
    bar = '█' * filled_length + '-' * (bar_length - filled_length)
    
    log("=" * 55)
    log(f"[스케줄링 현황] 현재 쿼터(Limit): {limit} 개")
    log(f"실행 중 (Running) : {running_cnt:2d} / {limit:2d} |{bar}|")
    log(f"대기 중 (Pending) : {len(pending_list):2d} 개")
    
    if len(pending_list) > 0:
        log("-" * 55)
        log("   [대기열 순번 Top 3 (FIFO)]")
        for idx, item in enumerate(pending_list[:3]):
            ns = item['metadata']['namespace']
            name = item['metadata'].get('name') or item['metadata'].get('generateName', '') + "(gen)"
            log(f"   {idx+1}. {ns}/{name}")
    log("=" * 55)

def is_target_namespace(namespace):
    return fnmatch.fnmatch(namespace, NAMESPACE_PATTERN)

def get_queue_status_from_cache():
    running_cnt = 0
    managed_pending_list = []
    with cache_lock:
        for key, item in local_cache.items():
            ns = item['metadata']['namespace']
            if not is_target_namespace(ns): continue
            
            spec_status = item.get('spec', {}).get('status')
            conditions = item.get('status', {}).get('conditions', [])
            
            # 이미 완료된 파이프라인 패스
            if conditions and conditions[0]['status'] != 'Unknown': continue
            
            if spec_status != 'PipelineRunPending':
                running_cnt += 1
            else:
                labels = item['metadata'].get('labels', {})
                if labels.get(MANAGED_LABEL_KEY) == MANAGED_LABEL_VAL:
                    managed_pending_list.append(item)

    # etcd의 creationTimestamp를 기준으로 정렬 (FIFO 보장)
    managed_pending_list.sort(key=lambda x: x['metadata']['creationTimestamp'])
    return running_cnt, managed_pending_list

def update_cache(event_type, obj):
    ns = obj['metadata']['namespace']
    name = obj['metadata'].get('name', 'unknown') 
    key = f"{ns}/{name}"
    with cache_lock:
        if event_type == 'DELETED' and key in local_cache:
            del local_cache[key]
        elif event_type != 'DELETED':
            local_cache[key] = obj

def get_limit_from_crd():
    try:
        obj = api.get_cluster_custom_object('tekton.devops', 'v1', 'globallimits', 'tekton-queue-limit')
        return int(obj['spec']['maxPipelines'])
    except:
        return DEFAULT_LIMIT

# ---------------------------------------------------------
# [Webhook 통제 로직] Proactive Admission Control
# ---------------------------------------------------------
@app.route('/mutate', methods=['POST'])
def mutate_pipelinerun():
    request_info = request.get_json()
    uid = request_info.get("request", {}).get("uid", "")
    req_obj = request_info.get("request", {}).get("object", {})
    metadata = req_obj.get("metadata", {})
    namespace = metadata.get("namespace", "")

    if not is_target_namespace(namespace):
        return jsonify({"apiVersion": "admission.k8s.io/v1", "kind": "AdmissionReview", "response": {"uid": uid, "allowed": True}})

    limit = get_limit_from_crd()
    running_cnt, _ = get_queue_status_from_cache()

    if running_cnt >= limit:
        pr_name = metadata.get('name') or metadata.get('generateName', 'unknown') + "(gen)"
        log(f"[Webhook 차단] {namespace}/{pr_name} ➔ 쿼터 초과(Running:{running_cnt} >= Limit:{limit}). 대기열로 보냅니다.")
        
        patch = [
            {"op": "add", "path": "/spec/status", "value": "PipelineRunPending"}
        ]
        
        if "labels" not in metadata:
            patch.append({"op": "add", "path": "/metadata/labels", "value": {MANAGED_LABEL_KEY: MANAGED_LABEL_VAL}})
        else:
            safe_label_key = MANAGED_LABEL_KEY.replace("/", "~1")
            patch.append({"op": "add", "path": f"/metadata/labels/{safe_label_key}", "value": MANAGED_LABEL_VAL})

        patch_b64 = base64.b64encode(json.dumps(patch).encode('utf-8')).decode('utf-8')
        return jsonify({
            "apiVersion": "admission.k8s.io/v1",
            "kind": "AdmissionReview",
            "response": {
                "uid": uid,
                "allowed": True,
                "patchType": "JSONPatch",
                "patch": patch_b64
            }
        })

    return jsonify({"apiVersion": "admission.k8s.io/v1", "kind": "AdmissionReview", "response": {"uid": uid, "allowed": True}})

# ---------------------------------------------------------
# [Manager & Watcher 루프]
# ---------------------------------------------------------
def manager_loop():
    log("[Manager] 스레드 시작 (스케줄링 주기: 5초)")
    last_log_time = 0

    while True:
        try:
            limit = get_limit_from_crd()
            running, pending = get_queue_status_from_cache()

            # [개선] 주기적 또는 대기열이 있을 때 대시보드 출력
            if len(pending) > 0 or abs(time.time() - last_log_time) > 60:
                print_dashboard(limit, running, pending)
                last_log_time = time.time()

            # 빈자리가 생기면 대기열에서 꺼내어 실행
            if running < limit and pending:
                slots = limit - running
                to_run = pending[:slots]

                for target in to_run:
                    t_name = target['metadata']['name']
                    t_ns = target['metadata']['namespace']
                    
                    try:
                        api.patch_namespaced_custom_object(
                            'tekton.dev', 'v1', t_ns, 'pipelineruns', t_name, {'spec': {'status': None}}
                        )
                        log(f"[스케줄링 완료] {t_ns}/{t_name} ➔ 대기열에서 빠져나와 실행을 시작합니다.")
                        running += 1
                        slots -= 1
                        
                        # 캐시 선제적 갱신
                        with cache_lock:
                            key = f"{t_ns}/{t_name}"
                            if key in local_cache:
                                local_cache[key]['spec']['status'] = None
                                
                    except Exception as e:
                        log(f"[에러] 실행 패치 실패 ({t_ns}/{t_name}): {e}")
        except Exception as e:
            log(f"[에러] Manager 루프 에러: {e}")
        time.sleep(5)

def watcher_loop():
    log("[Watcher] 스레드 시작 (Informer 동기화)")
    resource_version = None
    while True:
        try:
            if resource_version is None:
                log("클러스터 파이프라인 상태 전체 동기화 중...")
                raw_resp = api.list_cluster_custom_object('tekton.dev', 'v1', 'pipelineruns', _preload_content=False)
                data = json.loads(raw_resp.data)
                resource_version = data['metadata']['resourceVersion']
                with cache_lock:
                    local_cache.clear()
                    for item in data.get('items', []):
                        local_cache[f"{item['metadata']['namespace']}/{item['metadata']['name']}"] = item
                log(f"동기화 완료 (현재 추적 중인 리소스: {len(local_cache)}개)")

            w = watch.Watch()
            stream = w.stream(
                api.list_cluster_custom_object, 'tekton.dev', 'v1', 'pipelineruns',
                resource_version=resource_version, timeout_seconds=0
            )
            for event in stream:
                obj = event['object']
                etype = event['type']
                resource_version = obj['metadata']['resourceVersion']
                update_cache(etype, obj)
        except Exception as e:
            log(f"[Watcher] 스트림 끊김 재연결 시도 중... ({e})")
            resource_version = None
            time.sleep(2)

if __name__ == "__main__":
    log("Tekton Queue Controller 기동 준비 중...")
    threading.Thread(target=manager_loop, daemon=True).start()
    threading.Thread(target=watcher_loop, daemon=True).start()
    
    app.run(host='0.0.0.0', port=8443, ssl_context=('/certs/tls.crt', '/certs/tls.key'))
