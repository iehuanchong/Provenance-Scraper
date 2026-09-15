"""
Thin HTTP client for the two Provenance data sources we rely on:

  * service-explorer.provenance.io  -- transaction search / scope lookups
  * api.provenance.io               -- raw chain REST (scope detail, NAV)

Both are public, unauthenticated APIs. This module just adds retry/backoff
and consistent timeouts so the daily job doesn't fall over on a transient
network blip.
"""

import time
import logging
import requests

import config

logger = logging.getLogger("provenance_client")

_session = requests.Session()
_session.headers.update({"User-Agent": "figure-origination-scraper/1.0"})


def _get(url: str, params: dict | None = None) -> dict:
    """GET with retry/backoff. Raises on repeated failure."""
    last_exc = None
    for attempt in range(1, config.MAX_RETRIES + 1):
        try:
            resp = _session.get(
                url, params=params, timeout=config.REQUEST_TIMEOUT_SECONDS
            )
            if resp.status_code == 200:
                time.sleep(config.REQUEST_DELAY_SECONDS)
                return resp.json()
            if resp.status_code in (429, 500, 502, 503, 504):
                wait = config.RETRY_BACKOFF_BASE_SECONDS * (2 ** (attempt - 1))
                logger.warning(
                    "HTTP %s on %s (attempt %d/%d) - retrying in %.1fs",
                    resp.status_code, url, attempt, config.MAX_RETRIES, wait,
                )
                time.sleep(wait)
                continue
            # Non-retryable error (4xx other than 429)
            resp.raise_for_status()
        except requests.RequestException as exc:
            last_exc = exc
            wait = config.RETRY_BACKOFF_BASE_SECONDS * (2 ** (attempt - 1))
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
