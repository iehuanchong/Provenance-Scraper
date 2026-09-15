"""
Daily discovery job.

Walks service-explorer's txs/recent (module=nft, msgType=write_scope) for
a given date window, pulls full detail for each transaction, and extracts
every loan origination event from the transaction's event log.

Why we parse events instead of guessing from msgCount:
Each individual loan produces a `provenance.registry.v1.EventRoleGranted`
event with role="ORIGINATOR" and the loan's scope id (nft_id). A single
transaction can (rarely) bundle more than one loan, so we key off these
events directly rather than assuming 1 tx = 1 loan.

We also filter on asset_class_id == LOAN_ORIGINATION_SCOPE_SPEC so we
only capture genuine Figure loan originations, in case the general
nft-module write_scope stream ever includes unrelated scope types.
"""

import json
import logging
import datetime as dt

import config
import db
import provenance_client as pc

logger = logging.getLogger("scrape_originations")


def _event_attrs(event: dict) -> dict:
    """Flatten an event's attribute list into a {key: value} dict,
    stripping the surrounding quotes the chain wraps string values in."""
    out = {}
    for attr in event.get("attributes", []):
        key = attr.get("key")
        val = attr.get("value")
        if isinstance(val, str) and val.startswith('"') and val.endswith('"'):
            val = val[1:-1]
        out[key] = val
    return out


def parse_loans_from_tx(tx_detail: dict) -> list[dict]:
    """
    Returns a list of loan dicts extracted from one transaction's event log:
        {scope_addr, originator_address, servicer_address}
    Only includes scopes registered under LOAN_ORIGINATION_SCOPE_SPEC.
    """
    loans: dict[str, dict] = {}

    for event in tx_detail.get("events", []):
        if event.get("type") != "provenance.registry.v1.EventRoleGranted":
            continue
        attrs = _event_attrs(event)
        asset_class_id = attrs.get("asset_class_id")
        if asset_class_id != config.LOAN_ORIGINATION_SCOPE_SPEC:
            continue

        scope_addr = attrs.get("nft_id")
        role = attrs.get("role")
        try:
            addresses = json.loads(attrs.get("addresses", "[]"))
        except (TypeError, json.JSONDecodeError):
            addresses = []
        address = addresses[0] if addresses else None

        loan = loans.setdefault(
            scope_addr, {"scope_addr": scope_addr, "originator_address": None,
                         "servicer_address": None}
        )
        if role == "ORIGINATOR":
            loan["originator_address"] = address
        elif role == "SERVICER":
            loan["servicer_address"] = address

    # Only keep entries that actually got an originator (defensive)
    return [loan for loan in loans.values() if loan["originator_address"]]


def discover_originations(from_date: str, to_date: str) -> int:
    """
    Scrapes all loan-origination write_scope transactions in
    [from_date, to_date] (inclusive, 'YYYY-MM-DD' UTC) and stores them.
    Returns the number of new loan rows inserted.
    """
    inserted = 0
    page = 1
    total_pages = None

    with db.connect() as conn:
        while True:
            resp = pc.search_write_scope_txs(from_date, to_date, page=page)
            results = resp.get("results", [])
            total_pages = resp.get("pages", 1)
            logger.info("Page %s/%s - %d txs", page, total_pages, len(results))

            for tx_summary in results:
                tx_hash = tx_summary["txHash"]
                # De-dup happens per-scope via ON CONFLICT DO NOTHING in
                # db.upsert_loan, so it's safe to re-scan overlapping
                # date windows (e.g. when catching up after a missed run).
                tx_detail = pc.get_tx_detail(tx_hash)
                loans = parse_loans_from_tx(tx_detail)
                if not loans:
                    continue

                block_time = tx_detail.get("time")
                block_height = tx_detail.get("height")

                for loan in loans:
                    db.upsert_loan(
                        conn,
                        scope_addr=loan["scope_addr"],
                        tx_hash=tx_hash,
                        block_height=block_height,
                        block_time=block_time,
                        originator_address=loan["originator_address"],
                        servicer_address=loan["servicer_address"],
                        contract_spec_name=None,   # enriched separately, see enrich_loan_classes.py
                        loan_class=None,
                        discovered_at=dt.datetime.now(dt.timezone.utc).isoformat(),
                    )
                    inserted += 1

            if page >= total_pages:
                break
            page += 1

        db.set_state(conn, "last_discovered_to_date", to_date)

    logger.info("Discovery complete: %d loan rows inserted for %s..%s",
                inserted, from_date, to_date)
    return inserted
