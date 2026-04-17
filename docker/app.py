import os
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
from prometheus_client import Gauge, Counter, start_http_server

# =========================================================
# [설정 - 기본값]
# CRD에서 동적으로 읽어오며, CRD 조회 실패 시 아래 기본값을 사용한다.
# =========================================================
# 네임스페이스 패턴: CRD spec.namespacePatterns에서 설정
# CRD 미설정 또는 조회 실패 시 기본값 ["*-cicd"] 사용
DEFAULT_NAMESPACE_PATTERNS = ["*-cicd"]
DEFAULT_LIMIT = 10
DEFAULT_AGING_INTERVAL_SEC = 180
DEFAULT_AGING_MIN_TIER = 1
DEFAULT_TIER = 3
DEFAULT_TIER_RULES = [
    {"tier": 0, "matchType": "label", "labelKey": "queue.tekton.dev/urgent", "pattern": "true", "description": "긴급 배포 (수동 실행)"},
    {"tier": 1, "matchType": "env", "pattern": "prod", "description": "운영 배포"},
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
    "namespace_patterns": list(DEFAULT_NAMESPACE_PATTERNS),
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

# ---------------------------------------------------------
# [Leader Election]
# HA 구성에서 Manager 루프는 Leader Pod에서만 실행된다.
# Webhook과 Watcher는 모든 Pod에서 독립적으로 동작한다.
# ---------------------------------------------------------
LEASE_NAME = os.environ.get("LEASE_NAME", "tekton-queue-controller-leader")
LEASE_NAMESPACE = os.environ.get("POD_NAMESPACE", "tekton-pipelines")
POD_NAME = os.environ.get("POD_NAME", f"controller-{os.getpid()}")
LEASE_DURATION_SEC = 15
LEASE_RENEW_DEADLINE_SEC = 10
LEASE_RETRY_PERIOD_SEC = 2

is_leader = False
leader_lock = threading.Lock()

# ---------------------------------------------------------
# [Prometheus Metrics]
# ---------------------------------------------------------
METRIC_QUEUE_LIMIT = Gauge('tekton_queue_limit', '최대 동시 실행 파이프라인 수')
METRIC_QUEUE_RUNNING = Gauge('tekton_queue_running_total', '현재 실행 중인 파이프라인 수')
METRIC_QUEUE_PENDING = Gauge('tekton_queue_pending_total', '대기열에 있는 파이프라인 수', ['tier'])
METRIC_WEBHOOK_ADMITTED = Counter('tekton_queue_webhook_admitted_total', 'Webhook을 통해 즉시 실행이 허용된 횟수', ['tier'])
METRIC_WEBHOOK_QUEUED = Counter('tekton_queue_webhook_queued_total', 'Webhook을 통해 대기열로 보내진 횟수', ['tier'])
METRIC_SCHEDULED = Counter('tekton_queue_scheduled_total', 'Manager 루프에 의해 실행 상태로 스케줄링된 횟수', ['tier'])
METRIC_API_ERRORS = Counter('tekton_queue_kubernetes_api_errors_total', 'Kubernetes API 에러 횟수', ['operation'])

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

        # 네임스페이스 패턴: CRD에서 설정 (미설정 시 기본값 사용)
        ns_patterns = spec.get('namespacePatterns')
        if ns_patterns and isinstance(ns_patterns, list) and len(ns_patterns) > 0:
            resolved_patterns = ns_patterns
        else:
            resolved_patterns = list(DEFAULT_NAMESPACE_PATTERNS)

        new_config = {
            "max_pipelines": int(spec.get('maxPipelines', DEFAULT_LIMIT)),
            "aging_interval_sec": int(spec.get('agingIntervalSec', DEFAULT_AGING_INTERVAL_SEC)),
            "aging_min_tier": int(spec.get('agingMinTier', DEFAULT_AGING_MIN_TIER)),
            "tier_rules": spec.get('tierRules') or DEFAULT_TIER_RULES,
            "namespace_patterns": resolved_patterns,
        }
        with crd_config_lock:
            crd_config.update(new_config)
        return new_config["max_pipelines"]
    except ApiException as e:
        METRIC_API_ERRORS.labels(operation='get_crd').inc()
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
            # Medium 3 Fix: env 라벨 없는 PR에서 `pattern: "*"` 와일드카드가 매칭 안 되는 문제.
            # 기존: `if env_val and ...` → env 없으면 catch-all 규칙도 건너뜀
            # 수정: fnmatch에 위임 (fnmatch('', '*') == True 이므로 의도대로 동작)
            env_val = labels.get(ENV_LABEL_KEY, '')
            if fnmatch.fnmatch(env_val, pattern):
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
            labels = item['metadata'].get('labels') or {}

            original_tier = labels.get(TIER_LABEL_KEY, str(DEFAULT_TIER))
            creation_ts = item['metadata'].get('creationTimestamp', '')
            created_at = parse_k8s_timestamp(creation_ts)
            wait_secs = (now_utc - created_at).total_seconds()
            aging_bonus = int(wait_secs // aging_interval)
            wait_display = f"{int(wait_secs)}s" if wait_secs < 120 else f"{int(wait_secs//60)}m"
            ptype = labels.get('type', '?')
            env_val = labels.get(ENV_LABEL_KEY, '?')

            # Medium 2 Fix: tier 라벨 값이 오염된 경우 int() 변환 실패 방지
            # High Fix: tier-0 urgent PR이 aging_min 바닥값으로 강등되지 않도록 min(tier,...) 적용
            try:
                tier_int = int(original_tier)
                effective_tier = min(tier_int, max(aging_min, tier_int - aging_bonus))
            except ValueError:
                effective_tier = aging_min

            log(f"   {idx+1}. [Tier {original_tier}->{effective_tier}] "
                f"{ns}/{name} ({ptype}/{env_val}, 대기: {wait_display})")
    log("=" * 60)

def is_target_namespace(namespace):
    """주어진 네임스페이스가 관리 대상인지 패턴 매칭으로 확인한다.
    CRD 또는 환경변수에서 설정된 복수 패턴 중 하나라도 매칭되면 True.
    """
    cfg = get_cached_config()
    patterns = cfg.get("namespace_patterns", DEFAULT_NAMESPACE_PATTERNS)
    return any(fnmatch.fnmatch(namespace, p) for p in patterns)

# ---------------------------------------------------------
# [큐 상태 조회]
# ---------------------------------------------------------
def get_queue_status_from_cache():
    # Bug 4 Fix: cache_lock 밖에서 config를 미리 읽어
    # cache_lock 내부에서 crd_config_lock 획득을 방지한다 (락 중첩 → 데드락 위험 제거)
    cfg = get_cached_config()
    aging_interval = cfg["aging_interval_sec"]
    aging_min = cfg["aging_min_tier"]
    ns_patterns = cfg.get("namespace_patterns", DEFAULT_NAMESPACE_PATTERNS)

    running_cnt = 0
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

    def get_priority_and_time(item):
        labels = item['metadata'].get('labels') or {}
        tier_str = labels.get(TIER_LABEL_KEY, str(DEFAULT_TIER))
        try:
            tier = int(tier_str)
        except ValueError:
            tier = DEFAULT_TIER
        creation_ts = item['metadata'].get('creationTimestamp', '')
        created_at = parse_k8s_timestamp(creation_ts)
        wait_seconds = (now_utc - created_at).total_seconds()
        aging_bonus = int(wait_seconds // aging_interval)
        # High Fix: tier가 이미 aging_min보다 낮은 경우(예: tier-0 urgent) aging_min 바닥값으로
        # 강등되지 않아야 한다. min(tier, ...) 로 자신의 원래 tier보다 높아지지 않도록 보장.
        # 예) tier=0, aging_min=1: min(0, max(1,-N)) = 0 (긴급 우선순위 유지)
        # 예) tier=3, aging_min=1: min(3, max(1,-2)) = 1 (aging 정상 동작)
        effective_tier = min(tier, max(aging_min, tier - aging_bonus))
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
        existing = local_cache.get(key)
        # Critical 1 Fix: phantom entry(resourceVersion='__admitted__')가 실제 K8s 이벤트로
        # 교체되는 경우에도 webhook_admitted_count를 감소시켜야 한다.
        # 기존: is_new_addition=False (phantom 있음) → 카운터 감소 안 됨 → 영구 누적
        # 수정: phantom 교체 여부를 별도 플래그로 추적
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
            # 실제 K8s 객체가 도착: 새 항목이거나 phantom을 교체하는 경우 카운터 감소
            spec_status = obj.get('spec', {}).get('status')
            if spec_status != 'PipelineRunPending':
                with admitted_lock:
                    webhook_admitted_count = max(0, webhook_admitted_count - 1)
        elif event_type == 'DELETED' and is_phantom_replacement:
            # High Fix: DELETED phantom 미감소 수정
            # phantom이 ADDED 없이 DELETED로 사라지는 경우 (PR 생성 실패/즉시 삭제)
            # admitted_count는 여전히 1이 남아 있으므로 감소 필요
            with admitted_lock:
                webhook_admitted_count = max(0, webhook_admitted_count - 1)

# ---------------------------------------------------------
# [Health Check & Metrics]
# ---------------------------------------------------------
@app.route('/healthz', methods=['GET'])
def healthz():
    with leader_lock:
        leader_status = is_leader
    return jsonify({
        "status": "ok",
        "leader": leader_status,
        "pod": POD_NAME,
    }), 200

@app.route('/readyz', methods=['GET'])
def readyz():
    if not initial_sync_done:
        return jsonify({"status": "not_ready", "reason": "initial sync not complete"}), 503
    with cache_lock:
        cache_size = len(local_cache)
    with leader_lock:
        leader_status = is_leader
    return jsonify({
        "status": "ready",
        "cached_resources": cache_size,
        "leader": leader_status,
        "pod": POD_NAME,
    }), 200


# ---------------------------------------------------------
# [Webhook 통제 로직]
# ---------------------------------------------------------
@app.route('/mutate', methods=['POST'])
def mutate_pipelinerun():
    global webhook_admitted_count
    request_info = request.get_json()
    # High Fix: request_info / metadata null 방어
    # Content-Type 오류나 파싱 실패 시 None이 반환되어 AttributeError 발생 방지
    if not request_info:
        log("[경고] Webhook 요청 파싱 실패. 통과 처리.")
        return jsonify({
            "apiVersion": "admission.k8s.io/v1",
            "kind": "AdmissionReview",
            "response": {"uid": "", "allowed": True}
        })
    uid = request_info.get("request", {}).get("uid", "")
    req_obj = request_info.get("request", {}).get("object", {})
    metadata = req_obj.get("metadata") or {}
    namespace = metadata.get("namespace", "")
    # Critical 3 Fix: metadata.labels가 명시적 null로 넘어오는 경우 AttributeError 방지
    labels = metadata.get("labels") or {}

    # Bug 3 Fix: 초기 캐시 동기화가 완료되지 않은 상태에서 요청이 들어오면
    # running_cnt = 0으로 보여 모든 PR이 즉시 통과되는 문제를 방지한다.
    # (readyz probe가 503을 반환하지만, 방어적 이중 체크)
    if not initial_sync_done:
        log(f"[경고] 캐시 미동기화 상태에서 Webhook 요청 수신 ({namespace}). 통과 처리.")
        return jsonify({
            "apiVersion": "admission.k8s.io/v1",
            "kind": "AdmissionReview",
            "response": {"uid": uid, "allowed": True}
        })

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

    # 패치 준비
    tier_label_escaped = TIER_LABEL_KEY.replace("/", "~1")
    managed_label_escaped = MANAGED_LABEL_KEY.replace("/", "~1")
    pr_name = metadata.get('name') or metadata.get('generateName', 'unknown') + "(gen)"
    ptype = labels.get('type', '?')
    env_val = labels.get(ENV_LABEL_KEY, '?')
    is_urgent = labels.get('queue.tekton.dev/urgent', '') == 'true'
    match_info = "urgent" if is_urgent else f"env:{env_val}"

    # Bug 1 Fix: TOCTOU Race Condition 수정
    # 기존: 쿼터 체크(read)와 카운터 증가(write)가 별도 락 블록으로 분리되어
    #        Flask threaded=True 환경에서 동시 요청이 모두 체크를 통과할 수 있었음.
    # 수정: 단일 admitted_lock 블록 안에서 체크와 증가를 원자적으로 처리.
    with admitted_lock:
        effective_running = running_cnt + webhook_admitted_count
        if effective_running < limit:
            webhook_admitted_count += 1
            should_admit = True
        else:
            should_admit = False

    if not should_admit:
        METRIC_WEBHOOK_QUEUED.labels(tier=str(tier_val)).inc()
        # [대기열 전환]
        log(f"[Webhook 차단] {namespace}/{pr_name} ({ptype}/{match_info}, Tier {tier_val}) "
            f"-> 쿼터 초과(Running:{effective_running} >= Limit:{limit}). 대기열로 보냅니다.")

        patch = [
            {"op": "add", "path": "/spec/status", "value": "PipelineRunPending"}
        ]
        # Critical Fix: "labels" 키가 없거나 null인 경우 모두 labels 맵을 새로 생성해야 한다.
        # 기존: "labels" not in metadata → 키가 있지만 null인 경우 else로 진입해
        #       null 하위 경로에 add → K8s Patch 실패
        if not metadata.get("labels"):
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
    # Critical 2 Fix: generateName PR은 Webhook 시점에 실제 이름을 알 수 없어
    # phantom entry의 key가 Watcher 이벤트 key와 달라 캐시에 영구 잔존하는 문제 방지.
    # name이 확정된 PR에 한해서만 phantom entry를 삽입한다.
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

    METRIC_WEBHOOK_ADMITTED.labels(tier=str(tier_val)).inc()

    log(f"[Webhook 통과] {namespace}/{pr_name} ({ptype}/{match_info}, Tier {tier_val}) "
        f"-> 즉시 실행 허용 (Running:{effective_running + 1}/{limit})")

    # 통과 시에도 티어 라벨 부여
    # Critical Fix: labels null 케이스 동일하게 처리
    patch = []
    if not metadata.get("labels"):
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
# [Leader Election 루프]
# Kubernetes Lease 리소스를 사용하여 Leader를 선출한다.
# Leader만 Manager 스케줄링 루프를 실행한다.
# ---------------------------------------------------------
def leader_election_loop():
    global is_leader
    coord_api = client.CoordinationV1Api()
    log(f"[LeaderElection] 시작 (Pod: {POD_NAME}, Lease: {LEASE_NAMESPACE}/{LEASE_NAME})")

    while True:
        # Medium Fix: 409 핸들링 개선
        # 갱신(renewal) 실패 vs 탈취(takeover) 실패를 구분한다.
        # - 갱신 409: 일시적 충돌. is_leader 유지, Lease TTL이 자연 failover를 처리.
        # - 탈취/생성 409: 다른 Pod가 먼저 획득. is_leader 해제.
        _is_renewal_attempt = False
        try:
            now = datetime.datetime.now(datetime.timezone.utc)

            # Lease 조회 시도
            try:
                lease = coord_api.read_namespaced_lease(LEASE_NAME, LEASE_NAMESPACE)
            except ApiException as e:
                if e.status == 404:
                    # Lease가 없으면 새로 생성하여 Leader 획득
                    lease_body = client.V1Lease(
                        metadata=client.V1ObjectMeta(
                            name=LEASE_NAME,
                            namespace=LEASE_NAMESPACE,
                        ),
                        spec=client.V1LeaseSpec(
                            holder_identity=POD_NAME,
                            lease_duration_seconds=LEASE_DURATION_SEC,
                            acquire_time=now,
                            renew_time=now,
                        ),
                    )
                    coord_api.create_namespaced_lease(LEASE_NAMESPACE, lease_body)
                    with leader_lock:
                        if not is_leader:
                            is_leader = True
                            log(f"[LeaderElection] Leader 획득 (신규 Lease 생성)")
                    time.sleep(LEASE_RETRY_PERIOD_SEC)
                    continue
                else:
                    raise

            # 현재 Lease 상태 확인
            holder = lease.spec.holder_identity
            renew_time = lease.spec.renew_time
            duration = lease.spec.lease_duration_seconds or LEASE_DURATION_SEC

            if holder == POD_NAME:
                # 내가 Leader → 갱신
                _is_renewal_attempt = True
                lease.spec.renew_time = now
                coord_api.replace_namespaced_lease(LEASE_NAME, LEASE_NAMESPACE, lease)
                with leader_lock:
                    if not is_leader:
                        is_leader = True
                        log(f"[LeaderElection] Leader 재획득 (갱신 성공)")
            elif renew_time is None or (now - renew_time).total_seconds() > duration:
                # 기존 Leader의 Lease가 만료됨 → 탈취 시도
                lease.spec.holder_identity = POD_NAME
                lease.spec.acquire_time = now
                lease.spec.renew_time = now
                lease.spec.lease_duration_seconds = LEASE_DURATION_SEC
                coord_api.replace_namespaced_lease(LEASE_NAME, LEASE_NAMESPACE, lease)
                with leader_lock:
                    was_leader = is_leader
                    is_leader = True
                if not was_leader:
                    # 새로 Leader가 되면 admitted count 초기화
                    with admitted_lock:
                        webhook_admitted_count = 0
                    log(f"[LeaderElection] Leader 승격 (이전 Leader: {holder}, Lease 만료)")
            else:
                # 다른 Pod가 Leader → 대기
                with leader_lock:
                    if is_leader:
                        is_leader = False
                        log(f"[LeaderElection] Leader 해제 (현재 Leader: {holder})")

        except ApiException as e:
            METRIC_API_ERRORS.labels(operation='leader_election').inc()
            if e.status == 409:
                if _is_renewal_attempt:
                    # 갱신 409: is_leader 유지. Lease TTL(15s) 내 재시도로 자연 해결.
                    # 즉시 해제하면 매 네트워크 지연마다 불필요한 스케줄링 공백 발생.
                    log(f"[LeaderElection] Lease 갱신 충돌 (409). is_leader 유지 후 재시도.")
                else:
                    # 탈취/생성 409: 다른 Pod가 먼저 Lease 획득 → Leader 해제
                    log(f"[LeaderElection] Lease 탈취 충돌 (409). Leader 상태 해제.")
                    with leader_lock:
                        if is_leader:
                            is_leader = False
            else:
                log(f"[LeaderElection] API 에러 ({e.status}): {e.reason}")
        except Exception as e:
            log(f"[LeaderElection] 에러: {e}")

        time.sleep(LEASE_RETRY_PERIOD_SEC)

# ---------------------------------------------------------
# [Manager & Watcher 루프]
# ---------------------------------------------------------
def manager_loop():
    log("[Manager] 스레드 시작 (스케줄링 주기: 5초)")
    last_log_time = 0

    while True:
        try:
            # Leader가 아니면 스케줄링을 수행하지 않는다
            with leader_lock:
                currently_leader = is_leader
            if not currently_leader:
                time.sleep(5)
                continue

            limit = load_crd_config()
            cfg = get_cached_config()
            running, pending = get_queue_status_from_cache()

            if len(pending) > 0 or abs(time.time() - last_log_time) > 60:
                print_dashboard(limit, running, pending, cfg)
                last_log_time = time.time()

            # 업데이트 매트릭스
            METRIC_QUEUE_LIMIT.set(limit)
            METRIC_QUEUE_RUNNING.set(running)

            METRIC_QUEUE_PENDING.clear()
            pending_by_tier = {}
            for target in pending:
                t_labels = target['metadata'].get('labels') or {}
                tier_val = t_labels.get(TIER_LABEL_KEY, str(DEFAULT_TIER))
                pending_by_tier[tier_val] = pending_by_tier.get(tier_val, 0) + 1
            for t_val, count in pending_by_tier.items():
                METRIC_QUEUE_PENDING.labels(tier=str(t_val)).set(count)

            # High 1 Fix: Manager-Webhook 슬롯 이중 소비 방지
            # Webhook이 이미 통과시킨 in-flight PR을 포함해 가용 슬롯을 계산한다.
            with admitted_lock:
                effective_running = running + webhook_admitted_count
            available_slots = limit - effective_running

            if available_slots > 0 and pending:
                # Medium 1 Fix: 패치 실패 시 슬롯을 다음 우선순위 PR에 양보한다.
                # 기존: to_run = pending[:slots] 후 실패해도 해당 슬롯 낭비
                # 수정: pending 순회하며 성공한 수(scheduled)로만 슬롯 카운트
                scheduled = 0
                for target in pending:
                    if scheduled >= available_slots:
                        break
                    t_name = target['metadata']['name']
                    t_ns = target['metadata']['namespace']
                    t_labels = target['metadata'].get('labels') or {}
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
                        METRIC_SCHEDULED.labels(tier=str(tier_val)).inc()
                        log(f"[스케줄링 완료] {t_ns}/{t_name} ({ptype}/{env_val}, "
                            f"Tier {tier_val}, 대기시간: {int(wait_secs)}s) -> 실행 시작")
                        running += 1
                        scheduled += 1

                        with cache_lock:
                            key = f"{t_ns}/{t_name}"
                            if key in local_cache:
                                local_cache[key]['spec']['status'] = None

                    except ApiException as e:
                        METRIC_API_ERRORS.labels(operation='patch_pipelinerun').inc()
                        log(f"[에러] 실행 패치 실패 ({t_ns}/{t_name}): "
                            f"API 에러 {e.status} - {e.reason}")
                        # 슬롯 양보: scheduled 증가 없이 다음 PR로 계속
                    except Exception as e:
                        log(f"[에러] 실행 패치 실패 ({t_ns}/{t_name}): {e}")
                        # 슬롯 양보: scheduled 증가 없이 다음 PR로 계속
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
            METRIC_API_ERRORS.labels(operation='watch_pipelinerun').inc()
            if e.status == 410:
                log("[Watcher] resourceVersion 만료 (410 Gone). 전체 재동기화를 수행합니다.")
                resource_version = None
            else:
                log(f"[Watcher] API 에러 ({e.status}): {e.reason}. "
                    f"기존 위치에서 재연결 시도 중...")
            time.sleep(2)
        except Exception as e:
            METRIC_API_ERRORS.labels(operation='watch_pipelinerun_stream').inc()
            log(f"[Watcher] 스트림 끊김, 재연결 시도 중... ({e})")
            resource_version = None
            time.sleep(2)

# ---------------------------------------------------------
# [기동]
# ---------------------------------------------------------
if __name__ == "__main__":
    log("Tekton Queue Controller 기동 준비 중...")
    log(f"  Pod: {POD_NAME}")

    initial_limit = load_crd_config()
    cfg = get_cached_config()
    log(f"  네임스페이스 패턴: {cfg['namespace_patterns']}")
    log(f"  Limit: {cfg['max_pipelines']}")
    log(f"  Aging: {cfg['aging_interval_sec']}초당 Tier 1 승격 (최소 Tier {cfg['aging_min_tier']})")
    log(f"  Tier Rules:")
    for rule in cfg['tier_rules']:
        mt = rule.get('matchType', 'env')
        extra = f", labelKey: {rule['labelKey']}" if mt == 'label' else ""
        log(f"    Tier {rule['tier']} [{mt}{extra}] "
            f"{rule['pattern']} ({rule.get('description', '')})")
    log(f"  취소 상태 목록: {sorted(CANCEL_STATUSES)}")
    log(f"  Leader Election: Lease={LEASE_NAMESPACE}/{LEASE_NAME}, "
        f"Duration={LEASE_DURATION_SEC}s, Renew={LEASE_RENEW_DEADLINE_SEC}s")

    start_http_server(9090)
    log("Prometheus metrics server started on port 9090")

    threading.Thread(target=leader_election_loop, daemon=True).start()
    threading.Thread(target=manager_loop, daemon=True).start()
    threading.Thread(target=watcher_loop, daemon=True).start()

    app.run(host='0.0.0.0', port=8443,
            ssl_context=('/certs/tls.crt', '/certs/tls.key'),
            threaded=True)