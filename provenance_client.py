"""
Thin HTTP client for the two Provenance data sources we rely on:

  * service-explorer.provenance.io  -- transaction search / scope lookups
  * api.provenance.io               -- raw chain REST (scope detail, NAV, ledger)

Both are public, unauthenticated APIs, with NO documented rate limit --
but api.provenance.io turned out, empirically, to have a much stricter
real limit than service-explorer.provenance.io. In a real run, 1,200+
sequential calls to service-explorer at 0.25s pacing produced zero 429s,
while api.provenance.io started 429-ing almost every single request at
that same pacing once enrichment and rate-refresh both hammered it
back-to-back for hundreds of loans.

So this client:
  1. Uses a much larger minimum delay between requests to api.provenance.io
     specifically (config.CHAIN_API_REQUEST_DELAY_SECONDS) than to
     service-explorer (config.SERVICE_EXPLORER_REQUEST_DELAY_SECONDS).
  2. Honors a `Retry-After` header exactly, if the server sends one,
     instead of guessing at a backoff schedule.
  3. Tracks a shared per-host "cooldown until" timestamp. A 429 on ONE
     resource pushes that cooldown forward for ALL subsequent requests
     to that host, not just retries of the same URL -- this is what was
     missing before: we were backing off per-resource while immediately
     hammering the very next, different resource at full speed.
"""

import time
import logging
from urllib.parse import urlparse

import requests

import config

logger = logging.getLogger("provenance_client")

_session = requests.Session()
_session.headers.update({"User-Agent": "figure-origination-scraper/1.0"})

# host -> epoch timestamp of last request sent to it
_last_request_time: dict[str, float] = {}
# host -> epoch timestamp before which we should not send ANY request
# to that host (set forward whenever that host 429s us)
_cooldown_until: dict[str, float] = {}
# host -> current adaptive minimum delay (starts at the config default,
# escalates on 429s, eases back down after a run of clean successes).
# We don't actually know api.provenance.io's real limit -- this lets the
# client find a working rate empirically within a run rather than
# guessing a fixed number that might still be too aggressive.
_current_delay: dict[str, float] = {}
_consecutive_successes: dict[str, int] = {}
_MAX_ADAPTIVE_DELAY_SECONDS = 60.0
_EASE_DOWN_AFTER_SUCCESSES = 20
_EASE_DOWN_FACTOR = 0.7  # multiply delay by this after enough clean successes


def _host_of(url: str) -> str:
    return urlparse(url).netloc


def _base_delay_for(host: str) -> float:
    if host == urlparse(config.CHAIN_API_BASE).netloc:
        return config.CHAIN_API_REQUEST_DELAY_SECONDS
    return config.SERVICE_EXPLORER_REQUEST_DELAY_SECONDS


def _current_delay_for(host: str) -> float:
    return _current_delay.setdefault(host, _base_delay_for(host))


def _escalate_delay(host: str):
    base = _base_delay_for(host)
    current = _current_delay_for(host)
    _current_delay[host] = min(max(current, base) * 2, _MAX_ADAPTIVE_DELAY_SECONDS)
    _consecutive_successes[host] = 0
    logger.info("Escalated %s request delay to %.1fs after a 429", host, _current_delay[host])


def _note_success(host: str):
    base = _base_delay_for(host)
    current = _current_delay_for(host)
    if current <= base:
        return
    _consecutive_successes[host] = _consecutive_successes.get(host, 0) + 1
    if _consecutive_successes[host] >= _EASE_DOWN_AFTER_SUCCESSES:
        new_delay = max(base, current * _EASE_DOWN_FACTOR)
        if new_delay < current:
            logger.info("Easing %s request delay back to %.1fs after %d clean requests",
                        host, new_delay, _consecutive_successes[host])
        _current_delay[host] = new_delay
        _consecutive_successes[host] = 0


def _wait_for_host(host: str):
    """Blocks until it's this host's turn, respecting both the current
    adaptive per-host pacing and any active cooldown from a recent 429."""
    now = time.monotonic()

    cooldown = _cooldown_until.get(host, 0)
    if cooldown > now:
        time.sleep(cooldown - now)
        now = time.monotonic()

    min_delay = _current_delay_for(host)
    last = _last_request_time.get(host, 0)
    elapsed = now - last
    if elapsed < min_delay:
        time.sleep(min_delay - elapsed)


def _record_request(host: str):
    _last_request_time[host] = time.monotonic()


def _apply_cooldown(host: str, seconds: float):
    _cooldown_until[host] = time.monotonic() + seconds
    logger.info("Cooling down all requests to %s for %.1fs", host, seconds)


def _get(url: str, params: dict | None = None) -> dict:
    """GET with per-host pacing, Retry-After support, and retry/backoff.
    Raises RuntimeError on repeated failure."""
    host = _host_of(url)
    last_exc = None

    for attempt in range(1, config.MAX_RETRIES + 1):
        _wait_for_host(host)
        try:
            resp = _session.get(
                url, params=params, timeout=config.REQUEST_TIMEOUT_SECONDS
            )
            _record_request(host)

            if resp.status_code == 200:
                _note_success(host)
                return resp.json()

            if resp.status_code in (429, 500, 502, 503, 504):
                retry_after = resp.headers.get("Retry-After")
                if retry_after is not None:
                    try:
                        wait = float(retry_after)
                    except ValueError:
                        wait = config.RETRY_BACKOFF_BASE_SECONDS * (2 ** (attempt - 1))
                else:
                    wait = min(
                        config.RETRY_BACKOFF_BASE_SECONDS * (2 ** (attempt - 1)),
                        config.MAX_BACKOFF_SECONDS,
                    )

                logger.warning(
                    "HTTP %s on %s (attempt %d/%d) - retrying in %.1fs%s",
                    resp.status_code, url, attempt, config.MAX_RETRIES, wait,
                    " (server-specified via Retry-After)" if retry_after else "",
                )

                if resp.status_code == 429:
                    # A 429 means this whole host is telling us to slow
                    # down, not just this one resource -- apply the
                    # cooldown globally AND permanently raise our
                    # baseline pacing for this host, so the NEXT request
                    # (likely a different scope/loan entirely) doesn't
                    # immediately get throttled too.
                    _escalate_delay(host)
                    _apply_cooldown(host, wait)
                else:
                    time.sleep(wait)
                continue

            # Non-retryable error (4xx other than 429)
            resp.raise_for_status()
        except requests.RequestException as exc:
            last_exc = exc
            wait = min(
                config.RETRY_BACKOFF_BASE_SECONDS * (2 ** (attempt - 1)),
                config.MAX_BACKOFF_SECONDS,
            )
            logger.warning(
                "Request error on %s (attempt %d/%d): %s - retrying in %.1fs",
                url, attempt, config.MAX_RETRIES, exc, wait,
            )
            time.sleep(wait)

    raise RuntimeError(f"Failed to GET {url} after {config.MAX_RETRIES} attempts") from last_exc


# --- service-explorer.provenance.io ----------------------------------------

def search_write_scope_txs(from_date: str, to_date: str, page: int = 1,
                            count: int = config.TX_PAGE_SIZE) -> dict:
    """
    Search recent nft-module write_scope transactions in a date window.

    from_date / to_date: 'YYYY-MM-DD' strings (UTC).
    Returns the raw paginated response: {"pages", "results", "total", ...}
    """
    url = f"{config.SERVICE_EXPLORER_BASE}/txs/recent"
    params = {
        "module": "nft",
        "msgType": "write_scope",
        "fromDate": from_date,
        "toDate": to_date,
        "page": page,
        "count": count,
    }
    return _get(url, params)


def get_tx_detail(tx_hash: str) -> dict:
    """Full transaction detail, including the event log we parse for
    scope/originator/servicer info."""
    url = f"{config.SERVICE_EXPLORER_BASE}/txs/{tx_hash}"
    return _get(url)


# --- api.provenance.io (raw chain REST) -------------------------------------

def get_net_asset_values(scope_addr: str) -> list[dict]:
    """
    Returns the list of NAV entries for a scope, e.g.:
      [{"price": {"denom": "usd", "amount": "200000"}, "updated_block_height": "...", "volume": "1"}]
    Empty list if the loan hasn't been NAV'd yet (typically pre-funding).
    """
    url = f"{config.CHAIN_API_BASE}/provenance/metadata/v1/netassetvalues/{scope_addr}"
    data = _get(url)
    return data.get("net_asset_values", [])


def get_ledger(scope_addr: str,
                asset_class_id: str = config.LOAN_ORIGINATION_SCOPE_SPEC) -> dict | None:
    """
    Returns the `ledger` module's servicing record for a loan scope, e.g.:
      {
        "key": {...}, "ledger_class_id": "figure-servicing-1-0",
        "status_type_id": 1, "next_pmt_date": 0, "next_pmt_amt": "0",
        "interest_rate": 9300000,   # raw units; divide by 1,000,000 for percent
        "maturity_date": 0,
        "interest_day_count_convention": "DAY_COUNT_CONVENTION_ACTUAL_ACTUAL",
        "interest_accrual_method": "INTEREST_ACCRUAL_METHOD_SIMPLE_INTEREST",
        "payment_frequency": "PAYMENT_FREQUENCY_MONTHLY"
      }
    Unlike NAV, this is created in the same transaction as the loan scope
    itself (MsgCreateLedgerRequest), so it should be available immediately
    -- no funding lag to wait out. Returns None if no ledger is found.
    """
    url = f"{config.CHAIN_API_BASE}/provenance/ledger/v1/config/{asset_class_id}/{scope_addr}"
    data = _get(url)
    return data.get("ledger")


def get_scope_detail(scope_addr: str, include_records: bool = True,
                      include_sessions: bool = True) -> dict:
    url = f"{config.CHAIN_API_BASE}/provenance/metadata/v1/scope/{scope_addr}"
    params = {
        "include_records": str(include_records).lower(),
        "include_sessions": str(include_sessions).lower(),
    }
    return _get(url, params)
