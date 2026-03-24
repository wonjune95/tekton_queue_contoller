import time
import threading
import fnmatch
import json
import base64
import datetime
import logging
from flask import Flask, request, jsonify
from kubernetes import client, config, watch
from kubernetes.client.rest import ApiException

# =========================================================
# [설정 - 기본값]
# CRD에서 동적으로 읽어오며, CRD 조회 실패 시 아래 기본값을 사용한다.
# =========================================================
NAMESPACE_PATTERN = "*-cicd"
DEFAULT_LIMIT = 10
DEFAULT_AGING_INTERVAL_SEC = 180
DEFAULT_AGING_MIN_TIER = 1
DEFAULT_TIER = 3
DEFAULT_TIER_RULES = [
    {"tier": 0, "matchType": "label", "labelKey": "queue.tekton.dev/urgent", "pattern": "true", "description": "긴급 배포 (수동 실행)"},
    {"tier": 1, "matchType": "env", "pattern": "prd", "description": "운영 배포"},
    {"tier": 2, "matchType": "env", "pattern": "stg", "description": "검증 배포"},
    {"tier": 3, "matchType": "env", "pattern": "*",   "description": "개발 (기본값)"},
]

MANAGED_LABEL_KEY = "queue.tekton.dev/managed"
MANAGED_LABEL_VAL = "yes"
TIER_LABEL_KEY = "queue.tekton.dev/tier"
ENV_LABEL_KEY = "env"

# ---------------------------------------------------------
# [취소/중지 상태 정의]
# ---------------------------------------------------------
CANCEL_STATUSES = frozenset({
    'Cancelled',
    'CancelledRunFinally',
    'StoppedRunFinally',
})

local_cache = {}
cache_lock = threading.Lock()

# ---------------------------------------------------------
# [CRD 설정 캐시]
# ---------------------------------------------------------
crd_config = {
    "max_pipelines": DEFAULT_LIMIT,
    "aging_interval_sec": DEFAULT_AGING_INTERVAL_SEC,
    "aging_min_tier": DEFAULT_AGING_MIN_TIER,
    "tier_rules": DEFAULT_TIER_RULES,
}
crd_config_lock = threading.Lock()

# ---------------------------------------------------------
# [Race Condition 방어] Webhook Admission Counter
# ---------------------------------------------------------
webhook_admitted_count = 0
admitted_lock = threading.Lock()

# ---------------------------------------------------------
# [초기 동기화 플래그]
# Watcher의 최초 list 동기화가 완료되기 전까지 Webhook 트래픽을 차단한다.
# 불완전한 캐시로 인한 쿼터 초과 통과를 방지한다.
# ---------------------------------------------------------
initial_sync_done = False

try:
    config.load_incluster_config()
except config.ConfigException:
    config.load_kube_config()

api = client.CustomObjectsApi()
app = Flask(__name__)

logging.getLogger('werkzeug').setLevel(logging.ERROR)

# ---------------------------------------------------------
# [유틸리티 및 로깅 로직]
# ---------------------------------------------------------
def log(msg):
    now = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{now}] {msg}"
    try:
        print(line, flush=True)
    except UnicodeEncodeError:
        print(line.encode('utf-8', errors='replace').decode('utf-8'), flush=True)

def parse_k8s_timestamp(ts_str):
    if not ts_str:
        return datetime.datetime.min.replace(tzinfo=datetime.timezone.utc)
    try:
        return datetime.datetime.strptime(ts_str, "%Y-%m-%dT%H:%M:%SZ").replace(
            tzinfo=datetime.timezone.utc
        )
    except ValueError:
        return datetime.datetime.min.replace(tzinfo=datetime.timezone.utc)

def is_pipelinerun_finished(item):
    """PipelineRun 완료 판별.
    1순위: status.completionTime 존재
    2순위: Succeeded condition status가 True 또는 False
    """
    status = item.get('status', {})
    if status.get('completionTime'):
        return True
    conditions = status.get('conditions', [])
    for c in conditions:
        if c.get('type') == 'Succeeded':
            return c.get('status') in ('True', 'False')
    return False

# ---------------------------------------------------------
# [CRD 조회 및 설정 로딩]
# ---------------------------------------------------------
def load_crd_config():
    try:
        obj = api.get_cluster_custom_object(
            'tekton.devops', 'v1', 'globallimits', 'tekton-queue-limit'
        )
        spec = obj.get('spec', {})
        new_config = {
            "max_pipelines": int(spec.get('maxPipelines', DEFAULT_LIMIT)),
            "aging_interval_sec": int(spec.get('agingIntervalSec', DEFAULT_AGING_INTERVAL_SEC)),
            "aging_min_tier": int(spec.get('agingMinTier', DEFAULT_AGING_MIN_TIER)),
            "tier_rules": spec.get('tierRules', DEFAULT_TIER_RULES),
        }
        with crd_config_lock:
            crd_config.update(new_config)
        return new_config["max_pipelines"]
    except ApiException as e:
        log(f"[경고] GlobalLimit CRD 조회 실패 (API 에러 {e.status}): {e.reason}. 기본값 사용.")
        return DEFAULT_LIMIT
    except Exception as e:
        log(f"[경고] GlobalLimit CRD 조회 실패: {e}. 기본값 사용.")
        return DEFAULT_LIMIT

def get_cached_config():
    with crd_config_lock:
        return dict(crd_config)

# ---------------------------------------------------------
# [티어 자동 분류]
# 2단계 매칭: 1순위 label 매칭, 2순위 env 라벨 매칭
#
# matchType별 동작:
#   label: metadata.labels에서 labelKey의 값을 꺼내 pattern과 매칭
#   env:   metadata.labels에서 env의 값을 꺼내 pattern과 매칭
# ---------------------------------------------------------
def determine_tier(labels, tier_rules):
    """PipelineRun의 labels를 tierRules와 순서대로 매칭하여 티어를 결정한다.

    tierRules 순회 시 먼저 매칭되는 규칙이 적용된다.
    매칭되는 규칙이 없으면 DEFAULT_TIER를 반환한다.
    """
    for rule in tier_rules:
        match_type = rule.get('matchType', 'env')
        pattern = rule.get('pattern', '')

        if match_type == 'label':
            label_key = rule.get('labelKey', '')
            label_val = labels.get(label_key, '')
            if label_val and fnmatch.fnmatch(label_val, pattern):
                return int(rule.get('tier', DEFAULT_TIER))

        elif match_type == 'env':
            env_val = labels.get(ENV_LABEL_KEY, '')
            if env_val and fnmatch.fnmatch(env_val, pattern):
                return int(rule.get('tier', DEFAULT_TIER))

    return DEFAULT_TIER

# ---------------------------------------------------------
# [대시보드 출력]
# ---------------------------------------------------------
def print_dashboard(limit, running_cnt, pending_list, cfg):
    bar_length = 20
    filled_length = int(bar_length * running_cnt // limit) if limit > 0 else 0
    filled_length = min(filled_length, bar_length)
    bar = '█' * filled_length + '-' * (bar_length - filled_length)

    aging_interval = cfg["aging_interval_sec"]
    aging_min = cfg["aging_min_tier"]

    log("=" * 60)
    log(f"[스케줄링 현황] Limit: {limit} | Aging: {aging_interval}s | MinTier: {aging_min}")
    log(f"실행 중 (Running) : {running_cnt:2d} / {limit:2d} |{bar}|")
    log(f"대기 중 (Pending) : {len(pending_list):2d} 개")

    if len(pending_list) > 0:
        now_utc = datetime.datetime.now(datetime.timezone.utc)
        log("-" * 60)
        log("   [대기열 순번 Top 5 (Priority & FIFO + Aging)]")
        for idx, item in enumerate(pending_list[:5]):
            ns = item['metadata']['namespace']
            name = item['metadata'].get('name') or item['metadata'].get('generateName', '') + "(gen)"
            labels = item['metadata'].get('labels', {})

            original_tier = labels.get(TIER_LABEL_KEY, str(DEFAULT_TIER))
            creation_ts = item['metadata'].get('creationTimestamp', '')
            created_at = parse_k8s_timestamp(creation_ts)
            wait_secs = (now_utc - created_at).total_seconds()
            aging_bonus = int(wait_secs // aging_interval)
            effective_tier = max(aging_min, int(original_tier) - aging_bonus)

            wait_display = f"{int(wait_secs)}s" if wait_secs < 120 else f"{int(wait_secs//60)}m"
            ptype = labels.get('type', '?')
            env_val = labels.get(ENV_LABEL_KEY, '?')

            log(f"   {idx+1}. [Tier {original_tier}->{effective_tier}] "
                f"{ns}/{name} ({ptype}/{env_val}, 대기: {wait_display})")
    log("=" * 60)

def is_target_namespace(namespace):
    return fnmatch.fnmatch(namespace, NAMESPACE_PATTERN)

# ---------------------------------------------------------
# [큐 상태 조회]
# ---------------------------------------------------------
def get_queue_status_from_cache():
    cfg = get_cached_config()
    aging_interval = cfg["aging_interval_sec"]
    aging_min = cfg["aging_min_tier"]

    running_cnt = 0
    managed_pending_list = []
    with cache_lock:
        for key, item in local_cache.items():
            ns = item['metadata']['namespace']
            if not is_target_namespace(ns):
                continue
            if is_pipelinerun_finished(item):
                continue
            spec_status = item.get('spec', {}).get('status')
            if spec_status in CANCEL_STATUSES:
                continue
            if spec_status != 'PipelineRunPending':
                running_cnt += 1
            else:
                labels = item['metadata'].get('labels', {})
                if labels.get(MANAGED_LABEL_KEY) == MANAGED_LABEL_VAL:
                    managed_pending_list.append(item)

    now_utc = datetime.datetime.now(datetime.timezone.utc)

    def get_priority_and_time(item):
        labels = item['metadata'].get('labels', {})
        tier_str = labels.get(TIER_LABEL_KEY, str(DEFAULT_TIER))
        try:
            tier = int(tier_str)
        except ValueError:
            tier = DEFAULT_TIER
        creation_ts = item['metadata'].get('creationTimestamp', '')
        created_at = parse_k8s_timestamp(creation_ts)
        wait_seconds = (now_utc - created_at).total_seconds()
        aging_bonus = int(wait_seconds // aging_interval)
        effective_tier = max(aging_min, tier - aging_bonus)
        return (effective_tier, creation_ts)

    managed_pending_list.sort(key=get_priority_and_time)
    return running_cnt, managed_pending_list

# ---------------------------------------------------------
# [캐시 갱신]
# ---------------------------------------------------------
def update_cache(event_type, obj):
    global webhook_admitted_count
    ns = obj['metadata']['namespace']
    name = obj['metadata'].get('name', 'unknown')
    key = f"{ns}/{name}"

    with cache_lock:
        is_new_addition = key not in local_cache
        if event_type == 'DELETED' and key in local_cache:
            del local_cache[key]
        elif event_type != 'DELETED':
            local_cache[key] = obj

    if is_new_addition and event_type in ('ADDED', 'MODIFIED'):
        if is_target_namespace(ns):
            spec_status = obj.get('spec', {}).get('status')
            if spec_status != 'PipelineRunPending':
                with admitted_lock:
                    webhook_admitted_count = max(0, webhook_admitted_count - 1)

# ---------------------------------------------------------
# [Health Check]
# ---------------------------------------------------------
@app.route('/healthz', methods=['GET'])
def healthz():
    return jsonify({"status": "ok"}), 200

@app.route('/readyz', methods=['GET'])
def readyz():
    if not initial_sync_done:
        return jsonify({"status": "not_ready", "reason": "initial sync not complete"}), 503
    with cache_lock:
        cache_size = len(local_cache)
    return jsonify({"status": "ready", "cached_resources": cache_size}), 200

# ---------------------------------------------------------
# [Webhook 통제 로직]
# ---------------------------------------------------------
@app.route('/mutate', methods=['POST'])
def mutate_pipelinerun():
    global webhook_admitted_count
    request_info = request.get_json()
    uid = request_info.get("request", {}).get("uid", "")
    req_obj = request_info.get("request", {}).get("object", {})
    metadata = req_obj.get("metadata", {})
    namespace = metadata.get("namespace", "")
    labels = metadata.get("labels", {})

    if not is_target_namespace(namespace):
        return jsonify({
            "apiVersion": "admission.k8s.io/v1",
            "kind": "AdmissionReview",
            "response": {"uid": uid, "allowed": True}
        })

    # ---------------------------------------------------------
    # [티어 자동 분류]
    # 1순위: urgent 라벨 (수동 긴급 실행)
    # 2순위: env 라벨 (환경별 분류)
    # ---------------------------------------------------------
    cfg = get_cached_config()
    tier_val = determine_tier(labels, cfg["tier_rules"])

    limit = cfg["max_pipelines"]
    running_cnt, _ = get_queue_status_from_cache()

    with admitted_lock:
        effective_running = running_cnt + webhook_admitted_count

    # 패치 준비
    tier_label_escaped = TIER_LABEL_KEY.replace("/", "~1")
    managed_label_escaped = MANAGED_LABEL_KEY.replace("/", "~1")
    pr_name = metadata.get('name') or metadata.get('generateName', 'unknown') + "(gen)"
    ptype = labels.get('type', '?')
    env_val = labels.get(ENV_LABEL_KEY, '?')
    is_urgent = labels.get('queue.tekton.dev/urgent', '') == 'true'
    match_info = "urgent" if is_urgent else f"env:{env_val}"

    if effective_running >= limit:
        # [대기열 전환]
        log(f"[Webhook 차단] {namespace}/{pr_name} ({ptype}/{match_info}, Tier {tier_val}) "
            f"-> 쿼터 초과(Running:{effective_running} >= Limit:{limit}). 대기열로 보냅니다.")

        patch = [
            {"op": "add", "path": "/spec/status", "value": "PipelineRunPending"}
        ]
        if "labels" not in metadata:
            patch.append({
                "op": "add", "path": "/metadata/labels",
                "value": {
                    TIER_LABEL_KEY: str(tier_val),
                    MANAGED_LABEL_KEY: MANAGED_LABEL_VAL,
                }
            })
        else:
            patch.append({
                "op": "add",
                "path": f"/metadata/labels/{tier_label_escaped}",
                "value": str(tier_val)
            })
            patch.append({
                "op": "add",
                "path": f"/metadata/labels/{managed_label_escaped}",
                "value": MANAGED_LABEL_VAL
            })

        patch_b64 = base64.b64encode(json.dumps(patch).encode('utf-8')).decode('utf-8')
        return jsonify({
            "apiVersion": "admission.k8s.io/v1",
            "kind": "AdmissionReview",
            "response": {
                "uid": uid, "allowed": True,
                "patchType": "JSONPatch", "patch": patch_b64
            }
        })

    # [즉시 실행]
    with admitted_lock:
        webhook_admitted_count += 1

    log(f"[Webhook 통과] {namespace}/{pr_name} ({ptype}/{match_info}, Tier {tier_val}) "
        f"-> 즉시 실행 허용 (Running:{effective_running+1}/{limit})")

    # 통과 시에도 티어 라벨 부여
    patch = []
    if "labels" not in metadata:
        patch.append({
            "op": "add", "path": "/metadata/labels",
            "value": {TIER_LABEL_KEY: str(tier_val)}
        })
    else:
        patch.append({
            "op": "add",
            "path": f"/metadata/labels/{tier_label_escaped}",
            "value": str(tier_val)
        })

    patch_b64 = base64.b64encode(json.dumps(patch).encode('utf-8')).decode('utf-8')
    return jsonify({
        "apiVersion": "admission.k8s.io/v1",
        "kind": "AdmissionReview",
        "response": {
            "uid": uid, "allowed": True,
            "patchType": "JSONPatch", "patch": patch_b64
        }
    })

# ---------------------------------------------------------
# [Manager & Watcher 루프]
# ---------------------------------------------------------
def manager_loop():
    log("[Manager] 스레드 시작 (스케줄링 주기: 5초)")
    last_log_time = 0

    while True:
        try:
            limit = load_crd_config()
            cfg = get_cached_config()
            running, pending = get_queue_status_from_cache()

            if len(pending) > 0 or abs(time.time() - last_log_time) > 60:
                print_dashboard(limit, running, pending, cfg)
                last_log_time = time.time()

            if running < limit and pending:
                slots = limit - running
                to_run = pending[:slots]

                for target in to_run:
                    t_name = target['metadata']['name']
                    t_ns = target['metadata']['namespace']
                    t_labels = target['metadata'].get('labels', {})
                    tier_val = t_labels.get(TIER_LABEL_KEY, str(DEFAULT_TIER))
                    ptype = t_labels.get('type', '?')
                    env_val = t_labels.get(ENV_LABEL_KEY, '?')
                    creation_ts = target['metadata'].get('creationTimestamp', '')
                    created_at = parse_k8s_timestamp(creation_ts)
                    now_utc = datetime.datetime.now(datetime.timezone.utc)
                    wait_secs = (now_utc - created_at).total_seconds()

                    try:
                        api.patch_namespaced_custom_object(
                            'tekton.dev', 'v1', t_ns, 'pipelineruns', t_name,
                            {'spec': {'status': None}}
                        )
                        log(f"[스케줄링 완료] {t_ns}/{t_name} ({ptype}/{env_val}, "
                            f"Tier {tier_val}, 대기시간: {int(wait_secs)}s) -> 실행 시작")
                        running += 1
                        slots -= 1

                        with cache_lock:
                            key = f"{t_ns}/{t_name}"
                            if key in local_cache:
                                local_cache[key]['spec']['status'] = None

                    except ApiException as e:
                        log(f"[에러] 실행 패치 실패 ({t_ns}/{t_name}): "
                            f"API 에러 {e.status} - {e.reason}")
                    except Exception as e:
                        log(f"[에러] 실행 패치 실패 ({t_ns}/{t_name}): {e}")
        except Exception as e:
            log(f"[에러] Manager 루프 에러: {e}")
        time.sleep(5)

def watcher_loop():
    global webhook_admitted_count, initial_sync_done
    log("[Watcher] 스레드 시작 (Informer 동기화)")
    resource_version = None
    while True:
        try:
            if resource_version is None:
                log("클러스터 파이프라인 상태 전체 동기화 중...")
                raw_resp = api.list_cluster_custom_object(
                    'tekton.dev', 'v1', 'pipelineruns', _preload_content=False
                )
                data = json.loads(raw_resp.data)
                resource_version = data['metadata']['resourceVersion']

                new_cache = {}
                for item in data.get('items', []):
                    key = f"{item['metadata']['namespace']}/{item['metadata']['name']}"
                    new_cache[key] = item

                with cache_lock:
                    local_cache.clear()
                    local_cache.update(new_cache)
                with admitted_lock:
                    webhook_admitted_count = 0

                # 초기 동기화 완료 → Webhook 트래픽 수신 허용
                if not initial_sync_done:
                    initial_sync_done = True
                    log("초기 동기화 완료. Webhook 트래픽 수신을 시작합니다.")

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
        except ApiException as e:
            if e.status == 410:
                log("[Watcher] resourceVersion 만료 (410 Gone). 전체 재동기화를 수행합니다.")
                resource_version = None
            else:
                log(f"[Watcher] API 에러 ({e.status}): {e.reason}. "
                    f"기존 위치에서 재연결 시도 중...")
            time.sleep(2)
        except Exception as e:
            log(f"[Watcher] 스트림 끊김, 재연결 시도 중... ({e})")
            resource_version = None
            time.sleep(2)

# ---------------------------------------------------------
# [기동]
# ---------------------------------------------------------
if __name__ == "__main__":
    log("Tekton Queue Controller 기동 준비 중...")
    log(f"  네임스페이스 패턴: {NAMESPACE_PATTERN}")

    initial_limit = load_crd_config()
    cfg = get_cached_config()
    log(f"  Limit: {cfg['max_pipelines']}")
    log(f"  Aging: {cfg['aging_interval_sec']}초당 Tier 1 승격 (최소 Tier {cfg['aging_min_tier']})")
    log(f"  Tier Rules:")
    for rule in cfg['tier_rules']:
        mt = rule.get('matchType', 'env')
        extra = f", labelKey: {rule['labelKey']}" if mt == 'label' else ""
        log(f"    Tier {rule['tier']} [{mt}{extra}] "
            f"{rule['pattern']} ({rule.get('description', '')})")
    log(f"  취소 상태 목록: {sorted(CANCEL_STATUSES)}")

    threading.Thread(target=manager_loop, daemon=True).start()
    threading.Thread(target=watcher_loop, daemon=True).start()

    app.run(host='0.0.0.0', port=8443,
            ssl_context=('/certs/tls.crt', '/certs/tls.key'),
            threaded=True)