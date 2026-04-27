"""config.py 단위 테스트 — 설정 로드, 티어 결정, 네임스페이스 매칭."""
from unittest.mock import patch, MagicMock
from kubernetes.client.rest import ApiException

from src.config import (
    determine_tier, is_target_namespace, load_crd_config, get_cached_config,
    DEFAULT_LIMIT, DEFAULT_TIER, DEFAULT_TIER_RULES,
    DEFAULT_AGING_INTERVAL_SEC, DEFAULT_AGING_MIN_TIER,
    DASHBOARD_SA_PATTERN,
)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# 1) 기본 상수 검증
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
class TestDefaults:
    def test_default_limit(self):
        assert DEFAULT_LIMIT == 10

    def test_default_tier(self):
        assert DEFAULT_TIER == 3

    def test_default_aging_interval(self):
        assert DEFAULT_AGING_INTERVAL_SEC == 180

    def test_default_aging_min_tier(self):
        assert DEFAULT_AGING_MIN_TIER == 1

    def test_default_dashboard_sa(self):
        assert "tekton-dashboard" in DASHBOARD_SA_PATTERN

    def test_default_tier_rules_order(self):
        tiers = [r['tier'] for r in DEFAULT_TIER_RULES]
        assert tiers == [0, 1, 2, 3]

    def test_default_tier_rules_tier0_is_label_type(self):
        assert DEFAULT_TIER_RULES[0]['matchType'] == 'label'

    def test_default_tier_rules_others_are_env_type(self):
        for rule in DEFAULT_TIER_RULES[1:]:
            assert rule['matchType'] == 'env'


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# 2) determine_tier 티어 결정 로직
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
class TestDetermineTier:
    def test_urgent_label_returns_tier_0(self):
        labels = {"queue.tekton.dev/urgent": "true", "env": "dev"}
        assert determine_tier(labels, DEFAULT_TIER_RULES) == 0

    def test_prod_env_returns_tier_1(self):
        labels = {"env": "prod"}
        assert determine_tier(labels, DEFAULT_TIER_RULES) == 1

    def test_stg_env_returns_tier_2(self):
        labels = {"env": "stg"}
        assert determine_tier(labels, DEFAULT_TIER_RULES) == 2

    def test_dev_env_returns_tier_3(self):
        labels = {"env": "dev"}
        assert determine_tier(labels, DEFAULT_TIER_RULES) == 3

    def test_no_labels_returns_default(self):
        assert determine_tier({}, DEFAULT_TIER_RULES) == DEFAULT_TIER

    def test_wildcard_matches_any_env(self):
        labels = {"env": "random-anything"}
        assert determine_tier(labels, DEFAULT_TIER_RULES) == 3

    def test_first_match_wins(self):
        """urgent 라벨이 있으면 env=prod여도 Tier 0."""
        labels = {"queue.tekton.dev/urgent": "true", "env": "prod"}
        assert determine_tier(labels, DEFAULT_TIER_RULES) == 0

    def test_empty_tier_rules(self):
        assert determine_tier({"env": "prod"}, []) == DEFAULT_TIER

    def test_custom_label_rule(self):
        rules = [{"tier": 5, "matchType": "label",
                  "labelKey": "custom/key", "pattern": "yes"}]
        assert determine_tier({"custom/key": "yes"}, rules) == 5

    def test_label_rule_no_match(self):
        rules = [{"tier": 0, "matchType": "label",
                  "labelKey": "queue.tekton.dev/urgent", "pattern": "true"}]
        labels = {"queue.tekton.dev/urgent": "false"}
        assert determine_tier(labels, rules) == DEFAULT_TIER

    def test_fnmatch_pattern_in_env(self):
        rules = [{"tier": 1, "matchType": "env", "pattern": "prod-*"}]
        assert determine_tier({"env": "prod-kr"}, rules) == 1
        assert determine_tier({"env": "staging"}, rules) == DEFAULT_TIER


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# 3) is_target_namespace 네임스페이스 패턴 매칭
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
class TestIsTargetNamespace:
    def test_cicd_suffix_matches_default(self):
        assert is_target_namespace("myapp-cicd") is True

    def test_non_matching_namespace(self):
        assert is_target_namespace("myapp-deploy") is False

    def test_multiple_patterns(self):
        import src.config as cfg
        cfg.crd_config["namespace_patterns"] = ["*-cicd", "prod-*"]
        assert is_target_namespace("prod-api") is True
        assert is_target_namespace("test-cicd") is True
        assert is_target_namespace("staging") is False

    def test_empty_namespace_string(self):
        assert is_target_namespace("") is False


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# 4) load_crd_config CRD 로드
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
class TestLoadCrdConfig:
    @patch('src.config.api')
    def test_success(self, mock_api):
        mock_api.get_cluster_custom_object.return_value = {
            'spec': {
                'maxPipelines': 20,
                'agingIntervalSec': 300,
                'agingMinTier': 2,
                'namespacePatterns': ['prod-*', '*-cicd'],
                'dashboardSAPattern': 'system:sa:custom',
            }
        }
        result = load_crd_config()
        assert result == 20
        cfg = get_cached_config()
        assert cfg['max_pipelines'] == 20
        assert cfg['aging_interval_sec'] == 300
        assert cfg['aging_min_tier'] == 2
        assert cfg['namespace_patterns'] == ['prod-*', '*-cicd']
        assert cfg['dashboard_sa_pattern'] == 'system:sa:custom'

    @patch('src.config.api')
    def test_missing_optional_fields_uses_defaults(self, mock_api):
        mock_api.get_cluster_custom_object.return_value = {
            'spec': {'maxPipelines': 5}
        }
        result = load_crd_config()
        assert result == 5
        cfg = get_cached_config()
        assert cfg['aging_interval_sec'] == DEFAULT_AGING_INTERVAL_SEC
        assert cfg['aging_min_tier'] == DEFAULT_AGING_MIN_TIER

    @patch('src.config.api')
    def test_api_404_uses_defaults(self, mock_api):
        mock_api.get_cluster_custom_object.side_effect = ApiException(status=404)
        result = load_crd_config()
        assert result == DEFAULT_LIMIT

    @patch('src.config.api')
    def test_api_500_uses_defaults(self, mock_api):
        mock_api.get_cluster_custom_object.side_effect = ApiException(status=500)
        result = load_crd_config()
        assert result == DEFAULT_LIMIT

    @patch('src.config.api')
    def test_generic_exception_uses_defaults(self, mock_api):
        mock_api.get_cluster_custom_object.side_effect = RuntimeError("boom")
        result = load_crd_config()
        assert result == DEFAULT_LIMIT

    @patch('src.config.api')
    def test_empty_namespace_patterns_uses_defaults(self, mock_api):
        mock_api.get_cluster_custom_object.return_value = {
            'spec': {'maxPipelines': 10, 'namespacePatterns': []}
        }
        load_crd_config()
        cfg = get_cached_config()
        assert cfg['namespace_patterns'] == ['*-cicd']

    @patch('src.config.api')
    def test_null_tier_rules_uses_defaults(self, mock_api):
        mock_api.get_cluster_custom_object.return_value = {
            'spec': {'maxPipelines': 10, 'tierRules': None}
        }
        load_crd_config()
        cfg = get_cached_config()
        assert cfg['tier_rules'] == DEFAULT_TIER_RULES

    @patch('src.config.api')
    def test_thread_safety(self, mock_api):
        """여러 스레드에서 동시 호출해도 크래시하지 않아야 한다."""
        import threading
        mock_api.get_cluster_custom_object.return_value = {
            'spec': {'maxPipelines': 15}
        }
        errors = []
        def call():
            try:
                load_crd_config()
                get_cached_config()
            except Exception as e:
                errors.append(e)
        threads = [threading.Thread(target=call) for _ in range(20)]
        for t in threads: t.start()
        for t in threads: t.join()
        assert errors == []
