"""
Watcher 루프 모듈.

K8s Watch API로 PipelineRun 이벤트를 실시간으로 수신해
in-memory 캐시를 동기화합니다.
(docker/app.py L751~L799 발췌)
"""
import json
import time

from kubernetes import watch
from kubernetes.client.rest import ApiException

from src.config import log, api
from src.cache import local_cache, cache_lock, update_cache, _reset_global_admitted
from src import metrics as m
from src import state


def watcher_loop():
    log("[Watcher] 스레드 시작 (Informer 동기화)")
    resource_version = None

    while True:
        try:
            if resource_version is None:
                log("클러스터 파이프라인 상태 전체 동기화 중...")
                raw_resp = api.list_cluster_custom_object(
                    'tekton.dev', 'v1', 'pipelineruns', _preload_content=False
                )
                data             = json.loads(raw_resp.data)
                resource_version = data['metadata']['resourceVersion']
                new_cache        = {}
                for item in data.get('items', []):
                    key           = f"{item['metadata']['namespace']}/{item['metadata']['name']}"
                    new_cache[key] = item
                with cache_lock:
                    local_cache.clear()
                    local_cache.update(new_cache)
                _reset_global_admitted()
                if not state.initial_sync_done:
                    state.initial_sync_done = True
                    log("초기 동기화 완료. Webhook 트래픽 수신을 시작합니다.")
                log(f"동기화 완료 (현재 추적 중인 리소스: {len(local_cache)}개)")

            w      = watch.Watch()
            stream = w.stream(
                api.list_cluster_custom_object,
                'tekton.dev', 'v1', 'pipelineruns',
                resource_version=resource_version,
                timeout_seconds=300,
            )
            for event in stream:
                obj              = event['object']
                etype            = event['type']
                resource_version = obj['metadata']['resourceVersion']
                update_cache(etype, obj)

        except ApiException as e:
            m.METRIC_API_ERRORS.labels(operation='watch_pipelinerun').inc()
            if e.status == 410:
                log("[Watcher] resourceVersion 만료 (410 Gone). 전체 재동기화를 수행합니다.")
                resource_version = None
            else:
                log(f"[Watcher] API 에러 ({e.status}): {e.reason}. 재연결 시도 중...")
            time.sleep(2)
        except Exception as e:
            m.METRIC_API_ERRORS.labels(operation='watch_pipelinerun_stream').inc()
            log(f"[Watcher] 스트림 끊김, 재연결 시도 중... ({e})")
            resource_version = None
            time.sleep(2)
