import time
import datetime
import threading
import fnmatch
import copy
from kubernetes import client, config, watch
from kubernetes.client.rest import ApiException

# =========================================================
# [설정]
NAMESPACE_PATTERN = "*-cicd"
DEFAULT_LIMIT = 10
MANAGED_LABEL_KEY = "queue.tekton.dev/managed"
MANAGED_LABEL_VAL = "yes"
# =========================================================

try:
    config.load_incluster_config()
except:
    config.load_kube_config()

api = client.CustomObjectsApi()

def is_target_namespace(namespace):
    return fnmatch.fnmatch(namespace, NAMESPACE_PATTERN)

def get_limit_from_crd():
    try:
        obj = api.get_cluster_custom_object('tekton.devops', 'v1', 'globallimits', 'tekton-queue-limit')
        return int(obj['spec']['maxPipelines'])
    except:
        return DEFAULT_LIMIT

def add_managed_label(name, namespace):
    try:
        body = {'metadata': {'labels': {MANAGED_LABEL_KEY: MANAGED_LABEL_VAL}}}
        api.patch_namespaced_custom_object('tekton.dev', 'v1', namespace, 'pipelineruns', name, body)
        print(f"🏷️ [등록] {namespace}/{name} -> 관리 대상 지정")
    except: pass

def patch_status(name, namespace, status_val):
    """일반적인 상태 변경 (성공하면 True, 실패하면 False 리턴)"""
    try:
        body = {'spec': {'status': status_val}}
        api.patch_namespaced_custom_object(
            'tekton.dev', 'v1', namespace, 'pipelineruns', name, body
        )
        msg = "🚀 실행 시작" if status_val is None else "⛔ 대기 처리"
        print(f"[{msg}] {namespace}/{name}")
        return True
    except ApiException as e:
        # 이미 시작돼서(Started) 변경 불가능한 경우 -> 실패 리턴 -> 강제 집행 로직으로 넘어감
        if e.status == 400:
            print(f"⚠️ [변경 불가] {namespace}/{name}: 이미 실행되어 Pending 전환 실패.")
            return False
        return False
    except:
        return False

def recreate_as_pending(original_obj):
    """
    [핵심 로직] 실행되어버린 파이프라인을 '삭제'하고 'Pending 상태로 복제'
    """
    ns = original_obj['metadata']['namespace']
    name = original_obj['metadata']['name']

    print(f"👮 [강제 집행] {ns}/{name} -> 즉시 삭제 후 대기열로 재등록합니다.")

    # 1. 기존 파이프라인 삭제 (Background 삭제로 리소스 정리)
    try:
        api.delete_namespaced_custom_object(
            'tekton.dev', 'v1', ns, 'pipelineruns', name,
            body=client.V1DeleteOptions(propagation_policy='Background')
        )
    except Exception as e:
        print(f"❌ 삭제 실패: {e}")
        return

    # 2. 새 객체 준비 (기존 스펙 복사)
    new_obj = copy.deepcopy(original_obj)

    # 메타데이터 정리 (시스템이 부여한 필드 제거)
    if 'resourceVersion' in new_obj['metadata']: del new_obj['metadata']['resourceVersion']
    if 'uid' in new_obj['metadata']: del new_obj['metadata']['uid']
    if 'creationTimestamp' in new_obj['metadata']: del new_obj['metadata']['creationTimestamp']
    if 'ownerReferences' in new_obj['metadata']: del new_obj['metadata']['ownerReferences']

    # 상태값 초기화 (이전 실행 기록 삭제)
    if 'status' in new_obj: del new_obj['status']

    # [중요] Pending 상태로 설정 + 관리 라벨 부착
    if 'spec' not in new_obj: new_obj['spec'] = {}
    new_obj['spec']['status'] = 'PipelineRunPending'

    if 'labels' not in new_obj['metadata']: new_obj['metadata']['labels'] = {}
    new_obj['metadata']['labels'][MANAGED_LABEL_KEY] = MANAGED_LABEL_VAL

    # 이름 변경 (기존 이름 + "-queued")
    # 기존 이름이 너무 길면 잘라냄 (K8s 이름 길이 제한 63자 고려)
    base_name = name[:50]
    new_obj['metadata']['name'] = f"{base_name}-q{int(time.time())}" # 유니크하게 생성

    # 3. 신규 생성
    try:
        api.create_namespaced_custom_object('tekton.dev', 'v1', ns, 'pipelineruns', new_obj)
        print(f"✅ [재등록 완료] {ns}/{new_obj['metadata']['name']} (대기 중)")
    except Exception as e:
        print(f"❌ 재생성 실패: {e}")

def get_queue_status():
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

        if conditions and conditions[0]['status'] in ['True', 'False']:
            continue

        # Running 상태면 무조건 카운트
        if spec_status != 'PipelineRunPending':
            running_cnt += 1
        # Pending이면서 관리 라벨이 있어야 대기열
        elif labels.get(MANAGED_LABEL_KEY) == MANAGED_LABEL_VAL:
            managed_pending_list.append(item)

    managed_pending_list.sort(key=lambda x: x['metadata']['creationTimestamp'])
    return running_cnt, managed_pending_list

# ---------------------------------------------------------
# [Thread 1] 매니저
# ---------------------------------------------------------
def manager_loop():
    print("👷 매니저 시작")
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
                    print(f"⚡ 자리 남음({running}/{limit}). {t_ns}/{t_name} 입장!")
                    patch_status(t_name, t_ns, None)
                    running += 1
                    slots -= 1
        except Exception as e:
            print(f"⚠️ 매니저 에러: {e}")
        time.sleep(5)

# ---------------------------------------------------------
# [Thread 2] 왓쳐 (경찰)
# ---------------------------------------------------------
def watcher_loop():
    print("👀 왓쳐 시작")
    while True:
        w = watch.Watch()
        try:
            stream = w.stream(
                api.list_cluster_custom_object,
                'tekton.dev', 'v1', 'pipelineruns',
                timeout_seconds=0
            )
            for event in stream:
                if event['type'] == 'ADDED':
                    obj = event['object']
                    ns = obj['metadata']['namespace']
                    name = obj['metadata']['name']
                    spec_status = obj.get('spec', {}).get('status')

                    if not is_target_namespace(ns): continue

                    # 1. 템플릿(이미 Pending)은 무시
                    if spec_status == 'PipelineRunPending': continue

                    # 2. 일단 관리 대상 등록
                    add_managed_label(name, ns)

                    # 3. 과속 단속 (자리 없는데 실행됨?)
                    limit = get_limit_from_crd()
                    running, _ = get_queue_status()

                    if running > limit:
                        print(f"🚨 [과속 감지] {ns}/{name} (현재 {running-1}/{limit})")

                        # 1차 시도: Patch로 얌전히 멈춰본다.
                        success = patch_status(name, ns, 'PipelineRunPending')

                        # 2차 시도: Tekton이 거부하면(이미 시작됨)? -> 강제 집행(삭제 후 재생성)
                        if not success:
                            recreate_as_pending(obj)

        except ApiException: pass
        except Exception as e:
            print(f"⚠️ 왓쳐 에러: {e}")
            time.sleep(1)

if __name__ == "__main__":
    t1 = threading.Thread(target=manager_loop)
    t2 = threading.Thread(target=watcher_loop)
    t1.start(); t2.start()
    t1.join(); t2.join()
