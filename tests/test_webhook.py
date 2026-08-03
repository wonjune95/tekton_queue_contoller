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
    def test_dashboard_always_queued(self, mock_api, flask_client):
        """관리 대상은 쿼터 여유와 무관하게 항상 대기열(Pending)로 간다.

        웹훅은 인가를 판정하지 않는다. 요청마다 독립 실행되므로 동시 인가 요청에서
        상한이 깨졌다(동시 12요청, 상한 30 → 최대 48). 인가는 매니저가 단독 수행한다.
        """
        state.initial_sync_done = True
        cm = MagicMock()
        cm.data = {'admitted': '0'}
        mock_api.read_namespaced_config_map.return_value = cm
        req = make_admission_request(labels={"env": "prod"})
        resp = flask_client.post('/mutate', json=req, content_type='application/json')
        patches = _decode_patch(resp.data)
        assert any(p.get('value') == 'PipelineRunPending' for p in patches),             "관리 대상은 항상 Pending 이어야 한다"
        managed_key = config.MANAGED_LABEL_KEY.replace("/", "~1")
        assert any(managed_key in p.get('path', '') for p in patches),             "managed 라벨이 없으면 매니저가 스케줄링 대상으로 보지 않는다"

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
    def test_no_phantom_entry(self, mock_api, flask_client):
        """phantom entry 를 더 이상 삽입하지 않는다.

        phantom 은 웹훅이 즉시 인가한 건을 캐시에 미리 넣어 슬롯을 예약하던 장치다.
        웹훅이 인가하지 않게 되면서 예약할 대상 자체가 없어졌다.
        """
        state.initial_sync_done = True
        cm = MagicMock()
        cm.data = {'admitted': '0'}
        mock_api.read_namespaced_config_map.return_value = cm
        req = make_admission_request(name="real-pr", labels={"env": "dev"})
        flask_client.post('/mutate', json=req, content_type='application/json')
        assert "test-cicd/real-pr" not in cache.local_cache


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


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# 7) 응답 UID 일치
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
class TestMutateResponseUID:
    @patch('src.cache.core_api')
    def test_admitted_response_uid_matches(self, mock_api, flask_client):
        """즉시 실행 응답의 UID가 요청 UID와 일치한다."""
        state.initial_sync_done = True
        cm = MagicMock()
        cm.data = {'admitted': '0'}
        mock_api.read_namespaced_config_map.return_value = cm
        req = make_admission_request()
        req['request']['uid'] = 'specific-uid-abc123'
        resp = flask_client.post('/mutate', json=req, content_type='application/json')
        assert resp.get_json()['response']['uid'] == 'specific-uid-abc123'

    def test_non_target_ns_uid_matches(self, flask_client):
        """비대상 NS 응답도 요청 UID를 그대로 반환한다."""
        req = make_admission_request(namespace="other-ns")
        req['request']['uid'] = 'uid-non-target-xyz'
        resp = flask_client.post('/mutate', json=req, content_type='application/json')
        assert resp.get_json()['response']['uid'] == 'uid-non-target-xyz'

    def test_hold_response_uid_matches(self, flask_client):
        """보류(HOLD) 응답도 요청 UID를 그대로 반환한다."""
        state.initial_sync_done = True
        req = make_admission_request(username="system:serviceaccount:default:bastion")
        req['request']['uid'] = 'uid-hold-789'
        resp = flask_client.post('/mutate', json=req, content_type='application/json')
        assert resp.get_json()['response']['uid'] == 'uid-hold-789'


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# 8) managedSAPatterns 와일드카드
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
class TestManagedSAPatternsFnmatch:
    @patch('src.cache.core_api')
    def test_wildcard_pattern_matches_dashboard(self, mock_api, flask_client):
        """'system:serviceaccount:tekton-pipelines:*' 패턴이 tekton-dashboard에 매칭된다."""
        state.initial_sync_done = True
        config.crd_config['managed_sa_patterns'] = ['system:serviceaccount:tekton-pipelines:*']
        cm = MagicMock()
        cm.data = {'admitted': '0'}
        mock_api.read_namespaced_config_map.return_value = cm
        req = make_admission_request(
            username="system:serviceaccount:tekton-pipelines:tekton-dashboard")
        resp = flask_client.post('/mutate', json=req, content_type='application/json')
        patches = _decode_patch(resp.data)
        # 관리 대상 출처 → 대기열 등록(Pending). 인가는 매니저가 한다.
        assert any(p.get('value') == 'PipelineRunPending' for p in (patches or []))

    def test_wildcard_pattern_excludes_other_namespace(self, flask_client):
        """다른 네임스페이스의 SA는 와일드카드 패턴에 매칭되지 않아 보류 처리된다."""
        state.initial_sync_done = True
        config.crd_config['managed_sa_patterns'] = ['system:serviceaccount:tekton-pipelines:*']
        req = make_admission_request(
            username="system:serviceaccount:default:some-user")
        resp = flask_client.post('/mutate', json=req, content_type='application/json')
        patches = _decode_patch(resp.data)
        assert any(p.get('value') == 'PipelineRunPending' for p in (patches or []))

    @patch('src.cache.core_api')
    def test_multiple_sa_patterns(self, mock_api, flask_client):
        """여러 SA 패턴 중 하나라도 매칭되면 관리 대상으로 처리된다."""
        state.initial_sync_done = True
        config.crd_config['managed_sa_patterns'] = [
            'system:serviceaccount:tekton-pipelines:tekton-dashboard',
            'system:serviceaccount:ci:ci-runner',
        ]
        cm = MagicMock()
        cm.data = {'admitted': '0'}
        mock_api.read_namespaced_config_map.return_value = cm
        req = make_admission_request(
            username="system:serviceaccount:ci:ci-runner")
        resp = flask_client.post('/mutate', json=req, content_type='application/json')
        patches = _decode_patch(resp.data)
        # 관리 대상 출처 → 대기열 등록(Pending). 인가는 매니저가 한다.
        assert any(p.get('value') == 'PipelineRunPending' for p in (patches or []))


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# 9) generateName + 쿼터 초과 조합
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
class TestMutateGenerateNameQueued:
    @patch('src.cache.core_api')
    def test_generatename_queued_gets_pending_and_managed(self, mock_api, flask_client):
        """generateName PR이 쿼터 초과 시 Pending + managed 라벨이 모두 적용된다."""
        state.initial_sync_done = True
        config.crd_config['max_pipelines'] = 0
        cm = MagicMock()
        cm.data = {'admitted': '0'}
        mock_api.read_namespaced_config_map.return_value = cm
        req = make_admission_request(name=None, generate_name="build-", labels={"env": "dev"})
        resp = flask_client.post('/mutate', json=req, content_type='application/json')
        patches = _decode_patch(resp.data)
        assert any(p.get('value') == 'PipelineRunPending' for p in patches)
        managed_key = config.MANAGED_LABEL_KEY.replace("/", "~1")
        assert any(managed_key in p.get('path', '') for p in patches)
