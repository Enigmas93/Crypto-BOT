from aegis.utils.backoff import BackoffPolicy


def test_backoff_grows_exponentially_within_jitter():
    policy = BackoffPolicy(base_seconds=1.0, max_seconds=100.0, multiplier=2.0, jitter=0.0)
    delays = [policy.next_delay() for _ in range(5)]
    assert delays == [1.0, 2.0, 4.0, 8.0, 16.0]


def test_backoff_caps_at_max_seconds():
    policy = BackoffPolicy(base_seconds=10.0, max_seconds=15.0, multiplier=3.0, jitter=0.0)
    for _ in range(10):
        delay = policy.next_delay()
        assert delay <= 15.0


def test_backoff_reset_restarts_sequence():
    policy = BackoffPolicy(base_seconds=1.0, max_seconds=100.0, multiplier=2.0, jitter=0.0)
    policy.next_delay()
    policy.next_delay()
    policy.reset()
    assert policy.attempt == 0
    assert policy.next_delay() == 1.0


def test_backoff_jitter_stays_non_negative_and_bounded():
    policy = BackoffPolicy(base_seconds=5.0, max_seconds=5.0, multiplier=2.0, jitter=0.5)
    for _ in range(50):
        delay = policy.next_delay()
        assert 0.0 <= delay <= 7.5
