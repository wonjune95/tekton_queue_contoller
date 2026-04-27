"""
공유 전역 상태 모듈.

여러 스레드(Watcher, Manager, Leader Election, Webhook)에서
읽고 쓰는 전역 플래그를 한 곳에서 관리하여 순환 import를 방지합니다.
"""
import threading

# Leader Election 상태
is_leader: bool = False
leader_lock: threading.Lock = threading.Lock()

# 초기 캐시 동기화 완료 여부 (Watcher가 True로 설정)
initial_sync_done: bool = False
