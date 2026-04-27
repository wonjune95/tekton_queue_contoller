"""webhook.py 단위 테스트 — /mutate, /healthz, /readyz 엔드포인트."""
import json
import base64
from unittest.mock import patch, MagicMock

import src.state as state
import src.cache as cache
import src.config as config
from tests.conftest import make_pr, make_admission_request


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# 헬퍼
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
def _decode_patch(response_data):
    """AdmissionReview 응답에서 JSONPatch를 디코딩."""
    resp = json.loads(response_data)
    patch_b64 = resp.get("response", {}).get("patch")
    if not patch_b64:
        return None
    return json.loads(base64.b64decode(patch_b64))


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# 1) Health 엔드포인트
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
class TestHealthEndpoints:
    def test_healthz_returns_200(self, flask_client):
        resp = flask_client.get('/healthz')
        assert resp.status_code == 200
        data = resp.get_json()
        assert data['status'] == 'ok'

    def test_healthz_shows_leader_status(self, flask_client):
        state.is_leader = True
        resp = flask_client.get('/healthz')
        assert resp.get_json()['leader'] is True

    def test_readyz_not_synced_returns_503(self, flask_client):
        state.initial_sync_done = False
        resp = flask_client.get('/readyz')
        assert resp.status_code == 503

    def test_readyz_synced_returns_200(self, flask_client):
        state.initial_sync_done = True
        resp = flask_client.get('/readyz')
        assert resp.status_code == 200
        assert resp.get_json()['status'] == 'ready'

    def test_readyz_includes_cache_size(self, flask_client):
        state.initial_sync_done = True
        cache.local_cache["ns/pr1"] = {}
        cache.local_cache["ns/pr2"] = {}
        resp = flask_client.get('/readyz')
        assert resp.get_json()['cached_resources'] == 2


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# 2) /mutate — 비대상 네임스페이스
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
class TestMutateNonTarget:
    def test_non_target_namespace_passthrough(self, flask_client):
        state.initial_sync_done = True
        req = make_admission_request(namespace="other-ns")
        resp = flask_client.post('/mutate', json=req, content_type='application/json')
        data = resp.get_json()
        assert data['response']['allowed'] is True
        assert 'patch' not in data['response']

    def test_empty_body_passthrough(self, flask_client):
        resp = flask_client.post('/mutate', data='', content_type='application/json')
        data = resp.get_json()
        assert data['response']['allowed'] is True


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# 3) /mutate — Dashboard 외 출처 (HOLD)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
class TestMutateHold:
    def test_non_dashboard_user_gets_pending(self, flask_client):
        state.initial_sync_done = True
        req = make_admission_request(username="system:serviceaccount:default:bastion")
        resp = flask_client.post('/mutate', json=req, content_type='application/json')
        patches = _decode_patch(resp.data)
        statuses = [p['value'] for p in patches if p['path'] == '/spec/status']
        assert 'PipelineRunPending' in statuses

    def test_non_dashboard_no_managed_label(self, flask_client):
        """Dashboard 외 출처는 managed 라벨이 붙지 않아 스케줄링 제외."""
        state.initial_sync_done = True
        req = make_admission_request(username="admin")
        resp = flask_client.post('/mutate', json=req, content_type='application/json')
        patches = _decode_patch(resp.data)
        managed_key = config.MANAGED_LABEL_KEY.replace("/", "~1")
        managed_patches = [p for p in patches if managed_key in p.get('path', '')]
        assert len(managed_patches) == 0

    def test_non_dashboard_gets_tier_label(self, flask_client):
        state.initial_sync_done = True
        req = make_admission_request(username="ci-bot", labels={"env": "prod"})
        resp = flask_client.post('/mutate', json=req, content_type='application/json')
        patches = _decode_patch(resp.data)
        tier_key = config.TIER_LABEL_KEY.replace("/", "~1")
        tier_patches = [p for p in patches if tier_key in p.get('path', '')]
        assert len(tier_patches) > 0


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# 4) /mutate — Dashboard 출처 + 쿼터 여유 (ADMIT)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
class TestMutateAdmit:
    @patch('src.cache.core_api')
    def test_dashboard_admit_no_pending_status(self, mock_api, flask_client):
        """쿼터 여유 → Pending 없이 즉시 실행."""
        state.initial_sync_done = True
        cm = MagicMock()
        cm.data = {'admitted': '0'}
        mock_api.read_namespaced_config_map.return_value = cm
        req = make_admission_request(labels={"env": "prod"})
        resp = flask_client.post('/mutate', json=req, content_type='application/json')
        patches = _decode_patch(resp.data)
        has_pending = any(p.get('value') == 'PipelineRunPending' for p in patches)
        assert has_pending is False

    @patch('src.cache.core_api')
    def test_dashboard_admit_adds_tier_label(self, mock_api, flask_client):
        state.initial_sync_done = True
        cm = MagicMock()
        cm.data = {'admitted': '0'}
        mock_api.read_namespaced_config_map.return_value = cm
        req = make_admission_request(labels={"env": "stg"})
        resp = flask_client.post('/mutate', json=req, content_type='application/json')
        patches = _decode_patch(resp.data)
        tier_key = config.TIER_LABEL_KEY.replace("/", "~1")
        tier_patches = [p for p in patches if tier_key in p.get('path', '')]
        assert len(tier_patches) == 1
        assert tier_patches[0]['value'] == '2'  # stg = Tier 2

    @patch('src.cache.core_api')
    def test_phantom_entry_inserted(self, mock_api, flask_client):
        """즉시 실행 시 phantom entry가 캐시에 삽입."""
        state.initial_sync_done = True
        cm = MagicMock()
        cm.data = {'admitted': '0'}
        mock_api.read_namespaced_config_map.return_value = cm
        req = make_admission_request(name="real-pr", labels={"env": "dev"})
        flask_client.post('/mutate', json=req, content_type='application/json')
        assert "test-cicd/real-pr" in cache.local_cache
        assert cache.local_cache["test-cicd/real-pr"]['metadata']['resourceVersion'] == '__admitted__'


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# 5) /mutate — Dashboard 출처 + 쿼터 초과 (QUEUE)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
class TestMutateQueue:
    @patch('src.cache.core_api')
    def test_quota_exceeded_gets_pending(self, mock_api, flask_client):
        state.initial_sync_done = True
        config.crd_config['max_pipelines'] = 2
        # 이미 2개 running
        cache.local_cache["test-cicd/r1"] = make_pr(name="r1")
        cache.local_cache["test-cicd/r2"] = make_pr(name="r2")
        cm = MagicMock()
        cm.data = {'admitted': '0'}
        mock_api.read_namespaced_config_map.return_value = cm
        req = make_admission_request(labels={"env": "dev"})
        resp = flask_client.post('/mutate', json=req, content_type='application/json')
        patches = _decode_patch(resp.data)
        assert any(p.get('value') == 'PipelineRunPending' for p in patches)

    @patch('src.cache.core_api')
    def test_quota_exceeded_gets_managed_label(self, mock_api, flask_client):
        state.initial_sync_done = True
        config.crd_config['max_pipelines'] = 0
        cm = MagicMock()
        cm.data = {'admitted': '0'}
        mock_api.read_namespaced_config_map.return_value = cm
        req = make_admission_request(labels={"env": "dev"})
        resp = flask_client.post('/mutate', json=req, content_type='application/json')
        patches = _decode_patch(resp.data)
        managed_key = config.MANAGED_LABEL_KEY.replace("/", "~1")
        managed = [p for p in patches if managed_key in p.get('path', '')]
        assert len(managed) == 1
        assert managed[0]['value'] == config.MANAGED_LABEL_VAL


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# 6) /mutate — 특수 케이스
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
class TestMutateSpecialCases:
    def test_cache_not_synced_passthrough(self, flask_client):
        """캐시 동기화 전 Dashboard 요청은 통과 처리."""
        state.initial_sync_done = False
        req = make_admission_request()
        resp = flask_client.post('/mutate', json=req, content_type='application/json')
        data = resp.get_json()
        assert data['response']['allowed'] is True
        assert data['response'].get('patch') is None

    @patch('src.cache.core_api')
    def test_generatename_no_phantom(self, mock_api, flask_client):
        """generateName PR은 phantom entry를 삽입하지 않는다."""
        state.initial_sync_done = True
        cm = MagicMock()
        cm.data = {'admitted': '0'}
        mock_api.read_namespaced_config_map.return_value = cm
        req = make_admission_request(name=None, generate_name="build-")
        flask_client.post('/mutate', json=req, content_type='application/json')
        # generateName이므로 phantom entry 없음
        phantom_keys = [k for k in cache.local_cache if '__admitted__' in
                       cache.local_cache[k].get('metadata', {}).get('resourceVersion', '')]
        assert len(phantom_keys) == 0

    @patch('src.cache.core_api')
    def test_no_labels_at_all(self, mock_api, flask_client):
        """라벨이 전혀 없는 PR도 정상 처리."""
        state.initial_sync_done = True
        config.crd_config['max_pipelines'] = 0
        cm = MagicMock()
        cm.data = {'admitted': '0'}
        mock_api.read_namespaced_config_map.return_value = cm
        req = make_admission_request(labels=None)
        # labels가 None이면 메타데이터에 labels 키 자체가 없어야 함
        req['request']['object']['metadata'].pop('labels', None)
        resp = flask_client.post('/mutate', json=req, content_type='application/json')
        assert resp.status_code == 200
        patches = _decode_patch(resp.data)
        # 라벨 전체를 한 번에 추가하는 패치
        label_add = [p for p in patches if p.get('path') == '/metadata/labels']
        assert len(label_add) > 0

    @patch('src.cache.core_api')
    def test_urgent_label_gets_tier_0(self, mock_api, flask_client):
        state.initial_sync_done = True
        cm = MagicMock()
        cm.data = {'admitted': '0'}
        mock_api.read_namespaced_config_map.return_value = cm
        req = make_admission_request(
            labels={"queue.tekton.dev/urgent": "true", "env": "dev"})
        resp = flask_client.post('/mutate', json=req, content_type='application/json')
        patches = _decode_patch(resp.data)
        tier_key = config.TIER_LABEL_KEY.replace("/", "~1")
        tier_patches = [p for p in patches if tier_key in p.get('path', '')]
        assert tier_patches[0]['value'] == '0'
