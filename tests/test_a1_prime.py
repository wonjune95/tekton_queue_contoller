"""
A1′ (disableAdmittedCounter) 에이블레이션 모드 테스트.

disableAdmittedCounter=true 이면 admitted 카운터를 쓰지 않고 running 수만으로 슬롯을 판정한다.
이 분기는 ConfigMap(core_api) 접근 이전에 반환하므로 K8s 모킹 없이 검증 가능하다.
"""
import src.config as config
import src.cache as cache


def _enable_a1_prime(flag: bool):
    config.crd_config["disable_admitted_counter"] = flag


def test_default_flag_is_false():
    assert config.crd_config["disable_admitted_counter"] is False


def test_a1prime_admits_when_running_below_limit():
    _enable_a1_prime(True)
    ok, eff = cache._try_increment_global_admitted(running_cnt=29, limit=30)
    assert ok is True
    assert eff == 29  # admitted 무시 → running 값 그대로


def test_a1prime_blocks_when_running_at_limit():
    _enable_a1_prime(True)
    ok, eff = cache._try_increment_global_admitted(running_cnt=30, limit=30)
    assert ok is False
    assert eff == 30


def test_a1prime_ignores_admitted_counter():
    """카운터가 증가하지 않아야 한다(격리)."""
    _enable_a1_prime(True)
    cache.webhook_admitted_count = 0
    for _ in range(5):
        cache._try_increment_global_admitted(running_cnt=0, limit=30)
    # A1′ 경로는 카운터를 건드리지 않는다
    assert cache.webhook_admitted_count == 0


def test_crd_load_parses_disable_flag(monkeypatch):
    """CRD spec 의 disableAdmittedCounter 가 crd_config 로 반영되는지."""
    fake_obj = {"spec": {"maxPipelines": 30, "disableAdmittedCounter": True}}
    monkeypatch.setattr(config.api, "get_cluster_custom_object",
                        lambda *a, **k: fake_obj)
    config.load_crd_config()
    assert config.get_cached_config()["disable_admitted_counter"] is True
