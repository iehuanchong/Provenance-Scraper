"""
Regression test for the production bug where one transaction's timeout
(after exhausting all retries) crashed the entire discovery run AND
discarded all progress already made on earlier pages, because the whole
multi-page loop lived inside a single uncommitted SQLite transaction.

Verifies:
  1. A tx that raises RuntimeError (simulating exhausted retries) is
     skipped, not fatal -- discovery completes and returns a count.
  2. Loans from transactions BEFORE and AFTER the failing one are both
     committed to the database (proving incremental commits work, not
     just that the function returns without raising).

Run with: python test_discovery_resilience.py
"""

import sys
import tempfile
from unittest.mock import patch

import config

_tmp_db = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
config.DB_PATH = _tmp_db.name

import db
import scrape_originations as so

LOAN_SPEC = config.LOAN_ORIGINATION_SCOPE_SPEC


def _role_granted_event(scope_addr, role, address):
    return {
        "type": "provenance.registry.v1.EventRoleGranted",
        "attributes": [
            {"key": "addresses", "value": f'["{address}"]'},
            {"key": "asset_class_id", "value": f'"{LOAN_SPEC}"'},
            {"key": "nft_id", "value": f'"{scope_addr}"'},
            {"key": "role", "value": f'"{role}"'},
        ],
    }


def _fake_tx_detail(scope_addr, originator):
    return {
        "time": "2026-09-17T12:00:00Z",
        "height": 1000,
        "events": [
            _role_granted_event(scope_addr, "ORIGINATOR", originator),
            _role_granted_event(scope_addr, "SERVICER", "pb1servicer"),
        ],
    }


def test_one_bad_tx_does_not_lose_earlier_or_later_progress():
    db.init_db()

    # Three transactions on one page: good, bad (times out), good again.
    txs_page_1 = [{"txHash": "TX_GOOD_1"}, {"txHash": "TX_BAD"}, {"txHash": "TX_GOOD_2"}]

    def fake_search(from_date, to_date, page=1, count=100):
        if page == 1:
            return {"results": txs_page_1, "pages": 1, "total": 3}
        return {"results": [], "pages": 1, "total": 3}

    def fake_tx_detail(tx_hash):
        if tx_hash == "TX_GOOD_1":
            return _fake_tx_detail("scope_good_1", "pb1originatorA")
        if tx_hash == "TX_BAD":
            raise RuntimeError("Failed to GET ... after 5 attempts")  # simulates exhausted retries
        if tx_hash == "TX_GOOD_2":
            return _fake_tx_detail("scope_good_2", "pb1originatorB")
        raise AssertionError(f"unexpected tx_hash {tx_hash}")

    with patch.object(so.pc, "search_write_scope_txs", side_effect=fake_search), \
         patch.object(so.pc, "get_tx_detail", side_effect=fake_tx_detail):
        inserted = so.discover_originations("2026-09-17", "2026-09-17")

    # The function must complete (not raise) and report the 2 good loans.
    assert inserted == 2, f"expected 2 loans inserted, got {inserted}"

    # And both good loans must actually be committed to the DB --
    # proving this isn't just "didn't crash" but "didn't lose data".
    with db.connect() as conn:
        rows = conn.execute("SELECT scope_addr, originator_address FROM loans ORDER BY scope_addr").fetchall()
        scopes = {r["scope_addr"]: r["originator_address"] for r in rows}

    assert scopes == {
        "scope_good_1": "pb1originatorA",
        "scope_good_2": "pb1originatorB",
    }, f"unexpected DB contents: {scopes}"

    print("test_one_bad_tx_does_not_lose_earlier_or_later_progress: OK")


def main():
    test_one_bad_tx_does_not_lose_earlier_or_later_progress()
    print("\nAll discovery-resilience tests passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
