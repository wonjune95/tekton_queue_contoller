"""
웹훅 /mutate 지연 히스토그램 테스트 (§3.4 제어 계층 부하 p50/p99 소스).
"""
from prometheus_client import REGISTRY


def _count():
    return REGISTRY.get_sample_value(
        'tekton_queue_webhook_latency_seconds_count') or 0.0


def _mutate_req(namespace):
    return {
        "apiVersion": "admission.k8s.io/v1",
        "kind": "AdmissionReview",
        "request": {
            "uid": "u1",
            "object": {"metadata": {"namespace": namespace, "labels": {}}, "spec": {}},
            "userInfo": {"username": "someone"},
        },
    }


def test_mutate_records_latency(flask_client):
    before = _count()
    # 비대상 네임스페이스 → passthrough 경로도 계측되어야 함
    resp = flask_client.post('/mutate', json=_mutate_req("plain-ns"))
    assert resp.status_code == 200
    assert _count() == before + 1


def test_healthz_not_counted_as_webhook_latency(flask_client):
    before = _count()
    flask_client.get('/healthz')
    # /mutate 만 계측 대상
    assert _count() == before
