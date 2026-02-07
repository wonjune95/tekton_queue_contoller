import time
import threading
import fnmatch
import copy
import json
import datetime
from kubernetes import client, config, watch
from kubernetes.client.rest import ApiException

# =========================================================
# [설정]
NAMESPACE_PATTERN = "*-cicd"  # 관리 대상 네임스페이스
DEFAULT_LIMIT = 10            # 기본 동시 실행 제한
MANAGED_LABEL_KEY = "queue.tekton.dev/managed"
MANAGED_LABEL_VAL = "yes"
# =========================================================

# [핵심 아키텍처] API 호출을 없애기 위한 로컬 캐시 저장소
# Key: "{namespace}/{name}", Value: PipelineRun Object (Dict)
local_cache = {}
cache_lock = threading.Lock()

try:
    config.load_incluster_config()
except:
    config.load_kube_config()

api = client.CustomObjectsApi()

# ---------------------------------------------------------
# [유틸리티] 로그 및 포맷팅
# ---------------------------------------------------------
def log(msg):
    now = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{now}] {msg}", flush=True)

def print_dashboard(limit, running_cnt, pending_list):
    """
    현재 상태를 시각적으로 보여주는 대시보드 로그
    """
    bar_length = 20
    filled_length = int(bar_length * running_cnt // limit) if limit > 0 else 0
    bar = '█' * filled_length + '-' * (bar_length - filled_length)
    
    log("="*50)
    log(f"📊 [시스템 현황] Limit: {limit}")
    log(f"🟢 실행 중 : {running_cnt:2d} / {limit:2d} |{bar}| ({running_cnt/limit*100 if limit else 0:.1f}%)")
    log(f"⏳ 대기 중 : {len(pending_list):2d} 개")
    
    if len(pending_list) > 0:
        log("-" * 50)
        log("   [대기열 Top 3]")
        for idx, item in enumerate(pending_list[:3]):
            ns = item['metadata']['namespace']
            name = item['metadata']['name']
            log(f"   {idx+1}. {ns}/{name}")
    log("="*50)

# ---------------------------------------------------------
# [핵심 로직] 캐시 기반 상태 조회 (API 호출 0회)
# ---------------------------------------------------------
def is_target_namespace(namespace):
    return fnmatch.fnmatch(namespace, NAMESPACE_PATTERN)

def get_queue_status_from_cache():
    """
    etcd를 조회하지 않고, 메모리에 있는 local_cache를 뒤져서 계산함.
    논문의 핵심: O(N) API Call -> O(1) Memory Access
    """
    running_cnt = 0
    managed_pending_list = []

    with cache_lock:
        for key, item in local_cache.items():
            ns = item['metadata']['namespace']
            
            # 1. 관리 대상 네임스페이스인지 확인
            if not is_target_namespace(ns): 
                continue

            # 2. 상태 확인
            spec_status = item.get('spec', {}).get('status')
            conditions = item.get('status', {}).get('conditions', [])
            
            # 이미 완료된(Succeeded/Failed/Cancelled) 파이프라인은 카운트 제외
            if conditions and conditions[0]['status'] != 'Unknown':
                continue
            
            # 실행 중 vs 대기 중 분류
            if spec_status != 'PipelineRunPending':
                running_cnt += 1
            else:
                # 라벨이 있는 정식 대기열만 포함
                labels = item['metadata'].get('labels', {})
                if labels.get(MANAGED_LABEL_KEY) == MANAGED_LABEL_VAL:
                    managed_pending_list.append(item)

    # 먼저 생성된 순서대로 정렬 (FIFO)
    managed_pending_list.sort(key=lambda x: x['metadata']['creationTimestamp'])
    return running_cnt, managed_pending_list

def update_cache(event_type, obj):
    """
    Watcher로부터 이벤트를 받아 캐시를 현행화하는 함수
    """
    ns = obj['metadata']['namespace']
    name = obj['metadata']['name']
    key = f"{ns}/{name}"

    with cache_lock:
        if event_type == 'DELETED':
            if key in local_cache:
                del local_cache[key]
                # log(f"[Cache] 삭제됨: {key}") # 너무 시끄러우면 주석 처리
        else:
            local_cache[key] = obj
            # log(f"[Cache] 업데이트: {key}") # 디버깅용

# ---------------------------------------------------------
# [K8s 조작] 실제 변경이 필요할 때만 호출 (API 호출 최소화)
# ---------------------------------------------------------
def get_limit_from_crd():
    try:
        obj = api.get_cluster_custom_object('tekton.devops', 'v1', 'globallimits', 'tekton-queue-limit')
        return int(obj['spec']['maxPipelines'])
    except:
        return DEFAULT_LIMIT

def patch_status(name, namespace, status_val):
    try:
        body = {'spec': {'status': status_val}}
        api.patch_namespaced_custom_object(
            'tekton.dev', 'v1', namespace, 'pipelineruns', name, body
        )
        action = "🚀 실행 시작" if status_val is None else "⛔ 대기 전환"
        log(f"[{action}] {namespace}/{name}")
        return True
    except ApiException as e:
        log(f"[Patch 실패] {e}")
        return False
    except:
        return False

def add_managed_label(name, namespace):
    try:
        body = {'metadata': {'labels': {MANAGED_LABEL_KEY: MANAGED_LABEL_VAL}}}
        api.patch_namespaced_custom_object('tekton.dev', 'v1', namespace, 'pipelineruns', name, body)
    except: pass

def recreate_as_pending(original_obj):
    # (기존 코드와 동일 - 생략 없이 사용하세요)
    ns = original_obj['metadata']['namespace']
    name = original_obj['metadata']['name']
    log(f"👮 [강제 집행] {ns}/{name} -> 쿼터 초과로 즉시 삭제 후 대기열 이동")

    try:
        api.delete_namespaced_custom_object(
            'tekton.dev', 'v1', ns, 'pipelineruns', name,
            body=client.V1DeleteOptions(propagation_policy='Background')
        )
    except: return

    new_obj = copy.deepcopy(original_obj)
    # 메타데이터 정리
    for key in ['resourceVersion', 'uid', 'creationTimestamp', 'ownerReferences', 'generation']:
        if key in new_obj['metadata']: del new_obj['metadata'][key]
    
    if 'status' in new_obj: del new_obj['status']
    if 'spec' not in new_obj: new_obj['spec'] = {}
    new_obj['spec']['status'] = 'PipelineRunPending'

    if 'labels' not in new_obj['metadata']: new_obj['metadata']['labels'] = {}
    new_obj['metadata']['labels'][MANAGED_LABEL_KEY] = MANAGED_LABEL_VAL

    base_name = name[:40]
    new_obj['metadata']['name'] = f"{base_name}-q{int(time.time())}"

    try:
        api.create_namespaced_custom_object('tekton.dev', 'v1', ns, 'pipelineruns', new_obj)
        log(f"✅ [재등록 완료] {ns}/{new_obj['metadata']['name']} (순번 대기)")
    except Exception as e:
        log(f"재생성 실패: {e}")

# ---------------------------------------------------------
# [Thread 1] 매니저 (주기적 실행 담당)
# ---------------------------------------------------------
def manager_loop():
    log("🔧 매니저 스레드 시작 (스케줄링 주기: 5초)")
    last_log_time = 0

    while True:
        try:
            limit = get_limit_from_crd() # ConfigMap 조회 (가벼움)
            
            # [중요] API 호출 없이 캐시에서 즉시 조회
            running, pending = get_queue_status_from_cache()

            # 상태가 변했거나 일정 시간이 지났으면 로그 출력
            if len(pending) > 0 or abs(time.time() - last_log_time) > 60:
                print_dashboard(limit, running, pending)
                last_log_time = time.time()

            # 스케줄링 로직
            if running < limit and pending:
                slots = limit - running
                to_run = pending[:slots]

                for target in to_run:
                    t_name = target['metadata']['name']
                    t_ns = target['metadata']['namespace']
                    
                    # 실행 시도
                    if patch_status(t_name, t_ns, None):
                        running += 1
                        slots -= 1
                        # 캐시 즉시 업데이트 (API Watch 오기 전에 미리 반영해두기)
                        with cache_lock:
                            key = f"{t_ns}/{t_name}"
                            if key in local_cache:
                                if 'spec' not in local_cache[key]: local_cache[key]['spec'] = {}
                                local_cache[key]['spec']['status'] = None # Pending 해제
                        
        except Exception as e:
            log(f"매니저 에러: {e}")
        
        time.sleep(5)

# ---------------------------------------------------------
# [Thread 2] 왓쳐 (캐시 동기화 및 단속)
# ---------------------------------------------------------
def watcher_loop():
    log("👀 왓쳐 스레드 시작 (Informer Pattern)")
    resource_version = None

    while True:
        try:
            # 1. [List] 최초 1회 전체 동기화
            if resource_version is None:
                log("📡 클러스터 상태 전체 동기화 중... (List)")
                raw_resp = api.list_cluster_custom_object(
                    'tekton.dev', 'v1', 'pipelineruns', _preload_content=False
                )
                data = json.loads(raw_resp.data)
                resource_version = data['metadata']['resourceVersion']
                
                # 초기 캐시 구축
                with cache_lock:
                    local_cache.clear()
                    for item in data.get('items', []):
                        key = f"{item['metadata']['namespace']}/{item['metadata']['name']}"
                        local_cache[key] = item
                
                log(f"✅ 동기화 완료. 캐시 항목: {len(local_cache)}개. 감시 시작.")

            # 2. [Watch] 변경 사항 스트리밍
            w = watch.Watch()
            stream = w.stream(
                api.list_cluster_custom_object,
                'tekton.dev', 'v1', 'pipelineruns',
                resource_version=resource_version,
                timeout_seconds=0
            )

            for event in stream:
                obj = event['object']
                etype = event['type']
                
                # 다음 재연결을 위해 버전 갱신
                resource_version = obj['metadata']['resourceVersion']

                # [핵심] 1. 캐시 무조건 최신화
                update_cache(etype, obj)

                # [핵심] 2. 과속 단속 로직 (여기서도 API 조회 안 함!)
                if etype == 'ADDED' or etype == 'MODIFIED':
                    ns = obj['metadata']['namespace']
                    name = obj['metadata']['name']
                    
                    if not is_target_namespace(ns): continue
                    
                    # 이미 끝난거면 패스
                    conds = obj.get('status', {}).get('conditions', [])
                    if conds and conds[0]['status'] != 'Unknown': continue

                    # Pending 상태면 패스
                    spec_status = obj.get('spec', {}).get('status')
                    if spec_status == 'PipelineRunPending': continue

                    # 라벨 부착
                    if MANAGED_LABEL_KEY not in obj['metadata'].get('labels', {}):
                        add_managed_label(name, ns)

                    # 쿼터 체크 (캐시 기반)
                    limit = get_limit_from_crd()
                    running, _ = get_queue_status_from_cache()

                    # 내 자신이 Running에 포함되어 있으므로, limit보다 크면 내가 과속범임
                    if running > limit:
                        log(f"🚨 [과속 감지] {ns}/{name} (Running: {running} > Limit: {limit})")
                        success = patch_status(name, ns, 'PipelineRunPending')
                        if not success:
                            recreate_as_pending(obj)

        except ApiException as e:
            if e.status == 410: # Resource expired
                resource_version = None
            else:
                log(f"API 에러: {e}")
                time.sleep(2)
        except Exception as e:
            log(f"왓쳐 에러: {e}")
            resource_version = None
            time.sleep(2)

if __name__ == "__main__":
    t1 = threading.Thread(target=manager_loop, daemon=True)
    t2 = threading.Thread(target=watcher_loop, daemon=True)
    t1.start(); t2.start()
    
    try:
        while True: time.sleep(1)
    except KeyboardInterrupt:
        log("프로그램 종료")
