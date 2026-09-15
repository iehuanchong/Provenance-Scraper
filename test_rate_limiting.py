"""
Offline test for the adaptive per-host rate limiting in
provenance_client.py, using a mocked requests.Session so no real
network calls happen. Verifies:

  1. A 429 with a Retry-After header is honored exactly.
  2. A 429 escalates the host's baseline delay for SUBSEQUENT, different
     requests too (not just retries of the same URL) -- this was the
     actual bug we hit in production: per-resource backoff wasn't
     slowing down the next, different resource at all.
  3. A successful request after enough clean requests eases the delay
     back down.

Run with: python test_rate_limiting.py
"""

import sys
import time
from unittest.mock import MagicMock

import config
import provenance_client as pc


class FakeResponse:
    def __init__(self, status_code, json_data=None, headers=None):
        self.status_code = status_code
        self._json_data = json_data or {}
        self.headers = headers or {}

    def json(self):
        return self._json_data

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


def _reset_client_state():
    pc._last_request_time.clear()
    pc._cooldown_until.clear()
    pc._current_delay.clear()
    pc._consecutive_successes.clear()


def test_retry_after_is_honored():
    _reset_client_state()
    config.MAX_RETRIES = 3

    responses = [
        FakeResponse(429, headers={"Retry-After": "0.05"}),  # short, so test stays fast
        FakeResponse(200, json_data={"ok": True}),
    ]
    pc._session.get = MagicMock(side_effect=responses)

    start = time.monotonic()
    result = pc._get("https://api.provenance.io/some/path")
    elapsed = time.monotonic() - start

    assert result == {"ok": True}
    assert elapsed >= 0.05, f"expected to wait at least the Retry-After value, waited {elapsed:.3f}s"
    print("test_retry_after_is_honored: OK")


def test_429_escalates_delay_for_next_different_request():
    """
    This is the actual production bug: getting throttled on scope A
    should make us slow down before requesting scope B too, not just
    before retrying scope A.
    """
    _reset_client_state()
    config.MAX_RETRIES = 2
    config.CHAIN_API_REQUEST_DELAY_SECONDS = 0.02  # tiny, so the test is fast
    config.MAX_BACKOFF_SECONDS = 0.05

    host = "api.provenance.io"
    assert pc._current_delay_for(host) == config.CHAIN_API_REQUEST_DELAY_SECONDS

    # First call: 429 with no Retry-After header -> falls back to our
    # own capped backoff, and should escalate the host's baseline delay.
    pc._session.get = MagicMock(side_effect=[
        FakeResponse(429),  # attempt 1
        FakeResponse(200, json_data={"scope": "A"}),  # attempt 2, succeeds
    ])
    result_a = pc._get(f"https://{host}/scope/A")
    assert result_a == {"scope": "A"}

    escalated_delay = pc._current_delay_for(host)
    assert escalated_delay > config.CHAIN_API_REQUEST_DELAY_SECONDS, (
        "expected the 429 to raise the host's baseline delay for future requests"
    )

    # Second call, a DIFFERENT resource, succeeds on the first attempt --
    # but it should still have been paced by the escalated delay, not the
    # original tiny baseline.
    pc._session.get = MagicMock(side_effect=[
        FakeResponse(200, json_data={"scope": "B"}),
    ])
    start = time.monotonic()
    result_b = pc._get(f"https://{host}/scope/B")
    elapsed = time.monotonic() - start

    assert result_b == {"scope": "B"}
    # We can't assert exact timing (cooldown from call A may already have
    # elapsed), but the escalated delay should still be in effect.
    assert pc._current_delay_for(host) == escalated_delay

    print("test_429_escalates_delay_for_next_different_request: OK")


def test_delay_eases_back_down_after_clean_streak():
    _reset_client_state()
    host = "api.provenance.io"
    config.CHAIN_API_REQUEST_DELAY_SECONDS = 0.01

    # Manually put the host into an escalated state, as if a 429 happened earlier.
    pc._current_delay[host] = 0.08

    pc._session.get = MagicMock(return_value=FakeResponse(200, json_data={"ok": True}))

    for _ in range(pc._EASE_DOWN_AFTER_SUCCESSES):
        pc._get(f"https://{host}/whatever")

    eased_delay = pc._current_delay_for(host)
    assert eased_delay < 0.08, f"expected delay to ease down from 0.08, got {eased_delay}"
    print("test_delay_eases_back_down_after_clean_streak: OK")


def main():
    test_retry_after_is_honored()
    test_429_escalates_delay_for_next_different_request()
    test_delay_eases_back_down_after_clean_streak()
    print("\nAll rate-limiting tests passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
