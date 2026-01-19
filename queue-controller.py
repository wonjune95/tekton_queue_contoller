import time
import threading
import fnmatch
import copy
import json
import sys
from kubernetes import client, config, watch
from kubernetes.client.rest import ApiException

# =========================================================
# 관리 대상 네임스페이스 규칙
NAMESPACE_PATTERN = "*-cicd"

# CRD 조회가 실패할 경우 적용할 기본 동시 실행 제한 수
DEFAULT_LIMIT = 10

# 컨트롤러가 관리중임을 표시하는 식별 라벨
MANAGED_LABEL_KEY = "queue.tekton.dev/managed"
MANAGED_LABEL_VAL = "yes"
# =========================================================

# 컨테이너 환경에서는 표준 출력 버퍼링 때문에 로그가 남지 않고 유실될 수 있음, 강제 출력 보
def log(msg):
    print(f"{msg}", flush=True)
try:
    config.load_incluster_config()
except:
    config.load_kube_config()

api = client.CustomObjectsApi()

def is_target_namespace(namespace):
    # 설정된 패턴(*-cicd)과 일치하는 네임스페이스인지 검증
    return fnmatch.fnmatch(namespace, NAMESPACE_PATTERN)

def get_limit_from_crd():
    # ConfigMap에서 실시간으로 제한 값을 가져옴
    try:
        obj = api.get_cluster_custom_object('tekton.devops', 'v1', 'globallimits', 'tekton-queue-limit')
        return int(obj['spec']['maxPipelines'])
    except:
        return DEFAULT_LIMIT

def add_managed_label(name, namespace):
    # Tekton PipelineRun에 관리용 라벨 부착
    try:
        body = {'metadata': {'labels': {MANAGED_LABEL_KEY: MANAGED_LABEL_VAL}}}
        api.patch_namespaced_custom_object('tekton.dev', 'v1', namespace, 'pipelineruns', name, body)
        log(f"[등록] {namespace}/{name} -> 관리 대상 지정")
    except: pass

def patch_status(name, namespace, status_val):
    """
    상태 변경 로직
    - status_val=None 대기 해제(실행 시작)
    - status_val='PipelineRunPending' 대기 상태로 전환
    """
    try:
        body = {'spec': {'status': status_val}}
        api.patch_namespaced_custom_object(
            'tekton.dev', 'v1', namespace, 'pipelineruns', name, body
        )
        msg = "실행 시작" if status_val is None else "대기 처리"
        log(f"[{msg}] {namespace}/{name}")
        return True
    except ApiException as e:
        if e.status == 400 or e.status == 422:
            log(f"[변경 불가] {namespace}/{name}: 이미 실행되어 Pending 전환 실패.")
            return False
        return False
    except:
        return False

def recreate_as_pending(original_obj):
    """
    Fail-Safe 로직: 강제 집행
    이미 실행되어버린(Running) 파이프라인이 제한을 초과했다면,
    해당 리소스를 '삭제'하고 'Pending 상태로 복제'하여 대기열로 강제 이동시킴.
    
    * 주의: 이미 생성된 Pod는 종료되며, 파이프라인 ID(UID)가 변경됨.
    """
    ns = original_obj['metadata']['namespace']
    name = original_obj['metadata']['name']

    log(f"[강제 집행] {ns}/{name} -> 즉시 삭제 후 대기열로 재등록합니다.")

    # 1. 삭제 (Background)
    try:
        api.delete_namespaced_custom_object(
            'tekton.dev', 'v1', ns, 'pipelineruns', name,
            body=client.V1DeleteOptions(propagation_policy='Background')
        )
    except Exception as e:
        log(f"삭제 실패: {e}")
        return

    # 2. 객체 복제 및 클린업
    new_obj = copy.deepcopy(original_obj)

    # 시스템 필드 제거 (중요: 이 필드들이 있으면 생성 거부됨)
    for key in ['resourceVersion', 'uid', 'creationTimestamp', 'ownerReferences', 'generation']:
        if key in new_obj['metadata']:
            del new_obj['metadata'][key]

    # 상태 초기화
    if 'status' in new_obj: del new_obj['status']

    # Pending 설정
    if 'spec' not in new_obj: new_obj['spec'] = {}
    new_obj['spec']['status'] = 'PipelineRunPending'

    if 'labels' not in new_obj['metadata']: new_obj['metadata']['labels'] = {}
    new_obj['metadata']['labels'][MANAGED_LABEL_KEY] = MANAGED_LABEL_VAL

    # 이름 변경 (유니크성 보장)
    base_name = name[:40] # 길이 제한 고려
    new_obj['metadata']['name'] = f"{base_name}-q{int(time.time())}"

    # 3. 재생성
    try:
        api.create_namespaced_custom_object('tekton.dev', 'v1', ns, 'pipelineruns', new_obj)
        log(f"[재등록 완료] {ns}/{new_obj['metadata']['name']} (대기 중)")
    except Exception as e:
        log(f"재생성 실패: {e}")

def get_queue_status():
    """
    큐 상태 스냅샷
    현재 클러스터 내 '실행 중(Running)'인 파이프라인 수와
    '대기 중(Pending)'인 파이프라인 목록을 반환.
    """
    try:
        resp = api.list_cluster_custom_object('tekton.dev', 'v1', 'pipelineruns')
        items = resp.get('items', [])
    except:
        return 9999, []

    running_cnt = 0
    managed_pending_list = []

    for item in items:
        ns = item['metadata']['namespace']
        if not is_target_namespace(ns): continue

        spec_status = item.get('spec', {}).get('status')
        conditions = item.get('status', {}).get('conditions', [])
        labels = item['metadata'].get('labels', {})

        # 이미 끝난(Succeeded/Failed) 파이프라인은 카운트 제외
        if conditions and conditions[0]['status'] != 'Unknown':
            continue

        if spec_status != 'PipelineRunPending':
            running_cnt += 1
        elif labels.get(MANAGED_LABEL_KEY) == MANAGED_LABEL_VAL:
            managed_pending_list.append(item)

    # 먼저 생성된 순서대로 정렬 (FIFO)
    managed_pending_list.sort(key=lambda x: x['metadata']['creationTimestamp'])
    return running_cnt, managed_pending_list

# ---------------------------------------------------------
# [Thread 1] 매니저 (주기적 실행 담당)
# ---------------------------------------------------------
def manager_loop():
    log("매니저 시작")
    while True:
        try:
            limit = get_limit_from_crd()
            running, pending = get_queue_status()

            if running < limit and pending:
                slots = limit - running
                to_run = pending[:slots]

                for target in to_run:
                    t_name = target['metadata']['name']
                    t_ns = target['metadata']['namespace']
                    log(f"자리 남음({running}/{limit}). {t_ns}/{t_name} 입장!")
                    
                    # 실행 시도
                    if patch_status(t_name, t_ns, None):
                        running += 1
                        slots -= 1
        except Exception as e:
            log(f"매니저 에러: {e}")
        time.sleep(5)

# ---------------------------------------------------------
# [Thread 2] 왓쳐 (감시 및 단속 담당)
# ---------------------------------------------------------
def watcher_loop():
    log("왓쳐 시작")
    
    resource_version = None

    while True:
        try:
            # 1. [List 단계] 최초 연결 시, 현재 시점의 resourceVersion 획득
            if resource_version is None:
                log("[동기화] 현재 클러스터 시점 조회 중...")
                # _preload_content=False: 데이터 전체를 객체로 만들지 않고 헤더만 빠르게 읽음 (메모리 절약)
                raw_resp = api.list_cluster_custom_object(
                    'tekton.dev', 'v1', 'pipelineruns', _preload_content=False
                )
                # JSON 헤더 파싱
                data = json.loads(raw_resp.data)
                resource_version = data['metadata']['resourceVersion']
                log(f"기준점 획득: {resource_version} (이 시점 이후부터 감시)")

            # 2. [Watch 단계] 획득한 버전 '이후'의 변경사항만 스트리밍 (부하 99% 감소)
            w = watch.Watch()
            stream = w.stream(
                api.list_cluster_custom_object,
                'tekton.dev', 'v1', 'pipelineruns',
                resource_version=resource_version,
                timeout_seconds=0 # 무한 대기 (끊어지면 재연결)
            )

            for event in stream:
                obj = event['object']
                # 다음 재연결을 위해 ResourceVersion 갱신
                resource_version = obj['metadata']['resourceVersion']

                if event['type'] == 'ADDED':
                    ns = obj['metadata']['namespace']
                    name = obj['metadata']['name']
                    spec_status = obj.get('spec', {}).get('status')
                    
                    # 대상 네임스페이스가 아니거나 이미 Pending이면 무시
                    if not is_target_namespace(ns): continue
                    if spec_status == 'PipelineRunPending': continue
                    
                    # 이미 종료된 파이프라인 무시 (Watch 재연결 시 과거 이벤트 필터링)
                    conds = obj.get('status', {}).get('conditions', [])
                    if conds and conds[0]['status'] != 'Unknown': continue

                    # 관리 라벨 부착
                    add_managed_label(name, ns)

                    # 과속 단속
                    limit = get_limit_from_crd()
                    running, _ = get_queue_status()

                    # 실행 중인 개수가 리미트를 초과했다면?
                    if running > limit:
                        log(f"[과속 감지] {ns}/{name} (Limit: {limit}, Current: {running})")
                        
                        # 1차: Patch 시도
                        success = patch_status(name, ns, 'PipelineRunPending')
                        
                        # 2차: 실패 시 강제 재생성 (Recreate)
                        if not success:
                            recreate_as_pending(obj)

        except ApiException as e:
            # 410 Gone: ResourceVersion이 너무 오래됨 -> 초기화 후 다시 List
            if e.status == 410:
                log("버전 만료 (410). 전체 목록 다시 조회합니다.")
                resource_version = None
            else:
                log(f"API 에러: {e}")
            time.sleep(1)
            
        except Exception as e:
            log(f"왓쳐 내부 에러: {e}")
            # 알 수 없는 에러 시 안전하게 다시 List부터 시작
            resource_version = None
            time.sleep(2)

if __name__ == "__main__":
    t1 = threading.Thread(target=manager_loop, daemon=True)
    t2 = threading.Thread(target=watcher_loop, daemon=True)
    t1.start(); t2.start()
    
    # 메인 스레드가 죽지 않게 유지
    try:
        while True: time.sleep(1)
    except KeyboardInterrupt:
        log("프로그램 종료")
