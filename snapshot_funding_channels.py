"""
Funding-channel snapshot job.

Separate from the per-loan/per-originator pipeline: pulls the FIGR_HELOC
rollup scope's single "token" record, which Figure's own oracle process
(`connect-oracle`) updates periodically with an aggregate JSON summary:

    {
      "supply": ..., "account_count": ..., "structures_count": ...,
      "total_securitization": ..., "total_participation": ...,
      "total_warehoused": ..., "total_pools": ..., "total_unstructured": ...,
      "as_of_time": <epoch seconds>
    }

This is cheap (one API call) and gives the funding-structure breakdown
(securitization / participation / warehoused / pooled / unstructured)
independent of the per-loan originator data.
"""

import json
import logging
import datetime as dt

import config
import db
import provenance_client as pc

logger = logging.getLogger("snapshot_funding_channels")


def _extract_token_json(scope_detail: dict) -> dict | None:
    for record in scope_detail.get("records", []):
        rec = record.get("record", {})
        if rec.get("name") != "token":
            continue
        outputs = rec.get("outputs", [])
        if not outputs:
            continue
        raw = outputs[0].get("hash")
        try:
            return json.loads(raw)
        except (TypeError, json.JSONDecodeError):
            logger.warning("Could not parse token record JSON: %r", raw)
            return None
    return None


def compute_securitization_split(row: dict) -> dict:
    """
    Derives a securitized-vs-whole-loan split from the five raw funding
    channels Figure publishes on the FIGR_HELOC rollup.

    IMPORTANT CAVEAT: Figure hasn't published an official definition of
    these five categories anywhere we could find, so this mapping is our
    best industry-standard-terminology read, not confirmed ground truth:

      securitized_usd  = total_securitization
                          (loans pooled into a trust, securities issued
                          against them -- the classic "securitization" bucket)

      whole_loan_usd    = total_participation + total_warehoused
                           + total_pools + total_unstructured
                          (everything NOT securitized: sold/held as whole
                          loans, participated out to investors, sitting in
                          a warehouse facility, or not yet allocated to any
                          structure -- i.e. still on-balance-sheet in some
                          form rather than off-balance-sheet via securitization)

    If you get an authoritative breakdown from Figure/debloc later, revise
    this function rather than the raw stored values -- the five raw
    columns are untouched in the DB either way.
    """
    securitized = row.get("total_securitization") or 0
    whole_loan = (
        (row.get("total_participation") or 0)
        + (row.get("total_warehoused") or 0)
        + (row.get("total_pools") or 0)
        + (row.get("total_unstructured") or 0)
    )
    total = securitized + whole_loan
    securitized_share = (securitized / total) if total else None

    return {
        "securitized_usd": securitized,
        "whole_loan_usd": whole_loan,
        "securitized_share": securitized_share,
    }


def snapshot_funding_channels() -> bool:
    """Pulls today's FIGR_HELOC rollup snapshot and stores it.
    Returns True if a snapshot was recorded."""
    scope_detail = pc.get_scope_detail(
        config.FIGR_HELOC_ROLLUP_SCOPE, include_records=True, include_sessions=False
    )
    payload = _extract_token_json(scope_detail)
    if payload is None:
        logger.error("Failed to extract funding-channel payload from FIGR_HELOC scope")
        return False

    today = dt.date.today().isoformat()
    now_iso = dt.datetime.now(dt.timezone.utc).isoformat()

    with db.connect() as conn:
        db.upsert_funding_snapshot(
            conn,
            snapshot_date=today,
            as_of_time=payload.get("as_of_time"),
            supply=payload.get("supply"),
            account_count=payload.get("account_count"),
            structures_count=payload.get("structures_count"),
            total_securitization=payload.get("total_securitization"),
            total_participation=payload.get("total_participation"),
            total_warehoused=payload.get("total_warehoused"),
            total_pools=payload.get("total_pools"),
            total_unstructured=payload.get("total_unstructured"),
            scraped_at=now_iso,
        )

    logger.info("Funding-channel snapshot recorded for %s", today)
    return True
