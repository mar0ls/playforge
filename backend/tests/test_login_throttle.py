"""Failed-login throttling.

The password is one shared secret with no account to lock, so an unthrottled
/login is a guessing oracle for anyone who can reach the port.
"""
from __future__ import annotations

import pytest

from app.core import auth


@pytest.fixture(autouse=True)
def _clean():
    auth.reset_throttle()
    yield
    auth.reset_throttle()


def test_fresh_client_is_not_locked():
    assert auth.lockout_remaining("10.0.0.1") == 0


def test_failures_below_the_limit_do_not_lock():
    for _ in range(auth._MAX_FAILS - 1):
        assert auth.record_failure("10.0.0.1") == 0
    assert auth.lockout_remaining("10.0.0.1") == 0


def test_hitting_the_limit_locks_the_client():
    for _ in range(auth._MAX_FAILS - 1):
        auth.record_failure("10.0.0.1")

    penalty = auth.record_failure("10.0.0.1")

    assert penalty == auth._BASE_LOCKOUT
    assert auth.lockout_remaining("10.0.0.1") > 0


def test_lockout_is_per_client():
    for _ in range(auth._MAX_FAILS):
        auth.record_failure("10.0.0.1")

    assert auth.lockout_remaining("10.0.0.1") > 0
    assert auth.lockout_remaining("10.0.0.2") == 0


def test_repeat_lockouts_back_off_exponentially():
    penalties = []
    for _ in range(3):
        for _ in range(auth._MAX_FAILS):
            p = auth.record_failure("10.0.0.1")
        penalties.append(p)

    assert penalties == [auth._BASE_LOCKOUT, auth._BASE_LOCKOUT * 2, auth._BASE_LOCKOUT * 4]


def test_backoff_is_capped():
    for _ in range(20):
        for _ in range(auth._MAX_FAILS):
            penalty = auth.record_failure("10.0.0.1")

    assert penalty == auth._MAX_LOCKOUT


def test_lockout_expires():
    now = 1000.0
    for _ in range(auth._MAX_FAILS):
        auth.record_failure("10.0.0.1", now=now)

    assert auth.lockout_remaining("10.0.0.1", now=now + auth._BASE_LOCKOUT - 1) > 0
    assert auth.lockout_remaining("10.0.0.1", now=now + auth._BASE_LOCKOUT + 1) == 0


def test_success_clears_the_failure_streak():
    for _ in range(auth._MAX_FAILS - 1):
        auth.record_failure("10.0.0.1")

    auth.record_success("10.0.0.1")

    # The streak is gone, so the next failure starts over rather than locking.
    assert auth.record_failure("10.0.0.1") == 0


def test_stale_failures_fall_out_of_the_window():
    """Slow guessing spread beyond the window must not accumulate into a lockout."""
    now = 1000.0
    for _ in range(auth._MAX_FAILS - 1):
        auth.record_failure("10.0.0.1", now=now)

    later = now + auth._FAIL_WINDOW + 1
    assert auth.record_failure("10.0.0.1", now=later) == 0


def test_tracking_table_is_bounded():
    """Rotating source addresses must not grow the table without limit."""
    now = 1000.0
    for i in range(auth._MAX_TRACKED + 200):
        auth.record_failure(f"10.1.{i // 256}.{i % 256}", now=now + auth._FAIL_WINDOW * 2 + i)

    assert len(auth._attempts) <= auth._MAX_TRACKED + 1
