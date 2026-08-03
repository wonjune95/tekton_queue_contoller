"""
Prometheus 메트릭 모듈.

컨트롤러 전반에서 사용하는 모든 Gauge/Counter 객체를 여기서 정의합니다.
"""
from prometheus_client import Gauge, Counter, Histogram

METRIC_WEBHOOK_LATENCY = Histogram(
    'tekton_queue_webhook_latency_seconds',
    'Mutating webhook(/mutate) 처리 지연 시간 (초)',
    buckets=(0.001, 0.0025, 0.005, 0.01, 0.025, 0.05,
             0.1, 0.25, 0.5, 1.0, 2.5, 5.0),
)

METRIC_QUEUE_LIMIT = Gauge(
    'tekton_queue_limit',
    '최대 동시 실행 파이프라인 수'
)
METRIC_QUEUE_RUNNING = Gauge(
    'tekton_queue_running_total',
    '현재 실행 중인 파이프라인 수'
)
METRIC_QUEUE_PENDING = Gauge(
    'tekton_queue_pending_total',
    '대기열에 있는 파이프라인 수',
    ['tier']
)
METRIC_WEBHOOK_ADMITTED = Counter(
    'tekton_queue_webhook_admitted_total',
    'Webhook을 통해 즉시 실행이 허용된 횟수',
    ['tier']
)
METRIC_WEBHOOK_QUEUED = Counter(
    'tekton_queue_webhook_queued_total',
    'Dashboard PR이 쿼터 초과로 대기열로 보내진 횟수',
    ['tier']
)
METRIC_WEBHOOK_HELD = Counter(
    'tekton_queue_webhook_held_total',
    'Dashboard 외 출처에서 생성되어 보류된 횟수',
    ['tier']
)
METRIC_SCHEDULED = Counter(
    'tekton_queue_scheduled_total',
    'Manager 루프에 의해 실행 상태로 스케줄링된 횟수',
    ['tier']
)
METRIC_PROMOTION = Counter(
    'tekton_queue_promotion_total',
    '에이징으로 대기 중 PR의 유효 Tier가 승격된 횟수',
    ['from_tier', 'to_tier']
)
METRIC_API_ERRORS = Counter(
    'tekton_queue_kubernetes_api_errors_total',
    'Kubernetes API 에러 횟수',
    ['operation']
)
